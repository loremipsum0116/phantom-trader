"""
Composite trail + RSI strategy — #10.

Implements SRS Section 4.11.
  Timeframe: 1H
  LB: 48 bars
  SL: ATR × 1.0
  Trailing: ATR × 4.0
  RSI filter: long RSI > 55, short RSI < 45
  Risk: 1.5%, time exit 36 bars, 20x, both directions

Entry condition (long):
  close > highest price over the last 48 bars AND RSI(14) > 55

Entry condition (short):
  close < lowest price over the last 48 bars AND RSI(14) < 45

⚠️ 20x leverage + ATR×1.0 stop-loss = the highest risk among all strategies.
"""
from __future__ import annotations

from strategies.base_strategy import BaseStrategy, Signal
from config import StrategyConfig
from indicators.hub import IndicatorHub
from indicators.core import ATR, RSI, Donchian
from data.candle_builder import Candle


class CompositeStrategy(BaseStrategy):

    def __init__(self, config: StrategyConfig, hub: IndicatorHub):
        super().__init__(config, hub)
        self._lookback = self.params["lookback"]
        self._sl_atr_mult = self.params["sl_atr_mult"]
        self._trail_atr_mult = self.params["trail_atr_mult"]
        self._rsi_period = self.params["rsi_period"]
        self._rsi_long_min = self.params["rsi_long_min"]
        self._rsi_short_max = self.params["rsi_short_max"]
        self._risk_per_trade = self.params["risk_per_trade"]
        self._time_limit = self.params["time_limit_bars"]
        self._atr_period = self.params.get("atr_period", 14)

        for sym in self.symbols:
            iset = hub.get_set(sym, self.timeframe)
            iset.get_or_create(f"atr_{self._atr_period}", lambda: ATR(self._atr_period))
            iset.get_or_create(f"rsi_{self._rsi_period}", lambda: RSI(self._rsi_period))
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
        rsi = self.hub.get_rsi(symbol, self.timeframe, self._rsi_period)
        don_high, don_low = self.hub.get_donchian(symbol, self.timeframe, self._lookback)

        if atr is None or rsi is None or don_high is None or don_low is None:
            return None

        # ── Long: close > 48-bar high AND RSI > 55 ──
        if close > don_high and rsi > self._rsi_long_min:
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

        # ── Short: close < 48-bar low AND RSI < 45 ──
        if self.direction == "both" and close < don_low and rsi < self._rsi_short_max:
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