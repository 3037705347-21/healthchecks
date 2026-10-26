from __future__ import annotations

from django.utils.timezone import now

from hc.api.models import Channel, Check, Flip, Notification, Ping
from hc.test import BaseTestCase


class DeleteCheckTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.check = Check.objects.create(project=self.project)
        self.url = f"/api/v2/checks/{self.check.code}"
        self.urlv1 = f"/api/v1/checks/{self.check.code}"

    def test_it_moves_check_to_trash(self) -> None:
        r = self.client.delete(self.url, HTTP_X_API_KEY="X" * 32)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Access-Control-Allow-Origin"], "*")
        self.assertEqual(r.json()["uuid"], str(self.check.code))

        # It should be hidden from the default manager, but retained:
        self.assertFalse(Check.objects.filter(code=self.check.code).exists())
        self.assertTrue(Check.all_objects.filter(code=self.check.code).exists())

        # Subsequent reads and listings must not show it:
        r = self.client.get(self.url, HTTP_X_API_KEY="X" * 32)
        self.assertEqual(r.status_code, 404)
        r = self.client.get("/api/v2/checks/", HTTP_X_API_KEY="X" * 32)
        self.assertEqual(r.json()["checks"], [])

    def test_it_retains_related_data(self) -> None:
        check = Check.objects.create(project=self.project, alert_after=now())
        Ping.objects.create(owner=check, n=1)
        channel = Channel.objects.create(project=self.project, kind="email")
        channel.checks.add(check)
        Notification.objects.create(owner=check, channel=channel, check_status="down")
        Flip.objects.create(
            owner=check, created=now(), old_status="up", new_status="down"
        )
        through = Channel.checks.through
        before = (
            Ping.objects.count(),
            Flip.objects.count(),
            Notification.objects.count(),
            through.objects.count(),
        )

        r = self.client.delete(f"/api/v2/checks/{check.code}", HTTP_X_API_KEY="X" * 32)
        self.assertEqual(r.status_code, 200)

        after = (
            Ping.objects.count(),
            Flip.objects.count(),
            Notification.objects.count(),
            through.objects.count(),
        )
        self.assertEqual(after, before)
        check.refresh_from_db()
        self.assertIsNotNone(check.deleted_at)
        self.assertIsNone(check.alert_after)
        self.assertEqual(Flip.objects.filter(processed=None).count(), 0)

    def test_it_handles_missing_check(self) -> None:
        self.check.delete()
        r = self.client.delete(self.url, HTTP_X_API_KEY="X" * 32)
        self.assertEqual(r.status_code, 404)

    def test_it_handles_options(self) -> None:
        r = self.client.options(self.url)
        self.assertEqual(r.status_code, 204)
        self.assertIn("DELETE", r["Access-Control-Allow-Methods"])

    def test_it_handles_missing_api_key(self) -> None:
        r = self.client.delete(self.url)
        self.assertContains(r, "missing api key", status_code=401)

    def test_v1_reports_status_started(self) -> None:
        self.check.last_start = now()
        self.check.save()

        r = self.client.delete(self.urlv1, HTTP_X_API_KEY="X" * 32)
        doc = r.json()
        self.assertEqual(doc["status"], "started")
        self.assertTrue(doc["started"])

    def test_v2_reports_started_separately(self) -> None:
        self.check.last_start = now()
        self.check.save()

        r = self.client.delete(self.url, HTTP_X_API_KEY="X" * 32)
        doc = r.json()
        self.assertEqual(doc["status"], "new")
        self.assertTrue(doc["started"])
