"""M8 Phase 2 — the money layer: `Payment` (a bank-statement line) and
`Purchase` (a vendor receipt), plus their attachment tables.

See `apps.finance.models` for why these live in their own app and why every
reconciliation figure is derived rather than stored.

**RLS here is tenant-only, with no project predicate — deliberately, and it is
the one place in this codebase where the DB policy is broader than the
application's own access rule.** A payment has no single project (that is the
entire point of the table), so there is no project column for a policy to
constrain. The project-level restriction — a lead sees only charges touching a
project they lead, with other projects' lines collapsed to one unnamed total —
therefore lives in `apps.finance.permissions` + the viewset queryset, and is
covered by explicit tests rather than by the DB. Tenant isolation itself is
still fully enforced here, so the R4 blast radius is unchanged; what the DB
cannot express is the intra-tenant, cross-project redaction.

RLS is created in the same migration as the tables (not a follow-up one), for
the reason given in `apps.assets.migrations.0005_m8_asset_project_usage`.
"""


import apps.tenancy.managers
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from apps.tenancy.db import disable_rls_sql, enable_rls_sql

FINANCE_TENANT_TABLES = [
    "finance_payment",
    "finance_purchase",
    "finance_payment_attachment",
    "finance_purchase_attachment",
]


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('tenancy', '0007_tenant_branding'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Payment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('amount', models.DecimalField(decimal_places=2, help_text='What the bank actually took, in the settlement currency.', max_digits=14)),
                ('currency', models.CharField(help_text="Settlement currency, e.g. 'SAR'. What the statement shows.", max_length=3)),
                ('paid_on', models.DateField()),
                ('method', models.CharField(choices=[('card', 'Card'), ('bank_transfer', 'Bank transfer'), ('cash', 'Cash'), ('other', 'Other')], default='card', max_length=16)),
                ('account_label', models.CharField(blank=True, default='', help_text="Human label for the account/card, e.g. 'Visa •4321'. NEVER a full card number.", max_length=64)),
                ('statement_ref', models.CharField(blank=True, default='', help_text="The bank's own reference for this line — the auditor's join key.", max_length=128)),
                ('vendor', models.CharField(blank=True, default='', max_length=255)),
                ('notes', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_payments', to=settings.AUTH_USER_MODEL)),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='tenancy.tenant')),
            ],
            options={
                'db_table': 'finance_payment',
                'ordering': ['-paid_on', '-id'],
            },
            managers=[
                ('objects', apps.tenancy.managers.TenantScopedManager()),
            ],
        ),
        migrations.CreateModel(
            name='PaymentAttachment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('storage_key', models.CharField(max_length=500)),
                ('filename', models.CharField(max_length=255)),
                ('content_type', models.CharField(blank=True, default='', max_length=127)),
                ('size', models.PositiveBigIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('payment', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='attachments', to='finance.payment')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='tenancy.tenant')),
                ('uploaded_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='uploaded_payment_attachments', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'finance_payment_attachment',
                'ordering': ['-created_at'],
            },
            managers=[
                ('objects', apps.tenancy.managers.TenantScopedManager()),
            ],
        ),
        migrations.CreateModel(
            name='Purchase',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('vendor', models.CharField(blank=True, default='', max_length=255)),
                ('vendor_order_number', models.CharField(blank=True, default='', help_text="The vendor's order id. Deliberately NOT unique — split shipments of one order all share it.", max_length=128)),
                ('receipt_number', models.CharField(blank=True, default='', max_length=128)),
                ('date', models.DateField()),
                ('currency', models.CharField(help_text="Transaction currency — what the receipt is priced in, e.g. 'USD'.", max_length=3)),
                ('subtotal', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('shipping', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('tax', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('total', models.DecimalField(decimal_places=2, help_text="The receipt's own grand total, in its own currency.", max_digits=14)),
                ('settled_amount', models.DecimalField(blank=True, decimal_places=2, help_text="This receipt's cost in the PAYMENT's currency. NULL = derive it by sharing the payment across its receipts (see the model docstring).", max_digits=14, null=True)),
                ('notes', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_purchases', to=settings.AUTH_USER_MODEL)),
                ('payment', models.ForeignKey(blank=True, help_text='NULL = not yet reconciled against a bank charge.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='purchases', to='finance.payment')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='tenancy.tenant')),
            ],
            options={
                'db_table': 'finance_purchase',
                'ordering': ['date', 'id'],
            },
            managers=[
                ('objects', apps.tenancy.managers.TenantScopedManager()),
            ],
        ),
        migrations.CreateModel(
            name='PurchaseAttachment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('storage_key', models.CharField(max_length=500)),
                ('filename', models.CharField(max_length=255)),
                ('content_type', models.CharField(blank=True, default='', max_length=127)),
                ('size', models.PositiveBigIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('purchase', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='attachments', to='finance.purchase')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='tenancy.tenant')),
                ('uploaded_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='uploaded_purchase_attachments', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'finance_purchase_attachment',
                'ordering': ['-created_at'],
            },
            managers=[
                ('objects', apps.tenancy.managers.TenantScopedManager()),
            ],
        ),
        migrations.AddIndex(
            model_name='payment',
            index=models.Index(fields=['tenant', 'paid_on'], name='finance_pay_tenant__888301_idx'),
        ),
        migrations.AddIndex(
            model_name='payment',
            index=models.Index(fields=['tenant', 'vendor'], name='finance_pay_tenant__138971_idx'),
        ),
        migrations.AddIndex(
            model_name='paymentattachment',
            index=models.Index(fields=['tenant', 'payment'], name='finance_pay_tenant__c32ace_idx'),
        ),
        migrations.AddIndex(
            model_name='purchase',
            index=models.Index(fields=['tenant', 'payment'], name='finance_pur_tenant__e8d3b9_idx'),
        ),
        migrations.AddIndex(
            model_name='purchase',
            index=models.Index(fields=['tenant', 'date'], name='finance_pur_tenant__7cd377_idx'),
        ),
        migrations.AddIndex(
            model_name='purchase',
            index=models.Index(fields=['tenant', 'vendor'], name='finance_pur_tenant__6424db_idx'),
        ),
        migrations.AddIndex(
            model_name='purchaseattachment',
            index=models.Index(fields=['tenant', 'purchase'], name='finance_pur_tenant__d0bf88_idx'),
        ),
        *[
            migrations.RunSQL(
                sql=enable_rls_sql(table),
                reverse_sql=disable_rls_sql(table),
            )
            for table in FINANCE_TENANT_TABLES
        ],
    ]
