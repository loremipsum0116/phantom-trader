"""Fee, slippage, and funding-fee model — v3.8.0 realism."""
from __future__ import annotations
import bisect
import random
from dataclasses import dataclass, field

from utils.logger import log
from utils.time_utils import is_funding_time
import config as cfg


# ──────────────────────────── Slippage ────────────────────────────

def calc_slippage(price: float, atr: float | None) -> float:
    """ATR-adaptive slippage (ported from optimizer _pnl)."""
    if atr is None or price <= 0:
        return cfg.SLIPPAGE_BASE
    return min(
        cfg.SLIPPAGE_BASE + (atr / price) * cfg.SLIPPAGE_ATR_COEFF,
        cfg.SLIPPAGE_CAP,
    )


def simulate_latency_slip() -> float:
    """Additional slippage from network latency."""
    if not cfg.ENABLE_LATENCY_SIM:
        return 0.0
    ms = random.uniform(cfg.LATENCY_MIN_MS, cfg.LATENCY_MAX_MS)
    return (ms / 1000) * cfg.LATENCY_SLIP_PER_SEC


def estimate_spread(price: float, atr: float | None) -> float:
    """Estimate the bid-ask spread (half-spread)."""
    if not cfg.ENABLE_SPREAD_SIM:
        return 0.0
    rate = cfg.SPREAD_BASE_RATE
    if atr and price > 0 and (atr / price) > 0.02:
        rate = cfg.SPREAD_VOLATILE_RATE
    return price * rate


def total_slippage(price: float, atr: float | None) -> float:
    """Total slippage = ATR adaptation + latency + spread/price."""
    base = calc_slippage(price, atr)
    latency = simulate_latency_slip()
    spread_ratio = estimate_spread(price, atr) / price if price > 0 else 0
    return base + latency + spread_ratio


def apply_slippage(
    price: float, slip: float, side: str, is_entry: bool
) -> float:
    """Apply directional slippage."""
    if side == "LONG":
        return price * (1 + slip) if is_entry else price * (1 - slip)
    else:
        return price * (1 - slip) if is_entry else price * (1 + slip)


# ──────────────────────────── Fees ────────────────────────────

def calc_fee(notional: float) -> float:
    """Taker fee."""
    return notional * cfg.TAKER_FEE_RATE


# ──────────────────────────── PnL integration ────────────────────────────

def calc_trade_pnl(
    direction: str,
    actual_entry: float,
    exit_price: float,
    quantity: float,
    atr: float | None,
    entry_fee: float,
    cumulative_funding: float = 0.0,
) -> dict:
    """
    Final trade PnL.

    ⚠️ Slippage is applied **only on exit**.
    The entry side is not recalculated because it was already fixed as actual_entry at fill time.
    This prevents double application and avoids inconsistencies caused by different random latency components on entry and exit.
    """
    exit_slip = total_slippage(exit_price, atr)

    if direction == "LONG":
        actual_exit = exit_price * (1 - exit_slip)
        gross = (actual_exit - actual_entry) * quantity
    else:
        actual_exit = exit_price * (1 + exit_slip)
        gross = (actual_entry - actual_exit) * quantity

    exit_fee = calc_fee(actual_exit * quantity)
    total_fee = entry_fee + exit_fee

    notional = actual_entry * quantity
    net = gross - total_fee - cumulative_funding
    pct = (net / notional * 100) if notional > 0 else 0.0

    return {
        "gross_pnl": gross,
        "total_fee": total_fee,
        "funding_paid": cumulative_funding,
        "net_pnl": net,
        "pnl_pct": pct,
        "actual_entry": actual_entry,
        "actual_exit": actual_exit,
    }


# ──────────────────────────── Funding-fee management ────────────────────────────

class FundingFeeManager:
    """Realtime funding-fee management."""

    def __init__(self):
        self._current_rates: dict[str, float] = {}  # symbol → rate
        self._history: list[tuple[str, float]] = []  # (timestamp, rate)
        self._timestamps: list[str] = []
        self._cumulative: list[float] = [0.0]

    def init_from_history(self, data: dict[str, list[tuple[str, float]]]):
        """
        Load historical funding-fee data (multi-coin).

        - BTC history → keep for cumulative calculation (calc_cumulative)
        - Latest funding rate for all coins → initialize _current_rates
          (prevents fallback immediately after cold start, before the first _funding_rate_loop)
        """
        # BTC cumulative funding table (for retroactive calculation)
        btc_data = data.get(cfg.BTC, [])
        self._history = sorted(btc_data, key=lambda x: x[0])
        self._timestamps = [t for t, _ in self._history]
        cum = 0.0
        self._cumulative = [0.0]
        for _, r in self._history:
            cum += r
            self._cumulative.append(cum)

        # Initialize _current_rates with the latest funding rates for all coins
        for symbol, history in data.items():
            if history:
                self._current_rates[symbol] = history[-1][1]

        total = sum(len(v) for v in data.values())
        log.info(
            "펀딩비 히스토리 로드: %d건 (%d코인), 초기 rates: %s",
            total, len(data),
            {s: f"{r:.4%}" for s, r in self._current_rates.items()},
        )

    def update_rate(self, symbol: str, rate: float):
        """Update funding rates in realtime."""
        self._current_rates[symbol] = rate

    def get_rate(self, symbol: str) -> float:
        return self._current_rates.get(symbol, cfg.FUNDING_FALLBACK_RATE)

    def calc_funding_fee(
        self, direction: str, position_value: float, symbol: str
    ) -> float:
        """Cost of a single funding event."""
        rate = self.get_rate(symbol)
        raw = position_value * rate
        return raw if direction == "LONG" else -raw

    def calc_cumulative(
        self, direction: str, entry_time: str, exit_time: str, notional: float
    ) -> float:
        """Cumulative funding fee from entry to exit (ported from optimizer FundingLookup)."""
        if not self._timestamps:
            return 0.0
        ci = self._cumulative[bisect.bisect_right(self._timestamps, entry_time)]
        co = self._cumulative[bisect.bisect_right(self._timestamps, exit_time)]
        nr = co - ci
        return notional * nr if direction == "LONG" else notional * (-nr)
