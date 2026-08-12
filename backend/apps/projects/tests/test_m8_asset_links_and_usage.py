"""M8 Phase 1 acceptance: an expense links to MANY assets, and an asset is
used by MANY projects — without either fact moving money.

Exit criteria from `docs/tasks/M8-expense-reconciliation.md` §9, Phase 1:
- an expense links to N assets;
- an asset is used by N projects;
- **usage provably moves no money** (the headline invariant, §1.6 — violating
  it double-counts equipment across grants, which is the finding an auditor
  acts on);
- existing single-asset expenses keep working unchanged.

Plus the two security properties the new write paths could plausibly break:
cross-project asset selection (code-review finding #4 from M7, which the new
`assets` list field must not reintroduce) and tenant isolation on both new
tables.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.assets.models import AssetProjectUsage
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    AssetFactory,
    CategoryFactory,
    ExpenseFactory,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    add_project_membership,
    upgrade_tenant_wide_role,
)
from apps.projects.models import ExpenseAssetLink
from apps.projects.services import (
    budget_rollup,
    resolve_asset_allocations,
    split_amount_evenly,
)
from apps.rbac.permission_keys import ROLE_ADMIN, ROLE_PROJECT_LEAD
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


@pytest.fixture
def admin_setup(client):
    """One tenant, one project, an Admin logged in, and an asset category."""
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        user = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(user, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        category = CategoryFactory(tenant=tenant)
    _login(client, tenant, user)
    return tenant, user, project, category


# --------------------------------------------------------------------------
# The split helper — exactness is the whole point (M8 §3 rounding rule).
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("total", "parts", "expected"),
    [
        # The canonical case: 100/3 naively rounded gives 99.99 and loses a
        # cent. Largest-remainder hands it to the first share instead.
        (Decimal("100.00"), 3, [Decimal("33.34"), Decimal("33.33"), Decimal("33.33")]),
        (Decimal("834.56"), 2, [Decimal("417.28"), Decimal("417.28")]),
        (Decimal("0.01"), 2, [Decimal("0.01"), Decimal("0.00")]),
        (Decimal("10.00"), 1, [Decimal("10.00")]),
        # A credit/refund splits in the same direction.
        (Decimal("-100.00"), 3, [Decimal("-33.34"), Decimal("-33.33"), Decimal("-33.33")]),
    ],
)
def test_split_amount_evenly_sums_exactly(total, parts, expected):
    shares = split_amount_evenly(total, parts)
    assert shares == expected
    assert sum(shares) == total, "a split that doesn't sum back is an audit residual"


def test_split_amount_evenly_handles_zero_parts():
    assert split_amount_evenly(Decimal("10.00"), 0) == []


# --------------------------------------------------------------------------
# One expense -> many assets
# --------------------------------------------------------------------------


def test_expense_links_to_many_assets_and_splits_cost(client, admin_setup):
    tenant, _user, project, category = admin_setup
    with tenant_context(tenant.id):
        assets = [AssetFactory(tenant=tenant, category=category, project=project) for _ in range(3)]

    response = client.post(
        f"/api/v1/projects/{project.id}/expenses/",
        {
            "amount": "100.00",
            "date": "2026-03-02",
            "vendor": "Amazon",
            "assets": [a.id for a in assets],
        },
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    body = response.json()

    assert len(body["asset_links"]) == 3
    # No amounts were supplied, so every link is AUTO (`allocated_amount` null)
    # and the resolved shares split the line to the cent.
    assert all(link["allocated_amount"] is None for link in body["asset_links"])
    allocations = [Decimal(link["resolved_amount"]) for link in body["asset_links"]]
    assert sum(allocations) == Decimal("100.00")
    assert sorted(link["asset"] for link in body["asset_links"]) == sorted(a.id for a in assets)

    # The deprecated single FK is kept in sync with the first link so every
    # M7-era reader (CSV export, report asset section) stays correct during
    # the deprecation release (M8 §8).
    assert body["asset"] == assets[0].id


def test_updating_assets_replaces_links_and_resplits(client, admin_setup):
    tenant, _user, project, category = admin_setup
    with tenant_context(tenant.id):
        first, second, third = (
            AssetFactory(tenant=tenant, category=category, project=project) for _ in range(3)
        )
        expense = ExpenseFactory(tenant=tenant, project=project, amount=Decimal("90.00"))

    response = client.patch(
        f"/api/v1/expenses/{expense.id}/",
        {"assets": [first.id, second.id]},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content

    with tenant_context(tenant.id):
        links = ExpenseAssetLink.objects.filter(expense=expense)
        assert {link.asset_id for link in links} == {first.id, second.id}
        assert sum(resolve_asset_allocations(expense).values()) == Decimal("90.00")

    # Swapping the set drops the old links entirely rather than accumulating.
    response = client.patch(
        f"/api/v1/expenses/{expense.id}/",
        {"assets": [third.id]},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    with tenant_context(tenant.id):
        links = ExpenseAssetLink.objects.filter(expense=expense)
        assert [link.asset_id for link in links] == [third.id]
        assert resolve_asset_allocations(expense)[links[0].id] == Decimal("90.00")


def test_changing_amount_resplits_existing_links(client, admin_setup):
    """A line's total moving must not leave the per-asset shares stale — they
    would silently stop summing to the ledger figure."""
    tenant, _user, project, category = admin_setup
    with tenant_context(tenant.id):
        assets = [AssetFactory(tenant=tenant, category=category, project=project) for _ in range(2)]
        expense = ExpenseFactory(tenant=tenant, project=project, amount=Decimal("50.00"))

    client.patch(
        f"/api/v1/expenses/{expense.id}/",
        {"assets": [a.id for a in assets]},
        content_type="application/json",
    )
    response = client.patch(
        f"/api/v1/expenses/{expense.id}/",
        {"amount": "75.00"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content

    with tenant_context(tenant.id):
        expense.refresh_from_db()
        assert sum(resolve_asset_allocations(expense).values()) == Decimal("75.00")


def test_m7_single_asset_expense_still_works_unchanged(client, admin_setup):
    """Backward compatibility: an M7-era client posting `asset` alone (no
    `assets` list) must behave exactly as before."""
    tenant, _user, project, category = admin_setup
    with tenant_context(tenant.id):
        asset = AssetFactory(tenant=tenant, category=category, project=project)

    response = client.post(
        f"/api/v1/projects/{project.id}/expenses/",
        {"amount": "42.00", "date": "2026-03-02", "asset": asset.id},
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    assert response.json()["asset"] == asset.id


def test_assets_field_rejects_another_projects_asset(client, admin_setup):
    """M7 code-review finding #4 must not be reintroducible through the new
    multi-asset field: an expense may only link its OWN project's assets or
    general-pool ones."""
    tenant, _user, project, category = admin_setup
    with tenant_context(tenant.id):
        other_project = ProjectFactory(tenant=tenant)
        foreign_asset = AssetFactory(tenant=tenant, category=category, project=other_project)
        pool_asset = AssetFactory(tenant=tenant, category=category, project=None)

    response = client.post(
        f"/api/v1/projects/{project.id}/expenses/",
        {"amount": "10.00", "date": "2026-03-02", "assets": [foreign_asset.id]},
        content_type="application/json",
    )
    assert response.status_code == 400, response.content

    # ...while a general-pool asset is still allowed, as it was for `asset`.
    response = client.post(
        f"/api/v1/projects/{project.id}/expenses/",
        {"amount": "10.00", "date": "2026-03-02", "assets": [pool_asset.id]},
        content_type="application/json",
    )
    assert response.status_code == 201, response.content


def test_expense_asset_links_are_tenant_isolated(client, admin_setup):
    """R4: another tenant's asset id must never resolve, even though the id is
    a perfectly valid integer."""
    tenant, _user, project, category = admin_setup
    other_tenant = TenantFactory()
    with tenant_context(other_tenant.id):
        other_category = CategoryFactory(tenant=other_tenant)
        other_asset = AssetFactory(tenant=other_tenant, category=other_category, project=None)

    response = client.post(
        f"/api/v1/projects/{project.id}/expenses/",
        {"amount": "10.00", "date": "2026-03-02", "assets": [other_asset.id]},
        content_type="application/json",
    )
    assert response.status_code == 400, response.content


# --------------------------------------------------------------------------
# One asset -> many projects, and the money invariant
# --------------------------------------------------------------------------


def test_asset_used_by_many_projects(client, admin_setup):
    tenant, _user, project, category = admin_setup
    with tenant_context(tenant.id):
        asset = AssetFactory(tenant=tenant, category=category, project=project)
        using_a = ProjectFactory(tenant=tenant)
        using_b = ProjectFactory(tenant=tenant)

    for using in (using_a, using_b):
        response = client.post(
            f"/api/v1/assets/{asset.id}/usages/",
            {"project": using.id, "start_date": "2026-01-01"},
            content_type="application/json",
        )
        assert response.status_code == 201, response.content

    response = client.get(f"/api/v1/assets/{asset.id}/usages/")
    assert response.status_code == 200, response.content
    payload = response.json()
    rows = payload["results"] if isinstance(payload, dict) else payload
    assert {row["project"] for row in rows} == {using_a.id, using_b.id}

    # The funding project is untouched by any of this.
    with tenant_context(tenant.id):
        asset.refresh_from_db()
        assert asset.project_id == project.id


def test_asset_usage_moves_no_money(client, admin_setup):
    """THE Phase 1 invariant (M8 §1.6): recording usage must leave every
    project's budget rollup byte-identical. If this test ever fails, an asset's
    cost is being counted against a project that did not pay for it."""
    tenant, _user, funding_project, category = admin_setup
    with tenant_context(tenant.id):
        asset = AssetFactory(tenant=tenant, category=category, project=funding_project)
        ExpenseFactory(tenant=tenant, project=funding_project, amount=Decimal("1200.00"))
        using_project = ProjectFactory(tenant=tenant)

        before_funding = budget_rollup(funding_project)
        before_using = budget_rollup(using_project)

    response = client.post(
        f"/api/v1/assets/{asset.id}/usages/",
        {"project": using_project.id, "start_date": "2026-01-01"},
        content_type="application/json",
    )
    assert response.status_code == 201, response.content

    with tenant_context(tenant.id):
        assert budget_rollup(funding_project) == before_funding
        assert budget_rollup(using_project) == before_using
        # Specifically: the using project still shows zero spend.
        assert budget_rollup(using_project)["spent"] == Decimal("0")


def test_duplicate_open_usage_is_409_not_500(client, admin_setup):
    """The partial unique index must surface as a clean conflict — the client
    should close the open period, not create a second one."""
    tenant, _user, project, category = admin_setup
    with tenant_context(tenant.id):
        asset = AssetFactory(tenant=tenant, category=category, project=project)
        using = ProjectFactory(tenant=tenant)

    payload = {"project": using.id, "start_date": "2026-01-01"}
    first = client.post(
        f"/api/v1/assets/{asset.id}/usages/", payload, content_type="application/json"
    )
    assert first.status_code == 201, first.content

    second = client.post(
        f"/api/v1/assets/{asset.id}/usages/", payload, content_type="application/json"
    )
    assert second.status_code == 409, second.content

    # Closing the first period frees the pair for a new one.
    close = client.patch(
        f"/api/v1/asset-usages/{first.json()['id']}/",
        {"end_date": "2026-06-30"},
        content_type="application/json",
    )
    assert close.status_code == 200, close.content

    third = client.post(
        f"/api/v1/assets/{asset.id}/usages/",
        {"project": using.id, "start_date": "2026-07-01"},
        content_type="application/json",
    )
    assert third.status_code == 201, third.content


def test_usage_rejects_end_before_start(client, admin_setup):
    tenant, _user, project, category = admin_setup
    with tenant_context(tenant.id):
        asset = AssetFactory(tenant=tenant, category=category, project=project)
        using = ProjectFactory(tenant=tenant)

    response = client.post(
        f"/api/v1/assets/{asset.id}/usages/",
        {"project": using.id, "start_date": "2026-06-01", "end_date": "2026-01-01"},
        content_type="application/json",
    )
    assert response.status_code == 400, response.content


def test_usage_is_tenant_isolated(client, admin_setup):
    """R4: an asset id from another tenant 404s rather than accepting a usage
    row against it."""
    tenant, _user, _project, _category = admin_setup
    other_tenant = TenantFactory()
    with tenant_context(other_tenant.id):
        other_category = CategoryFactory(tenant=other_tenant)
        other_asset = AssetFactory(tenant=other_tenant, category=other_category, project=None)
        other_project = ProjectFactory(tenant=other_tenant)

    response = client.post(
        f"/api/v1/assets/{other_asset.id}/usages/",
        {"project": other_project.id},
        content_type="application/json",
    )
    assert response.status_code == 404, response.content

    with tenant_context(other_tenant.id):
        assert not AssetProjectUsage.objects.exists()


def test_project_lead_cannot_add_usage_to_another_projects_asset(client):
    """RBAC: `asset.edit` is evaluated against the ASSET's funding project, so
    a lead of project A gets a 403 (not an empty list, not a 404) on project
    B's equipment."""
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        lead = UserFactory(tenant=tenant)
        project_a = ProjectFactory(tenant=tenant)
        project_b = ProjectFactory(tenant=tenant)
        add_project_membership(lead, project_a, ROLE_PROJECT_LEAD)
        category = CategoryFactory(tenant=tenant)
        b_asset = AssetFactory(tenant=tenant, category=category, project=project_b)

    _login(client, tenant, lead)

    response = client.post(
        f"/api/v1/assets/{b_asset.id}/usages/",
        {"project": project_a.id, "start_date": "2026-01-01"},
        content_type="application/json",
    )
    assert response.status_code == 403, response.content


def test_project_lead_can_manage_usage_on_own_asset(client):
    """The other half of the rule the user asked for: a lead has full control
    over their own project's equipment records."""
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        lead = UserFactory(tenant=tenant)
        project_a = ProjectFactory(tenant=tenant)
        other = ProjectFactory(tenant=tenant)
        add_project_membership(lead, project_a, ROLE_PROJECT_LEAD)
        category = CategoryFactory(tenant=tenant)
        own_asset = AssetFactory(tenant=tenant, category=category, project=project_a)

    _login(client, tenant, lead)

    created = client.post(
        f"/api/v1/assets/{own_asset.id}/usages/",
        {"project": other.id, "start_date": "2026-01-01"},
        content_type="application/json",
    )
    assert created.status_code == 201, created.content

    deleted = client.delete(f"/api/v1/asset-usages/{created.json()['id']}/")
    assert deleted.status_code == 204, deleted.content


# --------------------------------------------------------------------------
# Per-asset amounts (the user's "split evenly makes no sense" report)
# --------------------------------------------------------------------------


def test_explicit_per_asset_amounts_are_stored_verbatim(client, admin_setup):
    """A real receipt does not divide equally: a $350 GPU and a $9 cable on one
    line are not $179.50 each. Typed figures must be stored exactly."""
    tenant, _user, project, category = admin_setup
    with tenant_context(tenant.id):
        gpu = AssetFactory(tenant=tenant, category=category, project=project)
        cable = AssetFactory(tenant=tenant, category=category, project=project)

    response = client.post(
        f"/api/v1/projects/{project.id}/expenses/",
        {
            "amount": "359.00",
            "date": "2026-03-02",
            "asset_allocations": [
                {"asset": gpu.id, "allocated_amount": "350.00"},
                {"asset": cable.id, "allocated_amount": "9.00"},
            ],
        },
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    by_asset = {link["asset"]: link for link in response.json()["asset_links"]}
    assert by_asset[gpu.id]["allocated_amount"] == "350.00"
    assert by_asset[cable.id]["allocated_amount"] == "9.00"
    assert by_asset[gpu.id]["resolved_amount"] == "350.00"


def test_mixed_explicit_and_auto_amounts(client, admin_setup):
    """Type the price you know; the rest share what's left."""
    tenant, _user, project, category = admin_setup
    with tenant_context(tenant.id):
        known = AssetFactory(tenant=tenant, category=category, project=project)
        rest = [AssetFactory(tenant=tenant, category=category, project=project) for _ in range(2)]

    response = client.post(
        f"/api/v1/projects/{project.id}/expenses/",
        {
            "amount": "100.00",
            "date": "2026-03-02",
            "asset_allocations": [
                {"asset": known.id, "allocated_amount": "40.00"},
                {"asset": rest[0].id},
                {"asset": rest[1].id},
            ],
        },
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    by_asset = {link["asset"]: link for link in response.json()["asset_links"]}
    assert by_asset[known.id]["resolved_amount"] == "40.00"
    # The remaining 60.00 splits between the other two.
    assert Decimal(by_asset[rest[0].id]["resolved_amount"]) == Decimal("30.00")
    assert Decimal(by_asset[rest[1].id]["resolved_amount"]) == Decimal("30.00")


def test_typed_amounts_survive_a_total_edit_but_auto_shares_follow(client, admin_setup):
    """The distinction that makes `allocated_amount = NULL` load-bearing:
    editing the line total must not silently overwrite a figure someone typed
    off a receipt, but must keep auto shares consistent with the new total."""
    tenant, _user, project, category = admin_setup
    with tenant_context(tenant.id):
        typed = AssetFactory(tenant=tenant, category=category, project=project)
        auto = AssetFactory(tenant=tenant, category=category, project=project)

    created = client.post(
        f"/api/v1/projects/{project.id}/expenses/",
        {
            "amount": "100.00",
            "date": "2026-03-02",
            "asset_allocations": [
                {"asset": typed.id, "allocated_amount": "40.00"},
                {"asset": auto.id},
            ],
        },
        content_type="application/json",
    )
    assert created.status_code == 201, created.content
    expense_id = created.json()["id"]

    bumped = client.patch(
        f"/api/v1/expenses/{expense_id}/",
        {"amount": "150.00"},
        content_type="application/json",
    )
    assert bumped.status_code == 200, bumped.content
    by_asset = {link["asset"]: link for link in bumped.json()["asset_links"]}
    assert by_asset[typed.id]["resolved_amount"] == "40.00", "typed figure was overwritten"
    assert Decimal(by_asset[auto.id]["resolved_amount"]) == Decimal("110.00")


def test_allocations_that_do_not_sum_are_saved_not_rejected(client, admin_setup):
    """M8's warn-never-block rule: real receipts carry roundings and partial
    refunds, and the app must not refuse the user's data over arithmetic."""
    tenant, _user, project, category = admin_setup
    with tenant_context(tenant.id):
        asset = AssetFactory(tenant=tenant, category=category, project=project)

    response = client.post(
        f"/api/v1/projects/{project.id}/expenses/",
        {
            "amount": "100.00",
            "date": "2026-03-02",
            "asset_allocations": [{"asset": asset.id, "allocated_amount": "97.50"}],
        },
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    assert response.json()["asset_links"][0]["resolved_amount"] == "97.50"


def test_asset_allocations_reject_another_projects_asset(client, admin_setup):
    """The new writable field must not be the hole that `assets` isn't."""
    tenant, _user, project, category = admin_setup
    with tenant_context(tenant.id):
        other_project = ProjectFactory(tenant=tenant)
        foreign = AssetFactory(tenant=tenant, category=category, project=other_project)

    response = client.post(
        f"/api/v1/projects/{project.id}/expenses/",
        {
            "amount": "10.00",
            "date": "2026-03-02",
            "asset_allocations": [{"asset": foreign.id, "allocated_amount": "10.00"}],
        },
        content_type="application/json",
    )
    assert response.status_code == 400, response.content


def test_linking_an_unassigned_asset_moves_it_into_the_project(client, admin_setup):
    """An asset created with no project could previously never be expensed
    anywhere — the form didn't offer it, and if it had, the asset would have
    stayed invisible on the project's Assets page while its cost sat in that
    project's ledger. Expensing it to a project means that project's fund paid
    for it, so its funding project follows."""
    tenant, _user, project, category = admin_setup
    with tenant_context(tenant.id):
        pool_asset = AssetFactory(tenant=tenant, category=category, project=None)

    response = client.post(
        f"/api/v1/projects/{project.id}/expenses/",
        {"amount": "25.00", "date": "2026-03-02", "assets": [pool_asset.id]},
        content_type="application/json",
    )
    assert response.status_code == 201, response.content

    with tenant_context(tenant.id):
        pool_asset.refresh_from_db()
        assert pool_asset.project_id == project.id


def test_unassigned_filter_lists_only_general_pool_assets(client, admin_setup):
    """`?unassigned=true` is how the expense form reaches general-pool assets —
    `?project=` has no way to express "none"."""
    tenant, _user, project, category = admin_setup
    with tenant_context(tenant.id):
        pool_asset = AssetFactory(tenant=tenant, category=category, project=None)
        owned = AssetFactory(tenant=tenant, category=category, project=project)

    response = client.get("/api/v1/assets/?unassigned=true")
    assert response.status_code == 200, response.content
    ids = [row["id"] for row in response.json()["results"]]
    assert pool_asset.id in ids
    assert owned.id not in ids
