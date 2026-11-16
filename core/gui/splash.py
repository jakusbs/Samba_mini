"""Start-up splash screen for Samba_mini and Samba_controls (Samba's look).

``python -m Samba_mini`` shows it before the measurement modules are imported,
so it covers the slow part of a cold start; ``main()`` then waits out the
minimum display time, builds the window and hands over::

    splash.start("samba_mini", APP_ID)     # in __main__, before the heavy imports
    ...
    splash.status("Opening the main window…")
    splash.wait()                          # before the window exists: its timers
    w = MainWindow()                       # (service check, login) must not fire
    splash.finish(w)                       # behind the splash

The artwork is ``assets/<name>_splash.png`` (2x, built by scripts/make_splash.py);
the version and the status line are drawn here.  ``SAMBA_MINI_NO_SPLASH=1``
starts without it.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QEventLoop, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QSplashScreen

ASSETS = Path(__file__).resolve().parents[2] / "assets"
SIZE = (720, 480)          # logical size; the PNG is twice that for high-DPI screens
MIN_SECONDS = 2.0          # a warm start takes about 1 s; long enough to be seen, not to annoy
MARGIN = 16                # status/version text inset from the rounded edge

_current: Splash | None = None
_app: QApplication | None = None   # an application created here must outlive start(), or Qt deletes the splash


class Splash(QSplashScreen):
    """Samba-style splash: artwork, status line bottom left, version bottom right."""

    def __init__(self, pixmap: QPixmap, version: str):
        super().__init__(pixmap, Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)   # keep the rounded corners
        self.version = version
        self.shown_at = time.monotonic()

    def drawContents(self, painter: QPainter) -> None:   # noqa: N802 - Qt override
        font = QFont("Segoe UI")
        font.setPixelSize(12)
        painter.setFont(font)
        area = self.rect().adjusted(MARGIN, 0, -MARGIN, -10)
        bottom_left = Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft
        bottom_right = Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight
        painter.setPen(QColor("#a6adc8"))
        painter.drawText(area, bottom_left, self.message())
        if self.version:
            painter.setPen(QColor("#6c7086"))
            painter.drawText(area, bottom_right, f"v{self.version}")


def _pixmap(name: str, app: QApplication) -> QPixmap:
    pm = QPixmap(str(ASSETS / f"{name}_splash.png"))
    if pm.isNull():                                      # artwork missing: plain dark card
        pm = QPixmap(*SIZE)
        pm.fill(QColor("#1e1e2e"))
        p = QPainter(pm)
        font = QFont("Segoe UI")
        font.setPixelSize(40)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor("#cdd6f4"))
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, name.replace("samba", "Samba"))
        p.end()
    screen = app.primaryScreen()
    dpr = screen.devicePixelRatio() if screen is not None else 1.0
    w, h = round(SIZE[0] * dpr), round(SIZE[1] * dpr)
    if (pm.width(), pm.height()) != (w, h):
        pm = pm.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    pm.setDevicePixelRatio(dpr)
    return pm


def start(name: str, app_id: str | None = None, message: str = "Loading…") -> Splash | None:
    """Show the splash for *name* (``"samba_mini"`` / ``"samba_controls"``) once.

    Creates the QApplication if there is none yet.  Returns the splash, the one
    already shown, or None when disabled.
    """
    global _current, _app
    if _current is not None:
        return _current
    if os.environ.get("SAMBA_MINI_NO_SPLASH", "").strip() not in ("", "0"):
        return None
    if app_id:                                           # before the first window, for the taskbar
        from core.gui import set_windows_app_id
        set_windows_app_id(app_id)
    from core.version import __version__
    _app = QApplication.instance() or QApplication(sys.argv)
    _current = Splash(_pixmap(name, _app), __version__)
    _current.show()
    status(message)
    return _current


def status(text: str) -> None:
    """Replace the status line (no-op without a splash)."""
    if _current is None:
        return
    _current.showMessage(text)
    QApplication.processEvents()


def wait(min_seconds: float = MIN_SECONDS) -> None:
    """Keep the splash up until it has been visible for *min_seconds*.

    Call it before the main window is built: the window's start-up timers only
    fire once it exists, so nothing can open a dialog behind the splash.
    """
    if _current is None:
        return
    remaining = min_seconds - (time.monotonic() - _current.shown_at)
    if remaining > 0:
        loop = QEventLoop()
        QTimer.singleShot(int(remaining * 1000), loop.quit)
        loop.exec()


def finish(window) -> None:
    """Show *window* and close the splash once the window is on screen."""
    global _current
    window.show()
    if _current is not None:
        _current.finish(window)
        _current = None
