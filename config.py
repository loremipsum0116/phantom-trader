"""
Phantom Trader — global settings and strategy parameters.
Based on SRS v1.0 + practical-realism supplement spec.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field

# ──────────────────────────── Environment variables ────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ──────────────────────────── Binance ────────────────────────────
BINANCE_WS_FUTURES = "wss://data-stream.binance.vision/stream"
BINANCE_REST_FUTURES = "https://data-api.binance.vision"

# Target symbols
BTC = "BTCUSDT"
MULTI_COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
               "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]

# WS streams (combined)
WS_STREAMS = [f"{s.lower()}@kline_1m" for s in MULTI_COINS]

# ──────────────────────────── Timeframes ────────────────────────────
TIMEFRAMES = ["1h", "4h"]
TF_MINUTES = {"1h": 60, "4h": 240}

# ──────────────────────────── Execution realism ────────────────────────────
# Fees
TAKER_FEE_RATE = 0.0004  # 0.04%

# ATR-adaptive slippage
SLIPPAGE_BASE = 0.0002
SLIPPAGE_ATR_COEFF = 0.05
SLIPPAGE_CAP = 0.005

# Network latency
ENABLE_LATENCY_SIM = True
LATENCY_MIN_MS = 50
LATENCY_MAX_MS = 200
LATENCY_SLIP_PER_SEC = 0.0005

# Spread
ENABLE_SPREAD_SIM = True
SPREAD_BASE_RATE = 0.00002
SPREAD_VOLATILE_RATE = 0.0001

# Margin/position
MAX_MARGIN_RATIO = 0.30

# Funding fee
FUNDING_FALLBACK_RATE = 0.0001
FUNDING_CHECK_INTERVAL_SEC = 60
FUNDING_HOURS = (0, 8, 16)

# Execution mode
EXECUTION_MODE = "next_bar_open"  # "next_bar_open" | "signal_close"

# Realtime checks
REALTIME_LIQ_CHECK = True

# Retroactive liquidation (on server restart)
ENABLE_RETROACTIVE_SCAN = True

# ──────────────────────────── Maintenance-margin tiers ────────────────────────────
MAINT_TIERS: list[tuple[float, float]] = [
    (50_000, 0.004),
    (250_000, 0.005),
    (1_000_000, 0.010),
    (5_000_000, 0.025),
    (20_000_000, 0.050),
    (float("inf"), 0.100),
]

# ──────────────────────────── Symbol precision ────────────────────────────
# ⚠️ The values below are FALLBACK defaults. They are refreshed dynamically from the exchangeInfo API at runtime.
# Binance may change these without prior notice, so do not rely on hardcoded values.
SYMBOL_SPECS_FALLBACK: dict[str, dict] = {
    "BTCUSDT":  {"tick_size": 0.10,    "lot_size": 0.001,  "min_notional": 5.0},
    "ETHUSDT":  {"tick_size": 0.01,    "lot_size": 0.001,  "min_notional": 5.0},
    "SOLUSDT":  {"tick_size": 0.010,   "lot_size": 0.1,    "min_notional": 5.0},
    "XRPUSDT":  {"tick_size": 0.0001,  "lot_size": 0.1,    "min_notional": 5.0},
    "DOGEUSDT": {"tick_size": 0.00001, "lot_size": 1.0,    "min_notional": 5.0},
    "ADAUSDT":  {"tick_size": 0.0001,  "lot_size": 0.1,    "min_notional": 5.0},
    "AVAXUSDT": {"tick_size": 0.01,    "lot_size": 0.1,    "min_notional": 5.0},
    "DOTUSDT":  {"tick_size": 0.001,   "lot_size": 0.1,    "min_notional": 5.0},
    "LINKUSDT": {"tick_size": 0.001,   "lot_size": 0.1,    "min_notional": 5.0},
}

# Runtime symbol specs (populated from exchangeInfo at startup, refreshed every 24h)
SYMBOL_SPECS: dict[str, dict] = {}

# exchangeInfo refresh interval
EXCHANGE_INFO_REFRESH_SEC = 86400  # 24 hours

# ──────────────────────────── DB ────────────────────────────
DB_PATH = "phantom_trader.db"
DB_DRIVER = "aiosqlite"  # Async driver required (prevents event-loop blocking)

# ──────────────────────────── Logging ────────────────────────────
LOG_DIR = "logs"
LOG_LEVEL = "INFO"
LOG_RETENTION_DAYS = 7

# ──────────────────────────── Health check ────────────────────────────
HEARTBEAT_INTERVAL_SEC = 30
CANDLE_TIMEOUT_SEC = 300
MEMORY_WARN_MB = 1024

# ──────────────────────────── WS reconnect ────────────────────────────
WS_RECONNECT_BASE_SEC = 1
WS_RECONNECT_MAX_SEC = 60

# ──────────────────────────── Telegram ────────────────────────────
TG_BATCH_DELAY_SEC = 5
TG_COOLDOWN_SYSTEM_SEC = 300

# ──────────────────────────── History ────────────────────────────
COLD_START_1M_BARS = 20160  # 14 days × 24 × 60 = 1m candles
COLD_START_DAYS = 14
REST_KLINE_LIMIT = 1500
REST_REQUEST_DELAY = 0.12

# ──────────────────────────── Strategy parameters (SRS Section 4 — fixed) ────────────────────────────


@dataclass
class StrategyConfig:
    strategy_id: str
    strategy_name: str
    initial_capital: float
    leverage: int
    timeframe: str
    symbols: list[str]
    direction: str  # "long_only" | "both"
    params: dict = field(default_factory=dict)


STRATEGIES: list[StrategyConfig] = [
    # #1 S2 trailing-stop breakout (4H, 5x)
    StrategyConfig(
        strategy_id="S1_trail_4h_5x",
        strategy_name="#1 S2 트레일링 4H 5x",
        initial_capital=10_000.0,
        leverage=5,
        timeframe="4h",
        symbols=[BTC],
        direction="both",
        params={
            "lookback": 7,
            "sl_atr_mult": 2.0,
            "trail_atr_mult": 3.0,
            "risk_per_trade": 0.005,
            "time_limit_bars": 20,
            "atr_period": 14,
        },
    ),
    # #2 Keltner-channel breakout (1H, 3x)
    StrategyConfig(
        strategy_id="S2_keltner_1h_3x",
        strategy_name="#2 켈트너 1H 3x",
        initial_capital=10_000.0,
        leverage=3,
        timeframe="1h",
        symbols=[BTC],
        direction="both",
        params={
            "kc_ema_period": 10,
            "kc_atr_mult": 2.0,
            "tp_r_mult": 3.0,
            "risk_per_trade": 0.008,
            "time_limit_bars": 40,
            "atr_period": 14,
        },
    ),
    # #3 trailing LB20 (4H, 1x)
    StrategyConfig(
        strategy_id="S3_trail_lb20_4h_1x",
        strategy_name="#3 트레일LB20 4H 1x",
        initial_capital=10_000.0,
        leverage=1,
        timeframe="4h",
        symbols=[BTC],
        direction="long_only",
        params={
            "lookback": 20,
            "sl_atr_mult": 2.0,
            "trail_atr_mult": 3.0,
            "risk_per_trade": 0.005,
            "time_limit_bars": 50,
            "atr_period": 14,
        },
    ),
    # #4 trailing LB50 (4H, 1x)
    StrategyConfig(
        strategy_id="S4_trail_lb50_4h_1x",
        strategy_name="#4 트레일LB50 4H 1x",
        initial_capital=10_000.0,
        leverage=1,
        timeframe="4h",
        symbols=[BTC],
        direction="long_only",
        params={
            "lookback": 50,
            "sl_atr_mult": 2.0,
            "trail_atr_mult": 3.0,
            "risk_per_trade": 0.005,
            "time_limit_bars": 50,
            "atr_period": 14,
        },
    ),
    # #5 fixed-TP breakout (4H, 15x)
    StrategyConfig(
        strategy_id="S5_fixed_tp_4h_15x",
        strategy_name="#5 브레이크 4H 15x",
        initial_capital=10_000.0,
        leverage=15,
        timeframe="4h",
        symbols=[BTC],
        direction="both",
        params={
            "lookback": 10,
            "sl_atr_mult": 1.0,
            "tp_r_mult": 4.0,
            "risk_per_trade": 0.015,
            "time_limit_bars": 10,
            "atr_period": 14,
        },
    ),
    # #6 MA crossover EMA5/20 (4H, 1x)
    StrategyConfig(
        strategy_id="S6_ma_5_20_4h_1x",
        strategy_name="#6 MA5/20 4H 1x",
        initial_capital=10_000.0,
        leverage=1,
        timeframe="4h",
        symbols=[BTC],
        direction="long_only",
        params={
            "fast_ema": 5,
            "slow_ema": 20,
            "sl_pct": 0.04,
            "tp_r_mult": 2.5,
            "position_pct": 0.10,
        },
    ),
    # #7 trailing LB10 (4H, 1x)
    StrategyConfig(
        strategy_id="S7_trail_lb10_4h_1x",
        strategy_name="#7 트레일LB10 4H 1x",
        initial_capital=10_000.0,
        leverage=1,
        timeframe="4h",
        symbols=[BTC],
        direction="long_only",
        params={
            "lookback": 10,
            "sl_atr_mult": 1.5,
            "trail_atr_mult": 3.0,
            "risk_per_trade": 0.005,
            "time_limit_bars": 50,
            "atr_period": 14,
        },
    ),
    # #8 multi-coin portfolio (4H, 1x)
    StrategyConfig(
        strategy_id="S8_multi_coin_4h_1x",
        strategy_name="#8 멀티코인 4H 1x",
        initial_capital=10_000.0,
        leverage=1,
        timeframe="4h",
        symbols=MULTI_COINS,
        direction="long_only",
        params={
            "lookback": 10,
            "sl_atr_mult": 1.5,
            "trail_atr_mult": 3.0,
            "risk_per_trade": 0.005,
            "time_limit_bars": 50,
            "atr_period": 14,
            "per_coin_capital": 10_000.0 / 9,  # $1,111.11
        },
    ),
    # #9 MA crossover EMA10/30 (4H, 1x)
    StrategyConfig(
        strategy_id="S9_ma_10_30_4h_1x",
        strategy_name="#9 MA10/30 4H 1x",
        initial_capital=10_000.0,
        leverage=1,
        timeframe="4h",
        symbols=[BTC],
        direction="long_only",
        params={
            "fast_ema": 10,
            "slow_ema": 30,
            "sl_pct": 0.03,
            "tp_r_mult": 3.0,
            "position_pct": 0.10,
        },
    ),
    # #10 composite trail+RSI (1H, 20x)
    StrategyConfig(
        strategy_id="S10_composite_1h_20x",
        strategy_name="#10 복합 1H 20x",
        initial_capital=10_000.0,
        leverage=20,
        timeframe="1h",
        symbols=[BTC],
        direction="both",
        params={
            "lookback": 48,
            "sl_atr_mult": 1.0,
            "trail_atr_mult": 4.0,
            "rsi_period": 14,
            "rsi_long_min": 55,
            "rsi_short_max": 45,
            "risk_per_trade": 0.015,
            "time_limit_bars": 36,
            "atr_period": 14,
        },
    ),
]
