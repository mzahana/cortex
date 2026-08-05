"""Project document/attachment ZIP bundle — "give me everything on file for
this project so I can hand it to an auditor / keep a local copy".

Distinct from `apps.projects.report`, deliberately: the report RENDERS a PDF
(and can *rasterize* scans into it, losing fidelity and filenames); this
module ships the **original files, byte-for-byte**, in a structured tree with
a manifest, which is what an audit actually wants.

Layout inside the archive (`_archive_root` is `<code or slug of name>`):

    <root>/
      README.txt                  — what this bundle is, when/who generated it
      manifest.csv                — one row per file: path, source, size, uploader, uploaded_at
      expenses.csv                — the full expense ledger (same columns as the CSV export)
      documents/<kind>/<file>     — ProjectDocument originals, foldered by kind
      invoices/<expense>/<file>   — ExpenseAttachment originals, foldered per expense
      assets/<asset>/<file>       — (opt-in) attachments of assets assigned to the project

**Memory posture (R2 / the DS220+'s RAM ceiling):** the ZIP is built into a
`SpooledTemporaryFile` on the worker's disk and every member file is streamed
in chunk-by-chunk from `default_storage` — the whole bundle is NEVER
materialized in RAM, which matters because a project with a few hundred
invoice scans can easily be hundreds of MB. `MAX_ARCHIVE_BYTES` is a hard cap
checked as bytes are copied: over it, the job FAILS with a clear, actionable
message rather than OOM-ing the worker (and taking Celery down with it).

Storage keys that are missing/unreadable are recorded in the manifest with a
`MISSING` status and skipped, never fatal: a bundle that is complete except
for one lost file is far more useful to an auditor than no bundle at all.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from datetime import timezone as dt_timezone
from tempfile import SpooledTemporaryFile
from typing import Any, Iterator

from django.core.files.storage import default_storage

from apps.assets.models import Asset, Attachment

from .models import Expense, ExpenseAttachment, Project, ProjectDocument

# Hard ceiling on total UNCOMPRESSED bytes copied into one archive. 512 MiB is
# comfortably above any realistic single-project document set for this lab
# while staying well under the NAS's per-container memory/disk headroom
# (`docs/deployment.md` mem_limits) — and, critically, it is enforced while
# streaming, so hitting it costs a failed job, not a killed worker.
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024

# `SpooledTemporaryFile` keeps the archive in RAM up to this size and
# transparently rolls over to a real temp file beyond it — small bundles never
# touch disk, large ones never blow up memory.
SPOOL_MAX_BYTES = 8 * 1024 * 1024

COPY_CHUNK_BYTES = 256 * 1024

_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


class ArchiveTooLarge(Exception):
    """Raised when the streamed byte count crosses `MAX_ARCHIVE_BYTES`."""


def _safe_component(value: str, *, fallback: str = "unnamed", max_length: int = 60) -> str:
    """One path component, sanitized for a ZIP entry name.

    Not cosmetic: a `filename` is user-supplied (upload metadata), so it is
    exactly the input a Zip-Slip payload (`../../etc/…`) would arrive on. By
    collapsing everything outside `[A-Za-z0-9._-]` and stripping leading dots,
    no member path can escape its folder when the auditor extracts the bundle.
    """
    cleaned = _UNSAFE_PATH_CHARS.sub("-", (value or "").strip()).strip("-._")
    cleaned = cleaned[:max_length].strip("-._")
    return cleaned or fallback


def _archive_root(project: Project) -> str:
    base = project.code or project.name
    return _safe_component(base, fallback=f"project-{project.id}")


@dataclass
class ManifestRow:
    path: str
    source: str
    filename: str
    size: int
    uploaded_by: str
    uploaded_at: str
    status: str = "OK"


@dataclass
class ArchiveStats:
    file_count: int = 0
    total_bytes: int = 0
    missing_count: int = 0
    rows: list[ManifestRow] = field(default_factory=list)


def _iter_storage_chunks(storage_key: str) -> Iterator[bytes]:
    with default_storage.open(storage_key, "rb") as fh:
        while True:
            chunk = fh.read(COPY_CHUNK_BYTES)
            if not chunk:
                return
            yield chunk


def _add_file(
    zf: zipfile.ZipFile,
    stats: ArchiveStats,
    *,
    arcname: str,
    storage_key: str,
    source: str,
    filename: str,
    declared_size: int,
    uploaded_by: str,
    uploaded_at: Any,
) -> None:
    """Stream one stored file into the archive, or record it as MISSING."""
    uploaded_at_str = uploaded_at.isoformat() if uploaded_at else ""
    try:
        written = 0
        with zf.open(arcname, "w") as dest:
            for chunk in _iter_storage_chunks(storage_key):
                stats.total_bytes += len(chunk)
                if stats.total_bytes > MAX_ARCHIVE_BYTES:
                    raise ArchiveTooLarge(arcname)
                written += len(chunk)
                dest.write(chunk)
    except ArchiveTooLarge:
        raise
    except Exception:
        # Missing/unreadable storage object (deleted volume file, backend
        # hiccup). Record it and keep going — see module docstring.
        stats.missing_count += 1
        stats.rows.append(
            ManifestRow(
                path=arcname,
                source=source,
                filename=filename,
                size=declared_size,
                uploaded_by=uploaded_by,
                uploaded_at=uploaded_at_str,
                status="MISSING",
            )
        )
        return

    stats.file_count += 1
    stats.rows.append(
        ManifestRow(
            path=arcname,
            source=source,
            filename=filename,
            size=written,
            uploaded_by=uploaded_by,
            uploaded_at=uploaded_at_str,
        )
    )


def _user_label(user) -> str:
    return getattr(user, "email", "") or ""


def _expenses_csv(project: Project) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "expense_id",
            "date",
            "amount",
            "currency",
            "category",
            "vendor",
            "invoice_number",
            "description",
            "asset",
            "created_by",
            "created_at",
        ]
    )
    expenses = (
        Expense.objects.filter(project=project)
        .select_related("category", "asset", "created_by")
        .order_by("date", "id")
    )
    for expense in expenses:
        writer.writerow(
            [
                expense.id,
                expense.date.isoformat() if expense.date else "",
                expense.amount,
                expense.currency or project.currency or "",
                expense.category.name if expense.category else "",
                expense.vendor,
                expense.invoice_number,
                expense.description,
                expense.asset.name if expense.asset else "",
                _user_label(expense.created_by),
                expense.created_at.isoformat() if expense.created_at else "",
            ]
        )
    return buffer.getvalue()


def _manifest_csv(stats: ArchiveStats) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["path", "source", "original_filename", "bytes", "uploaded_by", "uploaded_at", "status"]
    )
    for row in stats.rows:
        writer.writerow(
            [
                row.path,
                row.source,
                row.filename,
                row.size,
                row.uploaded_by,
                row.uploaded_at,
                row.status,
            ]
        )
    return buffer.getvalue()


def _readme(project: Project, stats: ArchiveStats, *, generated_by: str, options: dict) -> str:
    generated_at = datetime.now(tz=dt_timezone.utc).isoformat()
    missing_note = (
        f"\n{stats.missing_count} file(s) could not be read from storage and are listed "
        "in manifest.csv with status=MISSING.\n"
        if stats.missing_count
        else ""
    )
    return (
        f"Cortex project archive\n"
        f"======================\n\n"
        f"Project:      {project.name}\n"
        f"Code:         {project.code or '-'}\n"
        f"Sponsor:      {project.sponsor or '-'}\n"
        f"Status:       {project.status}\n"
        f"Generated:    {generated_at}\n"
        f"Generated by: {generated_by or '-'}\n"
        f"Options:      {options}\n\n"
        f"Contents\n"
        f"--------\n"
        f"  documents/  project documents (proposal, contract, progress reports, other)\n"
        f"  invoices/   invoice/receipt scans, one folder per expense\n"
        f"  assets/     attachments of assets assigned to this project (if included)\n"
        f"  expenses.csv  the full expense ledger\n"
        f"  manifest.csv  every file in this archive, with size/uploader/timestamps\n\n"
        f"{stats.file_count} file(s), {stats.total_bytes} bytes of original content.\n"
        f"{missing_note}"
    )


def build_project_archive(
    *,
    project: Project,
    include_documents: bool = True,
    include_invoices: bool = True,
    include_asset_attachments: bool = False,
    generated_by: str = "",
) -> tuple[SpooledTemporaryFile, ArchiveStats]:
    """Build the ZIP and return `(open temp file positioned at 0, stats)`.

    Caller owns closing the returned file (`apps.projects.tasks.
    generate_project_archive_zip` does, in a `finally`). Every DB read below
    goes through the tenant-scoped managers — the caller must already be
    inside `tenant_context(...)`, same requirement as the report task.
    """
    root = _archive_root(project)
    stats = ArchiveStats()
    spooled: SpooledTemporaryFile = SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES)

    try:
        with zipfile.ZipFile(spooled, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            if include_documents:
                documents = (
                    ProjectDocument.objects.filter(project=project)
                    .select_related("uploaded_by")
                    .order_by("kind", "id")
                )
                for index, document in enumerate(documents, start=1):
                    kind = _safe_component(document.kind, fallback="other")
                    name = _safe_component(document.filename, fallback=f"document-{document.id}")
                    arcname = f"{root}/documents/{kind}/{index:03d}-{name}"
                    _add_file(
                        zf,
                        stats,
                        arcname=arcname,
                        storage_key=document.storage_key,
                        source=f"project_document:{document.id}",
                        filename=document.filename,
                        declared_size=document.size,
                        uploaded_by=_user_label(document.uploaded_by),
                        uploaded_at=document.created_at,
                    )

            if include_invoices:
                attachments = (
                    ExpenseAttachment.objects.filter(expense__project=project)
                    .select_related("expense", "uploaded_by")
                    .order_by("expense_id", "id")
                )
                for attachment in attachments:
                    expense = attachment.expense
                    label = expense.invoice_number or expense.vendor or f"expense-{expense.id}"
                    folder = f"{expense.id:06d}-{_safe_component(label, fallback='expense')}"
                    name = _safe_component(attachment.filename, fallback=f"invoice-{attachment.id}")
                    arcname = f"{root}/invoices/{folder}/{attachment.id}-{name}"
                    _add_file(
                        zf,
                        stats,
                        arcname=arcname,
                        storage_key=attachment.storage_key,
                        source=f"expense_attachment:{attachment.id}",
                        filename=attachment.filename,
                        declared_size=attachment.size,
                        uploaded_by=_user_label(attachment.uploaded_by),
                        uploaded_at=attachment.created_at,
                    )

            if include_asset_attachments:
                asset_ids = list(Asset.objects.filter(project=project).values_list("id", flat=True))
                asset_names = dict(Asset.objects.filter(id__in=asset_ids).values_list("id", "name"))
                asset_attachments = (
                    Attachment.objects.filter(asset_id__in=asset_ids)
                    .select_related("uploaded_by")
                    .order_by("asset_id", "id")
                )
                for attachment in asset_attachments:
                    asset_label = _safe_component(
                        asset_names.get(attachment.asset_id, ""),
                        fallback=f"asset-{attachment.asset_id}",
                    )
                    folder = f"{attachment.asset_id:06d}-{asset_label}"
                    name = _safe_component(
                        attachment.filename, fallback=f"attachment-{attachment.id}"
                    )
                    arcname = f"{root}/assets/{folder}/{attachment.id}-{name}"
                    _add_file(
                        zf,
                        stats,
                        arcname=arcname,
                        storage_key=attachment.storage_key,
                        source=f"asset_attachment:{attachment.id}",
                        filename=attachment.filename,
                        declared_size=attachment.size,
                        uploaded_by=_user_label(attachment.uploaded_by),
                        uploaded_at=attachment.created_at,
                    )

            # Generated text files last: `manifest.csv` can only be written
            # once every member's real (streamed) size is known.
            zf.writestr(f"{root}/expenses.csv", _expenses_csv(project))
            zf.writestr(f"{root}/manifest.csv", _manifest_csv(stats))
            zf.writestr(
                f"{root}/README.txt",
                _readme(
                    project,
                    stats,
                    generated_by=generated_by,
                    options={
                        "documents": include_documents,
                        "invoices": include_invoices,
                        "asset_attachments": include_asset_attachments,
                    },
                ),
            )
    except Exception:
        spooled.close()
        raise

    spooled.seek(0)
    return spooled, stats
