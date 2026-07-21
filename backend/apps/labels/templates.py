"""Avery-style sheet template geometry (T4.5).

**Defaults per the task doc's documented default (Q9 unanswered in
`docs/risks.md` §3):** `avery_5160` and `avery_5163`, US Letter (8.5in x
11in) only for MVP. Every dimension below is in inches (WeasyPrint's CSS
accepts `in` units directly, so no unit conversion happens at render time —
`rendering.py` interpolates these numbers straight into the generated CSS).

Real-world Avery 5160/5163 sheet geometry (verified against Avery's own
published templates, not just the task doc's simplified "no gutter" note):
- **5160** (1in x 2⅝in, 3 cols x 10 rows = 30/sheet): 0.1875in side margins +
  a 0.125in horizontal gutter between columns is what actually centers 3 x
  2.625in labels under an 8.5in sheet (`3*2.625 + 2*0.125 + 2*0.1875 = 8.5`);
  0.5in top/bottom margins with NO vertical gutter exactly fits 10 x 1in rows
  (`10*1.0 + 2*0.5 = 11`). A zero horizontal gutter (the task doc's literal
  "no gutter" note applied to both axes) would misprint real 5160 stock, so
  this template keeps the small real horizontal gutter Avery actually cuts
  the sheet at.
- **5163** (2in x 4in, 2 cols x 5 rows = 10/sheet): 0.25in side margins with
  NO horizontal gutter fits 2 x 4in columns (`2*4 + 2*0.25 = 8.5`); 0.5in
  top/bottom margins with no vertical gutter fits 5 x 2in rows
  (`5*2.0 + 2*0.5 = 11`) — this one genuinely has no gutter either way,
  matching the task doc's note.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SheetTemplate:
    key: str
    label: str
    page_width_in: float
    page_height_in: float
    cols: int
    rows: int
    label_width_in: float
    label_height_in: float
    margin_top_in: float
    margin_left_in: float
    gutter_x_in: float
    gutter_y_in: float

    @property
    def labels_per_sheet(self) -> int:
        return self.cols * self.rows


AVERY_5160 = SheetTemplate(
    key="avery_5160",
    label='Avery 5160 (1" x 2⅝", 30/sheet)',
    page_width_in=8.5,
    page_height_in=11.0,
    cols=3,
    rows=10,
    label_width_in=2.625,
    label_height_in=1.0,
    margin_top_in=0.5,
    margin_left_in=0.1875,
    gutter_x_in=0.125,
    gutter_y_in=0.0,
)

AVERY_5163 = SheetTemplate(
    key="avery_5163",
    label='Avery 5163 (2" x 4", 10/sheet)',
    page_width_in=8.5,
    page_height_in=11.0,
    cols=2,
    rows=5,
    label_width_in=4.0,
    label_height_in=2.0,
    margin_top_in=0.5,
    margin_left_in=0.25,
    gutter_x_in=0.0,
    gutter_y_in=0.0,
)

SHEET_TEMPLATES: dict[str, SheetTemplate] = {
    AVERY_5160.key: AVERY_5160,
    AVERY_5163.key: AVERY_5163,
}

DEFAULT_TEMPLATE_KEY = AVERY_5160.key
