"""The expense pack PDF (M8 Phase 3 §6.1).

The artifact the whole milestone was requested for: one file that proves one
line on a bank statement, end to end — the statement itself, every receipt, the
items, and a photo of each thing bought.

These tests render real PDFs (WeasyPrint + fitz), so they are slower than the
rest of the finance suite and deliberately few. They cover what would actually
go wrong: the scans not making it in, page numbering not covering the appended
pages, a missing file taking the whole job down, and the job contract itself.
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
from apps.finance.models import Payment
from apps.finance.pack import render_order_pack_html, render_order_pack_pdf
from apps.finance.services import resolve_order_pack_data
from apps.jobs.models import Job
from apps.rbac.permission_keys import ROLE_ADMIN
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content


def _real_pdf_bytes(text: str) -> bytes:
    """A genuine one-page PDF.

    The merge step is best-effort by design: an unreadable file is skipped
    rather than failing the job. So a fixture using fake bytes silently tests
    nothing — the pages would just never be appended. Build a real one.
    """
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text(fitz.Point(72, 72), text, fontsize=12)
    raw = doc.tobytes()
    doc.close()
    return raw


@pytest.fixture
def order_with_paperwork(client):
    """A split order with a statement, two receipt scans, and a linked asset —
    i.e. everything the pack is supposed to assemble."""
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        category = CategoryFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=category, project=project, name="RTX 6000")
    _login(client, tenant, admin)

    created = client.post(
        "/api/v1/orders/",
        {
            "project": project.id,
            "vendor": "Amazon",
            "paid_on": "2026-03-02",
            "amount": "1234.56",
            "currency": "SAR",
            "account_label": "Visa 4321",
            "statement_ref": "STMT-77",
            "splits": [
                {
                    "receipt_number": "SHIP-1",
                    "items": [
                        {"description": "GPU", "amount": "400.00", "asset": asset.id},
                    ],
                },
                {
                    "receipt_number": "SHIP-2",
                    "items": [{"description": "Jetson", "amount": "834.56"}],
                },
            ],
        },
        content_type="application/json",
    ).json()

    client.post(
        f"/api/v1/payments/{created['id']}/attachment/",
        {"file": SimpleUploadedFile("stmt.pdf", _real_pdf_bytes("STATEMENT"), "application/pdf")},
    )
    for split in created["splits"]:
        client.post(
            f"/api/v1/purchases/{split['id']}/attachment/",
            {
                "file": SimpleUploadedFile(
                    f"receipt-{split['id']}.pdf",
                    _real_pdf_bytes("RECEIPT"),
                    "application/pdf",
                )
            },
        )

    return tenant, admin, project, created


def test_pack_html_carries_the_audit_facts(order_with_paperwork):
    """The typeset pages must state what an auditor matches against: the
    charge, its reference, every shipment, every item and its asset."""
    tenant, admin, _project, created = order_with_paperwork
    with tenant_context(tenant.id):
        order = Payment.objects.get(pk=created["id"])
        data = resolve_order_pack_data(order, generated_by=admin.email)

    html = render_order_pack_html(data)

    assert "Amazon" in html
    assert "STMT-77" in html
    assert "Visa 4321" in html
    assert "SAR 1,234.56" in html
    assert "Shipment 1 of 2" in html and "Shipment 2 of 2" in html
    assert "GPU" in html and "Jetson" in html
    assert "RTX 6000" in html, "the item's asset is missing from the pack"
    assert "fully itemized" in html, "the order balances but the summary says otherwise"


def test_pack_pdf_appends_every_scan_and_numbers_the_pages(order_with_paperwork):
    """The scans are the point: a pack without them is just a screenshot of
    the ledger. Page numbering must cover the APPENDED pages too — that is what
    makes 'receipt at page 7' resolve."""
    import fitz

    tenant, _admin, _project, created = order_with_paperwork
    with tenant_context(tenant.id):
        order = Payment.objects.get(pk=created["id"])
        data = resolve_order_pack_data(order)
        assert len(data.scan_files) == 3, "statement + two receipts should be collected"
        pdf_bytes = render_order_pack_pdf(data)

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        # Typeset page(s) + a divider and a page for each of the three scans.
        assert doc.page_count >= 7
        text = "".join(page.get_text() for page in doc)
        assert "Bank statement" in text
        assert "receipt" in text.lower()
        assert f"Page 1 of {doc.page_count}" in text
        assert f"Page {doc.page_count} of {doc.page_count}" in text
    finally:
        doc.close()


def test_a_missing_scan_does_not_fail_the_pack(order_with_paperwork):
    """One file gone from the volume must degrade the pack, never destroy it —
    the same best-effort posture as every other document helper."""
    from django.core.files.storage import default_storage

    tenant, _admin, _project, created = order_with_paperwork
    with tenant_context(tenant.id):
        order = Payment.objects.get(pk=created["id"])
        for attachment in order.attachments.all():
            default_storage.delete(attachment.storage_key)

        data = resolve_order_pack_data(order)
        # The statement is gone; the two receipts still made it.
        assert len(data.scan_files) == 2
        assert render_order_pack_pdf(data), "the pack should still render"


def test_pack_endpoint_enqueues_a_job(client, order_with_paperwork):
    tenant, _admin, _project, created = order_with_paperwork

    response = client.post(f"/api/v1/orders/{created['id']}/pack/")
    assert response.status_code == 202, response.content
    body = response.json()
    assert body["job_type"] == "expense_pack_pdf"

    with tenant_context(tenant.id):
        job = Job.objects.get(pk=body["id"])
        assert job.params["order_id"] == created["id"]


def test_pack_job_produces_a_downloadable_pdf(client, order_with_paperwork):
    """End to end through the real task (eager in tests): the job lands
    SUCCEEDED with a stored PDF the client can download."""
    from apps.finance.tasks import generate_order_pack_pdf

    tenant, _admin, _project, created = order_with_paperwork
    response = client.post(f"/api/v1/orders/{created['id']}/pack/")
    job_id = response.json()["id"]

    generate_order_pack_pdf(job_id=str(job_id), tenant_id=tenant.id, order_id=created["id"])

    with tenant_context(tenant.id):
        job = Job.objects.get(pk=job_id)
        assert job.status == Job.Status.SUCCEEDED, job.error
        assert job.result_key.startswith(f"expense-packs/{tenant.id}/")
        assert job.result_content_type == "application/pdf"

    polled = client.get(f"/api/v1/jobs/{job_id}")
    assert polled.status_code == 200, polled.content
    assert polled.json()["download_url"].startswith("/media/expense-packs/")


def test_pack_is_tenant_isolated(client, order_with_paperwork):
    tenant, _admin, _project, created = order_with_paperwork
    other = TenantFactory()
    with tenant_context(other.id):
        intruder = UserFactory(tenant=other)
        upgrade_tenant_wide_role(intruder, ROLE_ADMIN)
    _login(client, other, intruder)

    assert client.post(f"/api/v1/orders/{created['id']}/pack/").status_code == 404


def test_foreign_currency_pack_shows_both_amounts(client):
    """Original first, converted in parentheses with the rate — the auditor is
    holding the USD receipt, not the SAR statement."""
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
    _login(client, tenant, admin)

    created = client.post(
        "/api/v1/orders/",
        {
            "project": project.id,
            "vendor": "Amazon",
            "paid_on": "2026-03-02",
            "amount": "1500.00",
            "currency": "SAR",
            "splits": [
                {
                    "currency": "USD",
                    "total": "400.00",
                    "items": [{"description": "GPU", "amount": "400.00"}],
                }
            ],
        },
        content_type="application/json",
    ).json()

    with tenant_context(tenant.id):
        order = Payment.objects.get(pk=created["id"])
        html = render_order_pack_html(resolve_order_pack_data(order))

    assert "USD 400.00" in html
    assert "SAR 1,500.00" in html
    assert "@ 3.75" in html


def test_unbalanced_order_says_so_rather_than_hiding_it(client):
    """Warn, never block — but the pack must not quietly present an
    unaccounted-for order as if it reconciled."""
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
    _login(client, tenant, admin)

    created = client.post(
        "/api/v1/orders/",
        {
            "project": project.id,
            "vendor": "Acme",
            "paid_on": "2026-03-02",
            "amount": "500.00",
            "currency": "SAR",
            "splits": [{"items": [{"description": "Thing", "amount": "300.00"}]}],
        },
        content_type="application/json",
    ).json()

    with tenant_context(tenant.id):
        order = Payment.objects.get(pk=created["id"])
        data = resolve_order_pack_data(order)

    assert data.is_balanced is False
    assert "unaccounted" in data.variance_note
    assert Decimal("200.00") == Decimal(data.variance_note.split()[1].replace(",", ""))
