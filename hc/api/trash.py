from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils.timezone import now

from hc.accounts.models import Project
from hc.api.models import Check, Flip, TrashBatchOperation

Kind = Literal["restore", "purge"]
MAX_KEY_LENGTH = 64


class BatchError(Exception):
    def __init__(self, status: int, message: str, code: UUID | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


@dataclass
class BatchResult:
    status_code: int
    body: dict[str, Any]
    replayed: bool = False


def _error_body(message: str, code: UUID | None = None) -> dict[str, Any]:
    return {"error": message, "code": str(code) if code else None}


def _normalize_codes(raw: object) -> list[str]:
    if not isinstance(raw, list):
        raise BatchError(400, "'uuids' is not an array")

    normalized: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise BatchError(400, "'uuids' contains a non-string value")
        try:
            normalized.append(str(UUID(item)))
        except ValueError:
            raise BatchError(400, f"'{item}' is not a valid UUID")

    codes = sorted(set(normalized))
    if not codes:
        raise BatchError(400, "'uuids' is empty")

    return codes


def _get_existing_operation(
    project: Project, kind: Kind, codes: list[str], key: str
) -> TrashBatchOperation:
    """Fetch an existing idempotency record, or reserve a new one.

    Raises BatchError if the key was previously used with a different kind
    or a different set of UUIDs.
    """

    payload = json.dumps(codes)
    try:
        # Nested atomic block: the unique constraint violation rolls back to
        # this savepoint, leaving the outer transaction usable.
        with transaction.atomic():
            return TrashBatchOperation.objects.create(
                project=project, key=key, kind=kind, codes=payload
            )
    except IntegrityError:
        existing = TrashBatchOperation.objects.get(project=project, key=key)
        if existing.kind != kind or existing.codes != payload:
            raise BatchError(
                409, "idempotency key was already used with different parameters"
            )
        return existing


def _validate_and_apply(project: Project, kind: Kind, codes: list[str]) -> list[str]:
    """Validate the whole batch and apply the change atomically.

    Returns the list of affected UUID strings. Raises BatchError on the first
    invalid item; the surrounding transaction then rolls back, so either the
    whole batch succeeds or nothing changes.
    """

    uuids = [UUID(c) for c in codes]

    # One global lookup distinguishes "belongs to another project" (403) from
    # "does not exist anywhere" (404):
    global_rows = dict(
        Check.all_objects.filter(code__in=uuids).values_list("code", "project_id")
    )
    for candidate in uuids:
        owner_id = global_rows.get(candidate)
        if owner_id is None:
            raise BatchError(404, "check not found", candidate)
        if owner_id != project.id:
            raise BatchError(403, "check belongs to a different project", candidate)

    # Lock the selected rows so concurrent batches cannot race on them:
    locked = list(
        Check.all_objects.select_for_update()
        .filter(project=project, code__in=uuids)
        .values("id", "code", "slug", "status", "deleted_at")
    )
    locked_by_code = {row["code"]: row for row in locked}

    frozen_now = now()
    for candidate in uuids:
        row = locked_by_code[candidate]
        if row["deleted_at"] is None:
            # The check is live: it was already restored or never trashed.
            raise BatchError(409, "check is not in the trash", candidate)

    if kind == "restore":
        for candidate in uuids:
            deleted_at = locked_by_code[candidate]["deleted_at"]
            assert deleted_at is not None
            if deleted_at + settings.TRASH_RETENTION <= frozen_now:
                raise BatchError(
                    410, "check's trash retention period has expired", candidate
                )

        # Slug collision: a live check in the same project already uses a slug
        # of a check we are about to restore (ping-by-slug would be ambiguous):
        selected_ids = [row["id"] for row in locked]
        slugs = {row["slug"] for row in locked if row["slug"]}
        if slugs:
            collision = (
                Check.objects.filter(project=project, slug__in=slugs)
                .exclude(id__in=selected_ids)
                .order_by("slug")
                .values_list("slug", flat=True)
                .first()
            )
            if collision:
                offending = next(
                    candidate
                    for candidate in uuids
                    if locked_by_code[candidate]["slug"] == collision
                )
                raise BatchError(
                    409,
                    f"slug '{collision}' is already used by a live check",
                    offending,
                )

        # Check quota: restoring must not push the owner over the check limit:
        live_count = Check.objects.filter(project=project).count()
        check_limit = project.owner_profile.check_limit
        if live_count + len(locked) > check_limit:
            raise BatchError(409, "restoring would exceed the check limit")

        Check.all_objects.filter(id__in=selected_ids).update(
            deleted_at=None,
            status="paused",
            last_start=None,
            alert_after=None,
        )
        # Close the downtime/stats interval with a processed paused flip,
        # mirroring the existing pause semantics:
        Flip.objects.bulk_create(
            [
                Flip(
                    owner_id=row["id"],
                    created=frozen_now,
                    processed=frozen_now,
                    old_status=row["status"],
                    new_status="paused",
                )
                for row in locked
            ]
        )
    else:
        selected_ids = [row["id"] for row in locked]
        Check.all_objects.filter(id__in=selected_ids).delete()

    return [str(row["code"]) for row in locked]


def execute_batch(
    project: Project, *, kind: Kind, uuids: object, key: str | None
) -> BatchResult:
    """Atomically restore or purge a batch of trashed checks.

    `uuids` is the raw client-supplied value (a list of UUID strings).
    `key` is an optional idempotency key. Repeating a request with the same
    key and payload replays the original response without side effects.
    """

    try:
        codes = _normalize_codes(uuids)
    except BatchError as e:
        return BatchResult(e.status, _error_body(e.message, e.code))

    if key is not None and len(key) > MAX_KEY_LENGTH:
        return BatchResult(
            400, _error_body(f"idempotency key is longer than {MAX_KEY_LENGTH} chars")
        )

    operation: TrashBatchOperation | None = None
    if key:
        try:
            operation = _get_existing_operation(project, kind, codes, key)
        except BatchError as e:
            return BatchResult(e.status, _error_body(e.message, e.code))

        if (
            operation.completed is not None
            and operation.response_code is not None
            and operation.response
        ):
            return BatchResult(
                operation.response_code, json.loads(operation.response), replayed=True
            )

    try:
        with transaction.atomic():
            affected = _validate_and_apply(project, kind, codes)
            body = {f"{kind}d": affected}

            frozen_now = now()
            if operation is not None:
                # completed__isnull guards against overwriting a concurrent
                # request that already finished this same operation:
                updated = TrashBatchOperation.objects.filter(
                    id=operation.id, completed__isnull=True
                ).update(
                    completed=frozen_now,
                    response_code=200,
                    response=json.dumps(body),
                )
                if updated == 0:
                    stored = TrashBatchOperation.objects.get(id=operation.id)
                    return BatchResult(
                        stored.response_code or 200,
                        json.loads(stored.response),
                        replayed=True,
                    )
    except BatchError as e:
        if operation is not None:
            frozen_now = now()
            body = _error_body(e.message, e.code)
            updated = TrashBatchOperation.objects.filter(
                id=operation.id, completed__isnull=True
            ).update(
                completed=frozen_now,
                response_code=e.status,
                response=json.dumps(body),
            )
            if updated == 0:
                stored = TrashBatchOperation.objects.get(id=operation.id)
                return BatchResult(
                    stored.response_code or e.status,
                    json.loads(stored.response),
                    replayed=True,
                )
        return BatchResult(e.status, _error_body(e.message, e.code))

    # Refresh nag dates after a successful restore/purge (outside the DB
    # transaction). Not refreshed for replayed responses, the first execution
    # already took care of it.
    project.update_next_nag_dates()
    return BatchResult(200, body)
