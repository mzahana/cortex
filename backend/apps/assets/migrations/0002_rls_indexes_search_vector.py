"""T1.3 — RLS, search/spec indexes, and the ``search_vector`` trigger for the
M1 asset registry.

Three concerns, each as its own reversible ``RunSQL`` block:

1. **RLS (R4 backstop)** on every tenant-owned table introduced by T1.2
   (``assets_asset``, ``assets_asset_field_value``, ``assets_attachment``,
   ``assets_tag_link``), using the shared ``apps.tenancy.db.enable_rls_sql``
   helper so the policy is byte-identical to the M0 tables — the app-level
   ``TenantScopedManager`` filter and the DB policy cannot drift. Fail-closed:
   no ``app.current_tenant`` GUC -> zero rows.

2. **Indexes per ``docs/data-model.md`` §3** that Django's ORM can't express:
   - GIN on ``assets_asset.search_vector``  (full-text, F10)
   - ``pg_trgm`` GIN on ``assets_asset.name`` and ``.serial_number``  (fuzzy)
   - GIN on ``assets_asset_field_value.value`` (JSONB, spec filters)
   The composite ``(tenant_id, status|category|location|project)`` btrees are
   already emitted by ``0001_initial`` (they're plain ``models.Index`` the ORM
   handles); this migration adds only the specialized ones.

3. **``search_vector`` maintained by trigger** over name + serial_number + tag
   names + custom field values (weights A / B / B / C). See the trigger-design
   docstring on ``assets_compute_search_vector`` below for the invoker-vs-definer
   decision and the RLS reasoning (the crux: SECURITY INVOKER so the trigger can
   never read or write across tenants — fail-closed, never a silent empty
   vector). Existing rows are backfilled in-migration (owner role, bypasses RLS).

Everything is reversible and idempotent-safe to re-run (``IF [NOT] EXISTS`` /
``DROP ... IF EXISTS`` / ``CREATE OR REPLACE``).
"""
from __future__ import annotations

from django.db import migrations

from apps.tenancy.db import disable_rls_sql, enable_rls_sql

ASSET_TENANT_TABLES = [
    "assets_asset",
    "assets_asset_field_value",
    "assets_attachment",
    "assets_tag_link",
]

# --- 2. specialized indexes (data-model.md §3) -----------------------------
INDEXES_FORWARD = """
CREATE INDEX IF NOT EXISTS assets_asset_search_vector_gin
    ON assets_asset USING gin (search_vector);

CREATE INDEX IF NOT EXISTS assets_asset_name_trgm
    ON assets_asset USING gin (name gin_trgm_ops);

CREATE INDEX IF NOT EXISTS assets_asset_serial_number_trgm
    ON assets_asset USING gin (serial_number gin_trgm_ops);

CREATE INDEX IF NOT EXISTS assets_asset_field_value_value_gin
    ON assets_asset_field_value USING gin (value);
"""

INDEXES_REVERSE = """
DROP INDEX IF EXISTS assets_asset_field_value_value_gin;
DROP INDEX IF EXISTS assets_asset_serial_number_trgm;
DROP INDEX IF EXISTS assets_asset_name_trgm;
DROP INDEX IF EXISTS assets_asset_search_vector_gin;
"""

# --- 3. search_vector function + triggers ----------------------------------
#
# TRIGGER DESIGN (own it):
#
# `assets_compute_search_vector(asset_id, name, serial)` is the single source of
# truth for one asset's tsvector. It reads that asset's own tag names
# (assets_tag_link -> catalog_tag) and custom values (assets_asset_field_value)
# by asset_id, so a row's vector reflects data living in related tables.
#
#   Weights: name = A, serial_number + tags = B, custom field values = C.
#   Text search config = 'english' (see the T1.4 flag in the migration report:
#   the list endpoint MUST query with the SAME config, e.g.
#   `websearch_to_tsquery('english', :q)`, or the GIN index won't match).
#
# INVOKER vs DEFINER — decided SECURITY INVOKER, deliberately:
#   * At runtime the trigger fires on the non-superuser `cortex_app` connection
#     with RLS active and `app.current_tenant` set by the request. As INVOKER,
#     every cross-table read inside the function is itself constrained by that
#     same GUC. An asset and all its children share one tenant, and the
#     triggering statement can only touch current-tenant rows (RLS USING on
#     assets_asset), so the function always sees the COMPLETE child set for that
#     tenant -> a correct, non-empty vector. It is structurally impossible for
#     it to read or write another tenant's rows. Fail-CLOSED.
#   * SECURITY DEFINER (run as owner `cortex`) would BYPASS RLS: a mistaken or
#     future-edited WHERE could then aggregate another tenant's tag/field text
#     into a row's search_vector — a cross-tenant leak into search. Fail-OPEN.
#     Rejected. (During the in-migration backfill the function runs as the owner
#     and bypasses RLS, which is fine and intended — it keys strictly by
#     asset_id, so each vector is still built only from that asset's children.)
#
# Recursion safety: the asset BEFORE trigger only recomputes on INSERT or when
# name/serial actually change; the child triggers force a recompute by UPDATEing
# the parent's search_vector directly (which the BEFORE trigger then leaves
# intact because name/serial are unchanged). No trigger issues a statement that
# re-fires itself.
FUNCTIONS_FORWARD = """
CREATE OR REPLACE FUNCTION assets_compute_search_vector(
    p_asset_id bigint, p_name text, p_serial text
) RETURNS tsvector
LANGUAGE sql
STABLE
SECURITY INVOKER
AS $$
    SELECT
        setweight(to_tsvector('english', coalesce(p_name, '')), 'A')
        || setweight(to_tsvector('english', coalesce(p_serial, '')), 'B')
        || setweight(to_tsvector('english', coalesce((
                SELECT string_agg(t.name, ' ')
                FROM assets_tag_link tl
                JOIN catalog_tag t ON t.id = tl.tag_id
                WHERE tl.asset_id = p_asset_id
            ), '')), 'B')
        || setweight(to_tsvector('english', coalesce((
                SELECT string_agg(
                        CASE
                            WHEN afv.value IS NULL
                                 OR jsonb_typeof(afv.value) = 'null' THEN NULL
                            WHEN jsonb_typeof(afv.value)
                                 IN ('string', 'number', 'boolean')
                                THEN afv.value #>> '{}'
                            ELSE afv.value::text
                        END, ' ')
                FROM assets_asset_field_value afv
                WHERE afv.asset_id = p_asset_id
            ), '')), 'C');
$$;

CREATE OR REPLACE FUNCTION assets_asset_search_vector_trigger()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
BEGIN
    IF (TG_OP = 'INSERT')
       OR NEW.name IS DISTINCT FROM OLD.name
       OR NEW.serial_number IS DISTINCT FROM OLD.serial_number THEN
        NEW.search_vector := assets_compute_search_vector(
            NEW.id, NEW.name, NEW.serial_number);
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION assets_child_touch_search_vector()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
DECLARE
    v_new_asset bigint := NULL;
    v_old_asset bigint := NULL;
BEGIN
    IF (TG_OP <> 'DELETE') THEN
        v_new_asset := NEW.asset_id;
    END IF;
    IF (TG_OP <> 'INSERT') THEN
        v_old_asset := OLD.asset_id;
    END IF;

    IF v_new_asset IS NOT NULL THEN
        UPDATE assets_asset a
        SET search_vector =
            assets_compute_search_vector(a.id, a.name, a.serial_number)
        WHERE a.id = v_new_asset;
    END IF;
    IF v_old_asset IS NOT NULL AND v_old_asset IS DISTINCT FROM v_new_asset THEN
        UPDATE assets_asset a
        SET search_vector =
            assets_compute_search_vector(a.id, a.name, a.serial_number)
        WHERE a.id = v_old_asset;
    END IF;

    IF (TG_OP = 'DELETE') THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS assets_asset_search_vector_biu ON assets_asset;
CREATE TRIGGER assets_asset_search_vector_biu
    BEFORE INSERT OR UPDATE ON assets_asset
    FOR EACH ROW EXECUTE FUNCTION assets_asset_search_vector_trigger();

DROP TRIGGER IF EXISTS assets_afv_search_vector_aiud ON assets_asset_field_value;
CREATE TRIGGER assets_afv_search_vector_aiud
    AFTER INSERT OR UPDATE OR DELETE ON assets_asset_field_value
    FOR EACH ROW EXECUTE FUNCTION assets_child_touch_search_vector();

DROP TRIGGER IF EXISTS assets_tag_link_search_vector_aid ON assets_tag_link;
CREATE TRIGGER assets_tag_link_search_vector_aid
    AFTER INSERT OR DELETE ON assets_tag_link
    FOR EACH ROW EXECUTE FUNCTION assets_child_touch_search_vector();

-- Tag RENAME must re-vector every asset carrying that tag (tag names live in
-- search_vector at weight B). Without this, `UPDATE catalog_tag SET name=...`
-- leaves every linked asset with the stale term until some unrelated change
-- re-vectors it. Set-based (one UPDATE over all linked assets), reusing the
-- same recompute function. SECURITY INVOKER: same fail-closed RLS reasoning as
-- the other triggers — a tag, its links, and its assets share one tenant, and
-- at runtime the GUC constrains the set to the current tenant, so it can never
-- re-vector another tenant's assets. This trigger is ON catalog_tag but its
-- body targets assets_asset/assets_tag_link and the assets_compute_search_vector
-- function (all owned by THIS migration), so it is created here (not in catalog
-- 0002) to keep migration dependencies one-directional (assets depends on
-- catalog, never the reverse) and the down-path clean.
CREATE OR REPLACE FUNCTION assets_tag_rename_search_vector()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
BEGIN
    UPDATE assets_asset a
    SET search_vector =
        assets_compute_search_vector(a.id, a.name, a.serial_number)
    WHERE a.id IN (
        SELECT tl.asset_id FROM assets_tag_link tl WHERE tl.tag_id = NEW.id
    );
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS catalog_tag_search_vector_au ON catalog_tag;
CREATE TRIGGER catalog_tag_search_vector_au
    AFTER UPDATE OF name ON catalog_tag
    FOR EACH ROW
    WHEN (NEW.name IS DISTINCT FROM OLD.name)
    EXECUTE FUNCTION assets_tag_rename_search_vector();

-- Backfill existing rows (owner role -> bypasses RLS; keyed strictly by
-- asset_id so each vector is built only from that asset's own children).
UPDATE assets_asset a
SET search_vector =
    assets_compute_search_vector(a.id, a.name, a.serial_number);
"""

FUNCTIONS_REVERSE = """
UPDATE assets_asset SET search_vector = NULL;
DROP TRIGGER IF EXISTS catalog_tag_search_vector_au ON catalog_tag;
DROP TRIGGER IF EXISTS assets_tag_link_search_vector_aid ON assets_tag_link;
DROP TRIGGER IF EXISTS assets_afv_search_vector_aiud ON assets_asset_field_value;
DROP TRIGGER IF EXISTS assets_asset_search_vector_biu ON assets_asset;
DROP FUNCTION IF EXISTS assets_tag_rename_search_vector();
DROP FUNCTION IF EXISTS assets_child_touch_search_vector();
DROP FUNCTION IF EXISTS assets_asset_search_vector_trigger();
DROP FUNCTION IF EXISTS assets_compute_search_vector(bigint, text, text);
"""


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0001_initial"),
        # The tag-rename trigger is created ON catalog_tag (body targets the
        # assets tables + assets_compute_search_vector). Explicit so the table
        # is guaranteed present on apply and so our own reverse SQL runs before
        # catalog_tag is ever dropped on a down-to-zero.
        ("catalog", "0001_initial"),
    ]

    operations = [
        # 1. RLS on every new tenant-owned table.
        *[
            migrations.RunSQL(
                sql=enable_rls_sql(table),
                reverse_sql=disable_rls_sql(table),
            )
            for table in ASSET_TENANT_TABLES
        ],
        # 2. Specialized GIN / trigram / JSONB indexes (data-model.md §3).
        migrations.RunSQL(sql=INDEXES_FORWARD, reverse_sql=INDEXES_REVERSE),
        # 3. search_vector function + triggers + backfill.
        migrations.RunSQL(sql=FUNCTIONS_FORWARD, reverse_sql=FUNCTIONS_REVERSE),
    ]
