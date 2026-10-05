# Intraday Screener & Paper-Trading Simulator

A desktop app (Python + PyQt6) that scans NSE gainers/losers during market hours,
shows live indicators for each stock, and lets you practise trading them on a
paper account with position tracking and risk controls.

## Features
- **Filter Table** – top movers with RVOL, volume flow, VWAP, RSI(14), MACD histogram and EMA 9/21 trend
- **Volume Filter** – ranks liquid NSE names by volume activity (long and short views)
- **PMS** – paper portfolio: position sizing, stop-loss / trailing stop, P&L tracking, auto-cycle mode
- **Replay mode** – re-run yesterday's session bar by bar
- Sound alerts, CSV logging, and a switchable colour theme

All orders are simulated (see `screener/broker.py`); no real money is involved.

## Run
```bash
pip install -r requirements.txt
python main.py
```

## Layout
```
main.py            app window and entry point
screener/
  worker.py        background data fetching (yfinance, NSE movers list)
  data.py          indicator maths and scoring
  tables.py        Filter Table / Volume Filter widgets
  pms.py           paper portfolio manager UI
  models.py        Position data model and risk checks
  engine.py        auto-cycle logic
  broker.py        paper broker
  utils.py         sound, market hours, UI helpers
  constants.py     colours and styles
```

Built with AI assistance (Claude) while I learned PyQt6 and market-data handling.

## License
All rights reserved – see `LICENSE`. Shared for viewing and portfolio purposes only.
