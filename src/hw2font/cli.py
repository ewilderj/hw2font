"""hw2font CLI — single entry point for the full pipeline."""

from __future__ import annotations

import tomllib
from pathlib import Path

import click


def _load_overrides(config_path: str | None) -> dict:
    """Load per-glyph overrides from a TOML config file.

    Expected format::

        [overrides.a]
        scale = 0.8

        [overrides.i]
        scale = 0.9
        nudge = 10    # pixels up (positive = up)
    """
    if not config_path:
        return {}
    path = Path(config_path)
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text())
    return data.get("overrides", {})


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

    overrides = _load_overrides(config_path)
    if overrides:
        click.echo(f"  Loaded {len(overrides)} glyph override(s) from {config_path}")
    path = compile_font(extracted_dir, output, dpi, overrides)
    click.echo(f"✓ Font compiled → {path}")


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
