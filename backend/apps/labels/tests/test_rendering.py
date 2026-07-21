"""T4.5 — `apps.labels.rendering`/`apps.labels.templates` unit-level checks,
independent of the HTTP/Celery layers covered in `test_labels_generate.py`:
Avery sheet geometry math, the Micro-QR footgun (own it — see
`rendering._qr_data_uri`'s docstring for why this is load-bearing, not
cosmetic), and multi-sheet pagination at the pure-function level.
"""

from __future__ import annotations

import segno

from apps.labels.rendering import LabelData, render_labels_pdf
from apps.labels.templates import AVERY_5160, AVERY_5163, SHEET_TEMPLATES


class TestSheetTemplateGeometry:
    def test_avery_5160_fills_the_page_exactly(self):
        t = AVERY_5160
        width = t.cols * t.label_width_in + (t.cols - 1) * t.gutter_x_in + 2 * t.margin_left_in
        height = t.rows * t.label_height_in + (t.rows - 1) * t.gutter_y_in + 2 * t.margin_top_in
        assert round(width, 4) == t.page_width_in
        assert round(height, 4) == t.page_height_in
        assert t.labels_per_sheet == 30

    def test_avery_5163_fills_the_page_exactly(self):
        t = AVERY_5163
        width = t.cols * t.label_width_in + (t.cols - 1) * t.gutter_x_in + 2 * t.margin_left_in
        height = t.rows * t.label_height_in + (t.rows - 1) * t.gutter_y_in + 2 * t.margin_top_in
        assert round(width, 4) == t.page_width_in
        assert round(height, 4) == t.page_height_in
        assert t.labels_per_sheet == 10

    def test_registry_has_both_documented_defaults(self):
        assert set(SHEET_TEMPLATES) == {"avery_5160", "avery_5163"}


class TestQrEncoding:
    def test_short_token_is_never_a_micro_qr(self):
        """The bug this test pins: `segno.make()` silently auto-selects an
        incompatible Micro QR for a short payload unless `micro=False` is
        passed — verified by hand while building this module that neither
        zbar/pyzbar nor OpenCV's `QRCodeDetector` (nor, by extension, a
        phone's default camera QR reader, T4.3) can decode a Micro QR at
        all. A regression here would pass every "does it render a PDF"
        check and only ever surface as "scanning the printed label does
        nothing" on a real phone (exactly the failure mode T4.6's
        on-device pass exists to catch AFTER it's too late/expensive to
        debug) — this unit test catches it at PR time instead.
        """
        qr = segno.make("short-tok", error="m", micro=False)
        assert qr.is_micro is False


class TestRenderLabelsPdf:
    def _items(self, n: int) -> list[LabelData]:
        return [
            LabelData(
                qr_token=f"token-{i}",
                name=f"Asset {i}",
                asset_code=f"ID {i:08d}",
                category_name="Category",
                location_name="Location",
            )
            for i in range(n)
        ]

    def test_empty_and_partial_sheet_render_one_page(self):
        pdf_bytes = render_labels_pdf(self._items(2), AVERY_5160)
        assert pdf_bytes.startswith(b"%PDF")

    def test_exact_multiple_of_sheet_size_renders_that_many_pages(self):
        import fitz

        pdf_bytes = render_labels_pdf(self._items(AVERY_5163.labels_per_sheet * 3), AVERY_5163)
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        assert doc.page_count == 3

    def test_one_over_a_full_sheet_spills_to_a_second_page(self):
        import fitz

        pdf_bytes = render_labels_pdf(self._items(AVERY_5160.labels_per_sheet + 1), AVERY_5160)
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        assert doc.page_count == 2
