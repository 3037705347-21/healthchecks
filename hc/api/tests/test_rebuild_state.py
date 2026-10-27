from __future__ import annotations

from datetime import timedelta as td
from typing import Any
from uuid import uuid4

from django.core import mail
from django.db import transaction
from django.utils.timezone import now

from hc.api.models import Check, Flip, Ping, RebuildProgress, RebuildResult
from hc.test import BaseTestCase


def rebuild(check: Check, **kwargs: Any) -> RebuildResult:
    """Lock the check row and run a single rebuild pass."""
    with transaction.atomic():
        locked = Check.objects.select_for_update().get(id=check.id)
        return locked.rebuild_state(**kwargs)


def flip_tuples(check: Check) -> list[tuple[str, str, str]]:
    rows = Flip.objects.filter(owner=check).order_by("created")
    return [(f.old_status, f.new_status, f.reason) for f in rows]


class RebuildStateTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.check = Check.objects.create(
            project=self.project, timeout=td(minutes=30), grace=td(minutes=5)
        )
        # Backdate the creation date so the (created, horizon] rebuild window
        # covers the backdated ping history used in these tests.
        created = now() - td(days=10)
        Check.objects.filter(id=self.check.id).update(created=created)
        self.check.created = created

    def test_it_rebuilds_up_state(self) -> None:
        t1, t2 = now() - td(minutes=40), now() - td(minutes=10)
        Ping.objects.create(owner=self.check, n=1, created=t1)
        Ping.objects.create(owner=self.check, n=2, created=t2)

        # Simulate corrupted state after a migration:
        self.check.status = "down"
        self.check.last_ping = None
        self.check.alert_after = None
        self.check.save()

        result = rebuild(self.check, margin=td(minutes=1))

        self.assertTrue(result.completed)
        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "up")
        self.assertEqual(self.check.last_ping, t2)
        self.assertIsNone(self.check.last_start)
        # alert_after is recomputed from the last heartbeat:
        self.assertEqual(self.check.alert_after, t2 + td(minutes=35))

        # The first success creates a new->up flip; everything is processed.
        flips = Flip.objects.order_by("created")
        self.assertEqual([(f.old_status, f.new_status) for f in flips], [("new", "up")])
        self.assertTrue(all(f.processed is not None for f in flips))
        # No alerts are sent for historical state changes:
        self.assertEqual(len(mail.outbox), 0)

    def test_it_rebuilds_fail(self) -> None:
        t = now() - td(hours=2)
        Ping.objects.create(owner=self.check, n=1, created=t, kind="fail")

        result = rebuild(self.check, margin=td(minutes=1))

        self.assertTrue(result.completed)
        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "down")
        self.assertEqual(self.check.last_ping, t)
        self.assertIsNone(self.check.alert_after)
        self.assertEqual(flip_tuples(self.check), [("new", "down", "fail")])

    def test_it_rebuilds_timeout_episode(self) -> None:
        # t1 -> up; deadline = t1 + 30m timeout + 5m grace.
        # t2 arrives well after that deadline (a downtime episode),
        # t3 arrives recently and leaves the check up again.
        t1 = now() - td(minutes=180)
        t2 = now() - td(minutes=120)
        t3 = now() - td(minutes=10)
        Ping.objects.create(owner=self.check, n=1, created=t1)
        Ping.objects.create(owner=self.check, n=2, created=t2)
        Ping.objects.create(owner=self.check, n=3, created=t3)

        result = rebuild(self.check, margin=td(minutes=1))
        self.assertTrue(result.completed)

        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "up")
        self.assertEqual(
            flip_tuples(self.check),
            [
                ("new", "up", ""),
                ("up", "down", "timeout"),
                ("down", "up", ""),
                ("up", "down", "timeout"),
                ("down", "up", ""),
            ],
        )
        first_timeout = Flip.objects.filter(reason="timeout").earliest("created")
        self.assertEqual(first_timeout.created, t1 + td(minutes=35))

    def test_it_leaves_overdue_check_down(self) -> None:
        # The last ping is older than timeout + grace: the check is down now.
        t = now() - td(hours=2)
        Ping.objects.create(owner=self.check, n=1, created=t)

        rebuild(self.check, margin=td(minutes=1))

        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "down")
        self.assertIsNone(self.check.alert_after)
        self.assertEqual(
            flip_tuples(self.check),
            [("new", "up", ""), ("up", "down", "timeout")],
        )

    def test_it_rebuilds_cron_timeout(self) -> None:
        self.check.kind = "cron"
        self.check.schedule = "* * * * *"
        self.check.grace = td(minutes=1)
        self.check.save()

        t0 = (now() - td(minutes=30)).replace(second=0, microsecond=0)
        t1 = now() - td(seconds=20)
        Ping.objects.create(owner=self.check, n=1, created=t0)
        Ping.objects.create(owner=self.check, n=2, created=t1)

        # margin=0 so the recent heartbeat is inside the settled window
        result = rebuild(self.check, margin=td(seconds=0))
        self.assertTrue(result.completed)

        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "up")
        timeout_flip = Flip.objects.get(reason="timeout")
        # Deadline is the next scheduled fire (t0+1m) plus 1m grace:
        self.assertEqual(timeout_flip.created, t0 + td(minutes=2))

    def test_it_rebuilds_start_and_duration(self) -> None:
        self.check.timeout = td(days=30)
        self.check.save()
        rid = uuid4()
        base = now()
        t1, t2 = base - td(minutes=120), base - td(minutes=110)
        Ping.objects.create(owner=self.check, n=1, created=t1, kind="start", rid=rid)
        Ping.objects.create(owner=self.check, n=2, created=t2, rid=rid)

        result = rebuild(self.check, margin=td(minutes=1))
        self.assertTrue(result.completed)

        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "up")
        self.assertEqual(self.check.last_ping, t2)
        self.assertIsNone(self.check.last_start)
        self.assertEqual(self.check.last_duration, td(minutes=10))

    def test_it_rebuilds_open_start_timeout(self) -> None:
        t = now() - td(minutes=120)
        Ping.objects.create(owner=self.check, n=1, created=t, kind="start")

        result = rebuild(self.check, margin=td(minutes=1))
        self.assertTrue(result.completed)

        self.check.refresh_from_db()
        # A start with no completion goes down at start + grace.
        self.assertEqual(self.check.status, "down")
        self.assertEqual(self.check.last_start, t)
        self.assertIsNone(self.check.alert_after)
        self.assertEqual(flip_tuples(self.check), [("new", "down", "timeout")])

    def test_log_and_ign_pings_do_not_affect_state(self) -> None:
        t1 = now() - td(minutes=10)
        t2 = now() - td(minutes=8)
        t3 = now() - td(minutes=5)
        Ping.objects.create(owner=self.check, n=1, created=t1)
        Ping.objects.create(owner=self.check, n=2, created=t2, kind="log")
        Ping.objects.create(owner=self.check, n=3, created=t3, kind="ign")

        rebuild(self.check, margin=td(minutes=1))

        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "up")
        self.assertEqual(self.check.last_ping, t1)
        self.assertEqual(flip_tuples(self.check), [("new", "up", "")])

    def test_it_does_not_overwrite_live_tail(self) -> None:
        t1 = now() - td(hours=3)
        t2 = now() - td(seconds=10)  # a heartbeat arriving during the rebuild
        Ping.objects.create(owner=self.check, n=1, created=t1)
        Ping.objects.create(owner=self.check, n=2, created=t2)

        self.check.status = "down"
        self.check.last_ping = None
        self.check.save()

        # First pass with the default safety margin: t2 is in the live tail.
        result = rebuild(self.check, margin=td(minutes=5))
        self.assertFalse(result.completed)
        self.assertEqual(result.processed, 1)

        # Live fields must be left untouched.
        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "down")
        self.assertIsNone(self.check.last_ping)

        # The timeout episode before t2 is settled and marked processed.
        self.assertEqual(
            flip_tuples(self.check),
            [("new", "up", ""), ("up", "down", "timeout")],
        )

        # Once the tail is inside the margin, a rerun converges:
        result = rebuild(self.check, margin=td(seconds=0))
        self.assertTrue(result.completed)
        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "up")
        self.assertEqual(self.check.last_ping, t2)
        self.assertEqual(
            flip_tuples(self.check),
            [
                ("new", "up", ""),
                ("up", "down", "timeout"),
                ("down", "up", ""),
            ],
        )

    def test_gap_stops_at_last_confirmed_position(self) -> None:
        t1, t3 = now() - td(hours=3), now() - td(hours=2)
        Ping.objects.create(owner=self.check, n=1, created=t1)
        Ping.objects.create(owner=self.check, n=3, created=t3)  # n=2 missing

        result = rebuild(self.check, margin=td(minutes=1))
        self.assertFalse(result.completed)
        self.assertEqual(result.stop_reason, "gap")
        self.assertEqual(result.processed, 1)

        progress = RebuildProgress.objects.get(owner=self.check)
        self.assertEqual(progress.status, "up")
        self.assertEqual(progress.last_ping_dt, t1)
        # A timeout inside the gap is not certain (the missing ping may have
        # rescued the check), so no downtime flip is emitted:
        self.assertIsNone(progress.down_since)
        self.assertEqual(flip_tuples(self.check), [("new", "up", "")])

        # A rerun resumes from the checkpoint and does not duplicate flips.
        before = list(Flip.objects.order_by("created").values_list("id", flat=True))
        rebuild(self.check, margin=td(minutes=1))
        after = list(Flip.objects.order_by("created").values_list("id", flat=True))
        self.assertEqual(before, after)

    def test_truncated_prefix_anchors_on_first_event(self) -> None:
        t1, t2 = now() - td(minutes=30), now() - td(minutes=10)
        Ping.objects.create(owner=self.check, n=5, created=t1)  # n=1..4 pruned
        Ping.objects.create(owner=self.check, n=6, created=t2)

        result = rebuild(self.check, margin=td(minutes=1))
        self.assertTrue(result.completed)

        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "up")
        self.assertEqual(self.check.last_ping, t2)
        # The transition at the truncated boundary is unknowable and skipped:
        self.assertEqual(flip_tuples(self.check), [])

    def test_truncated_prefix_rebuilds_transitions_after_anchor(self) -> None:
        t1, t2 = now() - td(hours=3), now() - td(minutes=10)
        Ping.objects.create(owner=self.check, n=5, created=t1, kind="fail")
        Ping.objects.create(owner=self.check, n=6, created=t2)

        rebuild(self.check, margin=td(minutes=1))

        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "up")
        # The first (boundary) transition is skipped; down->up is certain.
        self.assertEqual(flip_tuples(self.check), [("down", "up", "")])

    def test_it_preserves_pause_flip(self) -> None:
        t1, tp, t2, t3 = (
            now() - td(minutes=30),
            now() - td(minutes=25),
            now() - td(minutes=20),
            now() - td(minutes=10),
        )
        Ping.objects.create(owner=self.check, n=1, created=t1)
        self.check.status = "up"
        self.check.last_ping = t1
        self.check.save()
        self.check.create_flip("paused", mark_as_processed=True)
        Flip.objects.filter(owner=self.check).update(created=tp)
        # A success while paused re-opens the check (manual_resume is off).
        Ping.objects.create(owner=self.check, n=2, created=t2)
        Ping.objects.create(owner=self.check, n=3, created=t3)

        rebuild(self.check, margin=td(seconds=30))

        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "up")
        self.assertTrue(
            Flip.objects.filter(owner=self.check, new_status="paused").exists()
        )
        self.assertEqual(
            flip_tuples(self.check),
            [
                ("new", "up", ""),
                ("up", "paused", ""),
                ("paused", "up", ""),
            ],
        )

    def test_it_does_not_override_manual_pause(self) -> None:
        Ping.objects.create(owner=self.check, n=1, created=now() - td(hours=3))
        self.check.status = "paused"
        self.check.save()

        result = rebuild(self.check, margin=td(minutes=1))
        self.assertFalse(result.completed)
        self.assertEqual(result.stop_reason, "paused")
        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "paused")

    def test_it_handles_no_pings(self) -> None:
        result = rebuild(self.check)
        self.assertEqual(result.stop_reason, "no_pings")
        self.assertFalse(RebuildProgress.objects.filter(owner=self.check).exists())

    def test_rerun_is_idempotent(self) -> None:
        t1 = now() - td(minutes=180)
        t2 = now() - td(minutes=120)
        Ping.objects.create(owner=self.check, n=1, created=t1)
        Ping.objects.create(owner=self.check, n=2, created=t2)

        rebuild(self.check, margin=td(minutes=1))
        first = list(
            Flip.objects.filter(owner=self.check)
            .order_by("created")
            .values_list("created", "old_status", "new_status", "reason")
        )

        rebuild(self.check, margin=td(minutes=1))
        second = list(
            Flip.objects.filter(owner=self.check)
            .order_by("created")
            .values_list("created", "old_status", "new_status", "reason")
        )
        self.assertEqual(first, second)

    def test_it_converges_over_batched_passes(self) -> None:
        for i in range(1, 4):
            Ping.objects.create(
                owner=self.check, n=i, created=now() - td(minutes=(4 - i) * 10)
            )

        result = rebuild(self.check, margin=td(minutes=1), batch_size=1)
        for _ in range(9):
            if result.completed or result.stop_reason:
                break
            result = rebuild(self.check, margin=td(minutes=1), batch_size=1)

        self.assertTrue(result.completed)
        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "up")
        # Only the first success causes a transition:
        self.assertEqual(flip_tuples(self.check), [("new", "up", "")])

    def test_replay_matches_live_state_machine(self) -> None:
        """Drive the real ping()/sendalerts path, corrupt, then compare."""
        from unittest.mock import Mock, patch

        from hc.api.management.commands.sendalerts import Command as SendAlerts

        self.check.timeout = td(minutes=30)
        self.check.grace = td(minutes=5)
        self.check.save()

        real_now = now()
        clock = Mock()

        def send_success(t: object) -> None:
            clock.return_value = t
            self.check.ping(
                "1.2.3.4", "http", "GET", "t", b"", "success", None
            )

        with patch("hc.api.models.now", clock), patch(
            "hc.api.management.commands.sendalerts.now", clock
        ):
            send_success(real_now - td(hours=3))
            # Grace elapses with no ping -> sendalerts flips to down.
            clock.return_value = real_now - td(hours=2)
            SendAlerts().handle_going_down()
            # A ping arrives: down -> up.
            send_success(real_now - td(hours=1))
            # Grace elapses again.
            clock.return_value = real_now - td(minutes=20)
            SendAlerts().handle_going_down()
            # Recent heartbeat: down -> up, and the check stays up.
            send_success(real_now - td(seconds=30))

        self.check.refresh_from_db()
        expected_fields = (
            self.check.status,
            self.check.last_ping,
            self.check.last_start,
            self.check.last_start_rid,
            self.check.last_duration,
            self.check.alert_after,
        )
        expected_flips = list(
            Flip.objects.filter(owner=self.check)
            .order_by("created")
            .values_list("created", "old_status", "new_status", "reason")
        )

        # Simulate migration corruption: live fields wrong and flips gone.
        Check.objects.filter(id=self.check.id).update(
            status="new",
            last_ping=None,
            last_start=None,
            last_start_rid=None,
            last_duration=None,
            alert_after=None,
        )
        Flip.objects.filter(owner=self.check).delete()

        clock.return_value = real_now
        with patch("hc.api.models.now", clock):
            result = rebuild(self.check, margin=td(seconds=0))

        self.assertTrue(result.completed)
        self.check.refresh_from_db()
        rebuilt_fields = (
            self.check.status,
            self.check.last_ping,
            self.check.last_start,
            self.check.last_start_rid,
            self.check.last_duration,
            self.check.alert_after,
        )
        self.assertEqual(rebuilt_fields, expected_fields)
        rebuilt_flips = list(
            Flip.objects.filter(owner=self.check)
            .order_by("created")
            .values_list("created", "old_status", "new_status", "reason")
        )
        self.assertEqual(rebuilt_flips, expected_flips)
        # The reconstructed history must not produce any notifications:
        self.assertTrue(
            all(f.processed is not None for f in Flip.objects.filter(owner=self.check))
        )
