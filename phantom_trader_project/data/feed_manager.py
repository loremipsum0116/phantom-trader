"""Integrated manager for multi-coin/timeframe streams."""
from __future__ import annotations
import asyncio
from collections import defaultdict
from typing import Callable, Awaitable

from data.candle_builder import CandleBuilder, Candle
from data.rest_client import BinanceRestClient, load_symbol_specs
from utils.logger import log
import config as cfg


class FeedManager:
    """
    Integrated management of the data pipeline:
    - Load historical data on cold start
    - 1m WS events → CandleBuilder → strategy engine
    """

    def __init__(
        self,
        on_candle_close: Callable[[str, str, Candle], Awaitable[None]],
        on_price_update: Callable[[str, float, str], Awaitable[None]],
    ):
        self._on_candle_close = on_candle_close
        self._on_price_update = on_price_update
        self._rest = BinanceRestClient()
        self._builder = CandleBuilder(on_candle_close, on_price_update)

        # Completed candle buffer: {(symbol, tf): [candle, ...]}
        self.candle_history: dict[tuple[str, str], list[Candle]] = defaultdict(list)

        self._running = True

    async def start(self):
        await self._rest.start()

    async def stop(self):
        self._running = False
        await self._rest.stop()

    # ────── Cold Start ──────
    async def load_history(self):
        """Load history for all symbols × timeframes. Dynamic symbol spec loading comes first."""
        # Step 0: dynamically load symbol specs from exchangeInfo
        all_symbols = set()
        for sc in cfg.STRATEGIES:
            all_symbols.update(sc.symbols)
        await load_symbol_specs(self._rest, list(all_symbols))

        # Step 1: load historical candles
        for symbol in all_symbols:
            for tf in cfg.TIMEFRAMES:
                candles = await self._rest.fetch_history(
                    symbol, tf, cfg.COLD_START_DAYS
                )
                key = (symbol, tf)
                self.candle_history[key] = candles
                log.info(
                    "히스토리 로드 완료: %s %s = %d봉", symbol, tf, len(candles)
                )

    async def load_funding_history(self) -> dict[str, list[tuple[str, float]]]:
        """Funding fee history for all coins (used to initialize _current_rates during cold start)."""
        result: dict[str, list[tuple[str, float]]] = {}
        for symbol in cfg.MULTI_COINS:
            data = await self._rest.fetch_funding_history(
                symbol, cfg.COLD_START_DAYS
            )
            if data:
                result[symbol] = data
        return result

    # ────── Real-time processing ──────
    async def on_ws_message(self, data: dict):
        """Forward WS events to CandleBuilder."""
        if data.get("e") == "kline":
            await self._builder.on_kline_event(data)

    def store_candle(self, symbol: str, tf: str, candle: Candle):
        """Append a completed candle to the buffer (for indicator calculation)."""
        key = (symbol, tf)
        self.candle_history[key].append(candle)
        # Memory management: keep only the most recent 500 candles
        if len(self.candle_history[key]) > 500:
            self.candle_history[key] = self.candle_history[key][-500:]

    def get_candles(self, symbol: str, tf: str) -> list[Candle]:
        return self.candle_history.get((symbol, tf), [])

    # ────── Real-time funding fees ──────
    async def fetch_current_funding(self, symbol: str) -> float | None:
        return await self._rest.fetch_funding_rate(symbol)

    async def fetch_ticker(self, symbol: str) -> float | None:
        return await self._rest.fetch_ticker_price(symbol)

    # ────── Periodic symbol spec refresh ──────
    async def exchange_info_refresh_loop(self):
        """Refresh exchangeInfo every 24 hours (to handle Binance spec changes)."""
        while self._running:
            await asyncio.sleep(cfg.EXCHANGE_INFO_REFRESH_SEC)
            if not self._running:
                break
            all_symbols = set()
            for sc in cfg.STRATEGIES:
                all_symbols.update(sc.symbols)
            log.info("🔄 exchangeInfo 주기 갱신 시작...")
            await load_symbol_specs(self._rest, list(all_symbols))
