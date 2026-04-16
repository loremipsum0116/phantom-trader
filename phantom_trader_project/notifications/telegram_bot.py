"""
Telegram bot — SRS Section 7.

Notification delivery + 8 command handlers.
Based on the python-telegram-bot 20.x async API.
Falls back to direct httpx calls in production when python-telegram-bot is unavailable.
"""
from __future__ import annotations
import asyncio
import json
from typing import TYPE_CHECKING, Callable, Awaitable

from utils.logger import log
import config as cfg

try:
    from telegram import Update, Bot
    from telegram.ext import (
        Application, CommandHandler, ContextTypes,
    )
    _HAS_PTB = True
except ImportError:
    _HAS_PTB = False
    log.warning("python-telegram-bot 미설치 — httpx 폴백 사용")

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

if TYPE_CHECKING:
    from execution.position import Account
    from execution.simulator import ExecutionSimulator
    from storage.database import Database
    from notifications.formatters import *


class TelegramBot:
    """Telegram notification delivery + command handling."""

    def __init__(self):
        self._token = cfg.TELEGRAM_BOT_TOKEN
        self._chat_id = cfg.TELEGRAM_CHAT_ID
        self._app = None  # python-telegram-bot Application
        self._http: httpx.AsyncClient | None = None
        self._enabled = bool(self._token and self._chat_id)

        # External injection (configured in main.py)
        self._get_accounts: Callable[[], dict[str, "Account"]] | None = None
        self._get_simulator: Callable[[], "ExecutionSimulator"] | None = None
        self._get_db: Callable[[], "Database"] | None = None
        self._strategy_names: dict[str, str] = {}

        if not self._enabled:
            log.warning("텔레그램 미설정 (토큰/챗ID 없음)")

    def set_providers(
        self,
        get_accounts: Callable,
        get_simulator: Callable,
        get_db: Callable,
        strategy_names: dict[str, str],
    ):
        """Inject runtime dependencies from main.py."""
        self._get_accounts = get_accounts
        self._get_simulator = get_simulator
        self._get_db = get_db
        self._strategy_names = strategy_names

    # ────── Start/stop ──────

    async def start(self):
        if not self._enabled:
            return

        if _HAS_PTB:
            self._app = (
                Application.builder()
                .token(self._token)
                .build()
            )
            self._register_handlers()
            await self._app.initialize()
            await self._app.start()
            # Run polling as a separate task
            log.info("텔레그램 봇 시작 (python-telegram-bot)")
        elif _HAS_HTTPX:
            self._http = httpx.AsyncClient(timeout=10)
            log.info("텔레그램 봇 시작 (httpx 폴백)")
        else:
            log.error("텔레그램 전송 불가: httpx, python-telegram-bot 모두 없음")
            self._enabled = False

    async def stop(self):
        if self._app:
            await self._app.stop()
            await self._app.shutdown()
        if self._http:
            await self._http.aclose()

    async def start_polling(self):
        """Poll for incoming commands (separate task)."""
        if self._app:
            await self._app.updater.start_polling(drop_pending_updates=True)
            log.info("텔레그램 polling 시작")

    # ────── Message sending ──────

    async def send(self, text: str, parse_mode: str = "HTML"):
        """Send a message."""
        if not self._enabled:
            return

        # Telegram message length limit (4096 chars)
        if len(text) > 4000:
            text = text[:4000] + "\n... (잘림)"

        try:
            if self._app:
                await self._app.bot.send_message(
                    chat_id=self._chat_id, text=text, parse_mode=None,
                )
            elif self._http:
                url = f"https://api.telegram.org/bot{self._token}/sendMessage"
                await self._http.post(url, json={
                    "chat_id": self._chat_id,
                    "text": text,
                })
        except Exception as e:
            log.error("텔레그램 전송 실패: %s", e)

    # ────── Register command handlers ──────

    def _register_handlers(self):
        if not self._app:
            return
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("strategy", self._cmd_strategy))
        self._app.add_handler(CommandHandler("trades", self._cmd_trades))
        self._app.add_handler(CommandHandler("equity", self._cmd_equity))
        self._app.add_handler(CommandHandler("performance", self._cmd_performance))
        self._app.add_handler(CommandHandler("health", self._cmd_health))
        self._app.add_handler(CommandHandler("pause", self._cmd_pause))
        self._app.add_handler(CommandHandler("resume", self._cmd_resume))

    # ────── /status ──────

    async def _cmd_status(self, update: "Update", ctx: "ContextTypes.DEFAULT_TYPE"):
        from notifications.formatters import format_status
        accounts = self._get_accounts() if self._get_accounts else {}
        sim = self._get_simulator() if self._get_simulator else None
        positions = {}
        if sim:
            for sid in accounts:
                pos_list = sim.get_all_positions(sid)
                if pos_list:
                    positions[sid] = pos_list
        text = format_status(accounts, self._strategy_names, positions)
        await update.message.reply_text(text)

    # ────── /strategy N ──────

    async def _cmd_strategy(self, update: "Update", ctx: "ContextTypes.DEFAULT_TYPE"):
        from notifications.formatters import format_strategy_detail
        args = ctx.args or []
        if not args:
            await update.message.reply_text("사용법: /strategy <번호> (1~10)")
            return

        try:
            n = int(args[0])
        except ValueError:
            await update.message.reply_text("숫자를 입력하세요: /strategy 1")
            return

        if n < 1 or n > len(cfg.STRATEGIES):
            await update.message.reply_text(f"1~{len(cfg.STRATEGIES)} 범위")
            return

        sc = cfg.STRATEGIES[n - 1]
        accounts = self._get_accounts() if self._get_accounts else {}
        acct = accounts.get(sc.strategy_id)
        if not acct:
            await update.message.reply_text("계좌 없음")
            return

        sim = self._get_simulator() if self._get_simulator else None
        positions = sim.get_all_positions(sc.strategy_id) if sim else []

        db = self._get_db() if self._get_db else None
        recent = await db.get_recent_trades(sc.strategy_id, 5) if db else []

        text = format_strategy_detail(
            sc.strategy_name, acct, positions, recent,
        )
        await update.message.reply_text(text)

    # ────── /trades N ──────

    async def _cmd_trades(self, update: "Update", ctx: "ContextTypes.DEFAULT_TYPE"):
        args = ctx.args or []
        if not args:
            await update.message.reply_text("사용법: /trades <번호>")
            return

        try:
            n = int(args[0])
            sc = cfg.STRATEGIES[n - 1]
        except (ValueError, IndexError):
            await update.message.reply_text(f"1~{len(cfg.STRATEGIES)} 범위")
            return

        db = self._get_db() if self._get_db else None
        if not db:
            await update.message.reply_text("DB 미연결")
            return

        trades = await db.get_recent_trades(sc.strategy_id, 10)
        if not trades:
            await update.message.reply_text(f"{sc.strategy_name}: 거래 없음")
            return

        from notifications.formatters import fmt_price, fmt_pct
        lines = [f"📜 {sc.strategy_name} 최근 {len(trades)}건", ""]
        for t in trades:
            lines.append(
                f"  {t['side']} {t['exit_reason']}: "
                f"{fmt_price(t['net_pnl'])} ({fmt_pct(t['net_pnl_pct'])})"
                f" | {t['exit_time'][:10]}"
            )
        await update.message.reply_text("\n".join(lines))

    # ────── /equity ──────

    async def _cmd_equity(self, update: "Update", ctx: "ContextTypes.DEFAULT_TYPE"):
        from notifications.formatters import fmt_price, fmt_pct
        accounts = self._get_accounts() if self._get_accounts else {}
        lines = ["📈 자산 곡선", ""]
        total = 0
        for sid, acct in sorted(accounts.items()):
            name = self._strategy_names.get(sid, sid)[:14]
            lines.append(f"  {name}: {fmt_price(acct.balance)} ({fmt_pct(acct.return_pct)})")
            total += acct.balance
        lines.extend(["", f"합계: {fmt_price(total)}"])
        await update.message.reply_text("\n".join(lines))

    # ────── /performance ──────

    async def _cmd_performance(self, update: "Update", ctx: "ContextTypes.DEFAULT_TYPE"):
        from notifications.formatters import format_performance
        accounts = self._get_accounts() if self._get_accounts else {}
        text = format_performance(accounts, self._strategy_names)
        await update.message.reply_text(text)

    # ────── /health ──────

    async def _cmd_health(self, update: "Update", ctx: "ContextTypes.DEFAULT_TYPE"):
        from notifications.formatters import format_health
        from utils.health_check import get_health
        h = get_health()
        await update.message.reply_text(format_health(h.summary()))

    # ────── /pause N ──────

    async def _cmd_pause(self, update: "Update", ctx: "ContextTypes.DEFAULT_TYPE"):
        args = ctx.args or []
        if not args:
            await update.message.reply_text("사용법: /pause <번호>")
            return
        try:
            n = int(args[0])
            sc = cfg.STRATEGIES[n - 1]
        except (ValueError, IndexError):
            await update.message.reply_text(f"1~{len(cfg.STRATEGIES)} 범위")
            return
        accounts = self._get_accounts() if self._get_accounts else {}
        acct = accounts.get(sc.strategy_id)
        if acct:
            acct.is_active = False
            await update.message.reply_text(f"⏸️ {sc.strategy_name} 일시정지")
        else:
            await update.message.reply_text("계좌 없음")

    # ────── /resume N ──────

    async def _cmd_resume(self, update: "Update", ctx: "ContextTypes.DEFAULT_TYPE"):
        args = ctx.args or []
        if not args:
            await update.message.reply_text("사용법: /resume <번호>")
            return
        try:
            n = int(args[0])
            sc = cfg.STRATEGIES[n - 1]
        except (ValueError, IndexError):
            await update.message.reply_text(f"1~{len(cfg.STRATEGIES)} 범위")
            return
        accounts = self._get_accounts() if self._get_accounts else {}
        acct = accounts.get(sc.strategy_id)
        if acct:
            acct.is_active = True
            await update.message.reply_text(f"▶️ {sc.strategy_name} 재개")
        else:
            await update.message.reply_text("계좌 없음")
