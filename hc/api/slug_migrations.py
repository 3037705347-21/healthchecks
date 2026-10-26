"""Batch slug rebinding: old slug -> new slug migration plans.

A migration plan covers a batch of checks and groups its items into three
states:

- pending   - the old->new pair is registered, the check is not rebound yet
- migrated  - the check carries the new slug, the old slug routes to it
- expired   - the old slug is retired and no longer resolves

The functions in this module are the single entry point for both the
`rebindslugs` management command and the test-suite. Writes are grouped into
transactions along state ("group") boundaries, so a run that does not return
completely can be re-invoked and continues from the first group with pending
work, never redoing committed groups.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta as td
from typing import Any

from django.db import transaction
from django.utils.timezone import now

from hc.accounts.models import Project
from hc.api.models import Check, SlugAlias

PENDING = "pending"
MIGRATED = "migrated"
EXPIRED = "expired"
ACTIVE_STATES = (PENDING, MIGRATED)

# Conflict reasons. When any pair conflicts, the whole batch is rejected and
# nothing gets committed.
INVALID_SLUG = (
    "slug is empty or contains characters other than a-z, 0-9, '-' and '_'"
)
SAME_SLUG = "old slug and new slug are identical"
DUPLICATE_OLD = "old slug appears more than once in the batch"
DUPLICATE_NEW = "new slug appears more than once in the batch"
CHAIN = "new slug of one pair is the old slug of another pair"
OLD_NOT_FOUND = "no check with the old slug exists in the project"
OLD_AMBIGUOUS = "several checks share the old slug"
NEW_OCCUPIED = "new slug is already used by another check"
NEW_IS_ALIAS = "new slug is currently an active alias of another migration"
ALIAS_TARGET_MISMATCH = "registered alias points to a different new slug"
CHECK_BUSY = "check is already part of another migration pair"
UNEXPECTED_SLUG = (
    "check was renamed to a different slug since the plan was submitted"
)
NOT_MIGRATED = "alias is not in the migrated group and cannot be expired"
UNKNOWN_SLUG = "no alias with this slug exists in the project"

SLUG_RE = re.compile(r"^[a-z0-9_-]+$")


@dataclass(frozen=True)
class Pair:
    old: str
    new: str


@dataclass
class Conflict:
    old: str
    new: str
    reason: str


class SlugMigrationError(Exception):
    """Raised when a batch contains conflicting checks.

    The whole surrounding transaction is rolled back, so nothing from the
    batch gets committed.
    """

    def __init__(self, conflicts: Iterable[Conflict]):
        self.conflicts = list(conflicts)
        super().__init__(f"{len(self.conflicts)} conflicting migration pair(s)")


@dataclass
class PlanGroups:
    """The three groups a migration plan consists of."""

    pending: list[SlugAlias] = field(default_factory=list)
    migrated: list[SlugAlias] = field(default_factory=list)
    expired: list[SlugAlias] = field(default_factory=list)

    def all(self) -> list[SlugAlias]:
        return self.pending + self.migrated + self.expired


@dataclass
class ReconcileResult:
    groups: PlanGroups
    added: int = 0
    rebinded: int = 0
    rolled_back: int = 0
    expired: int = 0


@dataclass
class _Resolved:
    pair: Pair
    check: Check
    existing: SlugAlias | None


def is_valid_slug(slug: str) -> bool:
    max_length: int = Check._meta.get_field("slug").max_length  # type: ignore[assignment]
    return bool(SLUG_RE.fullmatch(slug)) and len(slug) <= max_length


def coerce_pairs(raw: Any) -> list[Pair]:
    """Build Pair objects from JSON-friendly input.

    Accepts either ``[{"old": "a", "new": "b"}, ...]`` / ``[["a", "b"]]``
    or a mapping ``{"a": "b"}``, optionally wrapped in ``{"pairs": ...}``.
    """

    if isinstance(raw, dict) and set(raw.keys()) == {"pairs"}:
        raw = raw["pairs"]

    if isinstance(raw, dict):
        items: Iterable[Any] = list(raw.items())
    elif isinstance(raw, list):
        items = raw
    else:
        raise TypeError("expected a list of pairs or a mapping")

    pairs = []
    for item in items:
        if isinstance(item, dict):
            old, new = item["old"], item["new"]
        else:
            old, new = item
        pairs.append(Pair(old=str(old), new=str(new)))
    return pairs


def groups(project: Project | None = None) -> PlanGroups:
    qs = SlugAlias.objects.all()
    if project is not None:
        qs = qs.filter(project=project)

    result = PlanGroups()
    for alias in qs.select_related("owner").order_by("id"):
        if alias.state == PENDING:
            result.pending.append(alias)
        elif alias.state == MIGRATED:
            result.migrated.append(alias)
        else:
            result.expired.append(alias)
    return result


def _conflicts_within_batch(pairs: list[Pair]) -> list[Conflict]:
    conflicts: list[Conflict] = []
    seen_old: set[str] = set()
    seen_new: set[str] = set()
    olds = {p.old for p in pairs}

    for pair in pairs:
        if not is_valid_slug(pair.old) or not is_valid_slug(pair.new):
            conflicts.append(Conflict(pair.old, pair.new, INVALID_SLUG))
        if pair.old == pair.new:
            conflicts.append(Conflict(pair.old, pair.new, SAME_SLUG))
        if pair.old in seen_old:
            conflicts.append(Conflict(pair.old, pair.new, DUPLICATE_OLD))
        if pair.new in seen_new:
            conflicts.append(Conflict(pair.old, pair.new, DUPLICATE_NEW))
        # A chain (a->b and b->c) would make "b" both an alias and a primary
        # target, so its address would be ambiguous.
        if pair.new in olds:
            conflicts.append(Conflict(pair.old, pair.new, CHAIN))

        seen_old.add(pair.old)
        seen_new.add(pair.new)

    return conflicts


def _resolve(project: Project, pairs: list[Pair]) -> list[_Resolved]:
    """Validate the whole batch against live state and resolve it.

    Locks the involved checks and aliases. Raises SlugMigrationError (rolling
    back the caller's transaction) if any pair conflicts; otherwise returns
    one _Resolved per pair, in input order.
    """

    conflicts = _conflicts_within_batch(pairs)
    if conflicts:
        raise SlugMigrationError(conflicts)

    slugs = {p.old for p in pairs} | {p.new for p in pairs}
    # Lock the rows we are about to read so a concurrent rename cannot change
    # the decision between validation and the write.
    list(
        Check.objects.select_for_update()
        .filter(project=project, slug__in=slugs)
        .order_by("id")
    )
    list(
        SlugAlias.objects.select_for_update()
        .filter(project=project, slug__in=slugs)
        .order_by("id")
    )

    checks = list(Check.objects.filter(project=project, slug__in=slugs))
    by_slug: dict[str, list[Check]] = {}
    for check in checks:
        by_slug.setdefault(check.slug, []).append(check)

    active_in_window = {
        a.slug: a
        for a in SlugAlias.objects.filter(
            project=project, slug__in=slugs, state__in=ACTIVE_STATES
        )
    }

    resolved: list[_Resolved] = []
    source_ids = {c.id for cs in by_slug.values() for c in cs}
    # All aliases owned by any check the batch touches (the busy check needs
    # aliases outside the batch's slug window as well).
    aliases_by_owner: dict[int, list[SlugAlias]] = {}
    for alias in SlugAlias.objects.filter(
        project=project, owner_id__in=source_ids, state__in=ACTIVE_STATES
    ):
        aliases_by_owner.setdefault(alias.owner_id, []).append(alias)

    for pair in pairs:
        existing = active_in_window.get(pair.old)
        if existing is not None:
            # Converged re-submission (the pair is pending or already
            # migrated). Nothing is written for it, so it survives resume
            # after the owner check has been renamed to the new slug.
            if existing.new_slug != pair.new:
                conflicts.append(Conflict(pair.old, pair.new, ALIAS_TARGET_MISMATCH))
                continue
            resolved.append(_Resolved(pair, existing.owner, existing))
            continue

        sources = by_slug.get(pair.old, [])
        if len(sources) == 0:
            conflicts.append(Conflict(pair.old, pair.new, OLD_NOT_FOUND))
            continue
        if len(sources) > 1:
            conflicts.append(Conflict(pair.old, pair.new, OLD_AMBIGUOUS))
            continue

        source = sources[0]

        if by_slug.get(pair.new):
            conflicts.append(Conflict(pair.old, pair.new, NEW_OCCUPIED))

        if pair.new in active_in_window:
            conflicts.append(Conflict(pair.old, pair.new, NEW_IS_ALIAS))

        # The check must not already participate in another migration.
        busy = any(
            a.slug != pair.old for a in aliases_by_owner.get(source.id, [])
        )
        if busy:
            conflicts.append(Conflict(pair.old, pair.new, CHECK_BUSY))

        resolved.append(_Resolved(pair, source, None))

    if conflicts:
        raise SlugMigrationError(conflicts)

    return resolved


def submit_plan(
    project: Project, pairs: Iterable[Pair], dry_run: bool = False
) -> list[SlugAlias]:
    """Register a batch of old->new pairs in the pending group.

    Every pair is validated before anything is written. A single conflict
    rejects the whole batch (the transaction is rolled back, zero rows are
    committed). Re-submitting an already registered pair is a no-op and
    returns the existing alias, so interrupted submissions can be retried.
    """

    pair_list = list(pairs)
    with transaction.atomic():
        resolved = _resolve(project, pair_list)

        aliases: list[SlugAlias] = []
        for item in resolved:
            if item.existing is not None:
                # Idempotent re-submission.
                aliases.append(item.existing)
                continue

            if dry_run:
                aliases.append(
                    SlugAlias(
                        project=project,
                        owner=item.check,
                        slug=item.pair.old,
                        new_slug=item.pair.new,
                        state=PENDING,
                    )
                )
                continue

            # Reuse the slug of a superseded, expired alias.
            SlugAlias.objects.filter(project=project, slug=item.pair.old).delete()
            aliases.append(
                SlugAlias.objects.create(
                    project=project,
                    owner=item.check,
                    slug=item.pair.old,
                    new_slug=item.pair.new,
                    state=PENDING,
                )
            )

        return aliases


def advance(project: Project | None = None, dry_run: bool = False) -> list[SlugAlias]:
    """Move the pending group into the migrated group.

    For every pending alias the owner check is renamed to the stored new
    slug in the same transaction as the state change, so at no point is the
    old slug unroutable (it is a primary slug before commit and an active
    alias afterwards). Heartbeats therefore keep landing on the same check
    and the same ping history throughout the cutover.

    The whole group is one transaction: if one item can no longer be
    rebound, the group stays pending and the conflicts are returned via
    SlugMigrationError for a later retry. Already migrated items are never
    touched, which makes re-running this after an interruption a resume
    rather than a restart.
    """

    with transaction.atomic():
        qs = (
            SlugAlias.objects.select_for_update()
            .filter(state=PENDING)
            .select_related("owner")
            .order_by("id")
        )
        if project is not None:
            qs = qs.filter(project=project)
        pending = list(qs)

        if not pending:
            return []

        project_ids = {a.project_id for a in pending}
        check_ids = {a.owner_id for a in pending}
        list(Check.objects.select_for_update().filter(id__in=check_ids).order_by("id"))

        new_slugs = {a.new_slug for a in pending}
        pending_ids = [a.id for a in pending]
        occupied = set(
            Check.objects.filter(project_id__in=project_ids, slug__in=new_slugs)
            .values_list("slug", flat=True)
        )
        aliased = set(
            SlugAlias.objects.filter(
                state__in=ACTIVE_STATES,
                project_id__in=project_ids,
                slug__in=new_slugs,
            )
            .exclude(id__in=pending_ids)
            .values_list("slug", flat=True)
        )

        conflicts: list[Conflict] = []
        for alias in pending:
            check = alias.owner
            if check.slug == alias.new_slug:
                # Rebind happened out of band; just converge the state.
                continue
            if check.slug != alias.slug:
                conflicts.append(Conflict(alias.slug, alias.new_slug, UNEXPECTED_SLUG))
            elif alias.new_slug in occupied or alias.new_slug in aliased:
                conflicts.append(Conflict(alias.slug, alias.new_slug, NEW_OCCUPIED))

        if conflicts:
            raise SlugMigrationError(conflicts)

        migrated: list[SlugAlias] = []
        frozen_now = now()
        for alias in pending:
            if not dry_run:
                check = alias.owner
                if check.slug != alias.new_slug:
                    check.slug = alias.new_slug
                    check.save(update_fields=["slug"])
                alias.state = MIGRATED
                alias.rebinded = frozen_now
                alias.save(update_fields=["state", "rebinded"])
            migrated.append(alias)

        return migrated


def expire_items(
    project: Project | None = None,
    slugs: Iterable[str] | None = None,
    after_days: int | None = None,
    dry_run: bool = False,
) -> list[SlugAlias]:
    """Move migrated aliases into the expired group.

    Targets are the given explicit slugs and/or migrated aliases rebound at
    least ``after_days`` days ago. The whole group is one transaction.
    Unknown slugs, or slugs that are not migrated, reject the operation via
    SlugMigrationError without touching the others.
    """

    slug_list = list(slugs) if slugs is not None else None
    with transaction.atomic():
        qs = SlugAlias.objects.filter(state=MIGRATED).order_by("id")
        if project is not None:
            qs = qs.filter(project=project)
        if after_days is not None:
            qs = qs.filter(rebinded__lte=now() - td(days=after_days))

        if slug_list is None:
            to_expire = list(qs)
        else:
            wanted = set(slug_list)
            scoped = SlugAlias.objects.all()
            if project is not None:
                scoped = scoped.filter(project=project)
            known = {a.slug: a.state for a in scoped.filter(slug__in=wanted)}
            to_expire = [a for a in qs if a.slug in wanted]
            found = {a.slug for a in to_expire}
            conflicts = [
                Conflict(
                    slug,
                    "",
                    NOT_MIGRATED if known.get(slug) in ACTIVE_STATES else UNKNOWN_SLUG,
                )
                for slug in slug_list
                if slug not in found
            ]
            if conflicts:
                raise SlugMigrationError(conflicts)

        if not dry_run:
            frozen_now = now()
            SlugAlias.objects.filter(id__in=[a.id for a in to_expire]).update(
                state=EXPIRED, expired_at=frozen_now
            )

        return to_expire


def migrate(
    project: Project,
    pairs: Iterable[Pair] | None = None,
    expire_slugs: Iterable[str] | None = None,
    expire_after_days: int | None = None,
    dry_run: bool = False,
) -> PlanGroups:
    """Advance a plan through the groups, in a fixed order.

    1. pending group   - submit new pairs (all-or-nothing validation)
    2. migrated group  - rebind pending aliases
    3. expired group   - retire migrated aliases (explicit slugs or age)

    Each group is its own committed transaction. When a run does not return
    completely, calling this again resumes from the first group still having
    work: submitted/rebinded/expired items are not processed again.
    """

    if pairs is not None:
        submit_plan(project, pairs, dry_run=dry_run)
    advance(project, dry_run=dry_run)
    if expire_slugs is not None or expire_after_days is not None:
        expire_items(
            project,
            slugs=expire_slugs,
            after_days=expire_after_days,
            dry_run=dry_run,
        )
    return groups(project)


def reconcile(
    project: Project, pairs: Iterable[Pair], dry_run: bool = False
) -> ReconcileResult:
    """Converge stored aliases towards the source list of old->new pairs.

    - addition: a source pair without an active alias is registered and
      rebound (pending then migrated)
    - rollback: a reversed pair (new->old while old->new exists) undoes the
      forward migration and installs a reverse alias
    - deletion: an active alias absent from the source is moved to expired

    Everything is validated up front as one batch; a single conflict rejects
    the whole reconciliation. Re-running with the same source is idempotent.
    """

    pair_list = list(pairs)

    with transaction.atomic():
        conflicts = _conflicts_within_batch(pair_list)
        if conflicts:
            raise SlugMigrationError(conflicts)

        # Lock the project's rows so the reconciliation is serial with
        # concurrent renames and other migration batches.
        list(Check.objects.select_for_update().filter(project=project).order_by("id"))
        list(
            SlugAlias.objects.select_for_update()
            .filter(project=project)
            .order_by("id")
        )

        checks = list(Check.objects.filter(project=project))
        active = list(
            SlugAlias.objects.filter(
                project=project, state__in=ACTIVE_STATES
            ).select_related("owner")
        )
        active_by_slug = {a.slug: a for a in active}

        to_add: list[Pair] = []
        to_rollback: list[tuple[Pair, SlugAlias]] = []
        touched_alias_ids: set[int] = set()

        for pair in pair_list:
            forward = active_by_slug.get(pair.old)
            if forward is not None:
                touched_alias_ids.add(forward.id)
                if forward.new_slug != pair.new:
                    conflicts.append(
                        Conflict(pair.old, pair.new, ALIAS_TARGET_MISMATCH)
                    )
                # Pair matches an existing plan; advance() converges it.
                continue

            reversed_alias = active_by_slug.get(pair.new)
            if reversed_alias is not None and reversed_alias.new_slug == pair.old:
                to_rollback.append((pair, reversed_alias))
                touched_alias_ids.add(reversed_alias.id)
                continue

            to_add.append(pair)

        to_expire = [a for a in active if a.id not in touched_alias_ids]

        # Project the post-batch primary/alias slug sets, taking rollbacks
        # into account. A rollback pair (x -> y) reverses a forward y -> x:
        # x stops being the primary and becomes an alias, y is restored as
        # the primary.
        current_primary = {c.slug for c in checks if c.slug}
        primary_restored = {pair.new for pair, _ in to_rollback}
        primary_consumed = {pair.old for pair, _ in to_rollback}
        deleted_forward_ids = {f.id for _, f in to_rollback}
        post_primary = (current_primary - primary_consumed) | primary_restored
        post_alias = {
            a.slug for a in active if a.id not in deleted_forward_ids
        } | primary_consumed

        checks_by_slug: dict[str, list[Check]] = {}
        for check in checks:
            checks_by_slug.setdefault(check.slug, []).append(check)

        # Owner->slug map of aliases that survive the batch, for the busy
        # check (one check, one active migration).
        owner_slugs: dict[int, set[str]] = {}
        for alias in active:
            if alias.id not in deleted_forward_ids:
                owner_slugs.setdefault(alias.owner_id, set()).add(alias.slug)
        for pair, forward in to_rollback:
            # The reverse alias keeps routing the now-old x address to C.
            owner_slugs.setdefault(forward.owner_id, set()).add(pair.old)

        # Slugs restored to primary slugs by a rollback in this batch.
        rollback_restored = {pair.new: forward for pair, forward in to_rollback}

        add_sources: dict[Pair, Check] = {}
        for pair in to_add:
            sources = checks_by_slug.get(pair.old, [])
            source: Check | None = None
            if len(sources) > 1:
                conflicts.append(Conflict(pair.old, pair.new, OLD_AMBIGUOUS))
            elif len(sources) == 1:
                source = sources[0]
            elif pair.old in rollback_restored:
                # The check gets this slug back via a rollback in this batch.
                source = rollback_restored[pair.old].owner
            else:
                conflicts.append(Conflict(pair.old, pair.new, OLD_NOT_FOUND))

            if pair.new in post_primary:
                conflicts.append(Conflict(pair.old, pair.new, NEW_OCCUPIED))
            if pair.new in post_alias:
                conflicts.append(Conflict(pair.old, pair.new, NEW_IS_ALIAS))
            if source is not None:
                others = owner_slugs.get(source.id, set())
                if others:
                    conflicts.append(Conflict(pair.old, pair.new, CHECK_BUSY))
                owner_slugs.setdefault(source.id, set()).add(pair.old)
                add_sources[pair] = source

        if conflicts:
            raise SlugMigrationError(conflicts)

        frozen_now = now()
        added = 0
        rolled_back = 0

        if not dry_run:
            for pair, forward in to_rollback:
                check = forward.owner
                # Drop the forward alias, and any expired reverse alias that
                # still occupies the alias slug we are about to reuse.
                SlugAlias.objects.filter(
                    project=project, slug__in=(pair.old, pair.new)
                ).delete()
                # Restore the original slug as the primary one:
                check.slug = pair.new
                check.save(update_fields=["slug"])
                # Keep routing the rolled-back-from address to the same check
                # for stragglers pinging it during the rollback window:
                SlugAlias.objects.create(
                    project=project,
                    owner=check,
                    slug=pair.old,
                    new_slug=pair.new,
                    state=MIGRATED,
                    rebinded=frozen_now,
                )
                rolled_back += 1

            for pair in to_add:
                source_check = add_sources[pair]
                SlugAlias.objects.filter(project=project, slug=pair.old).delete()
                SlugAlias.objects.create(
                    project=project,
                    owner=source_check,
                    slug=pair.old,
                    new_slug=pair.new,
                    state=PENDING,
                )
                added += 1

            for alias in to_expire:
                alias.state = EXPIRED
                alias.expired_at = frozen_now
                alias.save(update_fields=["state", "expired_at"])
        else:
            added = len(to_add)
            rolled_back = len(to_rollback)

        expired_count = len(to_expire)

    # Migrated group: rebind the freshly registered pairs. Each pending row
    # is independently part of the resumable group, so an interruption here
    # is finished by a later reconcile/advance call.
    rebinded_count = 0
    if to_add and not dry_run:
        rebinded_count = len(advance(project))

    return ReconcileResult(
        groups=groups(project),
        added=added,
        rebinded=rebinded_count,
        rolled_back=rolled_back,
        expired=expired_count,
    )


def prune_expired(
    project: Project | None = None, older_than_days: int | None = None
) -> int:
    """Delete expired alias records, optionally older than N days."""

    qs = SlugAlias.objects.filter(state=EXPIRED)
    if project is not None:
        qs = qs.filter(project=project)
    if older_than_days is not None:
        qs = qs.filter(expired_at__lte=now() - td(days=older_than_days))
    return qs.delete()[0]
