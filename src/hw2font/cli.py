"""hw2font CLI — single entry point for the full pipeline."""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

import click


def _load_config(config_path: str) -> dict:
    """Load a TOML config file and return the parsed dict."""
    return tomllib.loads(Path(config_path).read_text())


_DEFAULT_WEIGHTS = [{"name": "Regular", "value": 400, "stroke_delta": 0}]


def _parse_weights(cfg: dict) -> list[dict]:
    """Parse ``[[weights]]`` from config, returning a validated list.

    Each entry must have ``name``, ``value`` (100-900), ``stroke_delta`` (int).
    If no weights section is present, returns a single Regular entry.
    """
    raw = cfg.get("weights")
    if not raw:
        return list(_DEFAULT_WEIGHTS)
    weights: list[dict] = []
    for w in raw:
        weights.append({
            "name": str(w.get("name", "Regular")),
            "value": int(w.get("value", 400)),
            "stroke_delta": int(w.get("stroke_delta", 0)),
        })
    return weights or list(_DEFAULT_WEIGHTS)


def _load_overrides(config_path: str | None) -> dict:
    """Load per-glyph overrides from a single-set config file."""
    if not config_path:
        return {}
    path = Path(config_path)
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text())
    return data.get("overrides", {})


def _load_compile_config(config_path: str | None) -> dict:
    """Load single-set compile options from a TOML config file."""
    if not config_path:
        return {}
    path = Path(config_path)
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text())
    return {
        "overrides": data.get("overrides", {}),
        "font_name": data.get("name"),
        "kern_cfg": data.get("kern", {}),
        "space_width": data.get("space_width"),
        "tightness": data.get("tightness", 1.0),
        "autotune": data.get("autotune", {}),
    }


def _merge_autotune_controls(base: dict | None, override: dict | None) -> dict:
    """Merge top-level and set-level autotune controls."""
    merged: dict = {}
    for key in ("disable_scale", "disable_hshift", "disable_kern_pairs", "disable_nudge"):
        values: list[str] = []
        for source in (base or {}, override or {}):
            for item in source.get(key, []):
                item = str(item)
                if item not in values:
                    values.append(item)
        if values:
            merged[key] = values
    return merged


def _apply_borrows(
    borrows_list: list[dict],
    extracted_dirs: list[Path],
) -> None:
    """Copy borrowed glyph PNGs and metadata between extracted sets.

    For each set, ``borrows_list[i]`` maps glyph strings to the source
    set index to borrow from.  Both the PNG file and the metadata.json
    entry are copied so that scaling/positioning stays correct.
    """
    for i, borrows in enumerate(borrows_list):
        if not borrows:
            continue
        dst_meta_path = extracted_dirs[i] / "metadata.json"
        dst_meta = json.loads(dst_meta_path.read_text())
        changed = False
        for glyph, source_idx in borrows.items():
            source_idx = int(source_idx)
            if source_idx < 0 or source_idx >= len(extracted_dirs):
                continue
            src_dir = extracted_dirs[source_idx] / "glyphs"
            dst_dir = extracted_dirs[i] / "glyphs"
            if len(glyph) == 1:
                fname = f"U+{ord(glyph):04X}.png"
            else:
                fname = f"lig_{''.join(glyph)}.png"
            src_file = src_dir / fname
            dst_file = dst_dir / fname
            if not src_file.exists():
                click.echo(f"  ⚠ Set {i}: borrow {glyph!r} from set {source_idx} — not found")
                continue
            shutil.copy2(src_file, dst_file)
            src_meta = json.loads((extracted_dirs[source_idx] / "metadata.json").read_text())
            if glyph in src_meta:
                dst_meta[glyph] = src_meta[glyph]
                changed = True
            click.echo(f"  Set {i}: borrowed {glyph!r} from set {source_idx}")
        if changed:
            dst_meta_path.write_text(json.dumps(dst_meta, indent=2))


@click.group()
@click.version_option(package_name="hw2font")
def main() -> None:
    """hw2font: convert handwriting scans into OpenType fonts."""


@main.command()
@click.option(
    "-o",
    "--output",
    default="output/template.pdf",
    show_default=True,
    type=click.Path(dir_okay=False, writable=True),
    help="Path for the generated PDF template.",
)
def template(output: str) -> None:
    """Generate a printable handwriting template PDF."""
    from hw2font.template.generator import generate_template

    path = generate_template(output)
    click.echo(f"✓ Template saved to {path}")


@main.command()
@click.argument("scans", nargs=-1, required=True, type=click.Path(exists=True))
@click.option(
    "-o",
    "--output",
    default="output/extracted",
    show_default=True,
    type=click.Path(file_okay=False, writable=True),
    help="Directory for extracted glyph images and metadata.",
)
@click.option(
    "--dpi",
    default=600,
    show_default=True,
    type=int,
    help="DPI of the scanned images.",
)
def extract(scans: tuple[str, ...], output: str, dpi: int) -> None:
    """Extract glyphs from scanned template page(s).

    Provide one image per template page, in order (page 1 first).
    """
    from hw2font.extract.pipeline import extract_glyphs

    path, stats = extract_glyphs(list(scans), output, dpi)
    click.echo(f"✓ Extracted {stats['processed']} glyphs → {path}/")
    if stats["empty"]:
        click.echo(f"  ({stats['empty']} empty cells skipped)")


@main.command()
@click.option(
    "-i",
    "--input",
    "extracted_dir",
    default="output/extracted",
    show_default=True,
    type=click.Path(exists=True, file_okay=False),
    help="Directory with extracted glyphs and metadata.json.",
)
@click.option(
    "-o",
    "--output",
    default="output/Handwriting_MVP.otf",
    show_default=True,
    type=click.Path(dir_okay=False, writable=True),
    help="Path for the compiled .otf font file.",
)
@click.option(
    "--dpi",
    default=600,
    show_default=True,
    type=int,
    help="DPI used during scanning (for scale calculations).",
)
@click.option(
    "-c",
    "--config",
    "config_path",
    default=None,
    type=click.Path(dir_okay=False),
    help="TOML config file with per-glyph overrides (scale, nudge).",
)
def compile(extracted_dir: str, output: str, dpi: int, config_path: str | None) -> None:
    """Vectorize extracted glyphs and compile into an OpenType font."""
    from hw2font.compile.builder import compile_font

    cfg = _load_compile_config(config_path)
    overrides = cfg.get("overrides", {})
    if overrides:
        click.echo(f"  Loaded {len(overrides)} glyph override(s) from {config_path}")
    path = compile_font(
        extracted_dir,
        output,
        dpi,
        overrides,
        font_name=cfg.get("font_name"),
        kern_cfg=cfg.get("kern_cfg"),
        space_width=cfg.get("space_width"),
        tightness=cfg.get("tightness", 1.0),
    )
    click.echo(f"✓ Font compiled → {path}")

    from hw2font.webfont import generate_webfont

    webfont_dir = Path(path).parent / "webfonts"
    css_path_out = webfont_dir / f"{Path(path).stem}.css"
    result = generate_webfont(
        font_path=path,
        output_dir=str(webfont_dir),
        emit_woff=True,
        css_path=str(css_path_out),
    )
    for asset_path in result["files"]:
        click.echo(f"  ✓ Webfont → {asset_path}")


@main.command()
@click.argument("config", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "-o",
    "--output",
    default=None,
    type=click.Path(dir_okay=False, writable=True),
    help="Path for the compiled .otf font file (default: derived from config name).",
)
@click.option(
    "--dpi",
    default=600,
    show_default=True,
    type=int,
    help="DPI of the scanned images.",
)
@click.option(
    "--no-autotune",
    is_flag=True,
    help="Disable the autotune pass before preview/final font compilation.",
)
@click.option(
    "--autotune-log",
    default=None,
    type=click.Path(dir_okay=False, writable=True),
    help="Path for the autotune JSON log (default: beside the output font).",
)
@click.option(
    "--autotune-max-iterations",
    default=2,
    show_default=True,
    type=click.IntRange(1, 10),
    help="Maximum number of autotune refinement iterations.",
)
def build(
    config: str,
    output: str | None,
    dpi: int,
    no_autotune: bool,
    autotune_log: str | None,
    autotune_max_iterations: int,
) -> None:
    """Extract + compile all scan sets from a config file into one font.

    The config file lists multiple scan sets, each with their own scans
    and optional per-glyph overrides. The resulting font uses contextual
    alternates (calt) to cycle between glyph variants for natural variety.

    If [[weights]] are defined in the config, produces one OTF per weight
    with appropriate stroke thickening/thinning.

    \b
    Example config (TOML):
        name = "My Handwriting"

        [[sets]]
        scans = ["extract1.png", "extract2.png"]

        [[weights]]
        name = "Regular"
        value = 400
        stroke_delta = 0

        [[weights]]
        name = "Bold"
        value = 700
        stroke_delta = 2
    """
    from hw2font.extract.pipeline import extract_glyphs
    from hw2font.autotune import autotune_build
    from hw2font.compile.builder import (
        apply_stroke_delta, compile_font, compile_font_multiset,
    )
    from hw2font.proof.sheet import generate_proof
    from hw2font.webfont import generate_webfont

    cfg = _load_config(config)
    sets = cfg.get("sets", [])
    if not sets:
        raise click.UsageError("Config file must contain at least one [[sets]] entry")

    font_name: str | None = cfg.get("name")
    kern_cfg: dict = cfg.get("kern", {})
    space_width: int | None = cfg.get("space_width")
    tightness: float = float(cfg.get("tightness", 1.0))
    weights = _parse_weights(cfg)
    base_stem = (font_name or "Handwriting").replace(" ", "_")

    if output is None:
        output = str(Path("output") / f"{base_stem}.otf")

    click.echo(f"Building font from {len(sets)} scan set(s), {len(weights)} weight(s)...")
    if font_name:
        click.echo(f"  Font name: {font_name}")
    autotune = not no_autotune

    extracted_dirs: list[Path] = []
    overrides_list: list[dict] = []
    per_set_kerns: list[dict] = []
    borrows_list: list[dict] = []
    autotune_controls: list[dict] = []
    output_base = Path("output/extracted")
    autotune_cfg: dict = cfg.get("autotune", {})

    for i, s in enumerate(sets):
        scans = s.get("scans", [])
        if not scans:
            raise click.UsageError(f"Set {i} has no scans")

        out_dir = output_base / f"set{i}"
        click.echo(f"  Set {i}: extracting {len(scans)} scan(s)...")
        extract_glyphs(scans, str(out_dir), dpi)
        click.echo(f"    ✓ Extracted → {out_dir}/")

        extracted_dirs.append(out_dir)
        overrides_list.append(s.get("overrides", {}))
        per_set_kerns.append(s.get("kern", {}))
        borrows_list.append(s.get("borrow", {}))
        autotune_controls.append(_merge_autotune_controls(autotune_cfg, s.get("autotune", {})))

    # Apply borrows at the extracted-glyph level (copy PNGs + metadata)
    _apply_borrows(borrows_list, extracted_dirs)

    if autotune:
        autotune_path = autotune_log
        if autotune_path is None:
            assert output is not None
            autotune_path = str(Path(output).with_name(base_stem + "_autotune.json"))
        click.echo(f"  Autotune: analyzing {len(extracted_dirs)} set(s)...")
        overrides_list, kern_cfg, per_set_kerns, artifacts = autotune_build(
            extracted_dirs=extracted_dirs,
            overrides_list=overrides_list,
            kern_cfg=kern_cfg,
            per_set_kerns=per_set_kerns,
            controls_list=autotune_controls,
            log_path=autotune_path,
            max_iterations=autotune_max_iterations,
            tightness=tightness,
        )
        click.echo(
            "    "
            f"✓ Autotune applied {artifacts['change_count']} change(s) in "
            f"{artifacts['iterations_run']} iteration(s)"
        )
        click.echo(f"    ✓ Log → {artifacts['json_log']}")
        click.echo(f"    ✓ Text log → {artifacts['text_log']}")

    # Generate per-set proof sheets (using Regular weight glyphs)
    for i, (edir, ovr, skern) in enumerate(zip(extracted_dirs, overrides_list, per_set_kerns)):
        merged_kern = {**kern_cfg, **skern} if skern else kern_cfg
        tmp_otf = output_base / f"set{i}" / "preview.otf"
        click.echo(f"  Set {i}: compiling preview font...")
        compile_font(
            edir, tmp_otf, dpi,
            overrides=ovr, font_name=font_name, kern_cfg=merged_kern,
            space_width=space_width, tightness=tightness,
        )
        proof_path = Path(f"output/proof_set{i}.png")
        generate_proof(tmp_otf, proof_path)
        tmp_otf.unlink(missing_ok=True)
        click.echo(f"    ✓ Proof → {proof_path}")

    # ── Compile each weight ──
    compiled_otfs: list[tuple[Path, dict]] = []
    multi_weight = len(weights) > 1

    for w in weights:
        w_name = w["name"]
        w_value = w["value"]
        w_delta = w["stroke_delta"]

        if multi_weight:
            w_stem = f"{base_stem}-{w_name}"
        else:
            w_stem = base_stem

        w_output = Path("output") / f"{w_stem}.otf"

        # Apply morphological stroke delta if non-zero
        if w_delta != 0:
            click.echo(f"  Weight {w_name}: applying stroke_delta={w_delta:+d}...")
            weight_dirs: list[Path] = []
            for i, edir in enumerate(extracted_dirs):
                w_dir = output_base / f"set{i}_weight_{w_name.lower()}"
                apply_stroke_delta(edir, w_dir, w_delta)
                weight_dirs.append(w_dir)
        else:
            weight_dirs = list(extracted_dirs)

        click.echo(f"  Weight {w_name} ({w_value}): compiling...")
        path = compile_font_multiset(
            weight_dirs, overrides_list, w_output, dpi,
            font_name=font_name, kern_cfg=kern_cfg, per_set_kerns=per_set_kerns,
            borrows_list=borrows_list, space_width=space_width, tightness=tightness,
            weight_value=w_value, weight_name=w_name,
        )
        click.echo(f"  ✓ {w_name} → {path}")
        compiled_otfs.append((path, w))

        # Per-weight proof for non-Regular
        if w_delta != 0:
            proof_path = Path(f"output/proof_set0_{w_name}.png")
            generate_proof(path, proof_path)
            click.echo(f"    ✓ Proof → {proof_path}")

    # ── Generate webfonts ──
    webfont_dir = Path("output/webfonts")
    all_webfont_files: list[Path] = []
    all_css_blocks: list[str] = []

    for otf_path, w in compiled_otfs:
        result = generate_webfont(
            font_path=otf_path,
            output_dir=str(webfont_dir),
            emit_woff=True,
            font_weight=str(w["value"]),
        )
        all_webfont_files.extend(result["files"])
        all_css_blocks.append(result["css"])
        for asset_path in result["files"]:
            click.echo(f"  ✓ Webfont → {asset_path}")

    # Write combined CSS if multi-weight
    css_path = webfont_dir / f"{base_stem}.css"
    css_path.parent.mkdir(parents=True, exist_ok=True)
    css_path.write_text("\n".join(all_css_blocks))

    click.echo(f"✓ Built {len(compiled_otfs)} weight(s), {len(sets)} variant set(s)")


@main.command()
@click.option(
    "-f",
    "--font",
    default="output/Handwriting_MVP.otf",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the .otf font file to proof.",
)
@click.option(
    "-o",
    "--output",
    default="output/proof.png",
    show_default=True,
    type=click.Path(dir_okay=False, writable=True),
    help="Path for the proof image.",
)
@click.option("--open/--no-open", default=True, help="Open the proof after generating.")
def proof(font: str, output: str, open: bool) -> None:
    """Generate a proof sheet image from a compiled font."""
    from hw2font.proof.sheet import generate_proof

    path = generate_proof(font, output)
    click.echo(f"✓ Proof sheet → {path}")
    if open:
        import subprocess
        subprocess.run(["open", str(path)])


@main.command()
@click.argument("font", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "-o",
    "--output-dir",
    default=None,
    type=click.Path(file_okay=False, writable=True),
    help="Directory for generated webfont assets (default: sibling webfonts/ dir).",
)
@click.option(
    "--family",
    default=None,
    help="Override the CSS font-family name.",
)
@click.option(
    "--url-prefix",
    default=".",
    show_default=True,
    help="URL prefix to use inside the emitted CSS.",
)
@click.option(
    "--with-woff/--woff2-only",
    default=False,
    help="Also emit a .woff fallback alongside .woff2.",
)
@click.option(
    "--css",
    "css_path",
    default=None,
    type=click.Path(dir_okay=False, writable=True),
    help="Path for the emitted CSS snippet (default: beside the webfont files).",
)
def webfont(
    font: str,
    output_dir: str | None,
    family: str | None,
    url_prefix: str,
    with_woff: bool,
    css_path: str | None,
) -> None:
    """Convert an OTF/TTF font into WOFF2 webfont assets and CSS."""
    from hw2font.webfont import generate_webfont

    result = generate_webfont(
        font,
        output_dir=output_dir,
        family_name=family,
        url_prefix=url_prefix,
        emit_woff=with_woff,
        css_path=css_path,
    )
    for path in result["files"]:
        click.echo(f"✓ Webfont → {path}")
    click.echo(f"✓ CSS → {result['css_path']}")
    click.echo("")
    click.echo(result["css"])
