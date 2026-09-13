"""`GET /api/v1/exports/asset-import-template.xlsx`: a blank, tenant-aware
Excel workbook whose header row is exactly the schema
`apps.imports.services` expects, so an Admin can download it, fill it in,
and upload it back through `POST /api/v1/imports` with the auto-detected
mapping matching every column.

**Why a separate endpoint rather than "export the CSV and clear it".**
`apps.imports.exports.AssetExportView` only emits custom-field columns for
the categories that the EXPORTED assets happen to use — an empty (or
narrowly filtered) tenant therefore exports a header row missing most of
its own fields, and a brand-new tenant exports no usable starting point at
all. This view is keyed off the tenant's CONFIG (every `Category` /
`CustomFieldDef` / `Location` / `Project` that exists) instead of off its
data, so the template is complete on day one.

**Import is create-only, and the template says so.** `apps.imports.
services.commit_import_rows` only ever calls `Asset.objects.create(...)` —
there is no upsert/overwrite/delete path — so uploading a filled template
ADDS rows and never modifies or removes existing assets. Re-uploading the
same file therefore creates duplicates rather than updating; the
"Instructions" sheet states both facts, because that's the single most
common thing a person needs to know before typing an inventory into a
spreadsheet.

**Columns.** `apps.imports.exports.CORE_EXPORT_COLUMNS` verbatim (the same
list the exporter writes and `default_column_mapping` auto-detects), plus
one column per DISTINCT `CustomFieldDef.key` across ALL the tenant's
categories. A custom-field column that doesn't apply to a given row's
category is simply ignored for that row (services.py module docstring), so
a single wide sheet works for a mixed-category inventory.

**Drop-downs come from the tenant's own config.** The `category`,
`location`, `project` and `status` columns carry Excel list validations
sourced from a `Lists` sheet of the tenant's current names, wired up
through workbook DEFINED NAMES rather than inline `"a,b,c"` formulas (see
`_add_list_dropdown` for why: the 255-char inline cap, and commas inside
names). The validations deliberately do NOT reject a typed-in value —
`apps.imports.services`' `create_missing` flow exists precisely so a new
category/location/project can be named in the sheet and created at commit
time, and a drop-down that refused unknown values would make that
impossible from the template.

**Sheet order matters.** `iter_source_rows` reads the worksheet named
"Assets" when one exists (and only falls back to the active sheet
otherwise) — this workbook's data sheet is named "Assets" precisely so
that a round-trip through Excel/Google Sheets, which persists whichever
tab the author last had selected as the active one, still imports the data
sheet rather than the instructions.

RBAC: `import.run` (Admin, tenant-wide) via the same `ImportRunPermission`
the import endpoints use — the template is an input to importing, and
`docs/rbac.md` §3 gives `import.run` no scoped cell. Nothing here is a
tenant-data read beyond the tenant's own admin config, all fetched through
the tenant-scoped managers (R4).

Synchronous, not a Celery job, for the same reason the CSV export is: the
workbook is a handful of small config-sized queries and one in-memory
`openpyxl` save (a header row plus a few dozen reference rows), not a
per-asset render.
"""

from __future__ import annotations

import io

import openpyxl
from django.http import HttpResponse
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation
from rest_framework import permissions
from rest_framework.views import APIView

from apps.assets.models import Asset
from apps.catalog.models import Category, CustomFieldDef, Location
from apps.projects.models import Project

from .exports import CORE_EXPORT_COLUMNS
from .permissions import ImportRunPermission

# Single source of truth: the parser picks the sheet by THIS name
# (`apps.imports.services._pick_worksheet`), so the two can never drift.
from .services import DATA_SHEET_TITLE

INSTRUCTIONS_SHEET_TITLE = "Instructions"
LISTS_SHEET_TITLE = "Lists"

#: Workbook-level defined names backing the four drop-downs. Excel identifiers:
#: no spaces, and they must not look like a cell reference.
_CATEGORY_RANGE_NAME = "CortexCategories"
_LOCATION_RANGE_NAME = "CortexLocations"
_PROJECT_RANGE_NAME = "CortexProjects"
_STATUS_RANGE_NAME = "CortexStatuses"

#: How far down the data sheet the drop-downs are attached. Excel applies a
#: validation to a fixed range, so this is simply the largest sheet we
#: bother to pre-arm; a longer paste still imports fine, it just loses the
#: drop-down (and the server validates every row regardless).
_DROPDOWN_LAST_ROW = 2000

TEMPLATE_FILENAME = "cortex-asset-import-template.xlsx"
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_HEADER_FILL = PatternFill("solid", fgColor="1F2A37")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_SECTION_FONT = Font(bold=True)

#: Per-core-column guidance rendered on the Instructions sheet. Kept in
#: lockstep with `apps.imports.services`'s module docstring (the schema
#: authority) — if a rule changes there, change the wording here too.
_CORE_COLUMN_HELP: dict[str, tuple[str, str]] = {
    "name": ("Required", "Free text, e.g. 'Jetson Orin #3'. Not checked for uniqueness."),
    "category": (
        "Required",
        "Drop-down of your existing categories (matched case-insensitively). "
        "A new name is allowed — the importer offers to create it. A name "
        "used by two categories is rejected as ambiguous rather than "
        "guessed at.",
    ),
    "location": (
        "Optional",
        "Drop-down of your existing locations; a new name can be created on "
        "import. Blank = no location.",
    ),
    "status": (
        "Optional",
        "Drop-down, fixed set of values. Blank defaults to 'available'.",
    ),
    "condition": ("Optional", "Free text, e.g. 'Scratched lid, works fine'."),
    "description": ("Optional", "Free text, any length."),
    "serial_number": ("Optional", "Free text, max 255 characters. Not checked for uniqueness."),
    "manufacturer": ("Optional", "Who MADE it, e.g. 'NVIDIA'. Max 255 characters."),
    "model": ("Optional", "Model name/number, e.g. 'Jetson Orin AGX'. Max 255 characters."),
    "supplier": ("Optional", "Who you BOUGHT it from, e.g. 'Mouser'. Max 255 characters."),
    "purchase_date": ("Optional", "Date, written YYYY-MM-DD (e.g. 2026-03-14)."),
    "purchase_cost": (
        "Optional",
        "A number, at most 2 decimal places, e.g. 1999.00. Commas and a "
        "leading currency symbol are ignored; put the currency in its own "
        "column.",
    ),
    "currency": ("Optional", "3-letter code, e.g. USD, EUR, SAR."),
    "warranty_expiry": ("Optional", "Date, written YYYY-MM-DD."),
    "project": (
        "Optional",
        "Drop-down of your existing projects; a new name can be created on "
        "import. Blank = general pool.",
    ),
    "tags": ("Optional", "Comma-separated, e.g. 'gpu, on-loan'. Unknown tags are created."),
    "url": ("Optional", "Must start with http:// or https:// if filled in."),
}


def _autosize(worksheet, widths: list[int]) -> None:
    for index, width in enumerate(widths, start=1):
        worksheet.column_dimensions[get_column_letter(index)].width = width


def _write_header_row(worksheet, header: list[str]) -> None:
    worksheet.append(header)
    for cell in worksheet[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(vertical="center")
    worksheet.freeze_panes = "A2"
    _wide = {"name", "description", "condition", "model", "manufacturer", "supplier", "url"}
    _autosize(
        worksheet,
        [32 if name in _wide else max(14, min(len(name) + 4, 40)) for name in header],
    )


def tenant_custom_field_columns() -> list[tuple[str, CustomFieldDef]]:
    """Distinct `(key, representative field def)` across ALL the tenant's
    categories, in a stable category/order/id order. Deliberately keyed by
    `key` (not label) so the template's headers match what
    `apps.imports.exports` writes and what a re-imported export contains;
    the importer matches either.

    Caller must already be inside the request's tenant context — this uses
    the tenant-scoped manager.
    """
    columns: list[tuple[str, CustomFieldDef]] = []
    seen: set[str] = set()
    for field_def in CustomFieldDef.objects.select_related("category").order_by(
        "category_id", "order", "id"
    ):
        if field_def.key in seen:
            continue
        seen.add(field_def.key)
        columns.append((field_def.key, field_def))
    return columns


def _write_instructions(worksheet, custom_columns: list[tuple[str, CustomFieldDef]]) -> None:
    def section(title: str) -> None:
        worksheet.append([])
        worksheet.append([title])
        worksheet.cell(row=worksheet.max_row, column=1).font = _SECTION_FONT

    worksheet.append(["How to use this template"])
    worksheet.cell(row=1, column=1).font = Font(bold=True, size=13)
    for line in (
        f"1. Fill one asset per row on the '{DATA_SHEET_TITLE}' sheet, under the existing headers.",
        "2. Do not rename, reorder or delete the header row — the importer matches on those names.",
        "3. Save as .xlsx (or .csv) and upload it on the Import screen.",
        "4. The upload is a dry run first: you review the per-row report, then press Commit.",
        "",
        "Importing ADDS assets. It never edits, overwrites or deletes assets already in Cortex.",
        "Because of that, uploading the same file twice creates duplicate assets.",
        "Commit is all-or-nothing: if any row has an error, nothing at all is created.",
    ):
        worksheet.append([line])

    section("Columns")
    worksheet.append(["Column", "Required?", "Notes"])
    for cell in worksheet[worksheet.max_row]:
        cell.font = _SECTION_FONT
    for column in CORE_EXPORT_COLUMNS:
        required, notes = _CORE_COLUMN_HELP.get(column, ("Optional", ""))
        worksheet.append([column, required, notes])
    for key, field_def in custom_columns:
        worksheet.append(
            [
                key,
                "Required" if field_def.required else "Optional",
                (
                    f"Custom field '{field_def.label}' ({field_def.data_type})"
                    + (
                        f", options: {', '.join(str(o) for o in field_def.enum_options)}"
                        if field_def.data_type == CustomFieldDef.DataType.ENUM
                        and field_def.enum_options
                        else ""
                    )
                    + ". Only applies to rows whose category defines it; leave blank otherwise."
                ),
            ]
        )

    section("Where the drop-down values come from")
    for line in (
        f"The category, location, project and status columns on the "
        f"'{DATA_SHEET_TITLE}' sheet are drop-downs, filled from what your "
        f"Cortex tenant has defined right now.",
        f"The full lists live on the '{LISTS_SHEET_TITLE}' sheet — don't edit "
        "them there, it won't create anything in Cortex.",
        "To use a NEW category/location/project, just type it over the cell "
        "instead of picking from the drop-down. Excel asks 'continue?' — "
        "choose Yes; the value is kept.",
        "The dry-run report then lists it under 'Not in Cortex yet', and you "
        "tick a box there to have it created when you commit.",
        "A name shared by two categories or two locations is reported as "
        "ambiguous and must be renamed in Cortex — creating another copy "
        "can't resolve which one a row meant.",
    ):
        worksheet.append([line])

    _autosize(worksheet, [38, 14, 90])


def _tenant_list_values() -> list[tuple[str, str, list[str]]]:
    """`(column heading, defined-name, values)` for each drop-down source,
    in the order they're written onto the `Lists` sheet. Names are read
    through the tenant-scoped managers, so a tenant only ever sees its own
    (R4) — proved by `test_import_template.py`'s cross-tenant test.
    """
    return [
        ("Categories", _CATEGORY_RANGE_NAME, _distinct_names(Category)),
        ("Locations", _LOCATION_RANGE_NAME, _distinct_names(Location)),
        ("Projects", _PROJECT_RANGE_NAME, _distinct_names(Project)),
        ("Statuses", _STATUS_RANGE_NAME, list(Asset.Status.values)),
    ]


def _distinct_names(model) -> list[str]:
    """Sorted, case-insensitively de-duplicated names. The de-dup matters:
    a tree can legitimately hold two `Battery` categories under different
    parents (the model only enforces uniqueness per parent), and listing
    that name twice would give the drop-down a duplicate entry that the
    importer then rejects as ambiguous anyway.
    """
    seen: set[str] = set()
    names: list[str] = []
    for name in model.objects.order_by("name").values_list("name", flat=True):
        if name.strip().lower() in seen:
            continue
        seen.add(name.strip().lower())
        names.append(name)
    return names


def _write_lists_sheet(worksheet) -> dict[str, int]:
    """One column per drop-down source, header in row 1. Returns
    `{defined-name: value count}` so the caller can size each named range
    exactly — a range padded with blank cells shows blank drop-down entries.
    """
    columns = _tenant_list_values()
    worksheet.append([heading for heading, _, _ in columns])
    for cell in worksheet[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
    longest = max((len(values) for _, _, values in columns), default=0)
    for offset in range(longest):
        worksheet.append(
            [values[offset] if offset < len(values) else None for _, _, values in columns]
        )
    worksheet.freeze_panes = "A2"
    _autosize(worksheet, [28] * len(columns))
    return {range_name: len(values) for _, range_name, values in columns}


def _add_list_dropdown(
    *, workbook, data_sheet, column_index: int, range_name: str, count: int, title: str
) -> None:
    """Attach a list validation on `column_index` of the data sheet, sourced
    from the `Lists` sheet via a workbook DEFINED NAME rather than an inline
    `"a,b,c"` formula.

    Two reasons, both load-bearing here: an inline list is capped at 255
    characters (a lab with a few dozen locations blows straight past that
    and the validation is silently dropped), and it uses commas as the
    separator, so any category name CONTAINING a comma would split into two
    bogus entries. A defined name also degrades better across Excel
    versions than a bare cross-sheet range reference.
    """
    if count <= 0:
        return  # nothing defined yet — a drop-down of nothing helps no one
    letter = get_column_letter(column_index)
    workbook.defined_names.add(
        DefinedName(
            range_name,
            attr_text=f"'{LISTS_SHEET_TITLE}'!$"
            f"{get_column_letter(_range_column_index(range_name))}$2:$"
            f"{get_column_letter(_range_column_index(range_name))}${count + 1}",
        )
    )
    validation = DataValidation(
        type="list",
        formula1=f"={range_name}",
        allow_blank=True,
        showDropDown=False,  # openpyxl inverts this flag: False = show the dropdown
        # Without this the `error` text below is dead config — Excel only
        # surfaces a validation message when `showErrorMessage` is set.
        showErrorMessage=True,
        # **Load-bearing, and NOT the default.** `errorStyle` defaults to
        # "stop", which makes Excel flatly refuse a value that isn't in the
        # list — that would make the whole `create_missing` flow
        # unreachable from this template, since naming a NEW category is
        # exactly "type something the list doesn't have". "warning" prompts
        # instead ("... continue?" -> Yes keeps the typed value), which is
        # the intended behaviour: nudge towards the existing name, allow the
        # new one. Google Sheets imports this as "Show a warning" for the
        # same effect.
        errorStyle="warning",
    )
    validation.errorTitle = title
    validation.error = (
        "That name isn't in Cortex yet. Choose Yes to keep it — when you "
        "upload the sheet, the Import screen will offer to create it."
    )
    data_sheet.add_data_validation(validation)
    validation.add(f"{letter}2:{letter}{_DROPDOWN_LAST_ROW}")


def _range_column_index(range_name: str) -> int:
    """Which `Lists` column a defined name points at — the order
    `_tenant_list_values` writes them in."""
    order = [
        _CATEGORY_RANGE_NAME,
        _LOCATION_RANGE_NAME,
        _PROJECT_RANGE_NAME,
        _STATUS_RANGE_NAME,
    ]
    return order.index(range_name) + 1


def build_import_template_workbook() -> openpyxl.Workbook:
    """The workbook itself. Caller must already be inside the tenant's
    context (see `tenant_custom_field_columns`).
    """
    custom_columns = tenant_custom_field_columns()
    header = CORE_EXPORT_COLUMNS + [key for key, _ in custom_columns]

    workbook = openpyxl.Workbook()
    data_sheet = workbook.active
    data_sheet.title = DATA_SHEET_TITLE
    _write_header_row(data_sheet, header)

    instructions = workbook.create_sheet(INSTRUCTIONS_SHEET_TITLE)
    _write_instructions(instructions, custom_columns)

    # The drop-down source sheet must EXIST before a validation can point a
    # defined name at it.
    lists_sheet = workbook.create_sheet(LISTS_SHEET_TITLE)
    counts = _write_lists_sheet(lists_sheet)

    for column, range_name, title in (
        ("category", _CATEGORY_RANGE_NAME, "Unknown category"),
        ("location", _LOCATION_RANGE_NAME, "Unknown location"),
        ("project", _PROJECT_RANGE_NAME, "Unknown project"),
        ("status", _STATUS_RANGE_NAME, "Unknown status"),
    ):
        _add_list_dropdown(
            workbook=workbook,
            data_sheet=data_sheet,
            column_index=CORE_EXPORT_COLUMNS.index(column) + 1,
            range_name=range_name,
            count=counts[range_name],
            title=title,
        )

    workbook.active = 0
    return workbook


class AssetImportTemplateView(APIView):
    permission_classes = [permissions.IsAuthenticated, ImportRunPermission]

    def get(self, request, *args, **kwargs):
        workbook = build_import_template_workbook()
        buffer = io.BytesIO()
        workbook.save(buffer)
        response = HttpResponse(buffer.getvalue(), content_type=XLSX_CONTENT_TYPE)
        response["Content-Disposition"] = f'attachment; filename="{TEMPLATE_FILENAME}"'
        return response
