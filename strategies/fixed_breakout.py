"""
Fixed-TP breakout strategy — #5.

Implements SRS Section 4.6.
  Timeframe: 4H
  LB: 10 bars
  Entry: close > LB10 high (long) / close < LB10 low (short)
  SL: ATR × 1.0 (tight)
  TP: 4.0R (fixed ratio)
  Risk: 1.5%, time exit 10 bars, 15x, both directions

⚠️ Risk note: 15x + ATR×1.0 stop-loss carries very high liquidation risk.
"""
from __future__ import annotations

from strategies.base_strategy import BaseStrategy, Signal
from config import StrategyConfig
from indicators.hub import IndicatorHub
from indicators.core import ATR, Donchian
from data.candle_builder import Candle


class FixedTPBreakoutStrategy(BaseStrategy):

    def __init__(self, config: StrategyConfig, hub: IndicatorHub):
        super().__init__(config, hub)
        self._lookback = self.params["lookback"]
        self._sl_atr_mult = self.params["sl_atr_mult"]
        self._tp_r = self.params["tp_r_mult"]
        self._risk = self.params["risk_per_trade"]
        self._time_limit = self.params["time_limit_bars"]
        self._atr_period = self.params.get("atr_period", 14)

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
        if has_position:
            return None

        close = candle["close"]
        atr = self.hub.get_atr(symbol, self.timeframe, self._atr_period)
        don_high, don_low = self.hub.get_donchian(symbol, self.timeframe, self._lookback)

        if atr is None or don_high is None or don_low is None:
            return None

        # ── Long ──
        if close > don_high:
            sl_dist = atr * self._sl_atr_mult
            stop = close - sl_dist
            tp = close + sl_dist * self._tp_r
            return self._make_entry_signal(
                symbol=symbol,
                direction="LONG",
                candle=candle,
                atr=atr,
                stop_loss=stop,
                take_profit=tp,
                time_limit=self._time_limit,
                trail_atr_mult=0.0,  # No trailing stop, fixed TP
                sl_atr_mult=self._sl_atr_mult,
            )

        # ── Short ──
        if self.direction == "both" and close < don_low:
            sl_dist = atr * self._sl_atr_mult
            stop = close + sl_dist
            tp = close - sl_dist * self._tp_r
            return self._make_entry_signal(
                symbol=symbol,
                direction="SHORT",
                candle=candle,
                atr=atr,
                stop_loss=stop,
                take_profit=tp,
                time_limit=self._time_limit,
                trail_atr_mult=0.0,
                sl_atr_mult=self._sl_atr_mult,
            )

        return None