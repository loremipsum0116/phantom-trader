"""Position and virtual-account management."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

import config as cfg
from execution.fee_model import calc_fee


@dataclass
class Position:
    """Active position."""

    strategy_id: str
    symbol: str
    side: str                       # "LONG" | "SHORT"
    entry_price: float
    actual_entry: float             # Effective entry price with slippage applied
    entry_time: str
    quantity: float
    leverage: int
    stop_loss: float
    take_profit: Optional[float] = None
    trailing_stop: Optional[float] = None
    trailing_activated: bool = False
    highest_price: float = 0.0      # Highest price while the position is open (long)
    lowest_price: float = float("inf")  # Lowest price while the position is open (short)
    candle_count: int = 0           # Elapsed candle count
    time_limit: Optional[int] = None  # Maximum holding candle count
    liquidation_price: float = 0.0
    margin_used: float = 0.0
    notional: float = 0.0

    # Cost tracking
    entry_fee: float = 0.0
    cumulative_funding: float = 0.0
    unrealized_pnl: float = 0.0

    # Trailing parameters
    trail_atr_mult: float = 0.0     # Trailing ATR multiplier
    sl_atr_mult: float = 0.0        # Stop-loss ATR multiplier
    entry_atr: float = 0.0          # ATR at entry time

    @property
    def position_value(self) -> float:
        """Current notional value (for unrealized PnL calculation)."""
        return self.quantity * self.entry_price

    def update_unrealized(self, current_price: float):
        """Update unrealized PnL."""
        if self.side == "LONG":
            self.unrealized_pnl = (current_price - self.actual_entry) * self.quantity
        else:
            self.unrealized_pnl = (self.actual_entry - current_price) * self.quantity
        # Subtract costs
        est_exit_fee = calc_fee(current_price * self.quantity)
        self.unrealized_pnl -= (self.entry_fee + est_exit_fee + self.cumulative_funding)

    def update_extremes(self, price: float):
        """Update trailing extremes."""
        if self.side == "LONG":
            self.highest_price = max(self.highest_price, price)
        else:
            self.lowest_price = min(self.lowest_price, price)


@dataclass
class Account:
    """Virtual account per strategy."""

    strategy_id: str
    balance: float
    initial_capital: float
    is_active: bool = True

    # Statistics
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_fees: float = 0.0
    total_funding: float = 0.0
    peak_balance: float = 0.0
    max_drawdown_pct: float = 0.0
    liquidations: int = 0
    gross_wins_sum: float = 0.0     # Sum gross_pnl of winning trades (for PF calculation)
    gross_losses_sum: float = 0.0   # Sum |gross_pnl| of losing trades (for PF calculation)

    def __post_init__(self):
        self.peak_balance = self.balance

    @property
    def return_pct(self) -> float:
        return ((self.balance - self.initial_capital) / self.initial_capital * 100
                if self.initial_capital > 0 else 0.0)

    @property
    def win_rate(self) -> float:
        return (self.wins / self.total_trades * 100
                if self.total_trades > 0 else 0.0)

    @property
    def profit_factor(self) -> float | None:
        """Profit Factor = total gross profit / |total gross loss|. Returns None (∞) when there are no losses."""
        if self.gross_losses_sum <= 0:
            return None if self.gross_wins_sum > 0 else 0.0
        return self.gross_wins_sum / self.gross_losses_sum

    def apply_trade(
        self, net_pnl: float, fee: float, funding: float,
        is_liquidation: bool = False, gross_pnl: float = 0.0,
    ):
        """Apply a trade result."""
        self.balance += net_pnl
        self.total_pnl += net_pnl
        self.total_fees += fee
        self.total_funding += funding
        self.total_trades += 1
        if net_pnl > 0:
            self.wins += 1
        else:
            self.losses += 1
        if is_liquidation:
            self.liquidations += 1

        # Gross classification for PF
        if gross_pnl > 0:
            self.gross_wins_sum += gross_pnl
        elif gross_pnl < 0:
            self.gross_losses_sum += abs(gross_pnl)

        # Update drawdown
        self.peak_balance = max(self.peak_balance, self.balance)
        if self.peak_balance > 0:
            dd = (self.peak_balance - self.balance) / self.peak_balance * 100
            self.max_drawdown_pct = max(self.max_drawdown_pct, dd)
