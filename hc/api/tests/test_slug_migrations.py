from __future__ import annotations

import io
import json
import tempfile
from datetime import timedelta as td

from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils.timezone import now

from hc.api import slug_migrations as sm
from hc.api.models import Check, SlugAlias
from hc.test import BaseTestCase


class SlugMigrationServiceTestCase(BaseTestCase):
    def _make_check(self, slug: str) -> Check:
        return Check.objects.create(project=self.project, name=slug, slug=slug)

    # -- groups / submission ------------------------------------------------

    def test_submit_creates_pending_group(self) -> None:
        self._make_check("foo")
        aliases = sm.submit_plan(self.project, [sm.Pair("foo", "bar")])

        self.assertEqual(len(aliases), 1)
        alias = SlugAlias.objects.get()
        self.assertEqual(alias.state, sm.PENDING)
        self.assertEqual(alias.slug, "foo")
        self.assertEqual(alias.new_slug, "bar")
        # The check is not renamed while pending:
        self.assertEqual(alias.owner.slug, "foo")

        groups = sm.groups(self.project)
        self.assertEqual([a.slug for a in groups.pending], ["foo"])
        self.assertEqual(groups.migrated, [])
        self.assertEqual(groups.expired, [])

    def test_groups_are_partitioned_and_project_scoped(self) -> None:
        c1 = self._make_check("a")
        c2 = self._make_check("b")
        SlugAlias.objects.create(
            project=self.project, owner=c1, slug="a", new_slug="a2",
            state=sm.PENDING,
        )
        SlugAlias.objects.create(
            project=self.project, owner=c2, slug="b", new_slug="b2",
            state=sm.MIGRATED,
        )
        SlugAlias.objects.create(
            project=self.bobs_project, owner=c2, slug="c", new_slug="c2",
            state=sm.EXPIRED,
        )

        groups = sm.groups(self.project)
        self.assertEqual({a.slug for a in groups.all()}, {"a", "b"})
        self.assertEqual([a.slug for a in groups.pending], ["a"])
        self.assertEqual([a.slug for a in groups.migrated], ["b"])
        self.assertEqual(groups.expired, [])

    def test_resubmit_is_idempotent(self) -> None:
        self._make_check("foo")
        sm.submit_plan(self.project, [sm.Pair("foo", "bar")])
        sm.submit_plan(self.project, [sm.Pair("foo", "bar")])

        self.assertEqual(SlugAlias.objects.count(), 1)

    def test_dry_run_writes_nothing(self) -> None:
        self._make_check("foo")
        result = sm.submit_plan(
            self.project, [sm.Pair("foo", "bar")], dry_run=True
        )
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0].id)
        self.assertEqual(SlugAlias.objects.count(), 0)

    # -- whole-batch conflicts ---------------------------------------------

    def _assert_batch_rejected(self, pairs: list[sm.Pair], reason: str) -> None:
        with self.assertRaises(sm.SlugMigrationError) as cm:
            sm.submit_plan(self.project, pairs)
        self.assertEqual(cm.exception.conflicts[0].reason, reason)
        # Whole batch rejected, nothing committed:
        self.assertEqual(SlugAlias.objects.count(), 0)

    def test_invalid_slug_conflicts(self) -> None:
        self._assert_batch_rejected(
            [sm.Pair("Foo", "bar")], sm.INVALID_SLUG
        )

    def test_same_slug_conflicts(self) -> None:
        self._make_check("foo")
        self._assert_batch_rejected([sm.Pair("foo", "foo")], sm.SAME_SLUG)

    def test_duplicate_old_conflicts(self) -> None:
        self._make_check("a")
        self._assert_batch_rejected(
            [sm.Pair("a", "x"), sm.Pair("a", "y")], sm.DUPLICATE_OLD
        )

    def test_duplicate_new_conflicts(self) -> None:
        self._make_check("a")
        self._make_check("b")
        self._assert_batch_rejected(
            [sm.Pair("a", "x"), sm.Pair("b", "x")], sm.DUPLICATE_NEW
        )

    def test_chain_conflicts(self) -> None:
        self._make_check("a")
        self._make_check("b")
        self._assert_batch_rejected(
            [sm.Pair("a", "b"), sm.Pair("b", "c")], sm.CHAIN
        )

    def test_old_not_found_conflicts(self) -> None:
        self._assert_batch_rejected(
            [sm.Pair("missing", "bar")], sm.OLD_NOT_FOUND
        )

    def test_old_ambiguous_conflicts(self) -> None:
        self._make_check("foo")
        self._make_check("foo")
        self._assert_batch_rejected(
            [sm.Pair("foo", "bar")], sm.OLD_AMBIGUOUS
        )

    def test_new_occupied_conflicts(self) -> None:
        self._make_check("foo")
        self._make_check("bar")
        self._assert_batch_rejected(
            [sm.Pair("foo", "bar")], sm.NEW_OCCUPIED
        )

    def test_new_is_alias_conflicts(self) -> None:
        alpha = self._make_check("alpha")
        sm.submit_plan(self.project, [sm.Pair("alpha", "beta")])
        sm.advance()
        alpha.refresh_from_db()
        self.assertEqual(alpha.slug, "beta")
        self._make_check("gamma")

        with self.assertRaises(sm.SlugMigrationError) as cm:
            sm.submit_plan(self.project, [sm.Pair("gamma", "alpha")])
        self.assertEqual(cm.exception.conflicts[0].reason, sm.NEW_IS_ALIAS)
        # Only the pre-existing alpha alias, no new pair committed:
        self.assertEqual(SlugAlias.objects.count(), 1)
        self.assertFalse(SlugAlias.objects.filter(slug="gamma").exists())

    def test_alias_target_mismatch_conflicts(self) -> None:
        # A migrated alias foo -> d, check d now carries bar:
        d = self._make_check("foo")
        SlugAlias.objects.create(
            project=self.project, owner=d, slug="foo", new_slug="bar",
            state=sm.MIGRATED, rebinded=now(),
        )
        d.slug = "bar"
        d.save(update_fields=["slug"])
        # Meanwhile a different primary check reclaimed slug "foo":
        c = self._make_check("foo")

        with self.assertRaises(sm.SlugMigrationError) as cm:
            sm.submit_plan(self.project, [sm.Pair("foo", "baz")])
        self.assertEqual(
            cm.exception.conflicts[0].reason, sm.ALIAS_TARGET_MISMATCH
        )
        # Nothing committed: the pre-existing alias is untouched, c keeps foo.
        preexisting = SlugAlias.objects.get(slug="foo")
        self.assertEqual(preexisting.owner_id, d.id)
        self.assertEqual(preexisting.new_slug, "bar")
        c.refresh_from_db()
        self.assertEqual(c.slug, "foo")

    def test_check_busy_conflicts(self) -> None:
        check = self._make_check("foo")
        SlugAlias.objects.create(
            project=self.project, owner=check, slug="older", new_slug="foo",
            state=sm.MIGRATED, rebinded=now(),
        )

        with self.assertRaises(sm.SlugMigrationError) as cm:
            sm.submit_plan(self.project, [sm.Pair("foo", "bar")])
        self.assertEqual(cm.exception.conflicts[0].reason, sm.CHECK_BUSY)
        # Nothing committed: the old alias stays, no foo -> bar pair.
        self.assertEqual(SlugAlias.objects.count(), 1)
        self.assertFalse(SlugAlias.objects.filter(new_slug="bar").exists())
        check.refresh_from_db()
        self.assertEqual(check.slug, "foo")

    def test_one_conflict_rejects_the_whole_batch(self) -> None:
        self._make_check("good")
        # The second pair is invalid; neither must be committed:
        with self.assertRaises(sm.SlugMigrationError):
            sm.submit_plan(
                self.project,
                [sm.Pair("good", "good2"), sm.Pair("bad", "bad2")],
            )
        self.assertEqual(SlugAlias.objects.count(), 0)
        self.assertEqual(Check.objects.get(slug="good").slug, "good")

    # -- advance / expire ---------------------------------------------------

    def test_advance_rebinds_pending_group(self) -> None:
        check = self._make_check("foo")
        sm.submit_plan(self.project, [sm.Pair("foo", "bar")])

        done = sm.advance()
        self.assertEqual(len(done), 1)
        check.refresh_from_db()
        self.assertEqual(check.slug, "bar")
        alias = SlugAlias.objects.get()
        self.assertEqual(alias.state, sm.MIGRATED)
        self.assertIsNotNone(alias.rebinded)

    def test_advance_is_idempotent_and_resumable(self) -> None:
        self._make_check("foo")
        sm.submit_plan(self.project, [sm.Pair("foo", "bar")])
        sm.advance()
        # Second run finds nothing pending:
        self.assertEqual(sm.advance(), [])
        self.assertEqual(SlugAlias.objects.count(), 1)
        self.assertEqual(Check.objects.get().slug, "bar")

    def test_advance_group_stays_pending_on_conflict(self) -> None:
        check = self._make_check("foo")
        sm.submit_plan(self.project, [sm.Pair("foo", "bar")])
        # Someone creates a check on the target slug in the meantime:
        self._make_check("bar")

        with self.assertRaises(sm.SlugMigrationError):
            sm.advance()

        # Nothing was renamed, the group stays pending:
        check.refresh_from_db()
        self.assertEqual(check.slug, "foo")
        self.assertEqual(SlugAlias.objects.get().state, sm.PENDING)

        # Fix the situation and resume:
        Check.objects.filter(slug="bar").delete()
        sm.advance()
        check.refresh_from_db()
        self.assertEqual(check.slug, "bar")
        self.assertEqual(SlugAlias.objects.get().state, sm.MIGRATED)

    def test_advance_allows_same_slug_names_across_projects(self) -> None:
        alice_check = self._make_check("foo")
        bob_check = Check.objects.create(
            project=self.bobs_project, slug="bfoo"
        )
        sm.submit_plan(self.project, [sm.Pair("foo", "shared")])
        sm.submit_plan(self.bobs_project, [sm.Pair("bfoo", "shared")])

        done = sm.advance()
        self.assertEqual(len(done), 2)
        alice_check.refresh_from_db()
        bob_check.refresh_from_db()
        self.assertEqual(alice_check.slug, "shared")
        self.assertEqual(bob_check.slug, "shared")

    def test_advance_flags_unexpected_rename(self) -> None:
        check = self._make_check("foo")
        sm.submit_plan(self.project, [sm.Pair("foo", "bar")])
        check.slug = "gone"
        check.save(update_fields=["slug"])

        with self.assertRaises(sm.SlugMigrationError) as cm:
            sm.advance()
        self.assertEqual(cm.exception.conflicts[0].reason, sm.UNEXPECTED_SLUG)
        self.assertEqual(SlugAlias.objects.get().state, sm.PENDING)

    def test_expire_moves_migrated_group_to_expired(self) -> None:
        self._make_check("foo")
        sm.migrate(self.project, [sm.Pair("foo", "bar")])

        expired = sm.expire_items(self.project, slugs=["foo"])
        self.assertEqual(len(expired), 1)
        alias = SlugAlias.objects.get()
        self.assertEqual(alias.state, sm.EXPIRED)
        self.assertIsNotNone(alias.expired_at)

    def test_expire_unknown_and_pending_slugs_conflict(self) -> None:
        self._make_check("foo")
        sm.submit_plan(self.project, [sm.Pair("foo", "bar")])

        with self.assertRaises(sm.SlugMigrationError) as cm:
            sm.expire_items(self.project, slugs=["foo", "missing"])
        reasons = {c.old: c.reason for c in cm.exception.conflicts}
        self.assertEqual(reasons["foo"], sm.NOT_MIGRATED)
        self.assertEqual(reasons["missing"], sm.UNKNOWN_SLUG)
        # Nothing expired:
        self.assertEqual(SlugAlias.objects.get().state, sm.PENDING)

    def test_expire_after_days(self) -> None:
        old = self._make_check("old")
        recent = self._make_check("new")
        sm.migrate(self.project, [sm.Pair("old", "old2"), sm.Pair("new", "new2")])
        SlugAlias.objects.filter(owner=old).update(rebinded=now() - td(days=30))

        expired = sm.expire_items(self.project, after_days=7)
        self.assertEqual([a.slug for a in expired], ["old"])
        # The recent alias stays migrated:
        self.assertEqual(
            SlugAlias.objects.get(owner=recent).state, sm.MIGRATED
        )

    # -- full migrate + resume ---------------------------------------------

    def test_migrate_runs_all_groups_in_order(self) -> None:
        self._make_check("foo")
        groups = sm.migrate(
            self.project,
            [sm.Pair("foo", "bar")],
            expire_slugs=["foo"],
        )
        self.assertEqual(groups.pending, [])
        self.assertEqual(groups.migrated, [])
        self.assertEqual([a.slug for a in groups.expired], ["foo"])
        self.assertEqual(Check.objects.get().slug, "bar")

    def test_migration_resumes_from_the_completed_group(self) -> None:
        self._make_check("foo")
        self._make_check("baz")
        pairs = [sm.Pair("foo", "bar"), sm.Pair("baz", "qux")]

        # First run returns after registering the pending group only:
        sm.submit_plan(self.project, pairs)
        self.assertEqual(len(sm.groups(self.project).pending), 2)

        # Re-invoking the migration resumes: no duplicates, rebinds both:
        sm.migrate(self.project, pairs)
        self.assertEqual(SlugAlias.objects.count(), 2)
        self.assertEqual(
            {c.slug for c in Check.objects.all()}, {"bar", "qux"}
        )

        # Another incomplete return before the expired group: resume again
        # with the expiry request, only the expired group is left to do:
        groups = sm.migrate(self.project, pairs, expire_slugs=["foo", "baz"])
        self.assertEqual(
            {a.slug for a in groups.expired}, {"foo", "baz"}
        )
        self.assertEqual(
            {a.state for a in SlugAlias.objects.all()}, {sm.EXPIRED}
        )

    # -- reconcile (source add / rollback / delete) ------------------------

    def test_reconcile_addition_converges_to_migrated(self) -> None:
        check = self._make_check("foo")
        result = sm.reconcile(self.project, [sm.Pair("foo", "bar")])

        self.assertEqual(result.added, 1)
        self.assertEqual(result.rebinded, 1)
        check.refresh_from_db()
        self.assertEqual(check.slug, "bar")
        alias = SlugAlias.objects.get()
        self.assertEqual(alias.state, sm.MIGRATED)

    def test_reconcile_is_idempotent(self) -> None:
        self._make_check("foo")
        sm.reconcile(self.project, [sm.Pair("foo", "bar")])
        result = sm.reconcile(self.project, [sm.Pair("foo", "bar")])
        self.assertEqual(result.added, 0)
        self.assertEqual(result.rebinded, 0)
        self.assertEqual(result.rolled_back, 0)
        self.assertEqual(result.expired, 0)
        self.assertEqual(SlugAlias.objects.count(), 1)

    def test_reconcile_deletion_expires_alias(self) -> None:
        self._make_check("foo")
        sm.reconcile(self.project, [sm.Pair("foo", "bar")])

        # Source no longer contains the pair:
        result = sm.reconcile(self.project, [])
        self.assertEqual(result.expired, 1)
        self.assertEqual(SlugAlias.objects.get().state, sm.EXPIRED)

    def test_reconcile_rollback_restores_old_slug_with_reverse_alias(self) -> None:
        check = self._make_check("foo")
        sm.reconcile(self.project, [sm.Pair("foo", "bar")])

        # Source rolls the rename back: new -> old:
        result = sm.reconcile(self.project, [sm.Pair("bar", "foo")])
        self.assertEqual(result.rolled_back, 1)

        check.refresh_from_db()
        self.assertEqual(check.slug, "foo")
        self.assertEqual(Check.objects.get(slug="foo").id, check.id)
        alias = SlugAlias.objects.get()
        self.assertEqual(alias.slug, "bar")
        self.assertEqual(alias.new_slug, "foo")
        self.assertEqual(alias.state, sm.MIGRATED)

        # Both addresses still write into the one history:
        self.assertEqual(
            self.client.get(f"/ping/{self.project.ping_key}/foo").status_code,
            200,
        )
        self.assertEqual(
            self.client.get(f"/ping/{self.project.ping_key}/bar").status_code,
            200,
        )
        self.assertEqual(check.ping_set.count(), 2)

    def test_reconcile_after_rollback_is_idempotent(self) -> None:
        check = self._make_check("foo")
        sm.reconcile(self.project, [sm.Pair("foo", "bar")])
        sm.reconcile(self.project, [sm.Pair("bar", "foo")])

        again = sm.reconcile(self.project, [sm.Pair("bar", "foo")])
        self.assertEqual(again.added, 0)
        self.assertEqual(again.rolled_back, 0)
        self.assertEqual(again.expired, 0)

        check.refresh_from_db()
        self.assertEqual(check.slug, "foo")
        self.assertEqual(SlugAlias.objects.count(), 1)
        # The full history, from before and after every rename, is one set:
        self.assertEqual(check.ping_set.count(), 0)

    def test_reconcile_mixes_converged_pair_and_addition(self) -> None:
        first = self._make_check("foo")
        second = self._make_check("baz")
        sm.reconcile(self.project, [sm.Pair("foo", "bar")])

        result = sm.reconcile(
            self.project, [sm.Pair("foo", "bar"), sm.Pair("baz", "qux")]
        )
        self.assertEqual(result.added, 1)
        self.assertEqual(result.rebinded, 1)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.slug, "bar")
        self.assertEqual(second.slug, "qux")

    def test_reconcile_conflict_changes_nothing(self) -> None:
        self._make_check("foo")
        sm.reconcile(self.project, [sm.Pair("foo", "bar")])

        with self.assertRaises(sm.SlugMigrationError):
            # The old slug "foo" is an alias now, not a primary check:
            sm.reconcile(self.project, [sm.Pair("foo", "different")])

        alias = SlugAlias.objects.get()
        self.assertEqual(alias.state, sm.MIGRATED)
        self.assertEqual(alias.new_slug, "bar")

    def test_reconcile_dry_run_writes_nothing(self) -> None:
        self._make_check("foo")
        result = sm.reconcile(
            self.project, [sm.Pair("foo", "bar")], dry_run=True
        )
        self.assertEqual(result.added, 1)
        self.assertEqual(SlugAlias.objects.count(), 0)
        self.assertEqual(Check.objects.get().slug, "foo")

    # -- prune ---------------------------------------------------------------

    def test_prune_expired(self) -> None:
        self._make_check("foo")
        sm.migrate(
            self.project, [sm.Pair("foo", "bar")], expire_slugs=["foo"]
        )
        SlugAlias.objects.update(expired_at=now() - td(days=30))

        self.assertEqual(sm.prune_expired(self.project, older_than_days=7), 1)
        self.assertEqual(SlugAlias.objects.count(), 0)

    # -- parsing -------------------------------------------------------------

    def test_coerce_pairs_accepts_mapping_and_wrappers(self) -> None:
        self.assertEqual(
            sm.coerce_pairs({"foo": "bar"}), [sm.Pair("foo", "bar")]
        )
        self.assertEqual(
            sm.coerce_pairs({"pairs": [["foo", "bar"]]}),
            [sm.Pair("foo", "bar")],
        )
        self.assertEqual(
            sm.coerce_pairs([{"old": "a", "new": "b"}]),
            [sm.Pair("a", "b")],
        )


class RebindSlugsCommandTestCase(BaseTestCase):
    def _source_file(self, payload: object) -> str:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(payload, f)
            return f.name

    def test_command_reconciles_source_file(self) -> None:
        Check.objects.create(project=self.project, slug="foo")
        path = self._source_file({"pairs": [{"old": "foo", "new": "bar"}]})

        out = io.StringIO()
        call_command(
            "rebindslugs", "--project", str(self.project.code),
            "--source", path, stdout=out,
        )
        text = out.getvalue()
        self.assertIn("Reconciled: 1 added", text)
        self.assertIn("migrated: 1", text)
        self.assertEqual(Check.objects.get().slug, "bar")

    def test_command_dry_run_changes_nothing(self) -> None:
        Check.objects.create(project=self.project, slug="foo")
        path = self._source_file({"foo": "bar"})

        call_command(
            "rebindslugs", "--project", str(self.project.code),
            "--source", path, "--dry-run",
        )
        self.assertEqual(SlugAlias.objects.count(), 0)
        self.assertEqual(Check.objects.get().slug, "foo")

    def test_command_apply_and_expire(self) -> None:
        check = Check.objects.create(project=self.project, slug="foo")
        sm.submit_plan(self.project, [sm.Pair("foo", "bar")])

        out = io.StringIO()
        call_command(
            "rebindslugs", "--project", str(self.project.code),
            "--apply", "--expire", "foo", stdout=out,
        )
        text = out.getvalue()
        self.assertIn("Rebound 1", text)
        self.assertIn("Expired 1", text)
        check.refresh_from_db()
        self.assertEqual(check.slug, "bar")
        self.assertEqual(SlugAlias.objects.get().state, sm.EXPIRED)

    def test_command_reports_conflict_and_nonzero_exit(self) -> None:
        path = self._source_file({"missing": "bar"})
        with self.assertRaises(CommandError):
            call_command(
                "rebindslugs", "--project", str(self.project.code),
                "--source", path,
            )
        self.assertEqual(SlugAlias.objects.count(), 0)

    def test_command_prints_groups_without_actions(self) -> None:
        check = Check.objects.create(project=self.project, slug="foo")
        SlugAlias.objects.create(
            project=self.project, owner=check, slug="old", new_slug="foo",
            state=sm.MIGRATED,
        )

        out = io.StringIO()
        call_command(
            "rebindslugs", "--project", str(self.project.code), stdout=out
        )
        self.assertIn("pending: 0", out.getvalue())
        self.assertIn("migrated: 1", out.getvalue())
