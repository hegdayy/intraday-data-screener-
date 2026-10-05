"""pms.py — paper portfolio manager (PMSManual) and the auto-cycle panel (AutoPanel).

Handles position sizing, entries and exits, stop-loss and trailing-stop monitoring,
P&L tracking and session persistence. All orders go through the paper broker.
"""
import math, datetime, csv, os, threading, json, logging
log = logging.getLogger(__name__)

_PMS_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _entry_log_write(sym, entry_price, direction, ind):
    """Log an indicator snapshot at every entry (one row per entry)."""
    try:
        d = os.path.join(_PMS_SCRIPT_DIR, "sim_logs")
        os.makedirs(d, exist_ok=True)
        now = datetime.datetime.now()
        path = os.path.join(d, f"entries_{now.strftime('%Y-%m-%d')}.csv")
        new = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "symbol", "direction", "entry_price",
                            "higher_lows", "rsi",
                            "vol_flow", "vol_current", "rvol", "vwap",
                            "above_vwap", "liquidity_note", "spike",
                            "spike_ratio"])
            ind = ind or {}
            w.writerow([
                now.strftime("%H:%M:%S"), sym, direction, entry_price,
                ind.get("higher_lows"), ind.get("rsi"),
                ind.get("vol_flow"), ind.get("vol_current"), ind.get("rvol"), ind.get("vwap"),
                ind.get("above_vwap"), ind.get("liquidity_note"), ind.get("spike"),
                ind.get("spike_ratio"),
            ])
    except Exception as e:
        log.warning(f"entry log write failed for {sym}: {e}")

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QLabel,
    QSpinBox, QDoubleSpinBox, QComboBox, QCheckBox, QPushButton,
    QMenu, QMessageBox, QTextEdit, QFrame, QDialog,
    QScrollArea, QSizePolicy, QHeaderView
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QColor, QBrush, QPainter, QPen, QLinearGradient, QPainterPath, QFont

from screener.constants import (
    BG, ALT, PANEL, BORDER, HEADER, GOLD, TEXT, DIM, GREEN, RED,
    ORANGE, CYAN, MAG, MONO, ROSE, TEAL, STEEL, UI, SS
)
from screener.utils import (
    _i, _bg, _pc, _divider, _divider_v, _section_lbl, _set_headers,
    _weighted_allocs, _alloc_preview,
    _play, _play_confirm, _play_exit, _play_sl_warn, _play_pstop, _play_btn,
    _play_auto_exit_warn,
    _SND_WARNING, _SND_BREACH,
    _log_warn, _backfill_warn_log, _SCRIPT_DIR,
    open_order_book_dialog
)
from screener.data import _combined_score
from screener.models import Position

from screener.broker import broker_buy, broker_sell, set_safety, is_safety_on, get_available_funds, get_order_book, get_market_depth

POS_H = ["SYMBOL","DIR","RANK","ENTRY","CURRENT","QTY","ALLOC","% GAIN","PEAK %",
         "PnL Rs","VOL FLOW","STRUCTURE","LIQUIDITY","SL/TRAIL","SL LOG"]

BROKERAGE_RATE = 0.0003          # 0.03% per leg
BROKERAGE_CAP  = 20.0            # Rs 20 per order cap

def _brok(cost_rs: float) -> float:
    """Brokerage: 0.03% capped at Rs 20 per leg."""
    return min(cost_rs * BROKERAGE_RATE, BROKERAGE_CAP)
TOTAL_MKT_MINS = 375      # NSE session


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _confirm_dialog(parent, title, message) -> bool:
    dlg = QMessageBox(parent)
    dlg.setWindowTitle(title); dlg.setText(message)
    dlg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    dlg.setDefaultButton(QMessageBox.StandardButton.No)
    dlg.setStyleSheet(
        f"QMessageBox{{background:{PANEL};color:{TEXT};}}"
        f"QLabel{{color:{TEXT};font-size:12px;}}"
        f"QPushButton{{background:{HEADER};color:{GOLD};border:1px solid {BORDER};"
        f"padding:6px 18px;border-radius:10px;font-weight:700;}}")
    return dlg.exec() == QMessageBox.StandardButton.Yes


def _liquidity_ratio(cost_rs, ind, default_slippage=0.002):
    """
    Returns (ratio 0-1, participation_rate, proj_vol_rs, note_str).
    ratio ~1.0 → very liquid (your order is tiny vs market).
    ratio ~0.0 → moves market.
    participation_rate = your_order_Rs / projected_daily_vol_Rs
    """
    try:
        import datetime as _dt
        vol_cum = ind.get("vol_cumulative")   # shares traded today so far
        ltp     = ind.get("vwap") or 0        # proxy price
        if not vol_cum or not ltp or ltp <= 0:
            return None, None, None, "n/a (no vol data)"
        now_t        = _dt.datetime.now().time()
        elapsed      = max((now_t.hour * 60 + now_t.minute) - (9 * 60 + 15), 1)
        elapsed_frac = elapsed / TOTAL_MKT_MINS
        traded_rs    = vol_cum * ltp
        proj_vol     = traded_rs / elapsed_frac if elapsed_frac > 0 else traded_rs
        part_rate    = cost_rs / proj_vol if proj_vol > 0 else 1.0
        # Orders < 0.5% of projected daily Rs volume are liquid
        LIQUID_THRESHOLD = 0.005
        ratio = max(0.0, min(1.0, 1.0 - (part_rate / LIQUID_THRESHOLD)))
        return round(ratio, 3), round(part_rate, 6), round(proj_vol, 0), None
    except Exception as e:
        return None, None, None, f"calc err: {e}"


# ══════════════════════════════════════════════════════════════════════════════
# ADD / EXIT CONFIRM DIALOG
# ══════════════════════════════════════════════════════════════════════════════

class TradeConfirmDialog(QDialog):
    """
    Entry and Exit confirm popup.
    Shows: qty, cost, brokerage (entry+exit), leftover, liquidity ratio.
    For EXIT mode: shows realised PnL instead of qty/cost.
    For P_STOP: shows comparison with replacement stock.
    Invest amount and alloc mode adjustable live.
    """
    def __init__(self, mode, sym, ltp, ind=None, default_alloc=0,
                 principal=0, leftover_cash=0, alloc_mode_idx=0,
                 pos=None,           # for EXIT
                 replace_sym=None, replace_ltp=None, replace_ind=None,  # P_STOP
                 parent=None):
        super().__init__(parent)
        self._mode       = mode    # "ENTRY" | "EXIT" | "PSTOP"
        self._ltp        = ltp
        self._ind        = ind or {}
        self._pos        = pos
        self._result_alloc = default_alloc
        self._result_mode  = "Weighted" if alloc_mode_idx == 0 else "Equal"
        self.setMinimumWidth(760)
        self.setStyleSheet(f"""
            QDialog      {{ background:{BG}; color:{TEXT}; }}
            QLabel        {{ background:transparent; border:none; }}
            QSpinBox, QDoubleSpinBox, QComboBox {{
                background:{PANEL}; color:{TEXT};
                border:1px solid {BORDER}; border-radius:6px;
                padding:4px 10px; font-family:'{MONO}'; font-size:12px; }}
            QPushButton   {{
                background:{HEADER}; color:{GOLD};
                border:1px solid {BORDER}; padding:7px 22px;
                border-radius:12px; font-weight:700; font-size:11px;
                letter-spacing:0.5px; }}
            QPushButton:hover {{ background:{BORDER}; color:{TEXT}; }}
        """)

        root = QVBoxLayout(self); root.setSpacing(0); root.setContentsMargins(0,0,0,0)

        # ── Coloured header bar ──────────────────────────────────────────────
        if mode == "ENTRY":
            hdr_bg = "#0B1A0B"; hdr_accent = GREEN
            title_text = f"ENTRY  ·  {sym}  ·  ₹{ltp:.2f}"
            title_col  = GREEN
        elif mode == "EXIT":
            hdr_bg = "#1A0808"; hdr_accent = RED
            title_text = f"EXIT  ·  {sym}  ·  ₹{ltp:.2f}"
            title_col  = RED
        else:
            hdr_bg = "#1A1000"; hdr_accent = ORANGE
            title_text = f"P_STOP  ·  {sym}  →  {replace_sym}"
            title_col  = ORANGE

        hdr = QFrame(); hdr.setFixedHeight(52)
        hdr.setStyleSheet(
            f"background:{hdr_bg}; border-bottom:2px solid {hdr_accent}; border-radius:0px;")
        hdr_lo = QHBoxLayout(hdr); hdr_lo.setContentsMargins(18,0,18,0)
        t = QLabel(title_text)
        t.setStyleSheet(
            f"color:{title_col}; font-size:15px; font-weight:700;"
            f" font-family:'{MONO}'; letter-spacing:1px; background:transparent; border:none;")
        hdr_lo.addWidget(t)
        root.addWidget(hdr)

        body = QVBoxLayout(); body.setSpacing(7); body.setContentsMargins(20,14,20,14)

        self._info_labels = []
        def _row(txt="", col=CYAN, size=12, bold=False):
            l = QLabel(txt)
            w = "700" if bold else "400"
            l.setStyleSheet(
                f"color:{col}; font-size:{size}px; font-weight:{w};"
                f" font-family:'{MONO}'; background:transparent; border:none;")
            body.addWidget(l); self._info_labels.append(l); return l

        def _divline():
            f = QFrame(); f.setFrameShape(QFrame.Shape.HLine)
            f.setStyleSheet(f"background:{BORDER}; max-height:1px; border:none;")
            body.addWidget(f)

        if mode == "ENTRY":
            # Market price display (read-only)
            price_row = QHBoxLayout()
            pl = QLabel("Market price:")
            pl.setStyleSheet(f"color:{DIM}; font-size:11px; background:transparent; border:none;")
            pv = QLabel(f"₹{ltp:.2f}")
            pv.setStyleSheet(
                f"color:{GOLD}; font-size:14px; font-weight:700;"
                f" font-family:'{MONO}'; background:transparent; border:none;")
            price_row.addWidget(pl); price_row.addWidget(pv); price_row.addStretch()
            body.addLayout(price_row)

            # Invest amount + alloc mode
            amt_row = QHBoxLayout(); amt_row.setSpacing(10)
            al = QLabel("Invest  ₹")
            al.setStyleSheet(f"color:{DIM}; font-size:11px; background:transparent; border:none;")
            self._amt_spin = QSpinBox()
            self._amt_spin.setRange(100, max(int(principal), 100))
            self._amt_spin.setValue(int(default_alloc)); self._amt_spin.setSingleStep(500)
            self._amt_spin.setMaximumWidth(130)
            ml = QLabel("Mode:")
            ml.setStyleSheet(f"color:{DIM}; font-size:11px; background:transparent; border:none;")
            self._mode_cb = QComboBox()
            self._mode_cb.addItems(["Weighted","Equal"])
            self._mode_cb.setCurrentIndex(alloc_mode_idx); self._mode_cb.setMaximumWidth(105)
            deploy_btn = QPushButton("DEPLOY ALL")
            deploy_btn.setFixedHeight(26)
            deploy_btn.setStyleSheet(
                f"background:{GOLD};color:#000;font-weight:700;font-size:10px;"
                f"padding:2px 10px;border-radius:8px;border:none;")
            deploy_btn.setToolTip("Set invest amount to all available leftover cash")
            deploy_btn.clicked.connect(lambda: self._amt_spin.setValue(int(leftover_cash)))
            amt_row.addWidget(al); amt_row.addWidget(self._amt_spin)
            amt_row.addSpacing(6); amt_row.addWidget(deploy_btn)
            amt_row.addSpacing(12); amt_row.addWidget(ml); amt_row.addWidget(self._mode_cb)
            amt_row.addStretch()
            body.addLayout(amt_row)
            _divline()
            self._lbl_qty    = _row()
            self._lbl_cost   = _row()
            self._lbl_leftov = _row(col=DIM, size=11)
            self._lbl_brok   = _row(col=DIM, size=11)
            _divline()
            self._lbl_liq    = _row()
            self._lbl_funds  = _row(bold=True)
            self._leftover_cash = leftover_cash
            self._principal     = principal
            self._amt_spin.valueChanged.connect(self._update_entry)
            self._mode_cb.currentIndexChanged.connect(self._update_entry)
            self._update_entry()

        elif mode == "EXIT":
            if pos:
                pnl_col = GREEN if (pos.current >= pos.entry) else RED
                _row(f"  Entry  ·  ₹{pos.entry:.2f}  ·  Current  ·  ₹{ltp:.2f}", col=DIM, size=11)
                _row(f"  Invested  ·  ₹{pos.actual_cost:,.2f}", col=TEXT)
                _divline()

                # Qty selector — default all, user can reduce for partial exit
                qty_row = QHBoxLayout(); qty_row.setSpacing(10)
                ql = QLabel("Sell qty:")
                ql.setStyleSheet(f"color:{DIM}; font-size:11px; background:transparent; border:none;")
                self._qty_spin = QSpinBox()
                total_qty = max(int(pos.qty), 1)
                self._qty_spin.setRange(1, total_qty)
                self._qty_spin.setValue(total_qty)   # default = ALL
                self._qty_spin.setMaximumWidth(100)
                qty_hint = QLabel(f"of {total_qty} total")
                qty_hint.setStyleSheet(f"color:{DIM}; font-size:10px; background:transparent; border:none;")
                qty_row.addWidget(ql); qty_row.addWidget(self._qty_spin)
                qty_row.addWidget(qty_hint); qty_row.addStretch()
                body.addLayout(qty_row)
                _divline()

                # Live PnL labels — updated by _update_exit
                self._lbl_exit_pnl  = _row("", col=pnl_col, bold=True)
                self._lbl_exit_peak = _row("", col=GOLD)
                self._lbl_exit_brok = _row("", col=DIM, size=11)
                self._lbl_exit_net  = _row("", col=pnl_col, bold=True, size=13)
                _divline()

                # Liquidity (static — based on full cost)
                if ind:
                    liq_r, part_r, proj_v, liq_note = _liquidity_ratio(pos.actual_cost, ind)
                else:
                    liq_r = None; liq_note = pos.liquidity or "—"
                if liq_r is not None:
                    liq_col = GREEN if liq_r >= 0.7 else (ORANGE if liq_r >= 0.3 else RED)
                    _row(f"  Liquidity  ·  {liq_r*100:.0f}%  ·  "
                         f"your order = {part_r*100:.4f}% of daily vol", col=liq_col)
                else:
                    _row(f"  Liquidity  ·  {liq_note}", col=DIM, size=11)
                if pos.sl_log and pos.sl_log != "—":
                    _row(f"  SL events  ·  {pos.sl_log}", col=ORANGE)

                self._exit_pos      = pos
                self._exit_ltp      = ltp
                self._result_qty    = total_qty
                self._qty_spin.valueChanged.connect(self._update_exit)
                self._update_exit()
            else:
                _row("  No position data.", col=RED)
                self._result_qty = 0

        else:  # PSTOP
            pos_curr = pos   # may be None for manual P_STOP with no specific retiring pos
            if pos_curr:
                pnl_curr = (ltp - pos_curr.entry) * pos_curr.qty
                pnl_col  = GREEN if pnl_curr >= 0 else RED
                vf_s     = f"{pos_curr.vol_flow:+.0f}K" if pos_curr.vol_flow is not None else "n/a"
                _row(f"  RETIRE  {sym}:", col=RED, bold=True)
                _row(f"  Entry ₹{pos_curr.entry:.2f}  ·  Now ₹{ltp:.2f}  ·  P&L ₹{pnl_curr:+.2f}", col=pnl_col)
                _row(f"  VF {vf_s}  ·  Peak {pos_curr.peak_pct:+.2f}%  ·  {pos_curr.sl_log}", col=DIM, size=11)
            else:
                _row("  MANUAL P_STOP — selecting best candidate:", col=ORANGE, bold=True)
            _divline()
            rvf  = (replace_ind or {}).get("vol_flow")
            rhl  = (replace_ind or {}).get("higher_lows")
            rpx  = (replace_ind or {}).get("px_delta")
            rll  = (replace_ind or {}).get("last_low")
            rltp = replace_ltp or 0
            # Price migration: how far above last confirmed low?
            if rll and rll > 0 and rltp > 0:
                mig_pct = (rltp - rll) / rll * 100
                mig_s   = f"↑{mig_pct:.2f}% above LOW" if mig_pct > 0 else "⚠ AT LOW"
                mig_c   = GREEN if mig_pct >= 0.5 else (ORANGE if mig_pct >= 0.15 else RED)
            else:
                mig_s = "—"; mig_c = DIM
            liq_r, _, _, liq_note = _liquidity_ratio(default_alloc, replace_ind or {})
            liq_s   = f"{liq_r*100:.0f}%" if liq_r is not None else liq_note
            liq_col2 = (GREEN if liq_r and liq_r >= 0.7 else
                        ORANGE if liq_r and liq_r >= 0.3 else RED) if liq_r else DIM
            _row(f"  ENTER  {replace_sym}  @  ₹{rltp:.2f}:", col=GREEN, bold=True)
            _row(f"  VF {rvf:+.0f}K  ·  PX {rpx:+.2f}%  ·  HL {'✓' if rhl else '✗'}"
                 if (rvf is not None and rpx is not None) else
                 f"  VF {'n/a' if rvf is None else f'{rvf:+.0f}K'}  ·  HL {'✓' if rhl else '✗'}", col=CYAN)
            _row(f"  Structure: {mig_s}", col=mig_c)
            _row(f"  Liquidity: {liq_s}", col=liq_col2)

        split_row = QHBoxLayout(); split_row.setSpacing(0)
        body_w = QWidget(); body_w.setLayout(body)
        split_row.addWidget(body_w, 3)

        vdiv = QFrame(); vdiv.setFrameShape(QFrame.Shape.VLine)
        vdiv.setStyleSheet(f"background:{BORDER}; max-width:1px; border:none;")
        split_row.addWidget(vdiv)

        split_row.addWidget(self._build_depth_panel(sym), 2)

        root.addLayout(split_row)

        # ── Footer buttons ────────────────────────────────────────────────────
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background:{BORDER}; max-height:1px; border:none;")
        root.addWidget(sep)
        footer = QHBoxLayout(); footer.setContentsMargins(18,10,18,14); footer.setSpacing(10)
        footer.addStretch()
        ok_lbl  = "CONFIRM ENTRY" if mode == "ENTRY" else ("CONFIRM EXIT" if mode == "EXIT" else "CONFIRM SWAP")
        ok_col  = GREEN if mode == "ENTRY" else (RED if mode == "EXIT" else ORANGE)
        ok_btn  = QPushButton(ok_lbl)
        ok_btn.setStyleSheet(
            f"background:{ok_col}; color:#fff; font-weight:700; font-size:11px;"
            f" padding:8px 28px; border-radius:12px; border:none; letter-spacing:0.5px;")
        can_btn = QPushButton("CANCEL")
        ok_btn.clicked.connect(self.accept); can_btn.clicked.connect(self.reject)
        footer.addWidget(can_btn); footer.addWidget(ok_btn)
        root.addLayout(footer)

    def _build_depth_panel(self, sym):
        """Right-side panel: live 5-level bid/ask depth for `sym`, straight
        from broker. Fetched once, synchronously, when the dialog opens — a
        single quote() call, same acceptable brief pause as the order book
        popup. Fails quietly into a plain message if broker isn't available,
        never blocks the rest of the dialog from working."""
        w = QWidget()
        lo = QVBoxLayout(w); lo.setContentsMargins(16, 14, 14, 14); lo.setSpacing(4)
        title = QLabel("MARKET DEPTH")
        title.setStyleSheet(f"color:{DIM};font-size:10px;font-weight:700;"
                            f"letter-spacing:1px;background:transparent;border:none;")
        lo.addWidget(title)

        try:
            depth = get_market_depth(sym)
        except Exception as e:
            err = QLabel(f"Depth unavailable\n({e})")
            err.setWordWrap(True)
            err.setStyleSheet(f"color:{DIM};font-size:10px;background:transparent;border:none;")
            lo.addWidget(err); lo.addStretch()
            return w

        buy = depth.get("buy", []); sell = depth.get("sell", [])
        if not buy and not sell:
            empty = QLabel("No depth data returned.")
            empty.setStyleSheet(f"color:{DIM};font-size:10px;background:transparent;border:none;")
            lo.addWidget(empty); lo.addStretch()
            return w

        def _cell(txt, color=TEXT, bold=False):
            l = QLabel(txt)
            l.setAlignment(Qt.AlignmentFlag.AlignCenter)
            l.setStyleSheet(f"color:{color};font-size:10px;font-weight:{'700' if bold else '400'};"
                            f"font-family:'{MONO}';background:transparent;border:none;")
            return l

        hdr_row = QHBoxLayout()
        for txt in ["Bid Qty", "Bid ₹", "Ask ₹", "Ask Qty"]:
            hdr_row.addWidget(_cell(txt, color=DIM, bold=True))
        lo.addLayout(hdr_row)

        for i in range(5):
            b = buy[i] if i < len(buy) else {}
            s = sell[i] if i < len(sell) else {}
            row = QHBoxLayout()
            row.addWidget(_cell(str(b.get("quantity", "—")) if b else "—"))
            row.addWidget(_cell(f'{b["price"]:.2f}' if b else "—", color=GREEN, bold=True))
            row.addWidget(_cell(f'{s["price"]:.2f}' if s else "—", color=RED, bold=True))
            row.addWidget(_cell(str(s.get("quantity", "—")) if s else "—"))
            lo.addLayout(row)

        if buy and sell:
            spread = sell[0]["price"] - buy[0]["price"]
            spread_lbl = QLabel(f"Spread: ₹{spread:.2f}")
            spread_lbl.setStyleSheet(f"color:{ORANGE};font-size:10px;font-weight:700;"
                                     f"background:transparent;border:none;margin-top:6px;")
            lo.addWidget(spread_lbl)
            note = QLabel("Wider spread / thin qty at top of book\n"
                          "= more slippage risk on a MARKET order.")
            note.setWordWrap(True)
            note.setStyleSheet(f"color:{DIM};font-size:9px;background:transparent;border:none;margin-top:4px;")
            lo.addWidget(note)

        lo.addStretch()
        return w

    def _update_exit(self):
        qty   = self._qty_spin.value()
        pos   = self._exit_pos; ltp = self._exit_ltp
        pnl   = (ltp - pos.entry) * qty
        pct   = (ltp - pos.entry) / pos.entry * 100 if pos.entry else 0
        cost  = qty * pos.entry
        brok  = _brok(cost) * 2
        net   = pnl - brok
        col   = GREEN if pnl >= 0 else RED
        self._lbl_exit_pnl.setText( f"  P&L  ·  ₹{pnl:+,.2f}  ({pct:+.2f}%)  [{qty} shares]")
        self._lbl_exit_pnl.setStyleSheet(
            f"color:{col}; font-size:12px; font-weight:700;"
            f" font-family:'{MONO}'; background:transparent; border:none;")
        self._lbl_exit_peak.setText(f"  Peak gain  ·  {pos.peak_pct:+.2f}%")
        self._lbl_exit_brok.setText(f"  Brokerage  ·  ₹{brok:,.2f}")
        self._lbl_exit_net.setText( f"  Net P&L  ·  ₹{net:+,.2f}")
        self._lbl_exit_net.setStyleSheet(
            f"color:{col}; font-size:13px; font-weight:700;"
            f" font-family:'{MONO}'; background:transparent; border:none;")
        self._result_qty = qty

    def _update_entry(self):
        alloc     = self._amt_spin.value()
        ltp       = self._ltp   # always market price — not user-editable
        ind       = self._ind
        qty       = math.floor(alloc / ltp) if ltp > 0 else 0
        cost      = qty * ltp
        left_slot = alloc - cost
        brok_entry = _brok(cost)
        brok_exit  = _brok(cost)
        brok_total = brok_entry + brok_exit
        sufficient = alloc <= (self._leftover_cash + self._result_alloc + 1)
        qty_col  = GREEN if qty > 0 else RED
        fund_col = GREEN if sufficient else RED
        self._lbl_qty.setText(   f"  {qty} shares  @  ₹{ltp:.2f}  =  ₹{cost:,.2f}")
        self._lbl_cost.setText(  f"  Invested  ·  ₹{cost:,.2f}")
        self._lbl_leftov.setText(f"  Slot leftover  ·  ₹{left_slot:,.2f}")
        self._lbl_brok.setText(  f"  Brokerage  ·  ₹{brok_total:,.2f}  (entry + exit)")
        liq_r, part_r, proj_v, liq_note = _liquidity_ratio(cost, ind)
        if liq_r is not None:
            liq_col = GREEN if liq_r >= 0.7 else (ORANGE if liq_r >= 0.3 else RED)
            pct_of_mkt = part_r * 100 if part_r else 0
            x = int(proj_v / cost) if (proj_v and cost > 0) else 0
            self._lbl_liq.setText(
                f"  Liquidity  ·  {liq_r*100:.0f}%  ·  your order = {pct_of_mkt:.4f}% of mkt  ·  mkt {x:,}× bigger")
            self._lbl_liq.setStyleSheet(
                f"color:{liq_col}; font-size:11px; font-family:'{MONO}';"
                f" background:transparent; border:none;")
        else:
            self._lbl_liq.setText(f"  Liquidity  ·  {liq_note}")
        self._lbl_funds.setText(f"  {'✓  Sufficient funds' if sufficient else '⚠  Insufficient funds'}")
        self._lbl_qty.setStyleSheet(
            f"color:{qty_col}; font-size:13px; font-weight:700;"
            f" font-family:'{MONO}'; background:transparent; border:none;")
        self._lbl_funds.setStyleSheet(
            f"color:{fund_col}; font-size:12px; font-weight:700;"
            f" font-family:'{MONO}'; background:transparent; border:none;")
        self._result_alloc = alloc
        self._result_mode  = self._mode_cb.currentText()

    def result_values(self):
        """Returns (alloc, mode_str, price, qty).
        For EXIT: alloc=0, mode='', price=ltp, qty=chosen sell qty.
        For ENTRY: alloc, mode, price=ltp, qty computed externally."""
        qty = getattr(self, "_result_qty", None)
        return self._result_alloc, self._result_mode, self._ltp, qty


# ══════════════════════════════════════════════════════════════════════════════
# STAT CARD
# ══════════════════════════════════════════════════════════════════════════════

def _stat_card(label, value="—", val_color=CYAN):
    card = QFrame()
    card.setStyleSheet(f"background:{PANEL};border:1px solid {BORDER};border-radius:10px;")
    lo = QVBoxLayout(card); lo.setContentsMargins(10,6,10,6); lo.setSpacing(2)
    lbl = QLabel(label)
    lbl.setStyleSheet(f"color:{DIM};font-size:9px;font-weight:700;letter-spacing:1.2px;background:transparent;border:none;")
    lbl.setAlignment(Qt.AlignmentFlag.AlignLeft)
    val = QLabel(value)
    val.setStyleSheet(f"color:{val_color};font-size:13px;font-weight:700;font-family:'{MONO}';background:transparent;border:none;")
    val.setAlignment(Qt.AlignmentFlag.AlignLeft)
    lo.addWidget(lbl); lo.addWidget(val)
    return card, val


# ══════════════════════════════════════════════════════════════════════════════
# PMSManual
# ══════════════════════════════════════════════════════════════════════════════

class PMSManual(QWidget):
    funds_result = pyqtSignal(object, object)   # (amount: float|None, error: str|None)

    def __init__(self):
        super().__init__()
        self._last_avail_funds = None
        self._in_auto_exit_confirm = False
        self._live_feed_getter = None   # callable returning the feed instance, or None
        self.funds_result.connect(self._on_funds_result)
        self._positions       = {}
        self._blink_on        = False
        self._blink_count     = {}
        self._last_rows       = []
        self._last_loser_rows = []
        self._backfill_q      = {}
        self._hl_warn_on      = True
        self._hl_auto_exit    = False
        self._lh_warn_on      = False
        self._lh_auto_exit    = False
        self._avg_sl_cutoff   = False
        self._auto_pstop_on   = False   # master toggle for auto-cycle P_STOP
        self._auto_pstop_raw  = False   # RAW MODE — bypass filters, just use _best_candidate as-is
        self._cooldown_secs   = 0       # 0 = no cooldown
        self._min_px_filter   = 0.0     # 0 = disabled
        self._cooldown_until  = {}      # symbol slot rank -> epoch seconds when re-entry allowed
        self._last_screener_candidate = None   # for mini screener display
        self._confirm_timer   = QTimer(self)
        self._confirm_timer.setSingleShot(True)
        self._confirm_timer.timeout.connect(self._clear_confirm)
        self._dock_win        = None
        self._dock_tbl        = None

        root = QVBoxLayout(self); root.setContentsMargins(8,6,8,6); root.setSpacing(4)

        self._blink_tmr = QTimer(self)
        self._blink_tmr.setInterval(300)
        self._blink_tmr.timeout.connect(self._blink_tick)
        self._blink_tmr.start()

        # ══ SETTINGS FRAME ════════════════════════════════════════════════════
        sf_frame = QFrame()
        sf_frame.setStyleSheet(f"background:{HEADER};border:1px solid {BORDER};border-radius:10px;")
        sf = QVBoxLayout(sf_frame); sf.setContentsMargins(10,7,10,7); sf.setSpacing(5)

        sr = QHBoxLayout(); sr.setSpacing(6)

        def _lbl(t):
            l = QLabel(t); l.setStyleSheet(f"color:{DIM};font-size:10px;font-weight:600;background:transparent;border:none;"); return l
        def _spin(lo2, hi, val, maxw=70):
            s = QSpinBox(); s.setRange(lo2,hi); s.setValue(val); s.setMaximumWidth(maxw)
            s.setStyleSheet(f"background:{PANEL};border:1px solid {BORDER};border-radius:6px;padding:3px 6px;"); return s
        def _dspin(lo2, hi, val, step=0.1, dec=1, suf="", maxw=78):
            s = QDoubleSpinBox(); s.setRange(lo2,hi); s.setValue(val)
            s.setSingleStep(step); s.setDecimals(dec)
            if suf: s.setSuffix(suf)
            s.setMaximumWidth(maxw)
            s.setStyleSheet(f"background:{PANEL};border:1px solid {BORDER};border-radius:6px;padding:3px 6px;"); return s

        sr.addWidget(_lbl("₹ PRINCIPAL"))
        self._prin = QSpinBox(); self._prin.setRange(1,10_000_000)
        self._prin.setValue(10000); self._prin.setSingleStep(1000); self._prin.setMaximumWidth(100)
        self._prin.setStyleSheet(f"background:{PANEL};border:1px solid {BORDER};border-radius:6px;padding:3px 6px;")
        sr.addWidget(self._prin)

        self._lbl_avail = QLabel("AVAIL: —")
        self._lbl_avail.setStyleSheet(f"color:{DIM};font-size:10px;font-family:'{MONO}';background:transparent;")
        self._lbl_avail.setToolTip("Live broker account balance (net available for trading). Click ⟳ to fetch.")
        sr.addWidget(self._lbl_avail)
        avail_refresh_btn = QPushButton("⟳")
        avail_refresh_btn.setFixedWidth(24)
        avail_refresh_btn.setToolTip("Fetch current available funds from broker")
        avail_refresh_btn.setStyleSheet(f"background:{PANEL};color:{TEXT};border:1px solid {BORDER};border-radius:6px;padding:2px;")
        avail_refresh_btn.clicked.connect(self._refresh_avail_funds)
        sr.addWidget(avail_refresh_btn)
        use_funds_btn = QPushButton("→ USE")
        use_funds_btn.setFixedWidth(50)
        use_funds_btn.setToolTip("Set Principal to the last fetched available-funds figure")
        use_funds_btn.setStyleSheet(f"background:{PANEL};color:{GOLD};border:1px solid {BORDER};border-radius:6px;padding:2px 6px;font-size:10px;font-weight:700;")
        use_funds_btn.clicked.connect(self._use_avail_funds)
        sr.addWidget(use_funds_btn)

        sr.addWidget(_lbl("SLOTS")); self._slots = _spin(1,20,5,maxw=52); sr.addWidget(self._slots)
        sr.addWidget(_lbl("CONC"))
        self._conc = _dspin(0,1,0.5,0.05,2,maxw=68); self._conc.setToolTip("0=equal  1=max skew"); sr.addWidget(self._conc)
        sr.addWidget(_lbl("SL%")); self._sl = _dspin(0.1,20,1.5,0.1,1," %",maxw=78); sr.addWidget(self._sl)
        sr.addWidget(_lbl("BB1")); self._bb1 = _spin(1,5,3,maxw=48); self._bb1.setToolTip("V1 dips before BB1"); sr.addWidget(self._bb1)
        sr.addWidget(_lbl("TARGET%")); self._tgt = _dspin(1,500,12,1,1," %",maxw=78); sr.addWidget(self._tgt)

        self._auto_qty = QCheckBox("AUTO QTY"); self._auto_qty.setChecked(True)
        self._auto_qty.setStyleSheet(f"color:{TEXT};font-size:10px;background:transparent;"); sr.addWidget(self._auto_qty)

        self._confirm_popup = QCheckBox("CONFIRM POPUP"); self._confirm_popup.setChecked(True)
        self._confirm_popup.setToolTip(
            "ON: local dialog (adjust alloc/qty, see P_STOP candidate) before "
            "the broker opens.\n"
            "OFF: skip straight to broker using default values — faster, "
            "no local popup in front of the browser.")
        self._confirm_popup.setStyleSheet(f"color:{TEXT};font-size:10px;background:transparent;")
        sr.addWidget(self._confirm_popup)

        self._alloc_mode = QComboBox()
        self._alloc_mode.addItems(["Weighted","Equal"]); self._alloc_mode.setMaximumWidth(88)
        self._alloc_mode.setCurrentIndex(1)   # default: Equal split, not Weighted
        self._alloc_mode.setStyleSheet(f"background:{PANEL};border:1px solid {BORDER};border-radius:6px;padding:3px 6px;font-size:11px;")
        sr.addWidget(self._alloc_mode)

        sr.addWidget(_divider_v())

        def _tbtn(text, bg=PANEL, fg=DIM, w=100):
            b = QPushButton(text); b.setCheckable(True); b.setMaximumWidth(w)
            b.setStyleSheet(f"background:{bg};color:{fg};font-weight:700;font-size:10px;"
                            f"padding:4px 8px;border-radius:10px;border:1px solid {BORDER};"); return b

        self._hl_btn      = _tbtn("HL WARN  ON",  TEAL,  "#000", 102); self._hl_btn.setChecked(True)
        self._hl_exit_btn = _tbtn("HL EXIT  OFF", PANEL, DIM,    102)
        self._lh_btn      = _tbtn("LH WARN  OFF", PANEL, DIM,    106)
        self._lh_exit_btn = _tbtn("LH EXIT  OFF", PANEL, DIM,    106)
        self._avg_sl_btn  = _tbtn("AVG CUT  OFF", PANEL, DIM,    106)

        self._hl_btn.clicked.connect(self._toggle_hl_warn)
        self._hl_exit_btn.clicked.connect(self._toggle_hl_exit)
        self._lh_btn.clicked.connect(self._toggle_lh_warn)
        self._lh_exit_btn.clicked.connect(self._toggle_lh_exit)
        self._avg_sl_btn.clicked.connect(self._toggle_avg_sl_cutoff)

        for b in [self._hl_btn, self._hl_exit_btn, self._lh_btn, self._lh_exit_btn, self._avg_sl_btn]:
            sr.addWidget(b)

        sr.addWidget(_divider_v())

        # Float/dock button (feature 8)
        self._dock_btn = QPushButton("⇱ FLOAT")
        self._dock_btn.setMaximumWidth(80)
        self._dock_btn.setStyleSheet(
            f"background:{PANEL};color:{GOLD};font-weight:700;font-size:10px;"
            f"padding:4px 8px;border-radius:10px;border:1px solid {BORDER};")
        self._dock_btn.clicked.connect(self._toggle_dock)
        sr.addWidget(self._dock_btn)
        sr.addStretch()

        self._confirm_lbl = QLabel("")
        self._confirm_lbl.setStyleSheet(f"color:{GREEN};font-size:10px;font-weight:700;font-family:'{MONO}';background:transparent;")
        sr.addWidget(self._confirm_lbl)
        sf.addLayout(sr)

        # ── Auto P_STOP cycle row ────────────────────────────────────────────
        ar = QHBoxLayout(); ar.setSpacing(6)
        ar.addWidget(_lbl("AUTO P_STOP"))
        self._auto_pstop_btn = _tbtn("AUTO  OFF", PANEL, DIM, 86)
        self._auto_pstop_btn.clicked.connect(self._toggle_auto_pstop)
        ar.addWidget(self._auto_pstop_btn)
        self._manual_pstop_btn = _tbtn("P_STOP ▶", PANEL, ORANGE, 80)
        self._manual_pstop_btn.setToolTip("Manually trigger P_STOP — picks best candidate now")
        self._manual_pstop_btn.clicked.connect(self._manual_pstop)
        ar.addWidget(self._manual_pstop_btn)

        ar.addWidget(_divider_v())
        self._auto_raw_btn = _tbtn("RAW MODE  OFF", PANEL, DIM, 112)
        self._auto_raw_btn.setToolTip("RAW MODE: ignore cooldown/min-PX/max-HL-age filters below —\njust take whatever P_STOP's normal best-candidate logic finds.")
        self._auto_raw_btn.clicked.connect(self._toggle_auto_raw)
        ar.addWidget(self._auto_raw_btn)

        ar.addWidget(_divider_v())
        ar.addWidget(_lbl("COOLDOWN"))
        self._cooldown_spin = _spin(0, 300, 0, maxw=58)
        self._cooldown_spin.setSuffix("s")
        self._cooldown_spin.setToolTip("Seconds to wait after an auto-exit before auto-entering the next stock.\n0 = no cooldown, re-enter immediately.")
        self._cooldown_spin.valueChanged.connect(self._on_cooldown_change)
        ar.addWidget(self._cooldown_spin)

        ar.addWidget(_lbl("MIN PX"))
        self._minpx_spin = _dspin(0, 5, 0.0, 0.1, 1, "%", maxw=66)
        self._minpx_spin.setToolTip("Minimum per-bar px gain required for an auto-entry candidate.\n0 = disabled, any px accepted.")
        self._minpx_spin.valueChanged.connect(self._on_minpx_change)
        ar.addWidget(self._minpx_spin)

        ar.addStretch()

        self._screener_lbl = QLabel("SCREENER: —")
        self._screener_lbl.setStyleSheet(f"color:{DIM};font-size:10px;font-family:'{MONO}';background:transparent;")
        ar.addWidget(self._screener_lbl)
        sf.addLayout(ar)

        # Stat cards
        cards_lo = QHBoxLayout(); cards_lo.setSpacing(5)
        self._card_principal, self._lbl_principal = _stat_card("PRINCIPAL","—",GOLD)
        self._card_deployed,  self._lbl_deployed  = _stat_card("DEPLOYED","—",CYAN)
        self._card_leftover,  self._lbl_leftover  = _stat_card("LEFTOVER","—",TEAL)
        self._card_pnl,       self._lbl_pnl       = _stat_card("SESSION PnL","—",GREEN)
        self._card_target,    self._lbl_target    = _stat_card("TARGET","—",GOLD)
        self._card_progress,  self._lbl_progress  = _stat_card("PROGRESS","—",DIM)
        self._card_avg,       self._lbl_avg_gain  = _stat_card("AVG GAIN","—",CYAN)
        self._card_split,     self._lbl_split     = _stat_card("ALLOC SPLIT","—",DIM)
        for c in [self._card_principal, self._card_deployed, self._card_leftover,
                  self._card_pnl, self._card_target, self._card_progress,
                  self._card_avg, self._card_split]:
            cards_lo.addWidget(c)
        sf.addLayout(cards_lo)
        root.addWidget(sf_frame)

        # Main table (list only — graph/list toggle removed)
        self._tbl = QTableWidget(0, len(POS_H)); _set_headers(self._tbl, POS_H)
        for _c in (POS_H.index("DIR"), POS_H.index("RANK"), POS_H.index("QTY")):
            self._tbl.horizontalHeader().setSectionResizeMode(_c, QHeaderView.ResizeMode.ResizeToContents)
        self._tbl.setAlternatingRowColors(True)
        self._tbl.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tbl.customContextMenuRequested.connect(self._menu)
        self._tbl.setMouseTracking(True)
        root.addWidget(self._tbl)

        for w in [self._prin, self._slots, self._bb1]:
            w.valueChanged.connect(self._on_alloc_change)
        for w in [self._conc, self._sl, self._tgt]:
            w.valueChanged.connect(self._on_alloc_change)
        self._auto_qty.stateChanged.connect(self._on_alloc_change)
        # alloc_mode change does NOT clear positions — just updates preview
        self._alloc_mode.currentIndexChanged.connect(self._on_alloc_mode_change)
        self._on_alloc_change()

        self._load_session()
        self._refresh()

        self._live_breach_tmr = QTimer(self)
        self._live_breach_tmr.setInterval(300)
        self._live_breach_tmr.timeout.connect(self._check_live_breach)
        self._live_breach_tmr.start()


    def set_live_feed_getter(self, fn):
        """fn() should return the current feed instance, or None if not
        connected/not on broker. Wired once by main.py, since the feed object
        is created lazily on the worker thread and doesn't exist yet at
        PMSManual construction time."""
        self._live_feed_getter = fn

    def _check_live_breach(self):
        if self._in_auto_exit_confirm:
            return   # a modal popup is already deciding something this instant
        if self._live_feed_getter is None:
            return
        feed = self._live_feed_getter()
        if feed is None:
            return
        for sym, pos in list(self._positions.items()):
            if not (pos.is_open and not pos.pending):
                continue
            anchor = pos.last_low
            if not anchor or anchor <= 0:
                continue
            if getattr(pos, "_live_breach_fired", False):
                continue   # one-shot latch — already handled, don't refire every 300ms
            try:
                live = feed.get_live(sym)
            except Exception:
                continue
            if not live or not live.get("ltp"):
                continue
            ltp = live["ltp"]
            if ltp >= anchor:
                continue

            pos._live_breach_fired = True
            result = broker_sell(sym, qty=pos.qty, price=ltp, order_type="MARKET",
                                 direction=pos.direction, reason="LIVE LOW BREACH")
            if result.get("status") == "CANCELLED":
                pos._live_breach_fired = False
                if not hasattr(self, "_auto_pstop_trail"):
                    self._auto_pstop_trail = []
                self._auto_pstop_trail.append(
                    f"⚠ LIVE BREACH SELL NEVER CONFIRMED: {sym} — still open, retry manually")
                continue
            if result.get("status") == "FAILED":
                if not hasattr(self, "_auto_pstop_trail"):
                    self._auto_pstop_trail = []
                self._auto_pstop_trail.append(
                    f"⚠ LIVE BREACH SELL FAILED: {sym} — {result.get('error','?')}")
            rank = pos.alloc_rank
            pos.close(result.get("average_price") or ltp)
            _play_exit(); self._blink_count[sym] = 14
            self._refresh(); self._save_session()
            self._run_auto_pstop_cycle([(rank, sym, "LIVE LOW BREACH")],
                                       self._last_rows, self._last_loser_rows)

    # ── properties ──────────────────────────────────────────────────────────────
    @property
    def principal(self):     return self._prin.value()
    @property
    def n_slots(self):       return self._slots.value()
    @property
    def concentration(self): return self._conc.value() if self._alloc_mode.currentIndex() == 0 else 0.0
    @property
    def auto_qty(self):      return self._auto_qty.isChecked()
    @property
    def sl_pct(self):        return self._sl.value()

    def open_position_symbols(self):
        """Returns the set of symbols currently held (open, not pending).
        Used by SimWorker to keep fetching data for positions even after
        they fall out of NSE's top-20 gainers/losers list — without this,
        a held stock's HL/LH/px_delta/breach state freezes silently the
        moment it drops out of the scan window."""
        return {p.symbol for p in self._positions.values() if p.is_open and not p.pending}

    @property
    def screener_text(self):
        """One-line summary of the current best P_STOP candidate with all key signals."""
        cand = getattr(self, "_last_screener_candidate", None)
        if not cand: return None
        ind  = cand.get("ind", {})
        vf   = ind.get("vol_flow")
        rsi_v = ind.get("rsi")
        rhl  = ind.get("higher_lows", False)
        rll  = ind.get("last_low")
        rltp = cand.get("ltp") or 0
        vf_s  = f"VF{vf:+.0f}K" if vf is not None else "VF—"
        hla_s = f"HL{'✓' if rhl else '✗'}"
        px_s  = f"RSI{rsi_v:.0f}" if rsi_v is not None else "RSI—"
        if rll and rll > 0 and rltp > 0:
            mig_pct = (rltp - rll) / rll * 100
            mig_s   = f"↑{mig_pct:.2f}%>LOW" if mig_pct > 0 else "⚠AT-LOW"
        else:
            mig_s = ""
        return f"{cand['symbol']} {cand['per_change']:+.1f}%  {vf_s}  {hla_s}  {px_s}  {mig_s}"
    @property
    def bb1_chances(self):   return self._bb1.value()
    @property
    def hl_warn_on(self):    return self._hl_warn_on

    # ── settings change ──────────────────────────────────────────────────────
    def _on_alloc_change(self):
        prin = self._prin.value(); n = max(self._slots.value(),1); conc = self.concentration
        self._confirm_lbl.setText(f"✓ Rs{prin:,}  {n} slots  SL{self._sl.value():.1f}%")
        self._confirm_timer.start(2500)
        self._lbl_principal.setText(f"Rs {prin:,}")
        self._lbl_split.setText(_alloc_preview(prin, n, conc))
        tgt = self._tgt.value()
        self._lbl_target.setText(f"{tgt:.0f}%  =  Rs {prin*tgt/100:,.0f}")
        self._refresh_stats_cards()

    def _on_alloc_mode_change(self):
        """Switch weighted/equal: update preview only, NEVER clear positions."""
        self._lbl_split.setText(_alloc_preview(
            self._prin.value(), max(self._slots.value(),1), self.concentration))
        self._refresh_stats_cards()

    def _clear_confirm(self): self._confirm_lbl.setText("")

    # ── toggles ──────────────────────────────────────────────────────────────
    def _toggle_hl_warn(self, checked):
        self._hl_warn_on = checked
        if checked:
            self._hl_btn.setText("HL WARN  ON")
            self._hl_btn.setStyleSheet(f"background:{TEAL};color:#000;font-weight:700;font-size:10px;padding:4px 8px;border-radius:10px;border:none;")
        else:
            self._hl_btn.setText("HL WARN  OFF")
            self._hl_btn.setStyleSheet(f"background:{BORDER};color:{DIM};font-weight:700;font-size:10px;padding:4px 8px;border-radius:10px;border:none;")

    def _toggle_hl_exit(self, checked):
        self._hl_auto_exit = checked
        if checked:
            self._hl_exit_btn.setText("HL EXIT  ON")
            self._hl_exit_btn.setStyleSheet(f"background:{RED};color:#fff;font-weight:700;font-size:10px;padding:4px 8px;border-radius:10px;border:none;")
        else:
            self._hl_exit_btn.setText("HL EXIT  OFF")
            self._hl_exit_btn.setStyleSheet(f"background:{PANEL};color:{DIM};font-weight:700;font-size:10px;padding:4px 8px;border-radius:10px;border:1px solid {BORDER};")

    def _toggle_lh_warn(self, checked):
        self._lh_warn_on = checked
        if checked:
            self._lh_btn.setText("LH WARN  ON")
            self._lh_btn.setStyleSheet(f"background:{ROSE};color:#000;font-weight:700;font-size:10px;padding:4px 8px;border-radius:10px;border:none;")
        else:
            self._lh_btn.setText("LH WARN  OFF")
            self._lh_btn.setStyleSheet(f"background:{PANEL};color:{DIM};font-weight:700;font-size:10px;padding:4px 8px;border-radius:10px;border:1px solid {BORDER};")

    def _toggle_lh_exit(self, checked):
        self._lh_auto_exit = checked
        if checked:
            self._lh_exit_btn.setText("LH EXIT  ON")
            self._lh_exit_btn.setStyleSheet(f"background:{RED};color:#fff;font-weight:700;font-size:10px;padding:4px 8px;border-radius:10px;border:none;")
        else:
            self._lh_exit_btn.setText("LH EXIT  OFF")
            self._lh_exit_btn.setStyleSheet(f"background:{PANEL};color:{DIM};font-weight:700;font-size:10px;padding:4px 8px;border-radius:10px;border:1px solid {BORDER};")

    def _toggle_avg_sl_cutoff(self, checked):
        self._avg_sl_cutoff = checked
        if checked:
            self._avg_sl_btn.setText("AVG CUT  ON")
            self._avg_sl_btn.setStyleSheet(f"background:{ORANGE};color:#000;font-weight:700;font-size:10px;padding:4px 8px;border-radius:10px;border:none;")
        else:
            self._avg_sl_btn.setText("AVG CUT  OFF")
            self._avg_sl_btn.setStyleSheet(f"background:{PANEL};color:{DIM};font-weight:700;font-size:10px;padding:4px 8px;border-radius:10px;border:1px solid {BORDER};")

    # ── available funds (broker balance) ───────────────────────────────────────
    def _refresh_avail_funds(self):
        self._lbl_avail.setText("AVAIL: fetching...")
        self._lbl_avail.setStyleSheet(f"color:{ORANGE};font-size:10px;font-family:'{MONO}';background:transparent;")
        def _work():
            try:
                amt = get_available_funds()
                self.funds_result.emit(amt, None)
            except Exception as e:
                self.funds_result.emit(None, str(e))
        threading.Thread(target=_work, daemon=True).start()

    def _on_funds_result(self, amt, err):
        if err:
            self._lbl_avail.setText("AVAIL: fetch failed")
            self._lbl_avail.setStyleSheet(f"color:{RED};font-size:10px;font-family:'{MONO}';background:transparent;")
            self._lbl_avail.setToolTip(f"Fetch failed: {err}")
            self._last_avail_funds = None
        else:
            self._last_avail_funds = amt
            self._lbl_avail.setText(f"AVAIL: Rs{amt:,.0f}")
            self._lbl_avail.setStyleSheet(f"color:{GREEN};font-size:10px;font-weight:700;font-family:'{MONO}';background:transparent;")
            self._lbl_avail.setToolTip("Live broker account balance (net available for trading).")

    def _use_avail_funds(self):
        if self._last_avail_funds is None:
            QMessageBox.information(self, "Available Funds",
                "Fetch available funds first — click the ⟳ button next to AVAIL.")
            return
        self._prin.setValue(max(1, int(self._last_avail_funds)))

    # ── session persistence (survives app close, same trading day only) ─────
    def _session_path(self):
        return os.path.join(_SCRIPT_DIR, "session_state.json")

    def _save_session(self):
        try:
            import dataclasses
            data = {
                "date": datetime.date.today().isoformat(),
                "positions": {sym: dataclasses.asdict(pos) for sym, pos in self._positions.items()},
                "settings": {
                    "principal": self._prin.value(), "slots": self._slots.value(),
                    "conc": self._conc.value(), "sl": self._sl.value(),
                    "bb1": self._bb1.value(), "tgt": self._tgt.value(),
                    "alloc_mode_idx": self._alloc_mode.currentIndex(),
                    "auto_qty": self._auto_qty.isChecked(),
                },
            }
            path = self._session_path(); tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, path)   # atomic — avoids a half-written file if the app dies mid-save
        except Exception as e:
            print(f"[session save failed, non-fatal] {e}")

    def _load_session(self):
        """Called once at startup. Only restores state if the saved session
        is from TODAY — a session from a previous day is left alone (start
        fresh), since positions/targets from yesterday aren't meaningful."""
        path = self._session_path()
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("date") != datetime.date.today().isoformat():
                print(f"[session] Saved session is from {data.get('date')}, not today — starting fresh.")
                return
            s = data.get("settings", {})
            self._prin.setValue(int(s.get("principal", self._prin.value())))
            self._slots.setValue(int(s.get("slots", self._slots.value())))
            self._conc.setValue(float(s.get("conc", self._conc.value())))
            self._sl.setValue(float(s.get("sl", self._sl.value())))
            self._bb1.setValue(int(s.get("bb1", self._bb1.value())))
            self._tgt.setValue(float(s.get("tgt", self._tgt.value())))
            self._alloc_mode.setCurrentIndex(int(s.get("alloc_mode_idx", self._alloc_mode.currentIndex())))
            self._auto_qty.setChecked(bool(s.get("auto_qty", self._auto_qty.isChecked())))
            restored = {}
            for sym, d in data.get("positions", {}).items():
                try:
                    restored[sym] = Position(**d)
                except Exception as e:
                    print(f"[session] Could not restore position {sym}: {e}")
            self._positions = restored
            n_open = sum(1 for p in restored.values() if p.is_open)
            print(f"[session] Restored {len(restored)} positions ({n_open} still open) from earlier today.")
        except Exception as e:
            print(f"[session load failed, non-fatal] {e}")

    def reset_session(self):
        """Wired to the SESSION RESET button (main.py toolbar). Refuses to
        clear anything while any position is still open — closed-only
        history is safe to wipe, live risk is not."""
        open_pos = [p for p in self._positions.values() if p.is_open]
        if open_pos:
            QMessageBox.warning(self, "Cannot Reset",
                f"{len(open_pos)} position(s) are still open "
                f"({', '.join(p.symbol for p in open_pos)}).\n\n"
                f"Exit or close them first — session reset refuses to touch "
                f"anything that isn't fully closed, to avoid losing track of "
                f"a real open position.")
            return False
        if not _confirm_dialog(self, "Reset Session",
                "This clears today's closed-position history and resets "
                "settings to defaults.\n\nThis cannot be undone. Continue?"):
            return False
        self._positions = {}
        try:
            path = self._session_path()
            if os.path.exists(path):
                os.remove(path)
        except Exception as e:
            print(f"[session reset] couldn't remove session file: {e}")
        self._refresh()
        return True

    # ── auto P_STOP cycle controls ───────────────────────────────────────────
    def _toggle_auto_pstop(self, checked):
        self._auto_pstop_on = checked
        if checked:
            self._auto_pstop_btn.setText("AUTO  ON")
            self._auto_pstop_btn.setStyleSheet(f"background:{GOLD};color:#000;font-weight:700;font-size:10px;padding:4px 8px;border-radius:10px;border:none;")
        else:
            self._auto_pstop_btn.setText("AUTO  OFF")
            self._auto_pstop_btn.setStyleSheet(f"background:{PANEL};color:{DIM};font-weight:700;font-size:10px;padding:4px 8px;border-radius:10px;border:1px solid {BORDER};")

    def _manual_pstop(self):
        """Manual P_STOP — shows P_STOP confirm with full signal data."""
        _play_btn()
        rows       = self._last_rows
        loser_rows = self._last_loser_rows
        if not rows:
            QMessageBox.information(self, "P_STOP", "No data yet — wait for first scan.")
            return
        held   = {p.symbol for p in self._positions.values() if p.is_open and not p.pending}
        all_r  = rows + (loser_rows or [])
        scores = _combined_score(all_r)
        cand   = self._best_candidate(held, all_r, scores,
                                       min_px=self._min_px_filter)
        if cand is None:
            QMessageBox.information(self, "P_STOP", "No qualifying candidate found right now.")
            return
        c_ltp = cand.get("ltp") or 0
        c_ind = cand.get("ind", {})
        _play_confirm()
        dlg = TradeConfirmDialog(
            mode="PSTOP", sym="—", ltp=c_ltp, ind=c_ind,
            default_alloc=int(self._leftover_cash()),
            principal=self._prin.value(),
            leftover_cash=self._leftover_cash(),
            pos=None,                          # no retiring position — manual pick
            replace_sym=cand["symbol"],
            replace_ltp=c_ltp,
            replace_ind=c_ind,
            parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted: return
        self.add(cand["symbol"], c_ltp, vol_flow=c_ind.get("vol_flow"), skip_dialog=True)
        _play_pstop()

    def _toggle_auto_raw(self, checked):
        self._auto_pstop_raw = checked
        if checked:
            self._auto_raw_btn.setText("RAW MODE  ON")
            self._auto_raw_btn.setStyleSheet(f"background:{ROSE};color:#000;font-weight:700;font-size:10px;padding:4px 8px;border-radius:10px;border:none;")
        else:
            self._auto_raw_btn.setText("RAW MODE  OFF")
            self._auto_raw_btn.setStyleSheet(f"background:{PANEL};color:{DIM};font-weight:700;font-size:10px;padding:4px 8px;border-radius:10px;border:1px solid {BORDER};")

    def _on_cooldown_change(self, val):
        self._cooldown_secs = val

    def _on_minpx_change(self, val):
        self._min_px_filter = val

    # ── float dock (mirror — no reparenting) ─────────────────────────────────
    def _toggle_dock(self):
        if self._dock_win and not self._dock_win.isHidden():
            self._dock_win.hide()
            self._dock_btn.setText("⇱ FLOAT")
            self._dock_btn.setStyleSheet(f"background:{PANEL};color:{GOLD};font-weight:700;font-size:10px;padding:4px 8px;border-radius:10px;border:1px solid {BORDER};")
            return
        if self._dock_win is None:
            self._dock_win = QDialog(self)
            self._dock_win.setWindowTitle("Portfolio — Float View")
            self._dock_win.resize(1200,420)
            self._dock_win.setStyleSheet(SS)
            self._dock_win.setWindowFlags(
                Qt.WindowType.Window |
                Qt.WindowType.WindowMinMaxButtonsHint |
                Qt.WindowType.WindowCloseButtonHint)
            lo = QVBoxLayout(self._dock_win); lo.setContentsMargins(4,4,4,4)
            self._dock_tbl = QTableWidget(0, len(POS_H))
            _set_headers(self._dock_tbl, POS_H)
            self._dock_tbl.setAlternatingRowColors(True)
            self._dock_tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            lo.addWidget(self._dock_tbl)

            def _dock_close(e):
                e.accept()
                self._on_dock_closed()
            self._dock_win.closeEvent = _dock_close
        self._dock_win.show(); self._dock_win.raise_()
        self._sync_dock_tbl()
        self._dock_btn.setText("⇲ CLOSE FLOAT")
        self._dock_btn.setStyleSheet(f"background:{GOLD};color:#000;font-weight:700;font-size:10px;padding:4px 8px;border-radius:10px;border:none;")

    def _on_dock_closed(self):
        self._dock_btn.setText("⇱ FLOAT")
        self._dock_btn.setStyleSheet(f"background:{PANEL};color:{GOLD};font-weight:700;font-size:10px;padding:4px 8px;border-radius:10px;border:1px solid {BORDER};")

    def _sync_dock_tbl(self):
        if self._dock_tbl is None: return
        if self._dock_win is None or self._dock_win.isHidden(): return
        from PyQt6.QtWidgets import QTableWidgetItem
        src = self._tbl; dst = self._dock_tbl
        dst.setRowCount(src.rowCount())
        for r in range(src.rowCount()):
            for c in range(src.columnCount()):
                it = src.item(r,c)
                if it:
                    ni = QTableWidgetItem(it.text())
                    ni.setForeground(it.foreground()); ni.setBackground(it.background())
                    ni.setFont(it.font()); ni.setTextAlignment(it.textAlignment())
                    ni.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    dst.setItem(r,c,ni)

    def _refresh_stats_cards(self):
        pos_list  = list(self._positions.values())
        principal = self._prin.value(); tgt_pct = self._tgt.value()
        tgt_rs    = principal * tgt_pct / 100
        open_pos  = [p for p in pos_list if p.is_open and not p.pending]
        closed_pos= [p for p in pos_list if not p.is_open]
        total_pnl    = sum(p.pnl for p in pos_list)
        realised_pnl = sum(
            p.pnl - _brok(p.actual_cost) * 2
            for p in closed_pos
        )
        deployed  = sum(p.actual_cost for p in open_pos)
        leftover  = principal + realised_pnl - deployed

        total_cost_ever = deployed + sum(p.actual_cost for p in closed_pos)
        pct_total = total_pnl / total_cost_ever * 100 if total_cost_ever > 0 else 0

        if open_pos:
            total_deployed = sum(p.actual_cost for p in open_pos)
            if total_deployed > 0:
                avg_gain = sum(p.pct * p.actual_cost for p in open_pos) / total_deployed
            else:
                avg_gain = sum(p.pct for p in open_pos) / len(open_pos)
        else:
            avg_gain = 0.0

        progress  = total_pnl / tgt_rs * 100 if tgt_rs > 0 else 0
        remaining = max(tgt_rs - total_pnl, 0)
        needed    = remaining / len(open_pos) if open_pos else 0
        needed_pct= needed / (deployed/len(open_pos)) * 100 if open_pos and deployed > 0 else 0
        pnl_col   = GOLD if pct_total >= tgt_pct else (GREEN if pct_total > 0 else RED)
        prog_col  = GOLD if progress >= 100 else (GREEN if progress >= 50 else (ORANGE if progress >= 25 else DIM))
        avg_col   = GREEN if avg_gain > 0 else (RED if avg_gain < 0 else DIM)
        lft_col   = GREEN if leftover >= principal else (RED if leftover < principal else DIM)
        self._lbl_deployed.setText(f"Rs {deployed:,.0f}  ({len(open_pos)}/{self._slots.value()})")
        self._lbl_leftover.setText(f"Rs {leftover:,.0f}")
        self._lbl_leftover.setStyleSheet(
            f"color:{lft_col};font-size:13px;font-weight:700;"
            f"font-family:'{MONO}';background:transparent;border:none;")
        self._lbl_pnl.setText(f"Rs {total_pnl:+,.2f}  ({pct_total:+.2f}% on deployed)")
        self._lbl_pnl.setStyleSheet(f"color:{pnl_col};font-size:13px;font-weight:700;font-family:'{MONO}';background:transparent;border:none;")
        self._lbl_progress.setText(f"{progress:.1f}%  need Rs {needed:,.0f} ({needed_pct:.1f}%/stk)")
        self._lbl_progress.setStyleSheet(f"color:{prog_col};font-size:11px;font-weight:700;font-family:'{MONO}';background:transparent;border:none;")
        self._lbl_avg_gain.setText(f"{avg_gain:+.2f}%  ({len(open_pos)} stocks, wt)")
        self._lbl_avg_gain.setStyleSheet(f"color:{avg_col};font-size:13px;font-weight:700;font-family:'{MONO}';background:transparent;border:none;")
        self._lbl_target.setText(f"{tgt_pct:.0f}%  =  Rs {tgt_rs:,.0f}")

    # ── alloc helpers ─────────────────────────────────────────────────────────
    def _allocs_for_rank(self, rank_1_based):
        allocs = _weighted_allocs(self._prin.value(), max(self._slots.value(),1), self.concentration)
        return allocs[min(rank_1_based-1, len(allocs)-1)]

    def _leftover_cash(self):
        deployed     = sum(p.actual_cost for p in self._positions.values() if p.is_open and not p.pending)
        realised_pnl = sum(
            p.pnl - _brok(p.actual_cost) * 2
            for p in self._positions.values() if not p.is_open
        )
        return self._prin.value() + realised_pnl - deployed

    # ── blink ─────────────────────────────────────────────────────────────────
    def _blink_tick(self):
        self._blink_on = not self._blink_on
        dead = [s for s,n in self._blink_count.items() if n <= 0]
        for s in dead: del self._blink_count[s]
        if self._blink_count:
            for s in self._blink_count: self._blink_count[s] -= 1
            self._paint_blink_rows()

    def _paint_blink_rows(self):
        plist = list(self._positions.values())
        for r, pos in enumerate(plist):
            if pos.symbol in self._blink_count:
                flash = (_bg(r) if not self._blink_on else (
                    QColor("#550000") if pos.breach_alerted else
                    QColor("#440000") if pos.w2_active else
                    QColor("#443300") if pos.v1_active else QColor("#003344")))
                for c in range(self._tbl.columnCount()):
                    it = self._tbl.item(r,c)
                    if it: it.setBackground(QBrush(flash))
        self._tbl.viewport().update()

    # ── add position ──────────────────────────────────────────────────────────
    def add(self, sym, ltp, vol_flow=None, alloc_rank=None,
            alloc_override=None, skip_dialog=False, direction="LONG"):
        if sym in self._positions and self._positions[sym].is_open: return
        open_count = sum(1 for p in self._positions.values() if p.is_open)
        if open_count >= self._slots.value(): return
        if ltp <= 0: return
        if alloc_rank is None: alloc_rank = open_count + 1
        default_alloc = alloc_override if alloc_override else self._allocs_for_rank(alloc_rank)

        # Find ind data for this sym if available
        all_rows = self._last_rows + self._last_loser_rows
        ind = next((r.get("ind",{}) for r in all_rows if r["symbol"] == sym), {})

        final_alloc   = default_alloc
        final_mode    = "Weighted" if self._alloc_mode.currentIndex() == 0 else "Equal"
        final_price   = ltp   # may be overridden by dialog

        if not skip_dialog and self._confirm_popup.isChecked() and is_safety_on():
            dlg = TradeConfirmDialog(
                mode="ENTRY", sym=sym, ltp=ltp, ind=ind,
                default_alloc=default_alloc, principal=self._prin.value(),
                leftover_cash=self._leftover_cash(),
                alloc_mode_idx=self._alloc_mode.currentIndex(),
                parent=self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            final_alloc, final_mode, final_price, _ = dlg.result_values()
            # Sync alloc mode back to PMS bar if user changed it in dialog
            target_idx = 0 if final_mode == "Weighted" else 1
            if self._alloc_mode.currentIndex() != target_idx:
                self._alloc_mode.blockSignals(True)
                self._alloc_mode.setCurrentIndex(target_idx)
                self._alloc_mode.blockSignals(False)
                self._lbl_split.setText(_alloc_preview(
                    self._prin.value(), max(self._slots.value(),1), self.concentration))

        alloc = final_alloc
        entry_price = final_price if final_price > 0 else ltp
        if self._auto_qty.isChecked():
            qty = math.floor(alloc / entry_price)
            if qty <= 0:
                QMessageBox.warning(self,"Budget",
                    f"Slot Rs{alloc:.0f} too small for {sym} @ {entry_price:.2f}"); return
            actual_cost = qty * entry_price; leftover_slot = alloc - actual_cost
        else:
            qty = alloc / entry_price; actual_cost = alloc; leftover_slot = 0.0

        et = datetime.datetime.now().strftime("%H:%M:%S")
        pos = Position(symbol=sym, entry=entry_price, current=ltp,
                       qty=qty, alloc=alloc, actual_cost=actual_cost,
                       leftover=leftover_slot, entry_time=et,
                       entry_vf=vol_flow, alloc_rank=alloc_rank,
                       direction=direction, pending=False)
        pos.price_history = [ltp]
        _entry_log_write(sym, entry_price, direction, ind)

        if not is_safety_on():
            try:
                avail = get_available_funds()
                if actual_cost > avail + 1:  # +1 rupee slack for rounding
                    QMessageBox.warning(self, "Insufficient Funds",
                        f"{sym}: order needs ~Rs{actual_cost:,.0f} but only "
                        f"Rs{avail:,.0f} is available in your broker account.\n\n"
                        f"Order was NOT sent. Reduce qty/allocation or add funds.")
                    return
            except Exception as e:
                log.warning(f"Funds pre-check skipped for {sym}: {e}")

        result = broker_buy(sym, qty=qty, price=entry_price, order_type="MARKET",
                            direction=direction)
        if result.get("status") == "CANCELLED":
            err = result.get("error", "")
            if err.startswith("BUSY:"):
                QMessageBox.warning(self, "Order Queue Busy", err[5:].strip())
                self._refresh()
                return
            QMessageBox.warning(self, "Order Not Confirmed",
                f"{sym} BUY was not confirmed within the time window.\n\n"
                f"This usually means: insufficient funds/margin flagged by "
                f"broker, the order was rejected/closed on the broker's, or the "
                f"tab was closed before confirming. No position was recorded — "
                f"check your order log if unsure.")
            self._refresh()
            return
        self._positions[sym] = pos
        if result.get("status") == "FAILED":
            QMessageBox.warning(self, "Order Failed",
                f"{sym} BUY order was rejected by the broker:\n{result.get('error','unknown error')}\n\n"
                f"The position is still recorded locally — double-check your "
                f"broker terminal before trusting the fill.")
        elif result.get("average_price"):
            fill_px = result["average_price"]
            pos.entry = fill_px
            pos.actual_cost = pos.qty * fill_px
            pos.leftover = alloc - pos.actual_cost
        # ──────────────────────────────────────────────────────────────────────

        _play_confirm()
        self._refresh()
        self._save_session()

    def confirm(self, sym):
        pos = self._positions.get(sym)
        if pos and pos.pending:
            pos.pending = False; pos.entry = pos.current
            pos.entry_time = datetime.datetime.now().strftime("%H:%M:%S")
            pos.pct = 0.0; pos.peak_pct = 0.0
            _play_confirm()
        self._refresh()

    def reject(self, sym):
        if sym in self._positions and self._positions[sym].pending:
            del self._positions[sym]
        self._refresh()

    def exit_pos(self, sym, skip_dialog=False):
        pos = self._positions.get(sym)
        if not pos or not pos.is_open: return
        ind = {}
        all_rows = self._last_rows + self._last_loser_rows
        ind = next((r.get("ind",{}) for r in all_rows if r["symbol"] == sym), {})

        sell_qty = pos.qty   # default: exit all
        if not skip_dialog and self._confirm_popup.isChecked() and is_safety_on():
            dlg = TradeConfirmDialog(
                mode="EXIT", sym=sym, ltp=pos.current, ind=ind, pos=pos, parent=self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            _, _, _, chosen_qty = dlg.result_values()
            if chosen_qty and chosen_qty > 0:
                sell_qty = chosen_qty

        sell_qty = min(float(sell_qty), pos.qty)
        is_partial = sell_qty < pos.qty

        # ── BROKER HOOK: SELL ──────────────────────────────────────────────────
        result = broker_sell(sym, qty=sell_qty, price=pos.current, order_type="MARKET",
                             direction=pos.direction, reason="partial exit" if is_partial else "manual exit")
        if result.get("status") == "CANCELLED":
            err = result.get("error", "")
            if err.startswith("BUSY:"):
                QMessageBox.warning(self, "Order Queue Busy", err[5:].strip())
                self._refresh()
                return
            # Never confirmed in broker — nothing was sold, leave position as-is.
            QMessageBox.warning(self, "Order Not Confirmed",
                f"{sym} SELL was not confirmed within the time window.\n\n"
                f"This usually means the order was rejected/closed on the broker's "
                f"page, or the tab was closed before confirming. Position is "
                f"unchanged here — check your order log if unsure.")
            self._refresh()
            return
        if result.get("status") == "FAILED":
            QMessageBox.warning(self, "Order Failed",
                f"{sym} SELL order was rejected by the broker:\n{result.get('error','unknown error')}\n\n"
                f"The position is still recorded locally as exited — double-check "
                f"your broker terminal before trusting the fill.")
        close_price = result.get("average_price") or pos.current
        # ──────────────────────────────────────────────────────────────────────

        if is_partial:
            # Partial exit: reduce qty and adjust cost basis proportionally
            frac = sell_qty / pos.qty
            pos.qty      -= sell_qty
            pos.actual_cost *= (1 - frac)
            pos.alloc       *= (1 - frac)
            # Record partial exit in sl_events for audit trail
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            pos.sl_events.append((ts, "partial_exit", close_price, round(pos.pct, 2)))
            pos._update_sl_log()
        else:
            # Full close
            rank = pos.alloc_rank
            pos.close(close_price)
            self._run_auto_pstop_cycle(
                [(rank, sym, "MANUAL EXIT")], self._last_rows, self._last_loser_rows)

        _play_exit()
        self._refresh()
        self._save_session()

    def exit_all(self):
        failures = []
        cancelled = []
        for pos in self._positions.values():
            if pos.is_open:
                result = broker_sell(pos.symbol, qty=pos.qty, price=pos.current,
                                     order_type="MARKET", direction=pos.direction,
                                     reason="R_ALL")
                if result.get("status") == "CANCELLED":
                    cancelled.append(pos.symbol)
                    continue
                if result.get("status") == "FAILED":
                    failures.append(f"{pos.symbol}: {result.get('error','unknown error')}")
                pos.close(result.get("average_price") or pos.current)
        _play_exit(); self._refresh()
        self._save_session()
        if failures:
            QMessageBox.warning(self, "Some Orders Failed",
                "R_ALL: the following sell orders were rejected by the broker "
                "(positions are still recorded locally as exited — verify in "
                "your broker terminal):\n\n" + "\n".join(failures))
        if cancelled:
            QMessageBox.warning(self, "Some Orders Cancelled",
                "R_ALL: these were never confirmed in broker, so they're still "
                "open — retry them individually:\n\n" + "\n".join(cancelled))

    # ── price update every scan ───────────────────────────────────────────────
    def update_prices(self, rows, loser_rows=None):
        if getattr(self, "_in_auto_exit_confirm", False):
            return
        self._last_rows = rows
        if loser_rows: self._last_loser_rows = loser_rows
        all_rows = rows + (loser_rows or [])
        prices = {r["symbol"]: r["ltp"] for r in all_rows if r.get("ltp")}
        inds   = {r["symbol"]: r.get("ind",{}) for r in all_rows}
        sl_pct = self._sl.value(); bb1 = self._bb1.value()
        to_exit = []

        for sym, pos in self._positions.items():
            if not pos.is_open: continue
            if sym in self._backfill_q:
                _backfill_warn_log(sym, self._backfill_q.pop(sym))
            if sym in prices:
                pos.update(float(prices[sym]))
                if not hasattr(pos,'price_history'): pos.price_history = [pos.entry]
                pos.price_history.append(pos.current)
                if len(pos.price_history) > 60: pos.price_history = pos.price_history[-60:]
                ind = inds.get(sym,{})
                pos.px_delta = ind.get("px_delta"); pos.vol_flow = ind.get("vol_flow"); pos.last_low = ind.get("last_low")
                liq = ind.get("liquidity_note","")
                if liq and not pos.liquidity: pos.liquidity = liq
                if not pos.pending:
                    is_short = getattr(pos,"direction","LONG") == "SHORT"
                    if is_short:
                        signals = pos.check_sl(
                            sl_pct, pos.px_delta, bb1,
                            higher_lows=ind.get("higher_lows"),
                            hl_warn_enabled=False,           # HL doesn't apply to shorts
                            lower_highs=ind.get("lower_highs"),
                            lh_warn_enabled=True)            # LH is the SL for shorts
                    else:
                        signals = pos.check_sl(
                            sl_pct, pos.px_delta, bb1,
                            higher_lows=ind.get("higher_lows"),
                            hl_warn_enabled=self._hl_warn_on,
                            lower_highs=ind.get("lower_highs"),
                            lh_warn_enabled=self._lh_warn_on)
                    for sig in signals:
                        _log_warn(sym, sig, pos.current, pos.entry,
                                  pos.pct, pos.peak_pct, pos.vol_flow, pos.px_delta)
                        self._backfill_q[sym] = pos.current
                        _log_sl_event(sym, sig, pos.current, pos.trail_stop, pos.pct, pos.peak_pct)
                        if sig == "breach":
                            _play(_SND_BREACH); self._blink_count[sym] = 14
                        elif sig in ("w2","bb1"):
                            _play(_SND_WARNING); self._blink_count[sym] = 10
                        elif sig in ("v1","hl","lh","avg_sl"):
                            _play(_SND_WARNING); self._blink_count[sym] = 7
                        # Auto-exit conditions
                        if sig == "hl" and self._hl_auto_exit:
                            to_exit.append((sym,"HL AUTO EXIT"))
                        if sig == "lh" and (self._lh_auto_exit or is_short):
                            to_exit.append((sym,"LH AUTO EXIT"))
                        if sig == "breach":
                            to_exit.append((sym,"SL BREACH"))

        # Feature 13 — avg SL cutoff
        open_gains = [p.pct for p in self._positions.values() if p.is_open and not p.pending]
        if len(open_gains) >= 2:
            avg_g = sum(open_gains)/len(open_gains)
            for sym2, pos2 in self._positions.items():
                if pos2.is_open and not pos2.pending:
                    drag = (avg_g - pos2.pct) >= sl_pct
                    if drag and not pos2.avg_sl_warn:
                        pos2.avg_sl_warn = True
                        _log_warn(sym2,"avg_sl",pos2.current,pos2.entry,
                                  pos2.pct,pos2.peak_pct,pos2.vol_flow,pos2.px_delta)
                        _play(_SND_WARNING); self._blink_count[sym2] = 7
                        if self._avg_sl_cutoff: to_exit.append((sym2,"AVG SL"))
                    elif not drag: pos2.avg_sl_warn = False
                    pos2._update_sl_log()

        seen = set()
        freed_ranks = []   # (alloc_rank, exit_reason) for stocks closed by auto-exit this scan
        for sym, reason in to_exit:
            if sym in seen: continue
            seen.add(sym)
            pos = self._positions.get(sym)
            if not (pos and pos.is_open):
                continue

            if reason == "SL BREACH":
                result = broker_sell(sym, qty=pos.qty, price=pos.current,
                                    order_type="MARKET", direction=pos.direction, reason=reason)
                if result.get("status") == "CANCELLED":
                    if not hasattr(self, "_auto_pstop_trail"):
                        self._auto_pstop_trail = []
                    self._auto_pstop_trail.append(
                        f"⚠ AUTO SELL NEVER CONFIRMED: {sym} ({reason}) — still open, retry manually")
                    self._in_auto_exit_confirm = True
                    QMessageBox.warning(self, "Auto-Exit Not Confirmed",
                        f"{sym} auto SL BREACH sell was not confirmed within the "
                        f"time window (rejected/closed on the broker's, or the "
                        f"tab wasn't confirmed in time).\n\n"
                        f"Position is STILL OPEN — please check it manually.")
                    self._in_auto_exit_confirm = False
                    continue
                if result.get("status") == "FAILED":
                    if not hasattr(self, "_auto_pstop_trail"):
                        self._auto_pstop_trail = []
                    self._auto_pstop_trail.append(
                        f"⚠ AUTO SELL FAILED: {sym} ({reason}) — {result.get('error','?')}")
                    self._in_auto_exit_confirm = True
                    QMessageBox.warning(self, "Auto-Exit Rejected",
                        f"{sym} auto SL BREACH sell was rejected by the broker:\n"
                        f"{result.get('error','unknown error')}\n\n"
                        f"The position is recorded locally as exited — double-check "
                        f"your order log before trusting this.")
                    self._in_auto_exit_confirm = False
                rank = pos.alloc_rank
                pos.close(result.get("average_price") or pos.current); _play_exit(); self._blink_count[sym] = 6
                freed_ranks.append((rank, sym, reason))
                continue

            result = broker_sell(sym, qty=pos.qty, price=pos.current,
                                order_type="MARKET", direction=pos.direction, reason=reason)
            if result.get("status") == "CANCELLED":
                if not hasattr(self, "_auto_pstop_trail"):
                    self._auto_pstop_trail = []
                self._auto_pstop_trail.append(
                    f"⚠ AUTO SELL NEVER CONFIRMED: {sym} ({reason}) — still open, retry manually")
                self._in_auto_exit_confirm = True
                QMessageBox.warning(self, "Auto-Exit Not Confirmed",
                    f"{sym} auto {reason} sell was not confirmed within the "
                    f"time window (rejected/closed on the broker's, or the "
                    f"tab wasn't confirmed in time).\n\n"
                    f"Position is STILL OPEN — please check it manually.")
                self._in_auto_exit_confirm = False
                continue
            if result.get("status") == "FAILED":
                if not hasattr(self, "_auto_pstop_trail"):
                    self._auto_pstop_trail = []
                self._auto_pstop_trail.append(
                    f"⚠ AUTO SELL FAILED: {sym} ({reason}) — {result.get('error','?')}")
                self._in_auto_exit_confirm = True
                QMessageBox.warning(self, "Auto-Exit Rejected",
                    f"{sym} auto {reason} sell was rejected by the broker:\n"
                    f"{result.get('error','unknown error')}\n\n"
                    f"The position is recorded locally as exited — double-check "
                    f"your order log before trusting this.")
                self._in_auto_exit_confirm = False
            rank = pos.alloc_rank
            pos.close(result.get("average_price") or pos.current); _play_exit(); self._blink_count[sym] = 6
            freed_ranks.append((rank, sym, reason))

        if freed_ranks:
            self._run_auto_pstop_cycle(freed_ranks, rows, loser_rows)

        try:
            held_now = {p.symbol for p in self._positions.values() if p.is_open and not p.pending}
            all_rows_screen = rows + (loser_rows or [])
            scores_screen = _combined_score(all_rows_screen)
            screen_cand = self._best_candidate(held_now, all_rows_screen, scores_screen,
                                                min_px=self._min_px_filter if not self._auto_pstop_raw else 0.0)
            self._last_screener_candidate = screen_cand
        except Exception:
            self._last_screener_candidate = None

        self._refresh()
        self._save_session()

    def _run_auto_pstop_cycle(self, freed_ranks, rows, loser_rows):
        """
        Shared auto-cycle entry logic. Called from two places:
          1. update_prices() — after an automatic exit (HL/LH/breach/avg-sl)
          2. exit_pos()       — after a manual full exit (EXIT / EXIT SEL)
        so AUTO P_STOP behaves identically regardless of how the exit
        happened, including firing instantly on a manual exit rather than
        waiting for the next scan's auto-exit pass.

        freed_ranks: list of (alloc_rank, exited_symbol, reason_str)
        """
        if not self._auto_pstop_on or not freed_ranks:
            return
        now_ts = datetime.datetime.now().timestamp()
        held = {p.symbol for p in self._positions.values() if p.is_open and not p.pending}
        all_rows_now = rows + (loser_rows or [])
        scores_now = _combined_score(all_rows_now)
        for rank, exited_sym, reason in freed_ranks:
            # Apply cooldown: mark this rank slot as blocked until now+cooldown
            if self._cooldown_secs > 0:
                unlock_at = self._cooldown_until.get(rank, 0)
                if now_ts < unlock_at:
                    continue   # still cooling down — skip auto re-entry this scan
                self._cooldown_until[rank] = now_ts + self._cooldown_secs
            if self._auto_pstop_raw:
                cand = self._best_candidate(held, all_rows_now, scores_now)
            else:
                cand = self._best_candidate(held, all_rows_now, scores_now,
                                             min_px=self._min_px_filter)
            if cand is None: continue
            c_ltp = cand.get("ltp") or 0
            if c_ltp <= 0: continue
            c_ind = cand.get("ind", {})
            self.add(cand["symbol"], c_ltp, vol_flow=c_ind.get("vol_flow"),
                      alloc_rank=rank, skip_dialog=True)
            held.add(cand["symbol"])
            self._append_auto_pstop_log(exited_sym, cand["symbol"], reason)

    def _append_auto_pstop_log(self, exited_sym, entered_sym, reason):
        """Hook for any module listening to auto-cycle events — currently just
        keeps a tiny in-memory trail; AutoPanel reads _positions directly so
        no signal wiring is required for the table to reflect the change."""
        if not hasattr(self, "_auto_pstop_trail"):
            self._auto_pstop_trail = []
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._auto_pstop_trail.append(f"{ts}  AUTO P_STOP: {exited_sym} ({reason}) → {entered_sym}")
        if len(self._auto_pstop_trail) > 100:
            self._auto_pstop_trail = self._auto_pstop_trail[-100:]

    def _best_candidate(self, exclude_syms, rows, scores, min_px=0.0):
        """
        Replacement candidate: up 1%+, above VWAP, EMA trend up, RSI below 70,
        preferring healthy volume flow; sorted by price gain (highest first).
        Falls back to any positive volume flow if nothing matches.
        min_px — minimum per_change required, 0 = no filter.
        """
        def _qual(r, require_sust_vf):
            if r["symbol"] in exclude_syms: return False
            if r["per_change"] < 1.0: return False
            if min_px > 0 and r["per_change"] < min_px: return False
            ind = r.get("ind", {})
            if ind.get("above_vwap") is not True: return False
            if ind.get("ema_trend") != 1: return False
            rsi = ind.get("rsi")
            if rsi is None or rsi >= 70: return False
            if require_sust_vf:
                vf = ind.get("vol_flow")
                if vf is None or not (50.0 <= abs(vf) <= 2500.0): return False
            return True

        cands = [r for r in rows if _qual(r, require_sust_vf=True)]
        if not cands:
            cands = [r for r in rows if _qual(r, require_sust_vf=False)]
        if not cands:
            return None
        # Sort by per_change (highest px gain) descending
        cands.sort(key=lambda r: -r["per_change"])
        return cands[0]

    # ── context menu ──────────────────────────────────────────────────────────
    def _menu(self, mpos):
        row = self._tbl.rowAt(mpos.y())
        if row < 0: return
        sym_item = self._tbl.item(row, 0)
        if not sym_item: return
        sym = sym_item.text().strip().lstrip("● ").rstrip(" ")
        p   = self._positions.get(sym)
        if not p: return
        menu   = QMenu(self)
        scores = _combined_score(self._last_rows)
        held   = {s for s,pos in self._positions.items() if pos.is_open}

        if p.pending:
            ak = menu.addAction(f"✓ CONFIRM  {sym}  @ {p.current:.2f}")
            ar = menu.addAction(f"✗ REJECT  {sym}")
            ch = menu.exec(self._tbl.viewport().mapToGlobal(mpos))
            if ch == ak: self.confirm(sym)
            elif ch == ar: self.reject(sym)
            return

        if not p.is_open: return

        # Individual exit (with full popup)
        ae = menu.addAction(f"Exit  {sym}  @ {p.current:.2f}")

        best = self._best_candidate(held, self._last_rows, scores)
        ap   = (menu.addAction(
                    f"⚡ P_STOP → {best['symbol']}  @ {best.get('ltp',0):.2f}"
                    f"  score {scores.get(best['symbol'],0):.0f}")
                if best else None)

        # R_ALL
        ara = menu.addAction("🛑 R_ALL — exit all")

        # Leftover cash
        leftover = self._leftover_cash()
        a_lo = a_split = None
        if leftover > 100:
            a_lo = menu.addAction(f"💰 Add leftover Rs{leftover:,.0f} → {sym}")
            open_syms = [s for s,pos in self._positions.items() if pos.is_open and not pos.pending]
            if len(open_syms) > 1:
                a_split = menu.addAction(f"💰 Split Rs{leftover:,.0f} across {len(open_syms)} stocks")

        ch = menu.exec(self._tbl.viewport().mapToGlobal(mpos))

        if ch == ae:
            # Full exit popup with PnL, brokerage, liquidity
            self.exit_pos(sym, skip_dialog=False)

        elif ap and ch == ap:
            ltp_new = best.get("ltp") or 0
            if ltp_new <= 0: return
            replace_ind = best.get("ind",{})
            dlg = TradeConfirmDialog(
                mode="PSTOP", sym=sym, ltp=p.current, ind={}, pos=p,
                default_alloc=p.alloc, principal=self._prin.value(),
                leftover_cash=self._leftover_cash(),
                alloc_mode_idx=self._alloc_mode.currentIndex(),
                replace_sym=best["symbol"], replace_ltp=ltp_new,
                replace_ind=replace_ind, parent=self)
            if dlg.exec() != QDialog.DialogCode.Accepted: return
            # ── BROKER HOOK: P_STOP SELL ───────────────────────────────────────
            result = broker_sell(sym, qty=p.qty, price=p.current, order_type="MARKET",
                                 direction=p.direction, reason="P_STOP replace")
            if result.get("status") == "CANCELLED":
                QMessageBox.information(self, "Order Cancelled",
                    f"{sym} P_STOP SELL was never confirmed in broker — position "
                    f"is still open. Not entering the replacement.")
                return
            if result.get("status") == "FAILED":
                QMessageBox.warning(self, "Order Failed",
                    f"{sym} P_STOP SELL was rejected by the broker:\n{result.get('error','unknown error')}\n\n"
                    f"Stopping here — NOT entering the replacement, since the "
                    f"original position may still be open at the broker.")
                return
            # ──────────────────────────────────────────────────────────────────
            rank = p.alloc_rank; p.close(result.get("average_price") or p.current); _play_exit()
            self.add(best["symbol"], ltp_new,
                     vol_flow=replace_ind.get("vol_flow"),
                     alloc_rank=rank, skip_dialog=True)
            # ── BROKER HOOK: P_STOP BUY handled inside add() ──────────────────

        elif ch == ara:
            open_pos = [pos for pos in self._positions.values() if pos.is_open and not pos.pending]
            self.exit_all()

        elif a_lo and ch == a_lo:
            self._leftover_dialog(sym, leftover)

        elif a_split and ch == a_split:
            open_syms = [s for s,pos in self._positions.items() if pos.is_open and not pos.pending]
            self._leftover_split_dialog(open_syms, leftover)

    def _apply_leftover_to(self, sym, cash):
        pos = self._positions.get(sym)
        if not pos or not pos.is_open or pos.pending or pos.current <= 0: return
        extra = math.floor(cash / pos.current)
        if extra <= 0: return
        spent = extra * pos.current; new_qty = pos.qty + extra
        new_cost = pos.actual_cost + spent
        pos.entry = new_cost / new_qty; pos.qty = new_qty
        pos.actual_cost = new_cost; pos.alloc += spent
        pos.leftover = max(0, pos.leftover - spent)
        self._refresh()

    def _leftover_dialog(self, sym, available):
        """Popup to add leftover cash to a single position."""
        pos = self._positions.get(sym)
        if not pos or not pos.is_open or pos.current <= 0: return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Add Leftover → {sym}")
        dlg.setMinimumWidth(360)
        dlg.setStyleSheet(
            f"QDialog{{background:{PANEL};color:{TEXT};}}"
            f"QLabel{{color:{TEXT};font-size:12px;background:transparent;}}"
            f"QSpinBox{{background:{BG};color:{TEXT};"
            f"border:1px solid {BORDER};border-radius:6px;padding:4px 8px;font-size:12px;}}"
            f"QPushButton{{background:{HEADER};color:{GOLD};"
            f"border:1px solid {BORDER};padding:6px 18px;"
            f"border-radius:10px;font-weight:700;font-size:11px;}}")
        lo = QVBoxLayout(dlg); lo.setSpacing(8); lo.setContentsMargins(16,14,16,14)
        lo.addWidget(QLabel(f"<b>Add cash to {sym}</b>  (current ₹{pos.current:.2f})"))
        lo.addWidget(QLabel(f"Available leftover:  ₹{available:,.2f}"))
        row = QHBoxLayout()
        row.addWidget(QLabel("Amount ₹:"))
        amt = QSpinBox(); amt.setRange(100, max(int(available), 100))
        amt.setValue(int(available)); amt.setSingleStep(500); amt.setMaximumWidth(130)
        row.addWidget(amt); row.addStretch(); lo.addLayout(row)
        info = QLabel(""); info.setStyleSheet(f"color:{CYAN};font-size:11px;font-family:'{MONO}';")
        lo.addWidget(info)
        warn = QLabel(""); warn.setStyleSheet(f"color:{RED};font-size:11px;font-weight:700;")
        lo.addWidget(warn)
        from PyQt6.QtWidgets import QDialogButtonBox
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept); btns.rejected.connect(dlg.reject)
        lo.addWidget(btns)

        def _update():
            a = amt.value(); price = pos.current
            qty = math.floor(a / price) if price > 0 else 0
            cost = qty * price
            if a > available:
                warn.setText(f"⚠ Insufficient funds  (available ₹{available:,.0f})")
                btns.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            else:
                warn.setText("")
                btns.button(QDialogButtonBox.StandardButton.Ok).setEnabled(qty > 0)
            info.setText(f"Buys {qty} shares  ×  ₹{price:.2f}  =  ₹{cost:,.2f}  (leftover ₹{a-cost:,.2f})")

        amt.valueChanged.connect(_update); _update()
        if dlg.exec() == QDialog.DialogCode.Accepted:
            a = amt.value()
            if a <= available:
                self._apply_leftover_to(sym, a)

    def _leftover_split_dialog(self, open_syms, available):
        """Popup to distribute leftover across all open positions."""
        n = len(open_syms)
        if n == 0: return
        per = available / n
        # Check if each slot can buy at least 1 share
        insufficient = []
        for s in open_syms:
            p = self._positions.get(s)
            if p and p.current > 0 and math.floor(per / p.current) <= 0:
                insufficient.append(s)
        msg = (f"Distribute ₹{available:,.0f} across {n} positions\n"
               f"≈ ₹{per:,.0f} each")
        if insufficient:
            msg += f"\n\n⚠  Insufficient for: {', '.join(insufficient)}\n(those positions will be skipped)"
        if not _confirm_dialog(self, "Distribute Leftover", msg):
            return
        for s in open_syms:
            p = self._positions.get(s)
            if not p or p.current <= 0: continue
            if math.floor(per / p.current) <= 0: continue
            self._apply_leftover_to(s, per)

    # ── table refresh ─────────────────────────────────────────────────────────
    def _refresh(self):
        # Update mini-screener label in the settings bar
        try:
            txt = self.screener_text
            if txt:
                self._screener_lbl.setText(f"SCREENER:  {txt}")
                self._screener_lbl.setStyleSheet(f"color:{GREEN};font-size:10px;font-weight:700;font-family:'{MONO}';background:transparent;")
            else:
                self._screener_lbl.setText("SCREENER:  — no candidate —")
                self._screener_lbl.setStyleSheet(f"color:{DIM};font-size:10px;font-family:'{MONO}';background:transparent;")
        except Exception:
            pass
        plist = list(self._positions.values())
        self._tbl.setRowCount(len(plist))
        for r, pos in enumerate(plist):
            bg = (QColor("#3A0505" if r%2 else "#440606") if pos.breach_alerted and pos.is_open else
                  QColor("#2A0808" if r%2 else "#330A0A") if pos.w2_active and pos.is_open else
                  QColor("#2A2100" if r%2 else "#332900") if pos.v1_active and pos.is_open else
                  QColor("#1A0A2E" if r%2 else "#1E0D35") if pos.pending else _bg(r))
            is_short = getattr(pos,"direction","LONG") == "SHORT"
            pc   = (GREEN if pos.pct >= 0 else RED) if (pos.is_open and not pos.pending) else (MAG if pos.pending else DIM)
            pk_c = GREEN if pos.peak_pct > 0 else DIM
            liq_c = RED if "ILLIQ" in (pos.liquidity or "") else (GREEN if pos.liquidity == "OK" else DIM)
            sl_s,sl_c = (
                (f"BREACH {pos.trail_stop:.2f}", RED) if pos.breach_alerted else
                (f"W2@{pos.w2_price:.2f}", RED) if pos.w2_active and pos.w2_price > 0 else
                (f"V1@{pos.v1_price:.2f}", ORANGE) if pos.v1_active and pos.v1_price > 0 else
                (f"TIGHT {pos.trail_stop:.2f}", RED) if pos.trail_tight and pos.is_open else
                (f"TRAIL {pos.trail_stop:.2f}", ORANGE) if pos.trail_active and pos.is_open else
                (f"SL {pos.trail_stop:.2f}", DIM) if pos.is_open else ("—", DIM))
            vf_c = GREEN if (pos.vol_flow and pos.vol_flow > 0) else (RED if (pos.vol_flow and pos.vol_flow < 0) else DIM)
            tip  = pos.last_warn_tip or ""
            qty_s = f"{pos.qty:.0f}" if pos.qty >= 1 else f"{pos.qty:.2f}"
            blink_active = pos.symbol in self._blink_count
            is_illiquid = "ILLIQ" in (pos.liquidity or "")
            sym_display  = ("● " if blink_active and self._blink_on else "") + pos.symbol
            sym_color = RED if blink_active else (MAG if pos.pending else (ROSE if is_short else (GOLD if pos.is_open else DIM)))
            rank_s = f"#{pos.alloc_rank} (0)" if is_illiquid else f"#{pos.alloc_rank}"
            rank_c = RED if is_illiquid else CYAN
            dir_s = "SHORT" if is_short else "LONG"
            dir_c = ROSE if is_short else GREEN
            struct_s = "—"; struct_c = DIM; near_low = False
            ll = getattr(pos, "last_low", None)
            if pos.is_open and ll and ll > 0 and pos.current and pos.current > 0:
                mig_pct = (pos.current - ll) / ll * 100
                near_low = mig_pct < 0.20   # within 0.20% of the low — migration warning
                if mig_pct >= 0.5:
                    struct_s = f"▲{mig_pct:.2f}%>L"; struct_c = GREEN
                elif mig_pct >= 0.20:
                    struct_s = f"▲{mig_pct:.2f}%>L"; struct_c = ORANGE
                else:
                    struct_s = f"⚠{mig_pct:.2f}%>L"; struct_c = RED

            # Purple row background when price migrating toward low (override normal bg)
            if near_low and pos.is_open and not pos.breach_alerted and not pos.w2_active:
                bg = QColor("#2A0A3A" if r%2 else "#330D44")  # purple migration warning

            cells = [
                _i(sym_display, color=sym_color, bold=True),
                _i(dir_s, color=dir_c, bold=True),
                _i(rank_s, color=rank_c),
                _i(f"{pos.entry:.2f}", color=TEXT),
                _i(f"{pos.current:.2f}" if pos.current else "—", color=pc),
                _i(qty_s, color=CYAN),
                _i(f"Rs{pos.alloc:,.0f}", color=DIM),
                _i(f"{pos.pct:+.2f}%", color=pc, bold=True),
                _i(f"{pos.peak_pct:+.2f}%", color=pk_c, bold=True),
                _i(f"Rs{pos.pnl:+.2f}", color=pc),
                _i(f"{pos.vol_flow:+.0f}K" if pos.vol_flow is not None else "—", color=vf_c),
                _i(struct_s, color=struct_c, bold=(struct_c == RED)),
                _i(pos.liquidity or "—", color=liq_c),
                _i(sl_s, color=sl_c, bold=(pos.w2_active or pos.trail_tight or pos.breach_alerted), tooltip=tip),
                _i(pos.sl_log, color=RED if pos.w2_count>0 else (ORANGE if pos.v1_count>0 else DIM),
                   bold=pos.w2_count>0, tooltip=tip),
            ]
            for c,it in enumerate(cells): it.setBackground(QBrush(bg)); self._tbl.setItem(r,c,it)
        self._refresh_stats_cards()
        self._sync_dock_tbl()



class AutoPanel(QWidget):
    r_all_sig = pyqtSignal()

    def __init__(self, engine, pms: PMSManual):
        super().__init__()
        self._engine          = engine   # kept for compatibility, no longer used for logic
        self._pms             = pms
        self._last_rows       = []
        self._last_loser_rows = []
        self._log_win         = None

        root = QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(0)

        # ── Top bar ───────────────────────────────────────────────────────────
        top = QWidget()
        top.setStyleSheet(
            f"background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            f"stop:0 #131828,stop:1 {HEADER});"
            f"border-top:2px solid {GOLD};")
        tlo = QHBoxLayout(top); tlo.setContentsMargins(10,5,10,5); tlo.setSpacing(7)

        title = QLabel("  PMS MONITOR")
        title.setStyleSheet(f"color:{GOLD};font-size:10px;font-weight:700;letter-spacing:1.5px;")
        tlo.addWidget(title)
        tlo.addWidget(_divider_v())

        def _cbtn(text, bg, fg="#fff", w=80):
            b = QPushButton(text); b.setMaximumWidth(w)
            b.setStyleSheet(f"background:{bg};color:{fg};font-weight:700;font-size:10px;"
                            f"padding:4px 10px;border-radius:10px;border:none;"); return b

        self._exit_btn = _cbtn("🚪 EXIT SEL", RED, "#fff", 86)
        self._pit_btn  = _cbtn("⚡ P_STOP",  ORANGE, "#fff", 82)
        self._rall_btn = _cbtn("🛑 R_ALL",  "#2A2A3A", TEXT, 76)
        self._rall_btn.setStyleSheet(
            f"background:#2A2A3A;color:{TEXT};font-weight:700;font-size:10px;"
            f"padding:4px 10px;border-radius:10px;border:1px solid {BORDER};")
        self._save_btn    = _cbtn("💾 LOG", CYAN, "#000", 66)
        self._log_btn     = _cbtn("⇱ PORTFOLIO", GOLD, "#000", 90)
        self._orders_btn  = _cbtn("📋 ORDERS", STEEL, "#fff", 80)

        self._exit_btn.clicked.connect(self._do_exit_selected)
        self._pit_btn.clicked.connect(self._do_pitstop)
        self._rall_btn.clicked.connect(self._do_r_all)
        self._save_btn.clicked.connect(self._quick_save)
        self._log_btn.clicked.connect(self._toggle_portfolio_popout)
        self._orders_btn.clicked.connect(self._open_orderbook)

        for b in [self._exit_btn, self._pit_btn, self._rall_btn, self._save_btn, self._log_btn, self._orders_btn]:
            tlo.addWidget(b)

        tlo.addWidget(_divider_v())

        def _klbl(t):
            l = QLabel(t); l.setStyleSheet(f"color:{DIM};font-size:10px;background:transparent;"); return l
        tlo.addWidget(_klbl("Cycle%:"))
        self._cycle_tgt = QDoubleSpinBox()
        self._cycle_tgt.setRange(0.5,50); self._cycle_tgt.setValue(3.0); self._cycle_tgt.setDecimals(1)
        self._cycle_tgt.setMaximumWidth(55)
        self._cycle_tgt.setStyleSheet(
            f"background:{PANEL};border:1px solid {BORDER};border-radius:5px;"
            f"padding:2px 4px;font-size:11px;color:{TEXT};font-family:'{MONO}';")
        tlo.addWidget(self._cycle_tgt)
        tlo.addWidget(_divider_v())

        self._stat_pnl = QLabel("—")
        self._stat_pnl.setStyleSheet(f"color:{GOLD};font-size:11px;font-weight:700;font-family:'{MONO}';")
        tlo.addWidget(self._stat_pnl)
        self._stat_avg = QLabel("—")
        self._stat_avg.setStyleSheet(f"color:{CYAN};font-size:10px;font-family:'{MONO}';")
        tlo.addWidget(self._stat_avg)
        tlo.addStretch()
        root.addWidget(top)

        # ── Portfolio monitor (replaces activity log) ─────────────────────────
        content = QWidget(); content.setStyleSheet(f"background:{PANEL};")
        cl = QVBoxLayout(content); cl.setContentsMargins(6,3,6,3); cl.setSpacing(2)
        # Compact portfolio table
        MON_H = ["SYMBOL","DIR","ENTRY","CURRENT","% GAIN","PEAK%","PnL Rs","STRUCTURE","SL/TRAIL","SL LOG","⚡"]
        self._mon_tbl = QTableWidget(0, len(MON_H))
        _set_headers(self._mon_tbl, MON_H)
        self._mon_tbl.setMaximumHeight(120)
        self._mon_tbl.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._mon_tbl.customContextMenuRequested.connect(self._mon_ctx)
        self._mon_tbl.setStyleSheet(
            f"QTableWidget{{background:{BG};font-family:'{MONO}';font-size:11px;"
            f"border:1px solid {BORDER};}}")
        cl.addWidget(self._mon_tbl)
        root.addWidget(content)

        # Internal log (hidden, for _append_log compat — still used by feed_data)
        self._log = QTextEdit(); self._log.setReadOnly(True); self._log.setVisible(False)
        self._log_win = None

        self._stat_tmr = QTimer(self)
        self._stat_tmr.setInterval(2000)
        self._stat_tmr.timeout.connect(self._refresh_stats)
        self._stat_tmr.start()

    # ── log helpers (kept for feed_data compat) ───────────────────────────────
    def _append_log(self, msg, color=None):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._log.append(f"[{ts}] {msg}")
        if self._log.document().blockCount() > 400:
            cur = self._log.textCursor()
            cur.movePosition(cur.MoveOperation.Start)
            cur.select(cur.SelectionType.BlockUnderCursor)
            cur.removeSelectedText()

    # ── portfolio monitor table ────────────────────────────────────────────────
    def _refresh_monitor(self):
        """Refresh the compact portfolio monitor table in AutoPanel."""
        plist = [p for p in self._pms._positions.values() if p.is_open and not p.pending]
        self._mon_tbl.setRowCount(len(plist))
        for r, pos in enumerate(plist):
            from screener.utils import _i as _ui
            is_short = getattr(pos, "direction", "LONG") == "SHORT"
            pc   = GREEN if pos.pct >= 0 else RED
            pk_c = GREEN if pos.peak_pct > 0 else DIM
            sl_s, sl_c = (
                (f"BREACH {pos.trail_stop:.2f}", RED) if pos.breach_alerted else
                (f"W2@{pos.w2_price:.2f}", RED) if pos.w2_active and pos.w2_price > 0 else
                (f"V1@{pos.v1_price:.2f}", ORANGE) if pos.v1_active and pos.v1_price > 0 else
                (f"TIGHT {pos.trail_stop:.2f}", RED) if pos.trail_tight else
                (f"TRAIL {pos.trail_stop:.2f}", ORANGE) if pos.trail_active else
                (f"SL {pos.trail_stop:.2f}", DIM))
            # ── STRUCTURE: same logic as the main PMS table ──────────────────
            struct_s = "—"; struct_c = DIM; near_low = False
            ll = getattr(pos, "last_low", None)
            if pos.is_open and ll and ll > 0 and pos.current and pos.current > 0:
                mig_pct = (pos.current - ll) / ll * 100
                near_low = mig_pct < 0.20
                if mig_pct >= 0.5:
                    struct_s = f"▲{mig_pct:.2f}%>L"; struct_c = GREEN
                elif mig_pct >= 0.20:
                    struct_s = f"▲{mig_pct:.2f}%>L"; struct_c = ORANGE
                else:
                    struct_s = f"⚠{mig_pct:.2f}%>L"; struct_c = RED
            cells = [
                _ui(pos.symbol, color=GOLD if not is_short else ROSE, bold=True),
                _ui("S" if is_short else "L", color=ROSE if is_short else GREEN),
                _ui(f"{pos.entry:.2f}", color=TEXT),
                _ui(f"{pos.current:.2f}", color=pc),
                _ui(f"{pos.pct:+.2f}%", color=pc, bold=True),
                _ui(f"{pos.peak_pct:+.2f}%", color=pk_c),
                _ui(f"Rs{pos.pnl:+.2f}", color=pc),
                _ui(struct_s, color=struct_c, bold=(struct_c == RED)),
                _ui(sl_s, color=sl_c),
                _ui(pos.sl_log, color=RED if pos.w2_count > 0 else (ORANGE if pos.v1_count > 0 else DIM)),
                _ui("EXIT", color=RED),
            ]
            bg = (QColor("#440606") if pos.breach_alerted else
                  QColor("#330A0A") if pos.w2_active else
                  QColor("#332900") if pos.v1_active else
                  QColor("#2A0A3A" if r % 2 else "#330D44") if near_low else
                  QColor(ALT if r % 2 else BG))
            from PyQt6.QtGui import QBrush as _QBrush
            for c, it in enumerate(cells):
                it.setBackground(_QBrush(bg))
                self._mon_tbl.setItem(r, c, it)

    def _mon_ctx(self, mpos):
        """Right-click on monitor table → quick exit."""
        row = self._mon_tbl.rowAt(mpos.y())
        if row < 0: return
        it = self._mon_tbl.item(row, 0)
        if not it: return
        sym = it.text().strip()
        pos = self._pms._positions.get(sym)
        if not pos or not pos.is_open: return
        menu = QMenu(self)
        ae = menu.addAction(f"Exit  {sym}  @ {pos.current:.2f}")
        ara = menu.addAction("🛑 R_ALL — exit all")
        ch = menu.exec(self._mon_tbl.viewport().mapToGlobal(mpos))
        if ch == ae:
            self._pms.exit_pos(sym, skip_dialog=False)
        elif ch == ara:
            self._do_r_all()

    # ── portfolio popout ──────────────────────────────────────────────────────
    def _toggle_portfolio_popout(self):
        if self._log_win and not self._log_win.isHidden():
            self._log_win.hide()
            self._log_btn.setStyleSheet(
                f"background:{GOLD};color:#000;font-weight:700;font-size:10px;"
                f"padding:4px 10px;border-radius:10px;border:none;")
            return
        if self._log_win is None:
            from screener.constants import SS as _SS
            self._log_win = QDialog(self)
            self._log_win.setWindowTitle("Portfolio Monitor")
            self._log_win.resize(1100, 380)
            self._log_win.setStyleSheet(_SS)
            self._log_win.setWindowFlags(
                Qt.WindowType.Window |
                Qt.WindowType.WindowMinMaxButtonsHint |
                Qt.WindowType.WindowCloseButtonHint)
            lo2 = QVBoxLayout(self._log_win); lo2.setContentsMargins(6,6,6,6)

            topbar = QHBoxLayout(); topbar.setSpacing(8)
            self._pop_pnl  = QLabel("—")
            self._pop_pnl.setStyleSheet(f"color:{GOLD};font-size:12px;font-weight:700;font-family:'{MONO}';")
            self._pop_avg  = QLabel("—")
            self._pop_avg.setStyleSheet(f"color:{CYAN};font-size:11px;font-family:'{MONO}';")
            topbar.addWidget(self._pop_pnl); topbar.addWidget(self._pop_avg)
            topbar.addWidget(_divider_v())

            # Alloc mode toggle in popout (synced to PMS)
            from PyQt6.QtWidgets import QLabel as _QL, QComboBox as _QCB
            al = _QL("Alloc:"); al.setStyleSheet(f"color:{DIM};font-size:10px;")
            topbar.addWidget(al)
            self._pop_alloc_mode = _QCB()
            self._pop_alloc_mode.addItems(["Weighted", "Equal"])
            self._pop_alloc_mode.setCurrentIndex(self._pms._alloc_mode.currentIndex())
            self._pop_alloc_mode.setMaximumWidth(90)
            self._pop_alloc_mode.setStyleSheet(
                f"background:{PANEL};border:1px solid {BORDER};border-radius:5px;"
                f"padding:2px 6px;font-size:11px;color:{TEXT};")
            def _alloc_sync(idx):
                # Sync popup → PMS settings bar
                if self._pms._alloc_mode.currentIndex() != idx:
                    self._pms._alloc_mode.blockSignals(True)
                    self._pms._alloc_mode.setCurrentIndex(idx)
                    self._pms._alloc_mode.blockSignals(False)
                self._pms._on_alloc_mode_change()
            self._pop_alloc_mode.currentIndexChanged.connect(_alloc_sync)
            topbar.addWidget(self._pop_alloc_mode)
            topbar.addWidget(_divider_v())

            def _pb(text, bg, fg="#fff", w=80):
                b = QPushButton(text); b.setMaximumWidth(w)
                b.setStyleSheet(f"background:{bg};color:{fg};font-weight:700;font-size:10px;"
                                f"padding:4px 10px;border-radius:10px;border:none;"); return b
            pb_exit = _pb("🚪 EXIT SEL", RED)
            pb_pit  = _pb("⚡ P_STOP",   ORANGE)
            pb_rall = _pb("🛑 R_ALL", "#2A1010", TEXT, 86)
            pb_rall.setStyleSheet(
                f"background:#2A1010;color:{TEXT};font-weight:700;font-size:10px;"
                f"padding:4px 10px;border-radius:10px;border:1px solid {RED};")
            pb_exit.clicked.connect(self._do_exit_selected)
            pb_pit.clicked.connect(self._do_pitstop)
            pb_rall.clicked.connect(self._do_r_all)
            topbar.addWidget(pb_exit); topbar.addWidget(pb_pit); topbar.addWidget(pb_rall)
            topbar.addStretch()
            lo2.addLayout(topbar)

            # Full portfolio table
            POP_H = ["SYMBOL","DIR","ENTRY","CURRENT","% GAIN","PEAK %","PnL Rs",
                     "VOL FLOW","STRUCTURE","LIQUIDITY","SL/TRAIL","SL LOG"]
            self._pop_tbl = QTableWidget(0, len(POP_H))
            _set_headers(self._pop_tbl, POP_H)
            self._pop_tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self._pop_tbl.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._pop_tbl.customContextMenuRequested.connect(self._pop_ctx)
            lo2.addWidget(self._pop_tbl)

            def _pop_close(e):
                e.accept()
                self._on_portfolio_closed()
            self._log_win.closeEvent = _pop_close
        self._log_win.show(); self._log_win.raise_()
        self._sync_pop_tbl()
        self._log_btn.setStyleSheet(
            f"background:{CYAN};color:#000;font-weight:700;font-size:10px;"
            f"padding:4px 10px;border-radius:10px;border:none;")

    def _on_portfolio_closed(self):
        self._log_btn.setStyleSheet(
            f"background:{GOLD};color:#000;font-weight:700;font-size:10px;"
            f"padding:4px 10px;border-radius:10px;border:none;")

    def _pop_ctx(self, mpos):
        if not hasattr(self, "_pop_tbl"): return
        row = self._pop_tbl.rowAt(mpos.y())
        if row < 0: return
        it = self._pop_tbl.item(row, 0)
        if not it: return
        sym = it.text().strip()
        pos = self._pms._positions.get(sym)
        if not pos or not pos.is_open: return
        menu = QMenu(self)
        ae  = menu.addAction(f"Exit  {sym}  @ {pos.current:.2f}")
        ara = menu.addAction("🛑 R_ALL — exit all")
        ch  = menu.exec(self._pop_tbl.viewport().mapToGlobal(mpos))
        if ch == ae:
            self._pms.exit_pos(sym, skip_dialog=False)
        elif ch == ara:
            self._do_r_all()

    def _sync_pop_tbl(self):
        """Mirror PMS table into portfolio popout."""
        if not hasattr(self, "_pop_tbl"): return
        if self._log_win is None or self._log_win.isHidden(): return
        src = self._pms._tbl; dst = self._pop_tbl
        from PyQt6.QtWidgets import QTableWidgetItem as _QTI
        pop_cols = dst.columnCount()
        pop_src_map = [POS_H.index(h) for h in POP_H]
        dst.setRowCount(src.rowCount())
        for r in range(src.rowCount()):
            for pc, sc in enumerate(pop_src_map):
                if pc >= pop_cols: break
                it = src.item(r, sc)
                if it:
                    ni = _QTI(it.text())
                    ni.setForeground(it.foreground()); ni.setBackground(it.background())
                    ni.setFont(it.font()); ni.setTextAlignment(it.textAlignment())
                    ni.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    dst.setItem(r, pc, ni)
        # Update stats
        pos_list  = list(self._pms._positions.values())
        open_pos  = [p for p in pos_list if p.is_open and not p.pending]
        pnl       = sum(p.pnl for p in pos_list)
        prin      = self._pms.principal
        pct       = pnl / prin * 100 if prin else 0
        avg_gain  = sum(p.pct for p in open_pos) / len(open_pos) if open_pos else 0
        col       = GOLD if pct >= self._pms._tgt.value() else (GREEN if pct > 0 else RED)
        avg_col   = GREEN if avg_gain > 0 else (RED if avg_gain < 0 else DIM)
        if hasattr(self, "_pop_pnl"):
            self._pop_pnl.setText(f"PnL  Rs{pnl:+,.0f}  ({pct:+.2f}%)")
            self._pop_pnl.setStyleSheet(f"color:{col};font-size:12px;font-weight:700;font-family:'{MONO}';")
        if hasattr(self, "_pop_avg"):
            self._pop_avg.setText(f"avg {avg_gain:+.2f}%  [{len(open_pos)} open]")
            self._pop_avg.setStyleSheet(f"color:{avg_col};font-size:11px;font-family:'{MONO}';")
        # Keep alloc mode synced (PMS → popup, without triggering popup's signal)
        if hasattr(self, "_pop_alloc_mode"):
            idx = self._pms._alloc_mode.currentIndex()
            if self._pop_alloc_mode.currentIndex() != idx:
                self._pop_alloc_mode.blockSignals(True)
                self._pop_alloc_mode.setCurrentIndex(idx)
                self._pop_alloc_mode.blockSignals(False)

    def _on_log_closed(self):
        pass  # legacy compat

    # ── P_STOP / R_ALL ────────────────────────────────────────────────────────
    def _do_exit_selected(self):
        """Exit the selected row in the monitor table — with confirm popup."""
        row = self._mon_tbl.currentRow()
        if row < 0:
            # No row selected — show all open positions in a quick pick dialog
            open_pos = [p for p in self._pms._positions.values() if p.is_open and not p.pending]
            if not open_pos: return
            if len(open_pos) == 1:
                self._pms.exit_pos(open_pos[0].symbol)
                return
            from PyQt6.QtWidgets import QInputDialog
            names = [f"{p.symbol}  {p.pct:+.2f}%  Rs{p.pnl:+.0f}" for p in open_pos]
            choice, ok = QInputDialog.getItem(self, "Exit Position", "Choose:", names, 0, False)
            if ok and choice:
                sym = choice.split()[0]
                self._pms.exit_pos(sym)
            return
        it = self._mon_tbl.item(row, 0)
        if not it: return
        sym = it.text().strip()
        pos = self._pms._positions.get(sym)
        if not pos or not pos.is_open: return
        self._pms.exit_pos(sym)

    def _do_pitstop(self):
        """P_STOP from AutoPanel: exit worst scored position, enter best sust-VF candidate."""
        open_pos = [p for p in self._pms._positions.values() if p.is_open and not p.pending]
        if not open_pos: return
        rows = self._last_rows or self._pms._last_rows
        if not rows:
            QMessageBox.information(self, "P_STOP", "No market data yet — wait for first scan.")
            return
        scores = _combined_score(rows)
        worst  = min(open_pos, key=lambda p: scores.get(p.symbol, 0))
        held   = {p.symbol for p in open_pos}
        best   = self._pms._best_candidate(held, rows, scores)
        if best is None:
            QMessageBox.information(self, "P_STOP",
                "No qualifying replacement found.\n"
                "(Need: above VWAP, EMA trend up, RSI<70, pct≥1%)")
            return
        ltp_new     = best.get("ltp") or 0
        replace_ind = best.get("ind", {})
        if ltp_new <= 0: return
        if self._pms._confirm_popup.isChecked() and is_safety_on():
            dlg = TradeConfirmDialog(
                mode="PSTOP", sym=worst.symbol, ltp=worst.current, ind={}, pos=worst,
                default_alloc=worst.alloc, principal=self._pms.principal,
                leftover_cash=self._pms._leftover_cash(),
                alloc_mode_idx=self._pms._alloc_mode.currentIndex(),
                replace_sym=best["symbol"], replace_ltp=ltp_new,
                replace_ind=replace_ind, parent=self)
            if dlg.exec() != QDialog.DialogCode.Accepted: return
        rank = worst.alloc_rank
        result = broker_sell(worst.symbol, qty=worst.qty, price=worst.current,
                             order_type="MARKET", direction=worst.direction,
                             reason="P_STOP replace")
        if result.get("status") == "CANCELLED":
            QMessageBox.information(self, "Order Cancelled",
                f"{worst.symbol} P_STOP SELL was never confirmed in broker — "
                f"position is still open. Not entering the replacement.")
            return
        if result.get("status") == "FAILED":
            QMessageBox.warning(self, "Order Failed",
                f"{worst.symbol} P_STOP SELL was rejected by the broker:\n{result.get('error','unknown error')}\n\n"
                f"Stopping here — NOT entering the replacement, since the "
                f"original position may still be open at the broker.")
            return
        worst.close(result.get("average_price") or worst.current); _play_exit()
        self._pms.add(best["symbol"], ltp_new,
                      vol_flow=replace_ind.get("vol_flow"),
                      alloc_rank=rank, skip_dialog=True)

    def _open_orderbook(self):
        open_order_book_dialog(self, self._pms)

    def _do_r_all(self):
        open_pos = [p for p in self._pms._positions.values() if p.is_open and not p.pending]
        if not open_pos: return
        total_pnl = sum(p.pnl for p in open_pos)
        if not _confirm_dialog(self,"R_ALL Confirm",
                               f"Exit ALL {len(open_pos)} positions?\nTotal PnL: Rs{total_pnl:+.2f}"):
            return
        self.r_all_sig.emit()

    def _quick_save(self):
        try:
            d = os.path.join(_SCRIPT_DIR,"sim_logs"); os.makedirs(d, exist_ok=True)
            path = os.path.join(d,f"snapshot_{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.csv")
            with open(path,"w",newline="") as f:
                w = csv.writer(f)
                w.writerow(["symbol","dir","entry","current","qty","alloc","pct","peak_pct",
                            "pnl","vol_flow","px_delta","trail_stop","sl_log","entry_time"])
                for pos in self._pms._positions.values():
                    w.writerow([pos.symbol, getattr(pos,"direction","LONG"),
                                pos.entry, pos.current, pos.qty, pos.alloc,
                                f"{pos.pct:.2f}", f"{pos.peak_pct:.2f}", f"{pos.pnl:.2f}",
                                pos.vol_flow or "", pos.px_delta or "",
                                f"{pos.trail_stop:.2f}", pos.sl_log, pos.entry_time])
            fname = os.path.basename(path)
            self._append_log(f"💾 Saved → {fname}")
            # Visual feedback — flash button green briefly
            self._save_btn.setText("✓ SAVED")
            self._save_btn.setStyleSheet(
                f"background:{GREEN};color:#000;font-weight:700;font-size:10px;"
                f"padding:4px 10px;border-radius:10px;border:none;")
            QTimer.singleShot(1800, self._reset_save_btn)
        except Exception as e:
            self._append_log(f"💾 Save failed: {e}")
            self._save_btn.setText("✗ FAIL")
            self._save_btn.setStyleSheet(
                f"background:{RED};color:#fff;font-weight:700;font-size:10px;"
                f"padding:4px 10px;border-radius:10px;border:none;")
            QTimer.singleShot(2000, self._reset_save_btn)

    def _reset_save_btn(self):
        self._save_btn.setText("💾 LOG")
        self._save_btn.setStyleSheet(
            f"background:{CYAN};color:#000;font-weight:700;font-size:10px;"
            f"padding:4px 10px;border-radius:10px;border:none;")

    def _refresh_stats(self):
        pos_list = list(self._pms._positions.values())
        open_pos = [p for p in pos_list if p.is_open and not p.pending]
        pnl  = sum(p.pnl for p in pos_list); prin = self._pms.principal
        # pct vs capital actually deployed (not full principal)
        total_cost_ever = sum(p.actual_cost for p in pos_list)
        pct  = pnl / total_cost_ever * 100 if total_cost_ever > 0 else 0
        # Weighted avg gain across open positions
        total_deployed = sum(p.actual_cost for p in open_pos)
        avg_gain = (sum(p.pct * p.actual_cost for p in open_pos) / total_deployed
                    if total_deployed > 0 else 0.0)
        col  = GOLD if pct >= self._pms._tgt.value() else (GREEN if pct > 0 else RED)
        avg_col = GREEN if avg_gain > 0 else (RED if avg_gain < 0 else DIM)
        self._stat_pnl.setText(f"PnL  Rs{pnl:+,.0f}  ({pct:+.2f}%)")
        self._stat_pnl.setStyleSheet(f"color:{col};font-size:11px;font-weight:700;font-family:'{MONO}';")
        self._stat_avg.setText(f"avg {avg_gain:+.2f}%  [{len(open_pos)} open]")
        self._stat_avg.setStyleSheet(f"color:{avg_col};font-size:10px;font-family:'{MONO}';")
        # Cycle target check
        cyc_tgt = self._cycle_tgt.value()
        if prin > 0 and pct >= cyc_tgt:
            self._append_log(f"🎯 CYCLE TARGET {cyc_tgt:.1f}% REACHED  ({pct:.2f}%)")
        # Avg SL drag log
        if open_pos:
            sl_pct = self._pms.sl_pct; avg = avg_gain
            for pos in open_pos:
                if (avg - pos.pct) >= sl_pct and not pos.avg_sl_warn:
                    self._append_log(
                        f"⚠ AVG-SL DRAG: {pos.symbol}  {pos.pct:+.2f}%  avg {avg:+.2f}%  "
                        f"diff {avg-pos.pct:.2f}%  ≥ SL{sl_pct:.1f}%")
        # Refresh monitor and popout
        try: self._refresh_monitor()
        except Exception: pass
        try: self._sync_pop_tbl()
        except Exception: pass

    def feed_data(self, rows, loser_rows=None):
        self._last_rows = rows; self._last_loser_rows = loser_rows or []
        # Log any newly closed or newly warned positions
        for pos in self._pms._positions.values():
            if not pos.is_open and pos.exit_price and not getattr(pos,"_logged_exit",False):
                pos._logged_exit = True
                self._append_log(
                    f"EXIT  {pos.symbol}  entry {pos.entry:.2f}  exit {pos.exit_price:.2f}"
                    f"  PnL Rs{pos.pnl:+.2f}  ({pos.pct:+.2f}%)")
            if pos.is_open and pos.last_warn_tip and pos.last_warn_tip != getattr(pos,"_last_logged_tip",""):
                pos._last_logged_tip = pos.last_warn_tip
                self._append_log(f"WARN  {pos.symbol}  {pos.sl_log}  @{pos.current:.2f}  {pos.pct:+.2f}%")


# ══════════════════════════════════════════════════════════════════════════════
# SL event CSV logger
# ══════════════════════════════════════════════════════════════════════════════
_SL_LOG_PATH = None

def _sl_log_path():
    global _SL_LOG_PATH
    if _SL_LOG_PATH is None:
        d = os.path.join(_SCRIPT_DIR,"sim_logs"); os.makedirs(d, exist_ok=True)
        _SL_LOG_PATH = os.path.join(d,f"sl_events_{datetime.date.today().strftime('%Y-%m-%d')}.csv")
        if not os.path.exists(_SL_LOG_PATH):
            with open(_SL_LOG_PATH,"w",newline="") as f:
                csv.writer(f).writerow(["time","symbol","signal","price","trail_stop","gain_pct","peak_pct"])
    return _SL_LOG_PATH

def _log_sl_event(sym, signal, price, trail_stop, gain_pct, peak_pct):
    try:
        with open(_sl_log_path(),"a",newline="") as f:
            csv.writer(f).writerow([datetime.datetime.now().strftime("%H:%M:%S"),
                sym, signal, f"{price:.2f}", f"{trail_stop:.2f}",
                f"{gain_pct:.2f}", f"{peak_pct:.2f}"])
    except Exception: pass
