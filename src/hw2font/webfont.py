"""Helpers for converting OTF fonts into webfont assets."""

from __future__ import annotations

from pathlib import Path

from fontTools.ttLib import TTFont


def _join_url_prefix(prefix: str, filename: str) -> str:
    prefix = prefix.strip()
    if not prefix or prefix == ".":
        return filename
    return f"{prefix.rstrip('/')}/{filename}"


def _infer_family_name(font: TTFont, fallback: str) -> str:
    name_table = font["name"]
    for name_id in (16, 1):
        for record in name_table.names:
            if record.nameID == name_id:
                try:
                    text = record.toUnicode().strip()
                except Exception:
                    continue
                if text:
                    return text
    return fallback


def _css_font_face(
    family_name: str,
    sources: list[tuple[str, str]],
    *,
    font_weight: str = "normal",
    font_style: str = "normal",
    font_display: str = "swap",
) -> str:
    src_lines = ",\n       ".join(
        f'url("{url}") format("{fmt}")'
        for url, fmt in sources
    )
    return (
        "@font-face {\n"
        f'  font-family: "{family_name}";\n'
        f"  src: {src_lines};\n"
        f"  font-weight: {font_weight};\n"
        f"  font-style: {font_style};\n"
        f"  font-display: {font_display};\n"
        "}\n"
    )


def generate_webfont(
    font_path: str | Path,
    *,
    output_dir: str | Path | None = None,
    family_name: str | None = None,
    url_prefix: str = ".",
    emit_woff: bool = False,
    css_path: str | Path | None = None,
    font_weight: str = "normal",
) -> dict[str, object]:
    """Convert an OTF/TTF font into webfont assets and CSS."""
    font_path = Path(font_path)
    if output_dir is None:
        output_dir = font_path.parent / "webfonts"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    base_font = TTFont(str(font_path))
    resolved_family = family_name or _infer_family_name(base_font, font_path.stem)

    generated_files: list[Path] = []
    sources: list[tuple[str, str]] = []
    formats = ["woff2"]
    if emit_woff:
        formats.append("woff")

    for flavor in formats:
        out_path = output_dir / f"{font_path.stem}.{flavor}"
        font = TTFont(str(font_path))
        font.flavor = flavor
        font.save(str(out_path))
        generated_files.append(out_path)
        sources.append((_join_url_prefix(url_prefix, out_path.name), flavor))

    css = _css_font_face(resolved_family, sources, font_weight=font_weight)
    if css_path is None:
        css_path = output_dir / f"{font_path.stem}.css"
    css_path = Path(css_path)
    css_path.parent.mkdir(parents=True, exist_ok=True)
    css_path.write_text(css)

    return {
        "family_name": resolved_family,
        "files": generated_files,
        "css_path": css_path,
        "css": css,
    }
