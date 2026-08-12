"""Uploaded files require a session, and belong to exactly one tenant.

Before this, nginx served `/media/` off the volume with no check at all: the
exact URL was the only credential. Fine for an asset photo, poor for a bank
statement — which is what M8 started storing.

The security property under test is deliberately two-part: **logged in**, and
**a member of the tenant that owns the file**. The second half is the R4 rule
applied to bytes instead of rows, and is the one a plain `login_required`
decorator would have missed.
"""

from __future__ import annotations

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    TenantFactory,
    UserFactory,
)
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
def stored_file():
    """A file laid out the way every writer in this codebase lays them out:
    `<prefix>/<tenant_id>/<anchor_id>/<name>`."""
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        owner = UserFactory(tenant=tenant)
    key = f"purchase-attachments/{tenant.id}/1/statement.pdf"
    default_storage.save(key, ContentFile(b"%PDF-1.4 secret bank statement"))
    yield tenant, owner, key
    if default_storage.exists(key):
        default_storage.delete(key)


def test_anonymous_request_is_refused(client, stored_file):
    """The headline fix: holding the link is no longer enough."""
    _tenant, _owner, key = stored_file
    response = client.get(f"/media/{key}")
    # 404 rather than 403 — a stranger with a leaked link learns nothing about
    # whether it names a real file.
    assert response.status_code == 404


def test_owner_can_fetch_their_own_file(client, stored_file):
    tenant, owner, key = stored_file
    _login(client, tenant, owner)
    response = client.get(f"/media/{key}")
    assert response.status_code == 200, response.content


def test_another_tenants_user_is_refused(client, stored_file):
    """R4 for bytes: a valid session for the WRONG tenant must not open the
    file, even with the exact key."""
    _tenant, _owner, key = stored_file
    other_tenant = TenantFactory()
    with tenant_context(other_tenant.id):
        intruder = UserFactory(tenant=other_tenant)
    _login(client, other_tenant, intruder)

    response = client.get(f"/media/{key}")
    assert response.status_code == 404


def test_path_traversal_is_refused(client, stored_file):
    tenant, owner, _key = stored_file
    _login(client, tenant, owner)
    assert client.get("/media/../etc/passwd").status_code in (301, 400, 404)
    assert client.get(f"/media/purchase-attachments/{tenant.id}/../../secret").status_code == 404


def test_missing_file_is_a_404_not_a_500(client, stored_file):
    tenant, owner, _key = stored_file
    _login(client, tenant, owner)
    response = client.get(f"/media/purchase-attachments/{tenant.id}/1/nope.pdf")
    assert response.status_code == 404


def test_production_delegates_serving_to_nginx(client, stored_file, settings):
    """Django must NOT stream the bytes in production: it returns an
    `X-Accel-Redirect` and nginx serves the file, so a large download never
    occupies a gunicorn worker."""
    settings.DEBUG = False
    tenant, owner, key = stored_file
    _login(client, tenant, owner)

    response = client.get(f"/media/{key}")
    assert response.status_code == 200
    assert response["X-Accel-Redirect"] == f"/protected-media/{key}"
    assert response.content == b"", "the app streamed the file instead of delegating"


@pytest.mark.parametrize(
    "key",
    [
        "attachments/7/12/abc_photo.jpg",
        "expense-attachments/7/12/abc_invoice.pdf",
        "purchase-attachments/7/12/abc_receipt.pdf",
        "payment-attachments/7/12/abc_statement.pdf",
        "project-documents/7/3/abc_proposal.pdf",
        "project-reports/7/some-uuid.pdf",
        "project-archives/7/some-uuid.zip",
        "labels/7/some-uuid.pdf",
        "imports/7/44/rows.csv",
        "tenant-logos/7/abc_logo.png",
    ],
)
def test_every_storage_key_layout_carries_its_tenant(key):
    """The authorization check reads the tenant id out of the storage key
    itself, so a future key layout that omits it would make those files
    permanently un-fetchable (a silent 404, not a loud error).

    This lists every key builder in the codebase; add a row when you add one.
    """
    from apps.common.media import _TENANT_SCOPED_KEY

    match = _TENANT_SCOPED_KEY.match(key)
    assert match is not None, f"{key!r} has no tenant segment — it would 404 for everyone"
    assert match.group("tenant_id") == "7"
