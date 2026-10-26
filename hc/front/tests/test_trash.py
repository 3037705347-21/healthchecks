from __future__ import annotations

import re
from datetime import timedelta as td

from django.utils.timezone import now

from hc.api.models import Check, Flip
from hc.test import BaseTestCase


class TrashPageTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.trash_url = f"/projects/{self.project.code}/trash/"
        self.restore_url = f"/projects/{self.project.code}/trash/restore"
        self.purge_url = f"/projects/{self.project.code}/trash/purge"
        self.checks_url = f"/projects/{self.project.code}/checks/"

        self.a = Check.objects.create(project=self.project, name="Alpha")
        self.b = Check.objects.create(project=self.project, name="Beta")
        self.a.trash()
        self.b.trash()

    def _login(self, username: str = "alice@example.org") -> None:
        self.client.login(username=username, password="password")

    def _token(self) -> str:
        r = self.client.get(self.trash_url)
        match = re.search(
            r'name="idempotency_key" value="([0-9a-f]+)"', r.content.decode()
        )
        assert match
        return match.group(1)

    def test_it_lists_trashed_checks(self) -> None:
        self._login()
        live = Check.objects.create(project=self.project, name="Live")

        r = self.client.get(self.trash_url)
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("Alpha", body)
        self.assertIn("Beta", body)
        self.assertIn(str(self.a.code), body)
        self.assertIn("Expires", body)
        # The live check is not listed:
        self.assertNotIn(str(live.code), body)

    def test_checks_page_links_to_trash_with_count(self) -> None:
        self._login()
        r = self.client.get(self.checks_url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, self.trash_url)
        self.assertContains(r, "Trash (2)")

    def test_readonly_member_can_view_but_not_modify(self) -> None:
        self.bobs_membership.role = "r"
        self.bobs_membership.save()
        self._login("bob@example.org")

        r = self.client.get(self.trash_url)
        self.assertEqual(r.status_code, 200)
        # All restore checkboxes are disabled for read-only members:
        restore_boxes = re.findall(
            r'class="restore-check batch-check"[^>]*', r.content.decode()
        )
        self.assertEqual(len(restore_boxes), 2)
        self.assertTrue(all("disabled" in box for box in restore_boxes))

        r = self.client.post(self.restore_url, {"uuids": [str(self.a.code)]})
        self.assertEqual(r.status_code, 403)
        r = self.client.post(self.purge_url, {"uuids": [str(self.a.code)]})
        self.assertEqual(r.status_code, 403)

    def test_non_member_gets_404(self) -> None:
        self._login("charlie@example.org")
        r = self.client.get(self.trash_url)
        self.assertEqual(r.status_code, 404)

    def test_restore_works(self) -> None:
        self._login()
        token = self._token()

        r = self.client.post(
            self.restore_url,
            {"uuids": [str(self.a.code), str(self.b.code)], "idempotency_key": token},
            follow=True,
        )
        self.assertRedirects(r, self.trash_url)
        self.assertContains(r, "Restored 2 check(s)")

        self.assertEqual(Check.objects.count(), 2)
        restored = Check.objects.get(id=self.a.id)
        self.assertEqual(restored.status, "paused")

        # Details page and the original ping URL work again:
        r = self.client.get(f"/checks/{self.a.code}/details/")
        self.assertEqual(r.status_code, 200)
        r = self.client.get(f"/ping/{self.a.code}")
        self.assertEqual(r.status_code, 200)

    def test_purge_works_and_leaves_others_intact(self) -> None:
        self._login()
        token = self._token()

        r = self.client.post(
            self.purge_url,
            {"uuids": [str(self.a.code)], "idempotency_key": token},
            follow=True,
        )
        self.assertRedirects(r, self.trash_url)
        self.assertContains(r, "Permanently deleted 1 check(s)")

        self.assertFalse(Check.all_objects.filter(id=self.a.id).exists())
        # The unselected trashed item and no live checks were affected:
        self.assertTrue(Check.all_objects.filter(id=self.b.id).exists())

    def test_conflicting_batch_is_rejected_without_changes(self) -> None:
        self._login()
        token = self._token()
        # Make "a" un-restorable by restoring it concurrently-style:
        self.a_id = self.a.id
        Check.all_objects.filter(id=self.a.id).update(deleted_at=None)
        # Replace with a live check reusing Alpha's slug context; simplest:
        # a is live now, so the mixed batch must be rejected wholesale.

        r = self.client.post(
            self.restore_url,
            {"uuids": [str(self.a.code), str(self.b.code)], "idempotency_key": token},
            follow=True,
        )
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "not in the trash")

        # b was not restored either:
        self.assertIsNotNone(Check.all_objects.get(id=self.b.id).deleted_at)
        # a is still live:
        self.assertTrue(Check.objects.filter(id=self.a_id).exists())

    def test_empty_submission_shows_error(self) -> None:
        self._login()
        token = self._token()
        r = self.client.post(self.restore_url, {"idempotency_key": token}, follow=True)
        self.assertContains(r, "is empty")
        self.assertEqual(Check.objects.count(), 0)

    def test_same_token_replay_has_no_duplicate_effects(self) -> None:
        self._login()
        token = self._token()
        data = {"uuids": [str(self.a.code)], "idempotency_key": token}

        r1 = self.client.post(self.restore_url, data, follow=True)
        self.assertRedirects(r1, self.trash_url)
        flips = Flip.objects.filter(owner=self.a).count()

        # Re-post with the same rendered token:
        r2 = self.client.post(self.restore_url, data, follow=True)
        self.assertRedirects(r2, self.trash_url)
        self.assertEqual(Flip.objects.filter(owner=self.a).count(), flips)

    def test_expired_item_restore_checkbox_is_disabled(self) -> None:
        self._login()
        Check.all_objects.filter(id=self.a.id).update(deleted_at=now() - td(days=31))

        r = self.client.get(self.trash_url)
        body = r.content.decode()
        self.assertIn("Expired", body)
        # The expired row's restore checkbox is disabled, purge is not:
        expired_row = re.search(r'<tr id="%s".*?</tr>' % self.a.code, body, re.DOTALL)
        assert expired_row
        row_html = expired_row.group(0)
        restore_box = re.search(r'class="restore-check batch-check"[^>]*', row_html)
        purge_box = re.search(r'class="purge-check batch-check"[^>]*', row_html)
        assert restore_box and purge_box
        self.assertIn("disabled", restore_box.group(0))
        self.assertNotIn("disabled", purge_box.group(0))

        # Restoring the expired item is rejected server-side:
        token_match = re.search(r'name="idempotency_key" value="([0-9a-f]+)"', body)
        assert token_match
        token = token_match.group(1)
        r = self.client.post(
            self.restore_url,
            {"uuids": [str(self.a.code)], "idempotency_key": token},
            follow=True,
        )
        self.assertContains(r, "retention period has expired")
        self.assertIsNotNone(Check.all_objects.get(id=self.a.id).deleted_at)
