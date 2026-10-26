from __future__ import annotations

from hc.api.models import Channel, Check, Flip, Notification, Ping
from hc.test import BaseTestCase


class RemoveProjectTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()

        self.url = f"/projects/{self.project.code}/remove/"

    def test_it_works(self) -> None:
        Check.objects.create(project=self.project, tags="foo a-B_1  baz@")

        self.client.login(username="alice@example.org", password="password")
        r = self.client.post(self.url)
        self.assertRedirects(r, "/")

        # Alice should not own any projects
        self.assertFalse(self.alice.project_set.exists())

        # Check should be gone
        self.assertFalse(Check.objects.exists())

    def test_it_cascades_live_and_trashed_checks(self) -> None:
        channel = Channel.objects.create(project=self.project, kind="email")

        live = Check.objects.create(project=self.project)
        trashed = Check.objects.create(project=self.project)
        trashed.trash()

        for check in (live, trashed):
            Ping.objects.create(owner=check, n=1)
            Flip.objects.create(
                owner=check, created=live.created, old_status="up", new_status="down"
            )
            Notification.objects.create(
                owner=check, channel=channel, check_status="down"
            )
            channel.checks.add(check)

        self.client.login(username="alice@example.org", password="password")
        r = self.client.post(self.url)
        self.assertRedirects(r, "/")

        # Both the live and the trashed checks are physically gone:
        self.assertEqual(Check.all_objects.count(), 0)
        self.assertEqual(Ping.objects.count(), 0)
        self.assertEqual(Flip.objects.count(), 0)
        self.assertEqual(Notification.objects.count(), 0)
        self.assertEqual(Channel.checks.through.objects.count(), 0)

    def test_it_rejects_get(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 405)

    def test_it_checks_access(self) -> None:
        self.client.login(username="bob@example.org", password="password")
        r = self.client.post(self.url)
        self.assertEqual(r.status_code, 404)
