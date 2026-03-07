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


def _build_fontforge_script(
    svg_map: dict[str, str],
    metadata: dict,
    metadata_path: str,
    output_otf: str,
    dpi: int,
) -> str:
    """Generate a FontForge Python script as a string."""

    lines: list[str] = []

    glyph_entries: list[dict] = []
    for glyph, svg_path in svg_map.items():
        info = metadata.get(glyph, {})
        y_off = info.get("y_offset", 0)
        bbox_h = info.get("bbox_h", 1)

        if len(glyph) == 1:
            slot = f"font.createChar({_glyph_slot(glyph)})"
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
    _NON_DESC_LC = _XH_ONLY | _LC_ASCENDER
    import statistics as _stats

    def _baseline_group(g: str, y_offset: float, bbox_h: float) -> str:
        """Group glyphs by expected baseline behaviour."""
        if len(g) == 1:
            if g in _NON_DESC_LC:
                return "lc_nondesc"
            if g in _LC_DESCENDER:
                return "lc_desc"
            if g.isupper():
                # UC with real descenders (Q, Y, J-sometimes) get own group
                if bbox_h > 0 and y_offset / bbox_h > _DESCENDER_THRESHOLD:
                    return "uc_desc"
                return "uc_digit"
            if g.isdigit():
                return "uc_digit"
        elif len(g) > 1:
            if any(c in _LC_DESCENDER for c in g):
                return "lc_desc"
            return "lc_nondesc"
        return "sym"

    groups: dict[str, list[float]] = {}
    for e in glyph_entries:
        grp = _baseline_group(e["glyph"], e["y_offset"], e["bbox_h"])
        e["bl_group"] = grp
        groups.setdefault(grp, []).append(e["y_offset"])

    group_medians = {
        grp: _stats.median(offsets) for grp, offsets in groups.items()
    }

    for e in glyph_entries:
        grp = e["bl_group"]
        e["nudge_px"] = group_medians[grp] - e["y_offset"]

    lines.append(textwrap.dedent(f"""\
        import fontforge
        import psMat
        import json

        font = fontforge.font()
        font.familyname = "{FONT_FAMILY}"
        font.fontname = "{FONT_NAME}"
        font.fullname = "{FONT_FAMILY} MVP"
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

        accessor = e["slot_code"]
        desired_h = bbox_h * _px_to_fu
        yoff_frac = y_off / bbox_h if bbox_h > 0 else 0
        nudge_fu = nudge_px * _px_to_fu

        lines.append(textwrap.dedent(f"""\
            # ── {glyph} (nudge {nudge_px:+.1f}px) ──
            g = {accessor}
            bb = g.boundingBox()
            svg_h = bb[3] - bb[1]
            if svg_h > 0:
                sc = {desired_h:.2f} / svg_h
                g.transform(psMat.scale(sc))
                bb = g.boundingBox()
                descent_units = (bb[3] - bb[1]) * {yoff_frac:.4f}
                shift_y = -descent_units - bb[1] + {nudge_fu:.2f}
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

    # Set a space glyph
    lines.append(textwrap.dedent(f"""\
        # Space glyph
        space = font.createChar(0x0020)
        space.width = {DEFAULT_UPM // 4}

        font.generate("{output_otf}")
        print(f"Generated: {output_otf}")
    """))

    return "\n".join(lines)


# ── Public API ────────────────────────────────────────────────────────

def compile_font(
    extracted_dir: str | Path = "output/extracted",
    output_otf: str | Path = "output/Handwriting_MVP.otf",
    dpi: int = 600,
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
    script = _build_fontforge_script(svg_str_map, metadata, metadata_path, str(output_otf.resolve()), dpi)

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
