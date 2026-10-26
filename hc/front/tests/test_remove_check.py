from __future__ import annotations

from datetime import timedelta as td

from hc.api.models import Channel, Check, Flip, Notification, Ping
from hc.test import BaseTestCase


class RemoveCheckTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.check = Check.objects.create(project=self.project)
        self.remove_url = f"/checks/{self.check.code}/remove/"
        self.redirect_url = f"/projects/{self.project.code}/checks/"

    def test_it_moves_check_to_trash(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        r = self.client.post(self.remove_url, follow=True)
        self.assertRedirects(r, self.redirect_url)
        self.assertContains(r, "Moved to Trash")

        # The check is hidden from the default manager, but retained:
        self.assertEqual(Check.objects.count(), 0)
        self.assertEqual(Check.all_objects.count(), 1)
        self.assertIsNotNone(Check.all_objects.get().deleted_at)

    def test_it_retains_related_data(self) -> None:
        check = Check.objects.create(project=self.project)
        Ping.objects.create(owner=check, n=1)
        channel = Channel.objects.create(project=self.project, kind="email")
        channel.checks.add(check)
        Notification.objects.create(owner=check, channel=channel, check_status="down")
        Flip.objects.create(
            owner=check,
            created=self.check.created,
            old_status="up",
            new_status="down",
        )
        through = Channel.checks.through
        before = (
            Ping.objects.count(),
            Flip.objects.count(),
            Notification.objects.count(),
            through.objects.count(),
        )

        self.client.login(username="alice@example.org", password="password")
        self.client.post(f"/checks/{check.code}/remove/")

        after = (
            Ping.objects.count(),
            Flip.objects.count(),
            Notification.objects.count(),
            through.objects.count(),
        )
        self.assertEqual(after, before)

    def test_team_access_works(self) -> None:
        # Logging in as bob, not alice. Bob has team access so this
        # should work.
        self.client.login(username="bob@example.org", password="password")
        self.client.post(self.remove_url)

        self.assertEqual(Check.objects.count(), 0)
        self.assertEqual(Check.all_objects.count(), 1)

    def test_it_handles_bad_uuid(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        r = self.client.post("/checks/not-uuid/remove/")
        self.assertEqual(r.status_code, 404)

    def test_it_checks_owner(self) -> None:
        self.client.login(username="charlie@example.org", password="password")
        r = self.client.post(self.remove_url)
        self.assertEqual(r.status_code, 404)

    def test_it_handles_missing_uuid(self) -> None:
        # Valid UUID but there is no check for it:
        url = "/checks/6837d6ec-fc08-4da5-a67f-08a9ed1ccf62/remove/"

        self.client.login(username="alice@example.org", password="password")
        r = self.client.post(url)
        self.assertEqual(r.status_code, 404)

    def test_it_rejects_get(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        r = self.client.get(self.remove_url)
        self.assertEqual(r.status_code, 405)

    def test_it_requires_rw_access(self) -> None:
        self.bobs_membership.role = "r"
        self.bobs_membership.save()

        self.client.login(username="bob@example.org", password="password")
        r = self.client.post(self.remove_url)
        self.assertEqual(r.status_code, 403)

    def test_it_clears_nag_date(self) -> None:
        # The check is down and the profile has a scheduled nag:
        self.check.status = "down"
        self.check.save()
        self.profile.nag_period = td(hours=1)
        self.profile.next_nag_date = self.check.created
        self.profile.save()

        self.client.login(username="alice@example.org", password="password")
        self.client.post(self.remove_url)

        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.next_nag_date)
