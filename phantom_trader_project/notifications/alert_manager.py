"""
Notification queue + batch sending + scheduler — SRS Section 7.2/7.3.

- 5-second batching during bursts
- 5-minute cooldown for system warnings
- Daily report (every day at 00:00 UTC)
- Weekly report (every Sunday at 00:00 UTC)
"""
from __future__ import annotations
import asyncio
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

from notifications.telegram_bot import TelegramBot
from notifications import formatters
from utils.logger import log
from utils.time_utils import utc_now, utc_now_iso
import config as cfg

if TYPE_CHECKING:
    from execution.position import Account, Position
    from execution.simulator import TradeRecord, ExecutionSimulator
    from storage.database import Database


class AlertManager:
    """
    Notification manager.

    - Entry/exit alerts: send immediately (5-second batching during bursts)
    - Forced liquidation: send immediately
    - System warnings: 5-minute cooldown
    - Daily/weekly reports: scheduler
    """

    def __init__(self, bot: TelegramBot):
        self._bot = bot
        self._queue: deque[str] = deque()
        self._system_last_sent: datetime | None = None
        self._batch_task: asyncio.Task | None = None
        self._running = False

        # Strategy name mapping (injected externally)
        self._strategy_names: dict[str, str] = {}

    def set_strategy_names(self, names: dict[str, str]):
        self._strategy_names = names

    # ────── Start/stop ──────

    async def start(self):
        self._running = True
        self._batch_task = asyncio.create_task(self._batch_loop())
        log.info("AlertManager 시작")

    async def stop(self):
        self._running = False
        if self._batch_task:
            self._batch_task.cancel()
            try:
                await self._batch_task
            except asyncio.CancelledError:
                pass
        # Flush the remaining queue
        await self._flush_queue()

    # ────── Notification API (called externally) ──────

    async def notify_entry(self, strategy_id: str, pos: "Position"):
        """Position-entry alert."""
        name = self._strategy_names.get(strategy_id, strategy_id)
        text = formatters.format_entry(name, pos)
        self._queue.append(text)

    async def notify_exit(self, trade: "TradeRecord"):
        """Position-exit alert."""
        name = self._strategy_names.get(trade.strategy_id, trade.strategy_id)
        text = formatters.format_exit(name, trade)
        if "강제청산" in trade.exit_reason:
            # Urgent: send immediately
            await self._bot.send(text)
        else:
            self._queue.append(text)

    async def notify_system(self, message: str):
        """System warning (5-minute cooldown)."""
        now = utc_now()
        if self._system_last_sent:
            elapsed = (now - self._system_last_sent).total_seconds()
            if elapsed < cfg.TG_COOLDOWN_SYSTEM_SEC:
                return
        self._system_last_sent = now
        text = formatters.format_system_alert(message)
        await self._bot.send(text)

    # ────── Batch-send loop ──────

    async def _batch_loop(self):
        """Batch-send queued notifications every 5 seconds."""
        while self._running:
            await asyncio.sleep(cfg.TG_BATCH_DELAY_SEC)
            await self._flush_queue()

    async def _flush_queue(self):
        """Send all messages currently in the queue."""
        messages = []
        while self._queue:
            messages.append(self._queue.popleft())

        if not messages:
            return

        # Merge and send together during bursts (considering the 4000-character limit)
        combined = ""
        for msg in messages:
            if len(combined) + len(msg) + 2 > 3900:
                await self._bot.send(combined)
                combined = ""
            combined += msg + "\n\n"

        if combined.strip():
            await self._bot.send(combined.strip())

    # ────── Daily report ──────

    async def send_daily_report(
        self,
        accounts: dict[str, "Account"],
        simulator: "ExecutionSimulator",
        db: "Database",
        btc_price: float | None = None,
    ):
        """Send the daily report."""
        today = utc_now().strftime("%Y-%m-%d")

        # Active position count
        active_count = 0
        for sid in accounts:
            active_count += len(simulator.get_all_positions(sid))

        # Today's trade count
        today_trades = await db.get_today_trades(today)

        text = formatters.format_daily_report(
            today, accounts, self._strategy_names,
            btc_price=btc_price,
            active_count=active_count,
            today_trades=today_trades,
        )
        await self._bot.send(text)
        log.info("일일 리포트 전송 완료")

    # ────── Scheduler loop ──────

    async def report_scheduler_loop(
        self,
        get_accounts,
        get_simulator,
        get_db,
        get_btc_price,
    ):
        """
        Daily/weekly report scheduler.
        Sends the daily report every day at 00:00 UTC.
        Sends the weekly report every Sunday at 00:00 UTC (daily + weekly stats).
        """
        while self._running:
            now = utc_now()
            # Wait until the next 00:00 UTC
            tomorrow = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=5, microsecond=0
            )
            wait_sec = (tomorrow - now).total_seconds()
            log.debug("다음 리포트: %.0f초 후 (%s)", wait_sec, tomorrow.isoformat())

            await asyncio.sleep(wait_sec)

            try:
                accounts = get_accounts()
                sim = get_simulator()
                db = get_db()
                btc = await get_btc_price()

                await self.send_daily_report(accounts, sim, db, btc)

                # If it is Sunday, send the weekly report too
                if utc_now().weekday() == 6:
                    await self._send_weekly_extra(accounts)

            except Exception as e:
                log.error("리포트 전송 실패: %s", e)

    async def _send_weekly_extra(self, accounts: dict[str, "Account"]):
        """Additional weekly report (performance statistics)."""
        text = formatters.format_performance(accounts, self._strategy_names)
        header = "📊 주간 리포트 — 성과 요약\n\n"
        await self._bot.send(header + text)
        log.info("주간 리포트 전송 완료")
