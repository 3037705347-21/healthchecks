from __future__ import annotations

from datetime import timedelta as td
from io import StringIO
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils.timezone import now

from hc.api.models import Check, Ping, RedactionJob, RedactionRule, RedactionState
from hc.api.redaction import RedactionFailure, process_job, process_scope
from hc.lib.s3 import GetObjectError
from hc.test import BaseTestCase

CARD = "4111111111111111"


def body_of(ping: Ping) -> bytes:
    assert ping.body_raw is not None
    return bytes(ping.body_raw)


class RedactionTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.check = Check.objects.create(project=self.project)
        self.rule = RedactionRule.objects.create(pattern=r"\d{16}", replacement="[CARD]")

    def test_it_redacts_database_body(self) -> None:
        ping = Ping.objects.create(
            owner=self.check, n=1, body_raw=f"card {CARD}".encode()
        )

        job = process_scope([self.check])

        self.assertEqual((job.processed, job.changed), (1, 1))
        ping.refresh_from_db()
        self.assertEqual(body_of(ping), b"card [CARD]")
        # The event gets a processing state at the current fingerprint
        state = RedactionState.objects.get(ping=ping)
        self.assertEqual(state.fingerprint, RedactionRule.fingerprint())

    def test_it_preserves_unmatched_bytes(self) -> None:
        ping = Ping.objects.create(
            owner=self.check, n=1, body_raw=b"Hello\x01\x99 4111111111111111"
        )

        process_scope([self.check])

        ping.refresh_from_db()
        self.assertEqual(body_of(ping), b"Hello\x01\x99 [CARD]")

    def test_db_and_object_store_get_same_result(self) -> None:
        raw = f"card {CARD}".encode()
        db_ping = Ping.objects.create(owner=self.check, n=1, body_raw=raw)

        obj_ping = Ping.objects.create(owner=self.check, n=2, object_size=len(raw))
        store = {2: raw}

        def fake_get(code: str, n: int) -> bytes:
            return store[n]

        def fake_put(code: object, n: int, data: bytes) -> None:
            store[n] = data

        with patch("hc.api.models.get_object", fake_get), patch(
            "hc.api.redaction.put_object", fake_put
        ):
            process_scope([self.check])

        db_ping.refresh_from_db()
        obj_ping.refresh_from_db()
        self.assertEqual(body_of(db_ping), b"card [CARD]")
        # Same redaction result in object storage, same object key (n unchanged)
        self.assertEqual(store[2], b"card [CARD]")
        self.assertEqual(obj_ping.n, 2)
        self.assertTrue(obj_ping.object_size)
        # Both read paths now return identical bytes
        with patch("hc.api.models.get_object", fake_get):
            self.assertEqual(
                obj_ping.get_body_bytes(), body_of(db_ping)
            )

    def test_second_run_is_a_noop(self) -> None:
        Ping.objects.create(owner=self.check, n=1, body_raw=f"c {CARD}".encode())

        process_scope([self.check])

        # Re-running with the same rule set processes nothing
        job = RedactionJob.objects.create()
        job.checks.add(self.check)
        processed, changed = process_job(job)
        self.assertEqual((processed, changed), (0, 0))
        self.assertEqual(job.status, "done")
        self.assertEqual(RedactionState.objects.count(), 1)

    def test_adding_a_rule_only_reprocesses_stale_content(self) -> None:
        # This ping matches the current (card) rule and gets rewritten
        card_ping = Ping.objects.create(
            owner=self.check, n=1, object_size=200
        )
        # This ping does not match the card rule; it gets confirmed unchanged
        plain_ping = Ping.objects.create(
            owner=self.check, n=2, body_raw=b"contact me at a@example.org"
        )

        store = {1: f"c {CARD}".encode()}

        def fake_get(code: object, n: int) -> bytes:
            return store.get(n, b"")

        def fake_put(code: object, n: int, data: bytes) -> None:
            store[n] = data

        with patch("hc.api.models.get_object", fake_get), patch(
            "hc.api.redaction.put_object", fake_put
        ):
            process_scope([self.check])

            # Add a new rule: the fingerprint changes
            RedactionRule.objects.create(
                pattern=r"[\w.+-]+@[\w-]+\.[\w.-]+", replacement="[EMAIL]"
            )

            job = RedactionJob.objects.create()
            job.checks.add(self.check)
            processed, changed = process_job(job)

        # Both events are re-evaluated, but only the email one is rewritten.
        # The already-redacted card ping is not written to object store again.
        self.assertEqual((processed, changed), (2, 1))
        card_ping.refresh_from_db()
        plain_ping.refresh_from_db()
        self.assertEqual(store[1], b"c [CARD]")
        self.assertEqual(
            body_of(plain_ping), b"contact me at [EMAIL]"
        )
        # Both are now confirmed at the new fingerprint
        fp = RedactionRule.fingerprint()
        self.assertEqual(
            RedactionState.objects.filter(fingerprint=fp).count(), 2
        )

    def test_modifying_a_rule_reprocesses_stale_content(self) -> None:
        ping = Ping.objects.create(owner=self.check, n=1, body_raw=b"id 42")

        process_scope([self.check])
        # Body did not match the card rule, stored body stays untouched
        ping.refresh_from_db()
        self.assertEqual(body_of(ping), b"id 42")

        # Broaden the rule
        self.rule.pattern = r"\d+"
        self.rule.save()

        job = RedactionJob.objects.create()
        job.checks.add(self.check)
        processed, changed = process_job(job)

        self.assertEqual((processed, changed), (1, 1))
        ping.refresh_from_db()
        self.assertEqual(body_of(ping), b"id [CARD]")

    def test_time_range_limits_processing(self) -> None:
        old = Ping.objects.create(
            owner=self.check,
            n=1,
            created=now() - td(days=10),
            body_raw=f"a {CARD}".encode(),
        )
        mid = Ping.objects.create(
            owner=self.check,
            n=2,
            created=now() - td(days=5),
            body_raw=f"b {CARD}".encode(),
        )
        recent = Ping.objects.create(
            owner=self.check,
            n=3,
            created=now(),
            body_raw=f"c {CARD}".encode(),
        )

        process_scope(
            [self.check], start=now() - td(days=7), end=now() - td(days=1)
        )

        old.refresh_from_db()
        mid.refresh_from_db()
        recent.refresh_from_db()
        self.assertEqual(body_of(old), f"a {CARD}".encode())
        self.assertEqual(body_of(mid), b"b [CARD]")
        self.assertEqual(body_of(recent), f"c {CARD}".encode())

    def test_it_processes_multiple_checks(self) -> None:
        other_check = Check.objects.create(project=self.project)
        outside = Check.objects.create(project=self.charlies_project)

        ping = Ping.objects.create(
            owner=self.check, n=1, body_raw=f"a {CARD}".encode()
        )
        other_ping = Ping.objects.create(
            owner=other_check, n=1, body_raw=f"b {CARD}".encode()
        )
        outside_ping = Ping.objects.create(
            owner=outside, n=1, body_raw=f"c {CARD}".encode()
        )

        process_scope([self.check, other_check])

        for p in (ping, other_ping, outside_ping):
            p.refresh_from_db()
        self.assertEqual(body_of(ping), b"a [CARD]")
        self.assertEqual(body_of(other_ping), b"b [CARD]")
        # Checks outside the scope are not touched
        self.assertEqual(body_of(outside_ping), f"c {CARD}".encode())

    def test_event_identity_stays_unchanged(self) -> None:
        created = now() - td(hours=1)
        ping = Ping.objects.create(
            owner=self.check,
            n=7,
            created=created,
            body_raw=f"x {CARD}".encode(),
        )
        before = ping.to_dict(self.check.code, 3)

        process_scope([self.check])

        ping.refresh_from_db()
        after = ping.to_dict(self.check.code, 3)
        self.assertEqual(ping.n, 7)
        self.assertEqual(ping.created, created)
        # body_url (access entry) stays the same
        self.assertEqual(after["body_url"], before["body_url"])
        self.assertTrue(ping.has_body())

    def test_empty_body_is_confirmed_without_write(self) -> None:
        ping = Ping.objects.create(owner=self.check, n=1)

        with patch("hc.api.redaction.put_object") as put:
            job = process_scope([self.check])

        self.assertEqual((job.processed, job.changed), (1, 0))
        self.assertTrue(RedactionState.objects.filter(ping=ping).exists())
        put.assert_not_called()

    def test_read_failure_is_not_confirmed_and_resumes(self) -> None:
        for n in (1, 2, 3):
            Ping.objects.create(owner=self.check, n=n, object_size=100)

        fetched: list[int] = []

        def fake_get(code: object, n: int) -> bytes:
            fetched.append(n)
            if n == 2:
                raise GetObjectError()
            return f"body {n} {CARD}".encode()

        puts: list[int] = []

        def fake_put(code: object, n: int, data: bytes) -> None:
            puts.append(n)

        job = RedactionJob.objects.create()
        job.checks.add(self.check)

        with patch("hc.api.models.get_object", fake_get), patch(
            "hc.api.redaction.put_object", fake_put
        ):
            with self.assertRaises(RedactionFailure) as cm:
                process_job(job)

        self.assertEqual(cm.exception.operation, "read")
        self.assertEqual(cm.exception.ping_id, Ping.objects.get(n=2).id)

        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        # Cursor points at the last confirmed event (ping n=1)
        self.assertEqual(job.cursor, Ping.objects.get(n=1).id)
        self.assertEqual(job.processed, 1)
        # Only ping n=1 is confirmed
        self.assertEqual(RedactionState.objects.count(), 1)
        self.assertTrue(RedactionState.objects.filter(ping__n=1).exists())

        # Resume after the storage issue is fixed
        def healthy_get(code: object, n: int) -> bytes:
            fetched.append(n)
            return f"body {n} {CARD}".encode()

        with patch("hc.api.models.get_object", healthy_get), patch(
            "hc.api.redaction.put_object", fake_put
        ):
            processed, changed = process_job(job)

        self.assertEqual((processed, changed), (2, 2))
        job.refresh_from_db()
        self.assertEqual(job.status, "done")
        # Confirmed ping n=1 was not read again, resumed from n=2
        self.assertEqual(fetched, [1, 2, 2, 3])
        self.assertEqual(RedactionState.objects.count(), 3)

    def test_write_failure_is_not_confirmed_and_resumes(self) -> None:
        for n in (1, 2):
            Ping.objects.create(owner=self.check, n=n, object_size=100)

        def fake_get(code: object, n: int) -> bytes:
            return f"body {n} {CARD}".encode()

        failing = {"fail": True}

        def flaky_put(code: object, n: int, data: bytes) -> None:
            if failing["fail"]:
                raise RuntimeError("storage is down")

        job = RedactionJob.objects.create()
        job.checks.add(self.check)

        with patch("hc.api.models.get_object", fake_get), patch(
            "hc.api.redaction.put_object", flaky_put
        ):
            with self.assertRaises(RedactionFailure) as cm:
                process_job(job)

        self.assertEqual(cm.exception.operation, "write")
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.cursor, None)
        self.assertFalse(RedactionState.objects.filter(ping__n=1).exists())

        # Storage recovered: resume from the failed event
        failing["fail"] = False
        with patch("hc.api.models.get_object", fake_get), patch(
            "hc.api.redaction.put_object", flaky_put
        ):
            processed, changed = process_job(job)

        self.assertEqual((processed, changed), (2, 2))
        job.refresh_from_db()
        self.assertEqual(job.status, "done")
        self.assertEqual(RedactionState.objects.count(), 2)

    def test_limit_processes_in_batches_and_resumes(self) -> None:
        for n in (1, 2, 3):
            Ping.objects.create(
                owner=self.check, n=n, body_raw=f"x {CARD}".encode()
            )

        job = RedactionJob.objects.create()
        job.checks.add(self.check)

        process_job(job, limit=1)
        job.refresh_from_db()
        self.assertEqual(job.status, "running")
        self.assertEqual(job.processed, 1)

        process_job(job, limit=1)
        job.refresh_from_db()
        self.assertEqual(job.status, "running")
        self.assertEqual(job.processed, 2)

        processed, changed = process_job(job)
        job.refresh_from_db()
        self.assertEqual((processed, changed), (1, 1))
        self.assertEqual(job.status, "done")
        self.assertEqual(job.processed, 3)

    def test_it_rejects_invalid_regex(self) -> None:
        with self.assertRaises(ValidationError):
            RedactionRule.objects.create(pattern="(")

    def test_it_rejects_empty_replacement(self) -> None:
        with self.assertRaises(ValidationError):
            RedactionRule.objects.create(pattern=r"\d+", replacement="")


class RedactionCommandTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.check = Check.objects.create(project=self.project)
        RedactionRule.objects.create(pattern=r"\d{16}", replacement="[CARD]")
        self.ping = Ping.objects.create(
            owner=self.check, n=1, body_raw=f"c {CARD}".encode()
        )

    def test_command_redacts(self) -> None:
        out = StringIO()
        call_command(
            "redactpings", f"--check={self.check.code}", stdout=out
        )
        self.ping.refresh_from_db()
        self.assertEqual(body_of(self.ping), b"c [CARD]")
        self.assertIn("status=done", out.getvalue())

    def test_command_resumes_job(self) -> None:
        Ping.objects.create(
            owner=self.check, n=2, body_raw=f"d {CARD}".encode()
        )

        out = StringIO()
        call_command(
            "redactpings", f"--check={self.check.code}", "--limit=1", stdout=out
        )
        job = RedactionJob.objects.get()
        self.assertEqual(job.status, "running")

        out = StringIO()
        call_command("redactpings", f"--job={job.id}", stdout=out)
        job.refresh_from_db()
        self.assertEqual(job.status, "done")
        self.assertEqual(job.changed, 2)
        self.assertEqual(RedactionState.objects.count(), 2)

    def test_command_requires_check_or_job(self) -> None:
        out = StringIO()
        with self.assertRaises(CommandError):
            call_command("redactpings", stderr=out)

    def test_command_reports_failure(self) -> None:
        from unittest.mock import Mock

        self.ping.object_size = 100
        self.ping.body_raw = None
        self.ping.save()

        with patch("hc.api.models.get_object", Mock(side_effect=GetObjectError)):
            out = StringIO()
            with self.assertRaises(CommandError):
                call_command(
                    "redactpings", f"--check={self.check.code}", stderr=out
                )
        job = RedactionJob.objects.get()
        self.assertEqual(job.status, "failed")


class RedactionEndpointsTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.check = Check(project=self.project)
        self.check.n_pings = 1
        self.check.status = "up"
        self.check.save()
        RedactionRule.objects.create(pattern=r"\d{16}", replacement="[CARD]")

    def test_api_serves_redacted_body(self) -> None:
        Ping.objects.create(owner=self.check, n=1, body_raw=f"c {CARD}".encode())
        process_scope([self.check])

        r = self.client.get(
            f"/api/v1/checks/{self.check.code}/pings/1/body",
            HTTP_X_API_KEY="X" * 32,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"c [CARD]")

    def test_front_serves_redacted_body(self) -> None:
        Ping.objects.create(owner=self.check, n=1, body_raw=f"c {CARD}".encode())
        process_scope([self.check])

        self.client.login(username="alice@example.org", password="password")
        r = self.client.get(f"/checks/{self.check.code}/pings/1/body/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"c [CARD]")
