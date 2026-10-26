from __future__ import annotations

import json
from unittest.mock import Mock, patch

from django.utils.timezone import now

from hc.api.management.commands.prunepingsslow import Command as PruneCommand
from hc.api.management.commands.sendalerts import Command as SendAlertsCommand
from hc.api.management.commands.smtpd import _process_message
from hc.api.models import Channel, Check, Flip, Ping
from hc.test import BaseTestCase


class TrashIsolationTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.check = Check.objects.create(project=self.project, name="foo", slug="foo")

    def test_uuid_ping_endpoint_returns_404(self) -> None:
        self.check.trash()

        r = self.client.get(f"/ping/{self.check.code}")
        self.assertEqual(r.status_code, 404)

        self.check.refresh_from_db()
        self.assertEqual(self.check.n_pings, 0)
        self.assertEqual(Ping.objects.count(), 0)

    def test_slug_ping_endpoint_returns_404(self) -> None:
        self.check.trash()

        url = f"/ping/{self.project.ping_key}/foo"
        r = self.client.get(url)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(Ping.objects.count(), 0)

    def test_auto_create_builds_new_check_instead_of_reusing_trashed(self) -> None:
        self.check.n_pings = 5
        self.check.save()
        self.check.trash()

        url = f"/ping/{self.project.ping_key}/foo?create=1"
        r = self.client.get(url)
        self.assertEqual(r.status_code, 201)

        # A brand new live check was created, the trashed one was not reused:
        live = Check.objects.get(slug="foo")
        self.assertNotEqual(live.id, self.check.id)
        self.assertEqual(live.n_pings, 1)

        trashed = Check.all_objects.get(id=self.check.id)
        self.assertIsNotNone(trashed.deleted_at)
        self.assertEqual(trashed.n_pings, 5)

        # A normal slug ping now goes to the new live check:
        r = self.client.get(f"/ping/{self.project.ping_key}/foo")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Ping.objects.filter(owner=trashed).count(), 0)

    def test_smtpd_rejects_trashed_check(self) -> None:
        self.check.trash()

        result = _process_message(
            "1.2.3.4",
            "sender@example.org",
            f"{self.check.code}@localhost",
            b"hello",
        )
        self.assertTrue(result.startswith("Check not found"))
        self.assertEqual(Ping.objects.count(), 0)

    def test_upsert_does_not_match_trashed_check(self) -> None:
        self.check.trash()

        payload = json.dumps({"name": "foo", "unique": ["name"]})
        r = self.client.post(
            "/api/v3/checks/",
            data=payload,
            content_type="application/json",
            HTTP_X_API_KEY="X" * 32,
        )
        self.assertEqual(r.status_code, 201)

        # The trashed row stays trashed, a new live row was created:
        self.assertEqual(Check.objects.count(), 1)
        self.assertEqual(Check.all_objects.count(), 2)
        self.assertIsNotNone(Check.all_objects.get(id=self.check.id).deleted_at)

    def test_sendalerts_ignores_trashed_checks(self) -> None:
        self.check.status = "up"
        self.check.last_ping = now()
        self.check.save()
        self.check.trash()
        # Simulate a stale alert_after surviving in the database:
        Check.all_objects.filter(id=self.check.id).update(alert_after=now())

        result = SendAlertsCommand().handle_going_down()
        self.assertFalse(result)
        self.assertEqual(Flip.objects.count(), 0)

    @patch("hc.api.models.remove_objects", autospec=True)
    def test_prunepingsslow_keeps_trashed_check_pings(
        self, remove_objects: Mock
    ) -> None:
        check = Check.objects.create(project=self.project, n_pings=101)
        Ping.objects.create(owner=check, n=1)
        check.trash()

        PruneCommand(stdout=Mock()).handle()

        # The trashed check's ping history is retained:
        self.assertEqual(Ping.objects.filter(owner=check).count(), 1)

    def test_trashed_check_does_not_consume_quota(self) -> None:
        self.profile.check_limit = 2
        self.profile.save()
        self.assertEqual(self.profile.num_checks_used(), 1)

        self.check.trash()
        self.assertEqual(self.profile.num_checks_used(), 0)
        self.assertEqual(self.project.num_checks_available(), 2)

    def test_check_badge_returns_404_for_trashed_check(self) -> None:
        self.check.trash()
        r = self.client.get(f"/b/2/{self.check.badge_key}.svg")
        self.assertEqual(r.status_code, 404)

    def test_assign_all_checks_skips_trashed_checks(self) -> None:
        self.check.trash()
        through = Channel.checks.through

        channel = Channel.objects.create(project=self.project, kind="email")
        channel.assign_all_checks()
        self.assertFalse(
            through.objects.filter(channel=channel, check=self.check).exists()
        )

        # A later live check does get assigned, trashed one stays untouched:
        live = Check.objects.create(project=self.project)
        channel.assign_all_checks()
        self.assertTrue(through.objects.filter(channel=channel, check=live).exists())
        self.assertFalse(
            through.objects.filter(channel=channel, check=self.check).exists()
        )

    def test_reports_queryset_excludes_trashed_checks(self) -> None:
        self.check.trash()
        self.assertEqual(self.profile.checks_from_all_projects().count(), 0)

        live = Check.objects.create(project=self.project)
        self.assertEqual(
            list(self.profile.checks_from_all_projects().values_list("id", flat=True)),
            [live.id],
        )
