"""
data.py — yfinance data fetching, indicator computation, scoring, CSV logging.

Key functions:
  _compute(df)          → dict of all indicators for one stock
  _combined_score(rows) → {sym: 0-100 balanced score}
  _snap_write(rows,now) → append to daily CSV snapshot
  _find_cross_time(df)  → datetime when stock first crossed N%
"""
import os, csv, datetime
import pandas as pd
from typing import Optional

_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Sustainable vol flow range used by engine and vol filter
VF_SUST_LO = 50.0    # K units
VF_SUST_HI = 2500.0  # K units


def _strip_tz(df):
    if df is None or df.empty: return df
    if df.index.tz is not None:
        df = df.copy(); df.index = df.index.tz_localize(None)
    return df


def _slice_session(df, ref_date=None):
    if df is None or df.empty: return pd.DataFrame()
    if ref_date is None: ref_date = df.index.date.max()
    mask = ((df.index.date == ref_date) &
            (df.index.time >= datetime.time(9, 15)) &
            (df.index.time <= datetime.time(15, 30)))
    return df[mask].copy()


def _compute_px(tod):
    """1-bar px delta (synced to refresh interval) + previous bar delta."""
    if tod is None or len(tod) < 2: return None, None
    try:
        px_end   = float(tod.iloc[-1]["Close"])
        px_start = float(tod.iloc[-2]["Close"])
        px_now   = (px_end - px_start) / px_start * 100 if px_start > 0 else 0.0
        px_prev  = None
        if len(tod) >= 3:
            ps = float(tod.iloc[-3]["Close"]); pe = float(tod.iloc[-2]["Close"])
            px_prev = (pe - ps) / ps * 100 if ps > 0 else 0.0
        return round(px_now, 2), (round(px_prev, 2) if px_prev is not None else None)
    except:
        return None, None


def _compute(df) -> dict:
    """
    Compute all indicators for a stock's 5-day 1-min DataFrame.
    Returns dict with keys:
      vwap, above_vwap, vwap_slope,
      rvol, vol_flow, vol_current, vol_cumulative,
      spike, spike_ratio,
      higher_lows, lower_highs,
      liquidity_ok, liquidity_note,
      px_delta, px_delta_prev, px_rate
    """
    out = dict(
        vwap=None, above_vwap=None, vwap_slope=None,
        rvol=None, vol_flow=None, vol_current=None, vol_cumulative=None,
        spike=False, spike_ratio=0.0,
        higher_lows=None, lower_highs=None,
        hh_new_peak=False, latest_high=None,
        liquidity_ok=None, liquidity_note="",
        px_delta=None, px_delta_prev=None, px_rate=None,
        rsi=None, macd_hist=None, ema_trend=None,
    )
    if df is None or df.empty: return out
    df = _strip_tz(df)
    if df.empty: return out

    # Classic indicators on 1-min closes: RSI(14), MACD(12,26,9) histogram, EMA 9/21 trend
    try:
        close = df["Close"].astype(float)
        if len(close) >= 30:
            chg  = close.diff()
            gain = chg.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
            loss = (-chg.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
            rsi  = 100 - 100 / (1 + gain / loss.replace(0, float("nan")))
            if not pd.isna(rsi.iloc[-1]): out["rsi"] = round(float(rsi.iloc[-1]), 1)
            ema = lambda n: close.ewm(span=n, adjust=False).mean()
            out["ema_trend"] = 1 if ema(9).iloc[-1] > ema(21).iloc[-1] else -1
            macd = ema(12) - ema(26)
            out["macd_hist"] = round(float(macd.iloc[-1] - macd.ewm(span=9, adjust=False).mean().iloc[-1]), 3)
    except: pass

    session_dates = sorted(set(
        d for d, t in zip(df.index.date, df.index.time)
        if datetime.time(9, 15) <= t <= datetime.time(15, 30)
    ), reverse=True)
    if not session_dates: return out
    ref = session_dates[0]
    tod = _slice_session(df, ref)
    if len(tod) < 2: return out
    ltp = float(tod["Close"].iloc[-1])

    # vol_current — last non-zero bar volume
    try:
        vc = 0
        for idx in range(-1, -min(4, len(tod)) - 1, -1):
            v = int(tod["Volume"].iloc[idx])
            if v > 0: vc = v; break
        out["vol_current"] = vc if vc > 0 else None
    except: pass

    try: out["vol_cumulative"] = int(tod["Volume"].sum())
    except: pass

    # VWAP
    try:
        tp   = (tod["High"] + tod["Low"] + tod["Close"]) / 3
        cvol = tod["Volume"].cumsum().replace(0, float("nan"))
        vw   = (tp * tod["Volume"]).cumsum() / cvol
        if not vw.empty and not pd.isna(vw.iloc[-1]):
            vn = round(float(vw.iloc[-1]), 2)
            out["vwap"] = vn; out["above_vwap"] = ltp > vn
            n = min(3, len(vw) - 1)
            out["vwap_slope"] = float(vw.iloc[-1]) > float(vw.iloc[-1-n]) if n > 0 else None
    except: pass

    # RVOL
    try:
        cur_t = tod.index[-1].time(); cur_v = float(tod["Volume"].iloc[-1]); pvols = []
        for pd_ in session_dates[1:]:
            pt = _slice_session(df, pd_); m = pt[pt.index.time == cur_t]
            if not m.empty: pvols.append(float(m["Volume"].iloc[0]))
        if pvols:
            avg = sum(pvols) / len(pvols)
            if avg > 0: out["rvol"] = round(cur_v / avg, 1)
    except: pass

    # Vol flow (OBV-style, K)
    try:
        dirs = tod["Close"] >= tod["Open"]
        flow = float((tod["Volume"] * dirs.map({True: 1, False: -1})).sum())
        out["vol_flow"] = round(flow / 1000, 1)
    except: pass

    # Vol spike
    try:
        n_avail = len(tod)
        if n_avail >= 4:
            window = min(10, n_avail - 1)
            avg    = tod["Volume"].rolling(window).mean().shift(1).iloc[-1]
            cur    = float(out["vol_current"] or tod["Volume"].iloc[-1])
            if not pd.isna(avg) and avg > 0:
                ratio = cur / avg
                out["spike"] = ratio >= 2.0; out["spike_ratio"] = round(ratio, 1)
    except: pass

    # Higher lows: two consecutive rising 1-min lows
    try:
        if len(tod) >= 3:
            lows_all = tod["Low"].values
            n = len(lows_all)
            streak = 0
            for i in range(n - 1, 0, -1):
                if lows_all[i] > lows_all[i - 1]:
                    streak += 1
                else:
                    break
            out["higher_lows"] = streak >= 2
            if n >= 2:
                out["last_low"] = float(lows_all[-2])
    except: pass

    # Lower highs (shorts)
    try:
        if len(tod) >= 3:
            highs_all = tod["High"].values
            n2 = len(highs_all)
            streak2 = 0
            for i in range(n2 - 1, 0, -1):
                if highs_all[i] < highs_all[i - 1]:
                    streak2 += 1
                else:
                    break
            out["lower_highs"] = streak2 >= 2
            out["latest_high"] = float(highs_all[-1])
            prior_max = float(highs_all[:-1].max()) if n2 >= 2 else float("-inf")
            out["hh_new_peak"] = highs_all[-1] >= prior_max
    except: pass

    # Liquidity
    try:
        if len(tod) >= 2:
            day_open = float(tod["Open"].iloc[0]); total_move = abs(ltp - day_open)
            if day_open > 0 and total_move > 0:
                early        = tod.iloc[:min(10, len(tod))]
                biggest_jump = float((early["High"] - early["Low"]).max())
                jump_ratio   = biggest_jump / total_move
                first_gap    = abs(float(tod["Close"].iloc[0]) - day_open) / day_open * 100
                if jump_ratio > 0.80 or first_gap > 8.0:
                    parts = []
                    if jump_ratio > 0.80: parts.append(f"single-candle {jump_ratio*100:.0f}%")
                    if first_gap > 8.0:  parts.append(f"gap {first_gap:.1f}%")
                    out["liquidity_ok"] = False; out["liquidity_note"] = "ILLIQ: " + ", ".join(parts)
                else:
                    out["liquidity_ok"] = True; out["liquidity_note"] = "OK"
    except: pass

    # PX
    try:
        px, px_prev = _compute_px(tod)
        out["px_delta"] = px; out["px_delta_prev"] = px_prev
    except: pass

    # PX rate (gain%/minute since open)
    try:
        if len(tod) >= 2:
            mins = max((tod.index[-1] - tod.index[0]).total_seconds() / 60, 1)
            op   = float(tod.iloc[0]["Open"]); cl = float(tod.iloc[-1]["Close"])
            out["px_rate"] = round((cl - op) / op * 100 / mins, 3) if op > 0 else None
    except: pass

    return out


def _find_cross_time(df, pct=1.0) -> Optional[datetime.datetime]:
    """Return first datetime when stock crossed pct% above day open."""
    if df is None or df.empty: return None
    try:
        df = _strip_tz(df)
        session_dates = sorted(set(
            d for d, t in zip(df.index.date, df.index.time)
            if datetime.time(9, 15) <= t <= datetime.time(15, 30)
        ), reverse=True)
        if not session_dates: return None
        tod      = _slice_session(df, session_dates[0])
        if tod.empty: return None
        day_open = float(tod["Open"].iloc[0])
        if day_open <= 0: return None
        hit = tod[(tod["Close"] - day_open) / day_open * 100 >= pct]
        if hit.empty: return None
        ts = hit.index[0]
        return ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    except:
        return None


def _combined_score(rows) -> dict:
    """
    Balanced percentile score for each stock.
    score = min(vf_pct, px_pct)*0.6 + avg(vf_pct, px_pct)*0.4
    Penalises imbalance (high vol + low px = absorption).
    Returns {symbol: 0-100}.
    """
    vf_v = {}; px_v = {}
    for r in rows:
        sym = r["symbol"]; ind = r.get("ind") or {}
        vf  = ind.get("vol_flow"); px = ind.get("px_delta")
        vf_v[sym] = vf if vf is not None else float("-inf")
        px_v[sym] = px if px is not None else float("-inf")
    n = max(len(rows), 1)
    vf_sorted = sorted(vf_v.items(), key=lambda x: x[1])
    px_sorted = sorted(px_v.items(), key=lambda x: x[1])
    vf_rank   = {s: i/(n-1)*100 if n>1 else 50 for i,(s,_) in enumerate(vf_sorted)}
    px_rank   = {s: i/(n-1)*100 if n>1 else 50 for i,(s,_) in enumerate(px_sorted)}
    scores = {}
    for r in rows:
        sym = r["symbol"]
        vfr = vf_rank.get(sym, 0) if vf_v.get(sym, float("-inf")) > float("-inf") else 0
        pxr = px_rank.get(sym, 0) if px_v.get(sym, float("-inf")) > float("-inf") else 0
        scores[sym] = round(min(vfr, pxr)*0.6 + ((vfr+pxr)/2)*0.4, 1)
    return scores


def _snap_write(rows, now):
    """Append scan snapshot to daily CSV in sim_logs/."""
    d    = os.path.join(_SCRIPT_DIR, "sim_logs"); os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"snap_{now.strftime('%Y-%m-%d')}.csv")
    new  = not os.path.exists(path)
    def b(v): return "Y" if v is True else ("N" if v is False else "")
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time","display_rank","symbol","pct","ltp","cross_rank",
                        "cross_time","rvol","vol_current","vol_flow","vwap",
                        "above_vwap","higher_lows","lower_highs","spike",
                        "liquidity","is_setup","rsi","macd_hist","ema_trend"])
        ts = now.strftime("%H:%M:%S")
        for r in rows:
            ind = r.get("ind", {})
            w.writerow([ts, r["display_rank"], r["symbol"], r["per_change"], r["ltp"],
                        r.get("cross_rank",""), r.get("cross_time",""),
                        ind.get("rvol",""), ind.get("vol_current",""),
                        ind.get("vol_flow",""), ind.get("vwap",""),
                        b(ind.get("above_vwap")), b(ind.get("higher_lows")),
                        b(ind.get("lower_highs")), b(ind.get("spike")),
                        ind.get("liquidity_note",""), b(r.get("is_setup")),
                        ind.get("rsi",""), ind.get("macd_hist",""), ind.get("ema_trend","")])
