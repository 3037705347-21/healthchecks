from __future__ import annotations

import uuid
from argparse import ArgumentParser
from datetime import datetime
from datetime import timezone as dt_timezone
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime
from django.utils.timezone import is_aware, make_aware

from hc.api.models import Check, RedactionJob
from hc.api.redaction import RedactionFailure, process_job


def parse_dt(value: str) -> datetime:
    dt = parse_datetime(value)
    if dt is None:
        raise CommandError(f"Invalid datetime: {value}")
    if not is_aware(dt):
        dt = make_aware(dt, dt_timezone.utc)
    return dt


class Command(BaseCommand):
    help = "Redact sensitive fragments from historical ping bodies."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--check",
            type=uuid.UUID,
            action="append",
            dest="checks",
            help="Check code to include. Can be specified multiple times.",
        )
        parser.add_argument("--start", type=parse_dt, help="Start of time range.")
        parser.add_argument("--end", type=parse_dt, help="End of time range.")
        parser.add_argument(
            "--job",
            type=int,
            help="Resume an existing redaction job by its ID.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Maximum number of ping bodies to process in this run.",
        )

    def handle(self, **options: Any) -> str:
        if options["job"] is not None:
            try:
                job = RedactionJob.objects.get(id=options["job"])
            except RedactionJob.DoesNotExist:
                raise CommandError(f"Redaction job not found: {options['job']}")
        else:
            codes = options["checks"]
            if not codes:
                raise CommandError("Please specify at least one --check or --job.")

            checks = list(Check.objects.filter(code__in=codes))
            found = {c.code for c in checks}
            for code in codes:
                if code not in found:
                    raise CommandError(f"Check not found: {code}")

            job = RedactionJob.objects.create(
                start=options["start"], end=options["end"]
            )
            job.checks.set(checks)

        try:
            processed, changed = process_job(job, limit=options["limit"])
        except RedactionFailure as e:
            raise CommandError(str(e))

        return (
            f"Job {job.id}: status={job.status}, "
            f"processed={processed}, changed={changed}, cursor={job.cursor}"
        )
