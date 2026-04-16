# Phantom Trader — Execution Realism Supplement v1.2

**Document version:** 1.2  
**Updated English edition:** 2026-04-16  
**Target:** Execution layer of Phantom Trader  
**Baseline reference:** SRS v1.2  
**Principle:** The 10 strategy definitions remain fixed; realism improvements apply to the execution and simulation layer.

---

## 1. Purpose

This supplement explains how Phantom Trader moves beyond naive backtest-style fills and toward a more realistic paper-trading model.

Its goals are to:

1. carry realism techniques from research / backtest ideas into the live paper-trading engine
2. define additional runtime-specific realism that matters in an asynchronous service
3. improve execution quality **without changing the core entry / exit logic or parameter set of the 10 strategies**

This document is aligned with the **current public repository** and its present limitations.

---

## 2. Scope Boundaries

### 2.1 In scope

- slippage modeling
- next-bar execution timing
- gap-through exit handling
- funding-fee accounting using available data / fallback rates
- liquidation-price logic
- margin validation
- latency and spread simulation toggles
- restart-aware realism such as retroactive liquidation scanning
- dynamic symbol precision handling

### 2.2 Out of scope

- changing strategy alpha logic
- re-ranking or re-optimizing strategies
- replacing paper trading with live order routing
- redesigning the dashboard, Telegram language, or persistence model for non-execution reasons

---

## 3. Realism Principles

### 3.1 Preserve strategy intent

A realism improvement is acceptable only when it changes **how fills, fees, risk, or timing are simulated**, not **why a strategy wants to trade**.

### 3.2 Avoid look-ahead bias

A signal identified on candle close must not be treated as if it were executable at that same close price.

### 3.3 Prefer adverse assumptions when ambiguity exists

When candle-level data cannot perfectly reconstruct intrabar order, the simulator should favor the **less optimistic** fill interpretation.

### 3.4 Match the current codebase

This supplement is aligned with the current public repository and especially with:

- `execution/fee_model.py`
- `execution/liquidation.py`
- `execution/position.py`
- `execution/simulator.py`
- `data/rest_client.py`
- `storage/state_manager.py`
- `config.py`

---

## 4. Realism Controls in the Current Public Build

The current public repository exposes the following realism-related configuration values:

```python
TAKER_FEE_RATE = 0.0004
SLIPPAGE_BASE = 0.0002
SLIPPAGE_ATR_COEFF = 0.05
SLIPPAGE_CAP = 0.005
ENABLE_LATENCY_SIM = True
LATENCY_MIN_MS = 50
LATENCY_MAX_MS = 200
LATENCY_SLIP_PER_SEC = 0.0005
ENABLE_SPREAD_SIM = True
SPREAD_BASE_RATE = 0.00002
SPREAD_VOLATILE_RATE = 0.0001
MAX_MARGIN_RATIO = 0.30
FUNDING_FALLBACK_RATE = 0.0001
FUNDING_CHECK_INTERVAL_SEC = 60
FUNDING_HOURS = (0, 8, 16)
EXECUTION_MODE = "next_bar_open"
REALTIME_LIQ_CHECK = True
ENABLE_RETROACTIVE_SCAN = True
EXCHANGE_INFO_REFRESH_SEC = 86400
```

These values define the baseline realism envelope of the current public version.

---

## 5. Core Realism Enhancements

### 5.1 ATR-adaptive slippage

The simulator uses a volatility-aware slippage model rather than a flat optimistic fill assumption.

Core idea:

```text
slippage = min(base_slippage + ATR / price × coefficient, cap)
```

Current baseline:

- base slippage: `0.02%`
- ATR coefficient: `0.05`
- slippage cap: `0.5%`

Rationale:

- high volatility should worsen fill quality
- fill quality should degrade proportionally, not arbitrarily
- extreme conditions should still be capped to avoid absurd outputs

Required behavior:

- apply slippage on entry and exit
- apply side-aware adverse slippage
- keep the formula deterministic for a given price / ATR input before optional latency / spread add-ons

### 5.2 Next-bar execution

The engine operates in **pending signal -> next open execution** mode.

Reason:

- candle-close detection, decision calculation, and order transmission are not instantaneous
- same-candle-close fills would introduce look-ahead bias in a candle-based simulation model

Required flow:

1. strategy evaluates signal at confirmed candle close
2. signal is stored as pending
3. pending signal executes on the next candle open
4. execution costs are applied to that fill

Current baseline:

- `EXECUTION_MODE = "next_bar_open"`

### 5.3 Gap-through exit priority

The simulator models the case where price opens beyond a stop level.

Required principle:

- if the next candle opens through a stop, the fill should occur at the open, not at the previously planned stop level

For long positions, a conservative candle-priority model is:

1. liquidation if the open is already through liquidation
2. stop loss or trailing stop if the open is already through the effective stop
3. stop loss or trailing stop if intrabar low reaches the effective stop
4. take profit if intrabar high reaches TP

For short positions, use the mirrored logic.

Why this matters:

- it prevents over-optimistic stop fills
- it reduces the gap between naive backtests and paper-trading realism
- it correctly treats liquidation as higher priority than an idealized stop in certain leverage scenarios

### 5.4 Funding-fee accounting with fallback rates

The runtime accounts for periodic funding for leveraged positions.

Current baseline:

- funding-check loop every `60` seconds
- target funding hours: `00:00`, `08:00`, `16:00` UTC
- `FUNDING_FALLBACK_RATE = 0.0001`

Required behavior:

- apply funding only to relevant leveraged positions
- preserve long / short directionality
- accumulate funding effects over the holding period

**Important limitation of the public build**

The current `data/rest_client.py` uses the public `binance.vision` REST path and the funding-fetch methods return `None` / empty data because the required funding endpoints are not available there.

Therefore the current public build provides:

- funding-fee **accounting infrastructure**
- fallback-rate application at funding times
- no live public funding retrieval through the current REST source

This should be described as **funding-fee realism with fallback support**, not as fully live funding integration.

### 5.5 Tiered maintenance margin and liquidation

Liquidation depends on both leverage and position notional.

Required behavior:

- determine maintenance margin rate from notional tiers (`MAINT_TIERS`)
- compute liquidation price from entry, leverage, direction, and maintenance margin
- reject a trade if the margin ratio is non-viable from the beginning
- use real-time liquidation checks when configured

This protects against unrealistic position acceptance in high-leverage situations.

### 5.6 Integrated net P&L treatment

Realistic P&L is not just raw price delta.

Net trade results incorporate:

- adverse entry fill
- adverse exit fill
- taker fee on both sides
- funding cost or credit during the holding period

This ensures that strategy monitoring is grounded in simulated executable outcomes rather than idealized chart outcomes.

### 5.7 Margin cap and pre-open validation

The engine enforces a margin ceiling through:

- `MAX_MARGIN_RATIO = 0.30`

Required behavior:

- reject positions that would consume excessive equity
- respect symbol minimum notional and lot size
- round according to runtime symbol precision
- reject positions whose liquidation geometry makes the protective stop effectively meaningless

---

## 6. Runtime-Specific Realism Beyond Traditional Backtests

### 6.1 Latency simulation

The repository supports optional latency-based fill degradation.

Config hooks:

- `ENABLE_LATENCY_SIM`
- `LATENCY_MIN_MS`
- `LATENCY_MAX_MS`
- `LATENCY_SLIP_PER_SEC`

Purpose:

- represent a small but non-zero delay between signal generation and practical execution
- add realism without pretending to model the full exchange matching engine

### 6.2 Spread simulation

The repository also supports optional spread simulation.

Config hooks:

- `ENABLE_SPREAD_SIM`
- `SPREAD_BASE_RATE`
- `SPREAD_VOLATILE_RATE`

Purpose:

- represent bid/ask spread costs that are not captured by a pure mid-price model
- make fills less optimistic in more volatile conditions

### 6.3 Restart-aware retroactive liquidation scan

Real paper-trading services do not always run continuously without interruption.

The current build supports restart-aware realism via:

- `ENABLE_RETROACTIVE_SCAN = True`

When the service restarts, previously open positions can be checked against elapsed market history to determine whether liquidation would already have occurred during downtime.

This prevents unrealistic survival of positions that would have been liquidated while the process was offline.

### 6.4 Dynamic symbol-precision refresh

Binance can change symbol precision, minimum lot size, or minimum notional values.

The current build refreshes `exchangeInfo` at startup and every 24 hours:

- `EXCHANGE_INFO_REFRESH_SEC = 86400`

Fallback values are stored in `SYMBOL_SPECS_FALLBACK`.

This reduces the risk of unrealistic trade acceptance due to stale precision assumptions.

---

## 7. Realism Requirements for Metrics and Reporting

The dashboard and Telegram reports should reflect **net** trade and account outcomes rather than idealized raw price differences.

Required metrics include:

- realized P&L after fees / funding
- balance and equity snapshots
- drawdown tracking
- liquidation counts
- win / loss counts and win rate
- profit factor using gross wins and gross losses

This ensures operational monitoring is based on the same realism-aware execution model used by the engine.

---

## 8. Constraints and Known Limitations

The current public build still has realism limitations, including:

- no order-book or depth-of-market simulation
- no partial fills
- no exchange-matching-engine emulation
- no live order placement
- funding retrieval currently relies on a **fallback rate** because the configured public REST source does not provide the required funding endpoints
- optional latency / spread simulation are simplified rather than venue-calibrated

These limitations are acceptable for a portfolio-oriented paper-trading engine so long as they are described accurately.

---

## 9. Acceptance Criteria

The execution layer may be considered realism-aligned when it can:

1. delay fills until the next bar open after a confirmed signal
2. apply ATR-based adverse slippage and taker fees on entry and exit
3. model gap-through stop behavior conservatively
4. compute liquidation prices and reject non-viable positions
5. apply funding fees using fallback rates at scheduled funding times
6. continue to produce net P&L, balance, and trade metrics suitable for dashboard / Telegram reporting
7. recover from restart and perform retroactive liquidation scanning when enabled

---

## 10. Revision Notes

This v1.2 update clarifies the current public build in several ways:

- it retains the realism discussion around slippage, next-bar execution, liquidation, and restart-aware behavior
- it explicitly notes that **funding-fee accounting exists, but the current public REST source does not provide live funding retrieval**, so fallback rates are used
- it describes the current implementation conservatively and avoids overstating funding-data realism in the public repository
