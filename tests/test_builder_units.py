"""Unit tests for builder helper functions.

These test the pure logic in hw2font.compile.builder without needing
actual glyph files, FontForge, or potrace installed.
"""

from __future__ import annotations

import pytest

from hw2font.compile.builder import (
    DEFAULT_ASCENT,
    DEFAULT_DESCENT,
    DEFAULT_UPM,
    _baseline_group,
    _build_kern_map,
    _compute_nudge_medians,
    _ff_glyph_transform_code,
    _glyph_class,
    _glyph_font_name,
    _glyph_slot,
    _lig_glyph_name,
    _uni_name,
)


# ═══════════════════════════════════════════════════════════════════════
# _uni_name
# ═══════════════════════════════════════════════════════════════════════

class TestUniName:
    def test_ascii_letter(self):
        assert _uni_name("A") == "uni0041"
        assert _uni_name("z") == "uni007A"

    def test_digit(self):
        assert _uni_name("0") == "uni0030"

    def test_symbol(self):
        assert _uni_name("!") == "uni0021"

    def test_non_ascii(self):
        assert _uni_name("£") == "uni00A3"
        assert _uni_name("♥") == "uni2665"


# ═══════════════════════════════════════════════════════════════════════
# _lig_glyph_name
# ═══════════════════════════════════════════════════════════════════════

class TestLigGlyphName:
    def test_two_char_ligature(self):
        assert _lig_glyph_name("fi") == "lig_fi"

    def test_three_char_ligature(self):
        assert _lig_glyph_name("ing") == "lig_ing"


# ═══════════════════════════════════════════════════════════════════════
# _glyph_font_name
# ═══════════════════════════════════════════════════════════════════════

class TestGlyphFontName:
    def test_single_char(self):
        assert _glyph_font_name("A") == "uni0041"

    def test_ligature(self):
        assert _glyph_font_name("th") == "lig_th"


# ═══════════════════════════════════════════════════════════════════════
# _glyph_slot
# ═══════════════════════════════════════════════════════════════════════

class TestGlyphSlot:
    def test_single_char_returns_hex(self):
        assert _glyph_slot("A") == "0x0041"
        assert _glyph_slot("0") == "0x0030"

    def test_ligature_returns_quoted_name(self):
        assert _glyph_slot("fi") == '"lig_fi"'
        assert _glyph_slot("ing") == '"lig_ing"'


# ═══════════════════════════════════════════════════════════════════════
# _glyph_class
# ═══════════════════════════════════════════════════════════════════════

class TestGlyphClass:
    def test_uppercase(self):
        assert _glyph_class("A") == "uc"
        assert _glyph_class("Z") == "uc"

    def test_uppercase_with_descender(self):
        # y_offset > 25% of bbox_h
        assert _glyph_class("Q", y_offset=50, bbox_h=100) == "uc_desc"

    def test_uppercase_without_descender(self):
        assert _glyph_class("Q", y_offset=10, bbox_h=100) == "uc"

    def test_xheight_lowercase(self):
        for c in "acemnorsuvwxz":
            assert _glyph_class(c) == "lc_xh", f"Expected lc_xh for '{c}'"

    def test_descender_lowercase(self):
        for c in "gjpqy":
            assert _glyph_class(c) == "lc_desc", f"Expected lc_desc for '{c}'"

    def test_ascender_lowercase(self):
        for c in "bdfhklt":
            assert _glyph_class(c) == "lc_asc", f"Expected lc_asc for '{c}'"

    def test_digit(self):
        assert _glyph_class("5") == "digit"

    def test_symbol(self):
        assert _glyph_class("!") == "sym"
        assert _glyph_class("@") == "sym"

    def test_ligature_xheight(self):
        assert _glyph_class("an") == "lig_xh"

    def test_ligature_with_ascender(self):
        assert _glyph_class("th") == "lig_asc"

    def test_ligature_with_descender(self):
        # "ing" has 'g' → descender
        assert _glyph_class("ing") == "lig_desc"


# ═══════════════════════════════════════════════════════════════════════
# _baseline_group
# ═══════════════════════════════════════════════════════════════════════

class TestBaselineGroup:
    def test_xheight_letters_are_nondesc(self):
        for c in "acemnorsuvwxz":
            assert _baseline_group(c) == "lc_nondesc"

    def test_ascender_letters_are_nondesc(self):
        for c in "bdfhklt":
            assert _baseline_group(c) == "lc_nondesc"

    def test_descender_letters(self):
        for c in "gjpqy":
            assert _baseline_group(c) == "lc_desc"

    def test_uppercase_is_other(self):
        assert _baseline_group("A") == "other"

    def test_digit_is_other(self):
        assert _baseline_group("0") == "other"

    def test_symbol_is_other(self):
        assert _baseline_group("!") == "other"

    def test_ligature_without_descender(self):
        assert _baseline_group("th") == "lc_nondesc"

    def test_ligature_with_descender(self):
        assert _baseline_group("ing") == "lc_desc"


# ═══════════════════════════════════════════════════════════════════════
# _compute_nudge_medians
# ═══════════════════════════════════════════════════════════════════════

class TestComputeNudgeMedians:
    def test_groups_separated(self):
        metadata = {
            "a": {"y_offset": 10},
            "c": {"y_offset": 12},
            "e": {"y_offset": 8},
            "g": {"y_offset": 50},
            "p": {"y_offset": 60},
            "A": {"y_offset": 5},
        }
        medians = _compute_nudge_medians(metadata)
        assert "lc_nondesc" in medians
        assert "lc_desc" in medians
        assert "other" in medians
        assert medians["lc_nondesc"] == 10  # median of [10, 12, 8]
        assert medians["lc_desc"] == 55     # median of [50, 60]

    def test_empty_metadata(self):
        assert _compute_nudge_medians({}) == {}


# ═══════════════════════════════════════════════════════════════════════
# _build_kern_map
# ═══════════════════════════════════════════════════════════════════════

class TestBuildKernMap:
    def test_empty_config_uses_defaults(self):
        kern_map = _build_kern_map({})
        # Default: T is in "overhang" class → T + any lc should have -120
        assert kern_map[("T", "a")] == -120
        assert kern_map[("T", "z")] == -120
        # H is "straight" → -40
        assert kern_map[("H", "a")] == -40

    def test_override_class_value(self):
        kern_map = _build_kern_map({"overhang_lc": -200})
        assert kern_map[("T", "a")] == -200

    def test_per_pair_overrides_class(self):
        kern_map = _build_kern_map({"pairs": {"Ta": -300}})
        assert kern_map[("T", "a")] == -300
        # Other T pairs still use class default
        assert kern_map[("T", "b")] == -120

    def test_right_blanket(self):
        kern_map = _build_kern_map({"right": {"x": -40}})
        assert kern_map[("x", "a")] == -40
        assert kern_map[("x", "Z")] == -40

    def test_left_blanket(self):
        kern_map = _build_kern_map({"left": {"o": -20}})
        assert kern_map[("a", "o")] == -20
        assert kern_map[("Z", "o")] == -20

    def test_pair_overrides_blanket(self):
        kern_map = _build_kern_map({
            "right": {"x": -40},
            "pairs": {"xa": -100},
        })
        assert kern_map[("x", "a")] == -100  # pair wins
        assert kern_map[("x", "b")] == -40   # blanket

    def test_zero_pairs_dropped(self):
        kern_map = _build_kern_map({"pairs": {"ab": 0}})
        assert ("a", "b") not in kern_map


# ═══════════════════════════════════════════════════════════════════════
# _ff_glyph_transform_code
# ═══════════════════════════════════════════════════════════════════════

class TestFfGlyphTransformCode:
    def test_contains_import_and_scale(self):
        code = _ff_glyph_transform_code(
            slot_code='font.createChar(0x0041, "uni0041")',
            svg_path="/tmp/A.svg",
            label="A set0",
            desired_h=680.0,
            yoff_frac=0.0,
            total_nudge_fu=0.0,
        )
        assert 'font.createChar(0x0041, "uni0041")' in code
        assert 'importOutlines("/tmp/A.svg")' in code
        assert "psMat.scale" in code
        assert "psMat.translate" in code
        assert "if False:" in code

    def test_label_appears_as_comment(self):
        code = _ff_glyph_transform_code(
            slot_code='font.createChar(-1, "lig_fi")',
            svg_path="/tmp/fi.svg",
            label="fi set0",
            desired_h=400.0,
            yoff_frac=0.1,
            total_nudge_fu=-5.0,
        )
        assert "# ── fi set0 ──" in code

    def test_values_embedded(self):
        code = _ff_glyph_transform_code(
            slot_code='font.createChar(0x0067, "uni0067")',
            svg_path="/tmp/g.svg",
            label="g",
            desired_h=500.0,
            yoff_frac=0.3,
            total_nudge_fu=10.5,
        )
        assert "500.00" in code
        assert "0.3000" in code
        assert "10.50" in code

    def test_hshift_applied_after_body_alignment(self):
        code = _ff_glyph_transform_code(
            slot_code='font.createChar(0x0079, "uni0079")',
            svg_path="/tmp/y.svg",
            label="y",
            desired_h=500.0,
            yoff_frac=0.2,
            total_nudge_fu=0.0,
            hshift_fu=12.5,
            use_body_width=True,
        )
        assert "g.transform(psMat.translate(0, shift_y))" in code
        assert "g.transform(psMat.translate(lsb + 12.50 - body_xmin, 0))" in code
        assert "if True:" in code

    def test_tightness_scales_side_bearing(self):
        code = _ff_glyph_transform_code(
            slot_code='font.createChar(0x006E, "uni006E")',
            svg_path="/tmp/n.svg",
            label="n",
            desired_h=500.0,
            yoff_frac=0.0,
            total_nudge_fu=0.0,
            tightness=1.15,
        )
        assert "lsb = 1000 * 0.04 / 1.1500" in code

    def test_non_descenders_use_full_bbox_width(self):
        code = _ff_glyph_transform_code(
            slot_code='font.createChar(0x004C, "uni004C")',
            svg_path="/tmp/L.svg",
            label="L",
            desired_h=500.0,
            yoff_frac=0.0,
            total_nudge_fu=0.0,
            use_body_width=False,
        )
        assert "g.transform(psMat.translate(lsb + 0.00 - bb[0], 0))" in code
        assert "g.width = int((bb[2] - bb[0]) + 2 * lsb)" in code
