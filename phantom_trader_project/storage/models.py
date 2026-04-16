"""
DB row ↔ domain object mapping.

Conversions between Position, Account objects and SQLite rows.
"""
from __future__ import annotations
import json

from execution.position import Position, Account
from execution.simulator import TradeRecord
import config as cfg


# ──────────────────────────── Account ↔ strategy_state ────────────────────────────

def account_to_state_row(acct: Account, sc: cfg.StrategyConfig) -> dict:
    """Account → strategy_state upsert parameters."""
    return {
        "strategy_id": acct.strategy_id,
        "config_json": json.dumps(sc.params, ensure_ascii=False),
        "balance": acct.balance,
        "peak_balance": acct.peak_balance,
        "total_pnl": acct.total_pnl,
        "total_fees": acct.total_fees,
        "total_funding": acct.total_funding,
        "wins": acct.wins,
        "losses": acct.losses,
        "liquidations": acct.liquidations,
        "max_dd": acct.max_drawdown_pct,
        "gross_wins_sum": acct.gross_wins_sum,
        "gross_losses_sum": acct.gross_losses_sum,
        "is_active": acct.is_active,
    }


def state_row_to_account(row: dict, initial_capital: float) -> Account:
    """Restore Account from a strategy_state row."""
    acct = Account(
        strategy_id=row["strategy_id"],
        balance=row["account_balance"],
        initial_capital=initial_capital,
        is_active=bool(row["is_active"]),
    )
    acct.total_pnl = row.get("total_pnl", 0)
    acct.total_fees = row.get("total_fees", 0)
    acct.total_funding = row.get("total_funding", 0)
    acct.wins = row.get("wins", 0)
    acct.losses = row.get("losses", 0)
    acct.liquidations = row.get("liquidations", 0)
    acct.peak_balance = row.get("peak_balance", acct.balance)
    acct.max_drawdown_pct = row.get("max_drawdown_pct", 0)
    acct.gross_wins_sum = row.get("gross_wins_sum", 0) or 0
    acct.gross_losses_sum = row.get("gross_losses_sum", 0) or 0
    acct.total_trades = acct.wins + acct.losses
    return acct


# ──────────────────────────── Position ↔ positions ────────────────────────────

def position_row_to_obj(row: dict) -> Position:
    """Restore a Position object from a positions row."""
    return Position(
        strategy_id=row["strategy_id"],
        symbol=row["symbol"],
        side=row["side"],
        entry_price=row["entry_price"],
        actual_entry=row["actual_entry"],
        entry_time=row["entry_time"],
        quantity=row["quantity"],
        leverage=row["leverage"],
        stop_loss=row.get("stop_loss"),
        take_profit=row.get("take_profit"),
        trailing_stop=row.get("trailing_stop"),
        trailing_activated=bool(row.get("trailing_activated", 0)),
        highest_price=row.get("highest_price", row["entry_price"]),
        lowest_price=row.get("lowest_price", row["entry_price"]),
        candle_count=row.get("candle_count", 0),
        time_limit=row.get("time_limit"),
        liquidation_price=row.get("liquidation_price", 0),
        margin_used=row.get("margin_used", 0),
        notional=row.get("notional", 0),
        entry_fee=row["entry_fee"],
        cumulative_funding=row.get("cumulative_funding", 0),
        unrealized_pnl=row.get("unrealized_pnl", 0),
        trail_atr_mult=row.get("trail_atr_mult", 0),
        sl_atr_mult=row.get("sl_atr_mult", 0),
        entry_atr=row.get("entry_atr", 0),
    )


# ──────────────────────────── Candle ────────────────────────────

def candle_row_to_dict(row: dict) -> dict:
    """Convert a candle_buffer row to a candle dict."""
    return {
        "open_time": row["open_time"],
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "volume": row["volume"],
        "timestamp_utc": "",  # Convert with ms_to_utc_iso if needed
    }
