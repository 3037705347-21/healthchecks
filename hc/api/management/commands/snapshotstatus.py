from __future__ import annotations

from argparse import ArgumentParser
from typing import Any
from uuid import UUID

from django.core.management.base import BaseCommand
from django.db import DatabaseError

from hc.accounts.models import Project
from hc.api.snapshots import ProjectStatusUndeterminable, sync_project


class Command(BaseCommand):
    help = """Maintain per-project status snapshots.

    For every project it replays flip state changes not consumed yet (filling
    history gaps after downtime or a restart) and then records the current
    overall status. The command is safe to run repeatedly: it resumes at each
    project's cursor, only appends missing snapshots, and never duplicates or
    modifies existing ones.
    """

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--project",
            type=UUID,
            default=None,
            help="Process only the project with the given code",
        )

    def handle(self, project: UUID | None, **options: Any) -> None:
        q = Project.objects.order_by("id")
        if project is not None:
            q = q.filter(code=project)

        for current in q:
            try:
                processed, _snapshot = sync_project(current)
            except ProjectStatusUndeterminable as e:
                # Stop this project at its last confirmed snapshot. Its cursor
                # is untouched, so a subsequent run fills the gap automatically.
                self.stderr.write(
                    f"{current.code}: cannot determine status ({e}); "
                    f"stopped at the last confirmed snapshot\n"
                )
                continue
            except DatabaseError as e:
                # The write failed; the transaction was rolled back and the
                # cursor was not advanced. Resume from here on the next run.
                self.stderr.write(
                    f"{current.code}: snapshot write failed ({e}); "
                    f"stopped at the last confirmed snapshot\n"
                )
                continue

            self.stdout.write(
                f"{current.code}: up to date, {processed} flip(s) replayed\n"
            )
