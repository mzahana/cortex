"""Shared PDF post-processing: page stamping, bookmarks, watermarks (M8 §6.4).

Every audit artifact this codebase produces — the expense pack, the project
report, the audit-readiness checklist — needs the same three finishing touches,
so they live here once rather than three times:

- **Page numbering** (`Page N of M`) applied AFTER any scans are merged, which
  is what lets a ledger line say "receipt at page 7" and have that resolve.
- **Bookmarks**, so a 60-page pack is navigable instead of a scroll.
- **A DRAFT watermark**, for a document that is not the final signed-off copy.

Every function here is **best-effort**: a failure returns the input bytes
unchanged rather than losing a document that has already been rendered. A
missing page number is a blemish; a lost report is an incident.
"""

from __future__ import annotations

FOOTER_FONT_SIZE = 7
FOOTER_COLOR = (0.42, 0.42, 0.42)


def stamp_pages(pdf_bytes: bytes, *, footer: str = "") -> bytes:
    """Number every page `Page N of M`, prefixed by an optional provenance
    footer (who generated it, when).

    Applied after merging so the numbering covers appended scans too.
    """
    try:
        import fitz

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        total = doc.page_count
        for index, page in enumerate(doc, start=1):
            label = f"Page {index} of {total}"
            if footer:
                label = f"{footer}   ·   {label}"
            page.insert_text(
                fitz.Point(36, page.rect.height - 18),
                label,
                fontsize=FOOTER_FONT_SIZE,
                color=FOOTER_COLOR,
            )
        out = doc.tobytes()
        doc.close()
        return out
    except Exception:
        return pdf_bytes


def set_bookmarks(pdf_bytes: bytes, entries: list[tuple[int, str, int]]) -> bytes:
    """Attach a PDF outline. `entries` are `(level, title, page_number)`,
    1-based, in document order — fitz's own `set_toc` shape.

    Out-of-range pages are clamped rather than rejected: a bookmark pointing
    one page past the end should not cost the reader the whole outline.
    """
    if not entries:
        return pdf_bytes
    try:
        import fitz

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        last = max(1, doc.page_count)
        toc = [[level, title, min(max(1, page), last)] for level, title, page in entries]
        doc.set_toc(toc)
        out = doc.tobytes()
        doc.close()
        return out
    except Exception:
        return pdf_bytes


def watermark(pdf_bytes: bytes, text: str) -> bytes:
    """Diagonal grey text across every page — for DRAFT and similar.

    Drawn beneath nothing and above everything: it is deliberately obvious,
    because the entire purpose is that nobody mistakes the document for the
    final copy.
    """
    if not text:
        return pdf_bytes
    try:
        import fitz

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for page in doc:
            rect = page.rect
            page.insert_textbox(
                fitz.Rect(0, rect.height / 2 - 60, rect.width, rect.height / 2 + 60),
                text,
                fontsize=54,
                color=(0.85, 0.85, 0.85),
                align=1,
                rotate=0,
                overlay=True,
            )
        out = doc.tobytes()
        doc.close()
        return out
    except Exception:
        return pdf_bytes
