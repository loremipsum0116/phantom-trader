# Phantom Trader — Software Requirements Specification (SRS) v1.2

**Document version:** 1.2  
**Updated English edition:** 2026-04-16  
**Project:** Phantom Trader  
**Scope:** Multi-strategy paper trading, monitoring, and observability for Binance market data

---

## 1. Purpose and Scope

Phantom Trader is an asynchronous, long-running paper-trading system that runs **10 independent strategy configurations** on top of shared market data, simulated execution logic, persistent runtime state, Telegram notifications, and a FastAPI dashboard.

This document describes the behavior of the **current public GitHub edition** of the project and is aligned with the current repository structure and code behavior.

### 1.1 Main goals

- run multiple strategies concurrently on live Binance market data
- simulate execution realism beyond naive backtesting assumptions
- persist state so the service can recover after restart
- surface live status through Telegram and a browser dashboard
- remain deployable on a low-cost or free Linux VM with `systemd`

### 1.2 Non-goals

- no live order placement on Binance
- no portfolio optimization engine inside the runtime service
- no fully managed cloud dependency requirement
- no assumption that the project is tied to a single cloud vendor

### 1.3 High-level system summary

| Item | Requirement |
|---|---|
| Strategy count | 10 concurrent strategy configurations |
| Capital model | $10,000 virtual initial capital per strategy |
| Primary venue | Binance market-data endpoints |
| Data transport | `1m` WebSocket stream + REST-based historical preload |
| Timeframes used for decisions | `1h` and `4h` |
| Runtime style | 24/7 asynchronous service |
| Persistence | SQLite in WAL mode |
| Notifications | Telegram bot |
| Dashboard | FastAPI backend + browser frontend |
| Deployment target | Generic Linux VM with `systemd` |

### 1.4 Key terminology

| Term | Meaning |
|---|---|
| ATR | Average True Range |
| TP / SL | Take profit / stop loss |
| KC | Keltner Channel |
| Funding fee | Periodic futures funding transfer |
| Next-bar execution | Signal generated on candle close, filled on the next candle open |
| WAL | Write-Ahead Logging mode of SQLite |

---

## 2. Repository-aligned Architecture

### 2.1 Current repository layout

```text
phantom_trader/
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
│   └── static/
├── docs/
├── .env.example
├── phantom_trader.service
└── requirements.txt
```

### 2.2 High-level runtime architecture

The current implementation is organized around these subsystems:

1. **Market-data layer**
   - `BinanceWSClient` receives `1m` kline streams.
   - `FeedManager` coordinates historical preload and live events.
   - `CandleBuilder` aggregates `1m` candles into `1h` and `4h` candles.

2. **Indicator and strategy layer**
   - `IndicatorHub` computes indicators from stored candle history.
   - strategies evaluate only on **confirmed candle closes**.
   - strategies emit `Signal` objects but do not directly place or simulate orders.

3. **Execution layer**
   - `ExecutionSimulator` handles pending signals, next-bar fills, fees, slippage, liquidation checks, and trade recording.
   - `FundingFeeManager` maintains funding-fee state and applies fallback rates when live funding retrieval is unavailable.

4. **Persistence layer**
   - `Database` and `StateManager` store positions, trades, equity snapshots, and per-strategy state in SQLite with WAL mode.
   - persisted state is used for restart recovery.

5. **Observability layer**
   - `TelegramBot` and `AlertManager` send event notifications and respond to commands.
   - `dashboard/api.py` exposes data from the SQLite database via FastAPI.
   - the browser frontend consumes the FastAPI endpoints.

### 2.3 Main orchestrator

`main.py` provides the `PhantomTrader` orchestrator.

It is responsible for:

- loading environment variables
- initializing the database and runtime components
- loading historical data and persisted state
- creating strategy/account instances
- starting WebSocket streaming and background loops
- dispatching price updates and completed candles
- scheduling Telegram, health-check, and periodic-save tasks
- graceful shutdown and final state save

### 2.4 Background tasks

The public build runs several background tasks concurrently through `asyncio`:

- WebSocket data ingestion
- funding-check loop
- health-check loop
- daily report / scheduler
- `exchangeInfo` refresh loop
- Telegram polling
- periodic DB save loop

---

## 3. Market Data Requirements

### 3.1 Supported symbols

The current configuration includes the following symbols:

- single-asset strategies: `BTCUSDT`
- multi-coin strategy: `BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `XRPUSDT`, `DOGEUSDT`, `ADAUSDT`, `AVAXUSDT`, `DOTUSDT`, `LINKUSDT`

### 3.2 Streaming requirements

| Stream | Purpose |
|---|---|
| combined `kline_1m` streams | base 1-minute data for price monitoring and higher-timeframe aggregation |

Requirements:

- use combined WebSocket streams for the configured symbol set
- automatically reconnect after disconnects with backoff
- pass non-final `1m` updates to the real-time price-update path
- pass final `1m` candles into higher-timeframe aggregation
- ignore duplicates based on timestamp policy

### 3.3 REST supplementation requirements

| Endpoint category | Purpose |
|---|---|
| `klines` | cold-start preload for `1h` / `4h` candle history |
| `exchangeInfo` | dynamic tick size, lot size, and minimum notional |
| ticker / price fallback | consistency checks and recovery support |
| funding rate endpoints | **not available in the `binance.vision` REST path used here**; fallback rate is used |

### 3.4 Candle building requirements

The current service uses **`1m` WebSocket events** to build `1h` and `4h` candles.

Aggregation rules:

- `open` = first 1-minute candle open in the interval
- `high` = maximum high in the interval
- `low` = minimum low in the interval
- `close` = last 1-minute candle close in the interval
- `volume` = summed volume in the interval

Signal-generation rule:

- incomplete higher-timeframe candles must **not** be used for strategy decisions
- strategies evaluate signals only on **confirmed candle close**

### 3.5 Cold-start requirements

At startup, the current public code preloads approximately **14 days of historical `1h` and `4h` candles** through REST.

Relevant config values:

- `COLD_START_DAYS = 14`
- `REST_KLINE_LIMIT = 1500`
- `REST_REQUEST_DELAY = 0.12`

> Note: The current startup path does **not** preload 14 days of `1m` candles and then rebuild `1h` / `4h`.  
> Instead, `FeedManager.load_history()` directly fetches historical `1h` and `4h` candles and stores them in `candle_history`.

### 3.6 Data-integrity requirements

| Failure mode | Required handling |
|---|---|
| WebSocket disconnect | reconnect with backoff |
| delayed 1m updates | heartbeat / health-check path |
| duplicate events | timestamp-based deduplication |
| exchangeInfo fetch failure | fall back to built-in symbol specs |
| unavailable funding endpoint | use fallback funding rate |

---

## 4. Indicator and Strategy Requirements

### 4.1 Indicator subsystem

The repository centralizes indicator logic in:

- `indicators/core.py`
- `indicators/hub.py`

Indicators currently used include:

- EMA
- ATR
- RSI
- Keltner Channel components
- breakout lookback highs / lows

### 4.2 Strategy interface requirements

All strategies conform to the common contract defined in `strategies/base_strategy.py`.

Strategies:

- receive completed candles and indicator context
- determine whether a position already exists
- generate **entry** signals and, when necessary, **signal-based exit** signals
- do **not** execute orders directly
- do **not** calculate P&L internally

### 4.3 Configured strategies

The current public build defines the following strategies in `config.py`:

| # | Strategy ID | Implementation class | Timeframe | Leverage | Symbols | Direction |
|---|---|---|---:|---:|---|---|
| 1 | `S1_trail_4h_5x` | `TrailingBreakoutStrategy` | 4h | 5x | BTCUSDT | both |
| 2 | `S2_keltner_1h_3x` | `KeltnerBreakoutStrategy` | 1h | 3x | BTCUSDT | both |
| 3 | `S3_trail_lb20_4h_1x` | `TrailingBreakoutStrategy` | 4h | 1x | BTCUSDT | long only |
| 4 | `S4_trail_lb50_4h_1x` | `TrailingBreakoutStrategy` | 4h | 1x | BTCUSDT | long only |
| 5 | `S5_fixed_tp_4h_15x` | `FixedTPBreakoutStrategy` | 4h | 15x | BTCUSDT | both |
| 6 | `S6_ma_5_20_4h_1x` | `MACrossoverStrategy` | 4h | 1x | BTCUSDT | long only |
| 7 | `S7_trail_lb10_4h_1x` | `TrailingBreakoutStrategy` | 4h | 1x | BTCUSDT | long only |
| 8 | `S8_multi_4h_3x` | `MultiCoinStrategy` | 4h | 3x | 9 coins | both |
| 9 | `S9_ma_5_20_4h_3x` | `MACrossoverStrategy` | 4h | 3x | BTCUSDT | long only |
| 10 | `S10_composite_4h_3x` | `CompositeStrategy` | 4h | 3x | BTCUSDT | both |

### 4.4 Strategy evaluation requirements

- strategies are evaluated only for their configured timeframe and symbol set
- signals are generated only on **confirmed candle close**
- `MultiCoinStrategy` manages a multi-asset universe under one strategy ID
- `CompositeStrategy` combines internal logic from multiple decision components

---

## 5. Execution Requirements

### 5.1 Execution model

The public build uses a **pending-signal / next-bar-open** execution model.

Relevant config:

- `EXECUTION_MODE = "next_bar_open"`

Required behavior:

1. strategy evaluates a confirmed candle close
2. a signal is registered as **pending**
3. the pending signal is executed at the **next candle open**
4. slippage, fees, and validation are applied

### 5.2 Position and account model

The execution layer maintains:

- per-strategy `Account`
- per-strategy / per-symbol `Position`
- pending-signal storage
- trade records with fees, funding, and net P&L

Each strategy starts with **$10,000 virtual capital**.

### 5.3 Execution-cost simulation

The public build includes:

- taker fee on entry and exit (`TAKER_FEE_RATE = 0.0004`)
- ATR-adaptive slippage (`SLIPPAGE_BASE`, `SLIPPAGE_ATR_COEFF`, `SLIPPAGE_CAP`)
- optional latency simulation (`ENABLE_LATENCY_SIM`)
- optional spread simulation (`ENABLE_SPREAD_SIM`)

### 5.4 Exit handling requirements

The execution layer must support:

- stop loss
- trailing stop
- take profit
- time-based exits
- signal-based exits (e.g. moving-average crossover exit)
- forced liquidation

The simulator must apply **gap-through exit priority** conservatively when price opens through an exit level.

### 5.5 Margin and liquidation requirements

The current public build includes:

- maintenance-margin tiers (`MAINT_TIERS`)
- maximum margin ratio (`MAX_MARGIN_RATIO = 0.30`)
- symbol precision and minimum-notional validation
- real-time liquidation checks when enabled (`REALTIME_LIQ_CHECK = True`)
- retroactive liquidation scan after restart (`ENABLE_RETROACTIVE_SCAN = True`)

### 5.6 Funding-fee requirements

The execution layer contains funding-fee infrastructure through `FundingFeeManager`.

However, the public REST source configured in `data/rest_client.py` does **not** provide funding endpoints for live retrieval in this build.

Therefore the current public build uses:

- `FUNDING_FALLBACK_RATE = 0.0001`
- funding-check loop every `60` seconds
- target funding hours of `00:00`, `08:00`, `16:00` UTC

This means the code supports **funding-fee accounting**, but funding data retrieval is **fallback-based** in the public build.

---

## 6. Persistence Requirements

### 6.1 Database

The engine stores data in a SQLite database (default `phantom_trader.db`) using WAL mode.

Required persistent data includes:

- per-strategy state
- open / closed positions
- completed trades
- equity snapshots
- logs / metadata used by the dashboard and notifications

### 6.2 State management

`StateManager` is responsible for:

- restoring per-strategy account state on startup
- restoring open positions and pending execution context when applicable
- saving runtime state periodically
- enabling restart-aware liquidation scanning and continued monitoring

### 6.3 Periodic save

The main service runs a **periodic DB save loop** so that long-running strategy/account state is persisted even if the process is interrupted later.

---

## 7. Notification Requirements

### 7.1 Telegram delivery

The public build supports Telegram notifications through `notifications/telegram_bot.py` and `notifications/alert_manager.py`.

The system sends:

- trade-entry alerts
- trade-exit alerts
- liquidation notifications
- daily report messages

### 7.2 Telegram command interface

The current public build implements **8 commands**:

- `/status`
- `/strategy <number>`
- `/trades <number>`
- `/equity`
- `/performance`
- `/health`
- `/pause`
- `/resume`

The bot uses the async `python-telegram-bot` API when installed and can fall back to direct HTTP calls with `httpx`.

---

## 8. Dashboard Requirements

### 8.1 Backend

`dashboard/api.py` provides a FastAPI backend that reads the SQLite database.

Requirements:

- return overall strategy summaries
- return current positions
- return equity-curve data
- return per-strategy details and comparison metrics
- support basic authentication via password + signed token
- optionally honor `PHANTOM_DB_PATH` to locate the database file

### 8.2 Frontend

The browser-based frontend in `dashboard/static/` consumes the FastAPI API.

The current dashboard includes views such as:

- overview / KPI summary
- active positions / risk monitor
- equity curve
- strategy comparison
- market page with TradingView-based chart/technical widgets

### 8.3 Security notes

The public build does **not** include a hardcoded default password.

Operators are expected to:

- set `DASHBOARD_PASSWORD`
- set a strong random `DASHBOARD_SECRET`
- restrict `DASHBOARD_ALLOWED_ORIGINS` as appropriate

---

## 9. Operational and Deployment Requirements

### 9.1 Runtime environment

The project targets a generic Linux VM and is designed to be run under `systemd`.

### 9.2 Logging and health checks

The system includes:

- application logging (`utils/logger.py`)
- heartbeat and candle-timeout checks (`utils/health_check.py`)
- memory warnings and WebSocket status tracking

### 9.3 Configuration

Key runtime configuration includes:

- Binance endpoints
- Telegram token / chat ID
- strategy parameters and leverage
- slippage / fee / spread / latency settings
- funding-fallback settings
- dashboard password / secret / CORS settings
- symbol precision and fallback spec tables

---

## 10. Constraints and Known Limitations

The current public repository has the following limitations:

- it is a **paper-trading** system and does not place live orders
- automated test coverage is limited
- funding endpoints are not available in the public `binance.vision` REST path used here, so funding accounting currently relies on a **fallback rate**
- dashboard authentication is intentionally lightweight and should be hardened for Internet-facing deployment
- some deployment steps remain manually documented rather than fully automated
- the project is intended as a systems / engineering portfolio project rather than financial advice

---

## 11. Acceptance Criteria

The public build may be considered functionally aligned when it can:

1. start successfully with the required environment variables and dependencies
2. preload historical `1h` / `4h` candles for the configured symbols
3. ingest live `1m` WebSocket data and build higher-timeframe candles
4. run all 10 strategies and register signals on confirmed candle closes
5. execute fills at the next bar open with slippage, fees, and validation
6. update positions, trade records, account balances, and equity snapshots in SQLite
7. send Telegram alerts / respond to Telegram commands when configured
8. expose dashboard data through FastAPI and the browser frontend
9. recover persisted state after restart and continue monitoring

---

## 12. Revision Notes

This v1.2 update corrects and clarifies several points relative to earlier prose:

- cold-start history in the current code is **direct `1h` / `4h` REST preload**, not 14 days of `1m` preload rebuilt into higher timeframes
- **#8 is `MultiCoinStrategy`** and **#10 is `CompositeStrategy`**
- dashboard is a **separate FastAPI service that reads SQLite**, not a direct consumer of simulator internals
- funding-fee accounting is present, but **live funding retrieval is not available through the public REST source used here**, so the current build uses fallback funding rates
