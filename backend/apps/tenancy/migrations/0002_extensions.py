"""Enable the Postgres extensions the platform relies on (T0.5).

- ``btree_gist`` — required by the GiST **exclusion constraint** that prevents
  overlapping active reservations per asset (F4, added in M2). Enabled now so
  the extension is present cluster-wide before any table needs it.
- ``pg_trgm`` — trigram GIN indexes for fuzzy ``name``/``serial_number`` search
  (F10, added in M1). Enabled now for the same reason.

Both are created ``IF NOT EXISTS`` so the migration is idempotent-safe to
re-run in CI, and dropped on reverse. ``CREATE EXTENSION`` runs as the
migration/owner role (superuser in the docker image), which is the only role
permitted to install extensions.
"""
from __future__ import annotations

from django.contrib.postgres.operations import BtreeGistExtension, TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("tenancy", "0001_initial"),
    ]

    operations = [
        # Django's first-party extension operations already emit
        # `CREATE EXTENSION IF NOT EXISTS` forward and `DROP EXTENSION` reverse.
        BtreeGistExtension(),
        TrigramExtension(),
    ]
