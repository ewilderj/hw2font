"""Shared layout constants for template generation and scan extraction.

All measurements in PDF points (1 point = 1/72 inch).
The coordinate system origin is the bottom-left corner of the page.
"""

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch

# ── Page ──────────────────────────────────────────────────────────────
PAGE_W, PAGE_H = LETTER  # 612 × 792 pt  (8.5 × 11 in)
MARGIN = 0.75 * inch     # 54 pt — clear zone around grid

# ── Registration marks ────────────────────────────────────────────────
# Solid black squares at each page corner for perspective-transform detection.
MARK_SIZE = 0.25 * inch   # 18 pt side length
MARK_INSET = 0.2 * inch   # 14.4 pt from page edge to mark edge

# Mark corner positions (bottom-left corner of each square, PDF coords)
MARK_POSITIONS = {
    "top_left":     (MARK_INSET, PAGE_H - MARK_INSET - MARK_SIZE),
    "top_right":    (PAGE_W - MARK_INSET - MARK_SIZE, PAGE_H - MARK_INSET - MARK_SIZE),
    "bottom_left":  (MARK_INSET, MARK_INSET),
    "bottom_right": (PAGE_W - MARK_INSET - MARK_SIZE, MARK_INSET),
}

# Mark centers (used as control points for perspective transform)
MARK_CENTERS = {
    k: (x + MARK_SIZE / 2, y + MARK_SIZE / 2)
    for k, (x, y) in MARK_POSITIONS.items()
}

# ── Grid ──────────────────────────────────────────────────────────────
COLS = 10
COL_GAP = 4       # pt between columns
ROW_GAP = 4       # pt between rows
LABEL_H = 10      # pt reserved above each box for the character label
GUIDE_MARGIN = 6  # pt on each side of the box for external guide ticks

# Guide lines expressed as fractions from the *top* of the logical writing area.
# The writing area height equals BOX_H; guides are drawn OUTSIDE the box.
# BASELINE: 0.75 → 25 % up from box floor (descenders drop below this).
# X_HEIGHT: 0.35 → 65 % up from box floor (top of lowercase a, c, e, x, etc.).
BASELINE_FRAC = 0.75
X_HEIGHT_FRAC = 0.35

# ── Character set ─────────────────────────────────────────────────────
UPPERCASE = [chr(i) for i in range(0x41, 0x5B)]      # A-Z  (26)
LOWERCASE = [chr(i) for i in range(0x61, 0x7B)]      # a-z  (26)
DIGITS    = [chr(i) for i in range(0x30, 0x3A)]      # 0-9  (10)
SYMBOLS   = list("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")  # 32 ASCII
EXTRA_SYMBOLS = ["£", "°", "♥", "☺"]                     # GBP, degree, heart, smiley
# Display overrides for glyphs whose Unicode char won't render in Helvetica
LABEL_OVERRIDES = {"☺": "smiley", "♥": "heart"}
LIGATURES = [
    "fi", "fl", "ff", "tt", "th",
    "oo", "ee", "ll", "ss", "mm",
    "nn", "or", "os", "ve", "we", "br",
]  # 16
EXTRA_LIGATURES = [
    "ing", "tion", "an", "en", "er",
    "es", "ed", "re", "st", "qu",
]  # 10

# Grouped sections — each is padded to a full row for clean layout.
GLYPH_SECTIONS = [
    ("Uppercase",       UPPERCASE),
    ("Lowercase",       LOWERCASE),
    ("Digits",          DIGITS),
    ("Symbols",         SYMBOLS + EXTRA_SYMBOLS),
    ("Ligatures",       LIGATURES + EXTRA_LIGATURES),
]

ALL_GLYPHS = UPPERCASE + LOWERCASE + DIGITS + SYMBOLS + EXTRA_SYMBOLS + LIGATURES + EXTRA_LIGATURES

# ── Derived grid geometry ─────────────────────────────────────────────
GRID_W = PAGE_W - 2 * MARGIN
GRID_H = PAGE_H - 2 * MARGIN

BOX_W = (GRID_W - (COLS - 1) * (COL_GAP + 2 * GUIDE_MARGIN)) / COLS
BOX_H = BOX_W * 1.2           # slightly taller than wide for natural letter proportions
CELL_W = BOX_W + 2 * GUIDE_MARGIN  # box + guide tick margins on each side
ROW_H = LABEL_H + BOX_H + ROW_GAP
ROWS_PER_PAGE = int(GRID_H / ROW_H)
CELLS_PER_PAGE = COLS * ROWS_PER_PAGE
