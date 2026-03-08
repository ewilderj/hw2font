"""Module B — Computer Vision & Extraction Pipeline.

Processes scanned handwriting template pages:
  1. Detects registration marks → perspective transform (deskew).
  2. Adaptive binarization to separate ink from paper.
  3. Slices the grid into individual glyph images.
  4. Calculates Y-offset (ink bottom → baseline distance).
  5. Tight-crops and exports each glyph image + metadata JSON.

Guide lines are drawn OUTSIDE the boxes in the template, so no guide-
erasure step is needed — the box interior contains only handwriting ink.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

from hw2font.constants import (
    BASELINE_FRAC,
    BOX_H,
    BOX_W,
    CELL_W,
    CELLS_PER_PAGE,
    COL_GAP,
    COLS,
    GUIDE_MARGIN,
    MARK_CENTERS,
    MARK_SIZE,
    MARGIN,
    PAGE_H,
    PAGE_W,
    ROW_GAP,
    ROW_H,
    X_HEIGHT_FRAC,
)

# ── Defaults ──────────────────────────────────────────────────────────
DEFAULT_DPI = 600
_BORDER_INSET_PT = 5.0   # points inset from box edge to skip printed borders
_DESCENDER_EXT_PT = 8.0  # points to extend crop below box bottom for descenders


# ── Data classes ──────────────────────────────────────────────────────

@dataclass
class CellInfo:
    """Position of one glyph cell in image-pixel coords."""
    glyph: str
    page: int
    x: float
    y: float
    w: float
    h: float
    baseline_y: float    # relative to crop-area top
    xheight_y: float     # relative to crop-area top
    box_bottom_y: float  # row where box bottom border sits (from crop top)


@dataclass
class GlyphResult:
    """Metadata written to metadata.json for each extracted glyph."""
    glyph: str
    file: str
    y_offset: float  # px; positive → descender below baseline
    bbox_w: int
    bbox_h: int
    ink_area: int


# ── Coordinate helpers ────────────────────────────────────────────────

def _pt2px(pts: float, dpi: int) -> float:
    return pts * dpi / 72


def _pdf_to_img(x_pt: float, y_pt: float, dpi: int) -> tuple[float, float]:
    """PDF coords (origin bottom-left) → image coords (origin top-left)."""
    return _pt2px(x_pt, dpi), _pt2px(PAGE_H - y_pt, dpi)


def _expected_mark_centers(dpi: int) -> dict[str, tuple[float, float]]:
    return {k: _pdf_to_img(*v, dpi) for k, v in MARK_CENTERS.items()}


# ── Cell layout (mirrors generator grid logic) ────────────────────────

def _build_cell_layout(dpi: int) -> list[CellInfo]:
    """Compute image-pixel coordinates for every glyph cell across all pages."""
    from hw2font.template.generator import _build_padded_cells, _grid_origin_x

    pt2px = dpi / 72
    inset = _BORDER_INSET_PT * pt2px
    cells, _ = _build_padded_cells()
    x0 = _grid_origin_x()
    n_pages = -(-len(cells) // CELLS_PER_PAGE)

    layout: list[CellInfo] = []
    for pg in range(n_pages):
        start = pg * CELLS_PER_PAGE
        for idx, glyph in enumerate(cells[start : start + CELLS_PER_PAGE]):
            if glyph is None:
                continue
            col, row = idx % COLS, idx // COLS

            # PDF coords → box bottom-left (accounting for guide margins)
            cell_x = x0 + col * (CELL_W + COL_GAP)
            bx = cell_x + GUIDE_MARGIN
            by = (PAGE_H - MARGIN) - (row + 1) * ROW_H + ROW_GAP

            # Image coords (top-left of the inset crop area)
            ix = bx * pt2px + inset
            iy = (PAGE_H - (by + BOX_H)) * pt2px + inset
            iw = BOX_W * pt2px - 2 * inset
            # Extend crop downward to capture descenders below the box
            desc_ext = _DESCENDER_EXT_PT * pt2px
            ih = BOX_H * pt2px - inset + desc_ext  # inset at top only

            # Guide-line positions relative to the crop-area top
            bl_y = BASELINE_FRAC * BOX_H * pt2px - inset
            xh_y = X_HEIGHT_FRAC * BOX_H * pt2px - inset
            # Box bottom edge position relative to crop-area top
            box_bot_y = BOX_H * pt2px - inset

            layout.append(CellInfo(glyph, pg, ix, iy, iw, ih, bl_y, xh_y, box_bot_y))

    return layout


# ── Registration-mark detection ───────────────────────────────────────

def _detect_marks(gray: np.ndarray, dpi: int) -> dict[str, tuple[float, float]]:
    """Find the four solid-square registration marks in a grayscale scan."""
    _, thresh = cv2.threshold(gray, 80, 255, cv2.THRESH_BINARY_INV)
    contours, _ = cv2.findContours(
        thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
    )

    mark_px = MARK_SIZE * dpi / 72
    lo, hi = (mark_px * 0.3) ** 2, (mark_px * 3.0) ** 2

    cands: list[tuple[float, float]] = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if lo < area < hi:
            _, _, w, h = cv2.boundingRect(cnt)
            if 0.5 < w / max(h, 1) < 2.0:
                M = cv2.moments(cnt)
                if M["m00"] > 0:
                    cands.append((M["m10"] / M["m00"], M["m01"] / M["m00"]))

    if len(cands) < 4:
        raise RuntimeError(
            f"Found only {len(cands)} registration mark(s), need 4. "
            "Ensure all four corner squares are fully visible in the scan."
        )

    # Assign each candidate to the nearest image corner
    ih, iw = gray.shape[:2]
    corner_pts = {
        "top_left": (0, 0),
        "top_right": (iw, 0),
        "bottom_left": (0, ih),
        "bottom_right": (iw, ih),
    }
    marks: dict[str, tuple[float, float]] = {}
    used: set[int] = set()
    for name, (cx, cy) in corner_pts.items():
        best_i, best_d = -1, float("inf")
        for i, (px, py) in enumerate(cands):
            if i not in used:
                d = (px - cx) ** 2 + (py - cy) ** 2
                if d < best_d:
                    best_i, best_d = i, d
        marks[name] = cands[best_i]
        used.add(best_i)

    return marks


# ── Perspective transform ─────────────────────────────────────────────

def _deskew(
    gray: np.ndarray,
    marks: dict[str, tuple[float, float]],
    dpi: int,
) -> np.ndarray:
    """Warp the scan so registration marks land at their expected positions."""
    expected = _expected_mark_centers(dpi)
    order = ["top_left", "top_right", "bottom_left", "bottom_right"]

    src = np.float32([marks[k] for k in order])
    dst = np.float32([expected[k] for k in order])

    out_w = int(round(PAGE_W * dpi / 72))
    out_h = int(round(PAGE_H * dpi / 72))

    M = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(gray, M, (out_w, out_h), flags=cv2.INTER_LINEAR)


# ── Binarization ──────────────────────────────────────────────────────

def _binarize(gray_cell: np.ndarray, dpi: int) -> np.ndarray:
    """Hybrid threshold: Otsu (solid strokes) + adaptive (thin edges).

    Adaptive thresholding alone hollows out thick pen strokes because
    the center of a dark stroke doesn't differ from its local mean.
    A global Otsu threshold catches those solid interiors; the adaptive
    pass catches fine strokes and faint edges.  We union both results.
    """
    ksize = max(3, int(round(0.4 * dpi / 72)) | 1)
    blurred = cv2.GaussianBlur(gray_cell, (ksize, ksize), 0)

    # Global Otsu — fills solid dark areas reliably
    _, otsu = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)

    # Adaptive — catches thin strokes and faint ink the global misses
    block = max(11, int(round(2.5 * dpi / 72)) | 1)
    adaptive = cv2.adaptiveThreshold(
        blurred, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        blockSize=block,
        C=15,
    )

    # Union: pixel is ink if EITHER method says so
    binary = cv2.bitwise_or(otsu, adaptive)

    kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kern, iterations=1)

    return binary


def _process_cell(
    gray_cell: np.ndarray,
    baseline_y: float,
    box_bottom_y: float,
    dpi: int,
) -> tuple[np.ndarray | None, float, int]:
    """Binarize → erase box border → denoise → measure → crop.

    Returns (cropped_binary | None, y_offset_px, ink_area).
    ``y_offset`` is positive when ink extends below the baseline (descender).
    ``box_bottom_y`` is the row (from crop top) where the box border sits,
    used to erase it from the extended descender zone.
    """
    binary = _binarize(gray_cell, dpi)

    # Erase the bottom box border. The printed border is a thin full-width line
    # near box_bottom_y, but printer scaling can offset it by up to ~30px.
    # Use two passes: tight band with moderate threshold, then wider with strict.
    border_band = max(8, int(round(1.5 * dpi / 72)))  # ~12 px @ 600 DPI
    w = binary.shape[1]
    # Pass 1: narrow band, moderate threshold
    search_lo = max(0, int(box_bottom_y) - border_band)
    search_hi = min(binary.shape[0], int(box_bottom_y) + border_band)
    for row_idx in range(search_lo, search_hi):
        if np.count_nonzero(binary[row_idx]) > w * 0.5:
            binary[row_idx] = 0
    # Pass 2: wider band into descender zone, strict threshold (only printed lines)
    desc_search_hi = min(binary.shape[0], int(box_bottom_y) + int(6 * dpi / 72))
    for row_idx in range(search_hi, desc_search_hi):
        if np.count_nonzero(binary[row_idx]) > w * 0.8:
            binary[row_idx] = 0

    # Remove small connected components (noise specks)
    min_cc_area = max(50, int(round(0.6 * dpi / 72)) ** 2)
    n_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
        binary, connectivity=8,
    )
    # Find the largest component (the actual glyph stroke)
    areas = [stats[i, cv2.CC_STAT_AREA] for i in range(1, n_labels)]
    if not areas:
        return None, 0.0, 0
    largest_idx = 1 + int(np.argmax(areas))
    largest_top = stats[largest_idx, cv2.CC_STAT_TOP]
    largest_bot = largest_top + stats[largest_idx, cv2.CC_STAT_HEIGHT]
    largest_area = stats[largest_idx, cv2.CC_STAT_AREA]

    for i in range(1, n_labels):
        if stats[i, cv2.CC_STAT_AREA] < min_cc_area:
            binary[labels == i] = 0
        elif i != largest_idx:
            comp_top = stats[i, cv2.CC_STAT_TOP]
            cy = centroids[i][1]
            cell_h = binary.shape[0]

            # Components starting at or below box_bottom_y are printed
            # labels / headers from the next grid row, not handwriting.
            if comp_top >= box_bottom_y:
                binary[labels == i] = 0
            # Remove small components near the cell border (bottom 15%).
            # These are border/noise fragments after erasure.
            # Components closer to the main body are kept — they could be
            # dots on glyphs like ? ! ; i j
            elif cy > cell_h * 0.85 and \
                 stats[i, cv2.CC_STAT_AREA] < largest_area * 0.15:
                binary[labels == i] = 0

    ink_area = int(np.count_nonzero(binary))
    if ink_area < binary.size * 0.001:
        return None, 0.0, 0  # empty cell

    coords = cv2.findNonZero(binary)
    bx, by, bw, bh = cv2.boundingRect(coords)

    ink_bottom = by + bh
    y_offset = float(ink_bottom - baseline_y)

    return binary[by : by + bh, bx : bx + bw], y_offset, ink_area


# ── Filename helper ───────────────────────────────────────────────────

def _glyph_filename(glyph: str) -> str:
    """Filesystem-safe filename for a glyph."""
    if len(glyph) == 1:
        cp = ord(glyph)
        suffix = f"_{glyph}" if glyph.isalnum() else ""
        return f"U+{cp:04X}{suffix}.png"
    return f"lig_{glyph}.png"


# ── Public API ────────────────────────────────────────────────────────

def extract_glyphs(
    scan_paths: list[str | Path],
    output_dir: str | Path = "output/extracted",
    dpi: int = DEFAULT_DPI,
) -> tuple[Path, dict[str, int]]:
    """Run the full extraction pipeline on scanned template page(s).

    Parameters
    ----------
    scan_paths : one image per template page, in order (page 1, page 2, …).
    output_dir : where to write glyph images and ``metadata.json``.
    dpi : scan resolution (must match the actual scan).

    Returns
    -------
    (output_dir, stats) where stats has keys ``processed`` and ``empty``.
    """
    out = Path(output_dir)
    glyph_dir = out / "glyphs"
    glyph_dir.mkdir(parents=True, exist_ok=True)

    layout = _build_cell_layout(dpi)

    pages: dict[int, list[CellInfo]] = {}
    for c in layout:
        pages.setdefault(c.page, []).append(c)

    if len(scan_paths) < len(pages):
        raise ValueError(
            f"Template has {len(pages)} page(s) but only "
            f"{len(scan_paths)} scan(s) provided."
        )

    metadata: dict[str, dict] = {}
    stats = {"processed": 0, "empty": 0}

    for pg_idx, spath in enumerate(scan_paths):
        spath = Path(spath)
        img = cv2.imread(str(spath), cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise FileNotFoundError(f"Cannot read image: {spath}")

        marks = _detect_marks(img, dpi)
        warped = _deskew(img, marks, dpi)

        for cell in pages.get(pg_idx, []):
            x, y = int(round(cell.x)), int(round(cell.y))
            w, h = int(round(cell.w)), int(round(cell.h))
            cell_img = warped[y : y + h, x : x + w].copy()

            cropped, y_off, ink = _process_cell(
                cell_img, cell.baseline_y, cell.box_bottom_y, dpi,
            )
            if cropped is None:
                stats["empty"] += 1
                continue

            fname = _glyph_filename(cell.glyph)
            cv2.imwrite(str(glyph_dir / fname), cropped)

            metadata[cell.glyph] = asdict(GlyphResult(
                glyph=cell.glyph,
                file=fname,
                y_offset=round(y_off, 1),
                bbox_w=cropped.shape[1],
                bbox_h=cropped.shape[0],
                ink_area=ink,
            ))
            stats["processed"] += 1

    (out / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
    )

    return out, stats
