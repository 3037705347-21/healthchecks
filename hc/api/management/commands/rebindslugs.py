from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.shortcuts import get_object_or_404

from hc.accounts.models import Project
from hc.api import slug_migrations as sm
from hc.api.models import SlugAlias


class Command(BaseCommand):
    help = (
        "Manage batch slug migration plans (old slug -> new slug). Plans have "
        "three groups: pending, migrated and expired. Active aliases route "
        "pings sent to old slugs to the renamed checks, so old and new "
        "addresses write into the same ping history. Use --source to "
        "reconcile the stored plan against a source list: additions are "
        "registered and rebound, reversed pairs are rolled back, and pairs "
        "removed from the source expire. A batch with a single conflicting "
        "check is rejected wholesale. Groups are committed independently, so "
        "re-running after an interrupted migration resumes from the "
        "remaining group. Pings by UUID and unrelated checks are untouched."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--project",
            type=str,
            default=None,
            help="Only handle the project with this code",
        )
        parser.add_argument(
            "--source",
            type=str,
            default=None,
            help=(
                "JSON file with the source pairs, e.g. "
                '{"pairs": [{"old": "foo", "new": "bar"}]} '
                'or {"foo": "bar"}; reconciles the plan towards it'
            ),
        )
        parser.add_argument(
            "--pending-only",
            action="store_true",
            dest="pending_only",
            default=False,
            help="With --source, register pairs but do not rebind yet",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            dest="apply",
            default=False,
            help="Rebind all pending aliases (pending -> migrated)",
        )
        parser.add_argument(
            "--expire",
            type=str,
            action="append",
            dest="expire",
            help="Expire a migrated alias with this old slug (repeatable)",
        )
        parser.add_argument(
            "--expire-after-days",
            type=int,
            dest="expire_after_days",
            default=None,
            help="Expire migrated aliases rebound at least N days ago",
        )
        parser.add_argument(
            "--prune-expired-days",
            type=int,
            dest="prune_expired_days",
            default=None,
            help="Delete expired alias records older than N days",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            dest="dry_run",
            default=False,
            help="Report what would happen but change nothing",
        )

    def handle(self, *, project: str | None, source: str | None,
               pending_only: bool, apply: bool, expire: list[str] | None,
               expire_after_days: int | None, prune_expired_days: int | None,
               dry_run: bool, **options: Any) -> None:
        target = get_object_or_404(Project, code=project) if project else None

        try:
            if source is not None:
                with open(source) as f:
                    raw = json.load(f)
                pairs = sm.coerce_pairs(raw)
                if pending_only:
                    self._submit(target, pairs, dry_run)
                else:
                    self._reconcile(target, pairs, dry_run)

            if apply:
                done = sm.advance(target, dry_run=dry_run)
                verb = "Would rebind" if dry_run else "Rebound"
                self.stdout.write(f"{verb} {len(done)} alias(es)")

            if expire is not None or expire_after_days is not None:
                expired = sm.expire_items(
                    target,
                    slugs=expire,
                    after_days=expire_after_days,
                    dry_run=dry_run,
                )
                verb = "Would expire" if dry_run else "Expired"
                self.stdout.write(f"{verb} {len(expired)} alias(es)")

            if prune_expired_days is not None:
                if dry_run:
                    qs = SlugAlias.objects.filter(state=sm.EXPIRED)
                    if target is not None:
                        qs = qs.filter(project=target)
                    n = qs.count()
                else:
                    n = sm.prune_expired(target, older_than_days=prune_expired_days)
                verb = "Would prune" if dry_run else "Pruned"
                self.stdout.write(f"{verb} {n} expired alias(es)")
        except sm.SlugMigrationError as e:
            for conflict in e.conflicts:
                self.stdout.write(
                    f"Conflict {conflict.old} -> {conflict.new}: "
                    f"{conflict.reason}"
                )
            raise CommandError(
                f"batch rejected: {len(e.conflicts)} conflicting pair(s), "
                "nothing was committed"
            ) from e

        self._print_groups(target)

    def _submit(self, target: Project | None, pairs: list[sm.Pair],
                dry_run: bool) -> None:
        aliases = sm.submit_plan(
            target or self._single_project_required(), pairs, dry_run=dry_run
        )
        verb = "Would register" if dry_run else "Registered"
        self.stdout.write(f"{verb} {len(aliases)} alias(es) in the pending group")

    def _reconcile(self, target: Project | None, pairs: list[sm.Pair],
                   dry_run: bool) -> None:
        result = sm.reconcile(
            target or self._single_project_required(), pairs, dry_run=dry_run
        )
        verb = "Would reconcile" if dry_run else "Reconciled"
        self.stdout.write(
            f"{verb}: {result.added} added, {result.rebinded} rebound, "
            f"{result.rolled_back} rolled back, {result.expired} expired"
        )

    def _single_project_required(self) -> Project:
        # Source pairs are project-scoped old/new slugs; require an explicit
        # project when more than one exists.
        if Project.objects.count() == 1:
            return Project.objects.get()
        raise CommandError("--project is required when using --source")

    def _print_groups(self, target: Project | None) -> None:
        plan = sm.groups(target)
        labels = (
            ("pending", plan.pending),
            ("migrated", plan.migrated),
            ("expired", plan.expired),
        )
        for label, items in labels:
            self.stdout.write(f"{label}: {len(items)}")
            for alias in items:
                self.stdout.write(
                    f"  {alias.slug} -> {alias.new_slug} "
                    f"(check {alias.owner.code}, project {alias.project_id})"
                )
