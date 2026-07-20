"""T5.4 — DB-level append-only enforcement on ``audit_audit_log``.

``apps.audit.models.AuditLog``'s own docstring and T1.2/T1.3 both flag this
as M5's job: "Enforcing 'no UPDATE/DELETE' at the DB level (trigger) is M5's
job; the app layer already never exposes an update/delete path for this
model." This migration closes that gap, mirroring T2.3's
``stock_stock_txn`` append-only trigger
(``apps/stock/migrations/0002_rls_indexes_ledger_integrity.py``,
``stock_txn_forbid_mutation`` / ``stock_txn_immutable_bud``) exactly:

* ``audit_log_forbid_mutation()`` — a ``SECURITY INVOKER`` trigger function
  that unconditionally ``RAISE EXCEPTION``s on any ``UPDATE`` or ``DELETE``,
  using ``ERRCODE = 'restrict_violation'`` (same errcode as the stock
  trigger, so callers can catch it uniformly).
* ``audit_log_immutable_bud`` — ``BEFORE UPDATE OR DELETE ON
  audit_audit_log FOR EACH ROW`` — fires before the write is applied, so
  the row is never touched. ``INSERT`` is untouched (not in the trigger's
  event list), so writing new audit entries is unaffected.

This is a pure DB-level backstop behind the existing app-layer guard (no
update/delete view or serializer is ever exposed for ``AuditLog`` — see
``apps.audit.services.write_audit_log``, the only sanctioned write path).
A raw ``UPDATE``/``DELETE`` issued directly against Postgres — bypassing
the ORM and the app entirely — is rejected exactly as it is for
``stock_stock_txn``.

``CREATE OR REPLACE FUNCTION`` / ``DROP TRIGGER IF EXISTS`` / ``DROP
FUNCTION IF EXISTS`` keep both directions idempotent-safe to re-run in CI.
"""
from __future__ import annotations

from django.db import migrations

TRIGGER_FORWARD = """
CREATE OR REPLACE FUNCTION audit_log_forbid_mutation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
BEGIN
    RAISE EXCEPTION
        'audit_audit_log is append-only: % is not permitted. Write a new '
        'entry instead (docs/data-model.md §2).',
        TG_OP
        USING ERRCODE = 'restrict_violation';
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS audit_log_immutable_bud ON audit_audit_log;
CREATE TRIGGER audit_log_immutable_bud
    BEFORE UPDATE OR DELETE ON audit_audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_forbid_mutation();
"""

TRIGGER_REVERSE = """
DROP TRIGGER IF EXISTS audit_log_immutable_bud ON audit_audit_log;
DROP FUNCTION IF EXISTS audit_log_forbid_mutation();
"""


class Migration(migrations.Migration):

    dependencies = [
        ("audit", "0002_rls_policies"),
    ]

    operations = [
        migrations.RunSQL(sql=TRIGGER_FORWARD, reverse_sql=TRIGGER_REVERSE),
    ]
