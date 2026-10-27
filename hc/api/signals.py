"""Signals keeping per-project status snapshots in sync with check data.

* a new Flip -> the project's overall status may have changed;
* a new Check -> the project gained a check.

Check renames and user-initiated removals are handled explicitly (a rename
does not produce a model-level event, and a post_delete hook would also fire
while a project is being cascade-deleted). All hooks are best-effort: they
never break the request that triggered them. The "snapshotstatus" management
command repairs any gap left behind.
"""

from __future__ import annotations

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from hc.accounts.models import Project
from hc.api.models import Check, Flip
from hc.api.snapshots import ingest_flip, try_record_current_snapshot

logger = logging.getLogger("hc")


@receiver(post_save, sender=Flip)
def flip_saved(sender: type[Flip], instance: Flip, **kwargs: object) -> None:
    ingest_flip(instance)


@receiver(post_save, sender=Check)
def check_saved(
    sender: type[Check], instance: Check, created: bool, **kwargs: object
) -> None:
    if not created:
        return

    try:
        project = instance.project
    except Project.DoesNotExist:
        return

    try_record_current_snapshot(project, source="check_added", when=instance.created)
