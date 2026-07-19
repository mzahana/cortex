"""Business rules kept out of serializers/views (per CLAUDE.md: "keep
serializers thin; put business rules in services/model methods").

Two responsibilities (T1.2, core F2):

1. `validate_custom_field_values` — type-check/coerce a raw
   `{key: value}` dict against the target `Category`'s `CustomFieldDef` list
   (`text|int|float|bool|date|enum|json`, `required`, `enum_options`
   membership), returning ready-to-persist `{field_def: coerced_value}`
   pairs or raising a field-scoped `serializers.ValidationError` (-> clean
   RFC-7807 400, not a 500).
2. `save_attachment_file` — write the uploaded file's bytes to the storage
   backend (the mounted media volume via `django-storages`/Django's storage
   API, `config/settings/base.py` STORAGES) and return ONLY the resulting
   key + metadata; callers persist just that on `Attachment` — the bytes
   never touch the DB.
3. `validate_attachment_upload` — code-review hardening finding (T1.2
   follow-up): a size cap + a content-type/extension allowlist, checked
   BEFORE `save_attachment_file` ever writes bytes to the volume. This is
   defense-in-depth for DoS (oversize uploads) and stored-XSS (e.g. an
   `.svg`/`.html` "photo" served back same-origin); it does NOT by itself
   guarantee inline-rendering is blocked — that also depends on nginx
   serving `/media/` with `Content-Disposition: attachment` (queued for
   devops-engineer), which this allowlist backs up rather than replaces.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.conf import settings
from django.core.files.storage import default_storage
from rest_framework import serializers

from apps.catalog.models import CustomFieldDef

from .models import Asset, AssetFieldValue

# --- Attachment upload hardening (code-review finding) ----------------------

DEFAULT_MAX_ATTACHMENT_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB

# Deliberately conservative, default-deny allowlists — never executable/
# script/HTML/SVG content-types (SVG is a same-origin XSS vector when served
# inline). `kind="photo"` is images only; `kind="doc"` covers the common
# office/PDF/plain-text formats a lab would actually attach.
PHOTO_CONTENT_TYPES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
)
DOC_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/plain",
    }
)
ALLOWED_CONTENT_TYPES_BY_KIND: dict[str, frozenset[str]] = {
    "photo": PHOTO_CONTENT_TYPES,
    "doc": DOC_CONTENT_TYPES,
}

# Extension must match the declared content-type (rejects e.g. a `.html`
# file relabeled with an allowed content-type, or vice versa).
CONTENT_TYPE_EXTENSIONS: dict[str, frozenset[str]] = {
    "image/jpeg": frozenset({"jpg", "jpeg"}),
    "image/png": frozenset({"png"}),
    "image/webp": frozenset({"webp"}),
    "image/heic": frozenset({"heic"}),
    "image/heif": frozenset({"heif"}),
    "application/pdf": frozenset({"pdf"}),
    "application/msword": frozenset({"doc"}),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": frozenset({"docx"}),
    "application/vnd.ms-excel": frozenset({"xls"}),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": frozenset({"xlsx"}),
    "text/plain": frozenset({"txt"}),
}


def validate_attachment_upload(*, kind: str, uploaded_file) -> None:
    """Reject (via `serializers.ValidationError` -> clean RFC-7807 400)
    BEFORE any bytes are written to the storage backend:
    - larger than `settings.MAX_ATTACHMENT_UPLOAD_BYTES` (default
      `DEFAULT_MAX_ATTACHMENT_UPLOAD_BYTES`, 25 MB);
    - a content-type not on the `kind`-specific allowlist above;
    - a filename extension that doesn't match the declared content-type.

    `uploaded_file.size` is already known (Django's request parsing has
    fully received the multipart body by the time a view runs) without
    reading the file again, so the size check costs nothing extra; the
    real request-body-size ceiling (protecting against the upload itself
    exhausting memory/disk) belongs at nginx/`DATA_UPLOAD_MAX_MEMORY_SIZE`,
    not here — this is the second, defense-in-depth layer that also stops
    an oversize file from ever reaching `default_storage.save()`.
    """
    max_bytes = getattr(
        settings, "MAX_ATTACHMENT_UPLOAD_BYTES", DEFAULT_MAX_ATTACHMENT_UPLOAD_BYTES
    )
    size = getattr(uploaded_file, "size", None)
    if size is not None and size > max_bytes:
        raise serializers.ValidationError(
            {"file": (f"File is too large ({size} bytes); the maximum is " f"{max_bytes} bytes.")}
        )

    content_type = (getattr(uploaded_file, "content_type", "") or "").split(";")[0].strip().lower()
    allowed_types = ALLOWED_CONTENT_TYPES_BY_KIND.get(kind, frozenset())
    if content_type not in allowed_types:
        raise serializers.ValidationError(
            {
                "file": (
                    f"Content type '{content_type or 'unknown'}' is not allowed "
                    f"for kind='{kind}'."
                )
            }
        )

    filename = uploaded_file.name or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    allowed_extensions = CONTENT_TYPE_EXTENSIONS.get(content_type, frozenset())
    if ext not in allowed_extensions:
        raise serializers.ValidationError(
            {"file": f"File extension '.{ext}' does not match content type '{content_type}'."}
        )


def _coerce_value(field_def: CustomFieldDef, raw: Any) -> Any:
    """Type-check/coerce one raw value against `field_def.data_type`.
    Raises `serializers.ValidationError` (a plain string, wrapped by the
    caller into the field-keyed dict) on mismatch.
    """
    data_type = field_def.data_type
    if data_type == CustomFieldDef.DataType.TEXT:
        if not isinstance(raw, str):
            raise serializers.ValidationError("Expected a text value.")
        return raw
    if data_type == CustomFieldDef.DataType.INT:
        if isinstance(raw, bool):  # bool is an int subclass in Python — reject explicitly.
            raise serializers.ValidationError("Expected an integer value.")
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
            return int(raw)
        raise serializers.ValidationError("Expected an integer value.")
    if data_type == CustomFieldDef.DataType.FLOAT:
        if isinstance(raw, bool):
            raise serializers.ValidationError("Expected a numeric value.")
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, str):
            try:
                return float(raw)
            except ValueError:
                raise serializers.ValidationError("Expected a numeric value.") from None
        raise serializers.ValidationError("Expected a numeric value.")
    if data_type == CustomFieldDef.DataType.BOOL:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
            return raw.strip().lower() == "true"
        raise serializers.ValidationError("Expected a boolean value.")
    if data_type == CustomFieldDef.DataType.DATE:
        if isinstance(raw, str):
            try:
                datetime.date.fromisoformat(raw)
            except ValueError:
                raise serializers.ValidationError(
                    "Expected an ISO-8601 date string (YYYY-MM-DD)."
                ) from None
            return raw
        raise serializers.ValidationError("Expected an ISO-8601 date string (YYYY-MM-DD).")
    if data_type == CustomFieldDef.DataType.ENUM:
        options = field_def.enum_options or []
        if raw not in options:
            raise serializers.ValidationError(
                f"'{raw}' is not one of the allowed values: {options}."
            )
        return raw
    if data_type == CustomFieldDef.DataType.JSON:
        # Any JSON-serializable value is accepted as-is (already JSON-decoded
        # by DRF's request parser by the time this runs).
        return raw
    raise serializers.ValidationError(f"Unsupported data_type '{data_type}'.")


def validate_custom_field_values(
    category, raw_values: dict[str, Any] | None
) -> list[tuple[CustomFieldDef, Any]]:
    """Validate `raw_values` (a `{key: value}` dict from the request body)
    against `category.field_defs`. Returns a list of
    `(CustomFieldDef, coerced_value)` pairs ready for
    `AssetFieldValue.objects.bulk_create`/update.

    Raises `serializers.ValidationError({"custom_field_values": {key: msg}})`
    — field-scoped, so the client sees exactly which key is wrong — for:
    - an unknown key (no matching `CustomFieldDef` under this category, a
      typo/stale-form guard),
    - a `required=True` field def missing from `raw_values` (or explicitly
      `None`),
    - a type/enum-membership mismatch (see `_coerce_value` above).
    """
    raw_values = raw_values or {}
    field_defs = list(category.field_defs.all())
    field_defs_by_key = {fd.key: fd for fd in field_defs}

    errors: dict[str, str] = {}
    result: list[tuple[CustomFieldDef, Any]] = []

    for key, raw in raw_values.items():
        field_def = field_defs_by_key.get(key)
        if field_def is None:
            errors[key] = (
                f"'{key}' is not a custom field defined for category " f"'{category.name}'."
            )
            continue
        if raw is None:
            if field_def.required:
                errors[key] = "This field is required."
            continue
        try:
            coerced = _coerce_value(field_def, raw)
        except serializers.ValidationError as exc:
            errors[key] = str(exc.detail[0]) if isinstance(exc.detail, list) else str(exc.detail)
            continue
        result.append((field_def, coerced))

    supplied_keys = {k for k, v in raw_values.items() if v is not None}
    for field_def in field_defs:
        if field_def.required and field_def.key not in supplied_keys:
            errors.setdefault(field_def.key, "This field is required.")

    if errors:
        raise serializers.ValidationError({"custom_field_values": errors})

    return result


def replace_asset_field_values(asset: Asset, category, raw_values: dict[str, Any] | None) -> None:
    """Validate `raw_values` against `category`, then replace `asset`'s
    entire `AssetFieldValue` set with the validated result (used on both
    create and full-replace edit of custom fields). A `None` `raw_values`
    (key not present in the request payload at all) leaves existing values
    untouched — see `apps.assets.serializers.AssetSerializer` for how create
    vs. partial-update distinguish "not supplied" from "supplied empty".
    """
    if raw_values is None:
        return
    coerced_pairs = validate_custom_field_values(category, raw_values)
    AssetFieldValue.objects.filter(asset=asset).delete()
    AssetFieldValue.objects.bulk_create(
        AssetFieldValue(tenant_id=asset.tenant_id, asset=asset, field_def=field_def, value=value)
        for field_def, value in coerced_pairs
    )


def attachment_upload_path(tenant_id: int, asset_id: int, filename: str) -> str:
    """Storage key layout: tenant/asset-scoped so two tenants' files never
    collide on the shared volume, plus a random component so an original
    filename never overwrites another upload.
    """
    import uuid

    safe_name = filename.replace("/", "_").replace("\\", "_")
    return f"attachments/{tenant_id}/{asset_id}/{uuid.uuid4().hex}_{safe_name}"


def save_attachment_file(*, tenant_id: int, asset_id: int, uploaded_file) -> tuple[str, str, int]:
    """Write `uploaded_file`'s bytes to the storage backend (the media
    volume via `django-storages`/Django's storage API) and return
    `(storage_key, content_type, size)` — the ONLY things ever persisted on
    `Attachment`. The bytes themselves never enter the DB.
    """
    key = attachment_upload_path(tenant_id, asset_id, uploaded_file.name)
    storage_key = default_storage.save(key, uploaded_file)
    content_type = getattr(uploaded_file, "content_type", "") or ""
    size = getattr(uploaded_file, "size", None)
    if size is None:
        size = default_storage.size(storage_key)
    return storage_key, content_type, size
