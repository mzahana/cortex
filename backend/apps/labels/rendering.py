"""QR + Avery sheet -> PDF bytes (T4.5).

Two libraries, one job each (`docs/architecture.md`/`CLAUDE.md` stack):
`segno` renders one QR code per asset as a PNG data URI; `WeasyPrint`
rasterizes an HTML+CSS sheet (built from `apps.labels.templates.
SheetTemplate` geometry) that lays those QR codes + human-readable text out
into a print-ready, selectable-text PDF.

**QR payload — the bare `qr_token`, not a full URL (deliberate; own it):**
`docs/tasks/M4-mobile-scan-labels.md` T4.3 describes the scan flow as
"`@zxing/browser` camera scan -> **extract token** -> call `/resolve/
{token}`" — i.e. the scanned payload IS the token, not a URL the client has
to parse a path segment out of. Encoding a full `https://<host>/...` URL
would also hard-code a not-yet-confirmed hostname (`docs/risks.md` §3 Q3)
into every already-printed label, permanently breaking them if the domain
ever changes. The bare token is hostname-independent, trivially decodable
in a test (`assert qr.data == asset.qr_token`, no URL parsing), and is
exactly what `GET /api/v1/resolve/{qr_token}` (T4.1) already expects to
receive.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from io import BytesIO

import segno
from weasyprint import HTML

from .templates import SheetTemplate

# QR scale (pixels per QR module) — high enough to stay crisp/scannable at
# the small physical sizes these templates print at (down to ~1in square on
# `avery_5160`), low enough that the embedded PNG data URIs don't bloat the
# PDF noticeably across a full 30-label sheet.
QR_PIXEL_SCALE = 6
QR_BORDER_MODULES = 2


@dataclass(frozen=True)
class LabelData:
    """One label's worth of pre-resolved, already-tenant-scoped display data
    — the ONLY thing `rendering.py` needs per asset. Keeping this a plain
    dataclass (rather than passing `Asset` instances straight through) keeps
    this module free of any DB/tenant-context concerns; `services.py`
    resolves these from `Asset` rows before calling in here.
    """

    qr_token: str
    name: str
    asset_code: str
    category_name: str
    location_name: str


def _qr_data_uri(payload: str) -> str:
    # `micro=False` is deliberate and load-bearing: `segno.make()` otherwise
    # auto-selects a Micro QR (a genuinely different, incompatible symbology)
    # for a short payload like a `qr_token` — verified while building this
    # module that neither `pyzbar`/zbar nor OpenCV's `QRCodeDetector` (nor,
    # by extension, `@zxing/browser`'s default QR reader, T4.3) can decode a
    # Micro QR at all, which would have silently broken the entire F6/F7
    # scan round-trip despite every unit test on the pre-render payload
    # passing. Forcing a standard (non-Micro) QR is what every off-the-shelf
    # QR reader — including the phone camera scan flow this label is
    # printed FOR — actually expects.
    qr = segno.make(payload, error="m", micro=False)
    return qr.png_data_uri(scale=QR_PIXEL_SCALE, border=QR_BORDER_MODULES)


def _label_html(data: LabelData) -> str:
    # `html.escape` on every user-supplied string (asset name/category/
    # location are free-text fields, `apps.assets.models`) — this HTML is
    # fed straight to WeasyPrint, so an unescaped `<`/`&` in an asset name
    # would corrupt the layout (not a security boundary here — WeasyPrint
    # never executes script — but still correctness-breaking without it).
    name = html.escape(data.name)
    code = html.escape(data.asset_code)
    category = html.escape(data.category_name)
    location = html.escape(data.location_name)
    qr_uri = _qr_data_uri(data.qr_token)

    return f"""
    <div class="label">
      <img class="qr" src="{qr_uri}" alt="QR" />
      <div class="text">
        <div class="name">{name}</div>
        <div class="meta">{code}</div>
        <div class="meta">{category}</div>
        <div class="meta">{location}</div>
      </div>
    </div>
    """


def _sheet_css(template: SheetTemplate) -> str:
    return f"""
    @page {{
      size: {template.page_width_in}in {template.page_height_in}in;
      margin: 0;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Helvetica, Arial, sans-serif; }}
    .sheet {{
      position: relative;
      width: {template.page_width_in}in;
      height: {template.page_height_in}in;
      page-break-after: always;
    }}
    .sheet:last-child {{ page-break-after: auto; }}
    .label {{
      position: absolute;
      width: {template.label_width_in}in;
      height: {template.label_height_in}in;
      padding: 0.06in 0.08in;
      display: flex;
      flex-direction: row;
      align-items: center;
      gap: 0.06in;
      overflow: hidden;
    }}
    .label .qr {{
      flex: 0 0 auto;
      width: {min(template.label_height_in - 0.12, template.label_width_in * 0.4):.3f}in;
      height: {min(template.label_height_in - 0.12, template.label_width_in * 0.4):.3f}in;
    }}
    .label .text {{ min-width: 0; overflow: hidden; }}
    .label .name {{
      font-size: 9pt;
      font-weight: 700;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .label .meta {{
      font-size: 7pt;
      color: #333;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    """


def _label_position_css(template: SheetTemplate, index_on_sheet: int) -> str:
    row, col = divmod(index_on_sheet, template.cols)
    left = template.margin_left_in + col * (template.label_width_in + template.gutter_x_in)
    top = template.margin_top_in + row * (template.label_height_in + template.gutter_y_in)
    return f"left: {left}in; top: {top}in;"


def render_labels_pdf(items: list[LabelData], template: SheetTemplate) -> bytes:
    """Render one print-ready PDF: every `LabelData` in `items` laid out
    `template.labels_per_sheet` at a time across as many sheets as needed.
    `items` order is preserved (the order the caller resolved asset ids in).
    """
    per_sheet = template.labels_per_sheet
    sheets_html: list[str] = []
    for sheet_start in range(0, len(items), per_sheet):
        sheet_items = items[sheet_start : sheet_start + per_sheet]
        labels_html = []
        for i, data in enumerate(sheet_items):
            position = _label_position_css(template, i)
            label_html = _label_html(data).replace(
                'class="label"', f'class="label" style="{position}"'
            )
            labels_html.append(label_html)
        sheets_html.append(f'<div class="sheet">{"".join(labels_html)}</div>')

    document_html = f"""<!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8" />
      <style>{_sheet_css(template)}</style>
    </head>
    <body>
      {"".join(sheets_html)}
    </body>
    </html>
    """

    buffer = BytesIO()
    HTML(string=document_html).write_pdf(buffer)
    return buffer.getvalue()
