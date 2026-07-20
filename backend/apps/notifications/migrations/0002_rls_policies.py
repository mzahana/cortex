"""T5.4 — Row-Level Security on the two T5.1 notifications tables
(``notifications_email_log``, ``notifications_notification_pref``) — the R4
backstop.

Both tables were introduced additively at T5.1 (see
``apps.notifications.models`` module docstring) as ``TenantScopedModel``
subclasses, so they need the exact same RLS backstop as every other tenant
table (mirrors ``apps.audit``'s ``0002_rls_policies.py`` and
``apps.stock``'s ``0002_rls_indexes_ledger_integrity.py`` precedent). Uses
the shared ``apps.tenancy.db.enable_rls_sql``/``disable_rls_sql`` helpers so
the policy predicate is byte-identical to every other tenant table — the
app-level ``TenantScopedManager`` filter and the DB policy cannot drift.
Fail-closed: no ``app.current_tenant`` GUC -> ``tenant_id = NULL`` -> zero
rows.

No other DB-level hardening is owed by this migration: the composite
``(tenant, event_type, created_at)`` and ``(tenant, user)`` indexes already
ship in ``0001_initial``; there is no immutability requirement on either
table (email logs are updated in place by the sending task, and prefs are
mutable by design).
"""
from __future__ import annotations

from django.db import migrations

from apps.tenancy.db import disable_rls_sql, enable_rls_sql

NOTIFICATIONS_TENANT_TABLES = [
    "notifications_email_log",
    "notifications_notification_pref",
]


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0001_initial"),
    ]

    operations = [
        *[
            migrations.RunSQL(
                sql=enable_rls_sql(table),
                reverse_sql=disable_rls_sql(table),
            )
            for table in NOTIFICATIONS_TENANT_TABLES
        ],
    ]
