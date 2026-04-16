"""Technical indicator engine — EMA, ATR, RSI, Keltner."""
from __future__ import annotations
from collections import deque


# ──────────────────────────── EMA ────────────────────────────

class EMA:
    """Exponential moving average (incremental)."""

    def __init__(self, period: int):
        self.period = period
        self.multiplier = 2.0 / (period + 1)
        self._value: float | None = None
        self._warmup: list[float] = []

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, close: float) -> float | None:
        if self._value is None:
            self._warmup.append(close)
            if len(self._warmup) >= self.period:
                self._value = sum(self._warmup) / self.period
                self._warmup.clear()
            return self._value
        self._value = (close - self._value) * self.multiplier + self._value
        return self._value

    def reset(self):
        self._value = None
        self._warmup.clear()


# ──────────────────────────── ATR ────────────────────────────

class ATR:
    """Average True Range (incremental)."""

    def __init__(self, period: int = 14):
        self.period = period
        self._value: float | None = None
        self._prev_close: float | None = None
        self._warmup: list[float] = []

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._prev_close is not None:
            tr = max(
                high - low,
                abs(high - self._prev_close),
                abs(low - self._prev_close),
            )
        else:
            tr = high - low
        self._prev_close = close

        if self._value is None:
            self._warmup.append(tr)
            if len(self._warmup) >= self.period:
                self._value = sum(self._warmup) / self.period
                self._warmup.clear()
            return self._value
        self._value = (self._value * (self.period - 1) + tr) / self.period
        return self._value


# ──────────────────────────── RSI ────────────────────────────

class RSI:
    """Relative Strength Index (incremental)."""

    def __init__(self, period: int = 14):
        self.period = period
        self._value: float | None = None
        self._prev_close: float | None = None
        self._avg_gain: float = 0.0
        self._avg_loss: float = 0.0
        self._warmup_gains: list[float] = []
        self._warmup_losses: list[float] = []

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, close: float) -> float | None:
        if self._prev_close is None:
            self._prev_close = close
            return None

        delta = close - self._prev_close
        self._prev_close = close
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)

        if self._value is None:
            self._warmup_gains.append(gain)
            self._warmup_losses.append(loss)
            if len(self._warmup_gains) >= self.period:
                self._avg_gain = sum(self._warmup_gains) / self.period
                self._avg_loss = sum(self._warmup_losses) / self.period
                if self._avg_loss == 0:
                    self._value = 100.0
                else:
                    rs = self._avg_gain / self._avg_loss
                    self._value = 100.0 - 100.0 / (1 + rs)
                self._warmup_gains.clear()
                self._warmup_losses.clear()
            return self._value

        self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
        self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period

        if self._avg_loss == 0:
            self._value = 100.0
        else:
            rs = self._avg_gain / self._avg_loss
            self._value = 100.0 - 100.0 / (1 + rs)
        return self._value


# ──────────────────────────── Keltner Channel ────────────────────────────

class KeltnerChannel:
    """Keltner channel (EMA ± ATR×mult)."""

    def __init__(self, ema_period: int = 10, atr_period: int = 14, multiplier: float = 2.0):
        self.ema = EMA(ema_period)
        self.atr = ATR(atr_period)
        self.multiplier = multiplier
        self.upper: float | None = None
        self.middle: float | None = None
        self.lower: float | None = None

    def update(self, high: float, low: float, close: float):
        mid = self.ema.update(close)
        atr_val = self.atr.update(high, low, close)
        if mid is not None and atr_val is not None:
            self.middle = mid
            self.upper = mid + atr_val * self.multiplier
            self.lower = mid - atr_val * self.multiplier
        return self.upper, self.middle, self.lower


# ──────────────────────────── Donchian (Lookback High/Low) ────────────────

class Donchian:
    """Donchian channel (high/low over the most recent N candles).

    ⚠️ upper/lower are computed from the window **before** the current candle's
    OHLC is added. This matches the optimizer pattern `max(highs[i-LB:i])`
    and excludes the current candle during breakout evaluation.

    Sequence:
      1. Call update(current high, low)
      2. upper/lower = value based on the previous N candles
         (current candle excluded)
      3. Strategy: close > upper? → breakout
      4. Current-candle data is appended to the internal deque
         (included in the window for the next call)
    """

    def __init__(self, period: int):
        self.period = period
        self._highs: deque[float] = deque(maxlen=period)
        self._lows: deque[float] = deque(maxlen=period)
        self.upper: float | None = None
        self.lower: float | None = None

    def update(self, high: float, low: float):
        # ① Compute upper/lower using the existing window (current candle excluded)
        if len(self._highs) >= self.period:
            self.upper = max(self._highs)
            self.lower = min(self._lows)

        # ② Append the current candle (reflected in the next call's window)
        self._highs.append(high)
        self._lows.append(low)

        return self.upper, self.lower
