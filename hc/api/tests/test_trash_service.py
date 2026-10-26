from __future__ import annotations

from datetime import timedelta as td
from typing import Any

from django.utils.timezone import now

from hc.accounts.models import Project
from hc.api.models import (
    Channel,
    Check,
    Flip,
    Notification,
    Ping,
    TrashBatchOperation,
)
from hc.api.trash import execute_batch
from hc.test import BaseTestCase


def make_check(project: Project, **kwargs: Any) -> Check:
    return Check.objects.create(project=project, **kwargs)


class TrashServiceTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.channel = Channel.objects.create(project=self.project, kind="email")

    def _populate(self, check: Check) -> None:
        Ping.objects.create(owner=check, n=1)
        Flip.objects.create(
            owner=check, created=now(), old_status="up", new_status="down"
        )
        self.channel.checks.add(check)
        Notification.objects.create(
            owner=check, channel=self.channel, check_status="down"
        )

    def test_restore_preserves_identity_history_and_channels(self) -> None:
        a = make_check(self.project, name="a", slug="a", status="down")
        b = make_check(self.project, name="b", slug="b")
        for check in (a, b):
            self._populate(check)
            check.trash()

        identity = {
            c.id: (c.code, c.slug, c.badge_key)
            for c in Check.all_objects.filter(id__in=[a.id, b.id])
        }
        before = {
            "pings": Ping.objects.count(),
            "flips": Flip.objects.count(),
            "notifications": Notification.objects.count(),
            "m2m": Channel.checks.through.objects.count(),
        }

        result = execute_batch(
            self.project,
            kind="restore",
            uuids=[str(b.code), str(a.code)],
            key=None,
        )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.body["restored"], sorted([str(a.code), str(b.code)]))

        self.assertEqual(Check.objects.count(), 2)
        for check in Check.all_objects.filter(id__in=[a.id, b.id]):
            self.assertIsNone(check.deleted_at)
            self.assertEqual(check.status, "paused")
            self.assertIsNone(check.alert_after)
            self.assertIsNone(check.last_start)
            self.assertEqual(
                (check.code, check.slug, check.badge_key), identity[check.id]
            )
            # The original channel assignment is visible again after restore:
            self.assertEqual(
                list(check.channel_set.values_list("id", flat=True)),
                [self.channel.id],
            )

        # Nothing was deleted, one processed "paused" flip was added per check:
        self.assertEqual(Ping.objects.count(), before["pings"])
        self.assertEqual(Notification.objects.count(), before["notifications"])
        self.assertEqual(Channel.checks.through.objects.count(), before["m2m"])
        self.assertEqual(Flip.objects.count(), before["flips"] + 2)
        paused_flips = Flip.objects.filter(new_status="paused")
        self.assertEqual(paused_flips.count(), 2)
        # All new flips are already processed (no notifications queued):
        self.assertEqual(paused_flips.filter(processed=None).count(), 0)

    def test_purge_physically_deletes_only_selected(self) -> None:
        a = make_check(self.project)
        b = make_check(self.project)
        c = make_check(self.project)
        d = make_check(self.project)
        for check in (a, b, c):
            self._populate(check)
            check.trash()

        result = execute_batch(
            self.project, kind="purge", uuids=[str(a.code), str(b.code)], key=None
        )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(set(result.body["purged"]), {str(a.code), str(b.code)})
        self.assertEqual(Check.all_objects.count(), 2)
        self.assertFalse(Check.all_objects.filter(id__in=[a.id, b.id]).exists())
        # Unselected trashed check and the live check are untouched:
        self.assertTrue(Check.all_objects.filter(id=c.id).exists())
        self.assertTrue(Check.objects.filter(id=d.id).exists())
        self.assertEqual(Ping.objects.filter(owner=c).count(), 1)
        self.assertEqual(Ping.objects.filter(owner=d).count(), 0)

    def test_purge_repeated_is_rejected(self) -> None:
        a = make_check(self.project)
        a.trash()
        code = str(a.code)

        first = execute_batch(self.project, kind="purge", uuids=[code], key=None)
        self.assertEqual(first.status_code, 200)

        second = execute_batch(self.project, kind="purge", uuids=[code], key=None)
        self.assertEqual(second.status_code, 404)

    # -- validation matrix -------------------------------------------------

    def test_it_rejects_non_array_payload(self) -> None:
        result = execute_batch(self.project, kind="restore", uuids="nope", key=None)
        self.assertEqual(result.status_code, 400)

    def test_it_rejects_invalid_uuid(self) -> None:
        result = execute_batch(
            self.project, kind="restore", uuids=["not-uuid"], key=None
        )
        self.assertEqual(result.status_code, 400)

    def test_it_rejects_empty_selection(self) -> None:
        result = execute_batch(self.project, kind="restore", uuids=[], key=None)
        self.assertEqual(result.status_code, 400)

    def test_it_rejects_other_project_check(self) -> None:
        other = make_check(self.bobs_project)
        other.trash()

        result = execute_batch(
            self.project, kind="restore", uuids=[str(other.code)], key=None
        )
        self.assertEqual(result.status_code, 403)
        self.assertEqual(result.body["code"], str(other.code))
        # It remains in the other project's trash:
        self.assertTrue(
            Check.all_objects.filter(id=other.id, project=self.bobs_project).exists()
        )

    def test_it_rejects_nonexistent_check(self) -> None:
        code = "6837d6ec-fc08-4da5-a67f-08a9ed1ccf62"
        result = execute_batch(self.project, kind="restore", uuids=[code], key=None)
        self.assertEqual(result.status_code, 404)

    def test_restore_rejects_live_check(self) -> None:
        live = make_check(self.project)
        result = execute_batch(
            self.project, kind="restore", uuids=[str(live.code)], key=None
        )
        self.assertEqual(result.status_code, 409)

    def test_purge_rejects_live_check(self) -> None:
        live = make_check(self.project)
        result = execute_batch(
            self.project, kind="purge", uuids=[str(live.code)], key=None
        )
        self.assertEqual(result.status_code, 409)

    def test_restore_rejects_expired_check(self) -> None:
        old = make_check(self.project)
        old.trash()
        Check.all_objects.filter(id=old.id).update(deleted_at=now() - td(days=31))

        result = execute_batch(
            self.project, kind="restore", uuids=[str(old.code)], key=None
        )
        self.assertEqual(result.status_code, 410)
        self.assertIsNotNone(Check.all_objects.get(id=old.id).deleted_at)

    def test_purge_allows_expired_check(self) -> None:
        old = make_check(self.project)
        old.trash()
        Check.all_objects.filter(id=old.id).update(deleted_at=now() - td(days=31))

        result = execute_batch(
            self.project, kind="purge", uuids=[str(old.code)], key=None
        )
        self.assertEqual(result.status_code, 200)

    def test_restore_rejects_slug_collision(self) -> None:
        trashed = make_check(self.project, slug="dup")
        trashed.trash()
        make_check(self.project, slug="dup")  # live replacement

        result = execute_batch(
            self.project, kind="restore", uuids=[str(trashed.code)], key=None
        )
        self.assertEqual(result.status_code, 409)
        self.assertIn("dup", result.body["error"])

    def test_slug_collision_ignores_other_project_and_empty_slugs(self) -> None:
        other = make_check(self.bobs_project, slug="dup")
        other.trash()
        noslug = make_check(self.project, slug="")
        noslug.trash()

        # Same slug in another project, and an empty slug, are fine:
        result = execute_batch(
            self.project, kind="restore", uuids=[str(noslug.code)], key=None
        )
        self.assertEqual(result.status_code, 200)

    def test_slug_collision_ignores_other_trashed_sibling(self) -> None:
        first = make_check(self.project, slug="dup")
        second = make_check(self.project, slug="dup")
        first.trash()
        second.trash()

        # Both trashed; no live slug owner, restore both succeeds:
        result = execute_batch(
            self.project,
            kind="restore",
            uuids=[str(first.code), str(second.code)],
            key=None,
        )
        self.assertEqual(result.status_code, 200)

    def test_restore_rejects_when_over_quota(self) -> None:
        self.profile.check_limit = 1
        self.profile.save()
        trashed = make_check(self.project)
        trashed.trash()
        make_check(self.project)  # the one live check fills the quota

        result = execute_batch(
            self.project, kind="restore", uuids=[str(trashed.code)], key=None
        )
        self.assertEqual(result.status_code, 409)

    def test_restore_succeeds_at_exact_quota_boundary(self) -> None:
        self.profile.check_limit = 2
        self.profile.save()
        trashed = make_check(self.project)
        trashed.trash()
        make_check(self.project)  # 1 live + 1 restored == limit 2

        result = execute_batch(
            self.project, kind="restore", uuids=[str(trashed.code)], key=None
        )
        self.assertEqual(result.status_code, 200)

    def test_batch_is_atomic_when_one_item_is_invalid(self) -> None:
        good = make_check(self.project, slug="good")
        bad = make_check(self.project, slug="bad")
        good.trash()
        bad.trash()
        make_check(self.project, slug="bad")  # makes "bad" un-restorable

        result = execute_batch(
            self.project,
            kind="restore",
            uuids=[str(good.code), str(bad.code)],
            key=None,
        )
        self.assertEqual(result.status_code, 409)

        # Neither item was restored:
        self.assertEqual(Check.objects.count(), 1)
        self.assertIsNotNone(Check.all_objects.get(id=good.id).deleted_at)
        self.assertIsNotNone(Check.all_objects.get(id=bad.id).deleted_at)

    # -- idempotency --------------------------------------------------------

    def test_same_key_replays_response_without_side_effects(self) -> None:
        a = make_check(self.project)
        a.trash()
        payload = [str(a.code)]

        first = execute_batch(self.project, kind="restore", uuids=payload, key="k1")
        flips_after_first = Flip.objects.count()
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.replayed)

        second = execute_batch(self.project, kind="restore", uuids=payload, key="k1")
        self.assertTrue(second.replayed)
        self.assertEqual(second.status_code, first.status_code)
        self.assertEqual(second.body, first.body)
        self.assertEqual(Flip.objects.count(), flips_after_first)

    def test_same_key_different_payload_is_conflict(self) -> None:
        a = make_check(self.project)
        b = make_check(self.project)
        a.trash()
        b.trash()

        execute_batch(self.project, kind="restore", uuids=[str(a.code)], key="k2")
        result = execute_batch(
            self.project, kind="restore", uuids=[str(b.code)], key="k2"
        )
        self.assertEqual(result.status_code, 409)
        # b was not restored:
        self.assertIsNotNone(Check.all_objects.get(id=b.id).deleted_at)

    def test_same_key_different_kind_is_conflict(self) -> None:
        a = make_check(self.project)
        a.trash()
        execute_batch(self.project, kind="restore", uuids=[str(a.code)], key="k3")
        # a is now live; purge of a live check fails anyway, but the key reuse
        # must be reported as an idempotency conflict first:
        result = execute_batch(
            self.project, kind="purge", uuids=[str(a.code)], key="k3"
        )
        self.assertEqual(result.status_code, 409)

    def test_failed_operation_is_replayed_with_same_key(self) -> None:
        live = make_check(self.project)
        first = execute_batch(
            self.project, kind="restore", uuids=[str(live.code)], key="k4"
        )
        self.assertEqual(first.status_code, 409)

        second = execute_batch(
            self.project, kind="restore", uuids=[str(live.code)], key="k4"
        )
        self.assertTrue(second.replayed)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.body, first.body)

    def test_unfinished_operation_can_continue(self) -> None:
        a = make_check(self.project)
        a.trash()
        # Simulate a crashed request that reserved a record but never finished:
        TrashBatchOperation.objects.create(
            project=self.project,
            key="k5",
            kind="restore",
            codes=f'["{a.code}"]',
        )

        result = execute_batch(
            self.project, kind="restore", uuids=[str(a.code)], key="k5"
        )
        self.assertEqual(result.status_code, 200)
        self.assertTrue(Check.objects.filter(id=a.id).exists())

        op = TrashBatchOperation.objects.get(key="k5")
        self.assertEqual(op.response_code, 200)
        self.assertIsNotNone(op.completed)

    def test_long_key_is_rejected(self) -> None:
        a = make_check(self.project)
        a.trash()
        result = execute_batch(
            self.project,
            kind="restore",
            uuids=[str(a.code)],
            key="x" * 65,
        )
        self.assertEqual(result.status_code, 400)
