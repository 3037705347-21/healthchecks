from __future__ import annotations

import io
from datetime import timedelta as td
from unittest.mock import Mock

from django.core.management import call_command
from django.utils.timezone import now

from hc.api.management.commands import mergechannels
from hc.api.models import Channel, Check, Notification
from hc.test import BaseTestCase


class MergeChannelsTestCase(BaseTestCase):
    def make_duplicates(self) -> tuple[Channel, Channel]:
        """Create two identical webhook channels, return (older, newer)."""

        value = '{"url_down": "http://example.org"}'
        older = Channel.objects.create(
            project=self.project,
            kind="webhook",
            value=value,
            created=now() - td(days=2),
        )
        newer = Channel.objects.create(
            project=self.project,
            kind="webhook",
            value=value,
            created=now() - td(days=1),
        )
        return older, newer

    def test_it_merges_duplicate_channels(self) -> None:
        older, newer = self.make_duplicates()
        check_a = Check.objects.create(project=self.project)
        check_b = Check.objects.create(project=self.project)
        older.checks.add(check_a)
        newer.checks.add(check_b)
        n1 = Notification.objects.create(
            owner=check_a, channel=older, check_status="down"
        )
        n2 = Notification.objects.create(
            owner=check_b, channel=newer, check_status="up"
        )
        # Test notifications (owner=None) belong to the duplicate as well.
        n3 = Notification.objects.create(channel=newer, check_status="down")

        stats = mergechannels.merge_duplicate_channels()

        self.assertEqual(len(stats.merged), 1)
        self.assertEqual(stats.removed_channels, 1)
        self.assertEqual(stats.merged[0].moved_checks, 1)
        self.assertEqual(stats.merged[0].moved_notifications, 2)

        # The earliest-created channel is the survivor.
        self.assertTrue(Channel.objects.filter(id=older.id).exists())
        self.assertFalse(Channel.objects.filter(id=newer.id).exists())

        # Check associations were unioned onto the survivor.
        self.assertEqual(set(older.checks.values_list("id", flat=True)),
                         {check_a.id, check_b.id})

        # Notification history (including test notifications) moved over.
        self.assertEqual(
            set(Notification.objects.values_list("id", flat=True)),
            {n1.id, n2.id, n3.id},
        )
        self.assertEqual(
            set(Notification.objects.values_list("channel_id", flat=True)),
            {older.id},
        )

    def test_it_keeps_survivor_name_and_state(self) -> None:
        value = '{"url_down": "http://example.org"}'
        older = Channel.objects.create(
            project=self.project,
            kind="webhook",
            name="First",
            value=value,
            disabled=True,
            created=now() - td(days=2),
        )
        Channel.objects.create(
            project=self.project,
            kind="webhook",
            name="Second",
            value=value,
            created=now() - td(days=1),
        )

        mergechannels.merge_duplicate_channels()

        older.refresh_from_db()
        self.assertEqual(older.name, "First")
        self.assertTrue(older.disabled)

    def test_it_handles_overlapping_check_associations(self) -> None:
        older, newer = self.make_duplicates()
        check = Check.objects.create(project=self.project)
        # The same check is associated with both channels.
        older.checks.add(check)
        newer.checks.add(check)

        stats = mergechannels.merge_duplicate_channels()

        self.assertEqual(stats.merged[0].moved_checks, 0)
        self.assertEqual(set(older.checks.values_list("id", flat=True)), {check.id})
        self.assertFalse(Channel.objects.filter(id=newer.id).exists())

    def test_it_is_idempotent(self) -> None:
        older, newer = self.make_duplicates()
        check = Check.objects.create(project=self.project)
        newer.checks.add(check)

        first = mergechannels.merge_duplicate_channels()
        self.assertEqual(first.removed_channels, 1)

        # Running again must converge: no groups, no changes.
        second = mergechannels.merge_duplicate_channels()
        self.assertEqual(second.merged, [])
        self.assertEqual(second.rejected, [])
        self.assertEqual(second.removed_channels, 0)
        self.assertTrue(Channel.objects.filter(id=older.id).exists())
        self.assertEqual(Channel.objects.count(), 1)
        self.assertEqual(set(older.checks.values_list("id", flat=True)), {check.id})

    def test_it_does_not_merge_across_projects(self) -> None:
        value = '{"url_down": "http://example.org"}'
        a1 = Channel.objects.create(project=self.project, kind="webhook", value=value)
        a2 = Channel.objects.create(project=self.project, kind="webhook", value=value)
        b1 = Channel.objects.create(project=self.bobs_project, kind="webhook", value=value)
        b2 = Channel.objects.create(project=self.bobs_project, kind="webhook", value=value)

        stats = mergechannels.merge_duplicate_channels()

        # Two independent within-project merges, never one cross-project group.
        self.assertEqual(len(stats.merged), 2)
        for group in stats.merged:
            self.assertEqual(len(group.channel_codes), 2)
        surviving = set(Channel.objects.values_list("id", flat=True))
        self.assertEqual(surviving, {a1.id, b1.id})
        self.assertNotIn(a2.id, surviving)
        self.assertNotIn(b2.id, surviving)

    def test_single_channels_in_different_projects_are_untouched(self) -> None:
        value = '{"url_down": "http://example.org"}'
        Channel.objects.create(project=self.project, kind="webhook", value=value)
        Channel.objects.create(project=self.bobs_project, kind="webhook", value=value)

        stats = mergechannels.merge_duplicate_channels()

        self.assertEqual(stats.merged, [])
        self.assertEqual(Channel.objects.count(), 2)

    def test_it_does_not_merge_different_values(self) -> None:
        Channel.objects.create(
            project=self.project, kind="webhook",
            value='{"url_down": "http://example.org"}',
        )
        Channel.objects.create(
            project=self.project, kind="webhook",
            value='{"url_down": "http://example.com"}',
        )

        stats = mergechannels.merge_duplicate_channels()

        self.assertEqual(stats.merged, [])
        self.assertEqual(Channel.objects.count(), 2)

    def test_it_does_not_merge_different_kinds(self) -> None:
        Channel.objects.create(project=self.project, kind="slack", value="http://x/1")
        Channel.objects.create(project=self.project, kind="mattermost", value="http://x/1")

        stats = mergechannels.merge_duplicate_channels()

        self.assertEqual(stats.merged, [])
        self.assertEqual(Channel.objects.count(), 2)

    def test_it_rejects_when_earliest_is_ambiguous(self) -> None:
        ts = now() - td(days=3)
        value = '{"url_down": "http://example.org"}'
        c1 = Channel.objects.create(project=self.project, kind="webhook",
                                    value=value, created=ts)
        c2 = Channel.objects.create(project=self.project, kind="webhook",
                                    value=value, created=ts)
        c3 = Channel.objects.create(project=self.project, kind="webhook",
                                    value=value, created=ts)
        check = Check.objects.create(project=self.project)
        c1.checks.add(check)
        Notification.objects.create(owner=check, channel=c2, check_status="down")

        stats = mergechannels.merge_duplicate_channels()

        self.assertEqual(stats.merged, [])
        self.assertEqual(len(stats.rejected), 1)
        self.assertEqual(
            stats.rejected[0].reason, mergechannels.AMBIGUOUS_SURVIVOR
        )
        # The whole group is left untouched.
        self.assertEqual(Channel.objects.count(), 3)
        self.assertEqual(Notification.objects.get(channel=c2).check_status, "down")
        self.assertTrue(c1.checks.filter(id=check.id).exists())

    def test_it_rejects_when_referenced_by_group_channel(self) -> None:
        older, newer = self.make_duplicates()
        group = Channel.objects.create(
            project=self.project, kind="group", value=str(newer.code)
        )

        stats = mergechannels.merge_duplicate_channels()

        self.assertEqual(stats.merged, [])
        self.assertEqual(len(stats.rejected), 1)
        self.assertEqual(
            stats.rejected[0].reason, mergechannels.REFERENCED_BY_GROUP
        )
        # Both channels and the referencing group stay as they were.
        self.assertEqual(Channel.objects.count(), 3)
        group.refresh_from_db()
        self.assertEqual(group.value, str(newer.code))
        self.assertTrue(Channel.objects.filter(id=older.id).exists())
        self.assertTrue(Channel.objects.filter(id=newer.id).exists())

    def test_rejected_group_does_not_block_other_groups(self) -> None:
        # A mergeable group.
        ok_older, ok_newer = self.make_duplicates()
        # A rejected group (tied timestamps).
        ts = now() - td(days=5)
        Channel.objects.create(
            project=self.project, kind="slack", value="http://example.org/1",
            created=ts,
        )
        Channel.objects.create(
            project=self.project, kind="slack", value="http://example.org/1",
            created=ts,
        )

        stats = mergechannels.merge_duplicate_channels()

        self.assertEqual(len(stats.merged), 1)
        self.assertEqual(stats.merged[0].kind, "webhook")
        self.assertEqual(len(stats.rejected), 1)
        self.assertEqual(stats.rejected[0].kind, "slack")
        self.assertTrue(Channel.objects.filter(id=ok_older.id).exists())
        self.assertFalse(Channel.objects.filter(id=ok_newer.id).exists())

    def test_dry_run_changes_nothing(self) -> None:
        older, newer = self.make_duplicates()
        check = Check.objects.create(project=self.project)
        newer.checks.add(check)

        stats = mergechannels.merge_duplicate_channels(dry_run=True)

        self.assertEqual(len(stats.merged), 1)
        self.assertEqual(stats.merged[0].survivor_code, str(older.code))
        # Nothing was actually changed or deleted.
        self.assertEqual(Channel.objects.count(), 2)
        self.assertTrue(newer.checks.filter(id=check.id).exists())

    def test_project_scoping(self) -> None:
        value = '{"url_down": "http://example.org"}'
        a1 = Channel.objects.create(project=self.project, kind="webhook", value=value)
        a2 = Channel.objects.create(project=self.project, kind="webhook", value=value)
        b1 = Channel.objects.create(project=self.bobs_project, kind="webhook", value=value)
        b2 = Channel.objects.create(project=self.bobs_project, kind="webhook", value=value)

        stats = mergechannels.merge_duplicate_channels(self.project)

        self.assertEqual(len(stats.merged), 1)
        self.assertEqual(stats.merged[0].project_id, self.project.id)
        self.assertTrue(Channel.objects.filter(id=a1.id).exists())
        self.assertFalse(Channel.objects.filter(id=a2.id).exists())
        # Bob's project is out of scope and stays intact.
        self.assertTrue(Channel.objects.filter(id=b1.id).exists())
        self.assertTrue(Channel.objects.filter(id=b2.id).exists())

    def test_command_dry_run(self) -> None:
        self.make_duplicates()

        stdout = io.StringIO()
        call_command("mergechannels", "--dry-run", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("Would merge 1 group(s)", output)
        self.assertEqual(Channel.objects.count(), 2)

    def test_command_reports_rejections(self) -> None:
        ts = now() - td(days=5)
        value = '{"url_down": "http://example.org"}'
        Channel.objects.create(project=self.project, kind="webhook",
                               value=value, created=ts)
        Channel.objects.create(project=self.project, kind="webhook",
                               value=value, created=ts)

        stdout = Mock()
        call_command("mergechannels", stdout=stdout)

        text = "\n".join(
            call.args[0] for call in stdout.write.call_args_list
        )
        self.assertIn("Rejected (several channels share", text)
        self.assertIn("rejected 1 group(s)", text)
        self.assertEqual(Channel.objects.count(), 2)
