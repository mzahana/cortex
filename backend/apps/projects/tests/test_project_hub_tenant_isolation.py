"""M7 acceptance test (`docs/tasks/M7-project-grants.md` exit criteria, R4):
tenant isolation across EVERY new project-hub endpoint, plus the RLS-fires
proof this milestone's Slice 2 explicitly deferred to qa-test-engineer.

Two layers, same as every other R4 acceptance suite in this codebase
(`apps.tenancy.tests.test_cortex_app_runtime_rls`,
`apps.common.tests.test_rls_canonical`):

1. **App-level**: a tenant-A caller hitting a guessed tenant-B object id on
   ANY new M7 endpoint gets 404 (never a 403-that-implies-existence, never a
   200 that leaks data) — proves `TenantScopedManager`'s per-request filter.
2. **DB-level (the deferred check)**: with NO app.current_tenant GUC set on
   the real, non-superuser `cortex_app` role, a raw SELECT against
   `projects_expense` / `projects_expense_attachment` /
   `projects_project_document` returns ZERO rows even though the row
   unquestionably exists and is committed — proves Postgres RLS itself would
   still fail closed if the app-level filter were ever missing/buggy, not
   just that the current code happens to filter correctly today.
"""

from __future__ import annotations

import json

import pytest

from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    ExpenseCategoryFactory,
    ExpenseFactory,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.projects.models import Expense, ExpenseAttachment, ProjectDocument
from apps.rbac.permission_keys import ROLE_ADMIN
from conftest import set_app_role_tenant

pytestmark = pytest.mark.django_db


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


class TestCrossTenantAppLevel404:
    """A tenant-A Admin (highest-privilege role, so this can't be mistaken
    for an RBAC-scope 403) never gets anything but 404 for a tenant-B
    object id, across every new M7 route."""

    def _two_tenants(self):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        project_b = ProjectFactory(tenant=tenant_b, budget_total="500.00")
        expense_b = ExpenseFactory(tenant=tenant_b, project=project_b, amount="10.00")
        document_b = ProjectDocument.all_objects.create(
            tenant=tenant_b,
            project=project_b,
            kind=ProjectDocument.Kind.OTHER,
            storage_key="project-documents/x/y/z.pdf",
            filename="z.pdf",
        )
        attachment_b = ExpenseAttachment.all_objects.create(
            tenant=tenant_b,
            expense=expense_b,
            storage_key="expense-attachments/x/y/z.pdf",
            filename="z.pdf",
        )
        return tenant_a, admin_a, project_b, expense_b, document_b, attachment_b

    def test_project_detail_and_patch_404(self, client):
        tenant_a, admin_a, project_b, *_ = self._two_tenants()
        _login(client, tenant_a, admin_a)

        get_resp = client.get(f"/api/v1/projects/{project_b.id}/")
        assert get_resp.status_code == 404, get_resp.content

        patch_resp = client.patch(
            f"/api/v1/projects/{project_b.id}/",
            data=json.dumps({"budget_total": "1.00"}),
            content_type="application/json",
        )
        assert patch_resp.status_code == 404, patch_resp.content

    def test_project_assets_expenses_documents_export_sub_resources_404(self, client):
        tenant_a, admin_a, project_b, *_ = self._two_tenants()
        _login(client, tenant_a, admin_a)

        for path in (
            f"/api/v1/projects/{project_b.id}/assets/",
            f"/api/v1/projects/{project_b.id}/expenses/",
            f"/api/v1/projects/{project_b.id}/documents/",
            f"/api/v1/projects/{project_b.id}/export.csv/",
        ):
            resp = client.get(path)
            assert resp.status_code == 404, (path, resp.content)

        create_expense = client.post(
            f"/api/v1/projects/{project_b.id}/expenses/",
            data=json.dumps({"amount": "1.00", "date": "2026-01-01"}),
            content_type="application/json",
        )
        assert create_expense.status_code == 404, create_expense.content

    def test_expense_detail_update_delete_and_attachment_404(self, client):
        tenant_a, admin_a, _project_b, expense_b, _document_b, _attachment_b = self._two_tenants()
        _login(client, tenant_a, admin_a)

        assert client.get(f"/api/v1/expenses/{expense_b.id}/").status_code == 404
        assert (
            client.patch(
                f"/api/v1/expenses/{expense_b.id}/",
                data=json.dumps({"amount": "2.00"}),
                content_type="application/json",
            ).status_code
            == 404
        )
        assert client.delete(f"/api/v1/expenses/{expense_b.id}/").status_code == 404
        assert client.get(f"/api/v1/expenses/{expense_b.id}/attachment/").status_code == 404

    def test_document_delete_404(self, client):
        tenant_a, admin_a, _project_b, _expense_b, document_b, _attachment_b = self._two_tenants()
        _login(client, tenant_a, admin_a)

        assert client.delete(f"/api/v1/documents/{document_b.id}/").status_code == 404


class TestCrossTenantExpenseCategoryReference:
    """`ExpenseCategory` has no dedicated CRUD endpoint in this slice — the
    only way it's reachable from the client is as `Expense.category` on
    create/update. Confirms the writable-FK queryset in
    `ExpenseSerializer.get_fields()` is tenant-scoped (`ExpenseCategory.
    objects`, never `.all_objects`): referencing another tenant's category id
    is rejected as an ordinary invalid-choice 400, never silently accepted
    (which would let a tenant-A expense link to a tenant-B category row)."""

    def test_expense_create_cannot_reference_another_tenants_category(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        project_a = ProjectFactory(tenant=tenant_a)
        category_b = ExpenseCategoryFactory(tenant=tenant_b, name="Tenant B Only Category")
        _login(client, tenant_a, admin_a)

        response = client.post(
            f"/api/v1/projects/{project_a.id}/expenses/",
            data=json.dumps({"amount": "5.00", "date": "2026-01-01", "category": category_b.id}),
            content_type="application/json",
        )
        assert response.status_code == 400, response.content
        assert "category" in response.json()["errors"]


@pytest.mark.django_db(transaction=True)
class TestRlsFiresOnNewM7Tables:
    """THE deferred proof (Slice 2 -> qa-test-engineer): Postgres RLS itself
    (not just the app-level tenant-scoped manager) fail-closes on
    `projects_expense`, `projects_expense_attachment`, and
    `projects_project_document` — mirrors
    `apps.common.tests.test_rls_canonical`'s visibility-differential pattern
    EXACTLY, driven through the real, non-superuser `cortex_app` role
    (`app_role_connection`), never the owner-role Django ORM connection.
    """

    def test_expense_table_rls_visibility_differential(self, app_role_connection):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        project_a = ProjectFactory(tenant=tenant_a)

        expense = Expense.all_objects.create(
            tenant=tenant_a, project=project_a, amount="42.00", date="2026-01-01"
        )

        with app_role_connection.cursor() as cur:
            set_app_role_tenant(app_role_connection, tenant_a.id)
            cur.execute("SELECT id FROM projects_expense WHERE id = %s", [expense.id])
            assert (
                cur.fetchone() is not None
            ), "RLS hid a projects_expense row from its OWN tenant's GUC."

            set_app_role_tenant(app_role_connection, tenant_b.id)
            cur.execute("SELECT id FROM projects_expense WHERE id = %s", [expense.id])
            assert cur.fetchone() is None, (
                "RLS did NOT block a cross-tenant SELECT on projects_expense -- "
                "tenant B's GUC could see tenant A's expense row."
            )

            set_app_role_tenant(app_role_connection, None)
            cur.execute("SELECT id FROM projects_expense WHERE id = %s", [expense.id])
            assert (
                cur.fetchone() is None
            ), "RLS did NOT fail closed on projects_expense with no tenant GUC set."

    def test_expense_attachment_table_rls_visibility_differential(self, app_role_connection):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        project_a = ProjectFactory(tenant=tenant_a)
        expense = Expense.all_objects.create(
            tenant=tenant_a, project=project_a, amount="10.00", date="2026-01-01"
        )
        attachment = ExpenseAttachment.all_objects.create(
            tenant=tenant_a,
            expense=expense,
            storage_key="expense-attachments/x/y/z.pdf",
            filename="z.pdf",
        )

        with app_role_connection.cursor() as cur:
            set_app_role_tenant(app_role_connection, tenant_a.id)
            cur.execute("SELECT id FROM projects_expense_attachment WHERE id = %s", [attachment.id])
            assert cur.fetchone() is not None

            set_app_role_tenant(app_role_connection, tenant_b.id)
            cur.execute("SELECT id FROM projects_expense_attachment WHERE id = %s", [attachment.id])
            assert (
                cur.fetchone() is None
            ), "RLS did NOT block a cross-tenant SELECT on projects_expense_attachment."

            set_app_role_tenant(app_role_connection, None)
            cur.execute("SELECT id FROM projects_expense_attachment WHERE id = %s", [attachment.id])
            assert (
                cur.fetchone() is None
            ), "RLS did NOT fail closed on projects_expense_attachment with no tenant GUC."

    def test_project_document_table_rls_visibility_differential(self, app_role_connection):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        project_a = ProjectFactory(tenant=tenant_a)
        document = ProjectDocument.all_objects.create(
            tenant=tenant_a,
            project=project_a,
            kind=ProjectDocument.Kind.PROPOSAL,
            storage_key="project-documents/x/y/z.pdf",
            filename="z.pdf",
        )

        with app_role_connection.cursor() as cur:
            set_app_role_tenant(app_role_connection, tenant_a.id)
            cur.execute("SELECT id FROM projects_project_document WHERE id = %s", [document.id])
            assert cur.fetchone() is not None

            set_app_role_tenant(app_role_connection, tenant_b.id)
            cur.execute("SELECT id FROM projects_project_document WHERE id = %s", [document.id])
            assert (
                cur.fetchone() is None
            ), "RLS did NOT block a cross-tenant SELECT on projects_project_document."

            set_app_role_tenant(app_role_connection, None)
            cur.execute("SELECT id FROM projects_project_document WHERE id = %s", [document.id])
            assert (
                cur.fetchone() is None
            ), "RLS did NOT fail closed on projects_project_document with no tenant GUC."
