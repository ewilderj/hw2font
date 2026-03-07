"""Proof sheet generator — renders sample text using the compiled font."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


_SAMPLES = [
    ("Uppercase (60px)", "ABCDEFGHIJKLMNOPQRSTUVWXYZ", 60, 90),
    ("Lowercase (60px)", "abcdefghijklmnopqrstuvwxyz", 60, 90),
    ("Digits (60px)", "0123456789", 60, 90),
    ("Symbols (48px)", "!@#$%&()[]{}.,;:\"'?-+=/<>\\|~^`_", 48, 75),
    ("Special (48px)", "\u00a3\u00b0\u2665\u263a", 48, 75),
    ("Mixed baseline (60px)", "Hxgpjy Quickly Brown Fox", 60, 90),
    ("Pangram (36px)", "The quick brown fox jumps over the lazy dog.", 36, 55),
    (None, "Pack my box with five dozen liquor jugs!", 36, 55),
    (None, "Grumpy wizards make toxic brew for the jovial queen.", 36, 55),
    ("Body text (24px)", "Dear friend, I wanted to write you a quick note to say hello. This is what", 24, 38),
    (None, "my handwriting looks like when digitized into a font. The quick brown fox", 24, 38),
    (None, "jumps over the lazy dog. How vexingly quick daft zebras jump!", 24, 38),
    (None, "Every good boy does fine. She sells sea shells by the sea shore.", 24, 38),
]

_SYS_FONT = "/System/Library/Fonts/Helvetica.ttc"


def generate_proof(
    font_path: str | Path,
    output_path: str | Path = "output/proof.png",
    width: int = 2400,
) -> Path:
    """Render a proof sheet and save as PNG."""
    font_path = Path(font_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Pre-calculate total height
    total_h = 40  # top padding
    for label, _, _, spacing in _SAMPLES:
        total_h += spacing + (18 if label else 0)
    total_h += 40  # bottom padding

    img = Image.new("RGB", (width, total_h), "white")
    draw = ImageDraw.Draw(img)

    # Cache fonts by size
    font_cache: dict[int, ImageFont.FreeTypeFont] = {}
    for _, _, size, _ in _SAMPLES:
        if size not in font_cache:
            font_cache[size] = ImageFont.truetype(str(font_path), size)

    try:
        label_font = ImageFont.truetype(_SYS_FONT, 13)
    except OSError:
        label_font = ImageFont.load_default()

    y = 30
    for label, text, size, spacing in _SAMPLES:
        # Light baseline reference
        draw.line([(20, y + spacing + 2), (width - 20, y + spacing + 2)],
                  fill="#f0f0f0", width=1)
        if label:
            draw.text((20, y), label, fill="#aaa", font=label_font)
            y += 18
        draw.text((20, y), text, fill="black", font=font_cache[size])
        y += spacing

    img.save(str(output_path))
    return output_path
