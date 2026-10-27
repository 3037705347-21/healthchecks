from __future__ import annotations

import uuid
from datetime import timedelta as td
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from hc.api.models import Check, RebuildProgress, RebuildResult


class Command(BaseCommand):
    help = """Rebuild a check's current status and status-change (Flip) records
from its ping sequence. Intended for recovering checks whose state no longer
matches the ping log, for example after a database migration.

The rebuild only settles history older than a safety margin (default 5
minutes), so heartbeats arriving while it is running are never overwritten.
Rebuilt flips are marked as processed and never produce alerts for the past.
The command can be re-run; each run continues from the last confirmed
position and converges further.
"""

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "codes",
            nargs="*",
            help="Check code(s) to rebuild. If omitted, use --all.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            dest="all_checks",
            help="Rebuild every check in the database.",
        )
        parser.add_argument(
            "--margin",
            type=int,
            default=300,
            help="Safety margin in seconds; do not settle events newer than "
            "now minus margin (default: 300).",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=1000,
            help="Maximum number of pings to replay per pass (default: 1000).",
        )
        parser.add_argument(
            "--max-passes",
            type=int,
            default=100,
            help="Maximum number of passes per check (default: 100).",
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Discard the saved rebuild checkpoint and start over.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            dest="dry_run",
            help="Run a single pass and roll it back; do not change any data.",
        )

    def handle(self, *args: Any, **options: Any) -> str:
        codes: list[str] = options["codes"]
        if not codes and not options["all_checks"]:
            raise CommandError("Please provide check code(s), or use --all.")
        if codes and options["all_checks"]:
            raise CommandError("Please specify either check code(s) or --all, not both.")

        checks_q = Check.objects.order_by("id")
        if codes:
            parsed: list[uuid.UUID] = []
            for code in codes:
                try:
                    parsed.append(uuid.UUID(code))
                except ValueError:
                    raise CommandError(f"Not a valid check code: {code}")
            checks_q = checks_q.filter(code__in=parsed)
            if checks_q.count() != len(set(parsed)):
                raise CommandError("One or more check codes were not found.")

        margin = td(seconds=options["margin"])
        lines = []
        for check in checks_q:
            lines.append(
                self.process_check(
                    check,
                    margin=margin,
                    batch_size=options["batch_size"],
                    max_passes=options["max_passes"],
                    reset=options["reset"],
                    dry_run=options["dry_run"],
                )
            )

        return "\n".join(lines)

    def process_check(
        self,
        check: Check,
        *,
        margin: td,
        batch_size: int,
        max_passes: int,
        reset: bool,
        dry_run: bool,
    ) -> str:
        if reset and not dry_run:
            RebuildProgress.objects.filter(owner_id=check.id).delete()

        last = RebuildResult(stop_reason="not_started")
        passes = 1 if dry_run else max_passes
        for _ in range(passes):
            try:
                # Each pass is a separate transaction. The row lock is
                # released between passes so live heartbeats can get through.
                with transaction.atomic():
                    locked = Check.objects.select_for_update().get(id=check.id)
                    if reset and dry_run:
                        RebuildProgress.objects.filter(owner_id=locked.id).delete()
                    last = locked.rebuild_state(margin=margin, batch_size=batch_size)
                    if dry_run:
                        # Throw away everything the pass wrote.
                        transaction.set_rollback(True)
            except Check.DoesNotExist:  # pragma: no cover
                # The check got deleted between the outer query and the lock.
                break

            self.report(check, last)

            if last.completed or last.stop_reason or not last.processed:
                break

        if dry_run:
            prefix = "[dry-run] "
        elif last.completed:
            prefix = "[completed] "
        elif last.stop_reason:
            prefix = f"[stopped: {last.stop_reason}] "
        else:
            prefix = "[in-progress] "

        return (
            f"{prefix}{check.code}: pings={last.processed}, "
            f"flips +{last.flips_created}/-{last.flips_removed}"
        )

    def report(self, check: Check, r: RebuildResult) -> None:
        tail = f"stop={r.stop_reason}" if r.stop_reason else "ok"
        self.stdout.write(
            f"{check.code}: processed {r.processed} ping(s), "
            f"flips +{r.flips_created}/-{r.flips_removed}, {tail}"
        )
