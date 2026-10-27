"""Persistent per-project status snapshots.

The project's overall status (up / down / grace) is continuously recorded in
the ProjectStatusSnapshot table. The source of every snapshot is the actual
state of the project's checks:

* live state changes arrive through Flip objects (see signals.py), and
* check additions, removals and renames are recorded as they happen.

A management command (``snapshotstatus``) can additionally sample the current
state (this is how transient "grace" periods get observed) and catch up after
downtime by replaying Flip objects the running process did not consume.

Guarantees:

* Snapshots are append-only. Rows are inserted, never updated or deleted. A
  row already present at a given timestamp -- including manually retained
  history rows -- is always left untouched.
* The resume cursor (ProjectSnapshotCursor) is advanced in the same
  transaction as the snapshot write. If the project state cannot be
  determined or the write fails, processing stops at the last confirmed
  snapshot and the cursor stays put. A re-run then only fills the missing
  gap, and the unique (project, created) constraint makes it impossible to
  insert duplicates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.utils.timezone import now

from hc.accounts.models import Project
from hc.api.models import (
    Check,
    Flip,
    ProjectSnapshotCursor,
    ProjectStatusSnapshot,
)

logger = logging.getLogger("hc")

# Check statuses that can be reconstructed from flip history. "grace" is a
# transient, computed state and never appears in the flip stream, so historical
# replay can only ever confirm up / down (plus new / paused for the roster).
REPLAYABLE_STATUSES = {"up", "down", "new", "paused"}


class ProjectStatusUndeterminable(Exception):
    """Raised when the project's status cannot be confidently established."""


@dataclass(frozen=True)
class SnapshotState:
    status: str
    total: int
    grace: int
    down: int
    checks: list[dict[str, str]]


def _roster_entry(check: Check, status: str) -> dict[str, str]:
    return {"code": str(check.code), "name": check.name_then_code(), "status": status}


def _overall(down: int, grace: int) -> str:
    if down:
        return "down"
    if grace:
        return "grace"
    return "up"


def compute_current_state(project: Project) -> SnapshotState:
    """Compute the project state from the actual check rows.

    Uses Check.get_status(), so the transient "grace" state is fully accounted
    for. Raises ProjectStatusUndeterminable if any individual check status
    cannot be calculated.
    """

    roster: list[dict[str, str]] = []
    down, grace = 0, 0
    for check in Check.objects.filter(project=project).order_by("id"):
        try:
            status = check.get_status()
        except Exception as e:
            raise ProjectStatusUndeterminable(
                f"Cannot determine status of check {check.code}"
            ) from e

        roster.append(_roster_entry(check, status))
        if status == "down":
            down += 1
        elif status == "grace":
            grace += 1

    return SnapshotState(_overall(down, grace), len(roster), grace, down, roster)


def compute_historical_state(project: Project, when: datetime) -> SnapshotState:
    """Reconstruct the confirmed project state at `when` from flip history.

    Only checks that already existed at `when` participate, and each check's
    status is taken from its latest flip at or before `when` (or "new" if it
    had not flipped yet). Grace intervals cannot be reconstructed from flips,
    so the result only ever reports "up" or "down". Raises
    ProjectStatusUndeterminable if the flip stream contains a status that
    cannot be interpreted.
    """

    roster: list[dict[str, str]] = []
    down = 0
    checks = Check.objects.filter(project=project, created__lte=when).order_by("id")
    for check in checks:
        status = check.flip_set.filter(created__lte=when).order_by("-created").first()
        status_value = status.new_status if status else "new"
        if status_value not in REPLAYABLE_STATUSES:
            raise ProjectStatusUndeterminable(
                f"Cannot replay status {status_value!r} of check {check.code}"
            )

        roster.append(_roster_entry(check, status_value))
        if status_value == "down":
            down += 1

    return SnapshotState(_overall(down, 0), len(roster), 0, down, roster)


def _state_matches(snapshot: ProjectStatusSnapshot, state: SnapshotState) -> bool:
    return (
        snapshot.status == state.status
        and snapshot.total == state.total
        and snapshot.grace == state.grace
        and snapshot.down == state.down
        and list(snapshot.checks) == state.checks
    )


def _append(
    project: Project,
    when: datetime,
    state: SnapshotState,
    source: str,
    manual: bool = False,
) -> tuple[ProjectStatusSnapshot, bool]:
    """Append a snapshot, unless one already represents this state.

    Must be called inside a transaction. Never updates or deletes rows.

    Returns (snapshot, created). `created` is False when an existing row was
    preserved (a row at the same timestamp, or an identical predecessor).
    """

    existing = ProjectStatusSnapshot.objects.filter(
        project=project, created=when
    ).first()
    if existing is not None:
        # A snapshot already exists for this instant. It may be a manually
        # retained history row -- never replace it.
        return existing, False

    predecessor = (
        ProjectStatusSnapshot.objects.filter(project=project, created__lt=when)
        .order_by("-created", "-id")
        .first()
    )
    if predecessor is not None and _state_matches(predecessor, state):
        # Nothing changed since the previous confirmed snapshot: do not
        # duplicate the same state.
        return predecessor, False

    snapshot = ProjectStatusSnapshot.objects.create(
        project=project,
        created=when,
        status=state.status,
        total=state.total,
        grace=state.grace,
        down=state.down,
        checks=state.checks,
        source=source,
        manual=manual,
    )
    return snapshot, True


def record_current_snapshot(
    project: Project, *, source: str = "check_status", when: datetime | None = None
) -> tuple[ProjectStatusSnapshot, bool]:
    """Compute the current project state and append it as a snapshot.

    Raises ProjectStatusUndeterminable if the state cannot be established,
    and propagates database write errors. In both cases no cursor is advanced
    by the caller, so processing remains at the last confirmed snapshot.
    """

    when = when or now()
    state = compute_current_state(project)
    with transaction.atomic():
        return _append(project, when, state, source)


def try_record_current_snapshot(
    project: Project, *, source: str, when: datetime | None = None
) -> ProjectStatusSnapshot | None:
    """Best-effort wrapper for request-path hooks (signals, views).

    Logs and returns None instead of raising, so a snapshot problem can never
    break ping delivery or the web UI. The catch-up command repairs any gap
    later, replaying from the unadvanced cursor.
    """

    try:
        snapshot, _ = record_current_snapshot(project, source=source, when=when)
        return snapshot
    except Exception:
        logger.exception(
            "Failed to record project status snapshot for project %s", project.id
        )
        return None


def _state_with_flip(state: SnapshotState, flip: Flip) -> SnapshotState:
    """Return the state after applying `flip` to its owner's roster entry.

    Needed because a Flip can be saved a fraction before the check row itself
    is updated (in Check.ping()), so a live scan could still observe the
    pre-flip status. The flip is the authoritative state change.
    """

    code = str(flip.owner.code)
    roster: list[dict[str, str]] = []
    found = False
    down, grace = 0, 0
    for entry in state.checks:
        status = entry["status"]
        if entry["code"] == code:
            status = flip.new_status
            found = True
        roster.append({**entry, "status": status})
        if status == "down":
            down += 1
        elif status == "grace":
            grace += 1

    if not found:
        roster.append(
            {"code": code, "name": flip.owner.name_then_code(), "status": flip.new_status}
        )
        if flip.new_status == "down":
            down += 1
        elif flip.new_status == "grace":
            grace += 1
        roster.sort(key=lambda item: item["code"])

    return SnapshotState(_overall(down, grace), len(roster), grace, down, roster)


def _get_locked_cursor(project: Project) -> ProjectSnapshotCursor:
    cursor, created = ProjectSnapshotCursor.objects.get_or_create(project=project)
    if not created:
        cursor = ProjectSnapshotCursor.objects.select_for_update().get(pk=cursor.pk)
    return cursor


def ingest_flip(flip: Flip) -> None:
    """Record the live state change represented by a newly created Flip.

    Also advances the project's resume cursor past this flip. Runs in a single
    transaction; any failure leaves both the snapshot history and the cursor
    at the last confirmed position.
    """

    try:
        project = flip.owner.project
    except Project.DoesNotExist:
        return

    try:
        with transaction.atomic():
            when = flip.created
            state = _state_with_flip(compute_current_state(project), flip)
            _append(project, when, state, "check_status")
            cursor = _get_locked_cursor(project)
            if (cursor.last_flip_id or 0) < flip.id:
                cursor.last_flip_id = flip.id
                cursor.updated = now()
                cursor.save(update_fields=["last_flip_id", "updated"])
    except Exception:
        logger.exception(
            "Failed to ingest flip %s into project status snapshots", flip.id
        )


def catch_up_project(project: Project) -> int:
    """Replay unconsumed flips and fill missing snapshot history.

    Processes flips in id order starting after the cursor position. For every
    flip it reconstructs the confirmed project state at the flip's timestamp
    and appends a snapshot (unless that state is already recorded), advancing
    the cursor in the same transaction.

    Stops at the first undeterminable state or write failure: the cursor
    remains at the last confirmed snapshot, and a re-run resumes there and
    only fills the remaining gap.

    Returns the number of flips consumed.
    """

    cursor, _ = ProjectSnapshotCursor.objects.get_or_create(project=project)
    processed = 0

    while True:
        flip = (
            Flip.objects.filter(
                owner__project=project, id__gt=cursor.last_flip_id or 0
            )
            .select_related("owner")
            .order_by("id")
            .first()
        )
        if flip is None:
            break

        # Reconstruct before opening the write transaction. If the state at
        # this point in history cannot be determined, raise without touching
        # the cursor, so the run stops at the last confirmed snapshot.
        state = compute_historical_state(project, flip.created)

        with transaction.atomic():
            locked = ProjectSnapshotCursor.objects.select_for_update().get(
                pk=cursor.pk
            )
            _append(project, flip.created, state, "catch_up")
            if (locked.last_flip_id or 0) < flip.id:
                locked.last_flip_id = flip.id
                locked.updated = now()
                locked.save(update_fields=["last_flip_id", "updated"])
            cursor = locked

        processed += 1

    return processed


def sync_project(project: Project) -> tuple[int, ProjectStatusSnapshot]:
    """Fill any history gap, then record the current project state."""

    processed = catch_up_project(project)
    snapshot, _ = record_current_snapshot(project, source="periodic")
    return processed, snapshot


def current_snapshot(project: Project) -> ProjectStatusSnapshot:
    """Return the project's latest confirmed snapshot.

    This is the single read entry point shared by the status page and the
    badge, so the two can never disagree. If no snapshot exists yet, one is
    materialized from the actual check states and persisted.
    """

    snapshot = (
        ProjectStatusSnapshot.objects.filter(project=project)
        .order_by("-created", "-id")
        .first()
    )
    if snapshot is None:
        snapshot, _ = record_current_snapshot(project, source="initial")
    return snapshot


def badge_status(snapshot: ProjectStatusSnapshot, with_late: bool = True) -> str:
    """Map a snapshot status to badge vocabulary ("grace" is shown as "late")."""

    if snapshot.status == "down":
        return "down"
    if snapshot.status == "grace" and with_late:
        return "late"
    return "up"
