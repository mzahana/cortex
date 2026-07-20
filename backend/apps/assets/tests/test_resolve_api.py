"""T4.1 — Scan resolver `GET /api/v1/resolve/{qr_token}`: tenant-scoped
lookup by the stable, unguessable `qr_token` (never trusts/echoes a
client-supplied tenant); an unknown token and a token belonging to another
tenant both 404 identically (R4: no existence leak), same as guessing
another tenant's numeric asset id already 404s on `GET /assets/{id}/`
(`TestAssetTenantIsolation.test_guessed_asset_id_from_another_tenant_404s`
in `test_assets_api.py`). Perf budget (<250ms) is asserted server-side via
the in-process test client, same proxy pattern as `test_perf_10k.py`.
"""

from __future__ import annotations

import time

import pytest

from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    CategoryFactory,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.rbac.permission_keys import ROLE_ADMIN

pytestmark = pytest.mark.django_db

RESOLVE_PERF_BUDGET_SECONDS = 0.25


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


def _create_asset(client, category, name="Scan Target"):
    response = client.post(
        "/api/v1/assets/",
        data={"category": category.id, "name": name},
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    return response.json()


class TestAssetResolve:
    def test_valid_token_resolves_to_its_asset(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant)
        _login(client, tenant, admin)

        asset = _create_asset(client, category, name="Jetson Orin #7")
        qr_token = asset["qr_token"]
        assert qr_token

        start = time.perf_counter()
        response = client.get(f"/api/v1/resolve/{qr_token}")
        elapsed = time.perf_counter() - start

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["id"] == asset["id"]
        assert body["name"] == "Jetson Orin #7"
        assert body["qr_token"] == qr_token
        assert elapsed < RESOLVE_PERF_BUDGET_SECONDS, (
            f"resolve took {elapsed * 1000:.1f}ms, budget is "
            f"{RESOLVE_PERF_BUDGET_SECONDS * 1000:.0f}ms"
        )

    def test_unknown_token_404s(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        response = client.get("/api/v1/resolve/this-token-does-not-exist-anywhere")
        assert response.status_code == 404

    def test_cross_tenant_token_404s_not_403(self, client):
        """The critical tenant-isolation regression test: authenticated as a
        tenant B user, resolving tenant A's real (existing, valid) token
        must 404 — never 403 (a 403 would leak "this token exists, you're
        just not allowed to see it"; a 404 makes "exists in another tenant"
        indistinguishable from "never existed", per CLAUDE.md's R4
        invariant)."""
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        category_a = CategoryFactory(tenant=tenant_a)

        admin_b = UserFactory(tenant=tenant_b)
        upgrade_tenant_wide_role(admin_b, ROLE_ADMIN)

        _login(client, tenant_a, admin_a)
        asset_a = _create_asset(client, category_a, name="Tenant A Secret Asset")
        qr_token_a = asset_a["qr_token"]
        client.post("/api/v1/auth/logout")

        _login(client, tenant_b, admin_b)
        response = client.get(f"/api/v1/resolve/{qr_token_a}")
        assert response.status_code == 404

    def test_unauthenticated_request_is_denied(self, client):
        """No session at all -> `AssetPermission.has_permission` denies
        before the tenant-scoped lookup even runs (same as every other
        endpoint here — auth is required, RBAC is server-side, never
        client-gated)."""
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant)
        _login(client, tenant, admin)
        asset = _create_asset(client, category, name="Needs Auth")
        client.post("/api/v1/auth/logout")

        response = client.get(f"/api/v1/resolve/{asset['qr_token']}")
        assert response.status_code in (401, 403)
