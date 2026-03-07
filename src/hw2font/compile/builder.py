"""Module C — Vectorization & Font Compilation.

Two-phase build:
  1. Potrace: convert each extracted bitmap → SVG (subprocess).
  2. FontForge: assemble SVGs into an OpenType font (subprocess, because
     fontforge's Python bindings are a system C extension, not pip-installable).

The FontForge script is generated dynamically and executed via
``fontforge -script``.
"""

from __future__ import annotations

import json
import statistics
import subprocess
import tempfile
import textwrap
from pathlib import Path

import cv2
import click

from hw2font.constants import (
    ALL_GLYPHS,
    EXTRA_LIGATURES,
    LIGATURES,
)

# ── Constants ─────────────────────────────────────────────────────────
DEFAULT_UPM = 1000       # units-per-em for the font
DEFAULT_ASCENT = 800
DEFAULT_DESCENT = 200
FONT_FAMILY = "Handwriting"
FONT_NAME = "Handwriting_MVP"

# Default kerning values (font units, negative = tighter)
# Shape-based UC classes for right-side kerning:
#   overhang: crossbar/diagonal extends over the space to the right (T, F, V, W, Y)
#   round:    curved right side (C, G, O, Q, D)
#   straight: vertical right side (H, I, M, N, B, E, K, L, R, U)
#   open:     open right side (A, J, P, S, X, Z)
_UC_SHAPE_CLASSES = {
    "overhang": list("TFVWY"),
    "round":    list("CGOQD"),
    "straight": list("HIMNBEKLRU"),
    "open":     list("AJPSXZ"),
}

DEFAULT_KERN = {
    "overhang_lc":  -120,   # T, F, V, W, Y → lowercase
    "round_lc":      -60,   # C, G, O, Q, D → lowercase
    "straight_lc":   -40,   # H, I, M, N, … → lowercase
    "open_lc":       -50,   # A, J, P, S, … → lowercase
}

# Potrace tuning — optimised for handwriting at 600 DPI
_POTRACE_OPTS = [
    "--turdsize", "3",       # suppress specks ≤ 3 px
    "--alphamax", "1.2",     # corner threshold (lower = more corners kept)
    "--opttolerance", "0.2", # curve optimisation tolerance
]


# ── Potrace vectorization ─────────────────────────────────────────────

def _bitmap_to_svg(png_path: Path, svg_path: Path) -> None:
    """Convert a binary PNG to SVG via potrace.

    Potrace requires PBM input, so we convert PNG→PBM on the fly using
    OpenCV (already a dependency) and pipe it in.
    """
    img = cv2.imread(str(png_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"Cannot read {png_path}")

    # Ensure binary: ink=black(1), bg=white(0) for PBM/potrace.
    # Extracted PNGs have ink=255, bg=0 — threshold without inversion.
    _, bw = cv2.threshold(img, 127, 255, cv2.THRESH_BINARY)

    # Build PBM P4 (packed binary) in memory
    h, w = bw.shape
    rows: list[bytes] = []
    for y in range(h):
        row_bytes = bytearray()
        for x in range(0, w, 8):
            byte = 0
            for bit in range(8):
                if x + bit < w and bw[y, x + bit] > 0:
                    byte |= 1 << (7 - bit)
            row_bytes.append(byte)
        rows.append(bytes(row_bytes))

    pbm = f"P4\n{w} {h}\n".encode() + b"".join(rows)

    result = subprocess.run(
        ["potrace", "-s", *_POTRACE_OPTS, "-o", str(svg_path), "-"],
        input=pbm,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"potrace failed: {result.stderr.decode()}")


def vectorize_all(
    extracted_dir: Path,
    svg_dir: Path,
) -> dict[str, Path]:
    """Run potrace on every glyph PNG. Returns {glyph: svg_path}."""
    metadata = json.loads((extracted_dir / "metadata.json").read_text())
    glyph_dir = extracted_dir / "glyphs"
    svg_dir.mkdir(parents=True, exist_ok=True)

    svg_map: dict[str, Path] = {}
    for glyph, info in metadata.items():
        png = glyph_dir / info["file"]
        svg = svg_dir / (png.stem + ".svg")
        _bitmap_to_svg(png, svg)
        svg_map[glyph] = svg

    return svg_map


# ── FontForge script generation ───────────────────────────────────────

def _glyph_slot(glyph: str) -> str:
    """Return the FontForge glyph slot reference for a glyph."""
    if len(glyph) == 1:
        return f"0x{ord(glyph):04X}"
    # Ligatures get an unencoded slot, referenced by name
    return f'"{_lig_glyph_name(glyph)}"'


def _lig_glyph_name(lig: str) -> str:
    """FontForge-friendly glyph name for a ligature."""
    return f"lig_{lig}"


_XH_ONLY = set("acemnorsuvwxz")  # x-height-only lowercase (no ascender/descender)
_LC_DESCENDER = set("gjpqy")
_LC_ASCENDER = set("bdfhklt")
_NON_DESC_LC = _XH_ONLY | _LC_ASCENDER
_NUDGE_GROUPS = frozenset({"lc_nondesc", "lc_desc"})
_DESCENDER_THRESHOLD = 0.25  # if >25% of bbox is below baseline, treat as descender


def _uni_name(char: str) -> str:
    """Canonical font glyph name for a single Unicode character."""
    return f"uni{ord(char):04X}"


def _glyph_font_name(glyph: str) -> str:
    """Font glyph name for any glyph (single char or ligature)."""
    if len(glyph) == 1:
        return _uni_name(glyph)
    return _lig_glyph_name(glyph)


def _baseline_group(glyph: str) -> str:
    """Classify a glyph for baseline-nudge grouping.

    Returns 'lc_nondesc', 'lc_desc', or 'other'.
    """
    if len(glyph) == 1:
        if glyph in _NON_DESC_LC:
            return "lc_nondesc"
        if glyph in _LC_DESCENDER:
            return "lc_desc"
    elif len(glyph) > 1:
        if any(c in _LC_DESCENDER for c in glyph):
            return "lc_desc"
        return "lc_nondesc"
    return "other"


def _compute_nudge_medians(
    metadata: dict[str, dict],
) -> dict[str, float]:
    """Compute median y_offset per baseline group from metadata.

    Returns {group_name: median_y_offset}.
    """
    groups: dict[str, list[float]] = {}
    for glyph, info in metadata.items():
        grp = _baseline_group(glyph)
        groups.setdefault(grp, []).append(info.get("y_offset", 0))
    return {grp: statistics.median(offs) for grp, offs in groups.items()}


def _glyph_class(glyph: str, y_offset: float = 0, bbox_h: float = 1) -> str:
    """Classify a glyph for scaling purposes.

    Returns one of: uc, uc_desc, digit, lc_xh, lc_asc, lc_desc, lig_xh, lig_asc, lig_desc, sym
    """
    if len(glyph) > 1:
        # Ligature: classify based on component letters
        has_asc = any(c in _LC_ASCENDER for c in glyph)
        has_desc = any(c in _LC_DESCENDER for c in glyph)
        if has_desc:
            return "lig_desc"
        if has_asc:
            return "lig_asc"
        return "lig_xh"
    if glyph in _XH_ONLY:
        return "lc_xh"
    if glyph in _LC_DESCENDER:
        return "lc_desc"
    if glyph in _LC_ASCENDER:
        return "lc_asc"
    if glyph.isupper():
        if bbox_h > 0 and y_offset / bbox_h > _DESCENDER_THRESHOLD:
            return "uc_desc"
        return "uc"
    if glyph.isdigit():
        return "digit"
    return "sym"


def _ff_glyph_transform_code(
    *,
    slot_code: str,
    svg_path: str,
    label: str,
    desired_h: float,
    yoff_frac: float,
    total_nudge_fu: float,
) -> str:
    """Generate FontForge Python code to import, scale, and position a glyph.

    This is the shared template used by both single-set and multi-set
    script generators.
    """
    return textwrap.dedent(f"""\
        # ── {label} ──
        g = {slot_code}
        g.importOutlines("{svg_path}")
        bb = g.boundingBox()
        svg_h = bb[3] - bb[1]
        if svg_h > 0:
            sc = {desired_h:.2f} / svg_h
            g.transform(psMat.scale(sc))
            bb = g.boundingBox()
            descent_units = (bb[3] - bb[1]) * {yoff_frac:.4f}
            shift_y = -descent_units - bb[1] + {total_nudge_fu:.2f}
            g.transform(psMat.translate(0, shift_y))
            bb = g.boundingBox()
            lsb = {DEFAULT_UPM} * 0.04
            g.left_side_bearing = int(lsb)
            g.right_side_bearing = int(lsb)
            g.width = int(bb[2] - bb[0] + 2 * lsb)
    """)


def _build_kern_map(kern_cfg: dict) -> dict[tuple[str, str], int]:
    """Build a map of (glyph_char, glyph_char) → kern value from config.

    kern_cfg supports:
      - Class defaults: overhang_lc, round_lc, straight_lc, open_lc
      - Right-side blanket: right.x = -40 (x followed by anything)
      - Left-side blanket:  left.o = -20 (anything followed by o)
      - Per-pair overrides: pairs.Ta = -150 (first char + second char)
    Priority: pairs > right/left > classes.
    """
    merged = {**DEFAULT_KERN, **kern_cfg}
    pair_overrides: dict = merged.pop("pairs", {})
    right_overrides: dict = merged.pop("right", {})
    left_overrides: dict = merged.pop("left", {})

    all_letters = list("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
    lc = list("abcdefghijklmnopqrstuvwxyz")
    kern_map: dict[tuple[str, str], int] = {}

    # 1. Class defaults: each UC shape class → all lowercase
    for class_key, uc_chars in _UC_SHAPE_CLASSES.items():
        value = merged.get(f"{class_key}_lc", 0)
        if value != 0:
            for u in uc_chars:
                for l in lc:
                    kern_map[(u, l)] = value

    # 2. Right-side blanket: glyph followed by any letter
    for char, value in right_overrides.items():
        if len(char) == 1:
            for other in all_letters:
                kern_map[(char, other)] = value

    # 3. Left-side blanket: any letter followed by glyph
    for char, value in left_overrides.items():
        if len(char) == 1:
            for other in all_letters:
                kern_map[(other, char)] = value

    # 4. Per-pair overrides (highest priority)
    for pair_str, value in pair_overrides.items():
        if len(pair_str) == 2:
            kern_map[(pair_str[0], pair_str[1])] = value

    # Drop zero-value pairs
    return {k: v for k, v in kern_map.items() if v != 0}


def _kern_lines(kern_cfg: dict) -> list[str]:
    """Generate FontForge kern lines for a single-set font (base glyph names)."""
    kern_map = _build_kern_map(kern_cfg)
    if not kern_map:
        return []

    lines: list[str] = []
    lines.append('font.addLookup("kern", "gpos_pair", (), '
                  '(("kern",(("latn",("dflt")),)),))')
    lines.append('font.addLookupSubtable("kern", "kern-1")')

    for (c1, c2), value in sorted(kern_map.items()):
        lines.append(f'font["{_uni_name(c1)}"].addPosSub("kern-1", "{_uni_name(c2)}", {value})')

    return lines


def _multiset_kern_lines(
    global_kern: dict,
    per_set_kerns: list[dict],
    alt_glyphs: dict[str, list[str]],
) -> list[str]:
    """Generate kern lines for multi-set font: base glyphs + alternates.

    Global kern applies to base (set 0) glyphs.  Each set's kern config
    is merged on top of the global config for that set's alternate glyphs.
    """
    # Base kern map from global config
    base_map = _build_kern_map(global_kern)

    # Per-set kern maps: merge global + set-specific
    set_maps: list[dict[tuple[str, str], int]] = []
    for set_kern in per_set_kerns:
        if set_kern:
            merged_cfg = {**global_kern}
            # Deep-merge sub-dicts (pairs, right, left)
            for key in ("pairs", "right", "left"):
                if key in set_kern:
                    merged_cfg.setdefault(key, {})
                    if isinstance(merged_cfg[key], dict):
                        merged_cfg[key] = {**merged_cfg[key], **set_kern[key]}
                    else:
                        merged_cfg[key] = set_kern[key]
            # Override scalar keys
            for key, val in set_kern.items():
                if key not in ("pairs", "right", "left"):
                    merged_cfg[key] = val
            set_maps.append(_build_kern_map(merged_cfg))
        else:
            set_maps.append(base_map)

    # Collect all kern pairs: (glyph_name_1, glyph_name_2) → value
    all_pairs: dict[tuple[str, str], int] = {}

    # Base glyphs (set 0)
    for (c1, c2), value in base_map.items():
        all_pairs[(_uni_name(c1), _uni_name(c2))] = value

    # Alternate glyphs: kern the alt glyph name against base second glyph,
    # AND base first glyph against alt second glyph
    for set_idx, smap in enumerate(set_maps):
        if set_idx == 0:
            continue  # set 0 is the base, already handled
        suffix = f".alt{set_idx}"
        for (c1, c2), value in smap.items():
            alt1 = alt_glyphs.get(c1, [])
            alt2 = alt_glyphs.get(c2, [])
            # alt of c1 followed by base c2
            if set_idx <= len(alt1):
                alt1_name = f"{_uni_name(c1)}{suffix}"
                all_pairs[(alt1_name, _uni_name(c2))] = value
            # base c1 followed by alt of c2
            if set_idx <= len(alt2):
                alt2_name = f"{_uni_name(c2)}{suffix}"
                all_pairs[(_uni_name(c1), alt2_name)] = value
            # alt c1 followed by alt c2
            if set_idx <= len(alt1) and set_idx <= len(alt2):
                all_pairs[(f"{_uni_name(c1)}{suffix}", f"{_uni_name(c2)}{suffix}")] = value

    all_pairs = {k: v for k, v in all_pairs.items() if v != 0}
    if not all_pairs:
        return []

    lines: list[str] = []
    lines.append('font.addLookup("kern", "gpos_pair", (), '
                  '(("kern",(("latn",("dflt")),)),))')
    lines.append('font.addLookupSubtable("kern", "kern-1")')

    for (g1, g2), value in sorted(all_pairs.items()):
        lines.append(f'font["{g1}"].addPosSub("kern-1", "{g2}", {value})')

    return lines


def _build_fontforge_script(
    svg_map: dict[str, str],
    metadata: dict,
    output_otf: str,
    dpi: int,
    overrides: dict | None = None,
    font_name: str | None = None,
    kern_cfg: dict | None = None,
) -> str:
    """Generate a FontForge Python script as a string.

    overrides: per-glyph tweaks from config file, keyed by glyph string.
        Each value is a dict with optional keys:
          - "scale": float multiplier (e.g. 0.8)
          - "nudge": float pixels to shift vertically (positive = up)
    """
    if overrides is None:
        overrides = {}

    lines: list[str] = []

    glyph_entries: list[dict] = []
    for glyph, svg_path in svg_map.items():
        info = metadata.get(glyph, {})
        y_off = info.get("y_offset", 0)
        bbox_h = info.get("bbox_h", 1)

        if len(glyph) == 1:
            glyph_name = _uni_name(glyph)
            slot = f'font.createChar({_glyph_slot(glyph)}, "{glyph_name}")'
        else:
            slot = f'font.createChar(-1, "{_lig_glyph_name(glyph)}")'

        glyph_entries.append({
            "glyph": glyph,
            "svg": svg_path,
            "slot_code": slot,
            "y_offset": y_off,
            "bbox_h": bbox_h,
            "is_uc": glyph.isupper() and len(glyph) == 1,
        })

    # ── Universal scale factor ──
    # Trust that the user wrote each glyph at the intended relative size.
    # One px→font-unit ratio derived from uppercase caps height.
    uc_px_heights = sorted([e["bbox_h"] for e in glyph_entries if e["is_uc"]])
    median_uc_px = uc_px_heights[len(uc_px_heights) // 2] if uc_px_heights else 200
    _cap_target = DEFAULT_ASCENT * 0.85  # 680
    _px_to_fu = _cap_target / median_uc_px  # pixels → font units

    # ── Baseline nudge ──
    # Compute median y_offset per baseline group and nudge outliers toward it.
    group_medians = _compute_nudge_medians(metadata)

    for e in glyph_entries:
        grp = _baseline_group(e["glyph"])
        e["bl_group"] = grp
        if grp in _NUDGE_GROUPS:
            e["nudge_px"] = -(group_medians[grp] - e["y_offset"])
        else:
            e["nudge_px"] = 0.0

    lines.append(textwrap.dedent(f"""\
        import fontforge
        import psMat
        import json

        font = fontforge.font()
        font.familyname = "{font_name or FONT_FAMILY}"
        font.fontname = "{(font_name or FONT_FAMILY).replace(' ', '_')}"
        font.fullname = "{font_name or FONT_FAMILY}"
        font.em = {DEFAULT_UPM}
        font.ascent = {DEFAULT_ASCENT}
        font.descent = {DEFAULT_DESCENT}

        px_to_fu = {_px_to_fu:.6f}  # universal pixels → font units
    """))

    for e in glyph_entries:
        glyph = e["glyph"]
        ovr = overrides.get(glyph, {})
        scale_mult = ovr.get("scale", 1.0)
        extra_nudge_px = ovr.get("nudge", 0.0)
        desired_h = e["bbox_h"] * _px_to_fu * scale_mult
        yoff_frac = e["y_offset"] / e["bbox_h"] if e["bbox_h"] > 0 else 0
        total_nudge_fu = (e["nudge_px"] + extra_nudge_px) * _px_to_fu

        lines.append(_ff_glyph_transform_code(
            slot_code=e["slot_code"],
            svg_path=e["svg"],
            label=f"{glyph} (nudge {e['nudge_px']:+.1f}px, ovr scale={scale_mult}, ovr nudge={extra_nudge_px:+.1f}px)",
            desired_h=desired_h,
            yoff_frac=yoff_frac,
            total_nudge_fu=total_nudge_fu,
        ))

    # Build OpenType feature lookups for ligatures
    all_ligs = []
    for lig in LIGATURES + EXTRA_LIGATURES:
        if lig in svg_map:
            components = " ".join(lig)
            name = _lig_glyph_name(lig)
            all_ligs.append((lig, components, name))

    if all_ligs:
        # Use addLookup API for ligature substitutions
        lines.append(textwrap.dedent("""\
            font.addLookup("liga", "gsub_ligature", (), (("liga",(("latn",("dflt")),)),))
            font.addLookupSubtable("liga", "liga-1")
        """))

        for lig, components, name in all_ligs:
            component_tuple = "(" + ",".join(f'"{c}"' for c in lig) + ",)"
            lines.append(
                f'font["{name}"].addPosSub("liga-1", {component_tuple})\n'
            )

    # ── Kerning ──
    klines = _kern_lines(kern_cfg or {})
    if klines:
        lines.append("")
        lines.extend(klines)
        lines.append("")

    # Set a space glyph
    lines.append(textwrap.dedent(f"""\
        # Space glyph
        space = font.createChar(0x0020)
        space.width = {DEFAULT_UPM // 4}

        font.generate("{output_otf}")
        print(f"Generated: {output_otf}")
    """))

    return "\n".join(lines)


# ── Multi-set compilation with contextual alternates ──────────────────

def _build_multiset_fontforge_script(
    sets: list[dict],
    output_otf: str,
    dpi: int,
    font_name: str | None = None,
    kern_cfg: dict | None = None,
) -> tuple[str, dict[str, list[str]]]:
    """Generate a FontForge script that imports multiple glyph sets and
    creates contextual alternates (calt) to cycle between them.

    Each entry in *sets* is a dict with:
      - svg_map: {glyph: svg_path_str}
      - metadata: parsed metadata.json
      - overrides: per-glyph {scale, nudge}
      - set_idx: 0-based set index
    """
    n_sets = len(sets)
    lines: list[str] = []

    # ── Compute universal scale from set 0 (primary) ──
    primary = sets[0]
    p_meta = primary["metadata"]
    uc_px = sorted(
        p_meta[g]["bbox_h"]
        for g in p_meta
        if len(g) == 1 and g.isupper()
    )
    median_uc_px = uc_px[len(uc_px) // 2] if uc_px else 200
    _cap_target = DEFAULT_ASCENT * 0.85
    _px_to_fu = _cap_target / median_uc_px

    # ── Compute baseline nudge medians from set 0 ──
    group_medians = _compute_nudge_medians(p_meta)

    lines.append(textwrap.dedent(f"""\
        import fontforge
        import psMat

        font = fontforge.font()
        font.familyname = "{font_name or FONT_FAMILY}"
        font.fontname = "{(font_name or FONT_FAMILY).replace(' ', '_')}"
        font.fullname = "{font_name or FONT_FAMILY}"
        font.em = {DEFAULT_UPM}
        font.ascent = {DEFAULT_ASCENT}
        font.descent = {DEFAULT_DESCENT}
    """))

    # Track which glyphs have alternates for calt generation
    alt_glyphs: dict[str, list[str]] = {}  # glyph -> [alt_name, ...]

    for s in sets:
        set_idx = s["set_idx"]
        svg_map = s["svg_map"]
        meta = s["metadata"]
        overrides = s.get("overrides", {})
        suffix = "" if set_idx == 0 else f".alt{set_idx}"

        # Use set 0's px_to_fu for all sets (consistent scale reference)
        # but compute per-set nudge medians
        set_medians = _compute_nudge_medians(meta)

        for glyph, svg_path in svg_map.items():
            info = meta.get(glyph, {})
            bbox_h = info.get("bbox_h", 1)
            y_off = info.get("y_offset", 0)
            grp = _baseline_group(glyph)

            # Nudge
            nudge_px = 0.0
            if grp in _NUDGE_GROUPS and grp in set_medians:
                nudge_px = -(set_medians[grp] - y_off)

            ovr = overrides.get(glyph, {})
            scale_mult = ovr.get("scale", 1.0)
            extra_nudge_px = ovr.get("nudge", 0.0)

            desired_h = bbox_h * _px_to_fu * scale_mult
            yoff_frac = y_off / bbox_h if bbox_h > 0 else 0
            total_nudge_fu = (nudge_px + extra_nudge_px) * _px_to_fu

            if set_idx == 0:
                if len(glyph) == 1:
                    base_name = _uni_name(glyph)
                    slot_create = f"font.createChar({_glyph_slot(glyph)}, \"{base_name}\")"
                else:
                    base_name = _lig_glyph_name(glyph)
                    slot_create = f'font.createChar(-1, "{base_name}")'
            else:
                # Alternate glyph — unencoded, named with suffix
                if len(glyph) == 1:
                    base_name = _uni_name(glyph)
                else:
                    base_name = _lig_glyph_name(glyph)
                alt_name = f"{base_name}{suffix}"
                slot_create = f'font.createChar(-1, "{alt_name}")'
                alt_glyphs.setdefault(glyph, []).append(alt_name)

            lines.append(_ff_glyph_transform_code(
                slot_code=slot_create,
                svg_path=svg_path,
                label=f"{glyph} set{set_idx}{suffix}",
                desired_h=desired_h,
                yoff_frac=yoff_frac,
                total_nudge_fu=total_nudge_fu,
            ))

    # ── Ligature lookups (base set only) ──
    all_ligs = []
    for lig in LIGATURES + EXTRA_LIGATURES:
        if lig in sets[0]["svg_map"]:
            components = " ".join(lig)
            name = _lig_glyph_name(lig)
            all_ligs.append((lig, components, name))

    if all_ligs:
        lines.append(textwrap.dedent("""\
            font.addLookup("liga", "gsub_ligature", (), (("liga",(("latn",("dflt")),)),))
            font.addLookupSubtable("liga", "liga-1")
        """))
        for lig, components, name in all_ligs:
            component_tuple = "(" + ",".join(f'"{c}"' for c in lig) + ",)"
            lines.append(f'font["{name}"].addPosSub("liga-1", {component_tuple})\n')

    # ── Contextual alternates (calt) ──
    # NOTE: calt is applied AFTER font generation using fonttools' feaLib
    # (FontForge's mergeFeature/addContextualSubtable produce broken output).
    # We pass alt_glyphs back via a comment the caller can parse, but the
    # actual .fea compilation happens in compile_font_multiset().

    # ── Kerning ──
    per_set_kerns = [s.get("kern", {}) for s in sets]
    klines = _multiset_kern_lines(kern_cfg or {}, per_set_kerns, alt_glyphs)
    if klines:
        lines.append("")
        lines.extend(klines)
        lines.append("")

    # ── Space + generate ──
    lines.append(textwrap.dedent(f"""\
        space = font.createChar(0x0020)
        space.width = {DEFAULT_UPM // 4}

        font.generate("{output_otf}")
        print(f"Generated: {output_otf}")
    """))

    return "\n".join(lines), alt_glyphs


def _add_calt_with_fonttools(otf_path: Path, alt_glyphs: dict[str, list[str]]) -> None:
    """Add calt feature to a compiled font using fonttools' feaLib.

    FontForge's mergeFeature/addContextualSubtable produce broken GSUB
    tables.  fonttools' feaLib compiles correct OpenType features.

    The strategy: define a single-substitution lookup (outside the feature
    block so it's not applied directly), then a contextual lookup that
    triggers it when the preceding glyph is in a context class.

    To avoid strict base/alt/base/alt, we include .alt1 versions of
    SOME characters (even codepoints) in the context class.  This makes
    the substitution pattern depend on the preceding character's identity,
    producing pseudo-random runs of base and alternate glyphs.
    """
    from fontTools.feaLib.builder import addOpenTypeFeatures
    from fontTools.ttLib import TTFont

    if not alt_glyphs:
        return

    # Build context class: ALL base glyphs + alt glyphs of even-codepoint chars.
    # After a base char → always substitute (base is always in @CTX).
    # After an alt char of an even-codepoint original → also substitute (in @CTX).
    # After an alt char of an odd-codepoint original → DON'T substitute (not in @CTX).
    # This breaks the strict alternation into irregular runs.
    ctx_names: list[str] = []
    for glyph in sorted(alt_glyphs.keys()):
        base_name = _glyph_font_name(glyph)
        ctx_names.append(base_name)
        # Include alt versions for even-codepoint single chars
        if len(glyph) == 1 and ord(glyph) % 2 == 0:
            for alt_name in alt_glyphs[glyph]:
                ctx_names.append(alt_name)

    all_ctx = " ".join(ctx_names)

    n_alts = max(len(v) for v in alt_glyphs.values())
    fea_lines: list[str] = []
    fea_lines.append(f"@CTX = [{all_ctx}];")
    fea_lines.append("")

    # Single-sub lookups (outside feature block — only called by reference)
    for alt_idx in range(n_alts):
        alt_num = alt_idx + 1
        fea_lines.append(f"lookup calt_single_{alt_num} {{")
        for glyph, alt_names in sorted(alt_glyphs.items()):
            if alt_num <= len(alt_names):
                base_name = _glyph_font_name(glyph)
                fea_lines.append(f"    sub {base_name} by {alt_names[alt_num - 1]};")
        fea_lines.append(f"}} calt_single_{alt_num};")
        fea_lines.append("")

    # Contextual lookup registered under calt feature
    fea_lines.append("feature calt {")
    for alt_idx in range(n_alts):
        alt_num = alt_idx + 1
        fea_lines.append(f"    lookup calt_ctx_{alt_num} {{")
        for glyph, alt_names in sorted(alt_glyphs.items()):
            if alt_num <= len(alt_names):
                base_name = _glyph_font_name(glyph)
                fea_lines.append(
                    f"        sub @CTX {base_name}' lookup calt_single_{alt_num};"
                )
        fea_lines.append(f"    }} calt_ctx_{alt_num};")
    fea_lines.append("} calt;")

    fea_code = "\n".join(fea_lines)

    font = TTFont(str(otf_path))
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".fea", delete=False
    ) as f:
        f.write(fea_code)
        fea_path = f.name

    addOpenTypeFeatures(font, fea_path)
    Path(fea_path).unlink(missing_ok=True)
    font.save(str(otf_path))


def compile_font_multiset(
    extracted_dirs: list[Path],
    overrides_list: list[dict],
    output_otf: str | Path = "output/Handwriting_MVP.otf",
    dpi: int = 600,
    font_name: str | None = None,
    kern_cfg: dict | None = None,
    per_set_kerns: list[dict] | None = None,
    borrows_list: list[dict] | None = None,
) -> Path:
    """Compile a font from multiple extracted scan sets with calt alternates."""
    output_otf = Path(output_otf)
    output_otf.parent.mkdir(parents=True, exist_ok=True)

    if per_set_kerns is None:
        per_set_kerns = [{} for _ in extracted_dirs]
    if borrows_list is None:
        borrows_list = [{} for _ in extracted_dirs]

    sets = []
    for i, (edir, ovr, skern) in enumerate(zip(extracted_dirs, overrides_list, per_set_kerns)):
        edir = Path(edir)
        metadata = json.loads((edir / "metadata.json").read_text())
        svg_map = vectorize_all(edir, edir / "svgs")
        svg_str_map = {g: str(p.resolve()) for g, p in svg_map.items()}
        sets.append({
            "set_idx": i,
            "svg_map": svg_str_map,
            "metadata": metadata,
            "overrides": ovr,
            "kern": skern,
        })

    # Apply borrows: replace glyphs with versions from other sets
    for i, borrows in enumerate(borrows_list):
        for glyph, source_set_idx in borrows.items():
            source_set_idx = int(source_set_idx)
            if source_set_idx < 0 or source_set_idx >= len(sets):
                click.echo(f"  ⚠ borrow.{glyph!r} = {source_set_idx}: set index out of range, skipping")
                continue
            source_svg = sets[source_set_idx]["svg_map"].get(glyph)
            if source_svg is None:
                click.echo(f"  ⚠ borrow.{glyph!r} = {source_set_idx}: glyph not found in set {source_set_idx}, skipping")
                continue
            sets[i]["svg_map"][glyph] = source_svg

    script, alt_glyphs = _build_multiset_fontforge_script(sets, str(output_otf.resolve()), dpi, font_name=font_name, kern_cfg=kern_cfg)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", prefix="ff_build_", delete=False,
    ) as f:
        f.write(script)
        script_path = f.name

    result = subprocess.run(
        ["fontforge", "-script", script_path],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"FontForge failed (exit {result.returncode}):\n"
            f"STDOUT: {result.stdout}\n"
            f"STDERR: {result.stderr}\n"
            f"Script: {script_path}"
        )

    Path(script_path).unlink(missing_ok=True)

    # Add calt feature via fonttools (FontForge can't produce valid calt)
    if alt_glyphs:
        _add_calt_with_fonttools(output_otf, alt_glyphs)

    return output_otf

def compile_font(
    extracted_dir: str | Path = "output/extracted",
    output_otf: str | Path = "output/Handwriting_MVP.otf",
    dpi: int = 600,
    overrides: dict | None = None,
    font_name: str | None = None,
    kern_cfg: dict | None = None,
) -> Path:
    """Full Module C pipeline: vectorize → assemble → compile .otf."""
    extracted_dir = Path(extracted_dir)
    output_otf = Path(output_otf)
    output_otf.parent.mkdir(parents=True, exist_ok=True)

    svg_dir = extracted_dir / "svgs"
    metadata = json.loads((extracted_dir / "metadata.json").read_text())

    # Step 1: Potrace — bitmap → SVG
    svg_map = vectorize_all(extracted_dir, svg_dir)

    # Step 2: Generate FontForge script
    svg_str_map = {g: str(p.resolve()) for g, p in svg_map.items()}
    script = _build_fontforge_script(
        svg_str_map, metadata,
        str(output_otf.resolve()), dpi, overrides,
        font_name=font_name, kern_cfg=kern_cfg,
    )

    # Step 3: Run FontForge
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", prefix="ff_build_", delete=False,
    ) as f:
        f.write(script)
        script_path = f.name

    result = subprocess.run(
        ["fontforge", "-script", script_path],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"FontForge failed (exit {result.returncode}):\n"
            f"STDOUT: {result.stdout}\n"
            f"STDERR: {result.stderr}\n"
            f"Script: {script_path}"
        )

    # Clean up temp script
    Path(script_path).unlink(missing_ok=True)

    return output_otf
