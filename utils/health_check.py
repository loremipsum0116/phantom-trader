"""Health checks + self-diagnostics."""
from __future__ import annotations
import asyncio
import os
import psutil  # type: ignore[import-untyped]
from dataclasses import dataclass, field
from datetime import datetime, timezone

from utils.logger import log
from utils.time_utils import utc_now_iso
import config as cfg


@dataclass
class HealthStatus:
    ws_connected: bool = False
    last_candle_time: str = ""
    memory_mb: float = 0.0
    uptime_sec: float = 0.0
    start_time: str = field(default_factory=utc_now_iso)
    errors_last_hour: int = 0

    @property
    def is_healthy(self) -> bool:
        if not self.ws_connected:
            return False
        if self.last_candle_time:
            last = datetime.fromisoformat(self.last_candle_time).replace(
                tzinfo=timezone.utc
            )
            gap = (datetime.now(timezone.utc) - last).total_seconds()
            if gap > cfg.CANDLE_TIMEOUT_SEC:
                return False
        if self.memory_mb > cfg.MEMORY_WARN_MB:
            return False
        return True

    def summary(self) -> str:
        return (
            f"WS: {'🟢' if self.ws_connected else '🔴'} | "
            f"Last candle: {self.last_candle_time or 'N/A'} | "
            f"Mem: {self.memory_mb:.0f}MB | "
            f"Up: {self.uptime_sec / 3600:.1f}h | "
            f"Errors(1h): {self.errors_last_hour}"
        )


_status = HealthStatus()


def get_health() -> HealthStatus:
    proc = psutil.Process(os.getpid())
    _status.memory_mb = proc.memory_info().rss / 1024 / 1024
    start = datetime.fromisoformat(_status.start_time).replace(tzinfo=timezone.utc)
    _status.uptime_sec = (datetime.now(timezone.utc) - start).total_seconds()
    return _status


def mark_ws_connected(connected: bool) -> None:
    _status.ws_connected = connected


def mark_candle_received(iso_utc: str) -> None:
    _status.last_candle_time = iso_utc


def increment_error() -> None:
    _status.errors_last_hour += 1


async def health_check_loop(alert_fn=None):
    """Periodic health check. If alert_fn is provided, call it on unhealthy status."""
    while True:
        await asyncio.sleep(cfg.HEARTBEAT_INTERVAL_SEC)
        h = get_health()
        if not h.is_healthy:
            log.warning("Health check FAIL: %s", h.summary())
            if alert_fn:
                await alert_fn(f"⚠️ 시스템 경고\n{h.summary()}")
        else:
            log.debug("Health OK: %s", h.summary())
