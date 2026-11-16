"""PyQt6 helpers shared by Samba_controls and Samba_mini (GUI processes only)."""

import logging
import logging.handlers
import sys
import threading
import traceback

log = logging.getLogger("core.gui")
_box_open = False


def set_windows_app_id(app_id: str) -> None:
    """Give the process its own taskbar identity (otherwise Windows groups the
    window under pythonw.exe and shows Python's icon)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except (AttributeError, OSError):
        pass


def setup_logging(app_name: str):
    """Log to ``%LOCALAPPDATA%\\SambaMini\\logs\\<app_name>.log`` (rotating) and stderr.

    The windows run under pythonw.exe, which has no console: without a file a
    crash leaves no trace.  Returns the log file path.
    """
    from core import paths
    path = paths.logs_dir() / f"{app_name}.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(getattr(h, "_samba_file", False) for h in root.handlers):
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        fh._samba_file = True
        root.addHandler(fh)
    if sys.stderr is not None and not any(isinstance(h, logging.StreamHandler)
                                          and not isinstance(h, logging.FileHandler) for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    return path


def handle_exception(exc_type, exc, tb, log_path=None) -> None:
    """Log an unexpected error and tell the user, instead of closing the window.

    PyQt6's default for an exception escaping a slot is to abort the whole
    application; a measurement window must not vanish because one button or
    file had a problem.  Running measurements are not affected: they run in
    the engine thread and the device service.
    """
    global _box_open
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    log.critical("unexpected error\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
        if QApplication.instance() is None or _box_open:
            return
        _box_open = True
        box = QMessageBox(QMessageBox.Icon.Warning, "Unexpected error",
                          f"Something went wrong, but the window stays open:\n\n{exc_type.__name__}: {exc}"
                          + (f"\n\nDetails are in {log_path}" if log_path else ""),
                          QMessageBox.StandardButton.Ok)
        box.setDetailedText("".join(traceback.format_exception(exc_type, exc, tb)))
        box.setModal(False)
        box.finished.connect(_box_closed)
        box.open()
        handle_exception._box = box          # keep a reference while it is shown
    except Exception:  # noqa: BLE001 - the guard itself must never raise
        _box_open = False


def _box_closed(*_):
    global _box_open
    _box_open = False


def install_crash_guard(app_name: str):
    """File logging plus the exception hooks above, for the GUI thread and other threads."""
    path = setup_logging(app_name)
    sys.excepthook = lambda t, e, tb: handle_exception(t, e, tb, path)
    threading.excepthook = lambda a: log.critical(
        "unexpected error in thread %s\n%s", getattr(a.thread, "name", "?"),
        "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))
    return path
