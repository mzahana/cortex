"""Add the per-tenant ``SessionSettings`` config table + its RLS backstop.

``SessionSettings`` is a new ``TenantScopedModel`` (per-tenant idle/absolute
session timeouts), so it needs the exact same RLS tenant-isolation policy as
every other tenant-owned table (the R4 backstop). RLS is added here via the
shared ``apps.tenancy.db.enable_rls_sql``/``disable_rls_sql`` helpers so the
policy predicate is byte-identical to every other tenant table — the app-level
``TenantScopedManager`` filter and the DB policy cannot drift. Fail-closed: no
``app.current_tenant`` GUC -> ``tenant_id = NULL`` -> zero rows.

No extra indexes are owed: exactly one row per tenant (the
``tenancy_session_settings_one_per_tenant`` unique constraint on ``tenant``)
and the ``tenant`` FK's own ``db_index`` cover the only access path (the
middleware's per-tenant lookup). No exclusion constraint / trigger / search
vector — this is a plain config row, not a domain entity.
"""
from __future__ import annotations

import apps.tenancy.managers
import django.core.validators
import django.db.models.deletion
from django.db import migrations, models

from apps.tenancy.db import disable_rls_sql, enable_rls_sql


class Migration(migrations.Migration):

    dependencies = [
        ('tenancy', '0004_rls_policies'),
    ]

    operations = [
        migrations.CreateModel(
            name='SessionSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('idle_timeout_minutes', models.PositiveIntegerField(default=60, validators=[django.core.validators.MinValueValidator(5), django.core.validators.MaxValueValidator(480)])),
                ('absolute_timeout_hours', models.PositiveIntegerField(default=24, validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(720)])),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='tenancy.tenant')),
            ],
            options={
                'db_table': 'tenancy_session_settings',
            },
            managers=[
                ('objects', apps.tenancy.managers.TenantScopedManager()),
            ],
        ),
        migrations.AddConstraint(
            model_name='sessionsettings',
            constraint=models.UniqueConstraint(fields=('tenant',), name='tenancy_session_settings_one_per_tenant'),
        ),
        # RLS backstop (R4) — enable RLS + tenant-isolation policy, identical
        # predicate to every other tenant table. Reverse drops the policy and
        # disables RLS.
        migrations.RunSQL(
            sql=enable_rls_sql('tenancy_session_settings'),
            reverse_sql=disable_rls_sql('tenancy_session_settings'),
        ),
    ]
