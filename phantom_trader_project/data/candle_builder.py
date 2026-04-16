"""1m raw data → 1H/4H candle aggregation."""
from __future__ import annotations
import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Awaitable

from utils.logger import log
from utils.time_utils import (
    candle_boundary_1h,
    candle_boundary_4h,
    ms_to_utc_iso,
)
from utils.health_check import mark_candle_received
import config as cfg

Candle = dict  # {"open_time","open","high","low","close","volume","timestamp_utc"}


@dataclass
class CandleAccumulator:
    """Accumulate 1m candles to build higher-timeframe candles."""
    open_time: int = 0
    open: float = 0.0
    high: float = 0.0
    low: float = float("inf")
    close: float = 0.0
    volume: float = 0.0
    count: int = 0

    def update(self, o: float, h: float, l: float, c: float, v: float):
        if self.count == 0:
            self.open = o
            self.high = h
            self.low = l
        else:
            self.high = max(self.high, h)
            self.low = min(self.low, l)
        self.close = c
        self.volume += v
        self.count += 1

    def to_candle(self) -> Candle:
        return {
            "open_time": self.open_time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "timestamp_utc": ms_to_utc_iso(self.open_time),
        }

    def reset(self, new_open_time: int):
        self.open_time = new_open_time
        self.open = 0.0
        self.high = 0.0
        self.low = float("inf")
        self.close = 0.0
        self.volume = 0.0
        self.count = 0


class CandleBuilder:
    """
    Receive 1m kline events and build 1H/4H candles.

    Call the on_candle_close callback when a candle is completed.
    Forward incomplete candles through the on_price_update callback.
    """

    def __init__(
        self,
        on_candle_close: Callable[[str, str, Candle], Awaitable[None]],
        on_price_update: Callable[[str, float, str], Awaitable[None]],
    ):
        self._on_candle_close = on_candle_close
        self._on_price_update = on_price_update

        # {(symbol, tf): CandleAccumulator}
        self._accumulators: dict[tuple[str, str], CandleAccumulator] = {}

        # Duplicate prevention: {(symbol): last_1m_open_time}
        self._last_1m: dict[str, int] = defaultdict(int)

    def _get_acc(self, symbol: str, tf: str) -> CandleAccumulator:
        key = (symbol, tf)
        if key not in self._accumulators:
            self._accumulators[key] = CandleAccumulator()
        return self._accumulators[key]

    def _boundary_fn(self, tf: str):
        return candle_boundary_1h if tf == "1h" else candle_boundary_4h

    def _tf_minutes(self, tf: str) -> int:
        return cfg.TF_MINUTES[tf]

    async def on_kline_event(self, event: dict):
        """
        Handle a Binance kline WS event.
        event = {"e":"kline", "s":"BTCUSDT", "k":{...}}
        """
        k = event.get("k", {})
        symbol = k.get("s", "")
        is_final = k.get("x", False)
        open_time_ms = k.get("t", 0)
        o, h, l, c, v = (
            float(k.get("o", 0)),
            float(k.get("h", 0)),
            float(k.get("l", 0)),
            float(k.get("c", 0)),
            float(k.get("v", 0)),
        )

        if not symbol or open_time_ms == 0:
            return

        # Duplicate check
        if open_time_ms <= self._last_1m[symbol] and not is_final:
            return

        # Incomplete candle → real-time price event
        if not is_final:
            ts = ms_to_utc_iso(k.get("T", open_time_ms))
            await self._on_price_update(symbol, c, ts)
            return

        # 1m candle confirmed
        self._last_1m[symbol] = open_time_ms
        mark_candle_received(ms_to_utc_iso(open_time_ms))

        # Aggregate for each timeframe
        for tf in cfg.TIMEFRAMES:
            boundary_fn = self._boundary_fn(tf)
            boundary = boundary_fn(open_time_ms)
            tf_ms = self._tf_minutes(tf) * 60 * 1000

            acc = self._get_acc(symbol, tf)

            # Start a new candle?
            if acc.count == 0:
                acc.open_time = boundary

            # If the boundary changes, finalize the previous candle
            if boundary != acc.open_time and acc.count > 0:
                completed = acc.to_candle()
                log.debug(
                    "🕯️ %s %s 캔들 완성: O=%.2f H=%.2f L=%.2f C=%.2f",
                    symbol, tf, completed["open"], completed["high"],
                    completed["low"], completed["close"],
                )
                await self._on_candle_close(symbol, tf, completed)
                acc.reset(boundary)

            acc.update(o, h, l, c, v)

    async def inject_history(self, symbol: str, tf: str, candles: list[Candle]):
        """Cold start: inject historical candles (for indicator initialization). No events are emitted."""
        log.info("📥 %s %s 히스토리 %d봉 주입", symbol, tf, len(candles))
        # Historical candles are already complete, so store them directly
        # Handled in feed_manager
