from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404

from hc.accounts.models import Project
from hc.api.models import Channel, Notification

# Reasons for rejecting a candidate group. When a group is rejected none of
# its channels are modified or deleted.
AMBIGUOUS_SURVIVOR = "several channels share the earliest creation timestamp"
REFERENCED_BY_GROUP = "a channel scheduled for removal is used by a group channel"


@dataclass
class GroupReport:
    project_id: int
    kind: str
    channel_codes: list[str]


@dataclass
class MergedGroup(GroupReport):
    survivor_code: str = ""
    moved_checks: int = 0
    moved_notifications: int = 0


@dataclass
class RejectedGroup(GroupReport):
    reason: str = ""


@dataclass
class MergeStats:
    merged: list[MergedGroup] = field(default_factory=list)
    rejected: list[RejectedGroup] = field(default_factory=list)

    @property
    def removed_channels(self) -> int:
        # Every merged group leaves one survivor and removes the rest.
        return sum(len(g.channel_codes) - 1 for g in self.merged)


def find_duplicate_groups(project: Project | None = None) -> list[list[Channel]]:
    """Return groups of channels sharing project, kind and configuration.

    Configuration is the channel's ``kind`` and its raw ``value`` field.
    Groups are strictly scoped to a single project: channels from different
    projects can never end up in the same group, even if their configuration
    is identical. Each group is ordered by creation time (then id), so the
    first member is the candidate survivor.
    """

    q = Channel.objects.all()
    if project is not None:
        q = q.filter(project=project)

    rows = (
        q.values("project_id", "kind", "value")
        .annotate(n=Count("id"))
        .filter(n__gte=2)
        .order_by("project_id", "kind")
    )

    groups: list[list[Channel]] = []
    for row in rows:
        members = list(
            Channel.objects.filter(
                project_id=row["project_id"],
                kind=row["kind"],
                value=row["value"],
            ).order_by("created", "id")
        )
        groups.append(members)

    return groups


def reject_reason(members: list[Channel]) -> str | None:
    """Check whether a candidate group can be merged safely.

    Returns None when the group is safe to merge, otherwise a reason string.
    """

    # The members are ordered by (created, id). If two members tie on the
    # earliest creation timestamp we cannot uniquely identify the survivor.
    if members[1].created == members[0].created:
        return AMBIGUOUS_SURVIVOR

    # "group" channels store the codes of their member channels, as a
    # comma-separated list, in their value field. Deleting a channel that a
    # group channel refers to would silently change that existing group's
    # behaviour, so we reject the whole candidate group instead.
    duplicate_codes = {str(m.code) for m in members[1:]}
    referenced_codes: set[str] = set()
    values = (
        Channel.objects.filter(project_id=members[0].project_id, kind="group")
        .values_list("value", flat=True)
    )
    for value in values:
        referenced_codes.update(value.split(","))

    if duplicate_codes & referenced_codes:
        return REFERENCED_BY_GROUP

    return None


def merge_group(members: list[Channel]) -> tuple[Channel, int, int]:
    """Merge one validated group into its earliest-created channel.

    Moves check associations and notification history to the survivor, then
    deletes the duplicate channels. Returns the survivor and the numbers of
    check associations and notifications that were moved.
    """

    ids = [m.id for m in members]
    with transaction.atomic():
        # Lock all members for the duration of the merge so concurrent
        # processes cannot add references while we are moving data.
        locked = list(
            Channel.objects.select_for_update().filter(id__in=ids).order_by("created", "id")
        )
        # A concurrent run may have merged some members already.
        if len(locked) < 2:
            return members[0], 0, 0

        survivor, duplicates = locked[0], locked[1:]
        duplicate_ids = [d.id for d in duplicates]

        through = Channel.checks.through

        # Union the check associations: attach every check linked to any
        # duplicate to the survivor, skipping associations it already has.
        existing = set(
            through.objects.filter(channel_id=survivor.id).values_list(
                "check_id", flat=True
            )
        )
        to_add = set(
            through.objects.filter(channel_id__in=duplicate_ids).values_list(
                "check_id", flat=True
            )
        )
        to_add.difference_update(existing)
        through.objects.bulk_create(
            [through(channel_id=survivor.id, check_id=check_id) for check_id in to_add]
        )

        # Move notification history (including test notifications with a null
        # owner) to the survivor.
        moved_notifications = Notification.objects.filter(
            channel_id__in=duplicate_ids
        ).update(channel=survivor)

        Channel.objects.filter(id__in=duplicate_ids).delete()

    return survivor, len(to_add), moved_notifications


def merge_duplicate_channels(
    project: Project | None = None, dry_run: bool = False
) -> MergeStats:
    """Find and merge all identical, duplicate channels.

    Repeated runs converge: after a merge the duplicates are gone, so a
    subsequent run finds no candidate groups for the same configuration.
    Groups that fail the safety checks are rejected and left untouched.
    """

    stats = MergeStats()
    for members in find_duplicate_groups(project):
        codes = [str(m.code) for m in members]
        reason = reject_reason(members)
        if reason is not None:
            stats.rejected.append(
                RejectedGroup(
                    project_id=members[0].project_id,
                    kind=members[0].kind,
                    channel_codes=codes,
                    reason=reason,
                )
            )
            continue

        if dry_run:
            stats.merged.append(
                MergedGroup(
                    project_id=members[0].project_id,
                    kind=members[0].kind,
                    channel_codes=codes,
                    survivor_code=str(members[0].code),
                )
            )
            continue

        survivor, moved_checks, moved_notifications = merge_group(members)
        stats.merged.append(
            MergedGroup(
                project_id=survivor.project_id,
                kind=survivor.kind,
                channel_codes=codes,
                survivor_code=str(survivor.code),
                moved_checks=moved_checks,
                moved_notifications=moved_notifications,
            )
        )

    return stats


class Command(BaseCommand):
    help = (
        "Merge channels that were added several times with identical "
        "configuration within the same project. For every duplicate group the "
        "earliest-created channel is kept, check associations and notification "
        "history are moved to it, and the duplicates are deleted. Groups with "
        "an ambiguous earliest channel or referenced by a group channel are "
        "rejected and left untouched. Channels in different projects are never "
        "merged together."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            dest="dry_run",
            default=False,
            help="List the merges that would be performed, but change nothing",
        )
        parser.add_argument(
            "--project",
            type=str,
            default=None,
            help="Only process channels in the project with this code",
        )

    def handle(self, *, dry_run: bool, project: str | None, **options: Any) -> None:
        target = get_object_or_404(Project, code=project) if project else None
        stats = merge_duplicate_channels(target, dry_run=dry_run)

        verb = "Would merge" if dry_run else "Merged"
        for group in stats.merged:
            self.stdout.write(
                f"{verb}: project={group.project_id} kind={group.kind} "
                f"keep={group.survivor_code} "
                f"remove=[{', '.join(c for c in group.channel_codes
                                    if c != group.survivor_code)}] "
                f"checks={group.moved_checks} notifications={group.moved_notifications}"
            )

        for rejected in stats.rejected:
            self.stdout.write(
                f"Rejected ({rejected.reason}): project={rejected.project_id} "
                f"kind={rejected.kind} "
                f"channels=[{', '.join(rejected.channel_codes)}]"
            )

        self.stdout.write(
            f"{verb} {len(stats.merged)} group(s), removing "
            f"{stats.removed_channels} channel(s); rejected "
            f"{len(stats.rejected)} group(s)."
        )
