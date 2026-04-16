"""
Trailing-stop breakout strategy — #1, #3, #4, #7.

Implements SRS Section 4.2 / 4.4 / 4.5 / 4.8.
  #1: LB7,  SL ATR×2.0, trail 3.0, risk 0.5%, time 20 bars, 5x,  both directions
  #3: LB20, SL ATR×2.0, trail 3.0, risk 0.5%, time 50 bars, 1x,  long only
  #4: LB50, SL ATR×2.0, trail 3.0, risk 0.5%, time 50 bars, 1x,  long only
  #7: LB10, SL ATR×1.5, trail 3.0, risk 0.5%, time 50 bars, 1x,  long only

Entry condition (long):
  current close > highest price over the recent lookback bars (breakout)
  AND no existing position

Entry condition (short, only for both-direction strategies):
  current close < lowest price over the recent lookback bars (breakdown)
  AND no existing position

Exits are handled by the simulator:
  1. Liquidation (real-time)
  2. Stop-loss: ATR × sl_atr_mult
  3. Trailing stop: activated after profit exceeds ATR × trail_atr_mult,
     then stop = extreme price - ATR × sl_atr_mult
  4. Time exit: after time_limit_bars bars elapse
"""
from __future__ import annotations

from strategies.base_strategy import BaseStrategy, Signal
from config import StrategyConfig
from indicators.hub import IndicatorHub
from indicators.core import ATR, Donchian
from data.candle_builder import Candle


class TrailingBreakoutStrategy(BaseStrategy):

    def __init__(self, config: StrategyConfig, hub: IndicatorHub):
        super().__init__(config, hub)
        self._lookback = self.params["lookback"]
        self._sl_atr_mult = self.params["sl_atr_mult"]
        self._trail_atr_mult = self.params["trail_atr_mult"]
        self._risk_per_trade = self.params["risk_per_trade"]
        self._time_limit = self.params["time_limit_bars"]
        self._atr_period = self.params.get("atr_period", 14)

        # Register required indicators for each symbol
        for sym in self.symbols:
            iset = hub.get_set(sym, self.timeframe)
            iset.get_or_create(
                f"atr_{self._atr_period}", lambda: ATR(self._atr_period)
            )
            iset.get_or_create(
                f"don_{self._lookback}", lambda: Donchian(self._lookback)
            )

    def on_candle_close(
        self,
        symbol: str,
        candle: Candle,
        has_position: bool,
        position_side: str | None,
    ) -> Signal | None:
        if has_position:
            return None

        close = candle["close"]
        atr = self.hub.get_atr(symbol, self.timeframe, self._atr_period)
        don_high, don_low = self.hub.get_donchian(
            symbol, self.timeframe, self._lookback
        )

        if atr is None or don_high is None or don_low is None:
            return None

        # ── Long: close > lookback-bar high (breakout) ──
        if close > don_high:
            stop = close - atr * self._sl_atr_mult
            return self._make_entry_signal(
                symbol=symbol,
                direction="LONG",
                candle=candle,
                atr=atr,
                stop_loss=stop,
                take_profit=None,  # Trailing strategies do not use a fixed TP
                time_limit=self._time_limit,
                trail_atr_mult=self._trail_atr_mult,
                sl_atr_mult=self._sl_atr_mult,
            )

        # ── Short: close < lookback-bar low (both-direction strategies only) ──
        if self.direction == "both" and close < don_low:
            stop = close + atr * self._sl_atr_mult
            return self._make_entry_signal(
                symbol=symbol,
                direction="SHORT",
                candle=candle,
                atr=atr,
                stop_loss=stop,
                take_profit=None,
                time_limit=self._time_limit,
                trail_atr_mult=self._trail_atr_mult,
                sl_atr_mult=self._sl_atr_mult,
            )

        return None
