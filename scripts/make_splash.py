"""Build the Samba_mini / Samba_controls splash screens from Samba's splash artwork.

    python scripts/make_splash.py <path to Samba's samba_splash.svg>

Samba's splash (jakusbs/Samba, Samba_main/samba_splash.svg: dancer, ETH logo,
Intermag Lab) is the lab's visual identity.  Its text is replaced: the title
gets the same coloured badge as the app icon (green "mini", blue "controls";
see make_icons.py) and the credit line names the author.  The text is drawn
with Qt and Segoe UI, so run this on Windows (not with QT_QPA_PLATFORM=offscreen,
which has no fonts).  Writes assets/<name>_splash.png at 2x (1440 x 960);
the app scales it to the screen and adds the version and status lines.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from PyQt6.QtCore import QByteArray, QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QGuiApplication, QImage, QPainter
from PyQt6.QtSvg import QSvgRenderer

REPO = Path(__file__).resolve().parents[1]
W, H, SCALE = 720, 480, 2                 # SVG units; rendered at 2x for high-DPI screens
FONT = "Segoe UI"
AUTHOR = "Jakub Strnad"

SPLASHES = {
    # name: (badge text, badge colour)
    "samba_mini": ("mini", "#a6e3a1"),
    "samba_controls": ("controls", "#89b4fa"),
}


def _font(px: float, weight: QFont.Weight, spacing: float = 0.0) -> QFont:
    f = QFont(FONT)
    f.setPixelSize(round(px * SCALE))
    f.setWeight(weight)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing * SCALE)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


def _width(f: QFont, text: str, spacing: float = 0.0) -> float:
    """Advance width without the letter spacing Qt adds after the last character."""
    return QFontMetricsF(f).horizontalAdvance(text) - spacing * SCALE


def _centered(p: QPainter, text: str, f: QFont, colour: str, baseline: float, spacing: float = 0.0) -> None:
    p.setFont(f)
    p.setPen(QColor(colour))
    p.drawText(QPointF(W / 2 * SCALE - _width(f, text, spacing) / 2, baseline * SCALE), text)


def background(svg_path: Path) -> QImage:
    """Samba's splash without its text (title, credits, lab name)."""
    svg = svg_path.read_text(encoding="utf-8")
    svg = re.sub(r"<text\b[^>]*>.*?</text>", "", svg, flags=re.S)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        raise SystemExit(f"cannot read {svg_path}")
    img = QImage(W * SCALE, H * SCALE, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    renderer.render(p, QRectF(0, 0, W * SCALE, H * SCALE))
    p.end()
    return img


def splash(base: QImage, badge: str, fill: str) -> QImage:
    img = base.copy()
    p = QPainter(img)
    p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)

    # Title: "SAMBA" as in Samba, followed by the app's badge pill
    title_f = _font(44, QFont.Weight.ExtraBold, 8)
    badge_f = _font(17, QFont.Weight.Bold, 0.5)
    title_w = _width(title_f, "SAMBA", 8)
    pad_x, pill_h, gap = 11 * SCALE, 27 * SCALE, 14 * SCALE
    pill_w = _width(badge_f, badge, 0.5) + 2 * pad_x
    x0 = W / 2 * SCALE - (title_w + gap + pill_w) / 2
    baseline = 342 * SCALE
    p.setFont(title_f)
    p.setPen(QColor("#cdd6f4"))
    p.drawText(QPointF(x0, baseline), "SAMBA")

    cap = QFontMetricsF(title_f).capHeight()
    pill = QRectF(x0 + title_w + gap, baseline - cap / 2 - pill_h / 2, pill_w, pill_h)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(fill))
    p.drawRoundedRect(pill, pill_h / 2, pill_h / 2)
    p.setFont(badge_f)
    p.setPen(QColor("#1e1e2e"))
    bm = QFontMetricsF(badge_f)
    p.drawText(QPointF(pill.left() + pad_x, pill.center().y() + bm.capHeight() / 2), badge)

    # Credits under Samba's separator line, lab name under the ETH logo
    _centered(p, AUTHOR, _font(13, QFont.Weight.Normal), "#bac2de", 398)
    _centered(p, "INTERMAG LAB", _font(11, QFont.Weight.Normal, 2), "#6c7086", 462, 2)
    p.end()
    return img


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(__doc__)
        return 2
    app = QGuiApplication(sys.argv[:1])  # noqa: F841 - fonts need a GUI application
    base = background(Path(args[0]))
    out = REPO / "assets"
    out.mkdir(exist_ok=True)
    for name, (badge, fill) in SPLASHES.items():
        path = out / f"{name}_splash.png"
        splash(base, badge, fill).save(str(path))
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
