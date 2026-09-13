"""Parsing/validation/commit engine for the CSV/Excel bulk importer (T6.1).

**ASSUMPTION flagged (Q8, `docs/risks.md`): no representative spreadsheet
sample was provided.** This module is designed against a generic, reasonable
asset-spreadsheet schema instead — the following columns, matched
case-insensitively and whitespace-trimmed against a spreadsheet's header row:

    name        (required)  — `Asset.name`.
    category    (required)  — matched BY NAME against the tenant's `Category`
                               tree (`apps.catalog.models.Category`, a
                               self-referential parent/child tree). Matching
                               is a case-insensitive exact match on `name`
                               ANYWHERE in the tree — a name that is not
                               unique tenant-wide (two categories with the
                               same name under different parents) is reported
                               as an "ambiguous category name" row error
                               rather than silently picking one; a lab's
                               category names are expected to be
                               tenant-wide-unique in practice even though the
                               model itself only enforces uniqueness per
                               (parent, name).
    location    (optional)  — same by-name matching against the `Location`
                               tree; blank = no location.
    project     (optional)  — by-name match against `Project` (tenant-wide
                               unique by `Project.name`, so never ambiguous);
                               blank = general pool.
    status      (optional)  — matched case-insensitively against
                               `Asset.Status` choices; defaults to
                               `Asset.Status.AVAILABLE` when blank/omitted.
    condition   (optional)  — free text, stored as-is on `Asset.condition`.
    tags        (optional)  — comma-separated tag names; matched-or-created
                               by name (`Tag.objects.get_or_create`), same as
                               `AssetSerializer._sync_tags` does for a normal
                               `POST /assets`.

Every OTHER column header is treated as a **custom-field column**: for each
row, once `category` has resolved, the header is matched case-insensitively
against that category's `CustomFieldDef.key` or `.label`; the cell value is
then validated/coerced by the SAME `apps.assets.services.
validate_custom_field_values` used by ordinary asset create/edit (reused
verbatim here, not reimplemented — a `text|int|float|bool|date|enum|json`
type-checked value, `required` enforcement, `enum_options` membership all
apply identically). A column that matches no custom field for a given row's
category is simply ignored for that row (it may still apply to a different
row whose category defines a field with that key/label) — no ambiguity error
is raised for this case, unlike category/location, because two DIFFERENT
categories legitimately define fields under the same spreadsheet column
(e.g. a "power_watts" column used by both "Compute" and "Edge" rows).

This exact schema is also what `apps.imports.exports.stream_assets_csv`
exports — see that module for the round-trip: an export always emits
`name, category, location, status, condition, project, tags` plus one column
per DISTINCT custom-field KEY used by the exported assets' categories, so
re-importing an unmodified export reproduces equivalent assets.

**Creating missing categories/locations/projects on request
(`create_missing`).** By default a `category`/`location`/`project` cell
naming something the tenant hasn't defined is a per-row error, and the
importer creates no config of its own. A caller may opt in per target
(`CREATABLE_TARGETS` — `POST /imports` and `POST /imports/{id}/commit` both
accept a `create_missing` list, gated on the same tenant-wide
`category.manage`/`location.manage`/`tenant.manage` permissions the
ordinary admin-CRUD screens require), in which case those names stop being
errors and are created at commit time, in the same transaction as the
assets. A dry-run never writes them — it only reports, in
`report["missing_references"]`, exactly which names a commit would create,
which is what lets the UI offer the choice with the real list in hand.
Three things this deliberately does NOT do: it never modifies an existing
category/location/project (a name that already resolves is reused
untouched), it never resolves an AMBIGUOUS name by creating a duplicate
(that stays a row error), and it never guesses tree structure — new
categories/locations land at the root with default flags for an Admin to
refine afterwards.

**Streaming, bounded-RAM parsing (R2, CLAUDE.md "slow work runs in
Celery... chunked to bound worker RAM").** `iter_source_rows` never
materializes an entire spreadsheet in memory: CSV is read row-by-row via the
stdlib `csv` module over the storage file's own stream; `.xlsx` is opened
with `openpyxl.load_workbook(..., read_only=True)`, which streams worksheet
rows from disk rather than loading the whole workbook into memory (the
`pandas.read_excel`-style "load it all" approach this deliberately avoids).
`resolve_import_rows` is itself a generator over that stream, so a dry-run
report is built with peak memory proportional to ONE row at a time during
parsing, converging to O(row count) only when the caller (`build_report`)
collects the per-row report list for storage — reasonable and bounded for a
lab inventory spreadsheet (M1 already proved 10k+ assets is comfortably
fast/well-indexed; a spreadsheet describing that many rows of everyday lab
gear, at a few hundred bytes of resolved state each, is a few MB, not a
worker-RAM risk).

**Duplicate detection (`on_duplicate`).** A row whose `(name, category)`
already matches an existing asset — or an earlier row of the same file — is
flagged as a duplicate, and the caller chooses what happens: `reject` (the
DEFAULT: the row becomes an error, so the all-or-nothing commit creates
nothing until a human decides), `skip` (import everything else), or
`create` (make the second copy anyway — buying an identical second Jetson
is normal). `Asset.name` has no DB uniqueness constraint and deliberately
gets none; this is a warning a person resolves, not a rule. The check costs
one extra query for the whole file, not one per row.

**Commit is all-or-nothing under a single transaction (documented decision,
task's own preferred default for MVP).** `commit_import_rows` fully resolves
every row FIRST (still via the same streaming/bounded parse above) and only
opens `transaction.atomic()` if EVERY row is valid; if any row is invalid it
creates nothing and returns the same per-row report so the caller sees
exactly what's still wrong. This is simpler and safer than a partial-commit
("valid rows only") for a spreadsheet-sized dataset — a half-imported
inventory sheet with silently-skipped rows is a worse failure mode for a lab
than "fix the 3 flagged rows and re-run the whole file", and it does not
conflict with the bounded-RAM requirement above: chunking there is about the
PARSE step never loading the whole file at once, not about the number of
transactions the DB write uses (a lab inventory import is at most low
thousands of rows — a single transaction's rowset, not a memory problem).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Iterator

import openpyxl
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models.functions import Lower
from rest_framework import serializers

from apps.assets.models import Asset, AssetFieldValue, TagLink
from apps.assets.services import validate_custom_field_values
from apps.catalog.models import Category, CustomFieldDef, Location, Tag
from apps.projects.models import Project

# --- Column-mapping targets (module docstring is the schema authority) ------

TARGET_NAME = "name"
TARGET_CATEGORY = "category"
TARGET_LOCATION = "location"
TARGET_PROJECT = "project"
TARGET_STATUS = "status"
TARGET_CONDITION = "condition"
TARGET_TAGS = "tags"
TARGET_URL = "url"
TARGET_DESCRIPTION = "description"
TARGET_SERIAL_NUMBER = "serial_number"
TARGET_MANUFACTURER = "manufacturer"
TARGET_MODEL = "model"
TARGET_SUPPLIER = "supplier"
TARGET_PURCHASE_DATE = "purchase_date"
TARGET_PURCHASE_COST = "purchase_cost"
TARGET_CURRENCY = "currency"
TARGET_WARRANTY_EXPIRY = "warranty_expiry"
TARGET_CUSTOM = "custom"
TARGET_IGNORE = "ignore"

#: Plain free-text `Asset` columns: copied through as-is, only length-checked
#: against the model's own `max_length` (see `_TEXT_MAX_LENGTHS`).
TEXT_TARGETS: frozenset[str] = frozenset(
    {
        TARGET_DESCRIPTION,
        TARGET_SERIAL_NUMBER,
        TARGET_MANUFACTURER,
        TARGET_MODEL,
        TARGET_SUPPLIER,
    }
)
DATE_TARGETS: frozenset[str] = frozenset({TARGET_PURCHASE_DATE, TARGET_WARRANTY_EXPIRY})

#: `max_length` for every `CharField`-backed target. Without this a value
#: that's one character too long sails through parsing and blows up as a
#: `DataError` mid-`INSERT`, failing the whole commit with a database
#: traceback instead of a per-row "this cell is too long" the user can act
#: on. `description`/`condition` are `TextField`s and so have no limit.
_TEXT_MAX_LENGTHS: dict[str, int] = {
    TARGET_NAME: 255,
    TARGET_SERIAL_NUMBER: 255,
    TARGET_MANUFACTURER: 255,
    TARGET_MODEL: 255,
    TARGET_SUPPLIER: 255,
    TARGET_CURRENCY: 3,
}

CORE_TARGETS: frozenset[str] = frozenset(
    {
        TARGET_NAME,
        TARGET_CATEGORY,
        TARGET_LOCATION,
        TARGET_PROJECT,
        TARGET_STATUS,
        TARGET_CONDITION,
        TARGET_TAGS,
        # `Asset.url` is a CORE target, not a custom field: it must round-trip
        # through `apps.imports.exports.CORE_EXPORT_COLUMNS` (which includes
        # it). If it were left unmapped, `default_column_mapping` would fall
        # through to `TARGET_CUSTOM` and a re-imported export would try to
        # materialize the built-in column as a per-category custom field.
        TARGET_URL,
        # The rest of `Asset`'s own scalar columns. These were missing from
        # the original T6.1 schema (which was designed blind against Q8's
        # unavailable sample spreadsheet) even though every one of them is a
        # real field on the model and a real column of the asset form — so a
        # serial number or purchase cost typed into a spreadsheet silently
        # became a per-category CUSTOM field, or was dropped.
        TARGET_DESCRIPTION,
        TARGET_SERIAL_NUMBER,
        TARGET_MANUFACTURER,
        TARGET_MODEL,
        TARGET_SUPPLIER,
        TARGET_PURCHASE_DATE,
        TARGET_PURCHASE_COST,
        TARGET_CURRENCY,
        TARGET_WARRANTY_EXPIRY,
    }
)
ALL_TARGETS: frozenset[str] = CORE_TARGETS | {TARGET_CUSTOM, TARGET_IGNORE}

#: The three targets whose values are FKs to a tenant-config row that the
#: importer can create on demand (`create_missing`, see this module's
#: docstring). `status` is deliberately absent (a closed enum on `Asset`,
#: not a config table) and so is `tags` (already auto-created by name on the
#: ordinary asset-create path, so there is nothing to opt into).
CREATABLE_TARGETS: frozenset[str] = frozenset({TARGET_CATEGORY, TARGET_LOCATION, TARGET_PROJECT})

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({"csv", "xlsx"})


def file_extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


#: Extra spreadsheet spellings that map onto a core target, beyond the
#: target's own name. Matched on `normalize_header` output, so punctuation
#: and separators are already gone ("Serial #" -> "serial", "Purchase Cost"
#: -> "purchase cost", "serial_number" -> "serial number").
#:
#: Deliberately conservative: only spellings that are unambiguous for a lab
#: inventory. "Vendor" goes to `supplier` rather than `manufacturer` (who
#: you BOUGHT from is what an invoice column means), and genuinely
#: ambiguous words ("notes", "room", "ref") are left to fall through to
#: custom-field matching, where a wrong guess is harmless — and the user can
#: always re-point any column by hand in the mapping step.
HEADER_ALIASES: dict[str, str] = {
    "asset name": TARGET_NAME,
    "item": TARGET_NAME,
    "item name": TARGET_NAME,
    "asset type": TARGET_CATEGORY,
    "serial": TARGET_SERIAL_NUMBER,
    "serial no": TARGET_SERIAL_NUMBER,
    "serial num": TARGET_SERIAL_NUMBER,
    "sn": TARGET_SERIAL_NUMBER,
    "s n": TARGET_SERIAL_NUMBER,
    "make": TARGET_MANUFACTURER,
    "brand": TARGET_MANUFACTURER,
    "model no": TARGET_MODEL,
    "model number": TARGET_MODEL,
    "desc": TARGET_DESCRIPTION,
    "vendor": TARGET_SUPPLIER,
    "seller": TARGET_SUPPLIER,
    "purchased from": TARGET_SUPPLIER,
    "cost": TARGET_PURCHASE_COST,
    "price": TARGET_PURCHASE_COST,
    "unit cost": TARGET_PURCHASE_COST,
    "unit price": TARGET_PURCHASE_COST,
    "ccy": TARGET_CURRENCY,
    "purchased": TARGET_PURCHASE_DATE,
    "purchased on": TARGET_PURCHASE_DATE,
    "date purchased": TARGET_PURCHASE_DATE,
    "warranty": TARGET_WARRANTY_EXPIRY,
    "warranty end": TARGET_WARRANTY_EXPIRY,
    "warranty until": TARGET_WARRANTY_EXPIRY,
    "warranty expiration": TARGET_WARRANTY_EXPIRY,
    "link": TARGET_URL,
    "tag": TARGET_TAGS,
    "labels": TARGET_TAGS,
}


def normalize_header(header: str) -> str:
    """A spreadsheet header reduced to comparable words: lower-cased, with
    every run of non-alphanumerics collapsed to one space. This is what lets
    "Serial #", "Serial Number", "serial_number" and "SERIAL-NUMBER" all
    land on the same target instead of only the machine spelling matching.
    """
    return re.sub(r"[^a-z0-9]+", " ", header.lower()).strip()


def default_column_mapping(headers: list[str]) -> dict[str, str]:
    """Auto-match every header against `CORE_TARGETS` — by the target's own
    name or any `HEADER_ALIASES` spelling, compared on `normalize_header`
    output; anything unmatched defaults to `"custom"` (per-row/per-category
    custom-field resolution, see module docstring) rather than `"ignore"` —
    every column is used unless the caller explicitly opts a header out via
    an override mapping.
    """
    by_normalized = {normalize_header(target): target for target in CORE_TARGETS}
    by_normalized.update(HEADER_ALIASES)
    mapping: dict[str, str] = {}
    for header in headers:
        mapping[header] = by_normalized.get(normalize_header(header), TARGET_CUSTOM)
    return mapping


def resolve_column_mapping(headers: list[str], override: dict[str, str] | None) -> dict[str, str]:
    """The auto-detected default (`default_column_mapping`), with any
    caller-supplied `override` entries replacing the default for the SAME
    header (an override for a header that isn't actually present in the
    file is silently ignored — it can't apply to anything).
    """
    resolved = default_column_mapping(headers)
    if override:
        for header, target in override.items():
            if header in resolved and target in ALL_TARGETS:
                resolved[header] = target
    return resolved


# --- Streaming source parsing (R2: bounded RAM) ------------------------------


#: The worksheet name `apps.imports.import_template` gives the downloadable
#: template's data sheet. Preferred over `workbook.active` because Excel and
#: Google Sheets both persist "whichever tab was selected when I saved" as
#: the active sheet — so a multi-sheet template that someone browsed the
#: instructions tab of would otherwise import the instructions.
DATA_SHEET_TITLE = "Assets"


def _pick_worksheet(workbook: Any):
    """The sheet to import from: one named `DATA_SHEET_TITLE`
    (case-insensitive) if the workbook has one, else the active sheet —
    which keeps every ordinary single-sheet spreadsheet working exactly as
    before.
    """
    for name in workbook.sheetnames:
        if name.strip().lower() == DATA_SHEET_TITLE.lower():
            return workbook[name]
    return workbook.active


def iter_source_rows(fileobj: Any, filename: str) -> Iterator[tuple[list[str], dict[str, Any]]]:
    """Yields `(headers, row_dict)` for every DATA row (the header row itself
    is consumed first and not yielded as data). `row_dict` is
    `{header: cell_value}`; missing trailing cells map to `None`.

    Streams from `fileobj` row-by-row for both formats — see module
    docstring's "bounded RAM" note.
    """
    ext = file_extension(filename)
    if ext == "csv":
        text_stream = io.TextIOWrapper(fileobj, encoding="utf-8-sig", newline="")
        reader = csv.reader(text_stream)
        try:
            headers = next(reader)
        except StopIteration:
            return
        headers = [h.strip() for h in headers]
        for raw_row in reader:
            if not any(cell.strip() for cell in raw_row if cell):
                continue  # skip a fully-blank line
            padded = raw_row + [None] * (len(headers) - len(raw_row))
            yield headers, dict(zip(headers, padded, strict=True))
        return

    if ext == "xlsx":
        workbook = openpyxl.load_workbook(fileobj, read_only=True, data_only=True)
        try:
            worksheet = _pick_worksheet(workbook)
            if worksheet is None:  # an empty workbook with no sheets at all
                return
            rows_iter = worksheet.iter_rows(values_only=True)
            try:
                header_row = next(rows_iter)
            except StopIteration:
                return
            headers = [str(h).strip() if h is not None else "" for h in header_row]
            for raw_row in rows_iter:
                if all(cell is None for cell in raw_row):
                    continue
                padded = list(raw_row) + [None] * (len(headers) - len(raw_row))
                yield headers, dict(zip(headers, padded, strict=True))
        finally:
            workbook.close()
        return

    raise ValueError(f"Unsupported file extension '.{ext}'. Only .csv and .xlsx are supported.")


def _normalize_cell(value: Any) -> Any:
    """Cell -> a plain Python value ready for `_coerce_value`-style
    validation: blank strings and `None` both become `None` ("not supplied");
    `datetime.date`/`datetime.datetime` (native openpyxl cell types)
    normalize to an ISO date string; everything else passes through as-is
    (CSV cells are already plain `str`; openpyxl already gives native
    `int`/`float`/`bool`/`str` for the rest).
    """
    import datetime

    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped != "" else None
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()[:10]
    return value


def _parse_decimal(value: Any) -> tuple[Decimal | None, str | None]:
    """A money cell -> `Decimal`, or an error message. Mirrors what DRF's
    `DecimalField` would do for the same value on `POST /assets`
    (`max_digits=12, decimal_places=2` on `Asset.purchase_cost`): more than
    two decimal places is REJECTED rather than silently rounded — quietly
    changing a recorded price is worse than making someone fix the cell.
    """
    if value is None:
        return None, None
    raw = str(value).strip()
    if not raw:
        return None, None
    # Tolerate the thousands separators and stray currency symbols a
    # spreadsheet cell picks up when it's been formatted as text.
    cleaned = re.sub(r"[,\s]", "", raw).lstrip("$€£")
    try:
        parsed = Decimal(cleaned)
    except InvalidOperation:
        return None, f"'{value}' is not a number."
    if not parsed.is_finite():
        return None, f"'{value}' is not a number."
    if parsed < 0:
        return None, "Cost cannot be negative."
    if -parsed.as_tuple().exponent > 2:  # type: ignore[operator]
        return None, "Use at most 2 decimal places."
    if parsed >= Decimal("10000000000"):  # max_digits=12, decimal_places=2
        return None, "Value is too large."
    return parsed, None


def _parse_date(value: Any) -> tuple[date | None, str | None]:
    """A date cell -> `datetime.date`, or an error message. `_normalize_cell`
    has already turned openpyxl's native date/datetime cells into ISO
    strings, so only the ISO form has to be handled here. An ambiguous
    written form (`03/04/2026`) is REJECTED rather than guessed at — day-
    first vs month-first would silently record the wrong date for half the
    world.
    """
    if value is None:
        return None, None
    raw = str(value).strip()
    if not raw:
        return None, None
    try:
        return date.fromisoformat(raw[:10]), None
    except ValueError:
        return None, f"'{value}' is not a date. Use YYYY-MM-DD (e.g. 2026-03-14)."


# --- Row resolution -----------------------------------------------------------


@dataclass
class ResolvedRow:
    row_number: int  # 1-based, counts header as row 1 (so row 2 = first data row)
    name: str | None = None
    category: Category | None = None
    location: Location | None = None
    project: Project | None = None
    tag_names: list[str] = field(default_factory=list)
    status: str = Asset.Status.AVAILABLE
    condition: str = ""
    url: str = ""
    description: str = ""
    serial_number: str = ""
    manufacturer: str = ""
    model: str = ""
    supplier: str = ""
    currency: str = ""
    purchase_cost: Decimal | None = None
    purchase_date: date | None = None
    warranty_expiry: date | None = None
    custom_field_pairs: list[tuple[CustomFieldDef, Any]] = field(default_factory=list)
    #: `{target: raw name}` for every `category`/`location`/`project` cell
    #: naming something this tenant doesn't have yet. Populated whether or
    #: not `create_missing` covers that target: with it, the name is what
    #: `commit_import_rows` creates; without it, the row ALSO carries the
    #: matching `errors` entry, and the aggregate is what tells the UI which
    #: names it could offer to create.
    unresolved: dict[str, str] = field(default_factory=dict)
    #: Existing assets with the same `(name, category)` — see
    #: `annotate_duplicates`. Non-empty means "the tenant already has this".
    duplicate_of_asset_ids: list[int] = field(default_factory=list)
    duplicate_of_asset_name: str | None = None
    #: An EARLIER row of this same file with the same `(name, category)`.
    duplicate_of_row: int | None = None
    #: Set by `annotate_duplicates` under `on_duplicate="skip"`: valid, but
    #: deliberately not created.
    skip: bool = False
    # `Any`, not `str`: most entries are a plain message string, but
    # `custom_field_values` nests `validate_custom_field_values`'s own
    # `{key: message}` dict verbatim (see below).
    errors: dict[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return not self.errors

    @property
    def is_duplicate(self) -> bool:
        return bool(self.duplicate_of_asset_ids) or self.duplicate_of_row is not None

    def duplicate_message(self) -> str:
        if self.duplicate_of_asset_ids:
            count = len(self.duplicate_of_asset_ids)
            return (
                f"An asset named '{self.name}' already exists in this category"
                + (f" ({count} of them)" if count > 1 else "")
                + ". Choose what to do with duplicates before committing."
            )
        return (
            f"Row {self.duplicate_of_row} of this file already has the same " "name and category."
        )

    def duplicate_report_entry(self) -> dict[str, Any]:
        return {
            "row_number": self.row_number,
            "name": self.name,
            "category": self.category.name if self.category else self.unresolved.get("category"),
            "existing_asset_ids": list(self.duplicate_of_asset_ids),
            "duplicate_of_row": self.duplicate_of_row,
            "skipped": self.skip,
        }

    def to_report_dict(self) -> dict[str, Any]:
        return {
            "row_number": self.row_number,
            "values": {
                "name": self.name,
                # Falls back to the raw spreadsheet text for a name that
                # doesn't exist yet, so the review table shows what the user
                # typed (and what would be created) rather than a blank.
                "category": (
                    self.category.name if self.category else self.unresolved.get(TARGET_CATEGORY)
                ),
                "location": (
                    self.location.name if self.location else self.unresolved.get(TARGET_LOCATION)
                ),
                "project": (
                    self.project.name if self.project else self.unresolved.get(TARGET_PROJECT)
                ),
                "tags": self.tag_names,
                "status": self.status,
                "condition": self.condition,
                "url": self.url,
                "description": self.description,
                "serial_number": self.serial_number,
                "manufacturer": self.manufacturer,
                "model": self.model,
                "supplier": self.supplier,
                # JSON-safe: `AuditLog`/`ImportJob.report` are JSONB, and
                # neither `Decimal` nor `date` survives that on their own.
                "purchase_cost": (
                    str(self.purchase_cost) if self.purchase_cost is not None else None
                ),
                "currency": self.currency,
                "purchase_date": self.purchase_date.isoformat() if self.purchase_date else None,
                "warranty_expiry": (
                    self.warranty_expiry.isoformat() if self.warranty_expiry else None
                ),
                "custom_field_values": {fd.key: value for fd, value in self.custom_field_pairs},
            },
            "unresolved_references": dict(self.unresolved),
            "duplicate_of_asset_ids": list(self.duplicate_of_asset_ids),
            "duplicate_of_row": self.duplicate_of_row,
            "skipped": self.skip,
            "errors": self.errors,
        }


class _NameIndex:
    """Case-insensitive-name -> object index over a tenant's tree/list, with
    ambiguity detection (module docstring: two rows sharing a `name` at
    different places in the `Category`/`Location` tree is reported as an
    error, never silently resolved to "the first match").
    """

    def __init__(self, objects: Iterable[Any]):
        self._by_name: dict[str, list[Any]] = {}
        for obj in objects:
            self._by_name.setdefault(obj.name.strip().lower(), []).append(obj)

    def resolve(self, raw_name: str) -> tuple[Any | None, str | None, int]:
        """Returns `(object_or_none, error_or_none, match_count)`.

        `match_count` is what lets the caller distinguish the two failure
        modes, which the `create_missing` feature treats very differently:
        **0 matches** is a name that simply doesn't exist yet and CAN be
        created on the user's say-so, while **2+ matches** is ambiguous and
        never can be — creating a third `Battery` category wouldn't tell us
        which of the existing two the row meant.
        """
        matches = self._by_name.get(raw_name.strip().lower(), [])
        if not matches:
            return None, f"'{raw_name}' does not match any known name.", 0
        if len(matches) > 1:
            return None, f"'{raw_name}' is ambiguous ({len(matches)} matches).", len(matches)
        return matches[0], None, 1

    def add(self, obj: Any) -> None:
        """Index a just-created object so later rows naming it resolve to
        the SAME row instead of trying to create it again."""
        self._by_name.setdefault(obj.name.strip().lower(), []).append(obj)


def resolve_import_rows(
    *,
    tenant,
    source_stream: Any,
    filename: str,
    mapping_override: dict[str, str] | None,
    resolved_mapping_out: dict[str, str] | None = None,
    create_missing: Iterable[str] = (),
) -> Iterator[ResolvedRow]:
    """Streams `ResolvedRow`s for every data row in `source_stream`
    (`filename` picks the CSV/xlsx parser). `tenant` is used to build the
    Category/Location/Project name indexes ONCE up front (small, tenant-wide
    admin-config-sized queries — not per-row) — caller MUST already be
    inside `apps.tenancy.context.tenant_context(tenant.id)` so the plain
    `Category.objects`/`Location.objects`/`Project.objects`/`Tag.objects`
    tenant-scoped managers resolve correctly (R4).

    `mapping_override` is resolved against the file's ACTUAL header row
    (`resolve_column_mapping`, module docstring) the moment it's read — a
    single streaming pass, never a separate "peek the header row first"
    pass (which would need a second `default_storage.open()`/`seek(0)`
    dance that's fragile across storage backends). If `resolved_mapping_out`
    (a plain `dict`) is supplied, it is mutated in place with the resolved
    `{header: target}` mapping as soon as it's known, so a caller that fully
    drains this generator (both `build_report` and `commit_import_rows` do)
    can read the ACTUAL confirmed mapping back out afterward without a
    second parse.

    `create_missing` is any subset of `CREATABLE_TARGETS`. For a target it
    covers, a cell naming a category/location/project this tenant doesn't
    have is NOT a row error — the name is recorded on
    `ResolvedRow.unresolved` for `commit_import_rows` to create. Resolution
    itself never writes: a dry-run with `create_missing` set reports exactly
    what a commit would create, without creating it. An AMBIGUOUS name
    (two config rows share it) stays an error regardless — see
    `_NameIndex.resolve`.
    """
    creatable = frozenset(create_missing) & CREATABLE_TARGETS
    categories = _NameIndex(Category.objects.all())
    locations = _NameIndex(Location.objects.all())
    projects = _NameIndex(Project.objects.all())
    status_by_lower = {choice.lower(): choice for choice in Asset.Status.values}

    header_to_target: dict[str, str] | None = None
    row_number = 1  # header row is row 1

    for headers, raw_row in iter_source_rows(source_stream, filename):
        if header_to_target is None:
            header_to_target = resolve_column_mapping(headers, mapping_override)
            if resolved_mapping_out is not None:
                resolved_mapping_out.update(header_to_target)
        row_number += 1

        resolved = ResolvedRow(row_number=row_number)
        custom_raw: dict[str, Any] = {}

        for header, cell in raw_row.items():
            target = header_to_target.get(header, TARGET_CUSTOM)
            value = _normalize_cell(cell)

            if target == TARGET_IGNORE:
                continue
            if target == TARGET_NAME:
                text = str(value).strip() if value is not None else None
                if text is not None and len(text) > _TEXT_MAX_LENGTHS[TARGET_NAME]:
                    resolved.errors[TARGET_NAME] = (
                        f"Too long ({len(text)} characters); the maximum is "
                        f"{_TEXT_MAX_LENGTHS[TARGET_NAME]}."
                    )
                else:
                    resolved.name = text or None
            elif target in (TARGET_CATEGORY, TARGET_LOCATION, TARGET_PROJECT):
                if value is not None:
                    index = {
                        TARGET_CATEGORY: categories,
                        TARGET_LOCATION: locations,
                        TARGET_PROJECT: projects,
                    }[target]
                    obj, err, match_count = index.resolve(str(value))
                    if obj is not None:
                        setattr(resolved, target, obj)
                    elif match_count == 0:
                        # Doesn't exist (yet). Always recorded so the report
                        # can offer to create it; only an error when the
                        # caller hasn't opted in to creating this target.
                        resolved.unresolved[target] = str(value).strip()
                        if target not in creatable:
                            resolved.errors[target] = err
                    else:  # ambiguous — creating another one cannot fix it
                        resolved.errors[target] = err
            elif target == TARGET_STATUS:
                if value is not None:
                    resolved_status = status_by_lower.get(str(value).strip().lower())
                    if resolved_status is None:
                        resolved.errors[TARGET_STATUS] = (
                            f"'{value}' is not a valid status "
                            f"({', '.join(Asset.Status.values)})."
                        )
                    else:
                        resolved.status = resolved_status
            elif target == TARGET_CONDITION:
                resolved.condition = str(value) if value is not None else ""
            elif target in TEXT_TARGETS:
                text = str(value).strip() if value is not None else ""
                limit = _TEXT_MAX_LENGTHS.get(target)
                if limit is not None and len(text) > limit:
                    resolved.errors[target] = (
                        f"Too long ({len(text)} characters); the maximum is {limit}."
                    )
                else:
                    setattr(resolved, target, text)
            elif target == TARGET_CURRENCY:
                # Upper-cased on the way in: ISO-4217 codes are upper-case by
                # definition, and a spreadsheet full of "usd" would otherwise
                # produce assets that don't group with the "USD" ones
                # anywhere that compares the string.
                code = str(value).strip().upper() if value is not None else ""
                if code and len(code) != 3:
                    resolved.errors[target] = (
                        f"'{value}' is not a 3-letter currency code (e.g. USD, EUR, SAR)."
                    )
                else:
                    resolved.currency = code
            elif target == TARGET_PURCHASE_COST:
                cost, err = _parse_decimal(value)
                if err:
                    resolved.errors[target] = err
                else:
                    resolved.purchase_cost = cost
            elif target in DATE_TARGETS:
                parsed, err = _parse_date(value)
                if err:
                    resolved.errors[target] = err
                else:
                    setattr(resolved, target, parsed)
            elif target == TARGET_URL:
                raw_url = str(value).strip() if value is not None else ""
                if raw_url and not raw_url.lower().startswith(("http://", "https://")):
                    # Same http/https-only rule the API enforces
                    # (`apps.assets.serializers.AssetSerializer.validate_url`)
                    # -- a spreadsheet is just another write path into the
                    # same field, and the value ends up in an `<a href>`.
                    resolved.errors[TARGET_URL] = "URL must start with http:// or https://."
                else:
                    resolved.url = raw_url
            elif target == TARGET_TAGS:
                if value is not None:
                    resolved.tag_names = [
                        part.strip() for part in str(value).split(",") if part.strip()
                    ]
            else:  # TARGET_CUSTOM
                custom_raw[header.strip().lower()] = value

        if resolved.name is None and TARGET_NAME not in resolved.errors:
            # Guarded on the existing error so an over-long name keeps the
            # specific "too long" message instead of being overwritten with
            # the generic "required" one (it IS present, just unusable).
            resolved.errors[TARGET_NAME] = "This field is required."
        if (
            resolved.category is None
            and TARGET_CATEGORY not in resolved.errors
            and TARGET_CATEGORY not in resolved.unresolved
        ):
            # Covers BOTH "column present but blank" and "no header maps to
            # category at all" — either way it's required. A category that
            # merely doesn't exist YET (and that `create_missing` covers) is
            # not a missing value, so it is excluded here.
            resolved.errors[TARGET_CATEGORY] = "This field is required."

        # Custom fields: only resolvable once `category` is known — matched
        # per-row against THAT category's field defs, by key or label
        # (case-insensitive), per the module docstring.
        if resolved.category is not None and custom_raw:
            field_defs = list(resolved.category.field_defs.all())
            by_key = {fd.key.lower(): fd for fd in field_defs}
            by_label = {fd.label.strip().lower(): fd for fd in field_defs}

            values_by_key: dict[str, Any] = {}
            for header_lower, raw_value in custom_raw.items():
                field_def = by_key.get(header_lower) or by_label.get(header_lower)
                if field_def is None:
                    continue  # column doesn't apply to this row's category
                values_by_key[field_def.key] = raw_value

            # Also require any field the category defines but whose column
            # wasn't present at all in this file, so `required=True` custom
            # fields are still enforced (mirrors ordinary asset creation).
            try:
                pairs = validate_custom_field_values(resolved.category, values_by_key)
            except serializers.ValidationError as exc:
                detail = exc.detail
                if isinstance(detail, dict):
                    errors = detail.get("custom_field_values", detail)
                else:
                    errors = detail
                resolved.errors["custom_field_values"] = errors
            else:
                resolved.custom_field_pairs = pairs

        yield resolved


# --- Report building (dry-run + commit's own re-validation) -----------------


def collect_missing_references(rows: Iterable[ResolvedRow]) -> dict[str, list[str]]:
    """`{target: [names]}` for every creatable target, de-duplicated
    case-insensitively but preserving the first spelling the file used (so
    the UI offers to create "Lab Bench 3", not "lab bench 3"). Always has
    all three keys, so a client can iterate it without existence checks.
    """
    missing: dict[str, list[str]] = {target: [] for target in sorted(CREATABLE_TARGETS)}
    seen: dict[str, set[str]] = {target: set() for target in missing}
    for row in rows:
        for target, name in row.unresolved.items():
            if target not in missing or name.lower() in seen[target]:
                continue
            seen[target].add(name.lower())
            missing[target].append(name)
    return missing


#: How a row that looks like an asset the tenant already has is handled.
#: The DEFAULT is deliberately the cautious one — before this existed the
#: importer created duplicates silently, which is exactly the failure a
#: person re-uploading "the same sheet, now with three more rows" walks
#: into.
ON_DUPLICATE_REJECT = "reject"
ON_DUPLICATE_SKIP = "skip"
ON_DUPLICATE_CREATE = "create"
ON_DUPLICATE_CHOICES: frozenset[str] = frozenset(
    {ON_DUPLICATE_REJECT, ON_DUPLICATE_SKIP, ON_DUPLICATE_CREATE}
)
DEFAULT_ON_DUPLICATE = ON_DUPLICATE_REJECT


def _duplicate_key(name: str | None, category_name: str | None) -> tuple[str, str] | None:
    """What counts as "the same asset" for duplicate detection: the
    case-insensitive, whitespace-trimmed `(name, category)` pair.

    Name alone is too broad — a lab legitimately owns five things called
    "USB-C cable" in different categories — and anything narrower (adding
    location, say) would miss the actual re-upload case this exists for,
    where the whole row is identical. `Asset.name` has no uniqueness
    constraint in the DB and shouldn't get one: buying a second identical
    Jetson is normal, which is precisely why this is a WARNING the user
    resolves, not a hard rule.
    """
    if not name or not category_name:
        return None
    return (name.strip().lower(), category_name.strip().lower())


def annotate_duplicates(rows: list[ResolvedRow], *, on_duplicate: str) -> None:
    """Flag every row that matches an asset the tenant already has, or an
    earlier row in the same file, and apply `on_duplicate` to it.

    - `reject` (default) — the row becomes invalid, so the all-or-nothing
      commit creates NOTHING until the user decides. This is the "don't
      blindly insert" default.
    - `skip` — the row stays valid but is not created; everything else in
      the file imports.
    - `create` — flagged in the report for visibility, but created anyway
      (a genuine second identical unit).

    One extra query for the whole file, not one per row: the existing
    `(lower(name), category_id)` pairs are fetched in a single `IN` lookup
    keyed on the names the file actually mentions.
    """
    in_file_seen: dict[tuple[str, str], int] = {}

    # Existing-asset lookup, only for rows whose category actually resolved
    # (a category being CREATED by this same import cannot already hold an
    # asset, so those rows can't collide with the DB — only with each other).
    # Falls back to the RAW category name for a row whose category is being
    # created by this same import (`create_missing` leaves `row.category`
    # None until commit). Without that fallback, two identical rows naming a
    # brand-new category would both key to `None` and slip past duplicate
    # detection entirely — the exact silent-duplicate case this exists to
    # stop. They still can't collide with the DB (a category that doesn't
    # exist yet holds no assets), which is why the existing-asset lookup
    # below stays guarded on `row.category is not None`.
    keyed_rows = [
        (
            row,
            _duplicate_key(
                row.name,
                row.category.name if row.category else row.unresolved.get(TARGET_CATEGORY),
            ),
        )
        for row in rows
    ]
    names = {key[0] for _, key in keyed_rows if key}
    existing: dict[tuple[str, int], list[tuple[int, str]]] = {}
    if names:
        for asset_id, asset_name, category_id, lname in (
            Asset.objects.annotate(lname=Lower("name"))
            .filter(lname__in=names)
            .values_list("id", "name", "category_id", "lname")
        ):
            existing.setdefault((lname, category_id), []).append((asset_id, asset_name))

    for row, key in keyed_rows:
        if key is None:
            continue

        if row.category is not None:
            for asset_id, asset_name in existing.get((key[0], row.category.id), []):
                row.duplicate_of_asset_ids.append(asset_id)
                row.duplicate_of_asset_name = asset_name

        first_row_number = in_file_seen.get(key)
        if first_row_number is not None:
            row.duplicate_of_row = first_row_number
        else:
            in_file_seen[key] = row.row_number

        if not row.is_duplicate:
            continue
        if on_duplicate == ON_DUPLICATE_REJECT:
            row.errors["duplicate"] = row.duplicate_message()
        elif on_duplicate == ON_DUPLICATE_SKIP:
            row.skip = True


def _resolve_all(
    *,
    tenant,
    source_stream: Any,
    filename: str,
    mapping_override: dict[str, str] | None,
    create_missing: Iterable[str],
    on_duplicate: str,
) -> tuple[list[ResolvedRow], dict[str, str]]:
    """The full parse both the dry-run and the commit do, identically: parse
    every row, then flag duplicates across the whole set. Shared so a
    commit's re-validation can never diverge from the report the user
    approved."""
    resolved_mapping: dict[str, str] = {}
    rows = list(
        resolve_import_rows(
            tenant=tenant,
            source_stream=source_stream,
            filename=filename,
            mapping_override=mapping_override,
            resolved_mapping_out=resolved_mapping,
            create_missing=create_missing,
        )
    )
    annotate_duplicates(rows, on_duplicate=on_duplicate)
    return rows, resolved_mapping


def _report_dict(
    *,
    rows: list[ResolvedRow],
    resolved_mapping: dict[str, str],
    creatable: list[str],
    on_duplicate: str,
) -> dict[str, Any]:
    duplicates = [row for row in rows if row.is_duplicate]
    return {
        "resolved_mapping": resolved_mapping,
        "rows": [row.to_report_dict() for row in rows],
        "total_rows": len(rows),
        "valid_count": sum(1 for row in rows if row.is_valid),
        "invalid_count": sum(1 for row in rows if not row.is_valid),
        # What a commit WOULD create — a dry run never writes these, whether
        # or not `create_missing` was requested.
        "missing_references": collect_missing_references(rows),
        "create_missing": creatable,
        "created_references": {target: [] for target in sorted(CREATABLE_TARGETS)},
        "on_duplicate": on_duplicate,
        "duplicate_count": len(duplicates),
        "duplicate_rows": [row.duplicate_report_entry() for row in duplicates],
        "skipped_count": sum(1 for row in rows if row.skip),
    }


def build_report(
    *,
    tenant,
    source_stream: Any,
    filename: str,
    mapping_override: dict[str, str] | None,
    create_missing: Iterable[str] = (),
    on_duplicate: str = DEFAULT_ON_DUPLICATE,
) -> dict[str, Any]:
    creatable = sorted(frozenset(create_missing) & CREATABLE_TARGETS)
    rows, resolved_mapping = _resolve_all(
        tenant=tenant,
        source_stream=source_stream,
        filename=filename,
        mapping_override=mapping_override,
        create_missing=creatable,
        on_duplicate=on_duplicate,
    )
    return _report_dict(
        rows=rows,
        resolved_mapping=resolved_mapping,
        creatable=creatable,
        on_duplicate=on_duplicate,
    )


# --- Commit -------------------------------------------------------------------


def _create_missing_references(
    *, tenant, targets: list[str], missing: dict[str, list[str]], actor=None
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    """Create the named `Category`/`Location`/`Project` config rows the
    spreadsheet referenced but the tenant doesn't have. Returns
    `({target: {lowercased name: object}}, {target: [created names]})` — the
    first for re-pointing the parsed rows, the second for the report.

    Caller must already hold `transaction.atomic()` (see
    `commit_import_rows`) so an import that fails partway leaves neither
    assets NOR half-invented config behind.

    New rows are created at the ROOT of the `Category`/`Location` tree
    (`parent=None`) with every other field defaulted — a spreadsheet column
    carries a bare name and nothing that could place it under a parent or
    set `requires_approval`/`kind`; an Admin refines it afterwards in the
    normal admin screens.

    Audit parity with each type's ordinary create path, not a new policy of
    its own: `Category`/`Location` creates are not audited anywhere in this
    codebase (`apps.catalog.api`'s `perform_create` just saves, and
    `docs/rbac.md` §5's mandatory list doesn't include them), while a
    `Project` create IS (`apps.projects.api.ProjectViewSet.perform_create`,
    `action=tenant.manage`) — so only projects write an `AuditLog` entry
    here, using that same action/snapshot.
    """
    # Local import: `apps.projects.api` pulls in a large slice of the DRF
    # layer, and importing it at module scope would make this parsing module
    # depend on the API layer (and risk a cycle) purely to reuse one
    # snapshot helper.
    from apps.audit.services import write_audit_log
    from apps.projects.api import _project_audit_snapshot
    from apps.rbac.permission_keys import TENANT_MANAGE

    resolved: dict[str, dict[str, Any]] = {target: {} for target in sorted(CREATABLE_TARGETS)}
    created_names: dict[str, list[str]] = {target: [] for target in sorted(CREATABLE_TARGETS)}
    for target in targets:
        for name in missing.get(target, []):
            if target == TARGET_CATEGORY:
                obj, was_created = Category.objects.get_or_create(
                    tenant=tenant, parent=None, name=name
                )
            elif target == TARGET_LOCATION:
                obj, was_created = Location.objects.get_or_create(
                    tenant=tenant, parent=None, name=name
                )
            else:
                obj, was_created = Project.objects.get_or_create(tenant=tenant, name=name)
                if was_created:
                    write_audit_log(
                        tenant_id=tenant.id,
                        actor=actor,
                        action=TENANT_MANAGE,
                        entity_type="project",
                        entity_id=obj.id,
                        before=None,
                        after=_project_audit_snapshot(obj),
                        ip=None,
                    )
            # Indexed unconditionally, `was_created` or not: `get_or_create`
            # matches the name case-SENSITIVELY while `_NameIndex` matched
            # case-insensitively, so a rare near-miss can come back as an
            # existing row. The rows still need pointing at it either way —
            # only the "we created this" reporting/audit is conditional.
            resolved[target][obj.name.strip().lower()] = obj
            if was_created:
                created_names[target].append(obj.name)
    return resolved, created_names


def commit_import_rows(
    *,
    tenant,
    source_stream: Any,
    filename: str,
    mapping_override: dict[str, str] | None,
    create_missing: Iterable[str] = (),
    on_duplicate: str = DEFAULT_ON_DUPLICATE,
    actor=None,
) -> tuple[list[int], dict[str, Any]]:
    """All-or-nothing (module docstring): fully resolves every row first; if
    any row is invalid, creates nothing and returns `([], report)`. If every
    row is valid, creates one `Asset` (+ custom-field values + tags) per row
    inside a single `transaction.atomic()` block and returns
    `(created_asset_ids, report)`.

    `on_duplicate` decides what happens to a row naming an asset the tenant
    already has (`annotate_duplicates`): `reject` (the default) makes it a
    row error so nothing at all is created, `skip` imports everything else,
    `create` makes the second copy anyway.

    `create_missing` (any subset of `CREATABLE_TARGETS`) additionally
    creates the category/location/project rows the file names but the
    tenant doesn't have — inside the SAME transaction as the assets, so
    "all-or-nothing" covers the new config rows too. It never touches an
    EXISTING config row: a name that already resolves is reused as-is, and
    nothing is renamed, re-parented or deleted. `actor` is only used for the
    project-create audit entry.

    Caller must already be inside `tenant_context(tenant.id)` (same
    requirement as `resolve_import_rows`) — `Asset.objects`/`Tag.objects`
    etc. below are the tenant-scoped managers.
    """
    creatable = sorted(frozenset(create_missing) & CREATABLE_TARGETS)
    resolved_rows, resolved_mapping = _resolve_all(
        tenant=tenant,
        source_stream=source_stream,
        filename=filename,
        mapping_override=mapping_override,
        create_missing=creatable,
        on_duplicate=on_duplicate,
    )
    report = _report_dict(
        rows=resolved_rows,
        resolved_mapping=resolved_mapping,
        creatable=creatable,
        on_duplicate=on_duplicate,
    )
    missing = report["missing_references"]

    if report["invalid_count"] or not resolved_rows:
        return [], report

    created_ids: list[int] = []
    with transaction.atomic():
        if creatable:
            by_target_name, created_names = _create_missing_references(
                tenant=tenant, targets=creatable, missing=missing, actor=actor
            )
            report["created_references"] = created_names
            # Re-point the parsed rows at the config rows just created.
            # Keyed by lower-cased name, the same case-insensitive matching
            # `_NameIndex` does, so two spreadsheet rows spelling one new
            # location differently still share a single new `Location`.
            for row in resolved_rows:
                for target, name in row.unresolved.items():
                    if target not in by_target_name:
                        continue
                    obj = by_target_name[target].get(name.strip().lower())
                    if obj is not None:
                        setattr(row, target, obj)

        tag_cache: dict[str, Tag] = {}
        for row in resolved_rows:
            if row.skip:
                # `on_duplicate="skip"`: valid, deliberately not created.
                # Counted in the report's `skipped_count` so the result
                # screen can say so rather than silently importing fewer
                # rows than the file had.
                continue
            # Every row here is already `is_valid` (the `invalid_count`
            # early-return above), and `category` is a required field
            # (`resolve_import_rows` always adds an error for a missing/
            # unresolved category) -- so `row.category` is guaranteed
            # non-`None` at this point; asserted for the type checker.
            assert row.category is not None
            asset = Asset.objects.create(
                tenant=tenant,
                category=row.category,
                name=row.name,
                project=row.project,
                location=row.location,
                status=row.status,
                condition=row.condition,
                url=row.url,
                description=row.description,
                serial_number=row.serial_number,
                manufacturer=row.manufacturer,
                model=row.model,
                supplier=row.supplier,
                purchase_date=row.purchase_date,
                purchase_cost=row.purchase_cost,
                currency=row.currency,
                warranty_expiry=row.warranty_expiry,
                is_consumable=row.category.default_is_consumable,
            )
            if row.custom_field_pairs:
                AssetFieldValue.objects.bulk_create(
                    AssetFieldValue(tenant=tenant, asset=asset, field_def=field_def, value=value)
                    for field_def, value in row.custom_field_pairs
                )
            for tag_name in row.tag_names:
                key = tag_name.strip()
                if not key:
                    continue
                tag = tag_cache.get(key.lower())
                if tag is None:
                    tag, _ = Tag.objects.get_or_create(tenant=tenant, name=key)
                    tag_cache[key.lower()] = tag
                TagLink.objects.create(tenant=tenant, asset=asset, tag=tag)
            created_ids.append(asset.id)

    return created_ids, report


# --- Source file storage ------------------------------------------------------


def import_source_storage_key(tenant_id: int, import_job_id: int, filename: str) -> str:
    safe_name = filename.replace("/", "_").replace("\\", "_")
    return f"imports/{tenant_id}/{import_job_id}/{safe_name}"


def save_import_source_file(
    *, tenant_id: int, import_job_id: int, uploaded_file: Any
) -> tuple[str, str, str]:
    """Writes `uploaded_file`'s bytes to the storage backend, same
    bytes-never-touch-the-DB pattern as `apps.assets.services.
    save_attachment_file`. Returns `(storage_key, filename, content_type)`.
    """
    key = import_source_storage_key(tenant_id, import_job_id, uploaded_file.name)
    storage_key = default_storage.save(key, uploaded_file)
    content_type = getattr(uploaded_file, "content_type", "") or ""
    return storage_key, uploaded_file.name, content_type
