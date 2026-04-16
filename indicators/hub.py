"""Indicator integration manager — manages indicator instances for multiple coins/timeframes."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

from indicators.core import EMA, ATR, RSI, KeltnerChannel, Donchian
from data.candle_builder import Candle
from utils.logger import log


@dataclass
class IndicatorSet:
    """Indicator set for a single (symbol, tf)."""

    symbol: str
    timeframe: str

    # Base indicators
    atr_14: ATR = field(default_factory=lambda: ATR(14))

    # Strategy-specific indicators (created dynamically)
    _indicators: dict[str, Any] = field(default_factory=dict)

    def get_or_create(self, name: str, factory) -> Any:
        if name == "atr_14":
            return self.atr_14  # Reuse the field-level instance (prevents duplicate creation)
        if name not in self._indicators:
            self._indicators[name] = factory()
        return self._indicators[name]

    def update_all(self, candle: Candle):
        """Update all indicators using a candle."""
        h, l, c = candle["high"], candle["low"], candle["close"]
        self.atr_14.update(h, l, c)
        for ind in self._indicators.values():
            if hasattr(ind, "update"):
                if isinstance(ind, (ATR, KeltnerChannel)):
                    ind.update(h, l, c)
                elif isinstance(ind, Donchian):
                    ind.update(h, l)
                elif isinstance(ind, (EMA, RSI)):
                    ind.update(c)


class IndicatorHub:
    """
    Global indicator hub.
    Automatically creates and manages indicators requested by strategies.
    """

    def __init__(self):
        self._sets: dict[tuple[str, str], IndicatorSet] = {}

    def get_set(self, symbol: str, tf: str) -> IndicatorSet:
        key = (symbol, tf)
        if key not in self._sets:
            self._sets[key] = IndicatorSet(symbol=symbol, timeframe=tf)
        return self._sets[key]

    def on_candle(self, symbol: str, tf: str, candle: Candle):
        """Update the relevant indicator set when a candle is updated."""
        iset = self.get_set(symbol, tf)
        iset.update_all(candle)

    def init_from_history(self, symbol: str, tf: str, candles: list[Candle]):
        """Warm up indicators using historical candles."""
        iset = self.get_set(symbol, tf)
        for candle in candles:
            iset.update_all(candle)
        log.info(
            "📊 지표 초기화: %s %s (%d봉, ATR=%.2f)",
            symbol, tf, len(candles),
            iset.atr_14.value or 0.0,
        )

    # ────── Convenience accessors for strategy indicators ──────

    def get_atr(self, symbol: str, tf: str, period: int = 14) -> float | None:
        iset = self.get_set(symbol, tf)
        if period == 14:
            return iset.atr_14.value
        atr = iset.get_or_create(f"atr_{period}", lambda: ATR(period))
        return atr.value

    def get_ema(self, symbol: str, tf: str, period: int) -> float | None:
        iset = self.get_set(symbol, tf)
        ema = iset.get_or_create(f"ema_{period}", lambda: EMA(period))
        return ema.value

    def get_rsi(self, symbol: str, tf: str, period: int = 14) -> float | None:
        iset = self.get_set(symbol, tf)
        rsi = iset.get_or_create(f"rsi_{period}", lambda: RSI(period))
        return rsi.value

    def get_keltner(
        self, symbol: str, tf: str,
        ema_period: int = 10, atr_period: int = 14, mult: float = 2.0
    ) -> tuple[float | None, float | None, float | None]:
        iset = self.get_set(symbol, tf)
        kc = iset.get_or_create(
            f"kc_{ema_period}_{atr_period}_{mult}",
            lambda: KeltnerChannel(ema_period, atr_period, mult),
        )
        return kc.upper, kc.middle, kc.lower

    def get_donchian(
        self, symbol: str, tf: str, period: int
    ) -> tuple[float | None, float | None]:
        iset = self.get_set(symbol, tf)
        don = iset.get_or_create(f"don_{period}", lambda: Donchian(period))
        return don.upper, don.lower
