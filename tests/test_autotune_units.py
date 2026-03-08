"""Unit tests for the build-time autotune helpers."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from hw2font.autotune.engine import (
    GlyphMetrics,
    _effective_pair_gap_px,
    _suggest_geometry_for_set,
    _suggest_nudge_for_set,
    autotune_build,
)


def _write_rect_glyph(path: Path, width: int, height: int, rect: tuple[int, int, int, int]) -> None:
    img = np.zeros((height, width), dtype=np.uint8)
    x0, y0, x1, y1 = rect
    cv2.rectangle(img, (x0, y0), (x1, y1), 255, thickness=-1)
    cv2.imwrite(str(path), img)


def test_effective_pair_gap_uses_hshift_and_kern():
    left = GlyphMetrics(
        glyph="T",
        glyph_class="uc",
        width=40,
        height=80,
        y_offset=0.0,
        centroid_x=18.0,
        body_left=0,
        body_right=39,
        median_left_indent=3.0,
        median_right_indent=7.0,
    )
    right = GlyphMetrics(
        glyph="a",
        glyph_class="lc_xh",
        width=30,
        height=50,
        y_offset=0.0,
        centroid_x=15.0,
        body_left=0,
        body_right=29,
        median_left_indent=4.0,
        median_right_indent=5.0,
    )
    gap = _effective_pair_gap_px(
        left,
        right,
        {"hshift": -2.0},
        {"hshift": 3.0},
        current_kern_fu=-20,
        px_to_fu=5.0,
        tightness=1.0,
    )
    # 2*lsb_px = 16, plus indents 7+4, plus hshift delta 5, plus kern -4
    assert gap == 28.0


def test_effective_pair_gap_respects_tightness():
    left = GlyphMetrics("T", "uc", 40, 80, 0.0, 18.0, 0, 39, 3.0, 7.0)
    right = GlyphMetrics("a", "lc_xh", 30, 50, 0.0, 15.0, 0, 29, 4.0, 5.0)
    loose = _effective_pair_gap_px(left, right, {}, {}, current_kern_fu=0, px_to_fu=5.0, tightness=1.0)
    tight = _effective_pair_gap_px(left, right, {}, {}, current_kern_fu=0, px_to_fu=5.0, tightness=1.25)
    assert tight < loose


def test_suggest_geometry_scales_height_outlier():
    metrics_map = {
        "a": GlyphMetrics("a", "lc_xh", 40, 120, 0.0, 20.0, 0, 39, 2.0, 2.0),
        "e": GlyphMetrics("e", "lc_xh", 40, 80, 0.0, 19.0, 0, 39, 2.0, 2.0),
        "o": GlyphMetrics("o", "lc_xh", 40, 82, 0.0, 20.0, 0, 39, 2.0, 2.0),
    }
    overrides: dict[str, dict] = {}
    changes: list[dict] = []
    changed = _suggest_geometry_for_set(
        metrics_map=metrics_map,
        overrides=overrides,
        controls={},
        iteration=1,
        change_log=changes,
    )
    assert changed
    assert overrides["a"]["scale"] < 1.0
    assert any(change["glyph"] == "a" and change["type"] == "scale" for change in changes)


def test_suggest_geometry_can_disable_hshift_for_specific_glyph():
    metrics_map = {
        "L": GlyphMetrics("L", "uc", 40, 80, 0.0, 2.0, 0, 39, 1.0, 12.0),
        "A": GlyphMetrics("A", "uc", 40, 80, 0.0, 20.0, 0, 39, 2.0, 2.0),
        "H": GlyphMetrics("H", "uc", 40, 80, 0.0, 22.0, 0, 39, 2.0, 2.0),
    }
    overrides: dict[str, dict] = {}
    changes: list[dict] = []
    changed = _suggest_geometry_for_set(
        metrics_map=metrics_map,
        overrides=overrides,
        controls={"disable_hshift": ["L"]},
        iteration=1,
        change_log=changes,
    )
    assert changed is False
    assert not overrides.get("L", {}).get("hshift")
    assert not any(change["glyph"] == "L" and change["type"] == "hshift" for change in changes)


def test_autotune_build_writes_logs_and_returns_tuned_config(tmp_path: Path):
    extracted_dir = tmp_path / "set0"
    glyph_dir = extracted_dir / "glyphs"
    glyph_dir.mkdir(parents=True)

    metadata = {
        "A": {"file": "U+0041_A.png", "y_offset": 0.0, "bbox_w": 60, "bbox_h": 100, "ink_area": 1000},
        "T": {"file": "U+0054_T.png", "y_offset": 0.0, "bbox_w": 60, "bbox_h": 100, "ink_area": 1000},
        "V": {"file": "U+0056_V.png", "y_offset": 0.0, "bbox_w": 60, "bbox_h": 100, "ink_area": 1000},
        "a": {"file": "U+0061_a.png", "y_offset": 0.0, "bbox_w": 40, "bbox_h": 120, "ink_area": 1000},
        "e": {"file": "U+0065_e.png", "y_offset": 0.0, "bbox_w": 40, "bbox_h": 80, "ink_area": 1000},
        "o": {"file": "U+006F_o.png", "y_offset": 0.0, "bbox_w": 40, "bbox_h": 82, "ink_area": 1000},
        "v": {"file": "U+0076_v.png", "y_offset": 0.0, "bbox_w": 40, "bbox_h": 82, "ink_area": 1000},
    }
    (extracted_dir / "metadata.json").write_text(json.dumps(metadata))

    _write_rect_glyph(glyph_dir / "U+0041_A.png", 60, 100, (10, 5, 49, 94))
    _write_rect_glyph(glyph_dir / "U+0054_T.png", 60, 100, (6, 5, 35, 94))
    _write_rect_glyph(glyph_dir / "U+0056_V.png", 60, 100, (12, 5, 47, 94))
    _write_rect_glyph(glyph_dir / "U+0061_a.png", 40, 120, (5, 5, 34, 114))
    _write_rect_glyph(glyph_dir / "U+0065_e.png", 40, 80, (6, 6, 33, 73))
    _write_rect_glyph(glyph_dir / "U+006F_o.png", 40, 82, (6, 6, 33, 75))
    _write_rect_glyph(glyph_dir / "U+0076_v.png", 40, 82, (4, 8, 31, 73))

    log_path = tmp_path / "autotune.json"
    tuned_overrides, tuned_kern, tuned_per_set, artifacts = autotune_build(
        extracted_dirs=[extracted_dir],
        overrides_list=[{}],
        kern_cfg={},
        per_set_kerns=[{}],
        controls_list=[{"disable_hshift": ["A"]}],
        log_path=log_path,
        max_iterations=2,
        tightness=1.1,
    )

    assert "a" in tuned_overrides[0]
    assert tuned_overrides[0]["a"]["scale"] < 1.0
    assert artifacts["change_count"] > 0
    assert Path(artifacts["json_log"]).exists()
    assert Path(artifacts["text_log"]).exists()
    payload = json.loads(Path(artifacts["json_log"]).read_text())
    assert payload["iterations_run"] >= 1
    assert payload["tightness"] == 1.1
    assert payload["config"]["sets"][0]["autotune"] == {"disable_hshift": ["A"]}
    assert payload["config"]["kern"] == tuned_kern
    assert payload["config"]["sets"][0]["kern"] == tuned_per_set[0]


def test_suggest_nudge_adjusts_uc_outlier():
    """Uppercase letters with y_offset far from class median get a nudge suggestion."""
    metrics_map = {
        "A": GlyphMetrics("A", "uc", 60, 100, -2.0, 30.0, 0, 59, 2.0, 2.0),
        "B": GlyphMetrics("B", "uc", 60, 95, -3.0, 30.0, 0, 59, 2.0, 2.0),
        "C": GlyphMetrics("C", "uc", 60, 90, -1.0, 30.0, 0, 59, 2.0, 2.0),
        # S sits 28px higher than the median (-30 vs ~ -2)
        "S": GlyphMetrics("S", "uc", 60, 91, -30.0, 30.0, 0, 59, 2.0, 2.0),
        # Q has a descender (positive y_offset) — should be skipped
        "Q": GlyphMetrics("Q", "uc", 60, 126, 19.7, 30.0, 0, 59, 2.0, 2.0),
    }
    overrides: dict[str, dict] = {}
    changes: list[dict] = []
    changed = _suggest_nudge_for_set(
        metrics_map=metrics_map,
        overrides=overrides,
        controls={},
        iteration=1,
        change_log=changes,
    )
    assert changed
    assert "S" in overrides
    # S needs a negative nudge to move it down toward the class median
    assert overrides["S"]["nudge"] < 0
    assert any(c["glyph"] == "S" and c["type"] == "nudge" for c in changes)
    # A, B, C should not be nudged (they're near the median)
    for letter in ("A", "B", "C"):
        assert not overrides.get(letter, {}).get("nudge")
    # Q should not be nudged (descender glyph, positive y_offset)
    assert not overrides.get("Q", {}).get("nudge")
    assert not any(c["glyph"] == "Q" for c in changes)


def test_suggest_nudge_respects_disable_nudge():
    """Glyphs listed in disable_nudge are not adjusted."""
    metrics_map = {
        "A": GlyphMetrics("A", "uc", 60, 100, -2.0, 30.0, 0, 59, 2.0, 2.0),
        "B": GlyphMetrics("B", "uc", 60, 95, -3.0, 30.0, 0, 59, 2.0, 2.0),
        "S": GlyphMetrics("S", "uc", 60, 91, -30.0, 30.0, 0, 59, 2.0, 2.0),
    }
    overrides: dict[str, dict] = {}
    changes: list[dict] = []
    changed = _suggest_nudge_for_set(
        metrics_map=metrics_map,
        overrides=overrides,
        controls={"disable_nudge": ["S"]},
        iteration=1,
        change_log=changes,
    )
    assert not changed
    assert not overrides.get("S", {}).get("nudge")
