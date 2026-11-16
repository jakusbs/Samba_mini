"""PyQt6 helpers shared by Samba_controls and Samba_mini (GUI processes only)."""

import sys


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
