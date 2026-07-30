"""M7 (`docs/tasks/M7-project-grants.md`) backend-slice smoke tests
(add-endpoint golden-path checklist): budget rollup math, the headline
ProjectLead-A-vs-B cross-project 403 isolation, expense create writing an
audit row, and a query-count budget on the rollup/list endpoints. The full
acceptance suite (cross-tenant, every RBAC row, RLS-as-`cortex_app`) is
qa-test-engineer's slice — this file only proves the critical path this
change is responsible for.

**Read before extending — every `UserFactory()` user ALSO gets an automatic
tenant-wide Member `Membership`** (`apps.rbac.signals.
assign_default_membership`, least-privilege default). Member holds
`project.view` tenant-wide (`docs/rbac.md` §3 additions matrix) — so a
"ProjectLead of project A" in this codebase is really "tenant-wide Member +
project-scoped ProjectLead on A" (`add_project_membership`'s own docstring).
Member's `project.view` still lets them see a project ROW exists (`GET
/projects/{id}` 200s), but per the product decision layered on in code
review — "financials and grant documents are restricted to that project's
Lead (scoped) + Admins only" — `budget_total`/`spent`/`remaining`/
`spend_by_category` are redacted to `null` and the ENTIRE `documents`
sub-resource 403s for anyone without `expense.view` scoped to that specific
project (a plain Member, or a Lead of a DIFFERENT project). That is the
isolation boundary the tests below assert on.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from apps.audit.models import AuditLog
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    AssetFactory,
    CategoryFactory,
    ExpenseCategoryFactory,
    ExpenseFactory,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    add_project_membership,
    upgrade_tenant_wide_role,
)
from apps.projects.models import Expense
from apps.rbac.permission_keys import ROLE_ADMIN, ROLE_PROJECT_LEAD

pytestmark = pytest.mark.django_db


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


class TestBudgetRollup:
    def test_spent_remaining_and_spend_by_category_are_correct(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant, budget_total=Decimal("1000.00"))
        equipment = ExpenseCategoryFactory(tenant=tenant, name="Test Equipment")
        travel = ExpenseCategoryFactory(tenant=tenant, name="Test Travel")
        ExpenseFactory(tenant=tenant, project=project, category=equipment, amount=Decimal("300.00"))
        ExpenseFactory(tenant=tenant, project=project, category=equipment, amount=Decimal("50.00"))
        ExpenseFactory(tenant=tenant, project=project, category=travel, amount=Decimal("75.50"))
        ExpenseFactory(tenant=tenant, project=project, category=None, amount=Decimal("10.00"))

        _login(client, tenant, admin)
        response = client.get(f"/api/v1/projects/{project.id}/")

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["budget_total"] == "1000.00"
        assert body["spent"] == "435.50"
        assert body["remaining"] == "564.50"

        by_category = {row["category"]: row["total"] for row in body["spend_by_category"]}
        assert by_category["Test Equipment"] == "350.00"
        assert by_category["Test Travel"] == "75.50"
        assert by_category["Uncategorized"] == "10.00"

    def test_no_budget_set_gives_null_remaining(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant, budget_total=None)

        _login(client, tenant, admin)
        response = client.get(f"/api/v1/projects/{project.id}/")

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["budget_total"] is None
        assert body["spent"] == "0.00"
        assert body["remaining"] is None

    def test_rollup_query_count_is_independent_of_category_count(
        self, client, django_assert_max_num_queries
    ):
        """docs/tasks/M7-project-grants.md: "Compute with a single aggregated
        query (no N+1)" — `apps.projects.services.budget_rollup` itself is
        ONE `GROUP BY` query regardless of how many distinct categories/
        expenses exist (verified directly by count below); the REST of a
        detail request's cost is fixed RBAC-check overhead
        (`apps.rbac.services.get_effective_permissions` is queried once per
        `has_permission`/`has_object_permission`/the serializer's own
        `expense.view` financial-gate check — a pre-existing, request-wide
        lack of memoization shared by every other permission class in this
        codebase, e.g. `apps.assets.permissions.AssetPermission`, not a new
        N+1 introduced here) that does NOT scale with the expense/category
        count either. Asserting a GENEROUS absolute budget here, and that it
        does not grow with data volume, is what actually proves "no N+1" —
        an arbitrarily tight number would be testing the wrong thing.
        """
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant, budget_total=Decimal("500.00"))
        for i in range(2):
            category = ExpenseCategoryFactory(tenant=tenant, name=f"Test Cat Small {i}")
            ExpenseFactory(tenant=tenant, project=project, category=category, amount="10.00")

        _login(client, tenant, admin)
        # Warm `SessionTimeoutMiddleware`'s per-tenant `SessionSettings`
        # cache (a cache-miss `get_or_create` on the FIRST authenticated
        # request only; `conftest.py::_clear_default_cache` clears it before
        # every test) BEFORE the two measured blocks below, so neither one
        # pays that one-time cost -- otherwise it would make `small_ctx`
        # artificially larger than `big_ctx` and break the "must not scale"
        # equality assertion for a reason that has nothing to do with
        # expense-category count.
        client.get(f"/api/v1/projects/{project.id}/")
        with django_assert_max_num_queries(30) as small_ctx:
            response = client.get(f"/api/v1/projects/{project.id}/")
        assert response.status_code == 200, response.content
        small_count = len(small_ctx.captured_queries)

        for i in range(20):
            category = ExpenseCategoryFactory(tenant=tenant, name=f"Test Cat Big {i}")
            ExpenseFactory(tenant=tenant, project=project, category=category, amount="5.00")

        with django_assert_max_num_queries(30) as big_ctx:
            response = client.get(f"/api/v1/projects/{project.id}/")
        assert response.status_code == 200, response.content
        assert (
            len(big_ctx.captured_queries) == small_count
        ), "Query count must not scale with the number of expense categories"


class TestFinancialFieldsGate:
    """Product decision (code-review pass): "financials and grant documents
    are restricted to that project's Lead (scoped) + Admins only" —
    `budget_total` (finding #1, in addition to the ledger-derived rollup
    fields) is redacted to `null`, not a 403, for a caller who can see the
    project row (`project.view`) but lacks `expense.view` scoped to it.
    """

    def test_member_sees_project_but_no_financials_at_all(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)  # default Member role, tenant-wide
        project = ProjectFactory(tenant=tenant, budget_total=Decimal("500.00"))
        ExpenseFactory(tenant=tenant, project=project, amount="50.00")
        _login(client, tenant, member)

        response = client.get(f"/api/v1/projects/{project.id}/")

        assert response.status_code == 200, response.content
        body = response.json()
        # The project ROW is still visible (project.view, tenant-wide) but
        # EVERY financial field — including budget_total, finding #1 — is
        # redacted, not just the ledger-derived rollup.
        assert body["budget_total"] is None
        assert body["spent"] is None
        assert body["remaining"] is None
        assert body["spend_by_category"] is None
        assert body["name"] == project.name  # non-financial metadata: unaffected

    def test_admin_sees_budget_total_on_list_too(self, client):
        """`budget_total`'s redaction lives on the shared `ProjectSerializer`
        base (finding #1's fix), so it must also apply on `GET /projects/`
        (list uses `ProjectSerializer`, not just the detail serializer) —
        and NOT falsely redact it for a caller who legitimately holds
        `expense.view` (Admin, tenant-wide)."""
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        ProjectFactory(tenant=tenant, budget_total=Decimal("250.00"))
        _login(client, tenant, admin)

        response = client.get("/api/v1/projects/")
        assert response.status_code == 200, response.content
        results = response.json()["results"]
        assert len(results) == 1
        assert results[0]["budget_total"] == "250.00"

    def test_member_never_sees_budget_total_on_list(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        ProjectFactory(tenant=tenant, budget_total=Decimal("250.00"))
        _login(client, tenant, member)

        response = client.get("/api/v1/projects/")
        assert response.status_code == 200, response.content
        results = response.json()["results"]
        assert len(results) == 1
        assert results[0]["budget_total"] is None

    def test_project_list_query_count_does_not_grow_with_project_count(
        self, client, django_assert_max_num_queries
    ):
        """Code-review finding: `ProjectSerializer._can_view_financials`
        used to call `user_has_permission(..., project=row.id)` PER ROW —
        each a fresh `Membership`/`Role`/`RolePermission`/`Permission`
        query — so `GET /projects` fired up to one query set PER PAGE ROW.
        `apps.projects.api.ProjectViewSet.get_serializer_context` now
        resolves the caller's `expense.view` scope ONCE per request and
        every row reuses it — so query count must stay CONSTANT as the
        number of projects on the page grows, not scale with N. Mirrors
        `TestBudgetRollup.test_rollup_query_count_is_independent_of_category_count`'s
        same "assert independence of data volume" technique.
        """
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        for i in range(2):
            ProjectFactory(tenant=tenant, name=f"Small Project {i}", budget_total=Decimal("1.00"))
        _login(client, tenant, admin)
        # Warm the per-tenant `SessionSettings` cache before the two measured
        # blocks below (see the matching comment in
        # `TestBudgetRollup.test_rollup_query_count_is_independent_of_category_count`
        # above) so the one-time cache-miss cost doesn't skew this
        # "must not scale with project count" equality assertion.
        client.get("/api/v1/projects/")

        with django_assert_max_num_queries(30) as small_ctx:
            response = client.get("/api/v1/projects/")
        assert response.status_code == 200, response.content
        assert response.json()["count"] == 2
        small_count = len(small_ctx.captured_queries)

        for i in range(15):
            ProjectFactory(tenant=tenant, name=f"Big Project {i}", budget_total=Decimal("1.00"))

        with django_assert_max_num_queries(30) as big_ctx:
            response = client.get("/api/v1/projects/")
        assert response.status_code == 200, response.content
        assert response.json()["count"] == 17
        assert len(big_ctx.captured_queries) == small_count, (
            "Query count must not scale with the number of projects on the page "
            f"(was {small_count} for 2 projects, {len(big_ctx.captured_queries)} for 17)"
        )

        # Confirm the redaction behavior itself is unaffected by the fix —
        # Admin (tenant-wide expense.view) still sees every row's budget.
        for row in response.json()["results"]:
            assert row["budget_total"] == "1.00"


class TestDocumentsReadBoundary:
    """Code-review finding #2 (product decision): project documents
    (proposal/contract/progress_report) routinely restate the exact budget
    figures redacted above, so document READS get the SAME project-scoped
    financial boundary as expenses — a plain Member (or a Lead of a
    DIFFERENT project) gets a 403 on the whole `documents` sub-resource, not
    an empty/redacted list.
    """

    def test_member_403s_on_documents_list(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        project = ProjectFactory(tenant=tenant)
        _login(client, tenant, member)

        response = client.get(f"/api/v1/projects/{project.id}/documents/")
        assert response.status_code == 403, response.content

    def test_admin_can_read_documents_list(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        _login(client, tenant, admin)

        response = client.get(f"/api/v1/projects/{project.id}/documents/")
        assert response.status_code == 200, response.content

    def test_lead_can_read_own_projects_documents_but_403s_on_anothers(self, client):
        tenant = TenantFactory()
        project_a = ProjectFactory(tenant=tenant, name="Project A")
        project_b = ProjectFactory(tenant=tenant, name="Project B")
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project_a, ROLE_PROJECT_LEAD)
        _login(client, tenant, lead)

        own = client.get(f"/api/v1/projects/{project_a.id}/documents/")
        assert own.status_code == 200, own.content

        other = client.get(f"/api/v1/projects/{project_b.id}/documents/")
        assert other.status_code == 403, other.content


class TestProjectLeadCrossProjectIsolation:
    """THE headline M7 acceptance test (task's own framing): a ProjectLead of
    project A gets a server-side 403 — not an empty list/404-by-omission —
    when WRITING project B's budget, expenses, or documents, and when READING
    project B's expense ledger (`expense.view`, unlike `project.view`, is
    never granted to Member tenant-wide)."""

    def _setup_two_projects_one_lead(self, tenant):
        project_a = ProjectFactory(tenant=tenant, name="Project A")
        project_b = ProjectFactory(tenant=tenant, name="Project B")
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project_a, ROLE_PROJECT_LEAD)
        return project_a, project_b, lead

    def test_lead_cannot_patch_other_projects_budget(self, client):
        tenant = TenantFactory()
        project_a, project_b, lead = self._setup_two_projects_one_lead(tenant)
        _login(client, tenant, lead)

        own = client.get(f"/api/v1/projects/{project_a.id}/")
        assert own.status_code == 200, own.content

        patch_other = client.patch(
            f"/api/v1/projects/{project_b.id}/",
            data=json.dumps({"budget_total": "999.00"}),
            content_type="application/json",
        )
        assert patch_other.status_code == 403, patch_other.content

    def test_lead_403s_reading_and_writing_other_projects_expenses(self, client):
        tenant = TenantFactory()
        project_a, project_b, lead = self._setup_two_projects_one_lead(tenant)
        expense_b = ExpenseFactory(tenant=tenant, project=project_b, amount="20.00")
        _login(client, tenant, lead)

        list_other = client.get(f"/api/v1/projects/{project_b.id}/expenses/")
        assert list_other.status_code == 403, list_other.content

        create_other = client.post(
            f"/api/v1/projects/{project_b.id}/expenses/",
            data=json.dumps({"amount": "5.00", "date": "2026-01-01"}),
            content_type="application/json",
        )
        assert create_other.status_code == 403, create_other.content

        detail_other = client.get(f"/api/v1/expenses/{expense_b.id}/")
        assert detail_other.status_code == 403, detail_other.content

        patch_other = client.patch(
            f"/api/v1/expenses/{expense_b.id}/",
            data=json.dumps({"amount": "1.00"}),
            content_type="application/json",
        )
        assert patch_other.status_code == 403, patch_other.content

    def test_lead_403s_writing_other_projects_documents(self, client):
        tenant = TenantFactory()
        project_a, project_b, lead = self._setup_two_projects_one_lead(tenant)
        _login(client, tenant, lead)

        create_other = client.post(f"/api/v1/projects/{project_b.id}/documents/", data={})
        assert create_other.status_code == 403, create_other.content

    def test_lead_can_manage_expenses_within_own_project(self, client):
        tenant = TenantFactory()
        project_a, _project_b, lead = self._setup_two_projects_one_lead(tenant)
        _login(client, tenant, lead)

        create = client.post(
            f"/api/v1/projects/{project_a.id}/expenses/",
            data=json.dumps({"amount": "42.00", "date": "2026-01-01", "vendor": "Acme"}),
            content_type="application/json",
        )
        assert create.status_code == 201, create.content
        assert create.json()["project"] == project_a.id


class TestExpenseAuditTrail:
    def test_expense_create_writes_an_immutable_audit_row(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        _login(client, tenant, admin)

        before_count = AuditLog.all_objects.filter(tenant=tenant, action="expense.manage").count()
        response = client.post(
            f"/api/v1/projects/{project.id}/expenses/",
            data=json.dumps({"amount": "123.45", "date": "2026-02-01", "vendor": "Test Vendor"}),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        expense_id = response.json()["id"]

        entries = AuditLog.all_objects.filter(
            tenant=tenant,
            action="expense.manage",
            entity_type="expense",
            entity_id=str(expense_id),
        )
        assert entries.count() == before_count + 1
        entry = entries.get()
        assert entry.before is None
        assert entry.after["amount"] == "123.45"
        assert entry.actor_id == admin.id

    def test_expense_update_writes_before_after_audit_row(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        expense = ExpenseFactory(tenant=tenant, project=project, amount="10.00")
        _login(client, tenant, admin)

        response = client.patch(
            f"/api/v1/expenses/{expense.id}/",
            data=json.dumps({"amount": "20.00"}),
            content_type="application/json",
        )
        assert response.status_code == 200, response.content

        entry = AuditLog.all_objects.get(
            tenant=tenant, action="expense.manage", entity_type="expense", entity_id=str(expense.id)
        )
        assert entry.before["amount"] == "10.00"
        assert entry.after["amount"] == "20.00"


class TestProjectCreateDestroyAudit:
    """Code-review finding #3: `ProjectViewSet` is a full `ModelViewSet` with
    no `perform_create`/`perform_destroy` audit before this fix — an Admin
    deleting a project silently cascade-wiped every `Expense`/
    `ExpenseAttachment`/`ProjectDocument` with zero `AuditLog` entry.
    """

    def test_project_create_writes_an_audit_row(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        response = client.post(
            "/api/v1/projects/",
            data=json.dumps({"name": "New Grant Project", "budget_total": "1000.00"}),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        project_id = response.json()["id"]

        entry = AuditLog.all_objects.get(
            tenant=tenant, action="tenant.manage", entity_type="project", entity_id=str(project_id)
        )
        assert entry.before is None
        assert entry.after["name"] == "New Grant Project"
        assert entry.after["budget_total"] == "1000.00"

    def test_project_destroy_audits_the_full_cascade(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant, budget_total=Decimal("500.00"))
        category = ExpenseCategoryFactory(tenant=tenant)
        expense = ExpenseFactory(
            tenant=tenant, project=project, category=category, amount=Decimal("42.00")
        )
        _login(client, tenant, admin)

        response = client.delete(f"/api/v1/projects/{project.id}/")
        assert response.status_code == 204, response.content

        entry = AuditLog.all_objects.get(
            tenant=tenant, action="tenant.manage", entity_type="project", entity_id=str(project.id)
        )
        assert entry.after is None
        assert entry.before["project"]["name"] == project.name
        cascaded_ids = [row["id"] for row in entry.before["cascaded_expenses"]]
        assert cascaded_ids == [expense.id]
        assert entry.before["cascaded_expenses"][0]["amount"] == "42.00"

        # The cascade actually happened at the DB level (CASCADE FK).
        assert not Expense.all_objects.filter(id=expense.id).exists()


class TestExpenseAssetScopedToProject:
    """Code-review finding #4: `Expense.asset`'s selectable queryset must be
    scoped to the expense's OWN project (or the general pool,
    `project_id IS NULL`) — never another project's asset, even though both
    are in the same tenant (not an R4 leak, but a cross-project scope
    violation)."""

    def test_can_link_an_expense_to_its_own_projects_asset(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        category = CategoryFactory(tenant=tenant)
        own_asset = AssetFactory(tenant=tenant, category=category, project=project)
        _login(client, tenant, admin)

        response = client.post(
            f"/api/v1/projects/{project.id}/expenses/",
            data=json.dumps({"amount": "10.00", "date": "2026-01-01", "asset": own_asset.id}),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        assert response.json()["asset"] == own_asset.id

    def test_can_link_an_expense_to_a_general_pool_asset(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        category = CategoryFactory(tenant=tenant)
        pool_asset = AssetFactory(tenant=tenant, category=category, project=None)
        _login(client, tenant, admin)

        response = client.post(
            f"/api/v1/projects/{project.id}/expenses/",
            data=json.dumps({"amount": "10.00", "date": "2026-01-01", "asset": pool_asset.id}),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        assert response.json()["asset"] == pool_asset.id

    def test_cannot_link_an_expense_to_another_projects_asset(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project_a = ProjectFactory(tenant=tenant, name="Project A")
        project_b = ProjectFactory(tenant=tenant, name="Project B")
        category = CategoryFactory(tenant=tenant)
        other_projects_asset = AssetFactory(tenant=tenant, category=category, project=project_b)
        _login(client, tenant, admin)

        response = client.post(
            f"/api/v1/projects/{project_a.id}/expenses/",
            data=json.dumps(
                {"amount": "10.00", "date": "2026-01-01", "asset": other_projects_asset.id}
            ),
            content_type="application/json",
        )
        assert response.status_code == 400, response.content
        assert "asset" in response.json()["errors"]

    def test_cannot_repoint_an_existing_expense_to_another_projects_asset(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project_a = ProjectFactory(tenant=tenant, name="Project A")
        project_b = ProjectFactory(tenant=tenant, name="Project B")
        category = CategoryFactory(tenant=tenant)
        other_projects_asset = AssetFactory(tenant=tenant, category=category, project=project_b)
        expense = ExpenseFactory(tenant=tenant, project=project_a, amount="10.00")
        _login(client, tenant, admin)

        response = client.patch(
            f"/api/v1/expenses/{expense.id}/",
            data=json.dumps({"asset": other_projects_asset.id}),
            content_type="application/json",
        )
        assert response.status_code == 400, response.content
        assert "asset" in response.json()["errors"]


class TestMemberHasProjectViewButNoExpenseAccess:
    """Matrix row (docs/tasks/M7-project-grants.md): Member gets
    `project.view` tenant-wide but NOT the financial keys."""

    def test_member_can_view_project_but_not_expenses(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)  # default Member role, tenant-wide
        project = ProjectFactory(tenant=tenant)
        _login(client, tenant, member)

        view = client.get(f"/api/v1/projects/{project.id}/")
        assert view.status_code == 200, view.content

        expenses = client.get(f"/api/v1/projects/{project.id}/expenses/")
        assert expenses.status_code == 403, expenses.content
