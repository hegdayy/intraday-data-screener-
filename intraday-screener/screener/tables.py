"""tables.py — FilterTable and VolumeFilter widgets.
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QLabel,
    QPushButton, QTabWidget, QMenu, QFrame, QDialog
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QBrush

from screener.constants import (
    BG, PANEL, BORDER, GOLD, TEXT, DIM, GREEN, RED, ORANGE,
    CYAN, STEEL, ROSE, MONO, SS
)
from screener.utils import _i, _bg, _pc, _ck, _fmt_vol, _set_headers, _pill_btn, open_order_book_dialog
from screener.data import _combined_score

# ── FilterTable ───────────────────────────────────────────────────────────────
F_H = ["#","SYMBOL","% CHG","LTP","RVOL","VF STREAK","VOL FLOW","RSI(14)","MACD",
       "VWAP","▲VWAP","SLOPE","HI-LOWS","HH/LH","SPIKE","STRUCTURE","EMA 9/21"]


class FilterTable(QWidget):
    add_to_pms = pyqtSignal(str, float)

    def __init__(self):
        super().__init__()
        lo = QVBoxLayout(self); lo.setContentsMargins(0,0,0,0)
        leg = QHBoxLayout(); leg.setContentsMargins(10,5,10,5); leg.setSpacing(20)
        for txt, col in [("GREEN = strong signal", GREEN), ("AMBER = crossed 1%", GOLD),
                         ("right-click → PMS", DIM)]:
            lb = QLabel(txt); lb.setStyleSheet(f"color:{col};font-size:11px;"); leg.addWidget(lb)
        leg.addStretch()
        self._phase = QLabel("—")
        self._phase.setStyleSheet(f"color:{DIM};font-size:11px;font-weight:700;")
        leg.addWidget(self._phase)
        lo.addLayout(leg)
        self._tbl = QTableWidget(0, len(F_H)); _set_headers(self._tbl, F_H)
        self._tbl.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tbl.customContextMenuRequested.connect(self._ctx)
        lo.addWidget(self._tbl)
        self._cache = []
        self._vf_streak = {}   # sym -> {"last_vf": float|None, "score": int} — persists across scans
        self._high_dir = {}    # sym -> last seen latest_high, for the ↑/↓/S exhaustion indicator

    def _ctx(self, pos):
        row = self._tbl.rowAt(pos.y())
        menu = QMenu(self)
        act = None
        if 0 <= row < len(self._cache):
            d = self._cache[row]; sym = d["symbol"]; ltp = d.get("ltp") or 0.0
            act = menu.addAction(f"Add  {sym}  to PMS  @  {ltp:.2f}")
            menu.addSeparator()
        ob_act = menu.addAction("📋 Order Book")
        chosen = menu.exec(self._tbl.viewport().mapToGlobal(pos))
        if act and chosen == act:
            self.add_to_pms.emit(sym, float(ltp))
        elif chosen == ob_act:
            open_order_book_dialog(self, getattr(self, "_pms_ref", None))

    def refresh(self, rows):
        self._cache = rows
        has_vals = any(r.get("ind",{}).get("vwap") is not None for r in rows)
        no_yf    = all(not r.get("has_yf") for r in rows)
        if has_vals:
            self._phase.setStyleSheet(f"color:{GREEN};font-size:11px;font-weight:700;")
            self._phase.setText("FULL DATA")
        elif no_yf:
            self._phase.setStyleSheet(f"color:{ORANGE};font-size:11px;font-weight:700;")
            self._phase.setText("LIVE PRICES — bar data loading...")
        else:
            self._phase.setStyleSheet(f"color:{ORANGE};font-size:11px;font-weight:700;")
            self._phase.setText("PARTIAL DATA")
        self._tbl.setRowCount(len(rows))
        for r, d in enumerate(rows): self._render(r, d)

    def _render(self, r, d):
        ind = d.get("ind") or {}; sym = d["symbol"]; pct = d["per_change"]; ltp = d["ltp"]
        dr = d.get("display_rank", r+1); is_setup = d.get("is_setup", False); crossed = pct >= 1.0
        no_data = not d.get("has_yf", True)
        bg = (QColor("#071A0C" if r%2 else "#091F0E") if is_setup else
              QColor("#1A140A" if r%2 else "#1F190A") if crossed else _bg(r))
        is_illiquid = "ILLIQ" in (ind.get("liquidity_note") or "")
        rank_col = RED if is_illiquid else DIM
        rank_s = f"{dr} (0)" if is_illiquid else str(dr)
        sym_col = GREEN if is_setup else (GOLD if crossed else TEXT)
        vf = ind.get("vol_flow")
        st = self._vf_streak.setdefault(sym, {"last_vf": None, "score": 0})
        vf_delta = 0.0
        direction = None  # "up" / "down" / None (flat or no comparison yet)
        if vf is not None and st["last_vf"] is not None:
            vf_delta = vf - st["last_vf"]
            if vf > st["last_vf"]:
                st["score"] += 1; direction = "up"
            elif vf < st["last_vf"]:
                st["score"] -= 1; direction = "down"
        if vf is not None:
            st["last_vf"] = vf
        if vf is None:
            vfstreak_s = "n/a" if no_data else "—"; vfstreak_c = DIM
        elif direction is None:
            vfstreak_s = f"{st['score']:+d}"
            vfstreak_c = GREEN if st["score"] > 0 else (RED if st["score"] < 0 else DIM)
        else:
            vfstreak_s = f"{st['score']:+d}({vf_delta:+.1f}K)"
            vfstreak_c = GREEN if direction == "up" else RED
        rvol = ind.get("rvol"); rvol_s = f"{rvol:.1f}x" if rvol is not None else "—"
        rvol_c = GREEN if (rvol and rvol >= 2) else (GOLD if (rvol and rvol >= 1.3) else DIM)
        vf = ind.get("vol_flow"); vf_s = f"{vf:+.0f}K" if vf is not None else "—"
        vf_c = GREEN if (vf and vf > 0) else (RED if (vf and vf < 0) else DIM)
        rsi = ind.get("rsi")
        if rsi is None: rsi_s, rsi_c = ("n/a" if no_data else "—"), DIM
        else: rsi_s = f"{rsi:.0f}"; rsi_c = RED if rsi >= 70 else (GREEN if rsi <= 30 else TEXT)
        mh = ind.get("macd_hist")
        mh_s = f"{mh:+.2f}" if mh is not None else ("n/a" if no_data else "—")
        mh_c = GREEN if (mh and mh > 0) else (RED if (mh and mh < 0) else DIM)
        vwap = ind.get("vwap"); vwap_s = f"{vwap:.2f}" if vwap is not None else ("n/a" if no_data else "—")
        av_s, av_c = _ck(ind.get("above_vwap")); sl_s, sl_c = _ck(ind.get("vwap_slope"))
        hl_s, hl_c = _ck(ind.get("higher_lows"))
        if ind.get("hh_new_peak"):
            lh_s, lh_c = "HH", GREEN
        elif ind.get("lower_highs") is True:
            lh_s, lh_c = "LH", RED
        else:
            lhigh = ind.get("latest_high")
            prev_high = self._high_dir.get(sym)
            if lhigh is None:
                lh_s, lh_c = "—", DIM
            elif prev_high is None:
                lh_s, lh_c = f"S({lhigh:.2f})", DIM   # first time seeing this symbol, nothing to compare yet
            elif lhigh > prev_high:
                lh_s, lh_c = f"↑({lhigh:.2f})", GREEN
            elif lhigh < prev_high:
                lh_s, lh_c = f"↓({lhigh:.2f})", ORANGE
            else:
                lh_s, lh_c = f"S({lhigh:.2f})", DIM
            if lhigh is not None:
                self._high_dir[sym] = lhigh
        spk = ind.get("spike", False); spkr = ind.get("spike_ratio", 0.0)
        if no_data:     spk_s = "n/a"; spk_c = DIM
        elif spkr >= 2: spk_s = f"YES x{spkr:.1f}"; spk_c = GOLD
        elif spkr > 0:  spk_s = f"x{spkr:.1f}"; spk_c = DIM
        else:           spk_s = "—"; spk_c = DIM
        ll = ind.get("last_low")
        near_low = False
        if no_data or not ll or ll <= 0 or not ltp or ltp <= 0:
            struct_s, struct_c, struct_bold = ("n/a" if no_data else "—"), DIM, False
        else:
            mig_pct = (ltp - ll) / ll * 100
            near_low = mig_pct < 0.20
            if mig_pct >= 0.5:
                struct_s, struct_c, struct_bold = f"▲{mig_pct:.2f}%>L", GREEN, False
            elif mig_pct >= 0.20:
                struct_s, struct_c, struct_bold = f"▲{mig_pct:.2f}%>L", ORANGE, False
            else:
                struct_s, struct_c, struct_bold = f"⚠{mig_pct:.2f}%>L", RED, True
        if near_low and not is_setup and not crossed:
            bg = QColor("#2A0A3A" if r % 2 else "#330D44")
        tr = ind.get("ema_trend")
        trend_s = "▲ UP" if tr == 1 else ("▼ DOWN" if tr == -1 else "—")
        trend_c = GREEN if tr == 1 else (RED if tr == -1 else DIM)
        cells = [
            _i(rank_s, color=rank_col), _i(sym, color=sym_col, bold=is_setup),
            _i(f"{pct:+.2f}%", color=GREEN if pct >= 1 else _pc(pct), bold=pct >= 1),
            _i(f"{ltp:.2f}" if ltp else "—", color=TEXT),
            _i(rvol_s, color=rvol_c, bold=bool(rvol and rvol >= 2)),
            _i(vfstreak_s, color=vfstreak_c, bold=(abs(st["score"]) >= 2)),
            _i(vf_s, color=vf_c), _i(rsi_s, color=rsi_c), _i(mh_s, color=mh_c),
            _i(vwap_s, color=TEXT),
            _i(av_s, color=av_c, bold=(ind.get("above_vwap") is True)),
            _i(sl_s, color=sl_c, bold=(ind.get("vwap_slope") is True)),
            _i(hl_s, color=hl_c, bold=(ind.get("higher_lows") is True)),
            _i(lh_s, color=lh_c, bold=(ind.get("hh_new_peak") or ind.get("lower_highs") is True)),
            _i(spk_s, color=spk_c, bold=spk),
            _i(struct_s, color=struct_c, bold=struct_bold),
            _i(trend_s, color=trend_c, bold=(tr is not None)),
        ]
        for c, it in enumerate(cells): it.setBackground(QBrush(bg)); self._tbl.setItem(r, c, it)


# ── VolumeFilter ──────────────────────────────────────────────────────────────
VF_SUST_LO = 50.0; VF_SUST_HI = 2500.0
VF_H    = ["RANK","SYMBOL","SCORE","VOL FLOW","RSI","% CHG","LTP",
           "VOL CURR","RVOL","▲VWAP","HI-LOWS","LIQUIDITY","CROSS TIME"]
SHORT_H = ["RANK","SYMBOL","% CHG","LTP","VOL FLOW","RSI","VWAP","LO-HIGHS","LIQUIDITY"]


class VolumeFilter(QWidget):
    add_to_pms = pyqtSignal(str, float, float)

    def __init__(self, pms_ref=None):
        super().__init__()
        self._pms_ref    = pms_ref
        self._cache      = []
        self._all_rows   = []
        self._loser_rows = []
        self._mode       = "SUSTAINABLE"
        self._float_win  = None   # dock-out dialog

        root = QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(0)

        # ── Control bar ───────────────────────────────────────────────────────
        ctrl = QHBoxLayout(); ctrl.setContentsMargins(10,6,10,6); ctrl.setSpacing(10)

        self._mode_btn = QPushButton("⚡ SUSTAINABLE  50K–2500K")
        self._mode_btn.setCheckable(True); self._mode_btn.setChecked(True)
        self._mode_btn.setStyleSheet(
            f"background:{STEEL};color:#fff;font-weight:700;padding:5px 16px;"
            f"border-radius:14px;border:none;font-size:11px;")
        self._mode_btn.clicked.connect(self._toggle_mode)
        ctrl.addWidget(self._mode_btn)
        ctrl.addStretch()

        self._cnt_lbl = QLabel("—")
        self._cnt_lbl.setStyleSheet(f"color:{GOLD};font-size:11px;font-weight:700;")
        ctrl.addWidget(self._cnt_lbl)

        # Dock/float button (feature 8)
        self._dock_btn = QPushButton("⇱ FLOAT")
        self._dock_btn.setMaximumWidth(80)
        self._dock_btn.setStyleSheet(
            f"background:{PANEL};color:{GOLD};font-weight:700;font-size:10px;"
            f"padding:4px 8px;border-radius:10px;border:1px solid {BORDER};")
        self._dock_btn.clicked.connect(self._toggle_float)
        ctrl.addWidget(self._dock_btn)

        ctrl_w = QWidget(); ctrl_w.setLayout(ctrl)
        ctrl_w.setStyleSheet(f"background:{PANEL};border-bottom:1px solid {BORDER};")
        root.addWidget(ctrl_w)

        # ── Sub-tabs ──────────────────────────────────────────────────────────
        self._sub_tabs = QTabWidget()

        buyers_w = QWidget(); bl = QVBoxLayout(buyers_w); bl.setContentsMargins(0,0,0,0)
        self._tbl = QTableWidget(0, len(VF_H)); _set_headers(self._tbl, VF_H)
        self._tbl.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tbl.customContextMenuRequested.connect(self._ctx)
        bl.addWidget(self._tbl)
        self._sub_tabs.addTab(buyers_w, "  BUYERS  ")

        sellers_w = QWidget(); sl2 = QVBoxLayout(sellers_w); sl2.setContentsMargins(0,0,0,0)
        note = QLabel("  Top losers — lower highs forming. Right-click → SHORT.")
        note.setStyleSheet(f"color:{DIM};font-size:11px;padding:4px;"); sl2.addWidget(note)
        self._short_tbl = QTableWidget(0, len(SHORT_H)); _set_headers(self._short_tbl, SHORT_H)
        self._short_tbl.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._short_tbl.customContextMenuRequested.connect(self._short_ctx)
        sl2.addWidget(self._short_tbl)
        self._sub_tabs.addTab(sellers_w, "  SELLERS  ")

        vg_w = QWidget(); vgl = QVBoxLayout(vg_w); vgl.setContentsMargins(0,0,0,0)
        vg_note = QLabel("  Ranked by volume spike ratio — biggest surge vs recent average")
        vg_note.setStyleSheet(f"color:{DIM};font-size:11px;padding:4px;"); vgl.addWidget(vg_note)
        self._vg_tbl = QTableWidget(0, 6)
        _set_headers(self._vg_tbl, ["RANK","SYMBOL","SPIKE x","VOL CURR","% CHG","LTP"])
        vgl.addWidget(self._vg_tbl)
        self._sub_tabs.addTab(vg_w, "  VOL GAINERS  ")

        root.addWidget(self._sub_tabs)

    def set_pms(self, pms): self._pms_ref = pms

    # ── Float/dock out ────────────────────────────────────────────────────────
    def _toggle_float(self):
        """Open a floating mirror dialog with tabs: BUYERS / SELLERS / VOL GAINERS."""
        if self._float_win and not self._float_win.isHidden():
            self._float_win.hide()
            self._dock_btn.setText("⇱ FLOAT")
            self._dock_btn.setStyleSheet(
                f"background:{PANEL};color:{GOLD};font-weight:700;font-size:10px;"
                f"padding:4px 8px;border-radius:10px;border:1px solid {BORDER};")
            return
        if self._float_win is None:
            self._float_win = QDialog(self)
            self._float_win.setWindowTitle("Volume Filter")
            self._float_win.resize(900, 520)
            self._float_win.setStyleSheet(SS)
            self._float_win.setWindowFlags(
                Qt.WindowType.Window |
                Qt.WindowType.WindowMinMaxButtonsHint |
                Qt.WindowType.WindowCloseButtonHint)
            lo = QVBoxLayout(self._float_win); lo.setContentsMargins(6,6,6,6); lo.setSpacing(4)

            # Top control bar
            top_row = QHBoxLayout(); top_row.setSpacing(8)
            mode_lbl = QLabel("MODE:")
            mode_lbl.setStyleSheet(f"color:{DIM};font-size:10px;")
            top_row.addWidget(mode_lbl)
            self._float_mode_btn = QPushButton("⚡ SUSTAINABLE  50K–2500K")
            self._float_mode_btn.setCheckable(True)
            self._float_mode_btn.setChecked(self._mode == "SUSTAINABLE")
            self._float_mode_btn.setStyleSheet(
                f"background:{STEEL};color:#fff;font-weight:700;padding:4px 14px;"
                f"border-radius:12px;border:none;font-size:11px;")
            self._float_mode_btn.clicked.connect(self._float_mode_toggle)
            top_row.addWidget(self._float_mode_btn); top_row.addStretch()
            self._float_cnt = QLabel("—")
            self._float_cnt.setStyleSheet(f"color:{GOLD};font-size:11px;font-weight:700;")
            top_row.addWidget(self._float_cnt)
            lo.addLayout(top_row)

            # Tabbed tables
            self._float_tabs = QTabWidget()

            # BUYERS tab
            buyers_w = QWidget(); bl = QVBoxLayout(buyers_w)
            bl.setContentsMargins(0,2,0,0); bl.setSpacing(2)
            add_buyers_row = QHBoxLayout()
            _ab = QPushButton("+ Add to PMS")
            _ab.setMaximumWidth(110)
            _ab.setStyleSheet(f"background:{GREEN};color:#000;font-weight:700;font-size:10px;"
                              f"padding:4px 10px;border-radius:10px;border:none;")
            _ab.clicked.connect(self._float_add_selected_buyer)
            add_buyers_row.addWidget(QLabel("  Right-click or select + click:"))
            add_buyers_row.addWidget(_ab); add_buyers_row.addStretch()
            bl.addLayout(add_buyers_row)
            self._float_buyers = QTableWidget(0, len(VF_H))
            _set_headers(self._float_buyers, VF_H)
            self._float_buyers.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self._float_buyers.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._float_buyers.customContextMenuRequested.connect(self._float_buyers_ctx)
            bl.addWidget(self._float_buyers)
            self._float_tabs.addTab(buyers_w, "  BUYERS  ")

            # SELLERS tab
            sellers_w = QWidget(); sl2 = QVBoxLayout(sellers_w)
            sl2.setContentsMargins(0,2,0,0); sl2.setSpacing(2)
            add_sellers_row = QHBoxLayout()
            _as = QPushButton("SHORT → PMS")
            _as.setMaximumWidth(110)
            _as.setStyleSheet(f"background:{RED};color:#fff;font-weight:700;font-size:10px;"
                              f"padding:4px 10px;border-radius:10px;border:none;")
            _as.clicked.connect(self._float_add_selected_seller)
            add_sellers_row.addWidget(QLabel("  Right-click or select + click:"))
            add_sellers_row.addWidget(_as); add_sellers_row.addStretch()
            sl2.addLayout(add_sellers_row)
            self._float_sellers = QTableWidget(0, len(SHORT_H))
            _set_headers(self._float_sellers, SHORT_H)
            self._float_sellers.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self._float_sellers.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._float_sellers.customContextMenuRequested.connect(self._float_sellers_ctx)
            sl2.addWidget(self._float_sellers)
            self._float_tabs.addTab(sellers_w, "  SELLERS  ")

            # VOL GAINERS tab (extended columns)
            VG_H_F = ["RANK","SYMBOL","SPIKE x","VOL CURR","% CHG","LTP","SCORE","VOL FLOW","▲VWAP","HI-LOWS"]
            vg_w = QWidget(); vgl = QVBoxLayout(vg_w)
            vgl.setContentsMargins(0,2,0,0); vgl.setSpacing(2)
            add_vg_row = QHBoxLayout()
            _avg_btn = QPushButton("+ Add to PMS")
            _avg_btn.setMaximumWidth(110)
            _avg_btn.setStyleSheet(f"background:{GOLD};color:#000;font-weight:700;font-size:10px;"
                                   f"padding:4px 10px;border-radius:10px;border:none;")
            _avg_btn.clicked.connect(self._float_add_selected_vg)
            add_vg_row.addWidget(QLabel("  Ranked by volume spike ratio:"))
            add_vg_row.addWidget(_avg_btn); add_vg_row.addStretch()
            vgl.addLayout(add_vg_row)
            self._float_vg = QTableWidget(0, len(VG_H_F))
            _set_headers(self._float_vg, VG_H_F)
            self._float_vg.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self._float_vg.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._float_vg.customContextMenuRequested.connect(self._float_vg_ctx)
            vgl.addWidget(self._float_vg)
            self._float_tabs.addTab(vg_w, "  VOL GAINERS  ")

            lo.addWidget(self._float_tabs)

            def _float_close(e):
                e.accept()
                self._on_float_closed()
            self._float_win.closeEvent = _float_close

        self._float_win.show(); self._float_win.raise_()
        self._sync_float_tables()
        self._dock_btn.setText("⇲ CLOSE FLOAT")
        self._dock_btn.setStyleSheet(
            f"background:{GOLD};color:#000;font-weight:700;font-size:10px;"
            f"padding:4px 8px;border-radius:10px;border:none;")

    def _on_float_closed(self):
        self._dock_btn.setText("⇱ FLOAT")
        self._dock_btn.setStyleSheet(
            f"background:{PANEL};color:{GOLD};font-weight:700;font-size:10px;"
            f"padding:4px 8px;border-radius:10px;border:1px solid {BORDER};")

    def _float_mode_toggle(self, checked):
        """Mirror the mode toggle in the float window."""
        self._mode_btn.setChecked(checked)
        self._toggle_mode(checked)
        if checked:
            self._float_mode_btn.setText("⚡ SUSTAINABLE  50K–2500K")
            self._float_mode_btn.setStyleSheet(
                f"background:{STEEL};color:#fff;font-weight:700;padding:4px 14px;"
                f"border-radius:12px;border:none;font-size:11px;")
        else:
            self._float_mode_btn.setText("🔥 HIGH VOL  (all flows)")
            self._float_mode_btn.setStyleSheet(
                f"background:#D35400;color:#fff;font-weight:700;padding:4px 14px;"
                f"border-radius:12px;border:none;font-size:11px;")
        self._sync_float_tables()

    def _sync_float_tables(self):
        """Copy main table rows into the float mirror tables."""
        if self._float_win is None or self._float_win.isHidden(): return
        from PyQt6.QtWidgets import QTableWidgetItem
        self._float_cnt.setText(self._cnt_lbl.text())
        # Buyers + sellers mirror
        for src, dst in [(self._tbl, self._float_buyers), (self._short_tbl, self._float_sellers)]:
            dst.setRowCount(src.rowCount())
            for r in range(src.rowCount()):
                for c in range(src.columnCount()):
                    it = src.item(r, c)
                    if it:
                        ni = QTableWidgetItem(it.text())
                        ni.setForeground(it.foreground()); ni.setBackground(it.background())
                        ni.setFont(it.font()); ni.setTextAlignment(it.textAlignment())
                        ni.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                        dst.setItem(r, c, ni)
        # Vol gainers mirror — extended columns
        if not hasattr(self, "_float_vg"): return
        scores = _combined_score(self._all_rows)
        vg = sorted([r for r in self._all_rows if r.get("ind",{}).get("spike_ratio",0) > 0],
                    key=lambda r: -r["ind"]["spike_ratio"])[:20]
        self._float_vg.setRowCount(len(vg))
        from screener.utils import _i as _ui, _fmt_vol as _fv, _ck as _ck2, _bg as _bg2
        for rank, d in enumerate(vg):
            ind = d.get("ind") or {}; spkr = ind.get("spike_ratio",0); vc = ind.get("vol_current")
            score = scores.get(d["symbol"],0)
            vf = ind.get("vol_flow"); vf_s = f"{vf:+.0f}K" if vf is not None else "—"
            vf_c = GREEN if (vf and vf > 0) else (RED if (vf and vf < 0) else DIM)
            av_s, av_c = _ck2(ind.get("above_vwap")); hl_s, hl_c = _ck2(ind.get("higher_lows"))
            sc_c = GREEN if score >= 67 else (GOLD if score >= 34 else DIM)
            bg = _bg2(rank)
            cells = [
                _ui(str(rank+1), color=DIM),
                _ui(d["symbol"], color=GOLD if spkr >= 2 else TEXT, bold=spkr >= 2),
                _ui(f"x{spkr:.1f}", color=RED if spkr >= 3 else (GOLD if spkr >= 2 else DIM), bold=spkr >= 2),
                _ui(_fv(vc), color=GOLD if (vc and vc > 500_000) else TEXT),
                _ui(f"{d['per_change']:+.2f}%", color=GREEN if d["per_change"] >= 1 else (RED if d["per_change"] < 0 else DIM)),
                _ui(f"{d['ltp']:.2f}" if d["ltp"] else "—", color=TEXT),
                _ui(f"{score:.0f}", color=sc_c, bold=(score >= 67)),
                _ui(vf_s, color=vf_c),
                _ui(av_s, color=av_c, bold=(ind.get("above_vwap") is True)),
                _ui(hl_s, color=hl_c, bold=(ind.get("higher_lows") is True)),
            ]
            from PyQt6.QtGui import QBrush as _QB, QColor as _QC
            for c, it in enumerate(cells):
                it.setBackground(_QB(_QC(bg) if not isinstance(bg, _QC) else bg))
                self._float_vg.setItem(rank, c, it)

    def _float_add_selected_buyer(self):
        row = self._float_buyers.currentRow()
        if row < 0 or row >= len(self._cache): return
        d = self._cache[row]; sym = d["symbol"]; ltp = d.get("ltp") or 0.0
        vf = d.get("ind",{}).get("vol_flow") or 0.0
        self.add_to_pms.emit(sym, float(ltp), float(vf))

    def _float_add_selected_seller(self):
        row = self._float_sellers.currentRow()
        if row < 0 or row >= len(self._loser_rows): return
        d = self._loser_rows[row]; sym = d["symbol"]; ltp = d.get("ltp") or 0.0
        if self._pms_ref:
            vf = d.get("ind",{}).get("vol_flow") or 0.0
            self._pms_ref.add(sym, float(ltp), vol_flow=float(vf), direction="SHORT")

    def _float_add_selected_vg(self):
        if not hasattr(self, "_float_vg"): return
        row = self._float_vg.currentRow()
        vg = sorted([r for r in self._all_rows if r.get("ind",{}).get("spike_ratio",0) > 0],
                    key=lambda r: -r["ind"]["spike_ratio"])[:20]
        if row < 0 or row >= len(vg): return
        d = vg[row]; sym = d["symbol"]; ltp = d.get("ltp") or 0.0
        vf = d.get("ind",{}).get("vol_flow") or 0.0
        self.add_to_pms.emit(sym, float(ltp), float(vf))

    def _float_vg_ctx(self, pos):
        if not hasattr(self, "_float_vg"): return
        row = self._float_vg.rowAt(pos.y())
        vg = sorted([r for r in self._all_rows if r.get("ind",{}).get("spike_ratio",0) > 0],
                    key=lambda r: -r["ind"]["spike_ratio"])[:20]
        if row < 0 or row >= len(vg): return
        d = vg[row]; sym = d["symbol"]; ltp = d.get("ltp") or 0.0
        menu = QMenu(self._float_vg)
        act = menu.addAction(f"Add  {sym}  to PMS  @  {ltp:.2f}")
        if menu.exec(self._float_vg.viewport().mapToGlobal(pos)) == act:
            vf = d.get("ind",{}).get("vol_flow") or 0.0
            self.add_to_pms.emit(sym, float(ltp), float(vf))

    def _float_buyers_ctx(self, pos):
        row = self._float_buyers.rowAt(pos.y())
        if row < 0 or row >= len(self._cache): return
        d = self._cache[row]; sym = d["symbol"]; ltp = d.get("ltp") or 0.0
        menu = QMenu(self._float_buyers)
        act  = menu.addAction(f"Add  {sym}  to PMS  @  {ltp:.2f}")
        if menu.exec(self._float_buyers.viewport().mapToGlobal(pos)) == act:
            vf = d.get("ind",{}).get("vol_flow") or 0.0
            self.add_to_pms.emit(sym, float(ltp), float(vf))

    def _float_sellers_ctx(self, pos):
        row = self._float_sellers.rowAt(pos.y())
        if row < 0 or row >= len(self._loser_rows): return
        d = self._loser_rows[row]; sym = d["symbol"]; ltp = d.get("ltp") or 0.0
        menu = QMenu(self._float_sellers)
        act  = menu.addAction(f"SHORT  {sym}  @  {ltp:.2f}")
        if menu.exec(self._float_sellers.viewport().mapToGlobal(pos)) == act and self._pms_ref:
            vf = d.get("ind",{}).get("vol_flow") or 0.0
            self._pms_ref.add(sym, float(ltp), vol_flow=float(vf), direction="SHORT")

    def _toggle_mode(self, checked):
        if checked:
            self._mode = "SUSTAINABLE"
            self._mode_btn.setText("⚡ SUSTAINABLE  50K–2500K")
            self._mode_btn.setStyleSheet(
                f"background:{STEEL};color:#fff;font-weight:700;padding:5px 16px;"
                f"border-radius:14px;border:none;font-size:11px;")
        else:
            self._mode = "HIGH_VOL"
            self._mode_btn.setText("🔥 HIGH VOL  (all flows)")
            self._mode_btn.setStyleSheet(
                f"background:#D35400;color:#fff;font-weight:700;padding:5px 16px;"
                f"border-radius:14px;border:none;font-size:11px;")
        self._rebuild(self._all_rows)

    def _ctx(self, pos):
        row = self._tbl.rowAt(pos.y())
        menu = QMenu(self._tbl)
        act = None
        if 0 <= row < len(self._cache):
            d = self._cache[row]; sym = d["symbol"]; ltp = d.get("ltp") or 0.0
            act = menu.addAction(f"Add  {sym}  to PMS  @  {ltp:.2f}")
            menu.addSeparator()
        ob_act = menu.addAction("📋 Order Book")
        chosen = menu.exec(self._tbl.viewport().mapToGlobal(pos))
        if act and chosen == act:
            vf = d.get("ind",{}).get("vol_flow") or 0.0
            self.add_to_pms.emit(sym, float(ltp), float(vf))
        elif chosen == ob_act:
            open_order_book_dialog(self, self._pms_ref)

    def _short_ctx(self, pos):
        row = self._short_tbl.rowAt(pos.y())
        menu = QMenu(self._short_tbl)
        act = None
        if 0 <= row < len(self._loser_rows):
            d = self._loser_rows[row]; sym = d["symbol"]; ltp = d.get("ltp") or 0.0
            # Says "SHORT" not "Add to PMS" for the sellers tab
            act = menu.addAction(f"SHORT  {sym}  @  {ltp:.2f}")
            menu.addSeparator()
        ob_act = menu.addAction("📋 Order Book")
        chosen = menu.exec(self._short_tbl.viewport().mapToGlobal(pos))
        if act and chosen == act and self._pms_ref:
            vf = d.get("ind",{}).get("vol_flow") or 0.0
            self._pms_ref.add(sym, float(ltp), vol_flow=float(vf), direction="SHORT")
        elif chosen == ob_act:
            open_order_book_dialog(self, self._pms_ref)

    def refresh(self, rows):       self._all_rows = rows; self._rebuild(rows)
    def refresh_losers(self, rows): self._loser_rows = rows; self._rebuild_shorts(rows)

    def _rebuild(self, rows):
        scores = _combined_score(rows)
        if self._mode == "SUSTAINABLE":
            visible = [r for r in rows
                       if r.get("ind",{}).get("vol_flow") is not None
                       and VF_SUST_LO <= abs(r["ind"]["vol_flow"]) <= VF_SUST_HI]
            if not visible: visible = rows
        else:
            visible = rows
        sorted_rows = sorted(visible, key=lambda r: (
            0 if r.get("is_setup") else 1,
            -scores.get(r["symbol"],0), -r["per_change"]))
        self._cache = sorted_rows
        self._cnt_lbl.setText(f"{len(sorted_rows)} stocks  [{self._mode}]")
        self._tbl.setRowCount(len(sorted_rows))
        for rank, d in enumerate(sorted_rows):
            ind = d.get("ind") or {}; sym = d["symbol"]; pct = d["per_change"]; ltp = d["ltp"]
            is_setup = d.get("is_setup", False); crossed = pct >= 1.0
            no_data = not d.get("has_yf", True); score = scores.get(sym, 0)
            bg = (QColor("#071A0C" if rank%2 else "#091F0E") if is_setup else
                  QColor("#1A140A" if rank%2 else "#1F190A") if crossed else _bg(rank))
            sym_c = GREEN if is_setup else (GOLD if crossed else TEXT)
            sc_c  = GREEN if score >= 67 else (GOLD if score >= 34 else DIM)
            vf = ind.get("vol_flow"); vf_s = f"{vf:+.0f}K" if vf is not None else ("n/a" if no_data else "—")
            vf_c = GREEN if (vf and vf > 0) else (RED if (vf and vf < 0) else DIM)
            rsi = ind.get("rsi")
            rsi_s = f"{rsi:.0f}" if rsi is not None else ("n/a" if no_data else "—")
            rsi_c = RED if (rsi and rsi >= 70) else (GREEN if (rsi and rsi <= 30) else TEXT)
            vc = ind.get("vol_current"); vc_s = _fmt_vol(vc) if vc else ("n/a" if no_data else "—")
            vc_c = GOLD if (vc and vc > 500_000) else (TEXT if vc else DIM)
            rvol = ind.get("rvol"); rvol_s = f"{rvol:.1f}x" if rvol else "—"
            rvol_c = GREEN if (rvol and rvol >= 2) else (GOLD if (rvol and rvol >= 1.3) else DIM)
            av_s, av_c = _ck(ind.get("above_vwap")); hl_s, hl_c = _ck(ind.get("higher_lows"))
            liq = ind.get("liquidity_note",""); liq_s = "OK" if liq == "OK" else (liq[:12] if liq else "—")
            liq_c = GREEN if liq == "OK" else (RED if liq else DIM)
            ct = d.get("cross_time")
            cells = [
                _i(str(rank+1), color=DIM), _i(sym, color=sym_c, bold=is_setup),
                _i(f"{score:.0f}", color=sc_c, bold=(score >= 67)),
                _i(vf_s, color=vf_c, bold=bool(vf and vf > 0)), _i(rsi_s, color=rsi_c),
                _i(f"{pct:+.2f}%", color=GREEN if pct >= 1 else _pc(pct), bold=pct >= 1),
                _i(f"{ltp:.2f}" if ltp else "—", color=TEXT),
                _i(vc_s, color=vc_c), _i(rvol_s, color=rvol_c),
                _i(av_s, color=av_c, bold=(ind.get("above_vwap") is True)),
                _i(hl_s, color=hl_c, bold=(ind.get("higher_lows") is True)),
                _i(liq_s, color=liq_c), _i(ct or "—", color=GOLD if ct else DIM),
            ]
            for c, it in enumerate(cells): it.setBackground(QBrush(bg)); self._tbl.setItem(rank, c, it)
        # Vol gainers
        vg = sorted([r for r in rows if r.get("ind",{}).get("spike_ratio",0) > 0],
                    key=lambda r: -r["ind"]["spike_ratio"])[:20]
        self._vg_tbl.setRowCount(len(vg))
        for rank, d in enumerate(vg):
            ind = d.get("ind") or {}; spkr = ind.get("spike_ratio",0); vc = ind.get("vol_current")
            bg = _bg(rank)
            cells = [
                _i(str(rank+1), color=DIM),
                _i(d["symbol"], color=GOLD if spkr >= 2 else TEXT, bold=spkr >= 2),
                _i(f"x{spkr:.1f}", color=RED if spkr >= 3 else (GOLD if spkr >= 2 else DIM), bold=spkr >= 2),
                _i(_fmt_vol(vc), color=GOLD if (vc and vc > 500_000) else TEXT),
                _i(f"{d['per_change']:+.2f}%", color=GREEN if d["per_change"] >= 1 else _pc(d["per_change"])),
                _i(f"{d['ltp']:.2f}" if d["ltp"] else "—", color=TEXT),
            ]
            for c, it in enumerate(cells): it.setBackground(QBrush(bg)); self._vg_tbl.setItem(rank, c, it)
        self._sync_float_tables()

    def _rebuild_shorts(self, rows):
        self._short_tbl.setRowCount(len(rows))
        for rank, d in enumerate(rows):
            ind = d.get("ind") or {}; pct = d["per_change"]; ltp = d["ltp"]
            is_short = d.get("is_short_setup", False)
            bg = QColor("#1A0A0A" if rank%2 else "#220C0C") if is_short else _bg(rank)
            sym_c = ROSE if is_short else TEXT
            vf = ind.get("vol_flow"); vf_s = f"{vf:+.0f}K" if vf is not None else "—"
            vf_c = RED if (vf and vf < 0) else (GREEN if (vf and vf > 0) else DIM)
            rsi = ind.get("rsi"); rsi_s = f"{rsi:.0f}" if rsi is not None else "—"
            rsi_c = GREEN if (rsi and rsi <= 30) else (RED if (rsi and rsi >= 70) else TEXT)
            vwap = ind.get("vwap"); vwap_s = f"{vwap:.2f}" if vwap else "—"
            lh_s, lh_c = _ck(ind.get("lower_highs"))
            liq = ind.get("liquidity_note",""); liq_s = "OK" if liq == "OK" else (liq[:12] if liq else "—")
            liq_c = GREEN if liq == "OK" else (RED if liq else DIM)
            cells = [
                _i(str(rank+1), color=DIM),
                _i(d["symbol"], color=sym_c, bold=is_short),
                _i(f"{pct:+.2f}%", color=RED if pct < 0 else TEXT, bold=pct <= -1),
                _i(f"{ltp:.2f}" if ltp else "—", color=TEXT),
                _i(vf_s, color=vf_c), _i(rsi_s, color=rsi_c),
                _i(vwap_s, color=TEXT),
                _i(lh_s, color=lh_c, bold=(ind.get("lower_highs") is True)),
                _i(liq_s, color=liq_c),
            ]
            for c, it in enumerate(cells): it.setBackground(QBrush(bg)); self._short_tbl.setItem(rank, c, it)
        self._sync_float_tables()
