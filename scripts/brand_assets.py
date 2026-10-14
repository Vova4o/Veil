"""Generate the text README banner from brand.json.

The app icon comes from the logo artwork (desktop/assets/logo.svg) via scripts/render_icon.mjs,
so a rename does not touch it.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BG, CARD, LINE = (13, 13, 14), (20, 21, 28), (38, 39, 48)
TEXT, DIM = (240, 240, 244), (160, 162, 175)

FONT_DIRS = (
    Path("C:/Windows/Fonts"),
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/Library/Fonts"),
    Path("/System/Library/Fonts/Supplemental"),
)
FONT_FILES = {
    "bold": ("segoeuib.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"),
    "regular": ("segoeui.ttf", "DejaVuSans.ttf", "Arial.ttf"),
    "italic": ("segoeuii.ttf", "DejaVuSans-Oblique.ttf", "Arial Italic.ttf"),
}


def _font(style: str, size: int) -> ImageFont.FreeTypeFont:
    for directory in FONT_DIRS:
        for name in FONT_FILES[style]:
            if (directory / name).is_file():
                return ImageFont.truetype(str(directory / name), size)
    raise FileNotFoundError(f"No {style} font found in {', '.join(map(str, FONT_DIRS))}")


def _spaced(draw: ImageDraw.ImageDraw, center: tuple[float, float], text: str, font, fill, spacing: float) -> None:
    widths = [draw.textlength(char, font=font) for char in text]
    x = center[0] - (sum(widths) + spacing * (len(text) - 1)) / 2
    for char, width in zip(text, widths, strict=True):
        draw.text((x, center[1]), char, font=font, fill=fill, anchor="lm")
        x += width + spacing


def generate(root: Path, brand: dict[str, str]) -> list[Path]:
    """Write the banner for ``brand`` under ``root``; return the files written."""
    width, height = 1000, 350
    banner = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(banner)
    draw.rounded_rectangle((8, 8, width - 9, height - 9), radius=22, fill=CARD)
    _spaced(draw, (width / 2, 105), brand["name"].upper(), _font("bold", 78), TEXT, 36)
    _spaced(draw, (width / 2, 175), brand["subtitle"].upper(), _font("regular", 20), TEXT, 2)
    draw.line((40, 218, width - 40, 218), fill=LINE, width=2)
    draw.text((width / 2, 272), brand["tagline"], font=_font("italic", 30), fill=DIM, anchor="mm")
    banner_png = root / "docs/assets/readme-banner.png"
    banner.save(banner_png)
    return [banner_png]
