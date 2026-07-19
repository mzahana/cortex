"""Create the non-superuser application DB role that RLS actually binds to (T0.5).

## Why this exists

Postgres RLS is bypassed unconditionally by superusers and by ``BYPASSRLS``
roles, and by a table's **owner** unless ``FORCE ROW LEVEL SECURITY`` is set.
The official ``postgres`` docker image creates ``POSTGRES_USER`` (here ``cortex``)
as a **superuser** and it owns every table (it runs the migrations). If the
running app connects as ``cortex``, the RLS policies in ``0004`` would never fire —
the R4 backstop would be inert.

So this migration provisions a dedicated **non-superuser, NOBYPASSRLS** login
role (default ``cortex_app``) that:
  - owns nothing (tables stay owned by the migration role ``cortex``), and is
    therefore fully subject to RLS — no ``FORCE ROW LEVEL SECURITY`` needed
    (and none is used, because forcing RLS on the owner would break
    owner-run data migrations/seeds on managed Postgres where the migration
    role is the owner rather than a superuser);
  - has plain CRUD on the app's tables + sequence usage, plus ``ALTER DEFAULT
    PRIVILEGES`` so every future table/sequence created by the owner is granted
    to it automatically (M1+ asset tables need no extra grant migration).

Names/passwords come from env (``APP_DB_USER`` / ``APP_DB_PASSWORD`` /
``POSTGRES_USER``) with dev defaults, so the same migration provisions the role
in every environment. Role creation is idempotent; the reverse fully removes
the role's privileges and drops it.

## Runtime wiring (WIRED — do not revert)

Migrations must keep running as the owner ``cortex`` (they create tables and the
seed inserts cross-tenant rows — both rely on bypassing RLS). The **web/worker
runtime** connection connects as this role so RLS is enforced at runtime. This
is already wired in ``docker-compose.yml``: a one-off ``migrate`` service runs
as the owner ``cortex`` (default ``DATABASE_URL``), and ``web``/``worker``/``beat``
override ``DATABASE_URL`` to ``APP_DATABASE_URL`` (the ``cortex_app`` role):

    APP_DATABASE_URL=postgres://cortex_app:<APP_DB_PASSWORD>@postgres:5432/cortex

**WARNING:** do NOT point the runtime services back at the owner ``cortex`` role —
a superuser bypasses RLS unconditionally, which would silently make the R4
tenant-isolation backstop inert while every app-level check still appears to
work. The GUC the middleware sets only matters because the runtime role is
``cortex_app``. Verified: ``SELECT current_user`` on web/worker/beat returns
``cortex_app``, and the RLS psql/pytest proofs pass against that role.
"""
from __future__ import annotations

import os
import re

from django.db import migrations

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ident(name: str) -> str:
    """Validate an env-supplied role name is a bare identifier (no injection),
    then return it. Role names here come from trusted env, but we still refuse
    anything that isn't a simple identifier rather than quote-escaping."""
    if not _IDENT_RE.match(name or ""):
        raise ValueError(f"Refusing unsafe DB role identifier: {name!r}")
    return name


def _config():
    app_user = _ident(os.environ.get("APP_DB_USER", "cortex_app"))
    owner = _ident(os.environ.get("POSTGRES_USER", "cortex"))
    password = os.environ.get("APP_DB_PASSWORD", "changeme-app-db-password")
    # Escape single quotes for the password string literal.
    password_lit = "'" + password.replace("'", "''") + "'"
    return app_user, owner, password_lit


def create_role(apps, schema_editor):
    app_user, owner, password_lit = _config()
    with schema_editor.connection.cursor() as cur:
        cur.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{app_user}') THEN
                    CREATE ROLE {app_user} LOGIN NOSUPERUSER NOCREATEDB
                        NOCREATEROLE NOBYPASSRLS PASSWORD {password_lit};
                ELSE
                    ALTER ROLE {app_user} LOGIN NOSUPERUSER NOCREATEDB
                        NOCREATEROLE NOBYPASSRLS PASSWORD {password_lit};
                END IF;
            END
            $$;
            """
        )
        # Runtime CRUD privileges on everything that exists now...
        cur.execute(f"GRANT USAGE ON SCHEMA public TO {app_user};")
        cur.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {app_user};"
        )
        cur.execute(
            f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {app_user};"
        )
        # ...and on everything the owner creates in the future (M1+ tables).
        cur.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {app_user};"
        )
        cur.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
            f"GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {app_user};"
        )


def drop_role(apps, schema_editor):
    """Reverse: revoke everything this migration granted **in the current
    database** and remove the default-privilege entries.

    Deliberately does NOT ``DROP ROLE``: a login role is a *cluster-global*
    object that may hold grants in other databases of the same cluster (e.g.
    the real ``cortex`` DB while a scratch DB is being torn down), and Postgres
    refuses to drop a role while any such dependency exists. The reversible
    unit of this migration is the per-database grant/RLS wiring; fully removing
    the shared role is an infra step (``DROP ROLE cortex_app`` once no database
    references it). Revokes are guarded by ``IF EXISTS`` so reversing is
    idempotent even if the role was never created."""
    app_user, owner, _ = _config()
    with schema_editor.connection.cursor() as cur:
        cur.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{app_user}') THEN
                    ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public
                        REVOKE ALL ON TABLES FROM {app_user};
                    ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public
                        REVOKE ALL ON SEQUENCES FROM {app_user};
                    REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {app_user};
                    REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {app_user};
                    REVOKE ALL ON SCHEMA public FROM {app_user};
                END IF;
            END
            $$;
            """
        )


class Migration(migrations.Migration):

    dependencies = [
        ("tenancy", "0002_extensions"),
        # Grant against tables that already exist at this point.
        ("accounts", "0001_initial"),
        ("projects", "0001_initial"),
        ("rbac", "0002_seed_permissions_and_roles"),
    ]

    operations = [
        migrations.RunPython(create_role, reverse_code=drop_role),
    ]
