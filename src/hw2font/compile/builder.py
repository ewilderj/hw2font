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
_DESCENDER_THRESHOLD = 0.25  # if >25% of bbox is below baseline, treat as descender


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


def _kern_lines(kern_cfg: dict) -> list[str]:
    """Generate FontForge Python lines for shape-based kerning.

    kern_cfg supports:
      - Class defaults: overhang_lc, round_lc, straight_lc, open_lc
      - Per-pair overrides: pairs.Ta = -150 (first char + second char)
      - Right-side blanket: right.x = -40 (x followed by anything)
      - Left-side blanket:  left.o = -20 (anything followed by o)
    Negative values tighten spacing.  Priority: pairs > right/left > classes.
    """
    merged = {**DEFAULT_KERN, **kern_cfg}
    pair_overrides: dict = merged.pop("pairs", {})
    right_overrides: dict = merged.pop("right", {})
    left_overrides: dict = merged.pop("left", {})

    def _name(c: str) -> str:
        return f"uni{ord(c):04X}"

    all_letters = list("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
    lc = list("abcdefghijklmnopqrstuvwxyz")
    kern_map: dict[tuple[str, str], int] = {}

    # 1. Class defaults: each UC shape class → all lowercase
    for class_key, uc_chars in _UC_SHAPE_CLASSES.items():
        value = merged.get(f"{class_key}_lc", 0)
        if value != 0:
            for u in uc_chars:
                for l in lc:
                    kern_map[(_name(u), _name(l))] = value

    # 2. Right-side blanket: glyph followed by any letter
    for char, value in right_overrides.items():
        if len(char) == 1:
            for other in all_letters:
                kern_map[(_name(char), _name(other))] = value

    # 3. Left-side blanket: any letter followed by glyph
    for char, value in left_overrides.items():
        if len(char) == 1:
            for other in all_letters:
                kern_map[(_name(other), _name(char))] = value

    # 4. Per-pair overrides (highest priority)
    for pair_str, value in pair_overrides.items():
        if len(pair_str) == 2:
            kern_map[(_name(pair_str[0]), _name(pair_str[1]))] = value

    # Drop zero-value pairs
    kern_map = {k: v for k, v in kern_map.items() if v != 0}
    if not kern_map:
        return []

    lines: list[str] = []
    lines.append('font.addLookup("kern", "gpos_pair", (), '
                  '(("kern",(("latn",("dflt")),)),))')
    lines.append('font.addLookupSubtable("kern", "kern-1")')

    for (g1, g2), value in sorted(kern_map.items()):
        lines.append(f'font["{g1}"].addPosSub("kern-1", "{g2}", {value})')

    return lines


def _build_fontforge_script(
    svg_map: dict[str, str],
    metadata: dict,
    metadata_path: str,
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
            glyph_name = f"uni{ord(glyph):04X}"
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
    # This corrects glyphs the user didn't perfectly place on the baseline
    # without changing their size.
    # Only applies to lowercase and ligatures — uppercase/digits/symbols
    # don't need nudging since they're already well-anchored.
    _NON_DESC_LC = _XH_ONLY | _LC_ASCENDER
    _NUDGE_GROUPS = {"lc_nondesc", "lc_desc"}
    import statistics as _stats

    def _baseline_group(g: str) -> str:
        """Group glyphs by expected baseline behaviour."""
        if len(g) == 1:
            if g in _NON_DESC_LC:
                return "lc_nondesc"
            if g in _LC_DESCENDER:
                return "lc_desc"
        elif len(g) > 1:
            if any(c in _LC_DESCENDER for c in g):
                return "lc_desc"
            return "lc_nondesc"
        return "other"

    groups: dict[str, list[float]] = {}
    for e in glyph_entries:
        grp = _baseline_group(e["glyph"])
        e["bl_group"] = grp
        groups.setdefault(grp, []).append(e["y_offset"])

    group_medians = {
        grp: _stats.median(offsets) for grp, offsets in groups.items()
    }

    for e in glyph_entries:
        grp = e["bl_group"]
        if grp in _NUDGE_GROUPS:
            # Negative sign: positive nudge_px means glyph floats high,
            # needs to move DOWN in font coordinates (negative Y).
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

        # ── Pass 1: Import all glyphs ──
    """))

    for e in glyph_entries:
        lines.append(textwrap.dedent(f"""\
            g = {e["slot_code"]}
            g.importOutlines("{e["svg"]}")
        """))

    lines.append(textwrap.dedent("""\

        # ── Pass 2: Scale and position each glyph ──
    """))

    for e in glyph_entries:
        glyph = e["glyph"]
        bbox_h = e["bbox_h"]
        y_off = e["y_offset"]
        nudge_px = e["nudge_px"]

        # Apply per-glyph overrides from config
        ovr = overrides.get(glyph, {})
        scale_mult = ovr.get("scale", 1.0)
        extra_nudge_px = ovr.get("nudge", 0.0)

        accessor = e["slot_code"]
        desired_h = bbox_h * _px_to_fu * scale_mult
        yoff_frac = y_off / bbox_h if bbox_h > 0 else 0
        total_nudge_fu = (nudge_px + extra_nudge_px) * _px_to_fu

        lines.append(textwrap.dedent(f"""\
            # ── {glyph} (nudge {nudge_px:+.1f}px, ovr scale={scale_mult}, ovr nudge={extra_nudge_px:+.1f}px) ──
            g = {accessor}
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
        """))

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
) -> str:
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
    _NON_DESC_LC = _XH_ONLY | _LC_ASCENDER
    _NUDGE_GROUPS = {"lc_nondesc", "lc_desc"}
    import statistics as _stats

    def _bl_group(g: str) -> str:
        if len(g) == 1:
            if g in _NON_DESC_LC:
                return "lc_nondesc"
            if g in _LC_DESCENDER:
                return "lc_desc"
        elif len(g) > 1:
            if any(c in _LC_DESCENDER for c in g):
                return "lc_desc"
            return "lc_nondesc"
        return "other"

    # Compute group medians from set 0
    groups: dict[str, list[float]] = {}
    for g, info in p_meta.items():
        grp = _bl_group(g)
        groups.setdefault(grp, []).append(info.get("y_offset", 0))
    group_medians = {grp: _stats.median(offs) for grp, offs in groups.items()}

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
        set_groups: dict[str, list[float]] = {}
        for g, info in meta.items():
            grp = _bl_group(g)
            set_groups.setdefault(grp, []).append(info.get("y_offset", 0))
        set_medians = {grp: _stats.median(offs) for grp, offs in set_groups.items()}

        for glyph, svg_path in svg_map.items():
            info = meta.get(glyph, {})
            bbox_h = info.get("bbox_h", 1)
            y_off = info.get("y_offset", 0)
            grp = _bl_group(glyph)

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
                    base_name = f"uni{ord(glyph):04X}"
                    slot_create = f"font.createChar({_glyph_slot(glyph)}, \"{base_name}\")"
                else:
                    base_name = _lig_glyph_name(glyph)
                    slot_create = f'font.createChar(-1, "{base_name}")'
            else:
                # Alternate glyph — unencoded, named with suffix
                if len(glyph) == 1:
                    base_name = f"uni{ord(glyph):04X}"
                    alt_name = f"{base_name}{suffix}"
                else:
                    base_name = _lig_glyph_name(glyph)
                    alt_name = f"{base_name}{suffix}"
                slot_create = f'font.createChar(-1, "{alt_name}")'
                alt_glyphs.setdefault(glyph, []).append(alt_name)

            lines.append(textwrap.dedent(f"""\
                # ── {glyph} set{set_idx}{suffix} ──
                g = {slot_create}
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
            """))

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
    if n_sets > 1 and alt_glyphs:
        # Generate .fea feature code for calt cycling.
        # Split printable ASCII into N-1 groups; after group K char, use alt(K+1).
        # This gives pseudo-random variation.
        all_chars = sorted(
            set(g for g in sets[0]["svg_map"] if len(g) == 1),
            key=lambda c: ord(c),
        )
        n_alts = n_sets - 1
        char_groups: list[list[str]] = [[] for _ in range(n_alts)]
        for i, c in enumerate(all_chars):
            char_groups[i % n_alts].append(c)

        fea_lines = []
        for group_idx in range(n_alts):
            group_names = []
            for c in char_groups[group_idx]:
                if c == " ":
                    group_names.append("space")
                else:
                    group_names.append(f"uni{ord(c):04X}")
            fea_lines.append(
                f"@ctx_group{group_idx} = [{' '.join(group_names)}];"
            )

        fea_lines.append("")
        fea_lines.append("feature calt {")
        for group_idx in range(n_alts):
            alt_suffix_idx = group_idx + 1
            fea_lines.append(f"  lookup calt_alt{alt_suffix_idx} {{")
            for glyph, alt_names in sorted(alt_glyphs.items()):
                if alt_suffix_idx <= len(alt_names):
                    alt_name = alt_names[alt_suffix_idx - 1]
                    if len(glyph) == 1:
                        base_name = f"uni{ord(glyph):04X}"
                    else:
                        base_name = _lig_glyph_name(glyph)
                    fea_lines.append(
                        f"    sub @ctx_group{group_idx} {base_name}' by {alt_name};"
                    )
            fea_lines.append(f"  }} calt_alt{alt_suffix_idx};")
        fea_lines.append("} calt;")

        fea_code = "\n".join(fea_lines)

        # Write .fea to a temp file and merge
        lines.append(textwrap.dedent(f"""\
            import tempfile, os
            fea_code = {fea_code!r}
            fea_file = tempfile.NamedTemporaryFile(
                mode="w", suffix=".fea", delete=False
            )
            fea_file.write(fea_code)
            fea_file.close()
            font.mergeFeature(fea_file.name)
            os.unlink(fea_file.name)
        """))

    # ── Kerning ──
    klines = _kern_lines(kern_cfg or {})
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

    return "\n".join(lines)


def compile_font_multiset(
    extracted_dirs: list[Path],
    overrides_list: list[dict],
    output_otf: str | Path = "output/Handwriting_MVP.otf",
    dpi: int = 600,
    font_name: str | None = None,
    kern_cfg: dict | None = None,
) -> Path:
    """Compile a font from multiple extracted scan sets with calt alternates."""
    output_otf = Path(output_otf)
    output_otf.parent.mkdir(parents=True, exist_ok=True)

    sets = []
    for i, (edir, ovr) in enumerate(zip(extracted_dirs, overrides_list)):
        edir = Path(edir)
        metadata = json.loads((edir / "metadata.json").read_text())
        svg_map = vectorize_all(edir, edir / "svgs")
        svg_str_map = {g: str(p.resolve()) for g, p in svg_map.items()}
        sets.append({
            "set_idx": i,
            "svg_map": svg_str_map,
            "metadata": metadata,
            "overrides": ovr,
        })

    script = _build_multiset_fontforge_script(sets, str(output_otf.resolve()), dpi, font_name=font_name, kern_cfg=kern_cfg)

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
    metadata_path = str((extracted_dir / "metadata.json").resolve())
    script = _build_fontforge_script(
        svg_str_map, metadata, metadata_path,
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
