from __future__ import annotations

from hc.api.models import Check
from hc.test import BaseTestCase


class TrashFrontIsolationTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.check = Check.objects.create(project=self.project, name="foo")

    def _login(self) -> None:
        self.client.login(username="alice@example.org", password="password")

    def test_checks_list_excludes_trashed_check(self) -> None:
        self._login()
        self.check.trash()

        r = self.client.get(f"/projects/{self.project.code}/checks/")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, str(self.check.code))

    def test_details_page_returns_404(self) -> None:
        self._login()
        self.check.trash()

        r = self.client.get(f"/checks/{self.check.code}/details/")
        self.assertEqual(r.status_code, 404)

    def test_status_endpoint_excludes_trashed_check(self) -> None:
        self._login()
        self.check.trash()

        r = self.client.get(f"/projects/{self.project.code}/checks/status/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["details"], [])

    def test_uncloak_returns_404(self) -> None:
        self._login()
        self.check.trash()

        r = self.client.get(f"/cloaked/{self.check.unique_key}/")
        self.assertEqual(r.status_code, 404)

    def test_projects_list_excludes_trashed_check_from_counts(self) -> None:
        self._login()
        self.check.trash()

        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        # The trashed check must not contribute to the project's check count:
        self.assertNotContains(r, str(self.check.code))

    def test_projects_menu_excludes_trashed_check(self) -> None:
        self._login()
        self.check.status = "down"
        self.check.save()
        self.check.trash()

        r = self.client.get("/projects/menu/")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "ic-down")
