"""
Moving-average crossover strategy — #6, #9.

Implements SRS Section 4.7 / 4.10.
  #6 MA5/20:  EMA(5)/EMA(20), SL=4%, TP=2.5R, 1x, long only
  #9 MA10/30: EMA(10)/EMA(30), SL=3%, TP=3.0R, 1x, long only

Entry: golden cross (EMA_fast crosses above EMA_slow)
  - current bar: EMA(fast) > EMA(slow)
  - previous bar: EMA(fast) ≤ EMA(slow)

Exit:
  1. Stop-loss: -sl_pct% from entry
  2. Take-profit: tp_r_mult times the risk
  3. Dead cross: EMA(fast) < EMA(slow) → signal-based exit

Position sizing: fixed 10% of capital (not risk-based)
"""
from __future__ import annotations

from strategies.base_strategy import BaseStrategy, Signal
from config import StrategyConfig
from indicators.hub import IndicatorHub
from indicators.core import EMA
from data.candle_builder import Candle
from utils.logger import log


class MACrossoverStrategy(BaseStrategy):

    def __init__(self, config: StrategyConfig, hub: IndicatorHub):
        super().__init__(config, hub)
        self._fast_period = self.params["fast_ema"]
        self._slow_period = self.params["slow_ema"]
        self._sl_pct = self.params["sl_pct"]
        self._tp_r = self.params["tp_r_mult"]
        self._position_pct = self.params.get("position_pct", 0.10)

        # Track previous-bar EMA values (for crossover detection)
        # {symbol: (prev_fast, prev_slow)}
        self._prev_ema: dict[str, tuple[float | None, float | None]] = {}

        for sym in self.symbols:
            iset = hub.get_set(sym, self.timeframe)
            iset.get_or_create(f"ema_{self._fast_period}", lambda: EMA(self._fast_period))
            iset.get_or_create(f"ema_{self._slow_period}", lambda: EMA(self._slow_period))
            self._prev_ema[sym] = (None, None)

    def on_candle_close(
        self,
        symbol: str,
        candle: Candle,
        has_position: bool,
        position_side: str | None,
    ) -> Signal | None:
        close = candle["close"]

        cur_fast = self.hub.get_ema(symbol, self.timeframe, self._fast_period)
        cur_slow = self.hub.get_ema(symbol, self.timeframe, self._slow_period)

        prev_fast, prev_slow = self._prev_ema.get(symbol, (None, None))

        # Store current values as the "previous" values for the next bar
        self._prev_ema[symbol] = (cur_fast, cur_slow)

        if cur_fast is None or cur_slow is None:
            return None
        if prev_fast is None or prev_slow is None:
            return None

        # ── While holding a position: check dead cross → EXIT signal ──
        if has_position and position_side == "LONG":
            if cur_fast < cur_slow:
                log.debug(
                    "📉 %s 데드크로스: EMA(%d)=%.2f < EMA(%d)=%.2f",
                    self.strategy_id, self._fast_period, cur_fast,
                    self._slow_period, cur_slow,
                )
                atr = self.hub.get_atr(symbol, self.timeframe) or 0.0
                return self._make_exit_signal(symbol, candle, atr)
            return None

        # ── No position: check golden cross → ENTRY signal ──
        if has_position:
            return None

        # Golden cross: previous-bar fast ≤ slow AND current-bar fast > slow
        if prev_fast <= prev_slow and cur_fast > cur_slow:
            atr = self.hub.get_atr(symbol, self.timeframe) or close * 0.01
            sl_distance = close * self._sl_pct  # Percentage-based SL
            stop = close - sl_distance
            tp = close + sl_distance * self._tp_r

            log.debug(
                "📈 %s 골든크로스: EMA(%d)=%.2f > EMA(%d)=%.2f @ %.2f",
                self.strategy_id, self._fast_period, cur_fast,
                self._slow_period, cur_slow, close,
            )

            signal = self._make_entry_signal(
                symbol=symbol,
                direction="LONG",
                candle=candle,
                atr=atr,
                stop_loss=stop,
                take_profit=tp,
                time_limit=None,  # MA crossover uses no time exit; it exits on a dead cross
                trail_atr_mult=0.0,
                sl_atr_mult=0.0,
            )
            # Since risk_per_trade for the MA crossover strategy is based on position_pct
            # it requires separate handling in the simulator → mark it in params
            return signal

        return None