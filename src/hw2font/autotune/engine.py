"""Build-time autotune for glyph geometry and kerning.

Autotune analyzes extracted glyph PNGs and metadata, then emits
deterministic override/kern suggestions that can be applied during
`hw2font build`. It does not change the normal proof-sheet workflow;
instead it writes separate log artifacts describing the tuning process.
"""

from __future__ import annotations

import copy
import json
import statistics
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from hw2font.compile.builder import (
    DEFAULT_ASCENT,
    DEFAULT_KERN,
    DEFAULT_UPM,
    _build_kern_map,
    _glyph_class,
)

_GEOMETRY_CLASSES = frozenset({"uc", "uc_desc", "digit", "lc_xh", "lc_asc", "lc_desc"})
_MAX_SCALE_STEP = 0.05
_MAX_HSHIFT_STEP = 2.0
_MAX_HSHIFT_ABS = 15.0
_SCALE_TOLERANCE = 0.15
_HSHIFT_TOLERANCE = 2.0
# Vertical nudge — only for classes NOT already handled by compiler baseline nudge
# (compiler auto-nudges lc_nondesc and lc_desc; we handle uc/digit)
_NUDGE_AUTOTUNE_CLASSES = frozenset({"uc", "digit"})
_NUDGE_TOLERANCE = 5.0      # px — only suggest nudge if deviation exceeds this
_MAX_NUDGE_STEP = 15.0       # max px adjustment per iteration
_MAX_NUDGE_ABS = 40.0        # max total nudge px
# Glyphs with y_offset above this are descenders whose y_offset reflects
# the descender extension, not body position — skip them from nudge.
_NUDGE_DESCENDER_SKIP = 3.0  # px
_KERN_TOLERANCE_PX = 3.0
_KERN_QUANTUM_FU = 5
_MAX_KERN_ABS = 220
_LSB_FU = DEFAULT_UPM * 0.04
_TUNING_STRINGS = [
    "minimum",
    "letter",
    "sassy",
    "woven",
    "vexed",
    "gypy",
    "quick",
    "To",
    "Ta",
    "Te",
    "Ty",
    "Vo",
    "Wa",
    "We",
    "Yo",
    "AV",
    "oo",
    "ee",
    "ll",
    "nn",
    "mm",
    "ss",
    "gy",
    "py",
    "rg",
    "ye",
    "va",
    "wo",
    "a.",
    ".a",
    "?!",
    "!?",
]


def _control_set(controls: dict | None, key: str) -> set[str]:
    values = (controls or {}).get(key, [])
    return {str(v) for v in values}


@dataclass(frozen=True)
class GlyphMetrics:
    glyph: str
    glyph_class: str
    width: int
    height: int
    y_offset: float
    centroid_x: float
    body_left: int
    body_right: int
    median_left_indent: float
    median_right_indent: float


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _round_float(value: float, digits: int = 2) -> float:
    return round(float(value), digits)


def _unique_tuning_pairs(available_glyphs: set[str]) -> list[str]:
    pairs: set[str] = set()
    for text in _TUNING_STRINGS:
        for i in range(len(text) - 1):
            pair = text[i : i + 2]
            if pair[0] in available_glyphs and pair[1] in available_glyphs:
                pairs.add(pair)
    return sorted(pairs)


def _png_metrics(glyph: str, png_path: Path, info: dict) -> GlyphMetrics:
    img = cv2.imread(str(png_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"Cannot read glyph image: {png_path}")

    mask = img > 0
    h, w = mask.shape
    y_offset = float(info.get("y_offset", 0.0))
    baseline_row = int(round(h - y_offset))
    baseline_row = max(0, min(h - 1, baseline_row))

    body_mask = mask[: baseline_row + 1, :]
    if not np.any(body_mask):
        body_mask = mask

    body_cols = np.where(body_mask.any(axis=0))[0]
    if len(body_cols) == 0:
        body_cols = np.where(mask.any(axis=0))[0]
    if len(body_cols) == 0:
        body_left = 0
        body_right = max(w - 1, 0)
    else:
        body_left = int(body_cols[0])
        body_right = int(body_cols[-1])

    _, xs = np.where(body_mask)
    centroid_x = float(xs.mean()) if len(xs) else (w - 1) / 2

    left_edges: list[int] = []
    right_edges: list[int] = []
    for row in body_mask:
        cols = np.where(row)[0]
        if len(cols):
            left_edges.append(int(cols[0]))
            right_edges.append(int(cols[-1]))

    if left_edges:
        median_left = statistics.median(left_edges) - body_left
        median_right = body_right - statistics.median(right_edges)
    else:
        median_left = 0.0
        median_right = 0.0

    return GlyphMetrics(
        glyph=glyph,
        glyph_class=_glyph_class(glyph, y_offset=y_offset, bbox_h=max(h, 1)),
        width=w,
        height=h,
        y_offset=y_offset,
        centroid_x=centroid_x,
        body_left=body_left,
        body_right=body_right,
        median_left_indent=float(median_left),
        median_right_indent=float(median_right),
    )


def _load_set_metrics(extracted_dir: Path) -> tuple[dict[str, dict], dict[str, GlyphMetrics]]:
    metadata = json.loads((extracted_dir / "metadata.json").read_text())
    glyph_dir = extracted_dir / "glyphs"
    metrics: dict[str, GlyphMetrics] = {}
    for glyph, info in metadata.items():
        metrics[glyph] = _png_metrics(glyph, glyph_dir / info["file"], info)
    return metadata, metrics


def _primary_px_to_fu(metadata: dict[str, dict]) -> float:
    uc_heights = sorted(
        info["bbox_h"]
        for glyph, info in metadata.items()
        if len(glyph) == 1 and glyph.isupper()
    )
    median_uc_px = uc_heights[len(uc_heights) // 2] if uc_heights else 200
    return (DEFAULT_ASCENT * 0.85) / median_uc_px


def _merge_kern_configs(base: dict, overlay: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if key in {"pairs", "right", "left"} and isinstance(value, dict):
            merged.setdefault(key, {})
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def _quantize_fu(value: float) -> int:
    return int(round(value / _KERN_QUANTUM_FU) * _KERN_QUANTUM_FU)


def _effective_pair_gap_px(
    left: GlyphMetrics,
    right: GlyphMetrics,
    left_ovr: dict,
    right_ovr: dict,
    current_kern_fu: int,
    px_to_fu: float,
    tightness: float,
) -> float:
    lsb_px = (_LSB_FU / max(tightness, 0.1)) / px_to_fu
    left_hshift = float(left_ovr.get("hshift", 0.0))
    right_hshift = float(right_ovr.get("hshift", 0.0))
    kern_px = current_kern_fu / px_to_fu
    return (
        2 * lsb_px
        + left.median_right_indent
        + right.median_left_indent
        + right_hshift
        - left_hshift
        + kern_px
    )


def _suggest_geometry_for_set(
    *,
    metrics_map: dict[str, GlyphMetrics],
    overrides: dict,
    controls: dict | None,
    iteration: int,
    change_log: list[dict],
) -> bool:
    changed = False
    disable_scale = _control_set(controls, "disable_scale")
    disable_hshift = _control_set(controls, "disable_hshift")
    grouped_heights: dict[str, list[float]] = {}
    grouped_centers: dict[str, list[float]] = {}

    for glyph, metrics in metrics_map.items():
        if metrics.glyph_class not in _GEOMETRY_CLASSES:
            continue
        ovr = overrides.get(glyph, {})
        grouped_heights.setdefault(metrics.glyph_class, []).append(
            metrics.height * float(ovr.get("scale", 1.0))
        )
        grouped_centers.setdefault(metrics.glyph_class, []).append(
            (metrics.centroid_x + float(ovr.get("hshift", 0.0))) / max(metrics.width - 1, 1)
        )

    target_heights = {
        cls: statistics.median(values)
        for cls, values in grouped_heights.items()
        if values
    }
    target_centers = {
        cls: statistics.median(values)
        for cls, values in grouped_centers.items()
        if values
    }

    for glyph in sorted(metrics_map):
        metrics = metrics_map[glyph]
        if metrics.glyph_class not in _GEOMETRY_CLASSES:
            continue
        ovr = overrides.setdefault(glyph, {})
        current_scale = float(ovr.get("scale", 1.0))
        effective_height = metrics.height * current_scale
        target_height = target_heights.get(metrics.glyph_class)
        if glyph not in disable_scale and target_height and target_height > 0:
            height_ratio = effective_height / target_height
            if abs(1.0 - height_ratio) > _SCALE_TOLERANCE:
                desired_scale = current_scale * (target_height / effective_height)
                desired_scale = _clamp(
                    desired_scale,
                    current_scale * (1 - _MAX_SCALE_STEP),
                    current_scale * (1 + _MAX_SCALE_STEP),
                )
                desired_scale = _clamp(desired_scale, 0.7, 1.3)
                desired_scale = _round_float(desired_scale, 2)
                if abs(desired_scale - current_scale) >= 0.03:
                    ovr["scale"] = desired_scale
                    change_log.append({
                        "iteration": iteration,
                        "type": "scale",
                        "glyph": glyph,
                        "old": current_scale,
                        "new": desired_scale,
                        "reason": (
                            f"{metrics.glyph_class} effective height {effective_height:.1f}px "
                            f"vs class median {target_height:.1f}px"
                        ),
                    })
                    current_scale = desired_scale
                    changed = True

        current_hshift = float(ovr.get("hshift", 0.0))
        target_center = target_centers.get(metrics.glyph_class)
        if glyph not in disable_hshift and target_center is not None:
            current_center = (metrics.centroid_x + current_hshift) / max(metrics.width - 1, 1)
            delta_px = (target_center - current_center) * metrics.width * 0.20
            if abs(delta_px) >= _HSHIFT_TOLERANCE:
                desired_hshift = _clamp(
                    current_hshift + delta_px,
                    current_hshift - _MAX_HSHIFT_STEP,
                    current_hshift + _MAX_HSHIFT_STEP,
                )
                desired_hshift = _clamp(desired_hshift, -_MAX_HSHIFT_ABS, _MAX_HSHIFT_ABS)
                desired_hshift = _round_float(desired_hshift, 1)
                if abs(desired_hshift - current_hshift) >= _HSHIFT_TOLERANCE:
                    ovr["hshift"] = desired_hshift
                    change_log.append({
                        "iteration": iteration,
                        "type": "hshift",
                        "glyph": glyph,
                        "old": current_hshift,
                        "new": desired_hshift,
                        "reason": (
                            f"{metrics.glyph_class} centroid {current_center:.3f} "
                            f"vs class median {target_center:.3f}"
                        ),
                    })
                    changed = True

    return changed


def _suggest_nudge_for_set(
    *,
    metrics_map: dict[str, GlyphMetrics],
    overrides: dict,
    controls: dict | None,
    iteration: int,
    change_log: list[dict],
) -> bool:
    """Suggest vertical nudge adjustments for non-lowercase glyphs.

    The compiler's baseline nudge only corrects lowercase groups.
    This fills the gap for uppercase and digits by computing the median
    y_offset per glyph class and nudging outliers.  Glyphs with positive
    y_offset (descender ink below baseline) are skipped — their y_offset
    reflects descender length, not body position.
    """
    changed = False
    disable_nudge = _control_set(controls, "disable_nudge")

    grouped_yoffsets: dict[str, list[float]] = {}
    for glyph, metrics in metrics_map.items():
        if metrics.glyph_class not in _NUDGE_AUTOTUNE_CLASSES:
            continue
        if metrics.y_offset > _NUDGE_DESCENDER_SKIP:
            continue
        grouped_yoffsets.setdefault(metrics.glyph_class, []).append(metrics.y_offset)

    target_yoffsets = {
        cls: statistics.median(values)
        for cls, values in grouped_yoffsets.items()
        if values
    }

    for glyph in sorted(metrics_map):
        metrics = metrics_map[glyph]
        if metrics.glyph_class not in _NUDGE_AUTOTUNE_CLASSES:
            continue
        if glyph in disable_nudge:
            continue
        if metrics.y_offset > _NUDGE_DESCENDER_SKIP:
            continue

        target = target_yoffsets.get(metrics.glyph_class)
        if target is None:
            continue

        ovr = overrides.setdefault(glyph, {})
        current_nudge = float(ovr.get("nudge", 0.0))

        # Desired nudge to align this glyph with the class median.
        # For uppercase (compiler sets nudge_px=0), config nudge IS the full adjustment.
        desired_nudge = -(target - metrics.y_offset)

        if abs(desired_nudge - current_nudge) < _NUDGE_TOLERANCE:
            continue

        step = _clamp(
            desired_nudge - current_nudge,
            -_MAX_NUDGE_STEP,
            _MAX_NUDGE_STEP,
        )
        new_nudge = current_nudge + step
        new_nudge = _clamp(new_nudge, -_MAX_NUDGE_ABS, _MAX_NUDGE_ABS)
        new_nudge = _round_float(new_nudge, 1)

        if abs(new_nudge - current_nudge) < _NUDGE_TOLERANCE:
            continue

        ovr["nudge"] = new_nudge
        change_log.append({
            "iteration": iteration,
            "type": "nudge",
            "glyph": glyph,
            "old": current_nudge,
            "new": new_nudge,
            "reason": (
                f"{metrics.glyph_class} y_offset {metrics.y_offset:.1f}px "
                f"vs class median {target:.1f}px"
            ),
        })
        changed = True

    return changed


def _suggest_kerning_for_set(
    *,
    set_idx: int,
    metrics_map: dict[str, GlyphMetrics],
    overrides: dict,
    controls: dict | None,
    base_kern_cfg: dict,
    target_kern_cfg: dict,
    px_to_fu: float,
    tightness: float,
    iteration: int,
    change_log: list[dict],
) -> bool:
    available = set(metrics_map)
    disable_kern_pairs = _control_set(controls, "disable_kern_pairs")
    tuning_pairs = [
        pair for pair in _unique_tuning_pairs(available)
        if pair not in disable_kern_pairs
    ]
    if not tuning_pairs:
        return False

    effective_cfg = _merge_kern_configs(base_kern_cfg, target_kern_cfg)
    current_map = _build_kern_map(copy.deepcopy(effective_cfg))

    pair_gaps: list[float] = []
    for pair in tuning_pairs:
        left = metrics_map[pair[0]]
        right = metrics_map[pair[1]]
        current_kern = current_map.get((pair[0], pair[1]), 0)
        gap = _effective_pair_gap_px(
            left, right, overrides.get(pair[0], {}), overrides.get(pair[1], {}),
            current_kern, px_to_fu, tightness,
        )
        pair_gaps.append(gap)

    if not pair_gaps:
        return False

    target_gap = statistics.median(pair_gaps)
    changed = False
    pair_cfg = target_kern_cfg.setdefault("pairs", {})
    for pair in tuning_pairs:
        left = metrics_map[pair[0]]
        right = metrics_map[pair[1]]
        current_kern = current_map.get((pair[0], pair[1]), 0)
        current_gap = _effective_pair_gap_px(
            left, right, overrides.get(pair[0], {}), overrides.get(pair[1], {}),
            current_kern, px_to_fu, tightness,
        )
        gap_delta = target_gap - current_gap
        if abs(gap_delta) < _KERN_TOLERANCE_PX:
            continue

        desired_kern = current_kern + _quantize_fu(gap_delta * px_to_fu)
        desired_kern = int(_clamp(desired_kern, -_MAX_KERN_ABS, _MAX_KERN_ABS))
        existing_pair = int(pair_cfg.get(pair, current_kern))
        if desired_kern == existing_pair:
            continue

        pair_cfg[pair] = desired_kern
        changed = True
        change_log.append({
            "iteration": iteration,
            "type": "kern_pair",
            "set": set_idx,
            "pair": pair,
            "old": existing_pair,
            "new": desired_kern,
            "reason": (
                f"visible gap {current_gap:.1f}px vs tuning median {target_gap:.1f}px"
            ),
        })

    return changed


def _json_ready_sets(
    overrides_list: list[dict],
    per_set_kerns: list[dict],
    controls_list: list[dict],
) -> list[dict]:
    payload: list[dict] = []
    for idx, (overrides, kern, controls) in enumerate(zip(overrides_list, per_set_kerns, controls_list)):
        payload.append({
            "set_idx": idx,
            "overrides": overrides,
            "kern": kern,
            "autotune": controls,
        })
    return payload


def _render_text_log(summary: dict) -> str:
    lines = [
        "hw2font autotune log",
        f"iterations_run: {summary['iterations_run']}",
        f"pixel_to_fu: {summary['px_to_fu']:.6f}",
        f"tuning_strings: {', '.join(summary['tuning_strings'])}",
        "",
        "Applied changes:",
    ]
    if not summary["changes"]:
        lines.append("  (no changes)")
    else:
        for change in summary["changes"]:
            if change["type"] == "kern_pair":
                lines.append(
                    f"  iter {change['iteration']} set {change['set']} kern {change['pair']}: "
                    f"{change['old']} -> {change['new']}  ({change['reason']})"
                )
            else:
                lines.append(
                    f"  iter {change['iteration']} glyph {change['glyph']} {change['type']}: "
                    f"{change['old']} -> {change['new']}  ({change['reason']})"
                )
    return "\n".join(lines) + "\n"


def autotune_build(
    *,
    extracted_dirs: list[Path],
    overrides_list: list[dict],
    kern_cfg: dict | None = None,
    per_set_kerns: list[dict] | None = None,
    controls_list: list[dict] | None = None,
    log_path: str | Path,
    max_iterations: int = 2,
    tightness: float = 1.0,
) -> tuple[list[dict], dict, list[dict], dict[str, Path | int]]:
    """Autotune extracted glyph sets before final build.

    Returns tuned `(overrides_list, kern_cfg, per_set_kerns, artifacts)`.
    """
    if per_set_kerns is None:
        per_set_kerns = [{} for _ in extracted_dirs]
    if controls_list is None:
        controls_list = [{} for _ in extracted_dirs]
    if max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")

    tuned_overrides = copy.deepcopy(overrides_list)
    tuned_global_kern = copy.deepcopy(kern_cfg or DEFAULT_KERN)
    tuned_per_set_kerns = copy.deepcopy(per_set_kerns)

    set_payloads: list[tuple[dict[str, dict], dict[str, GlyphMetrics]]] = []
    for extracted_dir in extracted_dirs:
        set_payloads.append(_load_set_metrics(Path(extracted_dir)))

    px_to_fu = _primary_px_to_fu(set_payloads[0][0])
    all_changes: list[dict] = []
    iterations_run = 0
    for iteration in range(1, max_iterations + 1):
        iterations_run = iteration
        changed = False
        for set_idx, (_, metrics_map) in enumerate(set_payloads):
            changed |= _suggest_geometry_for_set(
                metrics_map=metrics_map,
                overrides=tuned_overrides[set_idx],
                controls=controls_list[set_idx],
                iteration=iteration,
                change_log=all_changes,
            )
            changed |= _suggest_nudge_for_set(
                metrics_map=metrics_map,
                overrides=tuned_overrides[set_idx],
                controls=controls_list[set_idx],
                iteration=iteration,
                change_log=all_changes,
            )

        for set_idx, (_, metrics_map) in enumerate(set_payloads):
            base_cfg = tuned_global_kern if set_idx > 0 else {}
            target_cfg = tuned_per_set_kerns[set_idx] if set_idx > 0 else tuned_global_kern
            changed |= _suggest_kerning_for_set(
                set_idx=set_idx,
                metrics_map=metrics_map,
                overrides=tuned_overrides[set_idx],
                controls=controls_list[set_idx],
                base_kern_cfg=base_cfg,
                target_kern_cfg=target_cfg,
                px_to_fu=px_to_fu,
                tightness=tightness,
                iteration=iteration,
                change_log=all_changes,
            )

        if not changed:
            break

    summary = {
        "iterations_run": iterations_run,
        "px_to_fu": px_to_fu,
        "tightness": tightness,
        "tuning_strings": _TUNING_STRINGS,
        "changes": all_changes,
        "config": {
            "kern": tuned_global_kern,
            "sets": _json_ready_sets(tuned_overrides, tuned_per_set_kerns, controls_list),
        },
    }

    json_path = Path(log_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    text_path = json_path.with_suffix(".txt")
    text_path.write_text(_render_text_log(summary))

    artifacts = {
        "json_log": json_path,
        "text_log": text_path,
        "change_count": len(all_changes),
        "iterations_run": iterations_run,
    }
    return tuned_overrides, tuned_global_kern, tuned_per_set_kerns, artifacts
