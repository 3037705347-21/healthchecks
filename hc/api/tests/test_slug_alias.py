from __future__ import annotations

from django.test.utils import override_settings

from hc.api import slug_migrations as sm
from hc.api.management.commands.smtpd import _process_message
from hc.api.models import Check, Ping, SlugAlias
from hc.test import BaseTestCase


@override_settings(S3_BUCKET=None, PING_EMAIL_DOMAIN="hc.example.com")
class SlugAliasRoutingTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.check = Check.objects.create(project=self.project, name="foo", slug="foo")
        self.old_url = f"/ping/{self.project.ping_key}/foo"
        self.new_url = f"/ping/{self.project.ping_key}/bar"

    def _plan_foo_to_bar(self) -> None:
        sm.submit_plan(self.project, [sm.Pair("foo", "bar")])

    def test_pending_alias_keeps_old_address_working(self) -> None:
        self._plan_foo_to_bar()

        # Still pending: the check keeps its old slug, the alias is already
        # registered, the old address keeps landing on the same history.
        r = self.client.get(self.old_url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Ping.objects.filter(owner=self.check).count(), 1)
        # New address is not live until the rebind.
        self.assertEqual(self.client.get(self.new_url).status_code, 404)

    def test_migrated_alias_writes_both_addresses_into_same_history(self) -> None:
        self._plan_foo_to_bar()
        sm.advance()
        self.check.refresh_from_db()
        self.assertEqual(self.check.slug, "bar")

        # Heartbeat from an old deployment script, using the old slug:
        self.assertEqual(self.client.get(self.old_url).status_code, 200)
        # Heartbeat from the new deployment, using the new slug:
        self.assertEqual(self.client.get(self.new_url).status_code, 200)
        # And sub-paths work through the alias too:
        self.assertEqual(self.client.get(self.old_url + "/fail").status_code, 200)

        pings = Ping.objects.filter(owner=self.check).order_by("id")
        self.assertEqual(pings.count(), 3)
        self.assertEqual([p.kind for p in pings], [None, None, "fail"])

    def test_no_heartbeats_are_lost_during_cutover(self) -> None:
        # Old script pings while the plan is still pending:
        self._plan_foo_to_bar()
        self.client.get(self.old_url)

        # The rebind happens:
        sm.advance()

        # Old and new scripts keep pinging, nothing is lost:
        self.client.get(self.old_url)
        self.client.get(self.new_url)

        self.assertEqual(Ping.objects.filter(owner=self.check).count(), 3)
        self.check.refresh_from_db()
        self.assertEqual(self.check.n_pings, 3)

    def test_expired_alias_returns_not_found(self) -> None:
        self._plan_foo_to_bar()
        sm.advance()
        sm.expire_items(self.project, slugs=["foo"])

        self.assertEqual(self.client.get(self.old_url).status_code, 404)
        # The new address is unaffected:
        self.assertEqual(self.client.get(self.new_url).status_code, 200)

    def test_primary_slug_takes_precedence_over_alias(self) -> None:
        other = Check.objects.create(project=self.project, name="bar", slug="bar")
        # Alias foo -> other, while a primary check with slug foo exists:
        SlugAlias.objects.create(
            project=self.project, owner=other, slug="foo", new_slug="bar"
        )

        self.assertEqual(self.client.get(self.old_url).status_code, 200)
        self.assertEqual(Ping.objects.filter(owner=self.check).count(), 1)
        self.assertEqual(Ping.objects.filter(owner=other).count(), 0)

    def test_it_still_reports_ambiguous_primary_slug(self) -> None:
        Check.objects.create(project=self.project, name="foo2", slug="foo")

        self.assertEqual(self.client.get(self.old_url).status_code, 409)

    def test_alias_does_not_resolve_across_projects(self) -> None:
        self.bobs_project.ping_key = "b" * 22
        self.bobs_project.save()
        self._plan_foo_to_bar()
        sm.advance()

        # Bob has no "foo", Alice's alias must not resolve in Bob's project:
        r = self.client.get(f"/ping/{self.bobs_project.ping_key}/foo")
        self.assertEqual(r.status_code, 404)

    def test_auto_provisioning_uses_active_alias_instead_of_creating(self) -> None:
        self._plan_foo_to_bar()
        sm.advance()

        r = self.client.get(self.old_url + "?create=1")
        self.assertEqual(r.status_code, 200)
        # No new check provisioned, the ping landed on the aliased one:
        self.assertEqual(Check.objects.filter(project=self.project).count(), 1)
        self.assertEqual(Ping.objects.filter(owner=self.check).count(), 1)

    def test_auto_provisioning_reuses_expired_slug(self) -> None:
        self._plan_foo_to_bar()
        sm.advance()
        sm.expire_items(self.project, slugs=["foo"])

        r = self.client.get(self.old_url + "?create=1")
        self.assertEqual(r.status_code, 201)

        # A fresh check now owns the slug, the expired alias is gone:
        self.assertFalse(SlugAlias.objects.filter(slug="foo").exists())
        new_check = Check.objects.exclude(id=self.check.id).get()
        self.assertEqual(new_check.slug, "foo")
        self.assertEqual(Ping.objects.filter(owner=new_check).count(), 1)
        # The renamed check's history was not touched:
        self.assertEqual(Ping.objects.filter(owner=self.check).count(), 0)

    def test_uuid_address_is_unaffected(self) -> None:
        self._plan_foo_to_bar()
        sm.advance()
        sm.expire_items(self.project, slugs=["foo"])

        r = self.client.get(f"/ping/{self.check.code}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Ping.objects.filter(owner=self.check).count(), 1)

    def test_unrelated_check_is_unaffected(self) -> None:
        Check.objects.create(project=self.project, name="baz", slug="baz")
        self._plan_foo_to_bar()
        sm.advance()

        r = self.client.get(f"/ping/{self.project.ping_key}/baz")
        self.assertEqual(r.status_code, 200)
        baz = Check.objects.get(slug="baz")
        self.assertEqual(Ping.objects.filter(owner=baz).count(), 1)

    def test_email_ping_routes_via_migrated_alias(self) -> None:
        self._plan_foo_to_bar()
        sm.advance()

        mailto = f"{self.project.ping_key}+foo@hc.example.com"
        result = _process_message(
            "1.2.3.4", "ops@example.org", mailto, b"backup ok"
        )
        self.assertIn("Processed", result)
        ping = Ping.objects.latest("id")
        self.assertEqual(ping.owner_id, self.check.id)
        self.assertEqual(ping.scheme, "email")

    def test_email_ping_via_expired_alias_is_not_found(self) -> None:
        self._plan_foo_to_bar()
        sm.advance()
        sm.expire_items(self.project, slugs=["foo"])

        mailto = f"{self.project.ping_key}+foo@hc.example.com"
        result = _process_message(
            "1.2.3.4", "ops@example.org", mailto, b"backup ok"
        )
        self.assertIn("Check not found", result)
        self.assertEqual(Ping.objects.count(), 0)
