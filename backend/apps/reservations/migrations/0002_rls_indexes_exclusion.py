"""T3.1 — RLS, the open/overdue partial indexes, and the F4 no-overlap
exclusion constraint for the M3 reservations/checkout app.

Three concerns, each a reversible ``RunSQL`` block, mirroring the stock app's
``0002`` (RLS + partial index + specialized DB constraints layered on top of a
plain ``0001`` CreateModel):

1. **RLS (R4 backstop)** on both tenant-owned tables T3.1 introduces
   (``reservations_reservation``, ``reservations_checkout``) via the shared
   ``apps.tenancy.db.enable_rls_sql`` helper, so the policy is byte-identical to
   every other tenant table — the app-level ``TenantScopedManager`` filter and
   the DB policy cannot drift. Fail-closed: no ``app.current_tenant`` GUC ->
   ``tenant_id = NULL`` -> zero rows.

2. **Checkout partial indexes** (``docs/data-model.md`` §3): the required
   open-items index ``checkout (tenant_id, checked_in_at) WHERE checked_in_at IS
   NULL`` (backs ``GET /checkouts?open=true`` and the "who has it out" scans),
   plus an overdue-scan companion ``(tenant_id, due_at) WHERE checked_in_at IS
   NULL`` so ``GET /checkouts?overdue=true`` (open AND ``due_at < now()``) reads
   only the small open set ordered by due date instead of a full scan. Partial
   indexes -> ``RunSQL`` (Django's ``models.Index(condition=...)`` could express
   the ``IS NULL`` one, but both are kept here together, matching the stock
   low-stock-partial-index precedent and keeping ``0001`` to plain composites).

3. **F4 no-overlap exclusion constraint** (``docs/data-model.md`` §2/§3,
   ``docs/features.md`` F4) — the whole point of this task. A GiST EXCLUDE on
   ``reservations_reservation`` rejecting two ACTIVE reservations for the SAME
   asset whose ``[start_at, end_at)`` windows overlap:

       EXCLUDE USING gist (asset_id WITH =, tstzrange(start_at, end_at) WITH &&)
           WHERE (status IN ('pending', 'approved', 'fulfilled'))

   - Needs ``btree_gist`` (M0, ``tenancy/0002_extensions``) for the ``=`` operator
     class on the ``bigint`` ``asset_id`` inside a GiST index. Confirmed already
     enabled; NOT re-created here.
   - ``tstzrange(start_at, end_at)`` defaults to ``'[)'`` (lower-inclusive,
     upper-exclusive) — exactly the ``[start_at, end_at)`` semantics
     ``docs/data-model.md`` §2 specifies, so back-to-back bookings (one ends at
     the instant the next begins) do NOT conflict.
   - The ``WHERE (status IN (...))`` restricts participation to the ACTIVE
     statuses only (``Reservation.ACTIVE_STATUSES``): a ``cancelled`` / ``rejected``
     / ``expired`` reservation drops out of the index so its freed window can be
     re-booked. If ``Reservation.ACTIVE_STATUSES`` changes, this predicate MUST
     change with it — they are the two halves of one invariant.

   This is the DB-level backstop for the T3.2 service's app-level conflict
   pre-check (surfaced as a clean 409): same "central check, DB is the last line
   of defense" relationship as tenant RLS. Two overlapping active reservations
   are rejected even on a raw ``INSERT`` that never touches Django.

Everything is reversible and idempotent-safe to re-run (``IF [NOT] EXISTS`` /
existence-guarded ``ADD CONSTRAINT`` / ``DROP ... IF EXISTS``).
"""
from __future__ import annotations

from django.db import migrations

from apps.tenancy.db import disable_rls_sql, enable_rls_sql

RESERVATION_TENANT_TABLES = [
    "reservations_reservation",
    "reservations_checkout",
]

# --- 2. checkout open/overdue partial indexes (docs/data-model.md §3) -------
INDEXES_FORWARD = """
CREATE INDEX IF NOT EXISTS reservations_checkout_open_partial
    ON reservations_checkout (tenant_id, checked_in_at)
    WHERE checked_in_at IS NULL;

CREATE INDEX IF NOT EXISTS reservations_checkout_overdue_partial
    ON reservations_checkout (tenant_id, due_at)
    WHERE checked_in_at IS NULL;
"""

INDEXES_REVERSE = """
DROP INDEX IF EXISTS reservations_checkout_overdue_partial;
DROP INDEX IF EXISTS reservations_checkout_open_partial;
"""

# --- 3. F4 no-overlap GiST exclusion constraint ----------------------------
# Existence-guarded ADD so re-running in CI is a no-op (ALTER TABLE ... ADD
# CONSTRAINT has no IF NOT EXISTS for EXCLUDE, hence the DO block).
EXCLUSION_FORWARD = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'reservation_no_overlap_active'
    ) THEN
        ALTER TABLE reservations_reservation
            ADD CONSTRAINT reservation_no_overlap_active
            EXCLUDE USING gist (
                asset_id WITH =,
                tstzrange(start_at, end_at) WITH &&
            )
            WHERE (status IN ('pending', 'approved', 'fulfilled'));
    END IF;
END$$;
"""

EXCLUSION_REVERSE = """
ALTER TABLE reservations_reservation
    DROP CONSTRAINT IF EXISTS reservation_no_overlap_active;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0001_initial"),
        # The exclusion constraint needs btree_gist (the `=` opclass on the
        # bigint asset_id inside a GiST index). Enabled in M0; declared as a
        # dependency so this migration never runs before the extension exists.
        ("tenancy", "0002_extensions"),
    ]

    operations = [
        # 1. RLS on both new tenant-owned tables.
        *[
            migrations.RunSQL(
                sql=enable_rls_sql(table),
                reverse_sql=disable_rls_sql(table),
            )
            for table in RESERVATION_TENANT_TABLES
        ],
        # 2. Open/overdue checkout partial indexes (data-model.md §3).
        migrations.RunSQL(sql=INDEXES_FORWARD, reverse_sql=INDEXES_REVERSE),
        # 3. F4 no-overlap exclusion constraint on active reservations.
        migrations.RunSQL(sql=EXCLUSION_FORWARD, reverse_sql=EXCLUSION_REVERSE),
    ]
