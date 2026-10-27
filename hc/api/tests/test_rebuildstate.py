from __future__ import annotations

from datetime import timedelta as td
from io import StringIO

from django.core.management.base import CommandError
from django.utils.timezone import now

from hc.api.management.commands.rebuildstate import Command
from hc.api.models import Check, Flip, Ping, RebuildProgress
from hc.test import BaseTestCase


class RebuildStateCommandTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.check = Check.objects.create(
            project=self.project, timeout=td(minutes=30), grace=td(minutes=5)
        )
        Ping.objects.create(owner=self.check, n=1, created=now() - td(minutes=10))

    def run_cmd(self, *codes: str) -> str:
        cmd = Command(stdout=StringIO())
        return cmd.handle(
            codes=list(codes),
            all_checks=False,
            margin=60,
            batch_size=1000,
            max_passes=100,
            reset=False,
            dry_run=False,
        )

    def test_it_rebuilds_given_check(self) -> None:
        self.check.status = "down"
        self.check.last_ping = None
        self.check.save()

        output = self.run_cmd(str(self.check.code))

        self.assertIn("[completed]", output)
        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "up")
        self.assertTrue(Flip.objects.filter(owner=self.check).exists())

    def test_it_supports_all(self) -> None:
        stdout = StringIO()
        cmd = Command(stdout=stdout)
        result = cmd.handle(
            codes=[], all_checks=True, margin=60, batch_size=1000,
            max_passes=100, reset=False, dry_run=False,
        )
        self.assertIn(str(self.check.code), result)
        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "up")

    def test_dry_run_does_not_modify_data(self) -> None:
        stdout = StringIO()
        cmd = Command(stdout=stdout)
        cmd.handle(
            codes=[str(self.check.code)], all_checks=False, margin=60,
            batch_size=1000, max_passes=1, reset=False, dry_run=True,
        )

        self.assertFalse(Flip.objects.filter(owner=self.check).exists())
        self.assertFalse(RebuildProgress.objects.filter(owner=self.check).exists())

    def test_it_requires_codes_or_all(self) -> None:
        with self.assertRaises(CommandError):
            Command().handle(
                codes=[], all_checks=False, margin=60, batch_size=1000,
                max_passes=100, reset=False, dry_run=False,
            )

    def test_it_rejects_bad_code(self) -> None:
        with self.assertRaises(CommandError):
            Command().handle(
                codes=["not-a-uuid"], all_checks=False, margin=60,
                batch_size=1000, max_passes=100, reset=False, dry_run=False,
            )

    def test_it_rejects_missing_code(self) -> None:
        from uuid import uuid4

        with self.assertRaises(CommandError):
            Command().handle(
                codes=[str(uuid4())], all_checks=False, margin=60,
                batch_size=1000, max_passes=100, reset=False, dry_run=False,
            )
