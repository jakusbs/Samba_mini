"""Samba_mini: PyQt6 measurement application (client of the device service).

This package must never import hardware drivers or ``Samba_controls.service``;
``tests/test_boundaries.py`` enforces it.
"""

APP_ID = "ETH.Intermag.SambaMini"          # Windows taskbar identity (also used by the splash)
