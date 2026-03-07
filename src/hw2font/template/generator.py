"""Module A — Template Generator.

Produces a printable US-Letter PDF with:
  • Registration marks (solid black squares) at the four corners.
  • A labelled grid of bounding boxes for every glyph in the character set.
  • Baseline and x-height guide ticks drawn OUTSIDE each box so they are
    visible to the writer but do not create scanning artifacts.
  • Each section (uppercase, lowercase, digits, symbols, ligatures) starts on
    a fresh row with a subtle header for visual grouping.
"""

from __future__ import annotations

import math
from pathlib import Path

from reportlab.lib.colors import Color
from reportlab.pdfgen.canvas import Canvas

from hw2font.constants import (
    BASELINE_FRAC,
    BOX_H,
    BOX_W,
    CELL_W,
    CELLS_PER_PAGE,
    COL_GAP,
    COLS,
    GLYPH_SECTIONS,
    GUIDE_MARGIN,
    LABEL_H,
    LABEL_OVERRIDES,
    MARK_POSITIONS,
    MARK_SIZE,
    MARGIN,
    PAGE_H,
    PAGE_W,
    ROW_GAP,
    ROW_H,
    ROWS_PER_PAGE,
    X_HEIGHT_FRAC,
)

# ── Colours ───────────────────────────────────────────────────────────
_BLACK = Color(0, 0, 0)
_DARK_GRAY = Color(0.30, 0.30, 0.30)
_SECTION_GRAY = Color(0.55, 0.55, 0.55)
_GUIDE_COLOR = Color(0.70, 0.70, 0.70)
_BOX_STROKE = Color(0.25, 0.25, 0.25)
_TICK_LEN = 4  # pt — length of the guide tick marks outside the box


# ── Padded cell list ──────────────────────────────────────────────────

def _build_padded_cells() -> tuple[list[str | None], dict[int, str]]:
    """Pad each section to start on a new row.

    Returns (cells, section_starts) where cells contains glyph strings and
    None for padding slots, and section_starts maps cell-index → section name.
    """
    cells: list[str | None] = []
    section_starts: dict[int, str] = {}

    for name, glyphs in GLYPH_SECTIONS:
        if cells and len(cells) % COLS != 0:
            cells.extend([None] * (COLS - len(cells) % COLS))
        section_starts[len(cells)] = name
        cells.extend(glyphs)

    if len(cells) % COLS != 0:
        cells.extend([None] * (COLS - len(cells) % COLS))

    return cells, section_starts


# ── Drawing helpers ───────────────────────────────────────────────────

def _draw_registration_marks(c: Canvas) -> None:
    c.setFillColor(_BLACK)
    for x, y in MARK_POSITIONS.values():
        c.rect(x, y, MARK_SIZE, MARK_SIZE, fill=1, stroke=0)


def _grid_origin_x() -> float:
    """X offset that horizontally centres the grid on the page."""
    total_w = COLS * CELL_W + (COLS - 1) * COL_GAP
    return (PAGE_W - total_w) / 2


def _cell_xy(col: int, row: int, x0: float) -> tuple[float, float]:
    """Bottom-left corner of a cell's *box* area (inside the guide margins)."""
    cell_x = x0 + col * (CELL_W + COL_GAP)
    box_x = cell_x + GUIDE_MARGIN
    box_y = (PAGE_H - MARGIN) - (row + 1) * ROW_H + ROW_GAP
    return box_x, box_y


def _draw_section_header(c: Canvas, text: str, row: int, x0: float) -> None:
    """Subtle left-aligned section label inside the label area of a row."""
    bx, by = _cell_xy(0, row, x0)
    grid_right = x0 + COLS * CELL_W + (COLS - 1) * COL_GAP

    header_y = by + BOX_H + LABEL_H - 4
    c.setFont("Helvetica-Bold", 6)
    c.setFillColor(_SECTION_GRAY)
    c.drawString(x0, header_y, text.upper())

    rule_y = header_y - 2
    c.setStrokeColor(Color(0.85, 0.85, 0.85))
    c.setLineWidth(0.25)
    c.setDash([])
    c.line(x0, rule_y, grid_right, rule_y)


def _draw_cell(c: Canvas, glyph: str, x: float, y: float) -> None:
    """One glyph cell: label + bordered box + external guide ticks."""
    # ── Label ──
    label = LABEL_OVERRIDES.get(glyph, glyph)
    c.setFont("Helvetica", 6)
    c.setFillColor(_DARK_GRAY)
    c.drawCentredString(x + BOX_W / 2, y + BOX_H + 2, label)

    # ── Box border (solid, dark) ──
    c.setStrokeColor(_BOX_STROKE)
    c.setLineWidth(0.5)
    c.setDash([])
    c.rect(x, y, BOX_W, BOX_H, fill=0, stroke=1)

    # ── External guide ticks ──
    # Short horizontal ticks just outside the left & right edges of the box.
    c.setStrokeColor(_GUIDE_COLOR)
    c.setLineWidth(0.4)
    c.setDash([])

    baseline_y = y + BOX_H * (1 - BASELINE_FRAC)
    xheight_y = y + BOX_H * (1 - X_HEIGHT_FRAC)

    for gy in (baseline_y, xheight_y):
        # Left tick
        c.line(x - GUIDE_MARGIN, gy, x - GUIDE_MARGIN + _TICK_LEN, gy)
        # Right tick
        c.line(x + BOX_W + GUIDE_MARGIN - _TICK_LEN, gy, x + BOX_W + GUIDE_MARGIN, gy)


# ── Public API ────────────────────────────────────────────────────────

def generate_template(output_path: str | Path) -> Path:
    """Generate the handwriting template PDF and return its path."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    cells, section_starts = _build_padded_cells()
    total_pages = math.ceil(len(cells) / CELLS_PER_PAGE)
    x0 = _grid_origin_x()

    c = Canvas(str(output), pagesize=(PAGE_W, PAGE_H))
    c.setTitle("hw2font Handwriting Template")
    c.setAuthor("hw2font")

    for page_idx in range(total_pages):
        start = page_idx * CELLS_PER_PAGE
        page_cells = cells[start : start + CELLS_PER_PAGE]

        _draw_registration_marks(c)

        c.setFont("Helvetica-Bold", 10)
        c.setFillColor(_DARK_GRAY)
        c.drawCentredString(
            PAGE_W / 2,
            PAGE_H - MARGIN + 18,
            f"hw2font Template — Page {page_idx + 1}/{total_pages}",
        )

        for cell_idx, glyph in enumerate(page_cells):
            global_idx = start + cell_idx
            col = cell_idx % COLS
            row = cell_idx // COLS

            if global_idx in section_starts:
                _draw_section_header(c, section_starts[global_idx], row, x0)

            if glyph is None:
                continue

            x, y = _cell_xy(col, row, x0)
            _draw_cell(c, glyph, x, y)

        c.setFont("Helvetica", 7)
        c.setFillColor(_GUIDE_COLOR)
        c.drawCentredString(
            PAGE_W / 2,
            MARGIN - 20,
            "Write each character within its box. "
            "Align letter bases to the baseline ticks. "
            "Print at 100 % scale — do not fit-to-page.",
        )

        c.showPage()

    c.save()
    return output
