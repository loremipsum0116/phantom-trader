"""
Keltner Channel breakout strategy — #2.

Implements SRS Section 4.3.
  Timeframe: 1H
  KC: EMA(10) ± ATR(14) × 2.0
  Entry: close > upper (long) / close < lower (short)
  Exit: stop-loss (ATR-based), take-profit (3.0R), time (40 bars)
  Risk: 0.8%, leverage 3x, both directions

R-multiple TP calculation:
  Long:  TP = entry + (entry - SL) × tp_r_mult
  Short: TP = entry - (SL - entry) × tp_r_mult
  If the SL distance is 1R, TP is placed at the 3R level.
"""
from __future__ import annotations

from strategies.base_strategy import BaseStrategy, Signal
from config import StrategyConfig
from indicators.hub import IndicatorHub
from indicators.core import KeltnerChannel
from data.candle_builder import Candle


class KeltnerBreakoutStrategy(BaseStrategy):

    def __init__(self, config: StrategyConfig, hub: IndicatorHub):
        super().__init__(config, hub)
        self._kc_ema = self.params["kc_ema_period"]
        self._kc_mult = self.params["kc_atr_mult"]
        self._tp_r = self.params["tp_r_mult"]
        self._risk = self.params["risk_per_trade"]
        self._time_limit = self.params["time_limit_bars"]
        self._atr_period = self.params.get("atr_period", 14)

        for sym in self.symbols:
            iset = hub.get_set(sym, self.timeframe)
            iset.get_or_create(
                f"kc_{self._kc_ema}_{self._atr_period}_{self._kc_mult}",
                lambda: KeltnerChannel(self._kc_ema, self._atr_period, self._kc_mult),
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
        upper, middle, lower = self.hub.get_keltner(
            symbol, self.timeframe, self._kc_ema, self._atr_period, self._kc_mult
        )
        atr = self.hub.get_atr(symbol, self.timeframe, self._atr_period)

        if upper is None or lower is None or atr is None:
            return None

        # ── Long: close > upper band ──
        if close > upper:
            sl_distance = atr  # ATR-based stop-loss (the KC strategy uses ATR×1.0 stop-loss)
            stop = close - sl_distance
            tp = close + sl_distance * self._tp_r  # 3.0R
            return self._make_entry_signal(
                symbol=symbol,
                direction="LONG",
                candle=candle,
                atr=atr,
                stop_loss=stop,
                take_profit=tp,
                time_limit=self._time_limit,
                trail_atr_mult=0.0,  # Keltner uses no trailing stop, fixed TP
                sl_atr_mult=1.0,
            )

        # ── Short: close < lower band ──
        if self.direction == "both" and close < lower:
            sl_distance = atr
            stop = close + sl_distance
            tp = close - sl_distance * self._tp_r
            return self._make_entry_signal(
                symbol=symbol,
                direction="SHORT",
                candle=candle,
                atr=atr,
                stop_loss=stop,
                take_profit=tp,
                time_limit=self._time_limit,
                trail_atr_mult=0.0,
                sl_atr_mult=1.0,
            )

        return None