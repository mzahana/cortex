"""Business logic for label generation (T4.5), kept out of the view/task per
CLAUDE.md ("keep serializers/views thin; put business rules in services").

Two responsibilities:
1. `resolve_label_data` — turn a tenant/RBAC-scoped list of `Asset` rows into
   the plain `LabelData` the renderer needs (never leaks the `Asset` ORM
   object itself into `rendering.py`).
2. `save_label_pdf` — write the rendered PDF bytes to the SAME storage
   backend/volume `apps.assets.services.save_attachment_file` already uses
   (django-storages, `default_storage`) and return the key + metadata that
   `Job.mark_succeeded` persists — the bytes never touch the DB, same rule
   as an Attachment.
"""

from __future__ import annotations

import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from apps.assets.models import Asset

from .rendering import LabelData


def resolve_label_data(assets: list[Asset]) -> list[LabelData]:
    """`assets` is already the caller's tenant + RBAC-scoped, ordered list
    (`apps.labels.api.LabelGenerateView` / `apps.labels.tasks`). Each asset's
    `category`/`location` should already be `select_related` by the caller
    to avoid N+1s across a full sheet.
    """
    items: list[LabelData] = []
    for asset in assets:
        items.append(
            LabelData(
                qr_token=asset.qr_token,
                name=asset.name,
                # Short, human-scannable public code — the first 8 hex chars
                # of the public `uuid` (`docs/data-model.md`: "id (uuid,
                # public code)"), NOT the internal auto-increment pk (never
                # printed/exposed, same rule the rest of the API follows).
                asset_code=f"ID {str(asset.uuid)[:8]}",
                category_name=asset.category.name if asset.category else "",
                location_name=asset.location.name if asset.location else "No location",
            )
        )
    return items


def label_pdf_storage_key(tenant_id: int, job_id) -> str:
    """Storage key layout: tenant + job-scoped so two tenants'/jobs' PDFs
    never collide on the shared volume — same reasoning as
    `apps.assets.services.attachment_upload_path`, keyed by the job's own
    unguessable UUID rather than a separate random suffix (a `Job` id is
    already exactly that).
    """
    return f"labels/{tenant_id}/{job_id}.pdf"


def save_label_pdf(*, tenant_id: int, job_id, pdf_bytes: bytes) -> tuple[str, str]:
    """Write `pdf_bytes` to the storage backend; returns `(storage_key,
    filename)` — the only things ever persisted on `Job` (see module
    docstring: the bytes themselves never enter the DB).
    """
    key = label_pdf_storage_key(tenant_id, job_id)
    storage_key = default_storage.save(key, ContentFile(pdf_bytes))
    filename = f"labels-{uuid.UUID(str(job_id)).hex[:8]}.pdf"
    return storage_key, filename
