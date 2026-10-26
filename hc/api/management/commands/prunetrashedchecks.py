from __future__ import annotations

from argparse import ArgumentParser
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils.timezone import now

from hc.api.models import Check, TrashBatchOperation


class Command(BaseCommand):
    help = "Permanently delete checks whose trash retention period has expired."

    BATCH_SIZE = 500

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show how many expired checks would be deleted, delete nothing",
        )

    def handle(self, *, dry_run: bool = False, **options: Any) -> str:
        cutoff = now() - settings.TRASH_RETENTION
        qs = Check.all_objects.filter(deleted_at__lt=cutoff).order_by("id")

        total = qs.count()
        if dry_run:
            self.stdout.write(
                f"Would permanently delete {total} expired trashed check(s)."
            )
            return "Dry run complete."

        deleted_checks = 0
        while True:
            ids = list(qs.values_list("id", flat=True)[: self.BATCH_SIZE])
            if not ids:
                break
            Check.all_objects.filter(id__in=ids).delete()
            deleted_checks += len(ids)

        # Remove finished idempotency records from the same retention window:
        deleted_ops = TrashBatchOperation.objects.filter(completed__lt=cutoff).delete()[
            0
        ]

        self.stdout.write(
            f"Permanently deleted {deleted_checks} expired trashed check(s) "
            f"({total} found) and {deleted_ops} batch operation record(s)."
        )
        return "Done!"
