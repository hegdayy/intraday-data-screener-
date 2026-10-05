"""models.py — Position data model with stop-loss and warning checks.

Every signal raised by check_sl() is recorded in sl_events with a timestamp.
"""
import datetime
from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class Position:
    symbol:       str
    entry:        float
    alloc_rank:   int   = 1
    current:      float = 0.0
    qty:          float = 0.0
    alloc:        float = 0.0
    actual_cost:  float = 0.0
    leftover:     float = 0.0
    pct:          float = 0.0
    peak_pct:     float = 0.0
    is_open:      bool  = True
    pending:      bool  = False
    exit_price:   Optional[float] = None
    entry_time:   str   = ""
    liquidity:    str   = ""
    px_delta:     Optional[float] = None
    vol_flow:     Optional[float] = None
    last_low:     Optional[float] = None  # last confirmed 1-min bar low
    entry_vf:     Optional[float] = None
    direction:    str   = "LONG"

    # Trail SL
    trail_active:   bool  = False
    trail_stop:     float = 0.0
    trail_tight:    bool  = False

    v1_count:       int   = 0
    v1_dips:        int   = 0
    v1_active:      bool  = False
    v1_price:       float = 0.0
    peak_px:        float = 0.0
    prev_px:        Optional[float] = None
    prev_gain:      Optional[float] = None

    w2_count:       int   = 0
    w2_active:      bool  = False
    w2_price:       float = 0.0

    hl_warned:      bool  = False

    lh_warned:      bool  = False

    breach_alerted: bool  = False

    # Rebound tracking
    rebound_count:   int   = 0
    rebound_fails:   int   = 0
    rebound_target:  float = 0.0

    # Cycle avg drag
    avg_sl_warn:     bool  = False

    sl_log:         str   = ""
    last_warn_tip:  str   = ""

    # Full SL event history — list of (time_str, signal, price, gain_pct)
    sl_events: List[tuple] = field(default_factory=list)

    # ── properties ────────────────────────────────────────────────────────────
    @property
    def pnl(self):
        if self.is_open:
            return (self.current - self.entry) * self.qty
        return ((self.exit_price or self.entry) - self.entry) * self.qty

    def update(self, price):
        self.current = price
        if self.entry > 0:
            self.pct = (price - self.entry) / self.entry * 100
            if self.pct > self.peak_pct:
                self.peak_pct = self.pct

    def close(self, price):
        self.exit_price = price
        self.is_open = False
        if self.entry > 0:
            self.pct = (price - self.entry) / self.entry * 100

    # ── SL log string ─────────────────────────────────────────────────────────
    def _update_sl_log(self):
        parts = []
        if self.v1_count > 0:
            p  = f"@{self.v1_price:.2f}" if self.v1_price > 0 else ""
            rb = (f" rb{self.rebound_count}✓/{self.rebound_fails}✗"
                  if self.rebound_count + self.rebound_fails > 0 else "")
            parts.append(f"V1({self.v1_count}){p}{rb}")
        if self.v1_dips >= 2:
            parts.append(f"BB1({self.v1_dips})")
        if self.w2_count > 0:
            p = f"@{self.w2_price:.2f}" if self.w2_price > 0 else ""
            parts.append(f"W2({self.w2_count}){p}")
        if self.hl_warned:
            parts.append("HL✗")
        if self.lh_warned:
            parts.append("LH✗")
        if self.avg_sl_warn:
            parts.append("AVG⚠")
        if self.trail_tight:
            parts.append("TIGHT")
        self.sl_log = "  ".join(parts) if parts else "—"

    # ── Main SL check ─────────────────────────────────────────────────────────
    def check_sl(self, sl_pct: float, new_px: Optional[float],
                 bb1_chances: int = 2,
                 higher_lows: Optional[bool] = None,
                 hl_warn_enabled: bool = True,
                 lower_highs: Optional[bool] = None,
                 lh_warn_enabled: bool = False) -> List[str]:
        """
        Called every scan. Returns list of new signals fired.
        sl_pct          — stop loss % (from PMS settings)
        new_px          — current bar px_delta (% move this bar)
        bb1_chances     — consecutive V1 dips before BB1
        higher_lows     — current HL state from indicator
        hl_warn_enabled — whether HL warning is active
        lower_highs     — current LH state from indicator
        lh_warn_enabled — whether LH warning is active
        """
        if not self.is_open or self.pending or self.entry <= 0:
            return []
        signals = []
        ts = datetime.datetime.now().strftime("%H:%M:%S")

        # ── Trail activation ──────────────────────────────────────────────────
        if self.peak_pct >= sl_pct and not self.trail_active:
            self.trail_active = True

        # ── Stop price calculation ────────────────────────────────────────────
        if not self.trail_active:
            stop = self.entry * (1 - sl_pct / 100)
        elif self.trail_tight:
            stop = self.entry * (1 + (self.peak_pct - sl_pct * 0.4) / 100)
        else:
            stop = self.entry * (1 + (self.peak_pct - sl_pct) / 100)
        self.trail_stop = max(stop, 0)

        # ── Peak px tracking ──────────────────────────────────────────────────
        if new_px is not None and new_px > self.peak_px:
            self.peak_px = new_px
            self.rebound_target = self.peak_px * 0.5

        # ── V1 — px momentum collapse ─────────────────────────────────────────
        if new_px is not None and self.peak_px > 0:
            px_ratio = new_px / self.peak_px
            if px_ratio <= 0.5 and not self.v1_active:
                self.v1_active = True
                self.v1_count += 1
                self.v1_price = self.current
                self.v1_dips += 1
                signals.append("v1")
                self.sl_events.append((ts, "v1", self.current, round(self.pct, 2)))
            elif self.v1_active:
                if new_px >= self.rebound_target:
                    self.rebound_count += 1
                    self.v1_active = False
                    self.v1_dips = 0
                else:
                    if self.prev_px is not None and new_px < self.prev_px:
                        self.rebound_fails += 1
                    if self.v1_dips >= bb1_chances:
                        if "bb1" not in [e[1] for e in self.sl_events[-3:]]:
                            signals.append("bb1")
                            self.sl_events.append((ts, "bb1", self.current, round(self.pct, 2)))

        if new_px is not None:
            self.prev_px = new_px
        self.prev_gain = self.pct

        # ── W2 — gain near zero ───────────────────────────────────────────────
        if self.peak_pct > 1.0 and self.pct < 0.5 and not self.w2_active:
            self.w2_active = True
            self.w2_count += 1
            self.w2_price = self.current
            if self.trail_active and not self.trail_tight:
                self.trail_tight = True
            signals.append("w2")
            self.sl_events.append((ts, "w2", self.current, round(self.pct, 2)))
        elif self.pct >= 1.0:
            self.w2_active = False

        # ── HL lost ───────────────────────────────────────────────────────────
        if hl_warn_enabled:
            if higher_lows is False and not self.hl_warned:
                self.hl_warned = True
                signals.append("hl")
                self.sl_events.append((ts, "hl", self.current, round(self.pct, 2)))
            elif higher_lows is True:
                self.hl_warned = False

        # ── LH lost (lower_highs stopped) ────────────────────────────────────
        if lh_warn_enabled:
            if lower_highs is False and not self.lh_warned:
                self.lh_warned = True
                signals.append("lh")
                self.sl_events.append((ts, "lh", self.current, round(self.pct, 2)))
            elif lower_highs is True:
                self.lh_warned = False

        # ── Breach ────────────────────────────────────────────────────────────
        if self.current <= self.trail_stop and not self.breach_alerted:
            self.breach_alerted = True
            signals.append("breach")
            self.sl_events.append((ts, "breach", self.current, round(self.pct, 2)))

        self._update_sl_log()

        if signals:
            vf_s = f"  VF:{self.vol_flow:+.0f}K" if self.vol_flow else ""
            self.last_warn_tip = (
                f"{ts}  [{', '.join(signals)}]\n"
                f"Price:{self.current:.2f}  Gain:{self.pct:+.2f}%  Peak:{self.peak_pct:+.2f}%\n"
                f"PX:{(new_px or 0):+.2f}%  PeakPX:{self.peak_px:+.2f}%"
                f"  Rebounds:{self.rebound_count}✓/{self.rebound_fails}✗{vf_s}")

        return signals
