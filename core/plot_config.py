"""Constants that Samba's plot widgets and data browser import from Samba's
``Samba_main/config.py``.  Values copied from Samba ``75c0bdf``."""

from __future__ import annotations

from core import paths
from core.theme import PLOT_LEFT_COLORS, PLOT_RIGHT_COLORS

LEFT_COLORS = list(PLOT_LEFT_COLORS)
RIGHT_COLORS = list(PLOT_RIGHT_COLORS)
COLORMAPS = ['RdBu_r', 'seismic', 'coolwarm', 'PuOr_r', 'viridis', 'inferno', 'gray']

# Sentinel x-axis keys used by the live 1D plot.
X_NATURAL = "_natural_"   # the scan's natural x: position or coil command
X_TIME = "_time_"         # elapsed time in seconds

#: The data browser keeps its search index here (Samba used its config dir).
CONFIG_DIR = str(paths.state_dir())
