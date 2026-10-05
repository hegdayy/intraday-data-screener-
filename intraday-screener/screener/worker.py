"""
worker.py — SimWorker: background thread that fetches NSE gainers/losers
and 1-min bars (yfinance), emits data_ready and losers_ready.

Two modes:
  MODE_LIVE   — fetches gainers+losers every _interval seconds
  MODE_REPLAY — simulates yesterday bar by bar, 1 min per scan
"""
import datetime, threading, re, logging
import yfinance as yf
from nsetools import Nse

from PyQt6.QtCore import QObject, pyqtSignal

from screener.data import (
    _strip_tz, _slice_session, _compute,
    _find_cross_time, _snap_write
)
from screener.utils import _is_market_open
from screener.constants import VOL_GAINERS_UNIVERSE

log = logging.getLogger("worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

_JUNK_SYM_RE = re.compile(r'^[A-Z0-9&]{1,20}$')

def _looks_like_equity(sym: str) -> bool:
    return bool(sym) and not sym.startswith('$') and '-' not in sym and bool(_JUNK_SYM_RE.match(sym))


class SimWorker(QObject):
    data_ready       = pyqtSignal(list)   # top gainers rows — PURE gainers list only
    losers_ready     = pyqtSignal(list)   # top losers rows
    vol_gainers_ready = pyqtSignal(list)  # gainers + VOL_GAINERS_UNIVERSE, for the VOL GAINERS tab ONLY
    status_msg       = pyqtSignal(str)

    MODE_LIVE   = "live"
    MODE_REPLAY = "replay"

    def __init__(self):
        super().__init__()
        self._nse          = Nse()
        self._running      = True
        self._wake         = threading.Event()
        self._interval     = 60          # user-set, never overridden internally
        self._mode         = self.MODE_LIVE
        self._yf           = {}
        self._yf_lock      = threading.Lock()
        self._fetch_lock   = threading.Lock()
        self._log_enabled  = True
        self._cross1       = {}
        self._cross05      = {}
        self._replay_date  = None
        self._replay_cache = {}
        self._replay_ptr   = datetime.time(9, 15)
        self._held_symbols_fn = None   # callable returning set of currently-held symbols (set by main.py)

    def set_held_symbols_fn(self, fn):
        """main.py wires this to PMSManual's open-position symbol getter so the
        worker can keep fetching/indicator-computing for stocks the user holds
        even after they fall out of NSE's top-20 gainers/losers list. Without
        this, a held position's HL/LH/px_delta/breach data freezes the moment
        the stock drops out of the top-20 — it silently stops updating."""
        self._held_symbols_fn = fn

    def set_interval(self, s): self._interval = max(5, int(s)); self._wake.set()
    def set_mode(self, m):     self._mode = m; self._wake.set()
    def stop(self):
        self._running = False; self._wake.set()

    # ── Main loop ─────────────────────────────────────────────────────────────
    def run(self):
        first_run = True
        was_open  = None  # None = unknown yet, so first check always logs
        while self._running:
            self._wake.clear()
            now   = datetime.datetime.now()
            open_ = _is_market_open(now)

            if open_ != was_open:
                state = "OPEN" if open_ else "CLOSED"
                log.info(f"[market] {now.strftime('%H:%M:%S')}  state → {state} "
                        f"(system clock must be IST for this to be correct)")
                was_open = open_

            if self._mode == self.MODE_LIVE and not open_ and not first_run:
                self.status_msg.emit(
                    f"[{now.strftime('%H:%M:%S')}]  Market closed — auto-resumes 09:15 IST")
                self._wake.wait(timeout=30); continue
            try:
                if self._mode == self.MODE_LIVE: self._scan_live()
                else:                            self._scan_replay()
            except Exception as e:
                self.status_msg.emit(f"Scan error: {e}")
                log.exception("Scan error (full traceback below):")
            first_run = False
            if self._mode == self.MODE_LIVE and not open_:
                self.status_msg.emit(
                    f"[{now.strftime('%H:%M:%S')}]  Market closed — next check 30s")
                self._wake.wait(timeout=30)
            else:
                self._wake.wait(timeout=self._interval)

    # ── yfinance parallel fetch ───────────────────────────────────────────────
    def _fetch_yf_now(self, syms):
        with self._yf_lock:
            for s in syms:
                if s not in self._yf: self._yf[s] = None
        acquired = self._fetch_lock.acquire(blocking=False)
        if not acquired:
            self._fetch_lock.acquire(blocking=True); self._fetch_lock.release(); return
        try:
            results = {}; lock = threading.Lock()
            done = threading.Event(); pending = [len(syms)]

            def fetch_one(sym):
                try:
                    df = yf.Ticker(sym + ".NS").history(period="5d", interval="1m")
                    if df is not None and not df.empty:
                        with lock: results[sym] = _strip_tz(df)
                except: pass
                finally:
                    with lock:
                        pending[0] -= 1
                        if pending[0] <= 0: done.set()

            for sym in syms:
                threading.Thread(target=fetch_one, args=(sym,), daemon=True).start()
            done.wait(timeout=25)
            with self._yf_lock:
                for sym, df in results.items(): self._yf[sym] = df
            self.status_msg.emit(f"yf: {len(results)}/{len(syms)} loaded")
        finally:
            self._fetch_lock.release()

    def _fetch_now(self, syms):
        self._fetch_yf_now(syms)

    # ── Live scan ─────────────────────────────────────────────────────────────
    def _scan_live(self):
        now = datetime.datetime.now(); today = now.date()
        norm_gain, norm_loss = None, None

        if not norm_gain:
            self.status_msg.emit("Fetching NSE gainers + losers (NSEtools)...")
            try: raw_gain = self._nse.get_top_gainers(index="ALL") or []
            except Exception as e: self.status_msg.emit(f"NSE error: {e}"); raw_gain = []
            try: raw_loss = self._nse.get_top_losers(index="ALL") or []
            except: raw_loss = []

            def _parse(raw):
                out = []
                for g in raw[:20]:
                    sym = g.get("symbol", "")
                    if not _looks_like_equity(sym):
                        continue   # skip ETF NAV placeholders, SDL/SGB bonds, etc.
                    try:   pct = float(g.get("perChange", 0))
                    except: pct = 0.0
                    try:   ltp = float(g.get("ltp") or 0) or None
                    except: ltp = None
                    if sym: out.append({"symbol": sym, "per_change": pct, "ltp": ltp})
                return out

            norm_gain = _parse(raw_gain)
            norm_loss = _parse(raw_loss)
        if not norm_gain: self.status_msg.emit("No gainers returned."); return

        held_syms = set()
        if self._held_symbols_fn:
            try: held_syms = set(self._held_symbols_fn())
            except Exception: held_syms = set()
        present_syms = {g["symbol"] for g in norm_gain} | {g["symbol"] for g in norm_loss}
        missing_held = held_syms - present_syms
        for sym in missing_held:
            norm_gain.append({"symbol": sym, "per_change": 0.0, "ltp": None})

        present_syms = {g["symbol"] for g in norm_gain} | {g["symbol"] for g in norm_loss}
        missing_universe = set(VOL_GAINERS_UNIVERSE) - present_syms
        vg_extra = [{"symbol": sym, "per_change": 0.0, "ltp": None} for sym in missing_universe]

        for g in norm_gain:
            sym = g["symbol"]; pct = g["per_change"]
            if pct >= 1.0 and (sym not in self._cross1 or
                               self._cross1[sym].date() != today):
                self._cross1[sym] = now
            if pct >= 0.5 and (sym not in self._cross05 or
                               self._cross05[sym].date() != today):
                self._cross05[sym] = now

        all_syms = list({g["symbol"] for g in norm_gain + norm_loss + vg_extra})

        # Phase 1 — cached data
        with self._yf_lock: yf_snap = dict(self._yf)
        self._update_cross_times(yf_snap, today)
        self.data_ready.emit(self._build_rows(norm_gain, today, yf_snap))
        self.vol_gainers_ready.emit(self._build_rows(norm_gain + vg_extra, today, yf_snap))

        # Fetch fresh bar data
        self._fetch_now(all_syms)
        with self._yf_lock: yf_snap2 = dict(self._yf)
        self._update_cross_times(yf_snap2, today)

        rows_p2     = self._build_rows(norm_gain, today, yf_snap2)          # PURE gainers → Filter Table, PMS, auto-engine
        vg_rows_p2  = self._build_rows(norm_gain + vg_extra, today, yf_snap2)  # gainers + universe → VOL GAINERS tab only
        loser_rows  = self._build_loser_rows(norm_loss, today, yf_snap2)

        if self._log_enabled: _snap_write(rows_p2, now)
        n_setup = sum(1 for r in rows_p2 if r.get("is_setup"))
        self.status_msg.emit(
            f"[{now.strftime('%H:%M:%S')}]  {len(rows_p2)} gainers  "
            f"{len(loser_rows)} losers  |  {n_setup} setup  |  next {self._interval}s")
        self.data_ready.emit(rows_p2)
        self.vol_gainers_ready.emit(vg_rows_p2)
        self.losers_ready.emit(loser_rows)

    def _update_cross_times(self, yf_snap, today):
        for sym, df in yf_snap.items():
            if df is None: continue
            ct = _find_cross_time(df, 1.0)
            if ct is not None:
                ex = self._cross1.get(sym)
                if ex is None or ct.replace(tzinfo=None) < ex.replace(tzinfo=None):
                    self._cross1[sym] = ct.replace(tzinfo=None)

    def _build_rows(self, norm, today, yf_snap) -> list:
        yf_crosses = {}
        for sym, df in yf_snap.items():
            if df is None: continue
            ct = _find_cross_time(df, 1.0)
            if ct is not None: yf_crosses[sym] = ct.replace(tzinfo=None)
        live_only = {}
        for sym, ts in self._cross1.items():
            try:
                ts_n = ts.replace(tzinfo=None)
                if sym not in yf_crosses: live_only[sym] = ts_n
            except: pass
        all_crosses = {**live_only, **yf_crosses}
        sorted_c    = sorted(all_crosses.items(), key=lambda x: x[1])
        cross_rank  = {s: i+1 for i,(s,_) in enumerate(sorted_c)}
        cross_times = {s: t.strftime("%H:%M:%S") for s,t in all_crosses.items()}
        rows = []
        for g in norm:
            sym = g["symbol"]; pct = g["per_change"]; ltp = g["ltp"]
            df  = yf_snap.get(sym)
            ind = _compute(df) if df is not None else {}
            if ltp is None and df is not None:
                try:
                    last_close = float(df["Close"].iloc[-1])
                    if last_close > 0: ltp = round(last_close, 2)
                except Exception: pass
            if pct == 0.0 and ind.get("px_delta") is not None:
                pct = pct or ind.get("px_delta") or 0.0
            is_setup = (pct >= 1.0 and ind.get("above_vwap") is True and
                        ind.get("ema_trend") == 1)
            rows.append({"symbol":sym,"per_change":pct,"ltp":ltp,
                         "cross_rank":cross_rank.get(sym),
                         "cross_time":cross_times.get(sym),
                         "ind":ind,"is_setup":is_setup,"has_yf":df is not None})
        rows.sort(key=lambda r:(
            0 if r["is_setup"] else 1,
            r["cross_rank"] if r["cross_rank"] else 999,
            -r["per_change"]))
        for i, r in enumerate(rows): r["display_rank"] = i+1
        return rows

    def _build_loser_rows(self, norm, today, yf_snap) -> list:
        """Short setup rows: lower_highs + below VWAP + negative vol_flow."""
        rows = []
        for g in norm:
            sym = g["symbol"]; pct = g["per_change"]; ltp = g["ltp"]
            df  = yf_snap.get(sym)
            ind = _compute(df) if df is not None else {}
            is_short_setup = (pct <= -1.0 and
                              ind.get("above_vwap") is False and
                              ind.get("lower_highs") is True)
            rows.append({"symbol":sym,"per_change":pct,"ltp":ltp,
                         "ind":ind,"is_short_setup":is_short_setup,
                         "has_yf":df is not None,"display_rank":0})
        rows.sort(key=lambda r:(0 if r["is_short_setup"] else 1, r["per_change"]))
        for i, r in enumerate(rows): r["display_rank"] = i+1
        return rows

    # ── Replay scan ───────────────────────────────────────────────────────────
    def _scan_replay(self):
        replay_date = datetime.date.today() - datetime.timedelta(days=1)
        while replay_date.weekday() >= 5: replay_date -= datetime.timedelta(days=1)
        if self._replay_date != replay_date or not self._replay_cache:
            self._replay_date = replay_date; self._replay_ptr = datetime.time(9,15)
            self.status_msg.emit(f"[REPLAY] Loading {replay_date}...")
            test_syms = ["RPSGVENT","HEG","HINDCOPPER","SAIL","NATIONALUM",
                         "GMRAIRPORT","IRFC","PNB","BANKBARODA","IDEA",
                         "ZOMATO","DELTACORP","RBLBANK","MANAPPURAM",
                         "SUZLON","YESBANK","HFCL","TATACOMM","LTTS","IREDA"]
            cache = {}
            for sym in test_syms:
                try:
                    df = yf.Ticker(sym+".NS").history(period="5d", interval="1m")
                    if df is not None and not df.empty: cache[sym] = _strip_tz(df)
                except: pass
            self._replay_cache = cache

        end_t = self._replay_ptr; norm = []
        for sym, df in self._replay_cache.items():
            tod = _slice_session(df, replay_date); tod = tod[tod.index.time <= end_t]
            if tod.empty: continue
            do = float(tod["Open"].iloc[0])
            if do <= 0: continue
            pct = round((float(tod["Close"].iloc[-1])-do)/do*100, 2)
            norm.append({"symbol":sym,"per_change":pct,
                         "ltp":round(float(tod["Close"].iloc[-1]),2)})
        norm.sort(key=lambda g:-g["per_change"]); norm = norm[:20]

        rank_list = []
        for sym, df in self._replay_cache.items():
            tod = _slice_session(df, replay_date)
            if tod.empty: continue
            do = float(tod["Open"].iloc[0])
            if do <= 0: continue
            for ts, row in tod.iterrows():
                if ts.time() > end_t: break
                if (row["Close"]-do)/do*100 >= 1.0:
                    rank_list.append((sym, ts.to_pydatetime())); break
        rank_list.sort(key=lambda x: x[1])
        cross_rank  = {sym:i+1 for i,(sym,_) in enumerate(rank_list)}
        cross_times = {sym:ts.strftime("%H:%M:%S") for sym,ts in rank_list}

        rows = []
        for g in norm:
            sym = g["symbol"]; pct = g["per_change"]; ltp = g["ltp"]
            df  = self._replay_cache.get(sym); ind = {}
            if df is not None:
                tod = _slice_session(df, replay_date)
                tod = tod[tod.index.time <= end_t]
                if not tod.empty:
                    try: ind = _compute(tod)
                    except: pass
            is_setup = (pct>=1.0 and ind.get("above_vwap") is True and
                        ind.get("higher_lows") is True)
            rows.append({"symbol":sym,"per_change":pct,"ltp":ltp,
                         "cross_rank":cross_rank.get(sym),
                         "cross_time":cross_times.get(sym),
                         "ind":ind,"is_setup":is_setup,"has_yf":True})
        rows.sort(key=lambda r:(
            0 if r["is_setup"] else 1,
            r["cross_rank"] if r["cross_rank"] else 999,
            -r["per_change"]))
        for i, r in enumerate(rows): r["display_rank"] = i+1
        n_setup = sum(1 for r in rows if r["is_setup"])
        self.status_msg.emit(
            f"[REPLAY {replay_date}] @{end_t.strftime('%H:%M')}  "
            f"{len(rows)} stocks  |  {n_setup} setup")
        self.data_ready.emit(rows); self.losers_ready.emit([])
        self.vol_gainers_ready.emit(rows)  # replay mode: no live universe merge, same rows is fine
        m = end_t.hour*60+end_t.minute+1
        if m > 15*60+30: m = 9*60+15
        self._replay_ptr = datetime.time(m//60, m%60)
