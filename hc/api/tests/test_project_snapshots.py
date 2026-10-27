from __future__ import annotations

from datetime import timedelta as td
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.core.signing import base64_hmac
from django.db import OperationalError
from django.utils.timezone import now

from hc.api.models import Check, Flip, ProjectSnapshotCursor, ProjectStatusSnapshot
from hc.api.snapshots import (
    ProjectStatusUndeterminable,
    catch_up_project,
    current_snapshot,
    record_current_snapshot,
)
from hc.test import BaseTestCase


def master_badge_url(key: str, with_late: bool = True, fmt: str = "json") -> str:
    sig = base64_hmac(str(key), "*", settings.SECRET_KEY, algorithm="sha1")[:8]
    if not with_late:
        sig += "-2"
    return f"/badge/{key}/{sig}.{fmt}"


class ProjectSnapshotTestCase(BaseTestCase):
    def _history(self) -> list[ProjectStatusSnapshot]:
        return list(
            ProjectStatusSnapshot.objects.filter(project=self.project).order_by("id")
        )

    def test_check_add_records_snapshot(self) -> None:
        check = Check.objects.create(project=self.project, name="API")

        snap = self._history()[-1]
        self.assertEqual(snap.source, "check_added")
        self.assertEqual(snap.status, "up")
        self.assertEqual(snap.total, 1)
        self.assertEqual(snap.grace, 0)
        self.assertEqual(snap.down, 0)
        self.assertEqual(
            snap.checks,
            [{"code": str(check.code), "name": "API", "status": "new"}],
        )

    def test_flip_records_snapshot_and_advances_cursor(self) -> None:
        check = Check.objects.create(project=self.project)
        check.status = "up"
        check.last_ping = now()
        check.save(update_fields=["status", "last_ping"])

        flip = Flip(owner=check)
        flip.created = now()
        flip.old_status = "up"
        flip.new_status = "down"
        flip.reason = "fail"
        flip.save()

        snap = self._history()[-1]
        self.assertEqual(snap.source, "check_status")
        self.assertEqual(snap.status, "down")
        self.assertEqual(snap.down, 1)
        self.assertEqual(snap.checks[0]["status"], "down")
        self.assertEqual(snap.created, flip.created)

        cursor = ProjectSnapshotCursor.objects.get(project=self.project)
        self.assertEqual(cursor.last_flip_id, flip.id)

    def test_grace_is_recorded(self) -> None:
        check = Check.objects.create(project=self.project)
        check.status = "up"
        check.timeout = td(hours=1)
        check.grace = td(hours=1)
        # timeout passed 30 minutes ago, grace ends in 30 minutes
        check.last_ping = now() - td(hours=1, minutes=30)
        check.save(update_fields=["status", "last_ping", "timeout", "grace"])

        snap, created = record_current_snapshot(self.project)
        self.assertTrue(created)
        self.assertEqual(snap.status, "grace")
        self.assertEqual(snap.grace, 1)
        self.assertEqual(snap.down, 0)

    def test_unchanged_state_is_not_duplicated(self) -> None:
        Check.objects.create(project=self.project)
        before = len(self._history())

        _, created = record_current_snapshot(self.project)
        self.assertFalse(created)
        self.assertEqual(len(self._history()), before)

    def test_api_rename_records_snapshot(self) -> None:
        check = Check.objects.create(project=self.project, name="Old")

        r = self.csrf_client.post(
            f"/api/v3/checks/{check.code}",
            {"name": "New"},
            content_type="application/json",
            HTTP_X_API_KEY="X" * 32,
        )
        self.assertEqual(r.status_code, 200)

        snap = self._history()[-1]
        self.assertEqual(snap.source, "check_renamed")
        self.assertEqual(snap.checks[0]["name"], "New")

    def test_check_removal_records_snapshot(self) -> None:
        check = Check.objects.create(project=self.project)
        check.rename_and_delete()

        snap = self._history()[-1]
        self.assertEqual(snap.source, "check_removed")
        self.assertEqual(snap.total, 0)
        self.assertEqual(snap.checks, [])

    def test_existing_snapshot_at_timestamp_is_never_replaced(self) -> None:
        check = Check.objects.create(project=self.project)
        t = now() - td(hours=1)
        existing = ProjectStatusSnapshot.objects.create(
            project=self.project,
            created=t,
            status="up",
            total=1,
            grace=0,
            down=0,
            checks=[{"code": str(check.code), "name": "X", "status": "up"}],
            source="initial",
        )

        # Current state is different, but the row at `t` must survive unchanged:
        snap, created = record_current_snapshot(self.project, when=t)
        self.assertFalse(created)
        self.assertEqual(snap.id, existing.id)
        existing.refresh_from_db()
        self.assertEqual(existing.status, "up")
        self.assertEqual(existing.checks[0]["name"], "X")

    def test_manual_snapshot_is_preserved(self) -> None:
        check = Check.objects.create(project=self.project)
        t = now() - td(hours=2)
        manual = ProjectStatusSnapshot.objects.create(
            project=self.project,
            created=t,
            status="up",
            total=1,
            grace=0,
            down=0,
            checks=[{"code": str(check.code), "name": "Hand-kept", "status": "up"}],
            source="initial",
            manual=True,
        )

        catch_up_project(self.project)
        record_current_snapshot(self.project)

        manual.refresh_from_db()
        self.assertTrue(manual.manual)
        self.assertEqual(manual.checks[0]["name"], "Hand-kept")

    def test_undeterminable_state_stops_at_last_confirmed_snapshot(self) -> None:
        Check.objects.create(project=self.project)
        before = len(self._history())

        with patch("hc.api.models.Check.get_status", side_effect=RuntimeError("boom")):
            with self.assertRaises(ProjectStatusUndeterminable):
                record_current_snapshot(self.project)

        self.assertEqual(len(self._history()), before)
        self.assertFalse(
            ProjectSnapshotCursor.objects.filter(project=self.project).exists()
        )

    def test_catchup_stops_on_undeterminable_history(self) -> None:
        check = Check.objects.create(
            project=self.project, created=now() - td(hours=3)
        )
        good = Flip(owner=check)
        good.created = now() - td(hours=2)
        good.old_status = "new"
        good.new_status = "up"
        good.save()

        bad = Flip(owner=check)
        bad.created = now() - td(hours=1)
        bad.old_status = "up"
        bad.new_status = "grace"  # not replayable from flips
        bad.save()

        # Reset to simulate a fresh install replaying old flip history:
        ProjectStatusSnapshot.objects.filter(project=self.project).delete()
        ProjectSnapshotCursor.objects.filter(project=self.project).delete()

        with self.assertRaises(ProjectStatusUndeterminable):
            catch_up_project(self.project)

        # Stopped at the last confirmed snapshot (only the good flip landed),
        # cursor still points at it:
        cursor = ProjectSnapshotCursor.objects.get(project=self.project)
        self.assertEqual(cursor.last_flip_id, good.id)

        # Remove the offending flip, re-run resumes and fills the rest:
        bad.delete()
        processed = catch_up_project(self.project)
        self.assertEqual(processed, 0)

    def test_write_failure_does_not_advance_cursor(self) -> None:
        check = Check.objects.create(project=self.project)
        t = now() - td(hours=1)
        flip = Flip(owner=check)
        flip.created = t
        flip.old_status = "new"
        flip.new_status = "up"
        flip.save()

        # Simulate a crash before anything was persisted:
        ProjectStatusSnapshot.objects.filter(project=self.project).delete()
        cursor = ProjectSnapshotCursor.objects.get(project=self.project)
        cursor.last_flip_id = None
        cursor.save(update_fields=["last_flip_id"])

        with patch.object(
            ProjectStatusSnapshot.objects,
            "create",
            side_effect=OperationalError("disk full"),
        ):
            with self.assertRaises(OperationalError):
                catch_up_project(self.project)

        cursor.refresh_from_db()
        self.assertIsNone(cursor.last_flip_id)
        self.assertEqual(
            ProjectStatusSnapshot.objects.filter(project=self.project).count(), 0
        )

        # Re-run fills the gap exactly once:
        processed = catch_up_project(self.project)
        self.assertEqual(processed, 1)
        rows = ProjectStatusSnapshot.objects.filter(project=self.project)
        self.assertEqual(rows.count(), 1)
        inserted = rows.first()
        assert inserted is not None
        self.assertEqual(inserted.status, "up")
        cursor.refresh_from_db()
        self.assertEqual(cursor.last_flip_id, flip.id)

    def test_catchup_reconstructs_history_and_is_idempotent(self) -> None:
        born = now() - td(hours=4)
        a = Check.objects.create(project=self.project, name="A", created=born)
        b = Check.objects.create(project=self.project, name="B", created=born)

        t1 = now() - td(hours=3)
        t2 = now() - td(hours=2)
        t3 = now() - td(hours=1)

        f1 = Flip(owner=a)
        f1.created, f1.old_status, f1.new_status = t1, "new", "up"
        f1.save()
        f2 = Flip(owner=b)
        f2.created, f2.old_status, f2.new_status = t2, "new", "down"
        f2.reason = "fail"
        f2.save()
        f3 = Flip(owner=b)
        f3.created, f3.old_status, f3.new_status = t3, "down", "up"
        f3.save()

        # Simulate the recorder having been down: wipe snapshots and cursor.
        ProjectStatusSnapshot.objects.filter(project=self.project).delete()
        ProjectSnapshotCursor.objects.filter(project=self.project).delete()

        processed = catch_up_project(self.project)
        self.assertEqual(processed, 3)

        rows = self._history()
        self.assertEqual([r.created for r in rows], [t1, t2, t3])
        self.assertEqual([r.status for r in rows], ["up", "down", "up"])
        self.assertEqual(rows[1].down, 1)
        self.assertEqual(rows[1].total, 2)

        # Re-running does not duplicate anything:
        self.assertEqual(catch_up_project(self.project), 0)
        self.assertEqual(len(self._history()), 3)

        cursor = ProjectSnapshotCursor.objects.get(project=self.project)
        self.assertEqual(cursor.last_flip_id, f3.id)

    def test_catchup_resumes_mid_gap_without_duplicates(self) -> None:
        check = Check.objects.create(project=self.project, created=now() - td(hours=4))

        t1 = now() - td(hours=3)
        t2 = now() - td(hours=2)
        t3 = now() - td(hours=1)
        transitions = (
            (t1, "new", "up"),
            (t2, "up", "down"),
            (t3, "down", "up"),
        )
        for t, old, new in transitions:
            f = Flip(owner=check)
            f.created = t
            f.old_status = old
            f.new_status = new
            f.save()

        ProjectStatusSnapshot.objects.filter(project=self.project).delete()
        ProjectSnapshotCursor.objects.filter(project=self.project).delete()
        self.assertEqual(catch_up_project(self.project), 3)
        self.assertEqual(len(self._history()), 3)

        # Rewind the cursor by two flips as if the last run died mid-replay.
        f1 = Flip.objects.get(created=t1)
        cursor = ProjectSnapshotCursor.objects.get(project=self.project)
        cursor.last_flip_id = f1.id
        cursor.save(update_fields=["last_flip_id"])

        self.assertEqual(catch_up_project(self.project), 2)
        # The already-recorded rows at t2/t3 are reused, nothing is duplicated:
        self.assertEqual(len(self._history()), 3)

    def test_master_badge_reads_latest_snapshot(self) -> None:
        check = Check.objects.create(project=self.project)
        check.status = "up"
        check.last_ping = now()
        check.save(update_fields=["status", "last_ping"])

        flip = Flip(owner=check)
        flip.created = now()
        flip.old_status = "up"
        flip.new_status = "down"
        flip.reason = "fail"
        flip.save()

        r = self.client.get(master_badge_url(self.project.badge_key))
        self.assertEqual(
            r.json(), {"status": "down", "total": 1, "grace": 0, "down": 1}
        )

        snap = self._history()[-1]
        self.assertEqual((snap.status, snap.total, snap.grace, snap.down), ("down", 1, 0, 1))

    def test_master_badge_maps_grace_to_late(self) -> None:
        check = Check.objects.create(project=self.project)
        check.status = "up"
        check.timeout = td(hours=1)
        check.grace = td(hours=1)
        check.last_ping = now() - td(hours=1, minutes=30)
        check.save(update_fields=["status", "last_ping", "timeout", "grace"])
        record_current_snapshot(self.project)

        r = self.client.get(master_badge_url(self.project.badge_key))
        self.assertEqual(
            r.json(), {"status": "late", "total": 1, "grace": 1, "down": 0}
        )

        r = self.client.get(master_badge_url(self.project.badge_key, with_late=False))
        self.assertEqual(r.json()["status"], "up")

    def test_status_page_and_badge_report_same_result(self) -> None:
        check = Check.objects.create(project=self.project)
        check.status = "up"
        check.last_ping = now()
        check.save(update_fields=["status", "last_ping"])

        flip = Flip(owner=check)
        flip.created = now()
        flip.old_status = "up"
        flip.new_status = "down"
        flip.reason = "fail"
        flip.save()

        self.client.login(username="alice@example.org", password="password")
        r = self.client.get(f"/projects/{self.project.code}/checks/status/")
        self.assertEqual(r.status_code, 200)
        overall = r.json()["overall"]

        badge = self.client.get(master_badge_url(self.project.badge_key)).json()
        self.assertEqual(overall["status"], "down")
        self.assertEqual(
            (overall["total"], overall["grace"], overall["down"]),
            (badge["total"], badge["grace"], badge["down"]),
        )

    def test_initial_snapshot_is_materialized_once(self) -> None:
        ProjectStatusSnapshot.objects.filter(project=self.project).delete()

        current_snapshot(self.project)
        first = ProjectStatusSnapshot.objects.filter(project=self.project).count()
        self.assertEqual(first, 1)

        self.client.get(master_badge_url(self.project.badge_key))
        self.assertEqual(
            ProjectStatusSnapshot.objects.filter(project=self.project).count(), first
        )

    def test_command_catches_up_and_samples(self) -> None:
        check = Check.objects.create(project=self.project)
        check.status = "up"
        check.last_ping = now()
        check.save(update_fields=["status", "last_ping"])
        flip = Flip(owner=check)
        flip.created = now()
        flip.old_status = "up"
        flip.new_status = "down"
        flip.reason = "fail"
        flip.save()

        # Wipe, then let the command do the full rebuild for this project:
        ProjectStatusSnapshot.objects.filter(project=self.project).delete()
        ProjectSnapshotCursor.objects.filter(project=self.project).delete()

        out = StringIO()
        call_command("snapshotstatus", project=self.project.code, stdout=out)
        self.assertIn("up to date", out.getvalue())
        statuses = [r.status for r in self._history()]
        self.assertIn("down", statuses)

        # Second run is a no-op:
        count = len(self._history())
        call_command("snapshotstatus", project=self.project.code, stdout=StringIO())
        self.assertEqual(len(self._history()), count)

    def test_command_continues_when_project_undeterminable(self) -> None:
        Check.objects.create(project=self.project)

        out, err = StringIO(), StringIO()
        with patch("hc.api.models.Check.get_status", side_effect=RuntimeError("boom")):
            call_command("snapshotstatus", stdout=out, stderr=err)

        self.assertIn("cannot determine status", err.getvalue())
        # The other projects were still processed:
        self.assertTrue(
            ProjectStatusSnapshot.objects.filter(project=self.bobs_project).exists()
        )
