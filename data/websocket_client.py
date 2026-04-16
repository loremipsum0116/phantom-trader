"""Binance WebSocket client (automatic reconnection)."""
from __future__ import annotations
import asyncio
import json
from typing import Callable, Awaitable

try:
    import websockets  # type: ignore[import-untyped]
    from websockets.exceptions import ConnectionClosed  # type: ignore[import-untyped]
    _HAS_WS = True
except ImportError:
    _HAS_WS = False

from utils.logger import log
from utils.health_check import mark_ws_connected
import config as cfg


class BinanceWSClient:
    def __init__(self, on_message: Callable[[dict], Awaitable[None]]):
        self._on_message = on_message
        self._ws = None
        self._running = False
        self._reconnect_delay = cfg.WS_RECONNECT_BASE_SEC

    def _build_url(self) -> str:
        streams = "/".join(cfg.WS_STREAMS)
        return f"{cfg.BINANCE_WS_FUTURES}?streams={streams}"

    async def start(self):
        if not _HAS_WS:
            log.error("websockets 미설치 — WS 비활성화")
            return
        self._running = True
        while self._running:
            try:
                await self._connect()
            except Exception as e:
                mark_ws_connected(False)
                log.warning(
                    "WS 연결 실패: %s — %ds 후 재시도",
                    e,
                    self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, cfg.WS_RECONNECT_MAX_SEC
                )

    async def _connect(self):
        url = self._build_url()
        log.info("WS 연결 시도: %s", url[:80])

        async with websockets.connect(  # type: ignore[attr-defined]
            url, ping_interval=20, ping_timeout=10, max_size=2**20
        ) as ws:
            self._ws = ws
            self._reconnect_delay = cfg.WS_RECONNECT_BASE_SEC
            mark_ws_connected(True)
            log.info("✅ WS 연결 성공 (%d 스트림)", len(cfg.WS_STREAMS))

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    if "data" in msg:
                        await self._on_message(msg["data"])
                except json.JSONDecodeError:
                    log.warning("WS JSON 디코드 실패: %s", raw[:100])
                except Exception as e:
                    log.error("WS 메시지 처리 에러: %s", e)

        # Reconnect even after a normal shutdown
        mark_ws_connected(False)
        if self._running:
            log.warning("WS 연결 종료 — 재연결...")

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()
