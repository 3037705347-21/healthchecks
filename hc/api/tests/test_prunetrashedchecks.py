from __future__ import annotations

import io
from datetime import timedelta as td

from django.core.management import call_command
from django.utils.timezone import now

from hc.api.models import (
    Channel,
    Check,
    Flip,
    Notification,
    Ping,
    TrashBatchOperation,
)
from hc.test import BaseTestCase


class PruneTrashedChecksTestCase(BaseTestCase):
    def _make_trashed(self, age_days: int) -> Check:
        check = Check.objects.create(project=self.project)
        Ping.objects.create(owner=check, n=1)
        Flip.objects.create(
            owner=check, created=now(), old_status="up", new_status="down"
        )
        channel = Channel.objects.create(project=self.project, kind="email")
        channel.checks.add(check)
        Notification.objects.create(owner=check, channel=channel, check_status="down")
        check.trash()
        Check.all_objects.filter(id=check.id).update(
            deleted_at=now() - td(days=age_days)
        )
        return check

    def test_it_deletes_expired_checks_and_related_data(self) -> None:
        old = self._make_trashed(31)
        fresh = self._make_trashed(10)

        stdout = io.StringIO()
        call_command("prunetrashedchecks", stdout=stdout)

        self.assertFalse(Check.all_objects.filter(id=old.id).exists())
        self.assertEqual(Ping.objects.filter(owner_id=old.id).count(), 0)
        self.assertEqual(Flip.objects.filter(owner_id=old.id).count(), 0)
        self.assertEqual(Notification.objects.filter(owner_id=old.id).count(), 0)
        self.assertFalse(
            Channel.checks.through.objects.filter(check_id=old.id).exists()
        )
        # The fresh trashed check, and its data, are retained:
        self.assertTrue(Check.all_objects.filter(id=fresh.id).exists())
        self.assertEqual(Ping.objects.filter(owner_id=fresh.id).count(), 1)
        self.assertIn("1 expired", stdout.getvalue())

    def test_it_is_safe_to_run_twice(self) -> None:
        self._make_trashed(31)

        call_command("prunetrashedchecks", stdout=io.StringIO())
        stdout = io.StringIO()
        call_command("prunetrashedchecks", stdout=stdout)

        self.assertEqual(Check.all_objects.count(), 0)
        self.assertIn("0 expired", stdout.getvalue())

    def test_dry_run_deletes_nothing(self) -> None:
        self._make_trashed(31)

        stdout = io.StringIO()
        call_command("prunetrashedchecks", "--dry-run", stdout=stdout)

        self.assertEqual(Check.all_objects.count(), 1)
        self.assertIn("Would permanently delete 1", stdout.getvalue())

    def test_it_prunes_old_batch_operations(self) -> None:
        old_op = TrashBatchOperation.objects.create(
            project=self.project,
            key="old",
            kind="restore",
            codes="[]",
            response_code=200,
            response="{}",
        )
        TrashBatchOperation.objects.filter(id=old_op.id).update(
            completed=now() - td(days=31)
        )
        new_op = TrashBatchOperation.objects.create(
            project=self.project,
            key="new",
            kind="restore",
            codes="[]",
            response_code=200,
            response="{}",
        )
        TrashBatchOperation.objects.filter(id=new_op.id).update(
            completed=now() - td(days=1)
        )

        call_command("prunetrashedchecks", stdout=io.StringIO())

        self.assertFalse(TrashBatchOperation.objects.filter(id=old_op.id).exists())
        self.assertTrue(TrashBatchOperation.objects.filter(id=new_op.id).exists())
