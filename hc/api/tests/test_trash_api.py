from __future__ import annotations

import json
from datetime import timedelta as td

from django.utils.timezone import now

from hc.api.models import Check
from hc.test import BaseTestCase


class TrashApiTestCase(BaseTestCase):
    URL = "/api/v3/checks/trash/"

    def test_list_returns_trashed_checks(self) -> None:
        check = Check.objects.create(
            project=self.project, name="Foo", slug="foo", n_pings=3
        )
        check.trash()
        Check.objects.create(project=self.project, name="Live")

        r = self.client.get(self.URL, HTTP_X_API_KEY="X" * 32)
        self.assertEqual(r.status_code, 200)
        doc = r.json()
        self.assertEqual(len(doc["checks"]), 1)
        item = doc["checks"][0]
        self.assertEqual(item["uuid"], str(check.code))
        self.assertEqual(item["name"], "Foo")
        self.assertEqual(item["slug"], "foo")
        self.assertEqual(item["n_pings"], 3)
        self.assertTrue(item["deleted_at"])
        # Expiry must be 30 days after deletion:
        deleted = now().replace(microsecond=0) - td(seconds=5)
        expires = now().fromisoformat(item["expires_at"])
        self.assertTrue(expires - deleted >= td(days=30) - td(seconds=10))

    def test_list_allows_readonly_key(self) -> None:
        self.project.api_key_readonly = "R" * 32
        self.project.save()
        r = self.client.get(self.URL, HTTP_X_API_KEY="R" * 32)
        self.assertEqual(r.status_code, 200)

    def test_list_rejects_bad_key(self) -> None:
        r = self.client.get(self.URL, HTTP_X_API_KEY="Y" * 32)
        self.assertEqual(r.status_code, 401)

    def test_restore_works(self) -> None:
        check = Check.objects.create(project=self.project, name="Foo")
        check.trash()

        payload = json.dumps({"uuids": [str(check.code)]})
        r = self.client.post(
            self.URL + "restore",
            data=payload,
            content_type="application/json",
            HTTP_X_API_KEY="X" * 32,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"restored": [str(check.code)]})

        r = self.client.get(f"/api/v3/checks/{check.code}", HTTP_X_API_KEY="X" * 32)
        self.assertEqual(r.status_code, 200)

    def test_purge_works(self) -> None:
        check = Check.objects.create(project=self.project, name="Foo")
        check.trash()

        payload = json.dumps({"uuids": [str(check.code)]})
        r = self.client.post(
            self.URL + "purge",
            data=payload,
            content_type="application/json",
            HTTP_X_API_KEY="X" * 32,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"purged": [str(check.code)]})
        self.assertFalse(Check.all_objects.filter(id=check.id).exists())

    def test_idempotent_restore_replays_response(self) -> None:
        check = Check.objects.create(project=self.project, name="Foo")
        check.trash()
        payload = json.dumps({"uuids": [str(check.code)]})

        first = self.client.post(
            self.URL + "restore",
            data=payload,
            content_type="application/json",
            HTTP_X_API_KEY="X" * 32,
            HTTP_IDEMPOTENCY_KEY="abc123",
        )
        flips_after_first = check.flip_set.count()
        second = self.client.post(
            self.URL + "restore",
            data=payload,
            content_type="application/json",
            HTTP_X_API_KEY="X" * 32,
            HTTP_IDEMPOTENCY_KEY="abc123",
        )
        self.assertEqual(first.status_code, second.status_code)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(check.flip_set.count(), flips_after_first)

    def test_idempotency_key_reuse_with_other_payload_conflicts(self) -> None:
        a = Check.objects.create(project=self.project)
        b = Check.objects.create(project=self.project)
        a.trash()
        b.trash()

        r = self.client.post(
            self.URL + "restore",
            data=json.dumps({"uuids": [str(a.code)]}),
            content_type="application/json",
            HTTP_X_API_KEY="X" * 32,
            HTTP_IDEMPOTENCY_KEY="shared",
        )
        self.assertEqual(r.status_code, 200)

        r = self.client.post(
            self.URL + "restore",
            data=json.dumps({"uuids": [str(b.code)]}),
            content_type="application/json",
            HTTP_X_API_KEY="X" * 32,
            HTTP_IDEMPOTENCY_KEY="shared",
        )
        self.assertEqual(r.status_code, 409)
        self.assertFalse(Check.objects.filter(id=b.id).exists())

    def test_options_preview_allows_idempotency_header(self) -> None:
        r = self.client.options(self.URL + "restore")
        self.assertEqual(r.status_code, 204)
        self.assertIn("Idempotency-Key", r["Access-Control-Allow-Headers"])

    def test_missing_uuids_is_bad_request(self) -> None:
        r = self.client.post(
            self.URL + "restore",
            data="{}",
            content_type="application/json",
            HTTP_X_API_KEY="X" * 32,
        )
        self.assertEqual(r.status_code, 400)

    def test_other_project_check_is_forbidden(self) -> None:
        other = Check.objects.create(project=self.bobs_project)
        other.trash()

        r = self.client.post(
            self.URL + "restore",
            data=json.dumps({"uuids": [str(other.code)]}),
            content_type="application/json",
            HTTP_X_API_KEY="X" * 32,
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], str(other.code))

    def test_nonexistent_check_is_not_found(self) -> None:
        code = "6837d6ec-fc08-4da5-a67f-08a9ed1ccf62"
        r = self.client.post(
            self.URL + "restore",
            data=json.dumps({"uuids": [code]}),
            content_type="application/json",
            HTTP_X_API_KEY="X" * 32,
        )
        self.assertEqual(r.status_code, 404)

    def test_live_check_conflicts(self) -> None:
        live = Check.objects.create(project=self.project)
        r = self.client.post(
            self.URL + "restore",
            data=json.dumps({"uuids": [str(live.code)]}),
            content_type="application/json",
            HTTP_X_API_KEY="X" * 32,
        )
        self.assertEqual(r.status_code, 409)

    def test_expired_check_is_gone(self) -> None:
        old = Check.objects.create(project=self.project)
        old.trash()
        Check.all_objects.filter(id=old.id).update(deleted_at=now() - td(days=31))

        r = self.client.post(
            self.URL + "restore",
            data=json.dumps({"uuids": [str(old.code)]}),
            content_type="application/json",
            HTTP_X_API_KEY="X" * 32,
        )
        self.assertEqual(r.status_code, 410)

    def test_readonly_key_cannot_mutate(self) -> None:
        self.project.api_key_readonly = "R" * 32
        self.project.save()
        r = self.client.post(
            self.URL + "restore",
            data=json.dumps({"uuids": []}),
            content_type="application/json",
            HTTP_X_API_KEY="R" * 32,
        )
        self.assertEqual(r.status_code, 401)

    def test_repeated_restore_without_key_conflicts(self) -> None:
        check = Check.objects.create(project=self.project)
        check.trash()
        body = json.dumps({"uuids": [str(check.code)]})

        first = self.client.post(
            self.URL + "restore",
            data=body,
            content_type="application/json",
            HTTP_X_API_KEY="X" * 32,
        )
        self.assertEqual(first.status_code, 200)
        second = self.client.post(
            self.URL + "restore",
            data=body,
            content_type="application/json",
            HTTP_X_API_KEY="X" * 32,
        )
        self.assertEqual(second.status_code, 409)
