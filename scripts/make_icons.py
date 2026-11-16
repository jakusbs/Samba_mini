"""Build the Samba_mini / Samba_controls icons from Samba's application icon.

    python scripts/make_icons.py <path to Samba's samba_icon_512.png>

Samba's dancer artwork (jakusbs/Samba, Samba_main/samba_icon_512.png) is the
same lab's visual identity; each app gets a small badge so the two desktop
icons can be told apart: green "mini" for Samba_mini, a blue gear for
Samba_controls.  Writes assets/<name>.ico (16-256 px) and assets/<name>.png.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[1]
FONTS = Path(r"C:\Windows\Fonts")
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]

BADGES = {
    # name: (badge colour, text colour, text, font file)
    "samba_mini": ("#a6e3a1", "#1e1e2e", "mini", "segoeuib.ttf"),
    "samba_controls": ("#89b4fa", "#1e1e2e", "\u2699", "seguisym.ttf"),
}


def badge(base: Image.Image, fill: str, ink: str, text: str, font_file: str) -> Image.Image:
    img = base.convert("RGBA").resize((512, 512), Image.LANCZOS)
    d = ImageDraw.Draw(img)
    r = 118                                   # badge radius
    cx, cy = 512 - r - 6, 512 - r - 6
    d.ellipse((cx - r - 10, cy - r - 10, cx + r + 10, cy + r + 10), fill="#11111b")   # dark ring
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)
    size = 112 if len(text) > 1 else 190
    font = ImageFont.truetype(str(FONTS / font_file), size)
    box = d.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text((cx - w / 2 - box[0], cy - h / 2 - box[1]), text, font=font, fill=ink)
    return img


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(__doc__)
        return 2
    base = Image.open(args[0])
    out = REPO / "assets"
    out.mkdir(exist_ok=True)
    for name, (fill, ink, text, font) in BADGES.items():
        img = badge(base, fill, ink, text, font)
        img.resize((256, 256), Image.LANCZOS).save(out / f"{name}.png")
        img.save(out / f"{name}.ico", sizes=SIZES)
        print(out / f"{name}.ico")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
