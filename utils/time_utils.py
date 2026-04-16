"""UTC/KST conversion and candle time utilities."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().strftime("%Y-%m-%dT%H:%M:%S")


def utc_to_kst(dt: datetime) -> datetime:
    return dt.astimezone(KST)


def utc_to_kst_str(iso: str) -> str:
    dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")


def ms_to_utc_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


def iso_to_ms(iso: str) -> int:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def candle_boundary_4h(ts_ms: int) -> int:
    """4H candle boundary start (UTC): 00,04,08,12,16,20."""
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    hour = (dt.hour // 4) * 4
    boundary = dt.replace(hour=hour, minute=0, second=0, microsecond=0)
    return int(boundary.timestamp() * 1000)


def candle_boundary_1h(ts_ms: int) -> int:
    """1H candle boundary start."""
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    boundary = dt.replace(minute=0, second=0, microsecond=0)
    return int(boundary.timestamp() * 1000)


def is_funding_time(iso_utc: str) -> bool:
    """Check whether it is 00:00, 08:00, or 16:00 UTC."""
    dt = datetime.fromisoformat(iso_utc)
    return dt.hour in (0, 8, 16) and dt.minute == 0
