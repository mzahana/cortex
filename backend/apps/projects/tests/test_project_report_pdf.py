"""M7 Slice 3 acceptance test (`docs/tasks/M7-project-grants.md` "Report
PDF"): `POST /api/v1/projects/{id}/report` enqueues a Celery job (reusing the
`apps.jobs` poller exactly like `apps.labels.api.LabelGenerateView`), the
worker renders a non-empty PDF via WeasyPrint and stores it on the SAME
storage backend every attachment/label PDF uses, and the job is pollable via
`GET /api/v1/jobs/{id}` until `download_url` is populated.

Content assertions run against `apps.projects.report.
render_project_report_html`'s OUTPUT (the pre-WeasyPrint HTML string) rather
than parsed PDF bytes — cheaper and more precise (same technique this
module's docstring on `render_project_report_html` documents), and exercised
both directly (unit-style, `TestReportContent`) and through the full
Celery-task-and-poll round trip (`TestReportGenerateEndToEnd`).
"""

from __future__ import annotations

import io
from decimal import Decimal

import pytest

from apps.assets.models import Attachment
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
from apps.jobs.models import Job
from apps.projects.report import (
    AssetRow,
    CategorySpendRow,
    ExpenseRow,
    ProjectReportData,
    render_project_report_html,
)
from apps.projects.services import resolve_project_report_data
from apps.rbac.models import Membership
from apps.rbac.permission_keys import ROLE_ADMIN, ROLE_PROJECT_LEAD, ROLE_VIEWER
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


def _generate(client, project_id):
    return client.post(f"/api/v1/projects/{project_id}/report/")


def _full_data(**overrides) -> ProjectReportData:
    defaults = dict(
        tenant_name="Robotics Lab",
        name="Autonomous Rover",
        code="NSF-2026-014",
        funding_source="external",
        sponsor="NSF",
        lead_name="Ada Lovelace",
        start_date="2026-01-01",
        end_date="2026-12-31",
        status="active",
        currency="USD",
        budget_total=Decimal("10000.00"),
        spent=Decimal("1200.00"),
        remaining=Decimal("8800.00"),
        spend_by_category=[CategorySpendRow(category="Equipment", total=Decimal("1200.00"))],
        expenses=[
            ExpenseRow(
                date="2026-02-01",
                category="Equipment",
                vendor="Acme Corp",
                invoice_number="INV-001",
                description="RTX 4090",
                amount=Decimal("1200.00"),
            )
        ],
        assets=[
            AssetRow(
                name="RTX Box A",
                category="GPU",
                serial_number="SN-123",
                purchase_cost=Decimal("1200.00"),
                status="available",
            )
        ],
        documents=[],
        invoices=[],
    )
    defaults.update(overrides)
    return ProjectReportData(**defaults)


class TestReportContent:
    """Unit-style assertions directly against the rendered HTML (task
    instruction: "assert the budget numbers/expense rows appear in the
    rendered output ... to avoid parsing PDF bytes")."""

    def test_header_budget_expenses_and_assets_all_appear(self):
        html = render_project_report_html(_full_data())
        assert "Autonomous Rover" in html
        assert "NSF-2026-014" in html
        assert "NSF" in html
        assert "Ada Lovelace" in html
        assert "USD 10,000.00" in html  # budget_total
        assert "USD 1,200.00" in html  # spent / expense amount / spend-by-category
        assert "USD 8,800.00" in html  # remaining
        assert "Equipment" in html
        assert "Acme Corp" in html
        assert "INV-001" in html
        assert "RTX Box A" in html
        assert "SN-123" in html

    def test_no_budget_set_renders_not_set_without_crashing(self):
        data = _full_data(budget_total=None, remaining=None)
        html = render_project_report_html(data)
        assert "Not set" in html
        # `spent`/spend-by-category are independent of whether a budget was
        # ever awarded -- still rendered.
        assert "USD 1,200.00" in html

    def test_zero_expenses_renders_empty_state_without_crashing(self):
        data = _full_data(spent=Decimal("0.00"), spend_by_category=[], expenses=[])
        html = render_project_report_html(data)
        assert "No expenses recorded yet." in html
        assert "No expenses have been booked against this project." in html

    def test_no_assets_no_documents_no_invoices_renders_empty_states(self):
        data = _full_data(assets=[], documents=[], invoices=[])
        html = render_project_report_html(data)
        assert "No assets are linked to this project." in html
        assert "No project documents on file." in html
        assert "No invoice scans on file." in html

    def test_html_escapes_free_text_fields(self):
        """Asset/expense free-text fields are user-supplied -- an unescaped
        `<`/`&` must not corrupt the layout (same reasoning `apps.labels.
        rendering._label_html`'s module docstring documents for label text).
        """
        data = _full_data(
            expenses=[
                ExpenseRow(
                    date="2026-02-01",
                    category="Equipment",
                    vendor="<script>alert(1)</script>",
                    invoice_number="INV-002",
                    description="A & B",
                    amount=Decimal("5.00"),
                )
            ]
        )
        html = render_project_report_html(data)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestResolveProjectReportData:
    """`apps.projects.services.resolve_project_report_data` — the real DB
    -> `ProjectReportData` path, exercised inside `tenant_context` the same
    way the Celery task runs it."""

    def test_resolves_budget_expenses_and_assets_from_the_database(self):
        tenant = TenantFactory()
        project = ProjectFactory(
            tenant=tenant,
            name="Test Grant",
            code="G-1",
            funding_source="internal",
            budget_total="500.00",
            currency="USD",
        )
        # `apps.projects.signals.seed_expense_categories` already seeded the
        # default starter set (Equipment/Consumables/.../Travel/...) for this
        # brand-new tenant -- use a name outside that set to avoid the
        # `uniq_expense_category_tenant_name` collision.
        category = ExpenseCategoryFactory(tenant=tenant, name="Field Trip")
        ExpenseFactory(
            tenant=tenant,
            project=project,
            category=category,
            amount="150.00",
            vendor="Delta",
            invoice_number="INV-9",
        )
        asset_category = CategoryFactory(tenant=tenant, name="Sensor")
        with tenant_context(tenant.id):
            AssetFactory(
                tenant=tenant,
                category=asset_category,
                project=project,
                name="Lidar Unit",
                serial_number="LID-1",
                purchase_cost="300.00",
            )
            data = resolve_project_report_data(_reload_project(project))

        assert data.name == "Test Grant"
        assert data.budget_total == Decimal("500.00")
        assert data.spent == Decimal("150.00")
        assert data.remaining == Decimal("350.00")
        assert data.spend_by_category == [
            CategorySpendRow(category="Field Trip", total=Decimal("150.00"))
        ]
        assert len(data.expenses) == 1
        assert data.expenses[0].vendor == "Delta"
        assert len(data.assets) == 1
        assert data.assets[0].name == "Lidar Unit"
        assert data.assets[0].serial_number == "LID-1"

    def test_no_budget_and_zero_expenses_do_not_crash(self):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant, name="Bare Project")
        with tenant_context(tenant.id):
            data = resolve_project_report_data(_reload_project(project))
        assert data.budget_total is None
        assert data.remaining is None
        assert data.spent == Decimal("0.00")
        assert data.expenses == []
        assert data.assets == []

    def test_asset_photo_attachment_becomes_a_data_uri(self, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        asset_category = CategoryFactory(tenant=tenant)
        with tenant_context(tenant.id):
            asset = AssetFactory(
                tenant=tenant, category=asset_category, project=project, name="Photographed"
            )
            # 1x1 PNG bytes -- doesn't need to be a real image beyond having
            # an `image/*` content type; `_asset_photo_data_uri` only checks
            # the stored `content_type`, not the byte contents themselves.
            Attachment.all_objects.create(
                tenant=tenant,
                asset=asset,
                kind="photo",
                storage_key="attachments/x/1/fake.png",
                filename="fake.png",
                content_type="image/png",
                size=10,
            )
            from django.core.files.storage import default_storage

            default_storage.save("attachments/x/1/fake.png", io.BytesIO(b"\x89PNG\r\n\x1a\n"))

            data = resolve_project_report_data(_reload_project(project))

        assert len(data.assets) == 1
        assert data.assets[0].photo_data_uri is not None
        assert data.assets[0].photo_data_uri.startswith("data:image/png;base64,")

    def test_missing_photo_file_skips_gracefully(self, settings, tmp_path):
        """Task spec: "skip gracefully if not" (renders cleanly) -- a
        dangling `Attachment` row whose file was never actually written
        must not crash report generation."""
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        asset_category = CategoryFactory(tenant=tenant)
        with tenant_context(tenant.id):
            asset = AssetFactory(tenant=tenant, category=asset_category, project=project)
            Attachment.all_objects.create(
                tenant=tenant,
                asset=asset,
                kind="photo",
                storage_key="attachments/x/1/missing.png",
                filename="missing.png",
                content_type="image/png",
                size=10,
            )
            data = resolve_project_report_data(_reload_project(project))

        assert data.assets[0].photo_data_uri is None


def _reload_project(project):
    """`ProjectFactory` creates through `.all_objects` outside of
    `tenant_context` (module docstring, `apps.common.tests.factories`); a few
    tests above need the SAME row fetched through the tenant-scoped manager
    once inside `tenant_context`, matching how `apps.projects.tasks.
    generate_project_report_pdf` actually looks the project up.
    """
    from apps.projects.models import Project

    return Project.objects.get(pk=project.pk)


class TestReportGenerateEndToEnd:
    def test_generate_then_poll_produces_a_stored_non_empty_pdf(
        self, client, settings, tmp_path, django_capture_on_commit_callbacks
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant, name="Grant Alpha", budget_total="1000.00")
        # "Equipment" is already seeded per-tenant by
        # `apps.projects.signals.seed_expense_categories` -- reuse it rather
        # than colliding on `uniq_expense_category_tenant_name`.
        from apps.projects.models import ExpenseCategory

        category = ExpenseCategory.all_objects.get(tenant=tenant, name="Equipment")
        ExpenseFactory(tenant=tenant, project=project, category=category, amount="100.00")

        _login(client, tenant, admin)

        with django_capture_on_commit_callbacks(execute=True):
            response = _generate(client, project.id)
        assert response.status_code == 202, response.content
        job_id = response.json()["id"]

        poll = client.get(f"/api/v1/jobs/{job_id}")
        assert poll.status_code == 200, poll.content
        body = poll.json()
        assert body["status"] == "succeeded", body
        assert body["download_url"]

        storage_key = body["download_url"].removeprefix("/media/")
        pdf_path = tmp_path / storage_key
        assert pdf_path.exists()
        pdf_bytes = pdf_path.read_bytes()
        assert len(pdf_bytes) > 0
        assert pdf_bytes.startswith(b"%PDF")

        with tenant_context(tenant.id):
            job = Job.objects.get(pk=job_id)
            assert job.created_by_id == admin.id
            assert job.params["project_id"] == project.id

    def test_no_budget_zero_expense_project_still_succeeds(
        self, client, settings, tmp_path, django_capture_on_commit_callbacks
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant, name="Bare Project")
        _login(client, tenant, admin)

        with django_capture_on_commit_callbacks(execute=True):
            response = _generate(client, project.id)
        assert response.status_code == 202, response.content
        job_id = response.json()["id"]

        poll = client.get(f"/api/v1/jobs/{job_id}").json()
        assert poll["status"] == "succeeded", poll
        storage_key = poll["download_url"].removeprefix("/media/")
        pdf_bytes = (tmp_path / storage_key).read_bytes()
        assert pdf_bytes.startswith(b"%PDF")


class TestReportGenerateRBAC:
    def test_member_without_expense_view_is_denied(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)  # default membership is ROLE_MEMBER
        project = ProjectFactory(tenant=tenant, budget_total="100.00")
        _login(client, tenant, member)

        response = _generate(client, project.id)
        assert response.status_code == 403

    def test_viewer_is_denied(self, client):
        tenant = TenantFactory()
        viewer = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(viewer, ROLE_VIEWER)
        project = ProjectFactory(tenant=tenant, budget_total="100.00")
        _login(client, tenant, viewer)

        response = _generate(client, project.id)
        assert response.status_code == 403

    def test_project_lead_of_a_different_project_is_denied(self, client):
        tenant = TenantFactory()
        project_a = ProjectFactory(tenant=tenant, name="A", budget_total="100.00")
        project_b = ProjectFactory(tenant=tenant, name="B", budget_total="200.00")

        lead = UserFactory(tenant=tenant)
        Membership.all_objects.filter(user=lead, project__isnull=True).delete()
        add_project_membership(lead, project_a, ROLE_PROJECT_LEAD)
        _login(client, tenant, lead)

        response = _generate(client, project_b.id)
        assert response.status_code == 403

    def test_project_lead_of_own_project_can_generate(
        self, client, settings, tmp_path, django_capture_on_commit_callbacks
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant, budget_total="100.00")

        lead = UserFactory(tenant=tenant)
        Membership.all_objects.filter(user=lead, project__isnull=True).delete()
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        _login(client, tenant, lead)

        with django_capture_on_commit_callbacks(execute=True):
            response = _generate(client, project.id)
        assert response.status_code == 202, response.content


class TestReportGenerateTenantIsolation:
    def test_cross_tenant_project_id_is_never_reachable(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        project_b = ProjectFactory(tenant=tenant_b, budget_total="100.00")

        _login(client, tenant_a, admin_a)
        response = _generate(client, project_b.id)
        assert response.status_code in (403, 404)
