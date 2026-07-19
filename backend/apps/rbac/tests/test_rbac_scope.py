"""T0.9 — RBAC server-side enforcement + ProjectLead scope (F1 acceptance).

M0 has no admin/object CRUD endpoints yet (M1+) to hang a "guessed URL" test
on directly, so this proves the enforcement PRIMITIVE those future endpoints
will all share:

- `apps.rbac.permissions.HasPermission` (the DRF permission class every M1+
  endpoint is required to use) — exercised through DRF's real
  `dispatch()`/`check_permissions()` cycle via `APIRequestFactory` and a tiny
  test-only view (not registered in `config/urls.py`; exists purely to prove
  the permission class denies/allows through the SAME code path a real
  endpoint would use, not just a bare function call), so this is proof of
  server-side enforcement, not a UI-gating stand-in.
- `apps.rbac.services.get_effective_permissions` (the resolver every
  permission check — including `HasPermission` — is built on).

Follow-up flagged for M1: once real object endpoints exist, add an
integration-level test hitting an actual `/api/v1/...` URL with a Member
session and asserting the literal HTTP 403 body.
"""

from __future__ import annotations

import pytest
from django.db import transaction
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView

from apps.common.tests.factories import (
    ProjectFactory,
    TenantFactory,
    UserFactory,
    add_project_membership,
    upgrade_tenant_wide_role,
)
from apps.rbac.permission_keys import (
    ASSET_EDIT,
    ROLE_ADMIN,
    ROLE_PROJECT_LEAD,
    TENANT_MANAGE,
)
from apps.rbac.permissions import HasPermission
from apps.rbac.services import get_effective_permissions, user_has_permission
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db

# `apps.rbac.services.get_effective_permissions` queries `Membership.objects`
# — the fail-closed `TenantScopedManager` (T0.4) — so it requires an active
# `tenant_context()` exactly like every other tenant-owned query. On a REAL
# HTTP request, `apps.tenancy.middleware.CurrentTenantMiddleware` sets that
# context (from `request.user.tenant_id`) before the view/permission class
# ever runs. `APIRequestFactory` + calling a view directly deliberately
# bypasses the ENTIRE middleware stack (no `SessionMiddleware`,
# `AuthenticationMiddleware`, or `CurrentTenantMiddleware`), so tests below
# open `tenant_context(user.tenant_id)` explicitly around each permission
# check to reproduce exactly what that middleware would already have done —
# the same explicit-context pattern `tenant_context()`'s own docstring
# prescribes for "tests/scripts that need to run outside a request".


def _make_probe_view(permission_key: str):
    """A minimal, test-only `APIView` gated by `HasPermission(permission_key)`
    — exists only to drive DRF's real `check_permissions()` -> `PermissionDenied`
    -> 403 response cycle for a given user, exactly what a real M1 admin
    endpoint will do. Never registered in `config/urls.py`; built fresh per
    test via `as_view()`, same as DRF's own test patterns.
    """

    class _ProbeView(APIView):
        # `HasPermission(...)` is a deliberately instantiated instance, not a
        # class (see `apps.rbac.permissions.HasPermission`'s docstring/
        # `__call__`) -- DRF's own `permission_classes` type stub only
        # models the "list of classes" usage, so every M1+ endpoint using
        # this exact, documented pattern will need the same ignore.
        permission_classes = [HasPermission(permission_key)]  # type: ignore[list-item]

        def get(self, request):
            return Response({"ok": True})

    return _ProbeView


class TestMemberDeniedAdminEndpoint:
    """F1: 'a Member cannot hit an admin endpoint (403 server-side even if
    URL is guessed)'."""

    def test_member_gets_server_side_403_on_tenant_manage(self):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)  # default membership is Member, tenant-wide

        view = _make_probe_view(TENANT_MANAGE)
        factory = APIRequestFactory()
        request = factory.get("/probe/tenant-manage")
        force_authenticate(request, user=member)

        # DRF's default `exception_handler` calls `set_rollback()` on a
        # `PermissionDenied` (matching real production behavior under
        # `ATOMIC_REQUESTS=True`, see `config/settings/base.py`) -- normally
        # harmless because Django's `BaseHandler` wraps the WHOLE
        # request/view call in its own `transaction.atomic()` for exactly
        # this purpose. Calling `view.as_view()(request)` directly (required
        # to bypass the tenant middleware and drive tenant_context ourselves)
        # skips that wrapper, so an unguarded call here would poison
        # pytest-django's own outer test transaction instead. Wrapping in an
        # explicit nested `atomic()` reproduces exactly what `BaseHandler`
        # does for a real request, containing the rollback to this block.
        with tenant_context(tenant.id), transaction.atomic():
            response = view.as_view()(request)

        assert response.status_code == 403, (
            f"Member must get a server-side 403 on {TENANT_MANAGE!r} "
            f"(admin-only, docs/rbac.md matrix), got {response.status_code}."
        )

    def test_admin_is_allowed_on_the_same_endpoint(self):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)

        view = _make_probe_view(TENANT_MANAGE)
        factory = APIRequestFactory()
        request = factory.get("/probe/tenant-manage")
        force_authenticate(request, user=admin)

        with tenant_context(tenant.id), transaction.atomic():
            response = view.as_view()(request)

        assert response.status_code == 200
        assert response.data == {"ok": True}

    def test_denial_is_not_merely_ui_gating(self):
        """Prove the check is genuinely server-side: call the permission
        class's `has_permission` directly with no view/UI involved at all,
        confirm it returns `False` for a Member and `True` for an Admin —
        the same object DRF's `check_permissions()` calls internally."""
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)

        perm = HasPermission(TENANT_MANAGE)
        factory = APIRequestFactory()
        member_request = factory.get("/probe/tenant-manage")
        force_authenticate(member_request, user=member)
        admin_request = factory.get("/probe/tenant-manage")
        force_authenticate(admin_request, user=admin)

        # DRF wraps the underlying HttpRequest; `.user` is what the
        # permission class reads.
        member_request.user = member
        admin_request.user = admin

        with tenant_context(tenant.id):
            assert perm.has_permission(member_request, view=None) is False
            assert perm.has_permission(admin_request, view=None) is True

    def test_anonymous_request_is_denied_not_500(self):
        """No authenticated user at all -> denied, never a crash (guards the
        `HasPermission.has_permission` early-return branch)."""
        from django.contrib.auth.models import AnonymousUser

        perm = HasPermission(TENANT_MANAGE)
        factory = APIRequestFactory()
        request = factory.get("/probe/tenant-manage")
        request.user = AnonymousUser()

        assert perm.has_permission(request, view=None) is False


class TestProjectLeadScope:
    """F1: 'a ProjectLead can approve only their project's requests' — proven
    here at the resolver + permission-class level via `asset.edit`
    (🟡-scoped for Project Lead in docs/rbac.md's matrix): allowed on their
    own project, denied on another project, denied on the tenant-wide/
    general pool (project=None) absent an ADDITIONAL tenant-wide grant.
    """

    def test_project_lead_allowed_on_own_project_only(self):
        tenant = TenantFactory()
        own_project = ProjectFactory(tenant=tenant)
        other_project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, own_project, ROLE_PROJECT_LEAD)

        with tenant_context(tenant.id):
            assert user_has_permission(lead, ASSET_EDIT, project=own_project) is True
            assert user_has_permission(lead, ASSET_EDIT, project=other_project) is False

    def test_project_lead_scope_does_not_extend_to_general_pool(self):
        """docs/rbac.md §1: 'General-pool assets ... are governed by
        tenant-wide permissions only -- a ProjectLead's power does NOT
        extend to the shared pool unless they also hold a tenant-wide role.'
        The user's OTHER (default, auto-created) membership is plain
        tenant-wide Member, which does not grant `asset.edit` either -- so
        the general pool must deny.
        """
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)

        with tenant_context(tenant.id):
            assert user_has_permission(lead, ASSET_EDIT, project=None) is False

    def test_project_lead_with_additional_tenant_wide_role_reaches_general_pool(self):
        """The escape hatch docs/rbac.md itself calls out: if the SAME user
        also holds a tenant-wide membership that grants the permission, the
        general pool opens up -- proving the denial above is really about
        membership scope, not a blanket ProjectLead restriction."""
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        upgrade_tenant_wide_role(lead, ROLE_ADMIN)  # tenant-wide grant added

        with tenant_context(tenant.id):
            assert user_has_permission(lead, ASSET_EDIT, project=None) is True

    def test_member_role_never_gets_asset_edit_anywhere(self):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        member = UserFactory(tenant=tenant)  # default: tenant-wide Member

        with tenant_context(tenant.id):
            assert user_has_permission(member, ASSET_EDIT, project=project) is False
            assert user_has_permission(member, ASSET_EDIT, project=None) is False

    def test_has_permission_class_object_check_enforces_project_scope(self):
        """Same scope rule, exercised through `HasPermission.has_object_permission`
        (what a real M1 `retrieve`/`update` view calls) rather than the
        resolver directly -- proves the DRF-facing class, not just the
        function underneath it, obeys the same boundary."""
        tenant = TenantFactory()
        own_project = ProjectFactory(tenant=tenant)
        other_project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, own_project, ROLE_PROJECT_LEAD)

        class _FakeAsset:
            def __init__(self, project):
                self.project = project

        perm = HasPermission(ASSET_EDIT)
        factory = APIRequestFactory()
        request = factory.get("/probe/asset-edit")
        force_authenticate(request, user=lead)
        request.user = lead

        with tenant_context(tenant.id):
            assert (
                perm.has_object_permission(request, view=None, obj=_FakeAsset(own_project)) is True
            )
            assert (
                perm.has_object_permission(request, view=None, obj=_FakeAsset(other_project))
                is False
            )

    def test_project_leads_of_different_tenants_do_not_leak_across_tenants(self):
        """Belt-and-suspenders combination of R4 + RBAC: two ProjectLeads in
        TWO DIFFERENT tenants (the `ProjectFactory`/`UserFactory` default),
        each leading a same-named-by-coincidence project, never grant each
        other's scope -- the resolver only ever sees `Membership` rows
        already tenant-filtered by construction (each factory call here
        makes a distinct tenant unless overridden)."""
        project_1 = ProjectFactory()  # tenant X
        project_2 = ProjectFactory()  # tenant Y (DIFFERENT by factory default)
        lead_1 = UserFactory(tenant=project_1.tenant)
        add_project_membership(lead_1, project_1, ROLE_PROJECT_LEAD)

        assert project_1.tenant_id != project_2.tenant_id
        with tenant_context(project_1.tenant_id):
            assert user_has_permission(lead_1, ASSET_EDIT, project=project_1) is True
            assert user_has_permission(lead_1, ASSET_EDIT, project=project_2) is False


class TestEffectivePermissionsResolver:
    """Lower-level pinning of `get_effective_permissions` itself (already
    covered indirectly above; these make the union-of-memberships rule
    explicit per docs/rbac.md §1)."""

    def test_union_of_tenant_wide_and_project_scoped_memberships(self):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        user = UserFactory(tenant=tenant)  # tenant-wide Member (default)
        add_project_membership(user, project, ROLE_PROJECT_LEAD)

        with tenant_context(tenant.id):
            general_pool_perms = get_effective_permissions(user, project=None)
            project_perms = get_effective_permissions(user, project=project)

        # Tenant-wide Member grants are present in BOTH scopes...
        assert "asset.view" in general_pool_perms
        assert "asset.view" in project_perms
        # ...but the project-scoped ProjectLead grant (asset.edit) only
        # reaches the project it's scoped to, never the general pool.
        assert ASSET_EDIT not in general_pool_perms
        assert ASSET_EDIT in project_perms
