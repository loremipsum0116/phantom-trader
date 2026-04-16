"""
Multi-coin portfolio strategy — #8.

Implements SRS Section 4.9.
  Timeframe: 4H
  Universe: BTC, ETH, SOL, XRP, DOGE, ADA, AVAX, DOT, LINK (9 coins)
  Allocation per coin: $1,111.11 (= $10,000 ÷ 9)
  Trailing logic: same as #7 (LB10, ATR×1.5, trail 3.0)
  Leverage: 1x, long only

Key differences:
  - Manage 9 coins as independent positions
  - Compute ATR and breakout levels independently per coin
  - Allow simultaneous multiple positions (up to 9)
  - No capital transfer across coins (fixed equal allocation)

Because the simulator manages positions by the strategy_id + symbol
combination, on_candle_close generates signals independently when it is
called for each symbol.
"""
from __future__ import annotations

from strategies.base_strategy import BaseStrategy, Signal
from config import StrategyConfig
from indicators.hub import IndicatorHub
from indicators.core import ATR, Donchian
from data.candle_builder import Candle


class MultiCoinStrategy(BaseStrategy):

    def __init__(self, config: StrategyConfig, hub: IndicatorHub):
        super().__init__(config, hub)
        self._lookback = self.params["lookback"]
        self._sl_atr_mult = self.params["sl_atr_mult"]
        self._trail_atr_mult = self.params["trail_atr_mult"]
        self._risk_per_trade = self.params["risk_per_trade"]
        self._time_limit = self.params["time_limit_bars"]
        self._atr_period = self.params.get("atr_period", 14)
        self._per_coin_capital = self.params["per_coin_capital"]

        # Register indicators for all target coins
        for sym in self.symbols:
            iset = hub.get_set(sym, self.timeframe)
            iset.get_or_create(f"atr_{self._atr_period}", lambda: ATR(self._atr_period))
            iset.get_or_create(f"don_{self._lookback}", lambda: Donchian(self._lookback))

    def on_candle_close(
        self,
        symbol: str,
        candle: Candle,
        has_position: bool,
        position_side: str | None,
    ) -> Signal | None:
        """
        Called independently for each coin.
        Skip if a position already exists for that coin.
        """
        if has_position:
            return None

        close = candle["close"]
        atr = self.hub.get_atr(symbol, self.timeframe, self._atr_period)
        don_high, don_low = self.hub.get_donchian(symbol, self.timeframe, self._lookback)

        if atr is None or don_high is None or don_low is None:
            return None

        # ── Long only: close > highest price over the lookback bars ──
        if close > don_high:
            stop = close - atr * self._sl_atr_mult
            return self._make_entry_signal(
                symbol=symbol,
                direction="LONG",
                candle=candle,
                atr=atr,
                stop_loss=stop,
                take_profit=None,
                time_limit=self._time_limit,
                trail_atr_mult=self._trail_atr_mult,
                sl_atr_mult=self._sl_atr_mult,
            )

        return None