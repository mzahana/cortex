# Tenant branding: the lab's logo, shown in the app chrome after login and
# settable from the UI (`POST/DELETE /api/v1/tenancy/logo`).
#
# Four nullable/blank-defaulted columns on `tenancy_tenant`, no constraint,
# index, or data change — the bytes live on the media volume, only the storage
# key is stored (`apps.tenancy.services.save_tenant_logo`). `tenancy_tenant` is
# the ROOT table, not a tenant-owned one, so there is deliberately no RLS
# policy to add here (see `0004_rls_policies`, which covers tenant-owned tables
# only). Reversible with Django's default reverse (drop the columns).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenancy", "0006_alter_sessionsettings_absolute_timeout_hours"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="logo_storage_key",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Relative path/key on the storage backend — never the binary itself."
                ),
                max_length=500,
            ),
        ),
        migrations.AddField(
            model_name="tenant",
            name="logo_filename",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="tenant",
            name="logo_content_type",
            field=models.CharField(blank=True, default="", max_length=127),
        ),
        migrations.AddField(
            model_name="tenant",
            name="logo_updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
