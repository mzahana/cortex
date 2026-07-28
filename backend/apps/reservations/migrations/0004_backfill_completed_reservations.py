"""Data migration (deploy bug fix, code-review finding): backfill the new
`Reservation.Status.COMPLETED` (`0003`) onto reservations that were ALREADY
checked in before this deploy.

`checkout.perform_checkin` only moves `Reservation.status` `fulfilled` ->
`completed` going FORWARD from the moment `0003`/this app's code shipped. Any
reservation whose linked `Checkout` already had `checked_in_at IS NOT NULL`
at deploy time never goes through `perform_checkin` again (checkin on an
already-checked-in `Checkout` is an idempotent no-op, and `cancel_reservation`
rejects a `fulfilled` reservation outright) -- without this backfill such a
reservation is stuck at `fulfilled` forever: permanently inside
`Reservation.ACTIVE_STATUSES`, permanently blocking its window in the `0002`
GiST exclusion constraint, with no API path to ever move it out.

**Query, not `.get()`:** a reservation is looked up by "does ANY linked
`Checkout` have `checked_in_at IS NOT NULL`" via `.exists()`/a
`values_list(...).distinct()` id set, not `Checkout.objects.get(...)` --
defensively, in case more than one `Checkout` row is ever linked to the same
`reservation_id` (there should only be one in practice, per
`CheckoutSerializer.create`'s TOCTOU-guarded `fulfilled` transition, but nothing
in the schema itself enforces uniqueness of `Checkout.reservation_id`).

**Tenant-aware, `all_objects`:** imports the concrete models directly (safe --
this necessarily runs after the `0001`-`0003` schema it targets) and reads/
writes through `all_objects`, same reasoning as
`apps.projects.migrations.0006_seed_expense_categories`: migrations run as the
table owner outside any request/task tenant context, so the default
`TenantScopedManager` (fail-closed, requires a session tenant) can't be used
here -- this backfill must run once, tenant-agnostically, across every tenant's
rows.

**Reverse is a documented one-way no-op, not a real inverse (CLAUDE.md/
`add-migration` convention -- same trade-off as `0006_seed_expense_categories`'s
name-scoped delete, taken one step further here since even that isn't
available):** once this migration has run, an ordinary reservation
`fulfilled` -> `completed` transition made afterwards by
`checkout.perform_checkin` (the normal, code-driven path, completely
unrelated to this one-time backfill) is indistinguishable at the row level
from one this migration produced -- there is no timestamp/marker column that
tags "the backfill did this specifically". A reverse that flipped
`completed` -> `fulfilled` would therefore incorrectly un-complete every
ordinary checkin that happened to occur after this migration, not just the
stale pre-deploy rows it was meant to fix. Reversing this migration is
consequently not a meaningful operation; the reverse is a documented no-op
(flagged per CLAUDE.md's `risks.md` "documented default" convention) rather
than a silently-wrong data-losing "fix".
"""
from __future__ import annotations

from django.db import migrations


def backfill_completed(apps, schema_editor):
    from apps.reservations.models import Checkout, Reservation

    checked_in_reservation_ids = (
        Checkout.all_objects.filter(checked_in_at__isnull=False, reservation_id__isnull=False)
        .values_list("reservation_id", flat=True)
        .distinct()
    )
    Reservation.all_objects.filter(
        status=Reservation.Status.FULFILLED,
        id__in=list(checked_in_reservation_ids),
    ).update(status=Reservation.Status.COMPLETED)


def noop_reverse(apps, schema_editor):
    # See module docstring: reversing this backfill is not a meaningful
    # operation (it cannot be distinguished from ordinary post-migration
    # `fulfilled` -> `completed` transitions), so the reverse is intentionally
    # a no-op rather than a data-losing "fix".
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0003_alter_reservation_status"),
    ]

    operations = [
        migrations.RunPython(backfill_completed, reverse_code=noop_reverse),
    ]
