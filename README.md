# NSE India Paper Trading Bot

Real-time options paper-trading dashboard for Nifty & BankNifty.

## Features
- Live spot prices from NSE India (`allIndices` API)
- Live ATM option premiums from NSE option chain
- Black-Scholes fallback when market is closed
- Correct expiry dates: NIFTY→Thursday, BANKNIFTY→Wednesday, FINNIFTY→Tuesday
- Signal engine with 10-point scoring (requires 5/10 for entry)
- 2.5:1 minimum risk-reward on every trade
- Daily 8% profit target — auto-locks new entries once reached
- Time-of-day gate: no entries before 9:30 IST or after 15:00 IST
- VIX ceiling: no long-option buys when VIX > 25
- Trades logged to `trades.xlsx`

## Run Locally

```bash
cd trading_bot
pip install -r requirements.txt
python app.py
# Open http://127.0.0.1:5000
```

## Deploy on Railway (recommended)

Railway supports background threads and persistent filesystem — ideal for this bot.

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Select this repo
4. Set environment variables (see `.env.example`)
5. Railway auto-detects the `Procfile` and deploys

## Deploy on Render

1. Go to [render.com](https://render.com) → New Web Service → Connect GitHub
2. Build command: `pip install -r requirements.txt`
3. Start command: `gunicorn --chdir trading_bot app:app --workers 1 --threads 2 --timeout 120`
4. Set environment variables from `.env.example`

## Deploy on Vercel

> **Note:** Vercel is serverless — the background refresh thread is replaced by on-demand refresh on each page load. Wallet state resets on cold starts. For persistent trading state use Railway or Render.

1. Push to GitHub
2. Import repo at [vercel.com](https://vercel.com)
3. Framework: Other
4. Set environment variables from `.env.example` in Vercel dashboard
5. Deploy — the `vercel.json` handles routing automatically

## Environment Variables

Copy `.env.example` to `.env` and fill in:

| Variable | Default | Description |
|---|---|---|
| `PAPER_MODE` | `true` | `false` to enable live Angel One orders |
| `STARTING_WALLET` | `100000` | Starting virtual balance (₹) |
| `MAX_LOTS_PER_ORDER` | `2` | Max lots per signal |
| `FLASK_SECRET` | — | Random secret for Flask sessions |
| `ANGEL_API_KEY` | — | Angel One SmartAPI key (live mode only) |
| `ANGEL_CLIENT_CODE` | — | Angel One client ID |
| `ANGEL_MPIN` | — | Angel One 4-digit PIN |
| `ANGEL_TOTP_SECRET` | — | TOTP secret from SmartAPI |

## Signal Logic

A trade signal requires **5 out of 10 points** across four pillars:

| Pillar | Max Score | Indicators |
|---|---|---|
| Trend | ±3 | EMA 9/21/50 alignment, Supertrend direction |
| Momentum | ±3 | RSI (55–75 zone), MACD crossover, Price vs VWAP |
| Price Action | ±4 | ORB breakout, Bollinger position, Engulfing candles |
| OI Sentiment | ±1 | Put-Call Ratio (contrarian) |

Additional gates block entry even with high score:
- Before 9:30 IST (opening whipsaw)
- After 15:00 IST (expiry gamma squeeze)
- VIX > 25 (options too expensive)
- Trend and momentum disagree
