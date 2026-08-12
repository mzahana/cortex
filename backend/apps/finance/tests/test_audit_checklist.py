"""The audit-readiness checklist (M8 Phase 3 §6.3).

Finds what an auditor would ask about, before they ask. The distinction these
tests care most about is **blocker vs advisory**: unaccounted money and missing
paperwork would actually fail an audit; a missing serial number is untidy. A
list that ranks those equally is a list nobody reads.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    AssetFactory,
    CategoryFactory,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.finance.checklist import render_checklist_html
from apps.finance.services import resolve_checklist_data
from apps.jobs.models import Job
from apps.projects.models import Project
from apps.rbac.permission_keys import ROLE_ADMIN, ROLE_MEMBER
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content


@pytest.fixture
def setup(client):
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        category = CategoryFactory(tenant=tenant)
    _login(client, tenant, admin)
    return tenant, admin, project, category


def _make_order(client, project, *, amount="100.00", items=None):
    return client.post(
        "/api/v1/orders/",
        {
            "project": project.id,
            "vendor": "Amazon",
            "paid_on": "2026-03-02",
            "amount": amount,
            "currency": "SAR",
            "splits": [{"items": items if items is not None else []}],
        },
        content_type="application/json",
    ).json()


def test_unaccounted_money_and_missing_paperwork_are_blockers(client, setup):
    tenant, admin, project, _category = setup
    _make_order(client, project, amount="500.00", items=[{"description": "X", "amount": "300.00"}])

    with tenant_context(tenant.id):
        data = resolve_checklist_data(Project.objects.get(pk=project.id), generated_by=admin.email)

    blockers = " | ".join(f.what for f in data.blockers)
    assert "not itemized" in blockers, "the 200.00 gap was not reported"
    assert "No bank statement attached" in blockers
    assert "no receipt scan" in blockers
    # None of those are merely untidy.
    assert all(f.severity == "blocker" for f in data.blockers)


def test_untidy_records_are_advisories_not_blockers(client, setup):
    tenant, admin, project, category = setup
    with tenant_context(tenant.id):
        AssetFactory(
            tenant=tenant, category=category, project=project, name="Nameless", serial_number=""
        )

    with tenant_context(tenant.id):
        data = resolve_checklist_data(Project.objects.get(pk=project.id))

    advisories = " | ".join(f.what for f in data.advisories)
    assert "no photo" in advisories
    assert "no serial number" in advisories


def test_a_complete_project_reports_nothing_outstanding(client, setup):
    """The pass case has to be reachable, or the checklist is just noise."""
    tenant, _admin, project, category = setup
    created = _make_order(
        client, project, amount="100.00", items=[{"description": "X", "amount": "100.00"}]
    )
    # Attach the paperwork and categorize the item.
    client.post(
        f"/api/v1/payments/{created['id']}/attachment/",
        {"file": SimpleUploadedFile("s.pdf", b"%PDF-1.4", "application/pdf")},
    )
    split = created["splits"][0]
    client.post(
        f"/api/v1/purchases/{split['id']}/attachment/",
        {"file": SimpleUploadedFile("r.pdf", b"%PDF-1.4", "application/pdf")},
    )
    client.patch(
        f"/api/v1/orders/{created['id']}/",
        {
            "splits": [
                {
                    "id": split["id"],
                    "items": [
                        {
                            "id": split["items"][0]["id"],
                            "description": "X",
                            "amount": "100.00",
                            "category": category.id if hasattr(category, "id") else None,
                        }
                    ],
                }
            ]
        },
        content_type="application/json",
    )

    with tenant_context(tenant.id):
        data = resolve_checklist_data(Project.objects.get(pk=project.id))

    assert data.blockers == [], [f.what for f in data.blockers]
    assert "Nothing outstanding" in render_checklist_html(data)


def test_checklist_endpoint_enqueues_and_renders(client, setup):
    from apps.finance.tasks import generate_audit_checklist_pdf

    tenant, _admin, project, _category = setup
    _make_order(client, project, amount="500.00", items=[{"description": "X", "amount": "300.00"}])

    response = client.post(f"/api/v1/projects/{project.id}/audit-readiness/")
    assert response.status_code == 202, response.content
    job_id = response.json()["id"]

    generate_audit_checklist_pdf(job_id=str(job_id), tenant_id=tenant.id, project_id=project.id)

    with tenant_context(tenant.id):
        job = Job.objects.get(pk=job_id)
        assert job.status == Job.Status.SUCCEEDED, job.error
        assert job.result_key.startswith(f"audit-checklists/{tenant.id}/")


def test_checklist_requires_the_financial_permission(client, setup):
    """It names charges and amounts, so a Member who cannot see expenses must
    not be able to generate it."""
    tenant, _admin, project, _category = setup
    with tenant_context(tenant.id):
        member = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(member, ROLE_MEMBER)
    _login(client, tenant, member)

    response = client.post(f"/api/v1/projects/{project.id}/audit-readiness/")
    assert response.status_code == 403, response.content


def test_checklist_is_tenant_isolated(client, setup):
    tenant, _admin, project, _category = setup
    other = TenantFactory()
    with tenant_context(other.id):
        intruder = UserFactory(tenant=other)
        upgrade_tenant_wide_role(intruder, ROLE_ADMIN)
    _login(client, other, intruder)

    assert client.post(f"/api/v1/projects/{project.id}/audit-readiness/").status_code == 404


def test_blockers_are_listed_before_advisories(client, setup):
    """Ordering is the whole usability of the document."""
    tenant, _admin, project, category = setup
    _make_order(client, project, amount="500.00", items=[{"description": "X", "amount": "300.00"}])
    with tenant_context(tenant.id):
        AssetFactory(tenant=tenant, category=category, project=project, serial_number="")
        data = resolve_checklist_data(Project.objects.get(pk=project.id))

    html = render_checklist_html(data)
    assert html.index("Would fail an audit") < html.index("Worth tidying")
    assert Decimal("0") == Decimal("0")  # sanity: no arithmetic assertion needed here
