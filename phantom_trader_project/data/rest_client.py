"""Binance REST API client (historical candles + funding fees)."""
from __future__ import annotations
import asyncio
from typing import Any

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

from utils.logger import log
from utils.time_utils import ms_to_utc_iso
import config as cfg


class BinanceRestClient:
    def __init__(self):
        self._client = None  # httpx.AsyncClient when available

    async def start(self):
        if httpx is None:
            log.warning("httpx 미설치 — REST API 비활성화")
            return
        self._client = httpx.AsyncClient(
            base_url=cfg.BINANCE_REST_FUTURES, timeout=15.0
        )

    async def stop(self):
        if self._client:
            await self._client.aclose()

    # ────── Historical candles ──────
    async def fetch_klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        limit: int = cfg.REST_KLINE_LIMIT,
    ) -> list[dict]:
        """Call Binance /api/v3/klines."""
        assert self._client
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "startTime": start_ms,
            "limit": limit,
        }
        try:
            r = await self._client.get("/api/v3/klines", params=params)
            r.raise_for_status()
            candles = []
            for k in r.json():
                candles.append(
                    {
                        "open_time": k[0],
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                        "close_time": k[6],
                        "timestamp_utc": ms_to_utc_iso(k[0]),
                    }
                )
            return candles
        except Exception as e:
            log.error("REST klines error (%s %s): %s", symbol, interval, e)
            return []

    async def fetch_history(
        self, symbol: str, interval: str, days: int
    ) -> list[dict]:
        """
        Collect historical candles for the given number of days via pagination.
        """
        from datetime import datetime, timezone

        all_candles: list[dict] = []
        end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        cur = end_ms - days * 86_400_000
        log.info("📊 %s %s 히스토리 수집 (%d일)...", symbol, interval, days)

        while cur < end_ms:
            batch = await self.fetch_klines(symbol, interval, cur)
            if not batch:
                break
            all_candles.extend(batch)
            cur = batch[-1]["open_time"] + 1
            if len(batch) < cfg.REST_KLINE_LIMIT:
                break
            await asyncio.sleep(cfg.REST_REQUEST_DELAY)

        # Remove duplicates and sort
        seen: set[int] = set()
        unique = []
        for c in all_candles:
            if c["open_time"] not in seen:
                seen.add(c["open_time"])
                unique.append(c)
        unique.sort(key=lambda x: x["open_time"])

        # Remove incomplete candles (if close_time is after the current time, the candle is still in progress)
        if unique and unique[-1].get("close_time", 0) > end_ms:
            removed = unique.pop()
            log.debug(
                "   미완성 캔들 제거: %s %s open=%s",
                symbol, interval, removed["timestamp_utc"],
            )
        log.info(
            "   ✅ %s %s: %d봉 (%s ~ %s)",
            symbol,
            interval,
            len(unique),
            unique[0]["timestamp_utc"][:10] if unique else "N/A",
            unique[-1]["timestamp_utc"][:10] if unique else "N/A",
        )
        return unique

    # ────── Funding fees ──────
    async def fetch_funding_rate(self, symbol: str) -> float | None:
        """Funding rate — not supported on binance.vision, so fallback is used."""
        return None

    async def fetch_funding_history(
        self, symbol: str, days: int
    ) -> list[tuple[str, float]]:
        """Funding fee history — not supported on binance.vision, so fallback is used."""
        log.info("💰 %s 펀딩비: binance.vision 미지원 → fallback 0.01%%", symbol)
        return []

    async def fetch_ticker_price(self, symbol: str) -> float | None:
        """Fetch the current price (for WS cross-checking)."""
        assert self._client
        try:
            r = await self._client.get(
                "/api/v3/ticker/price", params={"symbol": symbol}
            )
            r.raise_for_status()
            return float(r.json()["price"])
        except Exception:
            return None

    # ────── Dynamic symbol spec loading ──────
    async def fetch_exchange_info(self, symbols: list[str]) -> dict[str, dict]:
        """
        Parse symbol-specific tick_size, lot_size, and min_notional from
        GET /api/v3/exchangeInfo.

        Binance may change these values without prior notice, so they must be
        refreshed dynamically at server startup and every 24 hours afterward.

        Returns:
            {symbol: {"tick_size": float, "lot_size": float, "min_notional": float}}
        """
        assert self._client
        result: dict[str, dict] = {}
        try:
            r = await self._client.get("/api/v3/exchangeInfo")
            r.raise_for_status()
            data = r.json()

            target = set(s.upper() for s in symbols)
            for info in data.get("symbols", []):
                sym = info.get("symbol", "")
                if sym not in target:
                    continue
                spec: dict[str, float] = {
                    "tick_size": 0.01,
                    "lot_size": 0.001,
                    "min_notional": 5.0,
                }
                for f in info.get("filters", []):
                    ft = f.get("filterType", "")
                    if ft == "PRICE_FILTER":
                        spec["tick_size"] = float(f.get("tickSize", spec["tick_size"]))
                    elif ft == "LOT_SIZE":
                        spec["lot_size"] = float(f.get("stepSize", spec["lot_size"]))
                    elif ft == "MIN_NOTIONAL":
                        spec["min_notional"] = float(
                            f.get("notional", f.get("minNotional", spec["min_notional"]))
                        )
                result[sym] = spec
                log.debug(
                    "   %s: tick=%.6f lot=%.6f min_notional=%.1f",
                    sym, spec["tick_size"], spec["lot_size"], spec["min_notional"],
                )
            log.info("✅ exchangeInfo 로드: %d/%d 심볼", len(result), len(target))
        except Exception as e:
            log.error("❌ exchangeInfo 실패: %s — fallback 사용", e)
        return result


async def load_symbol_specs(client: BinanceRestClient, symbols: list[str]) -> None:
    """
    Dynamically load symbol specs via exchangeInfo and apply them to
    cfg.SYMBOL_SPECS. If that fails, fall back to
    cfg.SYMBOL_SPECS_FALLBACK.
    """
    live_specs = await client.fetch_exchange_info(symbols)
    if live_specs:
        cfg.SYMBOL_SPECS.update(live_specs)
        log.info("심볼 스펙 동적 로드 완료 (%d심볼)", len(live_specs))
    else:
        log.warning("심볼 스펙 동적 로드 실패 — fallback 사용")

    # Fill in missing symbols using the fallback values
    for sym in symbols:
        if sym not in cfg.SYMBOL_SPECS:
            fallback = cfg.SYMBOL_SPECS_FALLBACK.get(sym, {
                "tick_size": 0.01, "lot_size": 0.001, "min_notional": 5.0,
            })
            cfg.SYMBOL_SPECS[sym] = fallback
            log.warning("   %s: fallback 사용 (tick=%.6f)", sym, fallback["tick_size"])
