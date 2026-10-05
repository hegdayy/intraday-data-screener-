"""main.py — main window, header and application entry point.
Run:  python main.py
"""
import sys, os, datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QFrame, QSpinBox, QPushButton, QCheckBox, QComboBox,
    QStatusBar, QSplitter, QTabWidget
)
from PyQt6.QtCore import Qt, QThread, QTimer
from PyQt6.QtGui import QColor, QPalette

from screener.constants import BG, PANEL, BORDER, HEADER, GOLD, TEXT, DIM, GREEN, RED, ORANGE, CYAN, ALT, MONO, UI, SS
from screener.utils import _is_market_open
from screener.worker import SimWorker
from screener.engine import AutoEngine
from screener.pms import PMSManual, AutoPanel
from screener.tables import FilterTable, VolumeFilter


class Header(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(62)
        self.setStyleSheet(
            f"background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            f"stop:0 #0A0D1A,stop:0.5 {HEADER},stop:1 #0A0D1A);")

        lo = QHBoxLayout(self); lo.setContentsMargins(22, 0, 22, 0); lo.setSpacing(0)

        # ── Left: Logo block ─────────────────────────────────────────────────
        logo_col = QVBoxLayout(); logo_col.setSpacing(0)
        logo_col.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        logo = QLabel("SCREENER")
        logo.setStyleSheet(
            f"color:{GOLD};font-size:24px;font-weight:700;"
            f"font-family:'{UI}';letter-spacing:12px;"
            f"background:transparent;")
        sub = QLabel("TRADING SIMULATOR")
        sub.setStyleSheet(
            f"color:{DIM};font-size:8px;font-weight:700;"
            f"letter-spacing:4px;font-family:'{UI}';background:transparent;")
        logo_col.addWidget(logo); logo_col.addWidget(sub)
        lo.addLayout(logo_col)

        # thin gold accent line
        vline = QFrame(); vline.setFrameShape(QFrame.Shape.VLine)
        vline.setStyleSheet(f"background:{GOLD};max-width:1px;margin:12px 18px;"); lo.addWidget(vline)

        # ── Centre: clock + status ────────────────────────────────────────────
        centre = QVBoxLayout(); centre.setSpacing(2)
        centre.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self._clock = QLabel("")
        self._clock.setStyleSheet(
            f"color:{TEXT};font-size:15px;font-weight:600;"
            f"font-family:'{MONO}';letter-spacing:2px;background:transparent;")

        self._mkt = QLabel("—")
        self._mkt.setStyleSheet(
            f"color:{DIM};font-size:9px;font-weight:700;"
            f"letter-spacing:2px;background:transparent;")

        centre.addWidget(self._clock)
        centre.addWidget(self._mkt)
        lo.addLayout(centre)

        lo.addStretch()

        # ── Right: exchange + session info ────────────────────────────────────
        right_col = QVBoxLayout(); right_col.setSpacing(2)
        right_col.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)

        exch = QLabel("NSE · INDIA")
        exch.setStyleSheet(
            f"color:{GOLD};font-size:11px;font-weight:700;"
            f"letter-spacing:3px;font-family:'{UI}';background:transparent;")
        exch.setAlignment(Qt.AlignmentFlag.AlignRight)

        sess = QLabel("EQUITIES  ·  09:15 – 15:30 IST")
        sess.setStyleSheet(
            f"color:{DIM};font-size:9px;letter-spacing:1px;"
            f"font-family:'{UI}';background:transparent;")
        sess.setAlignment(Qt.AlignmentFlag.AlignRight)

        right_col.addWidget(exch); right_col.addWidget(sess)
        lo.addLayout(right_col)

        sc_vline = QFrame(); sc_vline.setFrameShape(QFrame.Shape.VLine)
        sc_vline.setStyleSheet(f"background:{BORDER};max-width:1px;margin:14px 14px;")
        lo.addWidget(sc_vline)

        screener_frame = QFrame()
        screener_frame.setFixedWidth(190)
        screener_frame.setStyleSheet(
            f"background:{PANEL};border:1px solid {BORDER};border-radius:0px;")
        sc_lo = QVBoxLayout(screener_frame); sc_lo.setContentsMargins(10,4,10,4); sc_lo.setSpacing(1)
        sc_title = QLabel("P_STOP SCREENER")
        sc_title.setStyleSheet(
            f"color:{DIM};font-size:8px;font-weight:700;letter-spacing:1.5px;background:transparent;")
        sc_title.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._screener_val = QLabel("— scanning —")
        self._screener_val.setStyleSheet(
            f"color:{DIM};font-size:11px;font-weight:700;"
            f"font-family:'{MONO}';background:transparent;")
        self._screener_val.setAlignment(Qt.AlignmentFlag.AlignLeft)
        sc_lo.addWidget(sc_title); sc_lo.addWidget(self._screener_val)
        lo.addWidget(screener_frame)

    def update_screener(self, text):
        """Called from SimWindow._on_data with PMSManual.screener_text."""
        if text:
            self._screener_val.setText(text)
            self._screener_val.setStyleSheet(
                f"color:{GREEN};font-size:11px;font-weight:700;"
                f"font-family:'{MONO}';background:transparent;")
        else:
            self._screener_val.setText("— no candidate —")
            self._screener_val.setStyleSheet(
                f"color:{DIM};font-size:11px;font-weight:700;"
                f"font-family:'{MONO}';background:transparent;")

    def tick(self):
        now = datetime.datetime.now()
        self._clock.setText(now.strftime("%A  %d %b %Y   %H:%M:%S"))
        if _is_market_open(now):
            self._mkt.setText("●  MARKET OPEN  —  LIVE SESSION")
            self._mkt.setStyleSheet(
                f"color:{GREEN};font-size:9px;font-weight:700;"
                f"letter-spacing:2px;background:transparent;")
        else:
            # Show time until next open on weekdays
            wd = now.weekday()
            if wd >= 5:
                days_left = 7 - wd
                self._mkt.setText(f"●  MARKET CLOSED  —  OPENS MONDAY")
            elif now.time() < datetime.time(9, 15):
                mins = int((datetime.datetime.combine(now.date(), datetime.time(9, 15)) - now).total_seconds() / 60)
                self._mkt.setText(f"●  PRE-MARKET  —  OPENS IN {mins}m")
            else:
                self._mkt.setText("●  SESSION ENDED  —  CLOSED")
            self._mkt.setStyleSheet(
                f"color:{DIM};font-size:9px;font-weight:700;"
                f"letter-spacing:2px;background:transparent;")


class SimWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Intraday Screener")
        self.resize(1560, 960); self.setMinimumSize(1100, 640)
        central = QWidget(); self.setCentralWidget(central)
        root = QVBoxLayout(central); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)

        self._header = Header(); root.addWidget(self._header)
        div = QFrame(); div.setFrameShape(QFrame.Shape.HLine)
        div.setStyleSheet(f"background:{GOLD};max-height:2px;"); root.addWidget(div)

        # ── Control bar ──────────────────────────────────────────────────────
        ctrl = QHBoxLayout(); ctrl.setContentsMargins(12, 7, 12, 7); ctrl.setSpacing(10)

        mode_lbl = QLabel("Mode:")
        mode_lbl.setStyleSheet(f"color:{DIM};font-size:11px;")
        ctrl.addWidget(mode_lbl)

        self._mode_cb = QComboBox()
        self._mode_cb.addItems(["Live", "Replay yesterday"])
        self._mode_cb.setMaximumWidth(190)
        self._mode_cb.currentIndexChanged.connect(self._mode_changed)
        ctrl.addWidget(self._mode_cb)

        # Mode status badge — shows current active mode
        self._mode_badge = QLabel("● LIVE")
        self._mode_badge.setStyleSheet(
            f"color:{GREEN};font-size:10px;font-weight:700;"
            f"background:#0A1A0A;border:1px solid {GREEN};"
            f"border-radius:8px;padding:2px 10px;")
        ctrl.addWidget(self._mode_badge)

        ctrl.addWidget(QLabel("Refresh (s):"))
        self._spin = QSpinBox()
        self._spin.setRange(5, 300); self._spin.setValue(60); self._spin.setMaximumWidth(65)
        ctrl.addWidget(self._spin)

        ab = QPushButton("APPLY"); ab.setMaximumWidth(70)
        ab.clicked.connect(self._apply_interval); ctrl.addWidget(ab)

        rb = QPushButton("⟳ NOW"); rb.setMaximumWidth(65)
        rb.setStyleSheet(
            f"background:{HEADER};color:{CYAN};border:1px solid {BORDER};"
            f"padding:5px 10px;font-size:11px;font-weight:700;border-radius:14px;")
        rb.clicked.connect(self._force_refresh); ctrl.addWidget(rb)

        self._log_chk = QCheckBox("Log CSV"); self._log_chk.setChecked(True)
        ctrl.addWidget(self._log_chk)

        self._reset_btn = QPushButton("🔄 SESSION RESET")
        self._reset_btn.setMaximumWidth(150)
        self._reset_btn.setToolTip(
            "Clears today's CLOSED-position history and resets settings to "
            "defaults. Refuses to run if any position is still open.")
        self._reset_btn.setStyleSheet(
            f"background:{HEADER};color:{RED};border:1px solid {BORDER};"
            f"padding:5px 10px;font-size:11px;font-weight:700;border-radius:14px;")
        ctrl.addWidget(self._reset_btn)   # .clicked connected below, once self._pms exists

        ctrl.addStretch()

        from screener.constants import current_theme as _cur_theme
        _disp_name = "SCARLET" if _cur_theme() == "CRIMSON" else "GOLD"
        self._theme_btn = QPushButton(f"🎨 THEME: {_disp_name}")
        self._theme_btn.setMaximumWidth(150)
        self._theme_btn.setStyleSheet(
            f"background:{HEADER};color:{GOLD};border:1px solid {BORDER};"
            f"padding:5px 10px;font-size:11px;font-weight:700;border-radius:14px;")
        self._theme_btn.clicked.connect(self._toggle_theme_pref)
        ctrl.addWidget(self._theme_btn)

        self._info = QLabel("Logs → sim_logs/")
        self._info.setStyleSheet(f"color:{DIM};font-size:11px;"); ctrl.addWidget(self._info)

        ctrl_w = QWidget(); ctrl_w.setLayout(ctrl)
        ctrl_w.setStyleSheet(f"background:{PANEL};border-bottom:1px solid {BORDER};")
        root.addWidget(ctrl_w)

        # ── Main splitter ────────────────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Vertical)

        tabs_w = QWidget(); tabs_lo = QVBoxLayout(tabs_w)
        tabs_lo.setContentsMargins(0, 0, 0, 0); tabs_lo.setSpacing(0)
        tabs = QTabWidget()
        self._ft  = FilterTable()
        self._pms = PMSManual()
        self._reset_btn.clicked.connect(self._do_session_reset)
        self._vf  = VolumeFilter()
        self._vf.set_pms(self._pms)
        tabs.addTab(self._ft,  "  FILTER TABLE  ")
        tabs.addTab(self._vf,  "  VOLUME FILTER  ")
        tabs.addTab(self._pms, "  PMS  ")
        tabs_lo.addWidget(tabs)
        splitter.addWidget(tabs_w)

        self._engine     = AutoEngine()
        self._auto_panel = AutoPanel(self._engine, self._pms)
        self._auto_panel.setMinimumHeight(145); self._auto_panel.setMaximumHeight(200)
        self._auto_panel.setStyleSheet(f"background:{PANEL};border-top:2px solid {GOLD};")
        self._auto_panel.r_all_sig.connect(self._pms.exit_all)
        splitter.addWidget(self._auto_panel)
        splitter.setSizes([740, 165])
        root.addWidget(splitter)

        # Direct-add signals (user chose explicitly → auto confirm)
        self._ft.add_to_pms.connect(lambda s, l: self._pms.add(s, l))
        self._vf.add_to_pms.connect(lambda s, l, vf: self._pms.add(s, l, vol_flow=vf))

        # Worker
        self._loser_rows = []
        self._sb = QStatusBar(); self.setStatusBar(self._sb)
        self._sb.showMessage("Starting screener...")
        self._worker = SimWorker(); self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._worker.data_ready.connect(self._on_data)
        self._worker.losers_ready.connect(self._on_losers)
        self._worker.vol_gainers_ready.connect(self._on_vol_gainers)
        self._worker.status_msg.connect(self._sb.showMessage)
        self._worker.set_held_symbols_fn(self._pms.open_position_symbols)
        self._thread.started.connect(self._worker.run)
        self._thread.start()

        self._clk = QTimer(self)
        self._clk.timeout.connect(self._header.tick)
        self._clk.start(1000)

    def _apply_interval(self):
        v = self._spin.value()
        self._worker.set_interval(v)
        self._sb.showMessage(f"Refresh interval set to {v}s", 2000)

    def _do_session_reset(self):
        if self._pms.reset_session():
            self._sb.showMessage("Session reset — closed-position history cleared.", 4000)

    def _force_refresh(self):
        self._worker._wake.set()

    def _toggle_theme_pref(self):
        """Save the next-launch theme preference to a small text file next to
        the script. Does NOT hot-swap colours on existing widgets — that
        would require rebuilding every styled widget in the app, which is
        too invasive to do safely live. Restart the app to see the new theme."""
        from screener.constants import current_theme
        next_theme = "CRIMSON" if current_theme() == "GOLD" else "GOLD"
        display_name = "SCARLET" if next_theme == "CRIMSON" else "GOLD"
        try:
            pref_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "theme.cfg")
            with open(pref_path, "w") as f:
                f.write(next_theme)
            self._theme_btn.setText(f"🎨 THEME: {display_name}  (restart to apply)")
            self._sb.showMessage(
                f"Theme set to {display_name} — restart the app to apply", 5000)
        except Exception as e:
            self._sb.showMessage(f"Could not save theme preference: {e}", 4000)

    def _mode_changed(self, idx):
        # Properly reset worker state when switching modes
        mode = SimWorker.MODE_LIVE if idx == 0 else SimWorker.MODE_REPLAY
        self._worker.set_mode(mode)
        if idx == 1:
            self._info.setStyleSheet(f"color:{ORANGE};font-size:11px;font-weight:700;")
            self._info.setText("REPLAY — 1 min per scan")
            self._live_interval_before_replay = self._spin.value()
            self._spin.setValue(30)
            self._worker.set_interval(30)
            self._mode_badge.setText("● REPLAY")
            self._mode_badge.setStyleSheet(
                f"color:{ORANGE};font-size:10px;font-weight:700;"
                f"background:#1A0E00;border:1px solid {ORANGE};"
                f"border-radius:8px;padding:2px 10px;")
        else:
            self._info.setStyleSheet(f"color:{DIM};font-size:11px;")
            self._info.setText("Logs → sim_logs/")
            restore_to = getattr(self, "_live_interval_before_replay", 60)
            self._spin.setValue(restore_to)
            self._worker.set_interval(restore_to)
            # Reset replay state so live resumes immediately
            self._worker._replay_date  = None
            self._worker._replay_cache = {}
            self._mode_badge.setText("● LIVE")
            self._mode_badge.setStyleSheet(
                f"color:{GREEN};font-size:10px;font-weight:700;"
                f"background:#0A1A0A;border:1px solid {GREEN};"
                f"border-radius:8px;padding:2px 10px;")
        # Wake the worker so it picks up the new mode immediately
        self._worker._wake.set()

    def _on_data(self, rows):
        try: self._ft.refresh(rows)
        except: pass
        try: self._pms.update_prices(rows, self._loser_rows)
        except: pass
        try: self._header.update_screener(self._pms.screener_text)
        except: pass
        try: self._auto_panel.feed_data(rows, self._loser_rows)
        except: pass
        try: self._worker._log_enabled = self._log_chk.isChecked()
        except: pass

    def _on_vol_gainers(self, rows):
        try: self._vf.refresh(rows)
        except: pass

    def _on_losers(self, rows):
        self._loser_rows = rows
        try: self._vf.refresh_losers(rows)
        except: pass

    def closeEvent(self, e):
        self._worker.stop()
        self._thread.quit()
        self._thread.wait(3000)
        e.accept()


def main():
    def _excepthook(exc_type, exc_value, exc_tb):
        import traceback
        print("\n" + "=" * 70)
        print("UNCAUGHT EXCEPTION:")
        traceback.print_exception(exc_type, exc_value, exc_tb)
        print("=" * 70 + "\n")
    sys.excepthook = _excepthook

    # Load saved theme preference (if any) before anything else imports SS
    try:
        pref_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "theme.cfg")
        if os.path.exists(pref_path):
            with open(pref_path) as f:
                saved = f.read().strip()
            if saved in ("GOLD", "CRIMSON"):
                from screener.constants import set_theme
                set_theme(saved)
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setApplicationName("Intraday Screener")
    app.setApplicationDisplayName("Intraday Screener")
    from screener.constants import SS as _SS_LIVE
    app.setStyleSheet(_SS_LIVE)
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window,          QColor(BG))
    pal.setColor(QPalette.ColorRole.WindowText,      QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Base,            QColor(PANEL))
    pal.setColor(QPalette.ColorRole.AlternateBase,   QColor(ALT))
    pal.setColor(QPalette.ColorRole.Text,            QColor(TEXT))
    pal.setColor(QPalette.ColorRole.ButtonText,      QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Highlight,       QColor(BORDER))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(GOLD))
    app.setPalette(pal)
    win = SimWindow(); win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
