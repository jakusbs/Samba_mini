"""
theme.py — central colour / style tokens (copied from Samba 75c0bdf, core/theme.py;
the DARK_STYLE stylesheet comes from Samba_main/samba.py).

Single home for the Catppuccin Mocha values that were previously copy-pasted
across the UI and plot modules, plus the plot-specific palettes.  New code
should import tokens from here; existing hard-coded hex values are migrated
opportunistically when their file is touched.

The curve palettes were validated for colour-vision-deficiency and
normal-vision separation between adjacent entries on the dark plot surface
(#12121f) — keep the ORDER, it is part of the validation.
"""

# ── Catppuccin Mocha (dark UI) ───────────────────────────────────────────────
MOCHA = {
    "rosewater": "#f5e0dc", "flamingo": "#f2cdcd", "pink":     "#f5c2e7",
    "mauve":     "#cba6f7", "red":      "#f38ba8", "maroon":   "#eba0ac",
    "peach":     "#fab387", "yellow":   "#f9e2af", "green":    "#a6e3a1",
    "teal":      "#94e2d5", "sky":      "#89dceb", "sapphire": "#74c7ec",
    "blue":      "#89b4fa", "lavender": "#b4befe",
    "text":      "#cdd6f4", "subtext0": "#a6adc8", "overlay0": "#6c7086",
    "surface1":  "#45475a", "surface0": "#313244",
    "base":      "#1e1e2e", "mantle":   "#181825", "crust":    "#11111b",
}

# Plot surfaces
PLOT_FIG_BG  = "#1e1e2e"   # figure background
PLOT_AX_BG   = "#12121f"   # axes background
PLOT_TICK    = "#aaaacc"   # tick / axis-label ink
PLOT_SPINE   = "#3a3a5c"

# ── Curve palettes (validated adjacency order — do not reshuffle) ───────────
# Y1 (left axis): cool colours.  blue↔green↔lavender pass all separation
# checks; teal as a 4th is acceptable with the legend present.
PLOT_LEFT_COLORS  = ['#89b4fa', '#a6e3a1', '#b4befe', '#94e2d5', '#89dceb']
# Y2 (right axis): warm colours.  red↔yellow↔mauve↔peach pass all checks.
PLOT_RIGHT_COLORS = ['#f38ba8', '#f9e2af', '#cba6f7', '#fab387', '#eba0ac']

# ── Diverging colormaps (signed data → colour range centred on zero) ────────
DIVERGING_CMAPS = {
    'RdBu_r', 'seismic', 'bwr', 'coolwarm', 'PuOr_r', 'RdYlBu_r',
    'Spectral_r', 'PiYG', 'BrBG', 'twilight', 'twilight_shifted',
    # non-reversed twins, in case a config carries them
    'RdBu', 'PuOr', 'RdYlBu', 'Spectral', 'PiYG_r', 'BrBG_r',
}

# ── Dark → light curve-colour mapping for figure export ─────────────────────
# Catppuccin Mocha (pastel, for dark surfaces) → Catppuccin Latte (saturated,
# readable on white).  Used by the light-mode figure export so pastel curves
# don't wash out on a white background.
MOCHA_TO_LATTE = {
    "#f5e0dc": "#dc8a78", "#f2cdcd": "#dd7878", "#f5c2e7": "#ea76cb",
    "#cba6f7": "#8839ef", "#f38ba8": "#d20f39", "#eba0ac": "#e64553",
    "#fab387": "#fe640b", "#f9e2af": "#df8e1d", "#a6e3a1": "#40a02b",
    "#94e2d5": "#179299", "#89dceb": "#04a5e5", "#74c7ec": "#209fb5",
    "#89b4fa": "#1e66f5", "#b4befe": "#7287fd",
}

# Light-export ink colours
LIGHT_INK       = "#1f1f28"   # primary text / spines on white
LIGHT_INK_SOFT  = "#4c4f69"   # secondary text


def light_color_for(color) -> str:
    """Map a dark-theme curve colour to its light-surface counterpart.

    Accepts anything matplotlib returns as a colour; unknown colours are
    passed through unchanged.
    """
    try:
        key = str(color).lower()
        return MOCHA_TO_LATTE.get(key, color)
    except Exception:
        return color


# ── Main stylesheet (Samba_main/samba.py DARK_STYLE, Catppuccin Mocha) ─────────
DARK_STYLE = """
QMainWindow,QWidget{background:#1e1e2e;color:#cdd6f4;
  font-family:'Segoe UI',Ubuntu,sans-serif;font-size:12px;}
QGroupBox{border:1px solid #45475a;border-radius:6px;
  margin-top:9px;padding-top:9px;font-weight:bold;color:#89b4fa;}
QGroupBox::title{subcontrol-origin:margin;left:10px;padding:0 4px;}
QLineEdit,QDoubleSpinBox,QSpinBox,QComboBox{
  background:#313244;border:1px solid #45475a;
  border-radius:4px;padding:3px 6px;color:#cdd6f4;}
QLineEdit:focus,QDoubleSpinBox:focus,QSpinBox:focus{border:1px solid #89b4fa;}
QPushButton{background:#313244;border:1px solid #45475a;
  border-radius:5px;padding:5px 12px;color:#cdd6f4;}
QPushButton:hover{background:#45475a;}
QPushButton:pressed{background:#585b70;}
QPushButton:checked{background:#585b70;border:1px solid #89b4fa;color:#cdd6f4;}
QPushButton#start_btn{background:#a6e3a1;color:#1e1e2e;font-weight:bold;border:none;}
QPushButton#start_btn:hover{background:#94d992;}
QPushButton#abort_btn{background:#f38ba8;color:#1e1e2e;font-weight:bold;border:none;}
QPushButton#abort_btn:hover{background:#e07a97;}
QPushButton#pause_btn{background:#fab387;color:#1e1e2e;font-weight:bold;border:none;}
QPushButton#pause_btn:hover{background:#e8976e;}
QTextEdit{background:#12121f;border:1px solid #313244;
  border-radius:4px;color:#a6e3a1;font-family:'Courier New',monospace;font-size:10px;}
QCheckBox{spacing:6px;}
QCheckBox::indicator{width:14px;height:14px;
  border:1px solid #45475a;border-radius:3px;background:#313244;}
QCheckBox::indicator:checked{background:#89b4fa;}
QRadioButton{spacing:5px;}
QRadioButton::indicator{width:14px;height:14px;}
QTabWidget::pane{border:1px solid #45475a;border-radius:5px;}
QTabBar::tab{background:#313244;padding:6px 14px;border-radius:4px 4px 0 0;color:#6c7086;}
QTabBar::tab:selected{background:#45475a;color:#cdd6f4;}
QListWidget{background:#181825;border:1px solid #313244;border-radius:4px;}
QListWidget::item{padding:5px 7px;}
QListWidget::item:selected{background:#45475a;color:#cdd6f4;}
QScrollBar:vertical{background:#1e1e2e;width:8px;border-radius:4px;}
QScrollBar::handle:vertical{background:#45475a;border-radius:4px;}
QScrollBar:horizontal{background:#1e1e2e;height:8px;border-radius:4px;}
QScrollBar::handle:horizontal{background:#45475a;border-radius:4px;}
QSplitter::handle{background:#313244;}
QSplitter::handle:horizontal{width:4px;}
QSplitter::handle:vertical{height:4px;}
"""

# Samba_mini additions: badges and status colours used by both applications.
STATUS_COLORS = {
    "ON": "#a6e3a1", "RUNNING": "#89b4fa", "MOVING": "#89b4fa", "OFF": "#6c7086",
    "FAULT": "#f38ba8", "DISABLED": "#6c7086", "UNKNOWN": "#fab387", "INIT": "#f9e2af",
}
EXTRA_STYLE = """
QWidget#action_bar{background:#12121f;}
QLabel#sim_badge{background:#fab387;color:#1e1e2e;font-weight:bold;border-radius:4px;padding:2px 8px;}
QLabel#real_badge{background:#a6e3a1;color:#1e1e2e;font-weight:bold;border-radius:4px;padding:2px 8px;}
QLabel#offline_badge{background:#f38ba8;color:#1e1e2e;font-weight:bold;border-radius:4px;padding:2px 8px;}
QPushButton[class="pill"]{background:#252538;border:1px solid #45475a;border-radius:11px;padding:3px 12px;color:#a6adc8;}
QPushButton[class="pill"]:checked{background:#89b4fa;color:#1e1e2e;border:none;font-weight:bold;}
QPushButton#setup_longitudinal:checked{background:#00a86b;color:#ffffff;}
QPushButton#setup_polar:checked{background:#ab4b52;color:#ffffff;}
QTableWidget{background:#181825;gridline-color:#313244;border:1px solid #313244;}
QHeaderView::section{background:#313244;color:#cdd6f4;border:none;padding:4px;}
QStatusBar{background:#181825;border-top:1px solid #313244;}
QAbstractSpinBox:disabled, QLineEdit:disabled, QComboBox:disabled{color:#585b70;}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button{width:0px;border:none;}
QAbstractSpinBox::up-arrow, QAbstractSpinBox::down-arrow{image:none;width:0px;height:0px;}
QLabel:disabled, QCheckBox:disabled, QRadioButton:disabled{color:#585b70;}
QStatusBar QLabel{color:#a6adc8;padding:0 6px;}
"""


def app_style() -> str:
    return DARK_STYLE + EXTRA_STYLE
