"""Forced-liquidation engine — based on Binance isolated margin + tiered MMR."""
from __future__ import annotations
import math

import config as cfg
from execution.fee_model import calc_slippage, total_slippage


def get_symbol_spec(symbol: str) -> dict:
    """
    Retrieve symbol specs (runtime first → fallback second).
    Use the fallback when exchangeInfo is not loaded yet or the symbol is unavailable.
    """
    spec = cfg.SYMBOL_SPECS.get(symbol)
    if spec:
        return spec
    return cfg.SYMBOL_SPECS_FALLBACK.get(
        symbol, {"tick_size": 0.01, "lot_size": 0.001, "min_notional": 5.0}
    )


def get_maintenance_margin_rate(notional: float) -> float:
    """Maintenance-margin rate by position notional value."""
    for threshold, rate in cfg.MAINT_TIERS:
        if notional <= threshold:
            return rate
    return 0.10


def calc_liquidation_price(
    entry_price: float, leverage: int, direction: str, notional: float
) -> float | None:
    """
    Forced-liquidation price (isolated margin).
    Returns None when the position cannot be opened (immediate-liquidation condition).
    """
    mmr = get_maintenance_margin_rate(notional)
    margin_ratio = 1.0 / leverage - mmr
    if margin_ratio <= 0:
        return None
    if direction == "LONG":
        return entry_price * (1 - margin_ratio)
    else:
        return entry_price * (1 + margin_ratio)


# ──────────────────────────── Exit checks (gap-through) ────────────────────────────

def check_exit_long(
    open_: float, close: float, high: float, low: float,
    stop: float, tp: float | None, liq: float | None,
    trailing_stop: float | None = None, is_trailing_active: bool = False,
) -> tuple[str, float]:
    """Long-position exit checks — production priority order."""
    eff_stop = stop
    if trailing_stop is not None and is_trailing_active:
        eff_stop = max(stop, trailing_stop)

    exit_tag = "트레일링" if (trailing_stop and eff_stop == trailing_stop) else "손절"
    safe_liq = liq if liq is not None else 0.0

    if open_ <= safe_liq:
        return "강제청산", safe_liq
    if open_ <= eff_stop:
        return exit_tag, open_  # gap-through
    if low <= eff_stop:
        return exit_tag, eff_stop
    if tp is not None and high >= tp:
        return "익절", tp
    return "", 0.0


def check_exit_short(
    open_: float, close: float, high: float, low: float,
    stop: float, tp: float | None, liq: float | None,
    trailing_stop: float | None = None, is_trailing_active: bool = False,
) -> tuple[str, float]:
    """Short-position exit checks."""
    eff_stop = stop
    if trailing_stop is not None and is_trailing_active:
        eff_stop = min(stop, trailing_stop)

    exit_tag = "트레일링" if (trailing_stop and eff_stop == trailing_stop) else "손절"
    safe_liq = liq if liq is not None else float("inf")

    if open_ >= safe_liq:
        return "강제청산", safe_liq
    if open_ >= eff_stop:
        return exit_tag, open_  # gap-through
    if high >= eff_stop:
        return exit_tag, eff_stop
    if tp is not None and low <= tp:
        return "익절", tp
    return "", 0.0


# ──────────────────────────── Position-opening validation ────────────────────────────

def validate_position_opening(
    equity: float,
    entry_price: float,
    stop_price: float,
    direction: str,
    leverage: int,
    risk_pct: float,
    atr: float | None,
    symbol: str = "BTCUSDT",
) -> dict | None:
    """
    Validate before opening a position (ported from optimizer _try_open).
    Returns dict or None (validation failure).
    """
    if entry_price <= 0:
        return None

    slip = total_slippage(entry_price, atr)
    if direction == "LONG":
        actual_entry = entry_price * (1 + slip)
        actual_stop = stop_price * (1 - slip)
    else:
        actual_entry = entry_price * (1 - slip)
        actual_stop = stop_price * (1 + slip)

    stop_distance = abs(actual_entry - actual_stop)
    if stop_distance <= 0:
        return None

    margin = min(
        equity * risk_pct / (stop_distance / actual_entry * leverage),
        equity * cfg.MAX_MARGIN_RATIO,
    )

    spec = get_symbol_spec(symbol)
    min_notional = spec["min_notional"]
    lot_size = spec["lot_size"]
    tick_size = spec["tick_size"]

    if margin < min_notional / leverage:
        return None

    qty = margin * leverage / actual_entry
    qty = math.floor(qty / lot_size) * lot_size  # Round to lot size
    if qty <= 0:
        return None

    notional = actual_entry * qty
    if notional < min_notional:
        return None

    liq_price = calc_liquidation_price(actual_entry, leverage, direction, notional)
    if liq_price is None:
        return None

    # Round by tick_size precision (direction-aware)
    if tick_size > 0:
        if direction == "LONG":
            liq_price = math.floor(liq_price / tick_size) * tick_size
        else:
            liq_price = math.ceil(liq_price / tick_size) * tick_size

    if direction == "LONG" and liq_price >= stop_price:
        return None
    if direction == "SHORT" and liq_price <= stop_price:
        return None

    return {
        "entry_price": entry_price,
        "actual_entry": actual_entry,
        "quantity": qty,
        "margin": margin,
        "stop_loss": stop_price,
        "liquidation_price": liq_price,
        "direction": direction,
        "atr": atr,
        "notional": notional,
    }
