"""Test settings (T0.8) — used by pytest-django (`DJANGO_SETTINGS_MODULE` is
pinned to this module in `pyproject.toml`'s `[tool.pytest.ini_options]`).

Extends `dev.py` (not `prod.py`): tests run over plain HTTP against a
throwaway Postgres, never behind Cloudflare/nginx TLS termination, so the
SSL-redirect/HSTS/secure-cookie posture that `dev.py` already relaxes is
exactly what's needed here too — this module only adds test-speed and
test-isolation tweaks on top.
"""

from .dev import *  # noqa: F401,F403
from .dev import env

ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

# Argon2 stays the AUTH_PASSWORD_HASHERS[0] in base.py (so a test that cares
# about hashing behavior still gets the real prod hasher), but per-test user
# creation via factory_boy would otherwise pay Argon2's deliberately-slow cost
# on every fixture. MD5 first here is test-only and never affects prod/dev.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Slow work (Celery) must run synchronously in tests — no broker round-trip,
# no reliance on a running worker container.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# --- Cache/session backend override (T0.9 finding) --------------------------
#
# `base.py` wires `CACHES["default"]` to a REAL Redis (`REDIS_URL`, default
# `redis://redis:6379/0`) because Redis does "triple duty" in this stack
# (cache + sessions + Celery broker, docs/architecture.md). `SESSION_ENGINE`
# is the cache-backed session store, and `ScopedRateThrottle` (the login
# endpoint's brute-force guard, T0.6) also reads/writes through this same
# cache. CI's `backend-test` job (`.github/workflows/ci.yml`) provisions
# ONLY a Postgres service container -- no Redis -- so ANY test that exercises
# a real HTTP request through `django.contrib.auth.login()`/`ScopedRateThrottle`
# (e.g. a real login -> session cookie -> `GET /me` round trip, exactly what
# T0.9's tenant-isolation integration tests need) previously failed outside a
# machine that happens to have a `redis` hostname resolvable, with a
# `redis.exceptions.ConnectionError` that has nothing to do with the
# assertion under test. No prior test module exercised the login endpoint,
# so this was latent rather than caught. Matching the `EMAIL_BACKEND` swap
# above (never Brevo in tests) and the project rule that tests must not rely
# on external services, cache/session storage is swapped to an in-process
# `LocMemCache` here, test-settings-only -- `dev.py`/`prod.py` are untouched
# and still require real Redis, exactly as `docs/architecture.md` specifies.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    },
}

# --- RLS-vs-test-role note (T0.8, read before writing T0.9's cross-tenant test) --
#
# There are TWO independent ways an RLS test can false-pass here. A test that
# avoids only one of them is still worthless. See `backend/conftest.py`'s
# module docstring for the full writeup with the canonical fix; summary:
#
# TRAP 1 — owner-role bypass. `DATABASE_URL` here (from CI env, see
# .github/workflows/ci.yml) points at the migration/OWNER role (matching
# docker-compose's `migrate` one-off), because pytest-django needs owner
# privileges to CREATE the scratch test database and run migrations against
# it (including `tenancy.0003_app_db_role`, which itself needs
# CREATEROLE-equivalent rights). That means every ordinary
# `@pytest.mark.django_db`/`transactional_db` test — including anything using
# `TenantScopedManager`/`apps.tenancy.context.tenant_context()` — runs its ORM
# queries AS THE OWNER, which bypasses RLS by table ownership regardless of
# any GUC. This is expected and does not weaken those tests: the app-level
# tenant filter (`TenantScopedManager`, T0.4) is still exercised and
# asserted; RLS is deliberately a *second*, independent backstop that must be
# proven through a DIFFERENT connection (see TRAP 2 for how NOT to do that).
#
# TRAP 2 — transaction-isolation false-pass. Proving RLS means opening a
# SECOND connection authenticated as the non-superuser `cortex_app` role (which
# IS subject to RLS) and querying through it instead. But if the row under
# test was only written inside a transaction that gets ROLLED BACK at the end
# of the test (which is exactly what the plain `db` fixture does to every
# test), that second connection can never see it under READ COMMITTED — not
# because RLS hid it, but because it was never committed. A test built
# "`db` fixture creates a row -> query it from the other connection -> assert
# zero rows" PASSES even with RLS fully disabled. This bit an earlier draft
# of this fixture and is just as fatal as Trap 1.
#
# THE FIX: a same-committed-row, GUC-driven visibility DIFFERENTIAL, done
# entirely through the `cortex_app` connection: commit a real row (via
# `transactional_db`, never the rolled-back `db` fixture), then on the
# `cortex_app` connection show the SAME row is visible with the GUC set to its
# own tenant and invisible with the GUC set to a different tenant (or
# unset). Only a visibility change caused SOLELY by flipping the GUC isolates
# RLS as the cause.
#
# `backend/conftest.py::app_role_connection` (built on `transactional_db`) is
# that second connection; `apps/common/tests/test_smoke.py::
# test_rls_visibility_differential_on_committed_data` is the canonical,
# copy-able example T0.9 should model its cross-tenant RLS test on.
