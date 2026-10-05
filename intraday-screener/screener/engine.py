"""
engine.py — AutoEngine: two-phase automatic trading logic.

STARTUP phase: waits for N consecutive confirmations on qualifying stocks,
               then queues them for user confirm.
CYCLE phase:   monitors open positions, detects retirements, suggests replacements.

Emits Qt signals consumed by AutoPanel in pms.py.
No UI code here — pure logic.
"""
import math
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

from screener.utils import _weighted_allocs


class AutoEngine(QObject):
    thought    = pyqtSignal(str)               # log message
    add_pos    = pyqtSignal(str, float, float, int)  # sym, ltp, vf, rank
    retire_pos = pyqtSignal(str, str)          # sym, reason
    suggest    = pyqtSignal(str, float, str)   # sym, ltp, reason
    cycle_hit  = pyqtSignal(float)             # cycle target % hit

    PHASE_IDLE    = "IDLE"
    PHASE_STARTUP = "STARTUP"
    PHASE_CYCLE   = "CYCLE"

    def __init__(self):
        super().__init__()
        self._phase           = self.PHASE_IDLE
        self._confirms       = 2
        self._seen         = {}
        self._positions_ref   = {}
        self._last_rows       = []
        self._pending_queue   = []
        self._cycle_target    = 3.0
        self._retired_syms    = set()
        self._suggested_syms  = set()
        self._cycle_hit_fired = False

    def set_config(self, confirms, cycle_target, positions_ref):
        self._confirms     = confirms
        self._cycle_target  = cycle_target
        self._positions_ref = positions_ref

    def start(self):
        self._phase       = self.PHASE_STARTUP
        self._seen     = {}
        self._pending_queue = []
        self.thought.emit("🚀 STARTUP: waiting for signal confirmation")

    def stop(self):
        self._phase = self.PHASE_IDLE
        self.thought.emit("■ Engine stopped")

    def on_data(self, rows, scores, principal, n_slots, concentration, auto_qty, sl_pct):
        self._last_rows = rows
        if self._phase == self.PHASE_IDLE: return
        if self._phase == self.PHASE_STARTUP:
            self._startup_tick(rows, scores, principal, n_slots, concentration, auto_qty)
        elif self._phase == self.PHASE_CYCLE:
            self._cycle_tick(rows, scores, principal, n_slots, concentration, auto_qty, sl_pct)

    # ── Qualifying filter ─────────────────────────────────────────────────────
    def _qual_rows(self, rows):
        """Qualifying movers: up 1%+, above VWAP, EMA trend up, RSI not overbought, positive volume flow."""
        out = []
        for r in rows:
            if r["per_change"] < 1.0: continue
            ind = r.get("ind", {})
            if ind.get("above_vwap") is not True: continue
            if ind.get("ema_trend") != 1: continue
            rsi = ind.get("rsi")
            if rsi is None or rsi >= 70: continue
            vf = ind.get("vol_flow")
            if vf is None or vf <= 0: continue
            out.append(r)
        return out

    # ── Startup phase ─────────────────────────────────────────────────────────
    def _startup_tick(self, rows, scores, principal, n_slots, concentration, auto_qty):
        qual = self._qual_rows(rows)

        for r in qual:
            sym = r["symbol"]
            self._seen[sym] = self._seen.get(sym, 0) + 1
        for sym in list(self._seen):
            if not any(r["symbol"] == sym for r in qual):
                del self._seen[sym]

        confirmed = {s: c for s, c in self._seen.items() if c >= self._confirms}
        waiting   = [f"{s}({c}/{self._confirms})" for s, c in self._seen.items()]
        self.thought.emit(
            f"📡 STARTUP: {len(qual)} qualify  |  "
            f"seen: {', '.join(waiting) or 'none'}  |  confirmed: {len(confirmed)}")

        if not confirmed: return

        to_add  = sorted(confirmed.keys(), key=lambda s: -scores.get(s, 0))[:n_slots]
        allocs  = _weighted_allocs(principal, len(to_add), concentration)
        pending = {d["symbol"] for d in self._pending_queue}

        for idx, sym in enumerate(to_add):
            if sym in pending: continue
            row = next((r for r in rows if r["symbol"] == sym), None)
            if row is None: continue
            ltp = row.get("ltp") or 0
            if ltp <= 0: continue
            if auto_qty and math.floor(allocs[idx] / ltp) <= 0: continue
            vf = row.get("ind", {}).get("vol_flow") or 0
            self._pending_queue.append({
                "symbol": sym, "ltp": ltp, "vf": vf,
                "alloc": allocs[idx], "rank": idx+1,
                "score": scores.get(sym, 0),
            })
            self.thought.emit(
                f"✅ QUEUE [{idx+1}/{len(to_add)}]: {sym} @ {ltp:.2f}"
                f"  Rs{allocs[idx]:,.0f}  {confirmed[sym]}x"
                f"  score {scores.get(sym,0):.0f}")

        if len(self._pending_queue) >= min(n_slots, len(confirmed)):
            self._phase = self.PHASE_CYCLE
            self.thought.emit(f"⚙️ CYCLE active — {len(self._pending_queue)} queued")

    # ── Cycle phase ───────────────────────────────────────────────────────────
    def _cycle_tick(self, rows, scores, principal, n_slots, concentration, auto_qty, sl_pct):
        pos_ref   = self._positions_ref
        open_pos  = [p for p in pos_ref.values() if p.is_open and not p.pending]
        open_syms = {p.symbol for p in open_pos}
        self._suggested_syms -= open_syms

        # Retirement check
        for pos in open_pos:
            reason = self._retirement_reason(pos, rows, scores)
            if reason:
                self.thought.emit(f"⚠️ RETIRE: {pos.symbol}  {reason}")
                self.retire_pos.emit(pos.symbol, reason)

        # Re-entry check
        for sym in list(self._retired_syms):
            row = next((r for r in rows if r["symbol"] == sym), None)
            if (row and row.get("is_setup") and
                    row.get("ind", {}).get("ema_trend") == 1 and
                    sym not in self._suggested_syms):
                ltp = row.get("ltp") or 0
                if ltp > 0:
                    self.thought.emit(f"🔄 RE-ENTRY: {sym} @ {ltp:.2f}")
                    self.suggest.emit(sym, ltp, "Re-entry: setup reformed")
                    self._suggested_syms.add(sym)
                    self._retired_syms.discard(sym)

        # Free slot suggestions
        free = n_slots - len(open_pos)
        if free > 0:
            cands = [r for r in self._qual_rows(rows)
                     if r["symbol"] not in open_syms
                     and r["symbol"] not in self._suggested_syms]
            cands.sort(key=lambda r: -scores.get(r["symbol"], 0))
            for r in cands[:free]:
                ltp = r.get("ltp") or 0
                if ltp <= 0: continue
                sym = r["symbol"]
                vf  = r.get("ind", {}).get("vol_flow") or 0
                self.thought.emit(
                    f"💡 SUGGEST: {sym} @ {ltp:.2f}"
                    f"  score {scores.get(sym,0):.0f}  VF {vf:+.0f}K")
                self.suggest.emit(sym, ltp, f"Free slot  score {scores.get(sym,0):.0f}")
                self._suggested_syms.add(sym)

        # Cycle target
        total_pnl = sum(p.pnl for p in pos_ref.values())
        if principal > 0:
            pct = total_pnl / principal * 100
            if pct >= self._cycle_target and not self._cycle_hit_fired:
                self._cycle_hit_fired = True
                self.cycle_hit.emit(pct)
                self.thought.emit(f"🎯 CYCLE {self._cycle_target:.1f}% HIT ({pct:.2f}%)")
            elif pct < self._cycle_target * 0.8:
                self._cycle_hit_fired = False

    def _retirement_reason(self, pos, rows, scores) -> Optional[str]:
        if pos.breach_alerted:
            return "SL breach"
        if pos.w2_active:
            return f"W2 — gain near 0 (peak {pos.peak_pct:+.2f}%)"
        if pos.v1_dips >= 2:
            return f"BB1 — {pos.v1_dips} dips no rebound"
        if pos.entry_vf and pos.entry_vf > 0 and pos.vol_flow is not None:
            if pos.vol_flow < pos.entry_vf * 0.2:
                return f"VF collapsed {pos.vol_flow:+.0f}K (entry {pos.entry_vf:+.0f}K)"
        return None

    def add_retired(self, sym):
        self._retired_syms.add(sym)

    def pop_pending_queue(self):
        q = list(self._pending_queue); self._pending_queue = []; return q

    @property
    def phase(self): return self._phase
