"""Common strategy interface (ABC)."""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from config import StrategyConfig
from data.candle_builder import Candle
from indicators.hub import IndicatorHub
from execution.simulator import Signal


class BaseStrategy(ABC):
    """
    Base class for all strategies.

    Strategies generate signals only. Order execution is handled by
    ExecutionSimulator at the next-bar open.
    Strategies do not manage positions or calculate PnL internally.
    """

    def __init__(self, config: StrategyConfig, hub: IndicatorHub):
        self.cfg = config
        self.hub = hub
        self.strategy_id = config.strategy_id
        self.strategy_name = config.strategy_name
        self.timeframe = config.timeframe
        self.symbols = config.symbols
        self.direction = config.direction
        self.leverage = config.leverage
        self.params = config.params

    @abstractmethod
    def on_candle_close(
        self,
        symbol: str,
        candle: Candle,
        has_position: bool,
        position_side: str | None,
    ) -> Signal | None:
        """
        Called when a candle close is confirmed.

        Args:
            symbol: Symbol
            candle: Completed candle
            has_position: Whether a position is currently open (provided by the simulator)
            position_side: "LONG"|"SHORT" if a position exists, otherwise None

        Returns:
            Signal (entry signal) or None

        Note:
            Exits are handled by the simulator (SL/TP/trailing/time/forced liquidation).
            Strategies generate entry signals only.
            However, if a strategy requires signal-based exits, such as MA crossover,
            it may return signal_type="EXIT".
        """
        ...

    def _make_entry_signal(
        self,
        symbol: str,
        direction: str,
        candle: Candle,
        atr: float,
        stop_loss: float,
        take_profit: float | None = None,
        time_limit: int | None = None,
        trail_atr_mult: float = 0.0,
        sl_atr_mult: float = 0.0,
    ) -> Signal:
        """Helper for creating entry signals."""
        return Signal(
            strategy_id=self.strategy_id,
            symbol=symbol,
            direction=direction,
            signal_price=candle["close"],
            signal_time=candle["timestamp_utc"],
            atr_at_signal=atr,
            signal_type="ENTRY",
            stop_loss=stop_loss,
            take_profit=take_profit,
            time_limit=time_limit or self.params.get("time_limit_bars"),
            trail_atr_mult=trail_atr_mult,
            sl_atr_mult=sl_atr_mult,
        )

    def _make_exit_signal(self, symbol: str, candle: Candle, atr: float) -> Signal:
        """Signal-based exit (e.g. MA crossover death cross)."""
        return Signal(
            strategy_id=self.strategy_id,
            symbol=symbol,
            direction="",  # Direction is unnecessary because this is an exit
            signal_price=candle["close"],
            signal_time=candle["timestamp_utc"],
            atr_at_signal=atr,
            signal_type="EXIT",
        )