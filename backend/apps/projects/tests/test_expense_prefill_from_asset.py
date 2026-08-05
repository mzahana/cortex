"""Expense convenience: fetch the purchase facts (and the PO/invoice scan)
off a linked asset instead of re-keying them.

`GET /api/v1/assets/{id}/expense-prefill` is a pure read; copying a document
across is a separate, explicitly-authorized write
(`POST /api/v1/expenses/{id}/attachment-from-asset`). The security property
worth proving is the second one's DOUBLE gate: `expense.manage` on the
expense's project AND `asset.view` on the source asset's project — otherwise
a lead of project A could pull a document off project B's asset by id.
"""

from __future__ import annotations

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.assets.models import Attachment
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
from apps.projects.models import ExpenseAttachment
from apps.rbac.permission_keys import ROLE_ADMIN, ROLE_MEMBER, ROLE_PROJECT_LEAD
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


def _asset_doc(asset, *, filename="po-1234.pdf", content=b"PURCHASE-ORDER"):
    return Attachment.all_objects.create(
        tenant=asset.tenant,
        asset=asset,
        kind="doc",
        storage_key=default_storage.save(f"test-asset-docs/{filename}", ContentFile(content)),
        filename=filename,
        content_type="application/pdf",
        size=len(content),
    )


class TestPrefillRead:
    def test_returns_the_assets_purchase_facts_in_expense_vocabulary(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant)
        project = ProjectFactory(tenant=tenant)
        asset = AssetFactory(
            tenant=tenant,
            category=category,
            project=project,
            name="RTX 4090",
            purchase_cost="1899.00",
            currency="USD",
            purchase_date="2026-01-15",
            supplier="ACME Compute",
        )
        _asset_doc(asset)
        _login(client, tenant, admin)

        response = client.get(f"/api/v1/assets/{asset.id}/expense-prefill/")

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["amount"] == "1899.00"
        assert body["currency"] == "USD"
        assert body["date"] == "2026-01-15"
        assert body["vendor"] == "ACME Compute"
        assert body["description"] == "RTX 4090"
        assert [doc["filename"] for doc in body["documents"]] == ["po-1234.pdf"]

    def test_photos_are_offered_too_ranked_below_financial_documents(self, client):
        """Deliberate reversal of this endpoint's first cut, which filtered on
        `kind="doc"`: an invoice photographed with a phone is `kind="photo"`,
        so that filter hid the single most common case. Every attachment is a
        candidate now; `doc_type` does the ranking instead (see
        `TestDocTypeAndPrefillOrdering`)."""
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=category)
        Attachment.all_objects.create(
            tenant=tenant,
            asset=asset,
            kind="photo",
            storage_key="test/photo.jpg",
            filename="photo.jpg",
            content_type="image/jpeg",
            size=1,
        )
        invoice = Attachment.all_objects.create(
            tenant=tenant,
            asset=asset,
            kind="doc",
            doc_type="invoice",
            storage_key="test/inv.pdf",
            filename="inv.pdf",
            content_type="application/pdf",
            size=1,
        )
        _login(client, tenant, admin)

        response = client.get(f"/api/v1/assets/{asset.id}/expense-prefill/")

        documents = response.json()["documents"]
        assert [d["filename"] for d in documents] == ["inv.pdf", "photo.jpg"]
        assert documents[0]["id"] == invoice.id
        assert documents[1]["is_financial"] is False

    def test_requires_asset_view(self, client):
        tenant = TenantFactory()
        category = CategoryFactory(tenant=tenant)
        other_project = ProjectFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=category, project=other_project)
        my_project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        # Pure ProjectLead of a DIFFERENT project — no tenant-wide asset.view.
        Attachment.all_objects.filter(asset=asset).delete()
        add_project_membership(lead, my_project, ROLE_PROJECT_LEAD)
        with tenant_context(tenant.id):
            lead.memberships.filter(project__isnull=True).delete()
        _login(client, tenant, lead)

        response = client.get(f"/api/v1/assets/{asset.id}/expense-prefill/")

        assert response.status_code in (403, 404), response.content

    def test_cross_tenant_asset_id_is_not_reachable(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        foreign_asset = AssetFactory(tenant=tenant_b, category=CategoryFactory(tenant=tenant_b))
        _login(client, tenant_a, admin_a)

        response = client.get(f"/api/v1/assets/{foreign_asset.id}/expense-prefill/")

        assert response.status_code == 404, response.content


class TestCopyAttachmentToExpense:
    def test_copies_the_document_onto_the_expense(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant)
        project = ProjectFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=category, project=project)
        source = _asset_doc(asset, content=b"THE-INVOICE-BYTES")
        expense = ExpenseFactory(tenant=tenant, project=project, asset=asset)
        _login(client, tenant, admin)

        response = client.post(
            f"/api/v1/expenses/{expense.id}/attachment-from-asset/",
            {"attachment": source.id},
            content_type="application/json",
        )

        assert response.status_code == 201, response.content
        copy = ExpenseAttachment.all_objects.get(pk=response.json()["id"])
        assert copy.expense_id == expense.id
        assert copy.filename == source.filename
        # A real copy with its OWN storage object: deleting the asset's file
        # must never punch a hole in the financial record.
        assert copy.storage_key != source.storage_key
        with default_storage.open(copy.storage_key, "rb") as fh:
            assert fh.read() == b"THE-INVOICE-BYTES"

    def test_missing_attachment_id_is_a_400(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        expense = ExpenseFactory(tenant=tenant, project=project)
        _login(client, tenant, admin)

        response = client.post(
            f"/api/v1/expenses/{expense.id}/attachment-from-asset/",
            {},
            content_type="application/json",
        )

        assert response.status_code == 400, response.content

    def test_source_asset_outside_the_callers_scope_is_a_404(self, client):
        """The double gate: holding `expense.manage` on THIS project must not
        be enough to read a document off an asset in another project."""
        tenant = TenantFactory()
        category = CategoryFactory(tenant=tenant)
        mine = ProjectFactory(tenant=tenant)
        theirs = ProjectFactory(tenant=tenant)
        their_asset = AssetFactory(tenant=tenant, category=category, project=theirs)
        source = _asset_doc(their_asset)
        my_expense = ExpenseFactory(tenant=tenant, project=mine)

        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, mine, ROLE_PROJECT_LEAD)
        # Drop the auto-created tenant-wide Member membership, which would
        # otherwise grant `asset.view` across the whole tenant and mask the
        # very check under test.
        with tenant_context(tenant.id):
            lead.memberships.filter(project__isnull=True).delete()
        _login(client, tenant, lead)

        response = client.post(
            f"/api/v1/expenses/{my_expense.id}/attachment-from-asset/",
            {"attachment": source.id},
            content_type="application/json",
        )

        assert response.status_code == 404, response.content
        assert not ExpenseAttachment.all_objects.filter(expense=my_expense).exists()

    def test_cross_tenant_attachment_id_is_a_404(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        project_a = ProjectFactory(tenant=tenant_a)
        expense_a = ExpenseFactory(tenant=tenant_a, project=project_a)
        foreign_asset = AssetFactory(tenant=tenant_b, category=CategoryFactory(tenant=tenant_b))
        foreign_doc = _asset_doc(foreign_asset, filename="secret.pdf")
        _login(client, tenant_a, admin_a)

        response = client.post(
            f"/api/v1/expenses/{expense_a.id}/attachment-from-asset/",
            {"attachment": foreign_doc.id},
            content_type="application/json",
        )

        assert response.status_code == 404, response.content
        assert not ExpenseAttachment.all_objects.filter(expense=expense_a).exists()

    def test_plain_member_cannot_copy(self, client):
        tenant = TenantFactory()
        category = CategoryFactory(tenant=tenant)
        project = ProjectFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=category, project=project)
        source = _asset_doc(asset)
        expense = ExpenseFactory(tenant=tenant, project=project)
        member = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(member, ROLE_MEMBER)
        _login(client, tenant, member)

        response = client.post(
            f"/api/v1/expenses/{expense.id}/attachment-from-asset/",
            {"attachment": source.id},
            content_type="application/json",
        )

        assert response.status_code in (403, 404), response.content


class TestDocTypeAndPrefillOrdering:
    """The bug this class pins: an invoice photographed with a phone is
    `kind="photo"`, and the prefill originally filtered on `kind="doc"` — so
    the single most common way an invoice reaches an asset was invisible to
    "fetch from asset". Candidates are now ranked by `doc_type` instead.
    """

    def _admin(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)
        return tenant, admin

    def test_a_photographed_invoice_is_offered(self, client):
        tenant, _ = self._admin(client)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        Attachment.all_objects.create(
            tenant=tenant,
            asset=asset,
            kind="photo",
            doc_type="invoice",
            storage_key="test/invoice.jpg",
            filename="invoice.jpg",
            content_type="image/jpeg",
            size=10,
        )

        response = client.get(f"/api/v1/assets/{asset.id}/expense-prefill/")

        documents = response.json()["documents"]
        assert [d["filename"] for d in documents] == ["invoice.jpg"]
        assert documents[0]["doc_type"] == "invoice"
        assert documents[0]["is_financial"] is True

    def test_financial_documents_rank_above_everything_else(self, client):
        tenant, _ = self._admin(client)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        for kind, doc_type, filename in [
            ("photo", "", "rig-photo.jpg"),
            ("doc", "manual", "datasheet.pdf"),
            ("doc", "purchase_order", "po.pdf"),
            ("photo", "invoice", "invoice.jpg"),
        ]:
            Attachment.all_objects.create(
                tenant=tenant,
                asset=asset,
                kind=kind,
                doc_type=doc_type,
                storage_key=f"test/{filename}",
                filename=filename,
                content_type="application/pdf" if kind == "doc" else "image/jpeg",
                size=1,
            )

        response = client.get(f"/api/v1/assets/{asset.id}/expense-prefill/")

        names = [d["filename"] for d in response.json()["documents"]]
        # invoice -> receipt -> purchase_order -> quote, then the rest.
        assert names[0] == "invoice.jpg"
        assert names[1] == "po.pdf"
        assert set(names[2:]) == {"datasheet.pdf", "rig-photo.jpg"}

    def test_upload_accepts_and_stores_doc_type(self, client):
        tenant, _ = self._admin(client)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        upload = SimpleUploadedFile("inv.pdf", b"%PDF-1.4 fake", content_type="application/pdf")

        response = client.post(
            f"/api/v1/assets/{asset.id}/attachments/",
            {"file": upload, "kind": "doc", "doc_type": "invoice"},
        )

        assert response.status_code == 201, response.content
        assert response.json()["doc_type"] == "invoice"

    def test_upload_rejects_an_unknown_doc_type(self, client):
        tenant, _ = self._admin(client)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        upload = SimpleUploadedFile("inv.pdf", b"%PDF-1.4 fake", content_type="application/pdf")

        response = client.post(
            f"/api/v1/assets/{asset.id}/attachments/",
            {"file": upload, "kind": "doc", "doc_type": "tax-return"},
        )

        assert response.status_code == 400, response.content

    def test_untagged_attachments_are_still_offered(self, client):
        """Legacy rows (and anything nobody classified) keep working — they
        just rank below the tagged financial ones."""
        tenant, _ = self._admin(client)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        Attachment.all_objects.create(
            tenant=tenant,
            asset=asset,
            kind="doc",
            storage_key="test/legacy.pdf",
            filename="legacy.pdf",
            content_type="application/pdf",
            size=1,
        )

        response = client.get(f"/api/v1/assets/{asset.id}/expense-prefill/")

        assert [d["filename"] for d in response.json()["documents"]] == ["legacy.pdf"]
