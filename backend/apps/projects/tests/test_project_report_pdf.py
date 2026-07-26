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


class TestIncludeInvoiceScans:
    """Opt-in `include_invoice_scans` on `resolve_project_report_data` --
    invoice/receipt `ExpenseAttachment`s get a best-effort rasterized preview
    data URI (images inlined directly, PDFs rasterized to a first-page PNG),
    mirroring `_asset_photo_data_uri`'s "swallow all failures" posture. The
    default (`False`) must do NO extra storage reads/rasterization and leave
    the appendix HTML byte-for-byte unchanged from before this option
    existed.
    """

    def _make_expense_with_attachment(self, tenant, project, *, content_type, raw: bytes):
        from apps.projects.models import ExpenseAttachment

        expense = ExpenseFactory(tenant=tenant, project=project, amount="42.00")
        attachment = ExpenseAttachment.all_objects.create(
            tenant=tenant,
            expense=expense,
            storage_key=f"expense-attachments/x/1/scan-{content_type.replace('/', '-')}",
            filename="scan",
            content_type=content_type,
            size=len(raw),
        )
        from django.core.files.storage import default_storage

        default_storage.save(attachment.storage_key, io.BytesIO(raw))
        return expense, attachment

    @staticmethod
    def _make_png_bytes(*, width=40, height=30, color=(200, 30, 30)):
        import io as _io

        from PIL import Image

        img = Image.new("RGB", (width, height), color=color)
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_default_false_computes_no_data_uris(self, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        with tenant_context(tenant.id):
            self._make_expense_with_attachment(
                tenant, project, content_type="image/png", raw=self._make_png_bytes()
            )
            data = resolve_project_report_data(_reload_project(project))

        assert len(data.invoices) == 1
        assert data.invoices[0].scan_data_uri is None
        html = render_project_report_html(data)
        assert "<img" not in html.split("Documents &amp; invoice scans")[1]

    def test_true_with_image_attachment_embeds_a_downscaled_data_uri(self, settings, tmp_path):
        """Code-review fix: embedded scans go through `_resize_and_encode_png`
        (decode -> downscale -> re-encode as PNG) rather than being inlined
        byte-for-byte, so the URI is always `image/png` regardless of the
        original content-type."""
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        with tenant_context(tenant.id):
            self._make_expense_with_attachment(
                tenant, project, content_type="image/png", raw=self._make_png_bytes()
            )
            data = resolve_project_report_data(_reload_project(project), include_invoice_scans=True)

        assert len(data.invoices) == 1
        assert data.invoices[0].scan_data_uri is not None
        assert data.invoices[0].scan_data_uri.startswith("data:image/png;base64,")
        html = render_project_report_html(data)
        assert data.invoices[0].scan_data_uri in html
        # Rendered with the legible, full-size invoice-scan class, NOT the
        # tiny half-inch asset-inventory thumbnail class.
        assert '<img class="invoice-scan"' in html

    def test_scan_image_is_not_inside_a_table_cell(self, settings, tmp_path):
        """Regression test for the appendix-overflow bug: `<img
        class="invoice-scan">` must render inside a full-width
        `.invoice-entry` block, never sharing a `<table>` row with the
        expense-label/filename text columns -- that's what caused
        WeasyPrint's table auto-layout to expand the row past the page's
        content width in the first place."""
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        with tenant_context(tenant.id):
            self._make_expense_with_attachment(
                tenant, project, content_type="image/png", raw=self._make_png_bytes()
            )
            data = resolve_project_report_data(_reload_project(project), include_invoice_scans=True)

        html = render_project_report_html(data)
        appendix = html.split("Documents &amp; invoice scans")[1]
        assert '<div class="invoice-entry">' in appendix
        # No document rows in this fixture, so the ONLY table-cell markup
        # that could appear in the appendix would come from a regression
        # back to the old `<tr><td>{img}</td>...` layout.
        assert "<td>" not in appendix
        assert "<tr>" not in appendix

    def test_true_with_image_attachment_renders_within_page_bounds(self, settings, tmp_path):
        """Regression test for the appendix-overflow bug: renders an actual
        PDF (not just HTML/CSS) and inspects the rasterized page/image
        geometry via PyMuPDF, since the original bug was introduced by CSS
        that looked correct in isolation (`.invoice-scan { max-width: 6in
        }`) but broke once WeasyPrint's table auto-layout algorithm got hold
        of it -- asserting against HTML string content alone would not have
        caught that class of bug."""
        import fitz

        from apps.projects.report import render_project_report_pdf

        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        with tenant_context(tenant.id):
            # A wide image is the shape that triggered the original bug:
            # WeasyPrint's table auto-layout sized the image's column to its
            # up-to-6in max-width, and the row's demanded total width (image
            # column + two text columns) exceeded the 7.5in page content
            # area (letter minus 0.5in side margins).
            self._make_expense_with_attachment(
                tenant,
                project,
                content_type="image/png",
                raw=self._make_png_bytes(width=2000, height=400),
            )
            data = resolve_project_report_data(_reload_project(project), include_invoice_scans=True)

        assert data.invoices[0].scan_data_uri is not None
        pdf_bytes = render_project_report_pdf(data)

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            found_image = False
            for page in doc:
                page_width = page.rect.width
                for img_info in page.get_image_info():
                    found_image = True
                    x0, _y0, x1, _y1 = img_info["bbox"]
                    assert x0 >= -0.5, f"image starts left of the page: {img_info['bbox']}"
                    assert x1 <= page_width + 0.5, (
                        f"image right edge {x1} overflows page width {page_width}: "
                        f"{img_info['bbox']}"
                    )
            assert found_image, "expected the invoice scan image to be rendered on some page"
        finally:
            doc.close()

    def test_true_with_oversized_image_is_downscaled_to_the_dimension_cap(self, settings, tmp_path):
        """Code-review finding #2: a full-resolution upload must be capped,
        not inlined as-is, now that it renders large in the report."""
        import base64
        import io as _io

        from PIL import Image

        from apps.projects.services import _MAX_INVOICE_SCAN_DIMENSION_PX

        settings.MEDIA_ROOT = str(tmp_path)
        oversized = self._make_png_bytes(width=3000, height=2000, color=(10, 200, 10))
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        with tenant_context(tenant.id):
            self._make_expense_with_attachment(
                tenant, project, content_type="image/png", raw=oversized
            )
            data = resolve_project_report_data(_reload_project(project), include_invoice_scans=True)

        uri = data.invoices[0].scan_data_uri
        assert uri is not None
        encoded = uri.removeprefix("data:image/png;base64,")
        decoded_bytes = base64.b64decode(encoded)
        with Image.open(_io.BytesIO(decoded_bytes)) as decoded_img:
            # Source was 3000x2000 -- longest edge must be capped, and aspect
            # ratio preserved (`Image.thumbnail` behavior).
            assert max(decoded_img.size) <= _MAX_INVOICE_SCAN_DIMENSION_PX
            assert decoded_img.size[0] / decoded_img.size[1] == pytest.approx(3000 / 2000, rel=0.02)

    def test_true_with_pdf_attachment_rasterizes_first_page_to_png(self, settings, tmp_path):
        import fitz

        settings.MEDIA_ROOT = str(tmp_path)
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Invoice")
        pdf_bytes = doc.tobytes()
        doc.close()

        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        with tenant_context(tenant.id):
            self._make_expense_with_attachment(
                tenant, project, content_type="application/pdf", raw=pdf_bytes
            )
            data = resolve_project_report_data(_reload_project(project), include_invoice_scans=True)

        assert len(data.invoices) == 1
        assert data.invoices[0].scan_data_uri is not None
        assert data.invoices[0].scan_data_uri.startswith("data:image/png;base64,")

    def test_true_with_unsupported_content_type_falls_back_gracefully(self, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        with tenant_context(tenant.id):
            self._make_expense_with_attachment(
                tenant,
                project,
                content_type=(
                    "application/vnd.openxmlformats-officedocument" ".spreadsheetml.sheet"
                ),
                raw=b"not really an xlsx",
            )
            data = resolve_project_report_data(_reload_project(project), include_invoice_scans=True)

        assert len(data.invoices) == 1
        assert data.invoices[0].scan_data_uri is None
        # No crash, and the appendix still renders the filename-only row.
        html = render_project_report_html(data)
        assert data.invoices[0].filename in html

    def test_true_with_corrupt_or_missing_storage_file_swallows_and_returns_none(
        self, settings, tmp_path
    ):
        """Same defensive posture as `_asset_photo_data_uri`'s own
        `test_missing_photo_file_skips_gracefully` -- a dangling
        `ExpenseAttachment` row whose file was never actually written must
        not crash report generation."""
        settings.MEDIA_ROOT = str(tmp_path)
        from apps.projects.models import ExpenseAttachment

        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        with tenant_context(tenant.id):
            expense = ExpenseFactory(tenant=tenant, project=project, amount="10.00")
            ExpenseAttachment.all_objects.create(
                tenant=tenant,
                expense=expense,
                storage_key="expense-attachments/x/1/missing.pdf",
                filename="missing.pdf",
                content_type="application/pdf",
                size=10,
            )
            data = resolve_project_report_data(_reload_project(project), include_invoice_scans=True)

        assert len(data.invoices) == 1
        assert data.invoices[0].scan_data_uri is None

    def test_true_with_heic_attachment_embeds_a_data_uri_when_pillow_heif_available(
        self, settings, tmp_path
    ):
        """Code-review finding #3: `image/heic`/`image/heif` (iPhone camera
        default) is in `PHOTO_CONTENT_TYPES` but stock Pillow can't decode
        it. `pillow-heif` is installed (`requirements/base.txt`) and
        registers itself as a normal Pillow codec, so a real HEIC file
        should decode + downscale + embed exactly like any other image --
        NOT silently embed something broken."""
        import io as _io

        from apps.projects.services import _HEIF_AVAILABLE

        if not _HEIF_AVAILABLE:
            pytest.skip("pillow-heif not importable in this environment")

        from PIL import Image

        img = Image.new("RGB", (50, 40), color=(30, 60, 90))
        buf = _io.BytesIO()
        img.save(buf, format="HEIF")
        heic_bytes = buf.getvalue()

        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        with tenant_context(tenant.id):
            self._make_expense_with_attachment(
                tenant, project, content_type="image/heic", raw=heic_bytes
            )
            data = resolve_project_report_data(_reload_project(project), include_invoice_scans=True)

        assert len(data.invoices) == 1
        assert data.invoices[0].scan_data_uri is not None
        assert data.invoices[0].scan_data_uri.startswith("data:image/png;base64,")

    def test_true_with_heic_falls_back_to_none_when_pillow_heif_unavailable(
        self, settings, tmp_path, monkeypatch
    ):
        """When HEIF support isn't importable, HEIC must fall back to
        filename-only (`None`) rather than embedding unreadable bytes --
        the one-line content-type gate in `_invoice_scan_data_uri`."""
        import apps.projects.services as services_module

        monkeypatch.setattr(services_module, "_HEIF_AVAILABLE", False)

        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        with tenant_context(tenant.id):
            self._make_expense_with_attachment(
                tenant, project, content_type="image/heic", raw=b"not a real heic file"
            )
            data = resolve_project_report_data(_reload_project(project), include_invoice_scans=True)

        assert len(data.invoices) == 1
        assert data.invoices[0].scan_data_uri is None
        html = render_project_report_html(data)
        assert data.invoices[0].filename in html


class TestIncludeProjectDocuments:
    """Opt-in `include_project_documents` on `resolve_project_report_data` --
    unlike `include_invoice_scans` (an HTML-embedded thumbnail), this appends
    each `ProjectDocument`'s FULL PAGES onto the rendered report via a
    post-WeasyPrint fitz merge (`apps.projects.report.
    _append_project_documents`). Default (`False`) must add zero pages and
    do no extra storage reads.
    """

    def _make_document(self, tenant, project, *, kind, filename, content_type, raw: bytes):
        from django.core.files.storage import default_storage

        from apps.projects.models import ProjectDocument

        storage_key = f"project-documents/x/1/{filename}"
        document = ProjectDocument.all_objects.create(
            tenant=tenant,
            project=project,
            kind=kind,
            storage_key=storage_key,
            filename=filename,
            content_type=content_type,
            size=len(raw),
        )
        default_storage.save(storage_key, io.BytesIO(raw))
        return document

    @staticmethod
    def _make_pdf_bytes(*, page_count=1, text="Progress details"):
        import fitz

        doc = fitz.open()
        for _ in range(page_count):
            page = doc.new_page()
            page.insert_text((72, 72), text)
        pdf_bytes = doc.tobytes()
        doc.close()
        return pdf_bytes

    @staticmethod
    def _make_png_bytes(*, width=40, height=30, color=(60, 120, 200)):
        from PIL import Image

        img = Image.new("RGB", (width, height), color=color)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _page_count(self, pdf_bytes: bytes) -> int:
        import fitz

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            return doc.page_count
        finally:
            doc.close()

    def test_default_false_resolves_no_document_files_and_adds_no_pages(self, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        with tenant_context(tenant.id):
            self._make_document(
                tenant,
                project,
                kind="progress_report",
                filename="report.pdf",
                content_type="application/pdf",
                raw=self._make_pdf_bytes(page_count=3),
            )
            data = resolve_project_report_data(_reload_project(project))

        assert data.document_files == []
        from apps.projects.report import render_project_report_pdf

        with tenant_context(tenant.id):
            baseline_data = resolve_project_report_data(_reload_project(project))
        baseline_pages = self._page_count(render_project_report_pdf(baseline_data))
        flagged_pages = self._page_count(render_project_report_pdf(data))
        assert flagged_pages == baseline_pages

    def test_true_with_pdf_document_appends_a_divider_plus_source_pages(self, settings, tmp_path):
        from apps.projects.report import render_project_report_pdf

        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        with tenant_context(tenant.id):
            self._make_document(
                tenant,
                project,
                kind="progress_report",
                filename="q1-report.pdf",
                content_type="application/pdf",
                raw=self._make_pdf_bytes(page_count=3),
            )
            data_without = resolve_project_report_data(_reload_project(project))
            data_with = resolve_project_report_data(
                _reload_project(project), include_project_documents=True
            )

        assert len(data_with.document_files) == 1
        assert "Progress Report" in data_with.document_files[0].label
        assert "q1-report.pdf" in data_with.document_files[0].label

        pages_without = self._page_count(render_project_report_pdf(data_without))
        pdf_with_bytes = render_project_report_pdf(data_with)
        pages_with = self._page_count(pdf_with_bytes)
        # 1 divider page + 3 source pages appended.
        assert pages_with == pages_without + 4

        import fitz

        doc = fitz.open(stream=pdf_with_bytes, filetype="pdf")
        try:
            divider_page = doc.load_page(pages_without)
            divider_text = divider_page.get_text()
            assert "q1-report.pdf" in divider_text
            assert "Progress Report" in divider_text
            # Divider text must stay within the page bounds (CLAUDE.md:
            # confirm this doesn't repeat the just-fixed table-overflow bug).
            for block in divider_page.get_text("blocks"):
                x0, y0, x1, y1 = block[:4]
                assert x0 >= 0 and y0 >= 0
                assert x1 <= divider_page.rect.width
                assert y1 <= divider_page.rect.height

            source_page = doc.load_page(pages_without + 1)
            assert "Progress details" in source_page.get_text()
        finally:
            doc.close()

    def test_true_with_image_document_appends_a_divider_plus_one_page(self, settings, tmp_path):
        from apps.projects.report import render_project_report_pdf

        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        with tenant_context(tenant.id):
            self._make_document(
                tenant,
                project,
                kind="other",
                filename="site-photo.png",
                content_type="image/png",
                raw=self._make_png_bytes(),
            )
            data_without = resolve_project_report_data(_reload_project(project))
            data_with = resolve_project_report_data(
                _reload_project(project), include_project_documents=True
            )

        pages_without = self._page_count(render_project_report_pdf(data_without))
        pdf_with_bytes = render_project_report_pdf(data_with)
        pages_with = self._page_count(pdf_with_bytes)
        # 1 divider page + 1 image page appended.
        assert pages_with == pages_without + 2

        import fitz

        doc = fitz.open(stream=pdf_with_bytes, filetype="pdf")
        try:
            image_page = doc.load_page(pages_without + 1)
            assert len(image_page.get_images()) >= 1
        finally:
            doc.close()

    def test_true_with_image_document_is_downscaled_like_invoice_scans(self, settings, tmp_path):
        """Code-review finding #2: an oversized image document must be
        downscaled through the SAME `_resize_and_encode_png` helper (1600px
        longest-edge cap) the invoice-scan path already uses, not embedded
        at full resolution -- `_project_document_file` normalizes
        `content_type` to `image/png` once downscaled."""
        from apps.projects.services import _MAX_INVOICE_SCAN_DIMENSION_PX

        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        with tenant_context(tenant.id):
            self._make_document(
                tenant,
                project,
                kind="other",
                filename="huge-photo.png",
                content_type="image/png",
                raw=self._make_png_bytes(width=3000, height=2000),
            )
            data_with = resolve_project_report_data(
                _reload_project(project), include_project_documents=True
            )

        assert len(data_with.document_files) == 1
        document_file = data_with.document_files[0]
        assert document_file.content_type == "image/png"

        from PIL import Image

        with Image.open(io.BytesIO(document_file.raw_bytes)) as decoded:
            assert max(decoded.size) <= _MAX_INVOICE_SCAN_DIMENSION_PX

    def test_document_count_cap_stops_reading_further_documents(self, settings, tmp_path):
        """Code-review finding #1: `_MAX_APPENDED_DOCUMENT_COUNT` bounds how
        many documents are ever read into `document_files`, regardless of
        their individual size, so a project with a very large number of
        documents can't blow up worker memory."""
        from apps.projects.services import _MAX_APPENDED_DOCUMENT_COUNT

        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        extra = 5
        with tenant_context(tenant.id):
            for i in range(_MAX_APPENDED_DOCUMENT_COUNT + extra):
                self._make_document(
                    tenant,
                    project,
                    kind="other",
                    filename=f"doc-{i}.pdf",
                    content_type="application/pdf",
                    raw=self._make_pdf_bytes(page_count=1, text=f"doc {i}"),
                )
            data_with = resolve_project_report_data(
                _reload_project(project), include_project_documents=True
            )

        assert len(data_with.document_files) == _MAX_APPENDED_DOCUMENT_COUNT
        # The appendix table (unconditional) still lists every document by
        # filename/kind, even the ones the merge step never read.
        assert len(data_with.documents) == _MAX_APPENDED_DOCUMENT_COUNT + extra

    def test_aggregate_byte_budget_stops_reading_further_documents(self, settings, tmp_path):
        """Code-review finding #1: `_MAX_APPENDED_DOCUMENT_TOTAL_BYTES` is
        checked against each `ProjectDocument.size` (already-recorded
        upload-time metadata) BEFORE any storage I/O happens, so an
        over-budget document is never read into memory in the first place
        -- exercised here by recording an artificially large `size` on a
        tiny real file, so the test itself doesn't need to allocate 200MB
        to prove the cap fires."""
        from apps.projects.models import ProjectDocument
        from apps.projects.services import _MAX_APPENDED_DOCUMENT_TOTAL_BYTES

        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        with tenant_context(tenant.id):
            # First document's RECORDED size already consumes the entire
            # budget (real bytes on disk are tiny -- only `.size` matters
            # for the pre-read gate).
            first = self._make_document(
                tenant,
                project,
                kind="other",
                filename="big-recorded-size.pdf",
                content_type="application/pdf",
                raw=self._make_pdf_bytes(page_count=1),
            )
            ProjectDocument.all_objects.filter(pk=first.pk).update(
                size=_MAX_APPENDED_DOCUMENT_TOTAL_BYTES
            )
            # A second, later-created (so read first -- ordering is
            # `-created_at`) small document that would push the running
            # total over budget once the first is counted.
            self._make_document(
                tenant,
                project,
                kind="other",
                filename="second.pdf",
                content_type="application/pdf",
                raw=self._make_pdf_bytes(page_count=1),
            )

            data_with = resolve_project_report_data(
                _reload_project(project), include_project_documents=True
            )

        # `-created_at` ordering means "second.pdf" (created later) is read
        # FIRST; once its size is counted, the oversized-recorded-size
        # document no longer fits the remaining budget and is skipped.
        assert len(data_with.document_files) == 1
        assert data_with.document_files[0].label.endswith("second.pdf")
        # Still listed in the unconditional appendix table.
        assert len(data_with.documents) == 2

    def test_true_with_unsupported_document_adds_no_pages_but_stays_in_appendix(
        self, settings, tmp_path
    ):
        from apps.projects.report import render_project_report_pdf

        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        with tenant_context(tenant.id):
            self._make_document(
                tenant,
                project,
                kind="other",
                filename="notes.docx",
                content_type=(
                    "application/vnd.openxmlformats-officedocument"
                    ".wordprocessingml.document"
                ),
                raw=b"not really a docx",
            )
            data_without = resolve_project_report_data(_reload_project(project))
            data_with = resolve_project_report_data(
                _reload_project(project), include_project_documents=True
            )

        # The raw bytes are still resolved (content-type filtering happens
        # at append time, in `_append_one_document`, not at resolve time) --
        # but merging must skip an unsupported content-type entirely: no
        # divider, no pages.
        assert len(data_with.document_files) == 1
        pages_without = self._page_count(render_project_report_pdf(data_without))
        pages_with = self._page_count(render_project_report_pdf(data_with))
        assert pages_with == pages_without

        html = render_project_report_html(data_with)
        assert "notes.docx" in html

    def test_true_with_corrupt_pdf_document_swallows_and_adds_no_pages(self, settings, tmp_path):
        from apps.projects.report import render_project_report_pdf

        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        with tenant_context(tenant.id):
            self._make_document(
                tenant,
                project,
                kind="other",
                filename="corrupt.pdf",
                content_type="application/pdf",
                raw=b"this is not a real pdf file",
            )
            data_without = resolve_project_report_data(_reload_project(project))
            data_with = resolve_project_report_data(
                _reload_project(project), include_project_documents=True
            )

        pages_without = self._page_count(render_project_report_pdf(data_without))
        pages_with = self._page_count(render_project_report_pdf(data_with))
        assert pages_with == pages_without

    def test_true_with_missing_storage_file_swallows_and_resolves_no_document_files(
        self, settings, tmp_path
    ):
        """Same defensive posture as `_asset_photo_data_uri`'s
        `test_missing_photo_file_skips_gracefully` -- a dangling
        `ProjectDocument` row whose file was never actually written must not
        crash report generation."""
        from apps.projects.models import ProjectDocument

        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        with tenant_context(tenant.id):
            ProjectDocument.all_objects.create(
                tenant=tenant,
                project=project,
                kind="other",
                storage_key="project-documents/x/1/missing.pdf",
                filename="missing.pdf",
                content_type="application/pdf",
                size=10,
            )
            data = resolve_project_report_data(
                _reload_project(project), include_project_documents=True
            )

        assert data.document_files == []

    def test_page_count_safety_cap_skips_an_oversized_document(self, settings, tmp_path):
        """Judgment-call safety cap
        (`apps.projects.report._MAX_APPENDED_DOCUMENT_PAGES`): a document
        beyond the ceiling is skipped entirely (metadata-only, no pages
        appended) rather than merged in full."""
        from apps.projects.report import _MAX_APPENDED_DOCUMENT_PAGES, render_project_report_pdf

        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        with tenant_context(tenant.id):
            self._make_document(
                tenant,
                project,
                kind="other",
                filename="huge.pdf",
                content_type="application/pdf",
                raw=self._make_pdf_bytes(page_count=_MAX_APPENDED_DOCUMENT_PAGES + 1),
            )
            data_without = resolve_project_report_data(_reload_project(project))
            data_with = resolve_project_report_data(
                _reload_project(project), include_project_documents=True
            )

        assert len(data_with.document_files) == 1  # bytes were still read/resolved
        pages_without = self._page_count(render_project_report_pdf(data_without))
        pages_with = self._page_count(render_project_report_pdf(data_with))
        # Skipped, not merged: no divider, no source pages.
        assert pages_with == pages_without


class TestReportGenerateEndToEndIncludeProjectDocuments:
    def test_include_project_documents_request_body_is_threaded_through_and_appends_pages(
        self, client, settings, tmp_path, django_capture_on_commit_callbacks
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant, name="Grant With Docs", budget_total="1000.00")

        with tenant_context(tenant.id):
            import fitz
            from django.core.files.storage import default_storage

            from apps.projects.models import ProjectDocument

            doc = fitz.open()
            doc.new_page()
            doc.new_page()
            raw = doc.tobytes()
            doc.close()

            storage_key = "project-documents/x/1/progress.pdf"
            ProjectDocument.all_objects.create(
                tenant=tenant,
                project=project,
                kind="progress_report",
                storage_key=storage_key,
                filename="progress.pdf",
                content_type="application/pdf",
                size=len(raw),
            )
            default_storage.save(storage_key, io.BytesIO(raw))

        _login(client, tenant, admin)

        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                f"/api/v1/projects/{project.id}/report/",
                {"include_project_documents": True},
                content_type="application/json",
            )
        assert response.status_code == 202, response.content
        job_id = response.json()["id"]

        poll = client.get(f"/api/v1/jobs/{job_id}").json()
        assert poll["status"] == "succeeded", poll
        storage_key = poll["download_url"].removeprefix("/media/")
        pdf_bytes = (tmp_path / storage_key).read_bytes()
        assert pdf_bytes.startswith(b"%PDF")

        import fitz as _fitz

        merged = _fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            assert merged.page_count >= 3  # base report + divider + >=2 source pages
        finally:
            merged.close()

        with tenant_context(tenant.id):
            job = Job.objects.get(pk=job_id)
            assert job.params["include_project_documents"] is True

    def test_omitting_include_project_documents_defaults_to_false_in_job_params(
        self, client, settings, tmp_path, django_capture_on_commit_callbacks
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant, name="Grant No Docs", budget_total="1000.00")
        _login(client, tenant, admin)

        with django_capture_on_commit_callbacks(execute=True):
            response = _generate(client, project.id)
        assert response.status_code == 202, response.content
        job_id = response.json()["id"]

        with tenant_context(tenant.id):
            job = Job.objects.get(pk=job_id)
            assert job.params["include_project_documents"] is False


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

    def test_include_invoice_scans_request_body_is_threaded_through(
        self, client, settings, tmp_path, django_capture_on_commit_callbacks
    ):
        """`POST /api/v1/projects/{id}/report` with `{"include_invoice_scans":
        true}` -- the opt-in flag lands on `Job.params` and the report still
        succeeds end to end with an invoice scan attached."""
        import io as _io

        from apps.projects.models import ExpenseAttachment

        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant, name="Grant With Scans", budget_total="1000.00")
        with tenant_context(tenant.id):
            expense = ExpenseFactory(tenant=tenant, project=project, amount="50.00")
            attachment = ExpenseAttachment.all_objects.create(
                tenant=tenant,
                expense=expense,
                storage_key="expense-attachments/x/1/scan.png",
                filename="scan.png",
                content_type="image/png",
                size=8,
            )
            from django.core.files.storage import default_storage

            default_storage.save(attachment.storage_key, _io.BytesIO(b"\x89PNG\r\n\x1a\n"))

        _login(client, tenant, admin)

        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                f"/api/v1/projects/{project.id}/report/",
                {"include_invoice_scans": True},
                content_type="application/json",
            )
        assert response.status_code == 202, response.content
        job_id = response.json()["id"]

        poll = client.get(f"/api/v1/jobs/{job_id}").json()
        assert poll["status"] == "succeeded", poll
        storage_key = poll["download_url"].removeprefix("/media/")
        pdf_bytes = (tmp_path / storage_key).read_bytes()
        assert pdf_bytes.startswith(b"%PDF")

        with tenant_context(tenant.id):
            job = Job.objects.get(pk=job_id)
            assert job.params["include_invoice_scans"] is True

    def test_omitting_include_invoice_scans_defaults_to_false_in_job_params(
        self, client, settings, tmp_path, django_capture_on_commit_callbacks
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant, name="Grant No Scans", budget_total="1000.00")
        _login(client, tenant, admin)

        with django_capture_on_commit_callbacks(execute=True):
            response = _generate(client, project.id)
        assert response.status_code == 202, response.content
        job_id = response.json()["id"]

        with tenant_context(tenant.id):
            job = Job.objects.get(pk=job_id)
            assert job.params["include_invoice_scans"] is False


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
