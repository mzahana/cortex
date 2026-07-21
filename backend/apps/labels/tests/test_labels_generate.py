"""T4.5 — `POST /api/v1/labels/generate` end to end: successful generation
(job succeeds, PDF bytes non-empty, every embedded QR decodes back to the
EXACT asset it was printed for — the real F6/F7 round-trip, not just an
encode-side assertion), `label.generate` RBAC enforcement (Admin/ProjectLead/
Member/Viewer), and cross-tenant asset-id exclusion.

QR decode uses `pymupdf` (rasterize the rendered PDF page) + the `zbarimg`
CLI (decode the rasterized page) — see `docker/Dockerfile`'s `zbar` package
comment. `zbarimg` was chosen over a pip-only decoder (e.g.
`opencv-python-headless`) because OpenCV ships no musllinux wheel, so it
can't install in the Alpine-based app image at all; `zbar`/`zbarimg` is a
tiny Alpine package with no such gap, and shelling out avoids `pyzbar`'s
`ctypes.util.find_library` failing to locate `libzbar.so` under musl.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import fitz
import pytest
from PIL import Image

from apps.assets.models import Asset
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    CategoryFactory,
    LocationFactory,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    add_project_membership,
    upgrade_tenant_wide_role,
)
from apps.jobs.models import Job
from apps.labels.templates import SHEET_TEMPLATES
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


def _generate(client, asset_ids, template="avery_5160"):
    return client.post(
        "/api/v1/labels/generate",
        data=json.dumps({"asset_ids": asset_ids, "template": template}),
        content_type="application/json",
    )


QR_DECODE_DPI = 300


def _decode_qr_tokens_from_pdf(pdf_bytes: bytes, template, total_labels: int) -> list[str]:
    """Rasterize `pdf_bytes` and decode each label's QR ONE AT A TIME, by
    cropping exactly the label cell `template`'s own geometry says it's at
    (the SAME `cols`/`rows`/margin/gutter math `apps.labels.rendering.
    _label_position_css` uses to place it) and running `zbarimg --raw` on
    just that crop.

    Deliberately per-cell rather than one `zbarimg` call over the whole
    page: it keeps the same "a genuine encode/round-trip regression fails
    this specific label's decode, not a fuzzy multi-code page scan"
    property the earlier OpenCV-based version was built for.
    """
    if shutil.which("zbarimg") is None:
        pytest.skip("zbarimg not installed (see docker/Dockerfile's `zbar` package)")

    tokens: list[str] = []
    per_sheet = template.labels_per_sheet
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    remaining = total_labels
    with tempfile.TemporaryDirectory() as tmpdir:
        for page in doc:
            pix = page.get_pixmap(dpi=QR_DECODE_DPI)
            page_img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")

            labels_on_this_page = min(per_sheet, remaining)
            for i in range(labels_on_this_page):
                row, col = divmod(i, template.cols)
                left_in = template.margin_left_in + col * (
                    template.label_width_in + template.gutter_x_in
                )
                top_in = template.margin_top_in + row * (
                    template.label_height_in + template.gutter_y_in
                )
                x0 = int(left_in * QR_DECODE_DPI)
                y0 = int(top_in * QR_DECODE_DPI)
                x1 = int((left_in + template.label_width_in) * QR_DECODE_DPI)
                y1 = int((top_in + template.label_height_in) * QR_DECODE_DPI)

                crop_path = Path(tmpdir) / f"label_{row}_{col}.png"
                page_img.crop((x0, y0, x1, y1)).save(crop_path)

                result = subprocess.run(
                    ["zbarimg", "--raw", "-q", str(crop_path)],
                    capture_output=True,
                    text=True,
                )
                data = result.stdout.strip()
                if data:
                    tokens.append(data)
            remaining -= labels_on_this_page

    return tokens


class TestLabelGenerateSuccess:
    def test_generate_then_poll_then_pdf_qr_round_trips_to_exact_assets(
        self, client, settings, tmp_path, django_capture_on_commit_callbacks
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant, name="GPU")
        location = LocationFactory(tenant=tenant, name="Rack 3")

        with tenant_context(tenant.id):
            asset_one = Asset.all_objects.create(
                tenant=tenant, category=category, location=location, name="RTX Box A"
            )
            asset_two = Asset.all_objects.create(
                tenant=tenant, category=category, location=location, name="RTX Box B"
            )

        _login(client, tenant, admin)

        with django_capture_on_commit_callbacks(execute=True):
            response = _generate(client, [asset_one.id, asset_two.id])
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

        decoded_tokens = set(
            _decode_qr_tokens_from_pdf(pdf_bytes, SHEET_TEMPLATES["avery_5160"], 2)
        )
        assert decoded_tokens == {asset_one.qr_token, asset_two.qr_token}

        with tenant_context(tenant.id):
            job = Job.objects.get(pk=job_id)
            assert job.created_by_id == admin.id
            assert job.params["asset_ids"] == [asset_one.id, asset_two.id]

    def test_generate_paginates_across_multiple_sheets(
        self, client, settings, tmp_path, django_capture_on_commit_callbacks
    ):
        """`avery_5163` is 10/sheet — 15 assets must produce a 2-page PDF,
        every one of the 15 tokens still present and decodable."""
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant)

        with tenant_context(tenant.id):
            assets = [
                Asset.all_objects.create(tenant=tenant, category=category, name=f"Asset {i}")
                for i in range(15)
            ]

        _login(client, tenant, admin)
        with django_capture_on_commit_callbacks(execute=True):
            response = _generate(client, [a.id for a in assets], template="avery_5163")
        assert response.status_code == 202, response.content
        job_id = response.json()["id"]

        poll = client.get(f"/api/v1/jobs/{job_id}").json()
        assert poll["status"] == "succeeded", poll
        storage_key = poll["download_url"].removeprefix("/media/")
        pdf_bytes = (tmp_path / storage_key).read_bytes()

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        assert doc.page_count == 2

        decoded_tokens = set(
            _decode_qr_tokens_from_pdf(pdf_bytes, SHEET_TEMPLATES["avery_5163"], 15)
        )
        assert decoded_tokens == {a.qr_token for a in assets}


class TestLabelGenerateValidation:
    def test_unknown_template_key_is_rejected(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant)
        with tenant_context(tenant.id):
            asset = Asset.all_objects.create(tenant=tenant, category=category, name="X")
        _login(client, tenant, admin)

        response = _generate(client, [asset.id], template="avery_9999")
        assert response.status_code == 400

    def test_empty_asset_ids_is_rejected(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        response = _generate(client, [])
        assert response.status_code == 400


class TestLabelGenerateRBAC:
    def test_member_without_label_generate_is_denied(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)  # default membership is ROLE_MEMBER
        category = CategoryFactory(tenant=tenant)
        with tenant_context(tenant.id):
            asset = Asset.all_objects.create(tenant=tenant, category=category, name="X")
        _login(client, tenant, member)

        response = _generate(client, [asset.id])
        assert response.status_code == 403

    def test_viewer_is_denied(self, client):
        tenant = TenantFactory()
        viewer = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(viewer, ROLE_VIEWER)
        category = CategoryFactory(tenant=tenant)
        with tenant_context(tenant.id):
            asset = Asset.all_objects.create(tenant=tenant, category=category, name="X")
        _login(client, tenant, viewer)

        response = _generate(client, [asset.id])
        assert response.status_code == 403

    def test_project_lead_scoped_to_own_project_assets_only(
        self, client, settings, tmp_path, django_capture_on_commit_callbacks
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        category = CategoryFactory(tenant=tenant)
        project = ProjectFactory(tenant=tenant)
        other_project = ProjectFactory(tenant=tenant)

        lead = UserFactory(tenant=tenant)
        # Pure ProjectLead: remove the auto-assigned tenant-wide Member
        # membership so the ONLY grant is the project-scoped one below (same
        # "pure ProjectLead" setup `apps.stock.tests.test_stock_api` uses).
        Membership.all_objects.filter(user=lead, project__isnull=True).delete()
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)

        with tenant_context(tenant.id):
            own_asset = Asset.all_objects.create(
                tenant=tenant, category=category, project=project, name="Mine"
            )
            other_asset = Asset.all_objects.create(
                tenant=tenant, category=category, project=other_project, name="Not mine"
            )
            pool_asset = Asset.all_objects.create(
                tenant=tenant, category=category, name="General pool"
            )

        _login(client, tenant, lead)

        # Requesting a mix: only `own_asset` should ever end up in the job/PDF.
        with django_capture_on_commit_callbacks(execute=True):
            response = _generate(
                client, [own_asset.id, other_asset.id, pool_asset.id], template="avery_5160"
            )
        assert response.status_code == 202, response.content
        job_id = response.json()["id"]

        with tenant_context(tenant.id):
            job = Job.objects.get(pk=job_id)
            assert job.params["asset_ids"] == [own_asset.id]

        poll = client.get(f"/api/v1/jobs/{job_id}").json()
        assert poll["status"] == "succeeded", poll
        storage_key = poll["download_url"].removeprefix("/media/")
        pdf_bytes = (tmp_path / storage_key).read_bytes()
        decoded_tokens = set(
            _decode_qr_tokens_from_pdf(pdf_bytes, SHEET_TEMPLATES["avery_5160"], 1)
        )
        assert decoded_tokens == {own_asset.qr_token}
        assert other_asset.qr_token not in decoded_tokens
        assert pool_asset.qr_token not in decoded_tokens

    def test_project_lead_with_no_allowed_assets_gets_400(self, client):
        tenant = TenantFactory()
        category = CategoryFactory(tenant=tenant)
        project = ProjectFactory(tenant=tenant)
        other_project = ProjectFactory(tenant=tenant)

        lead = UserFactory(tenant=tenant)
        Membership.all_objects.filter(user=lead, project__isnull=True).delete()
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)

        with tenant_context(tenant.id):
            other_asset = Asset.all_objects.create(
                tenant=tenant, category=category, project=other_project, name="Not mine"
            )
        _login(client, tenant, lead)

        response = _generate(client, [other_asset.id])
        assert response.status_code == 400


class TestLabelGenerateTenantIsolation:
    def test_cross_tenant_asset_ids_are_dropped_never_in_pdf(
        self, client, settings, tmp_path, django_capture_on_commit_callbacks
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        category_a = CategoryFactory(tenant=tenant_a)

        with tenant_context(tenant_a.id):
            own_asset = Asset.all_objects.create(tenant=tenant_a, category=category_a, name="A")

        category_b = CategoryFactory(tenant=tenant_b)
        with tenant_context(tenant_b.id):
            foreign_asset = Asset.all_objects.create(tenant=tenant_b, category=category_b, name="B")

        _login(client, tenant_a, admin_a)
        with django_capture_on_commit_callbacks(execute=True):
            response = _generate(client, [own_asset.id, foreign_asset.id])
        assert response.status_code == 202, response.content
        job_id = response.json()["id"]

        with tenant_context(tenant_a.id):
            job = Job.objects.get(pk=job_id)
            assert job.params["asset_ids"] == [own_asset.id]

        poll = client.get(f"/api/v1/jobs/{job_id}").json()
        storage_key = poll["download_url"].removeprefix("/media/")
        pdf_bytes = (tmp_path / storage_key).read_bytes()
        decoded_tokens = set(
            _decode_qr_tokens_from_pdf(pdf_bytes, SHEET_TEMPLATES["avery_5160"], 1)
        )
        assert decoded_tokens == {own_asset.qr_token}
        assert foreign_asset.qr_token not in decoded_tokens

    def test_only_own_tenant_ids_yields_400_when_all_foreign(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        category_b = CategoryFactory(tenant=tenant_b)
        with tenant_context(tenant_b.id):
            foreign_asset = Asset.all_objects.create(tenant=tenant_b, category=category_b, name="B")

        _login(client, tenant_a, admin_a)
        response = _generate(client, [foreign_asset.id])
        assert response.status_code == 400
