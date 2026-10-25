# Crossline market dashboard

Crossline uses React 19/Vite, FastAPI, and SQLite. The overview scanner loads current Nifty 100 constituents from the NSE archive CSV (with Nifty Indices as fallback) and checks completed 4-hour bars for the SMA 6/30 bullish cross. Strategy Lab runs historical simulations only; it never submits live orders.

## Start the backend

Run these commands from the workspace root. If your terminal prompt ends in `backend`, first run `cd ..`.

```bash
eval "$(/opt/homebrew/bin/brew shellenv)"
python3 -m venv .venv315
source .venv315/bin/activate
python -m pip install --upgrade pip
python -m pip install -r backend/requirements.txt
python -m uvicorn backend.main:app --reload --port 8000
```

Put credentials in `backend/.env`; the backend loads it on startup:

```dotenv
UPSTOX_ACCESS_TOKEN=your_regular_upstox_access_token
API_KEY=your_upstox_api_key
API_SECRET=your_upstox_api_secret
```

Restart FastAPI after changing `.env`. Historical candles use the regular access token; the API key and secret are loaded for server-side authentication flows but are not needed by the current candle requests. The sandbox token used by `test_sandbox.py` is not a market-data credential. Credentials remain on the backend and are never sent to the browser.

## Start the dashboard

Open a second terminal in the workspace and run:

```bash
eval "$(/opt/homebrew/bin/brew shellenv)"
cd frontend/automationtrader
npm run dev
```

Open the URL Vite prints, usually `http://localhost:5173`. The frontend proxies `/api` requests to FastAPI on port 8000. FastAPI's interactive API page is at `http://localhost:8000/docs`.

## Trade log

Trade records are stored in `backend/crossline.db`. The current sandbox smoke test places and cancels an order, so it does not represent a completed trade or populate this log. A script can record an execution by posting to `POST /api/trades` with `symbol`, `side`, `quantity`, `price`, and optionally `status` and `order_id`. This endpoint records activity only; it does not place orders.

## Strategy Lab

The Strategy Lab offers SMA Crossover, Momentum Breakout, Opening Range Breakout, Trend-Following Pullback, Mean Reversion, VWAP Reclaim, Donchian Breakout, and EMA 7/21 + Supply/Demand. Each has independent strategy settings, persistent named presets, queued run status/cancellation, a trade ledger, stock performance, comparison, and CSV export. Every completed run stores a configuration snapshot. The EMA strategy keeps its own session times and does not change the other strategies.

Common settings include starting capital (default ₹1,00,000), dates, timeframe, universe, direction, portfolio risk/position limits, session times, volume confirmation, and per-component transaction-cost inputs. The UI defaults to 4-hour candles over a one-year range. You can choose current Nifty 100 members, selected NSE stocks, or all NSE cash equities. Defaults cap planned risk at 0.25% per trade, aggregate open risk at 0.5%, daily loss at 1%, and positions at three. A signal requires at least the configured relative volume (default 1.0x over 20 bars). After three consecutive losing trading days, new entries are paused for the next three data days. Entries use the next bar; stop/target collisions use stop-first handling; intraday positions are squared off by the configured time. Every trade is labeled SIMULATED.

### Data and limitations

Historical candles come from Upstox V3. The current Nifty 100 list is applied to the selected historical dates because historical index membership is not available here; this introduces survivorship bias and is displayed with run results. Upstox 3/5/15-minute history is limited to roughly one month, so longer requests for those intervals are rejected. 30-minute/hourly/four-hour data is fetched in chunks up to the one-year UI limit. All-NSE runs can be substantially slower and consume many API requests. Comparing 3-minute and 5-minute results takes a second user-started download.

Cost rates are intentionally unconfigured by default. Enter plan-specific brokerage, STT, exchange, GST, stamp-duty, and SEBI rates with source/effective date. Until all required charge rates are supplied, gross results are shown and net P&L/expectancy are withheld. Spread/slippage are separate estimates. Partial fills, market impact, historical universe membership, and overnight short borrowing are not modeled. When a stop and a target could both be touched inside one candle, the stop is filled first. This is a research simulator, not a profitability guarantee or a live execution system.

### EMA 7/21 + Supply/Demand

This strategy is historical simulation and paper simulation only. It does not place Upstox orders, and this build does not run a live Nifty 100 study or report profitability. Defaults: EMA 7 and 21 in trend-alignment mode, swing strength 3, fresh zones only, same-slot relative volume of at least 1.2 over the previous 20 sessions, entries from 09:30 IST with execution strictly before 13:00 IST, square-off at 15:15, and a maximum hold of 8 completed bars. The stop is the zone boundary plus 0.2 times ATR(14). The target is the nearest opposing zone, capped at 2R, and a setup is skipped when the room is below the minimum reward-to-risk. Shorts are flattened at square-off. The simulator does not have an exchange short-ban list. Paper sessions watch at most 10 selected symbols.

## Checks

```bash
python -m unittest backend.test_scanner backend.test_backtesting backend.test_universe backend.test_backtest_api backend.test_strategy_registry backend.test_simulation_engine backend.test_workbench_config backend.test_ema_supply_demand
cd frontend/automationtrader && npm run build
```