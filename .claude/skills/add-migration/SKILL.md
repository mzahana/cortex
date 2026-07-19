---
name: add-migration
description: >-
  The procedure for writing a database migration in the Cortex backend so it is
  reversible, ships RLS on any new tenant-owned table, adds the required indexes,
  and is verified by running up AND down against a scratch Postgres. Use for any
  schema change, new model, index, RLS policy, exclusion constraint, or trigger.
  Trigger cues: "add a migration", "new model/table", "add an index", "RLS policy",
  "exclusion constraint", "search_vector trigger", "make X immutable".
---

# Writing an Cortex migration

The database is the last line of defense for tenant isolation and correctness. No
tenant-owned table ships without RLS; no migration ships without a working reverse.
Read `docs/data-model.md` (§3 indexes) and `docs/architecture.md` §3 first.

## Checklist

1. **Prefer Django migrations;** drop to `migrations.RunSQL` only for what the ORM
   can't express: RLS policies, GiST exclusion constraints, triggers, and specialized
   indexes. Every `RunSQL` MUST include a real reverse SQL (not `RunSQL.noop` unless
   truly irreversible and justified).

2. **Extensions.** If you need them and they aren't already created, add
   `CREATE EXTENSION IF NOT EXISTS btree_gist` (exclusion constraints) and `pg_trgm`
   (fuzzy search) in an early migration.

3. **Indexes per `docs/data-model.md` §3.** For a new tenant-owned table add the
   composite `(tenant_id, …)` indexes it will be filtered/sorted by. Reuse the
   documented specifics where relevant: GIN on `search_vector`; `pg_trgm` GIN on
   `name`/`serial_number`; JSONB GIN on `asset_field_value.value`; the open-checkout
   partial index (`checked_in_at IS NULL`); the low-stock partial index; the
   maintenance `next_due_at` index; the audit `(tenant_id, entity_type, entity_id,
   created_at)` index.

4. **RLS on every new tenant-owned table (never optional).** Enable RLS and add a
   policy keyed off the session's current tenant, matching the pattern used on existing
   tables. The app-level filter and the RLS policy must agree. This is the R4 backstop.

5. **Correctness constructs, where the model requires them:**
   - **Overlapping reservations** → GiST `EXCLUDE (asset_id WITH =, tstzrange(start_at,
     end_at) WITH &&)` limited to active statuses.
   - **Audit immutability** → trigger forbidding UPDATE/DELETE on `audit_log`.
   - **Search vector** → trigger maintaining `search_vector` over name/serial/tags/
     custom values.
   - **Stock reconciliation** → constraint/trigger keeping `quantity_on_hand` equal to
     the `StockTxn` ledger sum.

6. **Verify up AND down.** Run the migration forward and backward against a scratch
   Postgres (e.g. `manage.py migrate` then `migrate <app> <prev>`), and confirm both
   succeed. This is required — a migration that can't reverse is not done.

7. **Prove the guarantee.** When you touch RLS, run a quick psql check that a
   cross-tenant `SELECT` is blocked. When you add a constraint/trigger, insert a row
   that should violate it and confirm the DB rejects it.

## Definition of done
Migration is reversible (verified up + down on a scratch DB), new tenant tables have
RLS, required indexes are present, and any correctness construct is proven to reject
the bad case. Then it's ready for **code-reviewer**.
