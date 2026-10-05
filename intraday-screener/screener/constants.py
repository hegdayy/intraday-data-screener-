"""constants.py — colour themes and the Qt stylesheet.

The active theme is chosen with set_theme() before the UI modules are imported;
main.py reads the saved preference from theme.cfg at start-up.
"""

_THEMES = {
    "GOLD": dict(
        BG="#08090F", PANEL="#0D1020", BORDER="#1A2540", HEADER="#10182E",
        GOLD="#C9A84C", TEXT="#D8E0F0", DIM="#5A6A8E", GREEN="#27AE60",
        RED="#C0392B", ORANGE="#D35400", BLUE="#2980B9", CYAN="#00BCD4",
        MAG="#8E44AD", ALT="#0B0D18",
        LIME="#39D353", ROSE="#E8475F", STEEL="#4A6FA5", TEAL="#1ABC9C",
    ),
    "CRIMSON": dict(
        BG="#1A0808", PANEL="#241010", BORDER="#3A1818", HEADER="#2A1010",
        GOLD="#E8E8E8", TEXT="#F5EBEB", DIM="#A07070", GREEN="#3FAE6B",
        RED="#FF2400", ORANGE="#FF5500", BLUE="#5090C0", CYAN="#50C0C8",
        MAG="#C04AAD", ALT="#200C0C",
        LIME="#5FD373", ROSE="#FF6878", STEEL="#C06A6A", TEAL="#4ABCAC",
    ),
}

MONO = "Consolas"
UI   = "SF Pro Display, Segoe UI, sans-serif"

VOL_GAINERS_UNIVERSE = [
    "RELIANCE","TCS","HDFCBANK","ICICIBANK","INFY","ITC","LT","SBIN",
    "BHARTIARTL","AXISBANK","KOTAKBANK","HINDUNILVR","BAJFINANCE","ASIANPAINT",
    "MARUTI","TITAN","SUNPHARMA","ULTRACEMCO","WIPRO","NESTLEIND","ONGC",
    "NTPC","POWERGRID","TATASTEEL","TATAMOTORS","JSWSTEEL","ADANIENT",
    "ADANIPORTS","COALINDIA","HCLTECH","TECHM","GRASIM","CIPLA","DRREDDY",
    "EICHERMOT","BAJAJFINSV","HEROMOTOCO","DIVISLAB","BRITANNIA","APOLLOHOSP",
    "INDUSINDBK","SBILIFE","HDFCLIFE","BPCL","TATACONSUM","UPL","M&M",
    "SHREECEM","BAJAJ-AUTO","HINDALCO",
]

_current_theme = "GOLD"


def _build_stylesheet():
    return f"""
QMainWindow,QWidget{{background:{BG};color:{TEXT};font-family:"{UI}";font-size:13px}}
QTabWidget::pane{{border:1px solid {BORDER};background:{PANEL};border-radius:6px}}
QTabBar::tab{{background:{HEADER};color:{DIM};padding:9px 22px;margin-right:3px;
  border-top-left-radius:6px;border-top-right-radius:6px;font-size:11px;font-weight:600;letter-spacing:0.8px}}
QTabBar::tab:selected{{background:{PANEL};color:{GOLD};border-bottom:2px solid {GOLD}}}
QTabBar::tab:hover{{color:{TEXT}}}
QTableWidget{{background:{BG};gridline-color:{BORDER};color:{TEXT};
  font-family:"{MONO}";font-size:12px;border:none;selection-background-color:#1C2840}}
QTableWidget::item{{padding:6px 10px;border:none}}
QHeaderView::section{{background:{HEADER};color:{GOLD};font-family:"{UI}";font-size:10px;
  font-weight:700;letter-spacing:1.2px;padding:7px 10px;border:none;
  border-right:1px solid {BORDER};border-bottom:2px solid {GOLD}}}
QScrollBar:vertical{{background:{PANEL};width:5px;border-radius:3px}}
QScrollBar::handle:vertical{{background:{BORDER};border-radius:3px;min-height:20px}}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0}}
QStatusBar{{background:{HEADER};color:{DIM};font-size:11px;border-top:1px solid {BORDER};padding:3px 10px}}
QPushButton{{background:{HEADER};color:{GOLD};border:1px solid {BORDER};
  padding:6px 18px;font-size:11px;font-weight:700;letter-spacing:0.5px;border-radius:14px}}
QPushButton:hover{{background:{BORDER};color:{TEXT}}}
QPushButton:disabled{{color:{DIM};border-color:#111825}}
QPushButton:checked{{background:{STEEL};color:#fff;border-color:{BLUE}}}
QSpinBox,QDoubleSpinBox,QComboBox{{background:{PANEL};color:{TEXT};
  border:1px solid {BORDER};padding:5px 10px;border-radius:6px;font-family:"{MONO}";font-size:12px}}
QComboBox::drop-down{{border:none}}
QComboBox QAbstractItemView{{background:{PANEL};color:{TEXT};selection-background-color:{BORDER}}}
QMenu{{background:{PANEL};color:{TEXT};border:1px solid {BORDER};border-radius:6px}}
QMenu::item{{padding:7px 22px;border-radius:4px}}
QMenu::item:selected{{background:{BORDER};color:{GOLD}}}
QSplitter::handle{{background:{BORDER};height:2px}}
QLabel#section{{color:{DIM};font-size:10px;font-weight:700;letter-spacing:1.5px;padding:4px 0px 2px 0px}}
QFrame#divider{{background:{BORDER};max-height:1px}}
QTextEdit{{background:{PANEL};color:{TEXT};border:1px solid {BORDER};border-radius:4px;
  font-family:"{MONO}";font-size:11px;padding:4px}}
"""


def set_theme(name):
    """Switch the active theme. Must be called before other modules import
    the colour names (main.py does this at the top of main(), before any
    UI module is imported). Rebinds every module-level colour constant."""
    global BG, PANEL, BORDER, HEADER, GOLD, TEXT, DIM, GREEN, RED, ORANGE
    global BLUE, CYAN, MAG, ALT, LIME, ROSE, STEEL, TEAL, SS, _current_theme
    c = _THEMES.get(name, _THEMES["GOLD"])
    BG, PANEL, BORDER, HEADER = c["BG"], c["PANEL"], c["BORDER"], c["HEADER"]
    GOLD, TEXT, DIM, GREEN    = c["GOLD"], c["TEXT"], c["DIM"], c["GREEN"]
    RED, ORANGE, BLUE, CYAN   = c["RED"], c["ORANGE"], c["BLUE"], c["CYAN"]
    MAG, ALT, LIME, ROSE      = c["MAG"], c["ALT"], c["LIME"], c["ROSE"]
    STEEL, TEAL               = c["STEEL"], c["TEAL"]
    SS = _build_stylesheet()
    _current_theme = name


def current_theme():
    return _current_theme


def theme_names():
    return list(_THEMES.keys())


set_theme("GOLD")
