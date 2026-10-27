"""Batch redaction of historical ping bodies.

Ping bodies live either in the database (``Ping.body_raw``) or in the
configured object store (``Ping.object_size`` + an S3 object). This module
applies one shared set of redaction rules (:class:`RedactionRule`) to ping
bodies within a check/time scope, and writes the redacted result back to the
*same* location, so every existing read path (web UI and API) serves the
redacted bytes without any other changes.

Guarantees:

* Rules operate on raw bytes, so body fragments which no rule matches stay
  byte-for-byte unchanged. The same input always produces the same redacted
  output, whether the body is stored in the database or in object storage.
* A ping is only marked as processed (:class:`RedactionState`) after its body
  has been both read and written back successfully. A read or write failure
  aborts the job and leaves the ping unconfirmed.
* Jobs remember the last confirmed event (cursor). After an interruption the
  next run resumes from the failed event; events already confirmed for the
  current rule set are never re-processed. When rules are added or modified
  the rule-set fingerprint changes, and only content not yet processed with
  the new rule set is picked up again.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from django.db import DatabaseError, transaction
from django.db.models import QuerySet
from django.utils.timezone import now

from hc.api.models import Ping, RedactionJob, RedactionRule, RedactionState
from hc.lib.s3 import put_object

if TYPE_CHECKING:
    from hc.api.models import Check

logger = logging.getLogger(__name__)

CompiledRule = tuple[re.Pattern[bytes], bytes]


class RedactionFailure(Exception):
    """Raised when a ping body could not be read or written back.

    The ping is intentionally left without a RedactionState row, so it gets
    retried on the next run.
    """

    def __init__(self, ping_id: int, operation: str, original: Exception):
        self.ping_id = ping_id
        self.operation = operation
        self.original = original
        super().__init__(
            f"failed to {operation} body of ping {ping_id}: {original!r}"
        )


def redact_bytes(data: bytes, rules: Sequence[CompiledRule]) -> bytes:
    """Apply all rules to raw body bytes and return the result."""
    for pattern, replacement in rules:
        data = pattern.sub(replacement, data)
    return data


def _confirm(ping_id: int, fingerprint: str) -> None:
    """Record that the ping's body now reflects the current rule set."""
    RedactionState.objects.update_or_create(
        ping_id=ping_id,
        defaults={"fingerprint": fingerprint, "updated": now()},
    )


def process_ping(
    ping: Ping,
    fingerprint: str | None = None,
    rules: Sequence[CompiledRule] | None = None,
) -> str:
    """Read, redact and write back a single ping's body.

    Returns "changed" if the body was rewritten, "unchanged" if no rule
    matched, and "empty" if the ping has no body. Raises RedactionFailure if
    the body could not be read or written back; in that case no processing
    state is recorded.
    """
    if fingerprint is None:
        fingerprint = RedactionRule.fingerprint()
    if rules is None:
        rules = RedactionRule.compiled()

    try:
        raw = ping.get_body_bytes()
    except Ping.GetBodyError as e:
        raise RedactionFailure(ping.id, "read", e) from e

    if not raw:
        # No body stored: nothing to read back or rewrite, simply confirm
        # the event so subsequent runs skip it.
        _confirm(ping.id, fingerprint)
        return "empty"

    redacted = redact_bytes(raw, rules)

    in_object_store = bool(ping.object_size and ping.n)

    if redacted != raw:
        if in_object_store:
            # Upload to object storage first, outside the DB transaction:
            # a failed upload must not leave a confirmation behind.
            assert ping.n is not None
            try:
                put_object(ping.owner.code, ping.n, redacted)
            except Exception as e:
                raise RedactionFailure(ping.id, "write", e) from e

        # Confirm the write and the new state atomically. If this transaction
        # fails, the next run re-processes the ping; byte-level redaction
        # with fixed replacement tokens is idempotent and yields the same
        # result.
        try:
            with transaction.atomic():
                if in_object_store:
                    Ping.objects.filter(id=ping.id).update(
                        object_size=len(redacted)
                    )
                else:
                    Ping.objects.filter(id=ping.id).update(body_raw=redacted)
                _confirm(ping.id, fingerprint)
        except DatabaseError as e:
            raise RedactionFailure(ping.id, "write", e) from e

        return "changed"

    # Content did not match any rule: do not touch the stored body, only
    # record that the current rule set has been applied to this ping.
    _confirm(ping.id, fingerprint)
    return "unchanged"


def _candidate_pings(job: RedactionJob, fingerprint: str) -> QuerySet[Ping]:
    qs = Ping.objects.filter(owner__in=job.checks.all()).select_related("owner")
    if job.start:
        qs = qs.filter(created__gte=job.start)
    if job.end:
        qs = qs.filter(created__lt=job.end)
    if job.cursor is not None:
        qs = qs.filter(id__gt=job.cursor)
    # Skip events already confirmed for the current rule set. States carrying
    # an older fingerprint (rules were added or modified) are not skipped.
    qs = qs.exclude(redaction_state__fingerprint=fingerprint)
    return qs.order_by("id")


def process_job(job: RedactionJob, limit: int | None = None) -> tuple[int, int]:
    """Process pending pings within the job's check/time scope.

    Processes at most ``limit`` pings per invocation (no limit when None).
    Returns a ``(processed, changed)`` tuple for this invocation. The job's
    cursor always points at the last confirmed event.
    """
    fingerprint = RedactionRule.fingerprint()
    rules = RedactionRule.compiled()

    if job.fingerprint != fingerprint:
        # The rule set is new or was modified since the last run: scanning
        # must restart from the beginning of the scope. Pings confirmed with
        # the current fingerprint (e.g. by another job) are still skipped.
        job.fingerprint = fingerprint
        job.cursor = None
        job.processed = 0
        job.changed = 0

    job.status = "running"
    job.save()

    pings = _candidate_pings(job, fingerprint)

    processed = 0
    changed = 0
    try:
        for ping in pings.iterator():
            outcome = process_ping(ping, fingerprint, rules)
            processed += 1
            if outcome == "changed":
                changed += 1

            # Advance the cursor only after the event is fully confirmed.
            job.cursor = ping.id
            job.processed += 1
            job.changed += 1 if outcome == "changed" else 0
            job.save(update_fields=["cursor", "processed", "changed", "updated"])

            if limit is not None and processed >= limit:
                has_more = pings.filter(id__gt=job.cursor).exists()
                job.status = "running" if has_more else "done"
                job.save(update_fields=["status", "updated"])
                return processed, changed
    except Exception:
        # Read/write failure: do not advance past the failed event and do not
        # count this run as complete. The cursor stays at the last confirmed
        # event, so the next invocation resumes from the failed one.
        job.status = "failed"
        job.save(update_fields=["status", "updated"])
        logger.exception("Redaction job %s interrupted", job.id)
        raise

    job.status = "done"
    job.save(update_fields=["status", "updated"])
    return processed, changed


def process_scope(
    checks: Iterable[Check],
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int | None = None,
) -> RedactionJob:
    """Create a job for the given checks and time range and run it once."""
    job = RedactionJob.objects.create(start=start, end=end)
    job.checks.set(checks)
    process_job(job, limit=limit)
    return job
