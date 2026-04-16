# Phantom Trader

Async Python paper-trading system for Binance perpetual-futures market data.

Phantom Trader is designed as a long-running, real-time decision system rather than a simple offline backtest script. The public repository combines live 1-minute market-data ingestion, higher-timeframe candle building, concurrent strategy evaluation, execution-cost simulation, SQLite persistence, Telegram notifications, and a FastAPI dashboard.

## At a glance

**What it is**

A real-time, event-driven paper-trading system with persistent runtime state, lightweight observability, and multiple independent strategy configurations.

**What it is not**

- not a live order-routing system
- not a claim of production-grade exchange execution
- not a claim that all historical documentation in `docs/` is fully synchronized with the current codebase

**What you can run today**

- the trading engine via `python main.py`
- the dashboard via `python dashboard/api.py`
- the Telegram command / alert layer if the required environment variables are configured

## What the current public repository actually contains

- Python 3.11+
- `asyncio`-based runtime orchestration
- Binance combined WebSocket `1m` kline streams
- REST-based historical preload for `1h` and `4h` candles
- 10 configured paper-trading strategies
- next-bar execution simulation with slippage, fees, funding fallback, and liquidation checks
- SQLite persistence for strategy/account/position/trade/equity state
- Telegram alerts and command handlers
- FastAPI dashboard backend plus a browser frontend
- Linux-style deployment templates via `systemd` service files

## Runtime architecture

1. `BinanceWSClient` receives `1m` kline events.
2. `FeedManager` and `CandleBuilder` aggregate them into confirmed `1h` and `4h` candles.
3. Strategies evaluate only on confirmed candle closes.
4. `ExecutionSimulator` registers signals and fills them at the next bar open.
5. State is persisted into SQLite.
6. Telegram and the dashboard consume the same runtime state.

## Strategy set (current code-aligned version)

All strategies are defined in `config.py` and instantiated through `strategies/factory.py`.

| # | Strategy ID | Implementation | Timeframe | Leverage | Universe | Direction | Core idea |
|---|---|---|---|---:|---|---|---|
| 1 | `S1_trail_4h_5x` | `TrailingBreakoutStrategy` | 4h | 5x | BTCUSDT | both | trailing breakout, lookback 7 |
| 2 | `S2_keltner_1h_3x` | `KeltnerBreakoutStrategy` | 1h | 3x | BTCUSDT | both | Keltner-channel breakout |
| 3 | `S3_trail_lb20_4h_1x` | `TrailingBreakoutStrategy` | 4h | 1x | BTCUSDT | long only | trailing breakout, lookback 20 |
| 4 | `S4_trail_lb50_4h_1x` | `TrailingBreakoutStrategy` | 4h | 1x | BTCUSDT | long only | trailing breakout, lookback 50 |
| 5 | `S5_fixed_tp_4h_15x` | `FixedTPBreakoutStrategy` | 4h | 15x | BTCUSDT | both | fixed take-profit breakout |
| 6 | `S6_ma_5_20_4h_1x` | `MACrossoverStrategy` | 4h | 1x | BTCUSDT | long only | EMA 5/20 crossover |
| 7 | `S7_trail_lb10_4h_1x` | `TrailingBreakoutStrategy` | 4h | 1x | BTCUSDT | long only | trailing breakout, lookback 10 |
| 8 | `S8_multi_coin_4h_1x` | `MultiCoinStrategy` | 4h | 1x | BTC, ETH, SOL, XRP, DOGE, ADA, AVAX, DOT, LINK | long only | 9-coin Donchian-style breakout portfolio |
| 9 | `S9_ma_10_30_4h_1x` | `MACrossoverStrategy` | 4h | 1x | BTCUSDT | long only | EMA 10/30 crossover |
| 10 | `S10_composite_1h_20x` | `CompositeStrategy` | 1h | 20x | BTCUSDT | both | 48-bar breakout + RSI filter |

### Important note about #8 to #10

The current public code is **not** aligned with older documentation that described:

- #8 as a 4h / 3x / both-direction multi-coin RSI strategy
- #9 as a 4h / 3x EMA 5/20 strategy
- #10 as a 4h / 3x composite strategy

The actual code currently uses:

- #8 = `S8_multi_coin_4h_1x`, long only, Donchian-style multi-coin breakout
- #9 = `S9_ma_10_30_4h_1x`, long only, EMA 10/30
- #10 = `S10_composite_1h_20x`, both directions, 1-hour composite breakout + RSI filter

## Repository structure

```text
phantom-trader/
├── config.py
├── main.py
├── data/
│   ├── candle_builder.py
│   ├── feed_manager.py
│   ├── rest_client.py
│   └── websocket_client.py
├── execution/
│   ├── fee_model.py
│   ├── liquidation.py
│   ├── position.py
│   └── simulator.py
├── indicators/
│   ├── core.py
│   └── hub.py
├── notifications/
│   ├── alert_manager.py
│   ├── formatters.py
│   └── telegram_bot.py
├── storage/
│   ├── database.py
│   ├── models.py
│   └── state_manager.py
├── strategies/
│   ├── base_strategy.py
│   ├── composite.py
│   ├── factory.py
│   ├── fixed_breakout.py
│   ├── keltner_breakout.py
│   ├── ma_crossover.py
│   ├── multi_coin.py
│   └── trailing_breakout.py
├── utils/
│   ├── health_check.py
│   ├── logger.py
│   └── time_utils.py
├── dashboard/
│   ├── api.py
│   ├── requirements.txt
│   ├── deploy.sh
│   ├── phantom_dashboard.service
│   └── static/
├── docs/
├── LICENSE
├── README.md
├── phantom_trader.service
└── requirements.txt
```

## Execution model and realism features

The public build uses a realism-oriented paper-trading model rather than same-bar idealized fills.

Current execution-related controls in `config.py` include:

- `EXECUTION_MODE = "next_bar_open"`
- taker-fee modeling via `TAKER_FEE_RATE`
- ATR-adaptive slippage via `SLIPPAGE_BASE`, `SLIPPAGE_ATR_COEFF`, and `SLIPPAGE_CAP`
- optional latency simulation
- optional spread simulation
- funding-fee fallback via `FUNDING_FALLBACK_RATE`
- liquidation checks with maintenance-margin tiers
- restart-aware retroactive liquidation scan
- dynamic `exchangeInfo` refresh for symbol precision and minimum-notional rules

### Funding-data limitation

The repository currently uses the public `binance.vision` REST path for market-data support. In this build, funding-rate retrieval is not available from that source, so the runtime falls back to configured funding assumptions rather than live public funding data.

## Telegram integration

The Telegram bot supports these commands:

- `/status`
- `/strategy <n>`
- `/trades <n>`
- `/equity`
- `/performance`
- `/health`
- `/pause <n>`
- `/resume <n>`

The alert pipeline also sends entry, exit, liquidation, daily-report, and weekly-summary style messages.

## Dashboard

`dashboard/api.py` serves a FastAPI backend that reads the SQLite database and exposes authenticated endpoints for:

- overview KPIs
- active positions
- equity curves
- trades
- calendar P&L
- strategy detail
- cross-strategy comparison
- risk metrics
- recent activity feed

The frontend in `dashboard/static/index.html` includes:

- login screen
- overview page
- positions / risk monitor
- equity page
- strategy detail pages
- comparison matrix
- market page with TradingView widgets
- activity feed

Authentication is intentionally lightweight:

- `POST /api/auth/login` issues a signed token
- protected endpoints require `Authorization: Bearer <token>`
- the frontend stores the token in `localStorage`

## Local setup

### 1. Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate
```

### 2. Install engine dependencies

```bash
pip install -r requirements.txt
```

### 3. Install dashboard dependencies

```bash
pip install -r dashboard/requirements.txt
```

### 4. Create environment variables manually

This repository currently does **not** include `.env.example`, so create your own `.env` file or export variables in your shell.

Minimum useful variables:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DASHBOARD_PASSWORD=
DASHBOARD_SECRET=
DASHBOARD_PORT=8080
DASHBOARD_ALLOWED_ORIGINS=
PHANTOM_DB_PATH=
```

Notes:

- `main.py` attempts to load `.env` via `python-dotenv` before importing `config.py`.
- `dashboard/api.py` reads environment variables via `os.getenv`, but does **not** currently load `.env` on its own.
- If you run the dashboard as a separate process, export the required variables in the shell or launch it through a service manager that injects them.

### 5. Run the engine

```bash
python main.py
```

### 6. Run the dashboard

```bash
python dashboard/api.py
```

By default, the dashboard binds to `0.0.0.0:8080` unless `DASHBOARD_PORT` is overridden.

## Deployment note

`phantom_trader.service` is included as a Linux `systemd`-style deployment template. The `dashboard/` directory also contains a dashboard-specific deployment script and service template. Adjust paths, user, and environment-file locations before using them outside local development.

## Documentation status

The files under `docs/` should be treated cautiously:

- `Phantom_Trader_Realism_Supplement_v1_2_EN.md` appears broadly aligned with the current execution layer.
- `Phantom_Trader_SRS_v1_2_EN.md` contains stale repository / strategy descriptions, especially around strategies #8 to #10 and the mention of `.env.example`.

## Current limitations

- paper trading only; no live order routing
- limited automated test coverage
- funding-rate ingestion is fallback-based in the current public build
- dashboard authentication is intentionally lightweight
- some operational steps are still manual
- documentation outside the code is not fully synchronized with the current repository state

## Disclaimer

This project is a software-engineering / systems portfolio project, not financial advice and not a live execution system.
