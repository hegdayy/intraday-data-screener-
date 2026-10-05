"""
utils.py — audio, market hours, warning log, UI helpers, allocation math.
All other modules import helpers from here.
"""
import os, csv, math, datetime, threading
from typing import List, Optional
# _play_confirm and _play_exit defined below after audio init

from PyQt6.QtWidgets import (
    QFrame, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QDialog, QVBoxLayout, QHBoxLayout
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QBrush

from screener.constants import (
    BG, ALT, DIM, GREEN, RED, BORDER, MONO, UI, TEXT, ORANGE
)

from screener.broker import get_order_book

# ── Paths ─────────────────────────────────────────────────────────────────────
_SCRIPT_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _find_sound(base):
    """Find sound file by base name, trying wav then mp3."""
    for ext in (".wav", ".mp3"):
        p = os.path.join(_SCRIPT_DIR, base + ext)
        if os.path.exists(p):
            return p
    return None

_SND_WARNING   = os.path.join(_SCRIPT_DIR, "stop_loss_warning.wav")  # compat
_SND_BREACH    = os.path.join(_SCRIPT_DIR, "stop_loss_breach.wav")   # compat
_SND_CONFIRM   = os.path.join(_SCRIPT_DIR, "confirm.mp3")
_SND_SL_WARN   = os.path.join(_SCRIPT_DIR, "sl_warning.mp3")
_SND_SL_EXIT   = os.path.join(_SCRIPT_DIR, "sl_exit.mp3")
_SND_PSTOP     = os.path.join(_SCRIPT_DIR, "pstop_enter.mp3")
_SND_BTN_CLICK = os.path.join(_SCRIPT_DIR, "button_click.mp3")
_SND_AUTO_EXIT_WARN = os.path.join(_SCRIPT_DIR, "auto_exit_warn.mp3")
_SND_ALERT        = os.path.join(_SCRIPT_DIR, "alert_ding.wav")


def _ensure_alert_ding():
    """Generate a short two-tone alert WAV on first use if it is missing."""
    if os.path.exists(_SND_ALERT):
        return
    import wave, struct, math as _m
    rate = 44100
    tones = [(1400, 0.075), (1900, 0.075)]
    frames = bytearray()
    for freq, dur in tones:
        n = int(rate * dur)
        for i in range(n):
            t = i / rate
            # quick attack, exponential-ish decay envelope to avoid clicks
            env = min(1.0, i / (rate * 0.004)) * (1.0 - i / n) ** 1.5
            sample = int(32767 * 0.5 * env * _m.sin(2 * _m.pi * freq * t))
            frames += struct.pack("<h", sample)
    try:
        with wave.open(_SND_ALERT, "wb") as f:
            f.setnchannels(1); f.setsampwidth(2); f.setframerate(rate)
            f.writeframes(bytes(frames))
    except Exception as e:
        print(f"[audio] couldn't generate alert_ding.wav: {e!r}")

# ── Audio ─────────────────────────────────────────────────────────────────────
_AUDIO_OK = False
try:
    import pygame
    pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
    _AUDIO_OK = True
    print("[audio] pygame mixer initialized OK — sounds enabled.")
except Exception as e:
    print(f"[audio] DISABLED — pygame mixer failed to initialize: {e!r}")
    print(f"[audio] No sound will play until this is fixed. Common fixes: "
         f"'pip install pygame --upgrade', or check Windows sound output device.")

def _play(path):
    """Play WAV or MP3 once. Silently skipped if pygame unavailable or file missing."""
    if not _AUDIO_OK:
        return
    # If the exact path doesn't exist, try finding it with alternate extension
    if not os.path.exists(path):
        base = os.path.splitext(path)[0]
        found = _find_sound(os.path.basename(base))
        if found:
            path = found
        else:
            print(f"[audio] sound file not found: {os.path.basename(path)} "
                 f"(looked in {_SCRIPT_DIR})")
            return
    try:
        threading.Thread(
            target=lambda: pygame.mixer.Sound(path).play(),
            daemon=True).start()
    except Exception as e:
        print(f"[audio] playback failed for {os.path.basename(path)}: {e!r}")

def _play_confirm():
    """Popup confirm — confirm.mp3"""
    p = _find_sound("confirm"); _play(p if p else _SND_WARNING)

def _play_exit():
    """SL auto-exit — sl_exit.mp3"""
    p = _find_sound("sl_exit"); _play(p if p else _SND_BREACH)

def _play_sl_warn():
    """SL warning state — sl_warning.mp3"""
    p = _find_sound("sl_warning"); _play(p if p else _SND_WARNING)

def _play_pstop():
    """P_STOP entering stock — pstop_enter.mp3"""
    p = _find_sound("pstop_enter"); _play(p if p else _SND_WARNING)

def _play_btn():
    """Button / setting confirmed — button_click.mp3"""
    p = _find_sound("button_click")
    if p: _play(p)

def _play_auto_exit_warn():
    """Automatic exit confirm popup appears (HL/LH/avg-SL, 3s countdown) — auto_exit_warn.mp3"""
    p = _find_sound("auto_exit_warn"); _play(p if p else _SND_WARNING)

_alert_ding_sound = None   # cached pygame.mixer.Sound — loaded once, reused every ring

def _play_alert_ding():
    """Play the cached alert ding (decoded once, reused on every call)."""
    global _alert_ding_sound
    if not _AUDIO_OK:
        return
    try:
        if _alert_ding_sound is None:
            _ensure_alert_ding()
            if not os.path.exists(_SND_ALERT):
                _play(_SND_WARNING); return   # fallback if synth somehow failed
            _alert_ding_sound = pygame.mixer.Sound(_SND_ALERT)
        _alert_ding_sound.play()
    except Exception as e:
        print(f"[audio] alert ding playback failed: {e!r}")

def _test_audio():
    _play(_SND_WARNING)
    threading.Timer(1.5, lambda: _play(_SND_BREACH)).start()

# ── Market hours ──────────────────────────────────────────────────────────────
try:
    from zoneinfo import ZoneInfo
    _IST = ZoneInfo("Asia/Kolkata")
except Exception as _tz_err:
    _IST = None
    print(f"[market-hours] zoneinfo unavailable ({_tz_err}) — falling back "
          f"to raw system clock for market-open detection. Run "
          f"'pip install tzdata' and restart to fix this properly.")

def _is_market_open(now=None):
    """Whether NSE cash market is open right now.

    Always computed from REAL IST wall-clock time via zoneinfo, regardless
    of what timezone (or clock drift) the host OS happens to be set to.
    This used to just compare datetime.datetime.now() directly, which
    silently produced the wrong open/closed state — including missing the
    09:15 auto-resume — on any machine whose local clock/timezone wasn't
    exactly IST. The `now` parameter is accepted for backward-compat but
    intentionally ignored for the actual time-of-day check.
    """
    if _IST is not None:
        ist_now = datetime.datetime.now(_IST)
    else:
        ist_now = now if now is not None else datetime.datetime.now()
    return (ist_now.weekday() < 5 and
            datetime.time(9, 15) <= ist_now.time() <= datetime.time(15, 30))

# ── Warning log ───────────────────────────────────────────────────────────────
_WARN_LOG_PATH = None

def _warn_log_path():
    global _WARN_LOG_PATH
    if _WARN_LOG_PATH is None:
        d = os.path.join(_SCRIPT_DIR, "sim_logs")
        os.makedirs(d, exist_ok=True)
        _WARN_LOG_PATH = os.path.join(
            d, f"warn_{datetime.date.today().strftime('%Y-%m-%d')}.csv")
        if not os.path.exists(_WARN_LOG_PATH):
            with open(_WARN_LOG_PATH, "w", newline="") as f:
                csv.writer(f).writerow([
                    "time","symbol","warn_type","price_at_warn","entry_price",
                    "gain_pct_at_warn","peak_pct","vol_flow","px_delta",
                    "next_bar_price","worked"])
    return _WARN_LOG_PATH

def _log_warn(sym, warn_type, price, entry, gain_pct, peak_pct, vol_flow, px_delta):
    try:
        with open(_warn_log_path(), "a", newline="") as f:
            csv.writer(f).writerow([
                datetime.datetime.now().strftime("%H:%M:%S"),
                sym, warn_type, f"{price:.2f}", f"{entry:.2f}",
                f"{gain_pct:.2f}", f"{peak_pct:.2f}",
                f"{vol_flow:.1f}" if vol_flow is not None else "",
                f"{px_delta:.2f}" if px_delta is not None else "",
                "", ""])
    except Exception: pass

def _backfill_warn_log(sym, next_price):
    """Fill next_bar_price for the most recent unfilled row for this symbol."""
    try:
        path = _warn_log_path(); rows = []
        with open(path, "r", newline="") as f:
            reader = csv.reader(f); rows = list(reader)
        filled = False
        for i in range(len(rows)-1, 0, -1):
            r = rows[i]
            if len(r) >= 11 and r[1] == sym and r[9] == "":
                r[9] = f"{next_price:.2f}"
                r[10] = "Y" if next_price > float(r[3]) else "N"
                filled = True; break
        if filled:
            with open(path, "w", newline="") as f:
                csv.writer(f).writerows(rows)
    except Exception: pass

# ── Allocation math ───────────────────────────────────────────────────────────
def _weighted_allocs(principal: float, n: int, concentration: float) -> List[float]:
    """
    Adaptive geometric decay.
    concentration=0 → exact equal split.  concentration=1 → maximum skew to rank 1.
    Auto-scales by 1/sqrt(n) so skew stays sensible as n grows.
    Examples:
      n=3 conc=0.8 → ~59% / 28% / 13%
      n=6 conc=0.8 → ~30% / 22% / 17% / 13% / 10% / 8%
    """
    if n <= 1: return [principal]
    concentration = max(0.0, min(1.0, concentration))
    if concentration == 0.0:
        # Exact equal split — bypass decay formula entirely
        per = principal / n
        return [per] * n
    eff_conc = concentration / math.sqrt(n)
    decay    = max(0.1, min(0.99, 1.0 - eff_conc * (1.0 - 1.0/n)))
    weights  = [decay**k for k in range(n)]
    total    = sum(weights)
    return [principal * w / total for w in weights]

def _alloc_preview(principal: float, n: int, concentration: float) -> str:
    allocs = _weighted_allocs(principal, n, concentration)
    parts  = [f"Rs{a:,.0f}({a/principal*100:.0f}%)" for a in allocs]
    return "  →  ".join(parts[:min(n, 4)]) + ("  ···" if n > 4 else "")

# ── UI helpers ────────────────────────────────────────────────────────────────
def _i(text, color=None, bold=False, tooltip=None):
    it = QTableWidgetItem(str(text) if text is not None else "—")
    it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    if color:   it.setForeground(QBrush(QColor(color)))
    if bold:    f = it.font(); f.setBold(True); it.setFont(f)
    if tooltip: it.setToolTip(tooltip)
    return it

def _divider():
    f = QFrame(); f.setObjectName("divider")
    f.setFrameShape(QFrame.Shape.HLine); return f

def _divider_v():
    f = QFrame(); f.setFrameShape(QFrame.Shape.VLine)
    f.setStyleSheet(f"background:{BORDER};max-width:1px;"); return f

def _section_lbl(txt):
    l = QLabel(txt); l.setObjectName("section"); return l

def _pill_btn(text, color=GREEN, text_color="#000"):
    b = QPushButton(text)
    b.setStyleSheet(
        f"background:{color};color:{text_color};font-weight:700;"
        f"padding:6px 20px;border-radius:14px;border:none;font-size:11px;")
    return b

def _bg(row):  return QColor(ALT if row % 2 else BG)
def _pc(v):    return DIM if v is None else (GREEN if v >= 0 else RED)
def _ck(v):
    if v is True:  return "Y", GREEN
    if v is False: return "N", DIM
    return "—", DIM

def _fmt_vol(v):
    if v is None: return "—"
    try:
        v = int(v)
        if v >= 1_000_000: return f"{v/1_000_000:.2f}M"
        if v >= 1_000:     return f"{v/1_000:.1f}K"
        return str(v)
    except: return "—"

def _set_headers(tbl, headers):
    tbl.setColumnCount(len(headers))
    tbl.setHorizontalHeaderLabels(headers)
    tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    tbl.verticalHeader().setVisible(False)
    tbl.setAlternatingRowColors(False)
    tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

def open_order_book_dialog(parent, pms=None):
    dlg = QDialog(parent); dlg.setWindowTitle("Order Book"); dlg.resize(980, 520)
    dlg.setStyleSheet(f"background:{BG};color:{TEXT};")
    lo = QVBoxLayout(dlg)
    status_lbl = QLabel(""); status_lbl.setWordWrap(True)
    status_lbl.setStyleSheet(f"color:{DIM};font-size:11px;")
    lo.addWidget(status_lbl)
    headers = ["Time", "Symbol", "Side", "Qty", "Filled", "Avg Price", "Status", "Note"]
    tbl = QTableWidget(0, len(headers)); _set_headers(tbl, headers)
    lo.addWidget(tbl)
    btn_row = QHBoxLayout()
    refresh_btn = QPushButton("⟳ Refresh"); close_btn = QPushButton("Close")
    btn_row.addWidget(refresh_btn); btn_row.addStretch(); btn_row.addWidget(close_btn)
    lo.addLayout(btn_row)
    close_btn.clicked.connect(dlg.accept)

    def _populate():
        status_lbl.setStyleSheet(f"color:{DIM};font-size:11px;")
        status_lbl.setText("Loading order book...")
        tbl.setRowCount(0)
        try:
            orders = get_order_book()
        except Exception as e:
            status_lbl.setText(f"⚠ Could not fetch order book: {e}")
            return
        open_syms = ({p.symbol: p for p in pms._positions.values() if p.is_open}
                     if pms is not None else {})
        tbl.setRowCount(len(orders))
        for r, o in enumerate(sorted(orders, key=lambda x: x.get("order_timestamp") or "", reverse=True)):
            sym    = o.get("tradingsymbol", "")
            side   = o.get("transaction_type", "")
            qty    = o.get("quantity", 0)
            filled = o.get("filled_quantity", 0)
            avgp   = o.get("average_price", 0)
            status = o.get("status", "")
            ts     = str(o.get("order_timestamp", ""))
            if status == "COMPLETE":
                note, note_c = "✓ filled", GREEN
            elif status in ("REJECTED", "CANCELLED"):
                note, note_c = f"✗ {o.get('status_message') or status}", RED
                if sym in open_syms:
                    note += "  — local position exists for this symbol, VERIFY"
            else:
                note, note_c = status, ORANGE
            vals = [ts, sym, side, str(qty), str(filled),
                    f"{avgp:.2f}" if avgp else "—", status, note]
            for c, v in enumerate(vals):
                col = (RED if side == "SELL" else GREEN) if c == 2 else (note_c if c == 7 else TEXT)
                tbl.setItem(r, c, _i(v, color=col))
        base = f"{len(orders)} orders today"
        if pms is not None:
            filled_syms = {o.get("tradingsymbol") for o in orders if o.get("status") == "COMPLETE"}
            unmatched = [s for s in open_syms if s not in filled_syms]
            base += f"  ·  {len(open_syms)} open local positions"
            if unmatched:
                status_lbl.setText(base + f"   ⚠ NO matching filled order found for: "
                                    f"{', '.join(unmatched)} — check these manually!")
                status_lbl.setStyleSheet(f"color:{RED};font-size:11px;font-weight:700;")
                return
        status_lbl.setText(base)

    refresh_btn.clicked.connect(_populate)
    _populate()
    dlg.exec()
