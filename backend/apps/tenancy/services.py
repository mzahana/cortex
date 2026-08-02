"""Tenant branding (logo) upload helpers.

Deliberately self-contained rather than importing
`apps.assets.services.validate_attachment_upload`/`save_attachment_file`:
`assets` depends on `tenancy` (every tenant-owned model inherits
`TenantScopedModel`), so importing the other way round would invert the app
dependency for the sake of ~30 lines. The security posture is the same one
those helpers established (and is intentionally *stricter* here):

- a default-deny content-type allowlist that never includes SVG/HTML — an
  inline-rendered `.svg` is a same-origin stored-XSS vector, and a logo is
  the one attachment the app renders with `<img>` on EVERY screen;
- the filename extension must match the declared content-type;
- a small size cap (a logo is chrome, not an attachment): 2 MB;
- validation runs BEFORE a single byte reaches the storage backend.

nginx serving `/media/` with `Content-Disposition: attachment` +
`Content-Security-Policy: sandbox` (see `docker/nginx/default.conf`) remains
the second layer; `<img src="/media/...">` still renders normally under it.
"""

from __future__ import annotations

import uuid

from django.core.files.storage import default_storage
from django.utils import timezone
from rest_framework import serializers

from .models import Tenant

MAX_LOGO_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MB

LOGO_CONTENT_TYPES: dict[str, frozenset[str]] = {
    "image/png": frozenset({"png"}),
    "image/jpeg": frozenset({"jpg", "jpeg"}),
    "image/webp": frozenset({"webp"}),
}


def validate_logo_upload(uploaded_file) -> str:
    """Reject (via `serializers.ValidationError` -> RFC-7807 400) anything
    that is not a small PNG/JPEG/WebP, and return the normalized
    content-type. Raises before any bytes are written."""
    size = getattr(uploaded_file, "size", None)
    if size is not None and size > MAX_LOGO_UPLOAD_BYTES:
        raise serializers.ValidationError(
            {
                "file": (
                    f"Logo is too large ({size} bytes); the maximum is "
                    f"{MAX_LOGO_UPLOAD_BYTES} bytes."
                )
            }
        )

    content_type = (getattr(uploaded_file, "content_type", "") or "").split(";")[0].strip().lower()
    if content_type not in LOGO_CONTENT_TYPES:
        raise serializers.ValidationError(
            {"file": "Logo must be a PNG, JPEG, or WebP image."}
        )

    name = getattr(uploaded_file, "name", "") or ""
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension not in LOGO_CONTENT_TYPES[content_type]:
        raise serializers.ValidationError(
            {"file": f"Filename extension '.{extension}' does not match {content_type}."}
        )

    return content_type


def tenant_logo_upload_path(tenant_id: int, filename: str) -> str:
    """`tenant-logos/{tenant_id}/{uuid}_{safe_name}` — its own prefix, so a
    logo can never collide with an asset attachment / project document (same
    convention as `apps.assets.services.attachment_upload_path`)."""
    safe_name = filename.replace("/", "_").replace("\\", "_")
    return f"tenant-logos/{tenant_id}/{uuid.uuid4().hex}_{safe_name}"


def save_tenant_logo(*, tenant: Tenant, uploaded_file) -> Tenant:
    """Validate, store the bytes, point `tenant` at the new key, and delete
    the previous logo's bytes (a tenant has exactly one logo — leaving the
    old file behind would leak disk on every re-upload). Returns the saved
    tenant.
    """
    content_type = validate_logo_upload(uploaded_file)
    previous_key = tenant.logo_storage_key

    key = tenant_logo_upload_path(tenant.id, uploaded_file.name)
    storage_key = default_storage.save(key, uploaded_file)

    tenant.logo_storage_key = storage_key
    tenant.logo_filename = uploaded_file.name
    tenant.logo_content_type = content_type
    tenant.logo_updated_at = timezone.now()
    tenant.save(
        update_fields=[
            "logo_storage_key",
            "logo_filename",
            "logo_content_type",
            "logo_updated_at",
        ]
    )

    _delete_stored_file(previous_key)
    return tenant


def clear_tenant_logo(*, tenant: Tenant) -> Tenant:
    """Remove the tenant's logo (row fields + the stored bytes). Idempotent:
    a tenant with no logo is left untouched."""
    previous_key = tenant.logo_storage_key
    tenant.logo_storage_key = ""
    tenant.logo_filename = ""
    tenant.logo_content_type = ""
    tenant.logo_updated_at = timezone.now()
    tenant.save(
        update_fields=[
            "logo_storage_key",
            "logo_filename",
            "logo_content_type",
            "logo_updated_at",
        ]
    )
    _delete_stored_file(previous_key)
    return tenant


def _delete_stored_file(storage_key: str) -> None:
    """Best-effort byte cleanup. A storage backend that has already lost the
    file (manual cleanup, restored volume) must not turn a successful DB
    update into a 500 — the row is the source of truth, same posture as
    `apps.projects.api`'s attachment delete."""
    if not storage_key:
        return
    try:
        default_storage.delete(storage_key)
    except Exception:  # pragma: no cover - defensive
        pass


def tenant_logo_url(tenant: Tenant) -> str | None:
    """Public URL the SPA renders in an `<img>`, or `None` when no logo is
    set. `/media/<key>` is served directly by nginx — the same trust model
    `Attachment.storage_key` already uses (a logo is not secret; it is shown
    on every screen of the app to every member of the tenant)."""
    if not tenant.logo_storage_key:
        return None
    return default_storage.url(tenant.logo_storage_key)
