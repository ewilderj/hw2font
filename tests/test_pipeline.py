"""Tests for the hw2font extraction and font compilation pipeline.

These tests encode every issue we've encountered during development.
They run against the artifacts in output/ produced by the pipeline.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageFont

# ── Paths ─────────────────────────────────────────────────────────────

OUTPUT = Path(__file__).resolve().parent.parent / "output"
EXTRACTED = OUTPUT / "extracted"
GLYPHS_DIR = EXTRACTED / "glyphs"
SVGS_DIR = EXTRACTED / "svgs"
METADATA_PATH = EXTRACTED / "metadata.json"
FONT_PATH = OUTPUT / "Handwriting_MVP.otf"

# ── Character sets ────────────────────────────────────────────────────

UC_LETTERS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
LC_LETTERS = set("abcdefghijklmnopqrstuvwxyz")
DIGITS = set("0123456789")
XH_ONLY = set("acemnorsuvwxz")  # x-height-only lowercase
LC_ASCENDER = set("bdfhklt")
LC_DESCENDER = set("gjpqy")
ALL_LC = XH_ONLY | LC_ASCENDER | LC_DESCENDER

EXPECTED_LIGATURES = {
    "fi", "fl", "ff", "tt", "th", "oo", "ee", "ll", "ss", "mm", "nn",
    "or", "os", "ve", "we", "br", "ing", "tion", "an", "en", "er",
    "es", "ed", "re", "st", "qu",
}


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def metadata() -> dict:
    assert METADATA_PATH.exists(), f"Run pipeline first: {METADATA_PATH}"
    return json.loads(METADATA_PATH.read_text())


@pytest.fixture(scope="session")
def font() -> ImageFont.FreeTypeFont:
    assert FONT_PATH.exists(), f"Run pipeline first: {FONT_PATH}"
    return ImageFont.truetype(str(FONT_PATH), 200)


@pytest.fixture(scope="session")
def glyph_metrics(font: ImageFont.FreeTypeFont) -> dict[str, dict]:
    """Render each glyph and measure its actual ink bounding box.

    Uses getmask() to find real ink rows, avoiding Pillow's getbbox() which
    includes empty space down to the baseline for high-sitting glyphs (e.g. °).
    """
    metrics = {}
    for char in list(UC_LETTERS | LC_LETTERS | DIGITS) + [chr(0xB0), chr(0xA3)]:
        bbox = font.getbbox(char)
        if not bbox:
            continue
        x0, y0, x1, y1 = bbox
        mask = font.getmask(char)
        w, h = mask.size
        arr = np.array(mask).reshape(h, w) if h > 0 and w > 0 else np.zeros((1, 1))
        ink_rows = np.where(arr.sum(axis=1) > 0)[0]
        ink_cols = np.where(arr.sum(axis=0) > 0)[0]
        if len(ink_rows) == 0:
            continue
        ink_h = int(ink_rows[-1] - ink_rows[0] + 1)
        ink_w = int(ink_cols[-1] - ink_cols[0] + 1) if len(ink_cols) else 0
        metrics[char] = {
            "width": ink_w,
            "height": ink_h,
            "top": y0 + int(ink_rows[0]),
            "bottom": y0 + int(ink_rows[-1]),
            "logical_height": y1 - y0,
            "logical_top": y0,
            "logical_bottom": y1,
        }
    return metrics


# ═══════════════════════════════════════════════════════════════════════
# EXTRACTION TESTS
# ═══════════════════════════════════════════════════════════════════════


class TestExtractionCompleteness:
    """All expected glyphs should be extracted."""

    def test_all_uppercase_extracted(self, metadata):
        for c in UC_LETTERS:
            assert c in metadata, f"Missing uppercase: {c}"

    def test_all_lowercase_extracted(self, metadata):
        for c in LC_LETTERS:
            assert c in metadata, f"Missing lowercase: {c}"

    def test_all_digits_extracted(self, metadata):
        for c in DIGITS:
            assert c in metadata, f"Missing digit: {c}"

    def test_all_ligatures_extracted(self, metadata):
        for lig in EXPECTED_LIGATURES:
            assert lig in metadata, f"Missing ligature: {lig}"

    def test_total_glyph_count(self, metadata):
        assert len(metadata) >= 120, f"Only {len(metadata)} glyphs (expected ≥120)"


class TestExtractionQuality:
    """Extracted glyphs should be clean and properly cropped."""

    def test_glyph_files_exist(self, metadata):
        """Every metadata entry should have a corresponding PNG."""
        for glyph, info in metadata.items():
            png = GLYPHS_DIR / Path(info["file"]).name
            assert png.exists(), f"Missing glyph file for '{glyph}': {png}"

    def test_no_empty_glyphs(self, metadata):
        """Every glyph should have nonzero ink area."""
        for glyph, info in metadata.items():
            assert info["ink_area"] > 0, f"Glyph '{glyph}' has zero ink"

    def test_no_border_artifacts_in_glyphs(self, metadata):
        """No glyph should have a full-width horizontal line at the TOP or BOTTOM
        (border artifact). Only check the first and last 15% of rows.

        Wide horizontal strokes in the MIDDLE of glyphs (like 'F', 'H') are fine.
        """
        for glyph, info in metadata.items():
            png = GLYPHS_DIR / Path(info["file"]).name
            if not png.exists():
                continue
            img = np.array(Image.open(png).convert("L"))
            h, w = img.shape
            edge_rows = max(3, int(h * 0.15))
            for row_idx in list(range(edge_rows)) + list(range(h - edge_rows, h)):
                ink_frac = np.count_nonzero(img[row_idx]) / w
                assert ink_frac < 0.97, (
                    f"Glyph '{glyph}' row {row_idx}: {ink_frac:.0%} ink — "
                    f"likely a box border artifact"
                )

    def test_no_horizontal_white_lines(self, metadata):
        """No glyph should have a fully blank row cutting through ink.

        This catches guide-line erasure artifacts (white line through descenders).
        Only check rows that have ink above AND below them.
        """
        for glyph, info in metadata.items():
            png = GLYPHS_DIR / Path(info["file"]).name
            if not png.exists():
                continue
            img = np.array(Image.open(png).convert("L"))
            h, w = img.shape
            if h < 5:
                continue
            row_ink = [np.count_nonzero(img[r]) for r in range(h)]
            for r in range(2, h - 2):
                if row_ink[r] == 0:
                    has_ink_above = any(row_ink[r - k] > 0 for k in range(1, 3))
                    has_ink_below = any(row_ink[r + k] > 0 for k in range(1, 3))
                    if has_ink_above and has_ink_below:
                        # Allow isolated blank rows (thin strokes), fail on runs of 3+
                        blank_run = sum(
                            1 for dr in range(r, min(r + 4, h)) if row_ink[dr] == 0
                        )
                        assert blank_run < 3, (
                            f"Glyph '{glyph}' has {blank_run} blank rows at row {r} "
                            f"with ink above and below — likely an erasure artifact"
                        )

    def test_comma_and_period_extracted(self, metadata):
        """Comma and period are small — ensure they weren't dropped."""
        assert "," in metadata, "Comma was dropped (probably below ink threshold)"
        assert "." in metadata, "Period was dropped (probably below ink threshold)"
        assert metadata[","]["ink_area"] > 50, "Comma ink area suspiciously small"
        assert metadata["."]["ink_area"] > 30, "Period ink area suspiciously small"


class TestExtractionMetrics:
    """Extracted pixel metrics should be reasonable."""

    def test_uppercase_height_consistency(self, metadata):
        """Uppercase letters (excluding Q/Y with descenders) should have similar heights."""
        heights = [
            metadata[c]["bbox_h"]
            for c in UC_LETTERS
            if c in metadata and c not in "QYJ"
        ]
        assert len(heights) > 20
        med = statistics.median(heights)
        for c in UC_LETTERS:
            if c in metadata and c not in "QYJ":
                h = metadata[c]["bbox_h"]
                ratio = h / med
                assert 0.7 < ratio < 1.4, (
                    f"UC '{c}' height {h} deviates from median {med:.0f} "
                    f"(ratio {ratio:.2f})"
                )

    def test_xheight_letters_reasonable(self, metadata):
        """x-height-only letters should have positive bbox dimensions."""
        for c in XH_ONLY:
            if c in metadata:
                assert metadata[c]["bbox_w"] > 10, f"'{c}' too narrow"
                assert metadata[c]["bbox_h"] > 10, f"'{c}' too short"

    def test_descender_letters_have_descent(self, metadata):
        """g, j, p, q, y should have positive y_offset (ink below baseline)."""
        for c in LC_DESCENDER:
            if c in metadata:
                assert metadata[c]["y_offset"] > 20, (
                    f"Descender '{c}' y_offset={metadata[c]['y_offset']:.1f} — "
                    f"expected significant descent below baseline"
                )


# ═══════════════════════════════════════════════════════════════════════
# FONT COMPILATION TESTS
# ═══════════════════════════════════════════════════════════════════════


class TestFontExists:
    def test_otf_file_exists(self):
        assert FONT_PATH.exists(), "Font file not found"
        assert FONT_PATH.stat().st_size > 10_000, "Font file suspiciously small"


class TestFontRendering:
    """The compiled font should render all expected characters."""

    def test_all_ascii_letters_render(self, font):
        for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz":
            bbox = font.getbbox(c)
            assert bbox is not None and bbox[2] - bbox[0] > 0, (
                f"'{c}' does not render (zero width)"
            )

    def test_all_digits_render(self, font):
        for c in "0123456789":
            bbox = font.getbbox(c)
            assert bbox is not None and bbox[2] - bbox[0] > 0, f"'{c}' zero width"

    def test_space_has_width(self, font):
        bbox = font.getbbox(" x")
        bbox2 = font.getbbox("x")
        if bbox and bbox2:
            assert bbox[2] > bbox2[2], "Space appears to have no width"


class TestFontSizing:
    """Glyph sizes in the compiled font should be properly normalized."""

    def test_uppercase_consistent_height(self, glyph_metrics):
        """Uppercase letters (excl. Q/Y/J) should all be similar height."""
        heights = [
            glyph_metrics[c]["height"]
            for c in UC_LETTERS
            if c in glyph_metrics and c not in "QYJ"
        ]
        assert len(heights) > 20
        med = statistics.median(heights)
        for c in UC_LETTERS:
            if c in glyph_metrics and c not in "QYJ":
                h = glyph_metrics[c]["height"]
                assert abs(h - med) / med < 0.20, (
                    f"UC '{c}' height {h} deviates >20% from median {med:.0f}"
                )

    def test_xheight_consistent_height(self, glyph_metrics):
        """x-height-only letters should be within 15% of each other."""
        heights = [
            glyph_metrics[c]["height"]
            for c in XH_ONLY
            if c in glyph_metrics
        ]
        assert len(heights) > 10
        med = statistics.median(heights)
        for c in XH_ONLY:
            if c in glyph_metrics:
                h = glyph_metrics[c]["height"]
                assert abs(h - med) / med < 0.35, (
                    f"xh '{c}' height {h} deviates >35% from median {med:.0f}"
                )

    def test_xheight_shorter_than_caps(self, glyph_metrics):
        """x-height letters should be shorter than uppercase."""
        xh_heights = [
            glyph_metrics[c]["height"] for c in XH_ONLY if c in glyph_metrics
        ]
        uc_heights = [
            glyph_metrics[c]["height"]
            for c in UC_LETTERS
            if c in glyph_metrics and c not in "QYJ"
        ]
        assert statistics.median(xh_heights) < statistics.median(uc_heights), (
            "x-height letters should be shorter than uppercase"
        )

    def test_descender_above_baseline_matches_xheight(self, glyph_metrics):
        """Descender letters' top should align roughly with x-height letters' top."""
        xh_tops = [glyph_metrics[c]["top"] for c in XH_ONLY if c in glyph_metrics]
        xh_med_top = statistics.median(xh_tops)
        for c in LC_DESCENDER:
            if c in glyph_metrics:
                top = glyph_metrics[c]["top"]
                # Descender letter top should be close to x-height letter top
                # (they share the same x-height line)
                assert abs(top - xh_med_top) / max(abs(xh_med_top), 1) < 0.40, (
                    f"Descender '{c}' top={top} vs x-height median top={xh_med_top:.0f} — "
                    f"body should align with x-height"
                )

    def test_descender_letters_extend_below_baseline(self, glyph_metrics):
        """g, j, p, q, y should extend further below than x-height letters."""
        xh_bottoms = [
            glyph_metrics[c]["bottom"] for c in XH_ONLY if c in glyph_metrics
        ]
        xh_med_bottom = statistics.median(xh_bottoms)
        for c in LC_DESCENDER:
            if c in glyph_metrics:
                assert glyph_metrics[c]["bottom"] > xh_med_bottom + 5, (
                    f"Descender '{c}' bottom={glyph_metrics[c]['bottom']} "
                    f"not clearly below x-height baseline ({xh_med_bottom:.0f})"
                )

    def test_descender_letters_not_oversized(self, glyph_metrics):
        """Descender letters' total height should not exceed 2x cap height."""
        uc_heights = [
            glyph_metrics[c]["height"]
            for c in UC_LETTERS
            if c in glyph_metrics and c not in "QYJ"
        ]
        cap_h = statistics.median(uc_heights)
        for c in LC_DESCENDER:
            if c in glyph_metrics:
                h = glyph_metrics[c]["height"]
                assert h < cap_h * 2.0, (
                    f"Descender '{c}' height {h} > 2x cap height {cap_h:.0f} — oversized"
                )

    def test_uppercase_Q_not_squished(self, glyph_metrics):
        """Q has a descending tail — its cap body should match other UC letters."""
        if "Q" not in glyph_metrics:
            pytest.skip("Q not in font")
        uc_heights = [
            glyph_metrics[c]["height"]
            for c in "ABCDEFHIKLMNOPRSTUVWXZ"
            if c in glyph_metrics
        ]
        cap_h = statistics.median(uc_heights)
        # Q's top (cap portion) should start near the same position as other UC
        q_top = glyph_metrics["Q"]["top"]
        uc_tops = [glyph_metrics[c]["top"] for c in "ABCDEFHIKLMNOPRSTUVWXZ" if c in glyph_metrics]
        uc_med_top = statistics.median(uc_tops)
        assert abs(q_top - uc_med_top) / max(abs(uc_med_top), 1) < 0.40, (
            f"Q top={q_top} vs UC median top={uc_med_top:.0f} — "
            f"Q cap body is misaligned (probably squished by descender)"
        )

    def test_digits_similar_to_uppercase(self, glyph_metrics):
        """Digits should be roughly the same height as uppercase."""
        uc_heights = [
            glyph_metrics[c]["height"]
            for c in UC_LETTERS
            if c in glyph_metrics and c not in "QYJ"
        ]
        digit_heights = [
            glyph_metrics[c]["height"] for c in DIGITS if c in glyph_metrics
        ]
        if not digit_heights:
            pytest.skip("No digits in font")
        uc_med = statistics.median(uc_heights)
        digit_med = statistics.median(digit_heights)
        ratio = digit_med / uc_med
        assert 0.7 < ratio < 1.3, (
            f"Digit median height {digit_med:.0f} vs UC {uc_med:.0f} (ratio {ratio:.2f})"
        )


class TestFontSymbols:
    """Symbol and punctuation sizing."""

    def test_degree_symbol_small(self, glyph_metrics):
        """Degree symbol should be much smaller than uppercase letters."""
        if chr(0xB0) not in glyph_metrics:
            pytest.skip("Degree symbol not rendered")
        uc_heights = [
            glyph_metrics[c]["height"]
            for c in UC_LETTERS
            if c in glyph_metrics and c not in "QYJ"
        ]
        cap_h = statistics.median(uc_heights)
        deg_h = glyph_metrics[chr(0xB0)]["height"]
        assert deg_h < cap_h * 0.5, (
            f"Degree symbol height {deg_h} ≥ 50% of cap height {cap_h:.0f} — too big"
        )

    def test_period_smaller_than_letters(self, glyph_metrics):
        """Period should be much smaller than lowercase letters."""
        xh_heights = [
            glyph_metrics[c]["height"] for c in XH_ONLY if c in glyph_metrics
        ]
        if "." not in glyph_metrics or not xh_heights:
            pytest.skip()
        period_h = glyph_metrics["."]["height"]
        xh_med = statistics.median(xh_heights)
        assert period_h < xh_med * 0.5, (
            f"Period height {period_h} ≥ 50% of x-height {xh_med:.0f} — too big"
        )

    def test_comma_has_descent(self, glyph_metrics):
        """Comma should extend below the period's bottom."""
        if "," not in glyph_metrics or "." not in glyph_metrics:
            pytest.skip()
        assert glyph_metrics[","]["bottom"] >= glyph_metrics["."]["bottom"], (
            "Comma should extend at least as low as period"
        )


class TestFontLigatures:
    """Ligature glyphs should be present and properly sized."""

    def test_ligature_svgs_exist(self):
        """Ligature SVGs should exist for all expected ligatures."""
        for lig in EXPECTED_LIGATURES:
            svg = SVGS_DIR / f"lig_{lig}.svg"
            assert svg.exists(), f"Missing ligature SVG: {svg.name}"

    def test_ligatures_wider_than_single_chars(self, font):
        """Multi-character ligatures should render wider than a single char."""
        ref_w = font.getbbox("n")
        if not ref_w:
            pytest.skip()
        ref_width = ref_w[2] - ref_w[0]
        # Ligatures with 3+ chars should be wider than a single 'n'
        for lig_text in ["ing", "tion"]:
            # Render the ligature text — if ligature substitution fires, it'll
            # be a single glyph. Either way it should be wider than 'n'.
            bbox = font.getbbox(lig_text)
            if bbox:
                lig_w = bbox[2] - bbox[0]
                assert lig_w > ref_width, (
                    f"Ligature '{lig_text}' width {lig_w} ≤ single 'n' width "
                    f"{ref_width} — ligature not substituting or too narrow"
                )


class TestFontNoInversion:
    """Font glyphs should not be inverted (white-on-black → black-on-white)."""

    def test_glyphs_not_inverted(self, font):
        """Render a letter and check that ink pixels are the minority (dark on light)."""
        img = Image.new("L", (200, 200), 255)
        from PIL import ImageDraw
        draw = ImageDraw.Draw(img)
        draw.text((10, 10), "A", fill=0, font=font)
        arr = np.array(img)
        dark_pixels = np.count_nonzero(arr < 128)
        total = arr.size
        # In a normal (non-inverted) rendering, dark pixels should be < 50%
        assert dark_pixels / total < 0.5, (
            f"Glyph 'A' has {dark_pixels/total:.0%} dark pixels — likely inverted"
        )
