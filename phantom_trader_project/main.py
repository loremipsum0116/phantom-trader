"""
Phantom Trader — entry point.

SRS 2.3 async execution model + realism 4.0/4.1 startup/runtime loop.

Run concurrently with asyncio.gather:
  1. WebSocket data ingestion
  2. Funding-rate polling loop
  3. Health-check loop
  4. Daily/weekly report scheduler
  5. exchangeInfo refresh loop
  6. Telegram command polling
  7. Periodic DB save loop
"""
from __future__ import annotations
import asyncio
import signal
import sys
from pathlib import Path

# Add the project root to sys.path
sys.path.insert(0, str(Path(__file__).parent))

# Load .env first (required because config.py calls os.getenv at import time)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # Rely on external injection such as systemd EnvironmentFile

import config as cfg
from utils.logger import log
from utils.time_utils import utc_now_iso, utc_now
from utils.health_check import health_check_loop, get_health

from data.feed_manager import FeedManager
from data.websocket_client import BinanceWSClient
from data.candle_builder import Candle

from indicators.hub import IndicatorHub
from strategies.factory import create_strategies
from strategies.base_strategy import BaseStrategy

from execution.position import Account
from execution.fee_model import FundingFeeManager
from execution.simulator import ExecutionSimulator, Signal, TradeRecord

from storage.database import Database
from storage.state_manager import StateManager

from notifications.telegram_bot import TelegramBot
from notifications.alert_manager import AlertManager


class PhantomTrader:
    """Main orchestrator."""

    def __init__(self):
        # Core components
        self.db = Database()
        self.state_mgr: StateManager | None = None
        self.hub = IndicatorHub()
        self.funding = FundingFeeManager()
        self.feed: FeedManager | None = None
        self.simulator: ExecutionSimulator | None = None
        self.ws: BinanceWSClient | None = None

        # Strategies
        self.strategies: dict[str, BaseStrategy] = {}
        self.accounts: dict[str, Account] = {}

        # Notifications
        self.bot = TelegramBot()
        self.alert = AlertManager(self.bot)

        # Strategy name mapping
        self._strategy_names = {
            sc.strategy_id: sc.strategy_name for sc in cfg.STRATEGIES
        }

        # Shutdown flag
        self._running = False

    # ══════════════════════ Initialization (Cold Start / Restart) ══════════════════════

    async def initialize(self):
        """
        Server startup sequence (realism 4.0).

        0a. Connect DB + initialize schema
        0b. Restore strategy state / active positions
        0c. Dynamically load exchangeInfo
        0d. Load historical candles (REST, 14 days)
        0e. Save candle buffers to DB
        0f. Load funding-rate history
        0g. Backfill missing candles + scan retroactive liquidations
        0h. Warm up the indicator engine
        0i. Create strategy instances
        0j. Initialize simulator (inject restored positions)
        0k. Start Telegram bot
        """
        log.info("=" * 50)
        log.info("Phantom Trader 시작...")
        log.info("=" * 50)

        # 0a. DB
        await self.db.connect()
        self.state_mgr = StateManager(self.db)

        # 0b. Restore state
        self.accounts = await self.state_mgr.restore_accounts()
        restored_positions = await self.state_mgr.restore_positions()

        # 0c~0d. Feed Manager (exchangeInfo + history)
        self.feed = FeedManager(
            on_candle_close=self._on_candle_close,
            on_price_update=self._on_price_update,
        )
        await self.feed.start()
        await self.feed.load_history()

        # 0e. Save candle buffers to DB
        for (symbol, tf), candles in self.feed.candle_history.items():
            await self.state_mgr.save_candles_batch(symbol, tf, candles)

        # 0f. Funding-rate history
        fr_data = await self.feed.load_funding_history()
        self.funding.init_from_history(fr_data)

        # 0g. Retroactive liquidation scan
        if cfg.ENABLE_RETROACTIVE_SCAN and restored_positions:
            missed = await self._collect_missed_candles(restored_positions)
            retro_trades = await self.state_mgr.retroactive_liquidation_scan(
                restored_positions, self.accounts, missed,
                funding_mgr=self.funding,
            )
            for trade in retro_trades:
                log.warning(
                    "소급 청산 알림: %s %s → %s",
                    trade.strategy_id, trade.exit_reason, trade.exit_price,
                )

        # 0h. Create strategies (indicator instances must be registered first for warm-up)
        self.strategies = create_strategies(self.hub)

        # 0i. Warm up indicators (including Donchian/EMA/RSI/KC registered by strategies)
        for (symbol, tf), candles in self.feed.candle_history.items():
            self.hub.init_from_history(symbol, tf, candles)

        # 0j. Simulator
        self.simulator = ExecutionSimulator(
            accounts=self.accounts,
            funding_mgr=self.funding,
            on_trade=self._on_trade,
            on_entry=self._on_entry,
            on_liquidation=self._on_liquidation,
        )
        # Inject restored positions
        if restored_positions:
            self.simulator._positions = restored_positions

        # 0k. Telegram
        self.bot.set_providers(
            get_accounts=lambda: self.accounts,
            get_simulator=lambda: self.simulator,
            get_db=lambda: self.db,
            strategy_names=self._strategy_names,
        )
        self.alert.set_strategy_names(self._strategy_names)
        await self.bot.start()
        await self.alert.start()

        # Startup notification
        total = sum(a.balance for a in self.accounts.values())
        pos_count = sum(
            len(v) for v in (restored_positions or {}).values()
        )
        await self.bot.send(
            f"🚀 Phantom Trader 시작\n"
            f"전략: {len(self.strategies)}개\n"
            f"총 잔고: ${total:,.2f}\n"
            f"활성 포지션: {pos_count}개\n"
            f"⏰ {utc_now_iso()}"
        )

        log.info("초기화 완료 — 정상 운영 모드 진입")

    # ══════════════════════ Main loop ══════════════════════

    async def run(self):
        """Main execution loop."""
        self._running = True

        # WebSocket client
        self.ws = BinanceWSClient(on_message=self.feed.on_ws_message)

        tasks = [
            self.ws.start(),                       # WS data ingestion
            self._funding_rate_loop(),              # Funding-rate polling
            health_check_loop(self.alert.notify_system),  # Health check
            self.alert.report_scheduler_loop(       # Daily/weekly reports
                get_accounts=lambda: self.accounts,
                get_simulator=lambda: self.simulator,
                get_db=lambda: self.db,
                get_btc_price=lambda: self.feed.fetch_ticker(cfg.BTC),
            ),
            self.feed.exchange_info_refresh_loop(), # exchangeInfo 24h refresh
            self._db_save_loop(),                   # Periodic DB save
        ]

        # Telegram polling (when the bot is enabled)
        if self.bot._enabled:
            tasks.append(self.bot.start_polling())

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            log.info("메인 루프 취소됨")
        except Exception as e:
            log.error("메인 루프 에러: %s", e, exc_info=True)
        finally:
            await self.shutdown()

    # ══════════════════════ Event handlers ══════════════════════

    async def _on_candle_close(self, symbol: str, tf: str, candle: Candle):
        """
        Candle-close event.
        SRS realism 4.1 normal runtime loop, steps 1–2.
        """
        # 1. Update indicators
        self.hub.on_candle(symbol, tf, candle)

        # 2. Save candle buffer
        self.feed.store_candle(symbol, tf, candle)
        if self.state_mgr:
            await self.state_mgr.save_candle(symbol, tf, candle)

        # 3. Simulator — pending fills + exit checks
        trades = await self.simulator.on_candle_close(
            symbol, tf, candle, cfg.STRATEGIES,
        )

        # 4. Evaluate strategy signals
        for sc in cfg.STRATEGIES:
            if sc.timeframe != tf or symbol not in sc.symbols:
                continue
            strat = self.strategies.get(sc.strategy_id)
            if not strat:
                continue
            acct = self.accounts.get(sc.strategy_id)
            if not acct or not acct.is_active:
                continue

            has_pos = self.simulator.has_position(sc.strategy_id, symbol)
            pos = self.simulator.get_position(sc.strategy_id, symbol)
            pos_side = pos.side if pos else None

            signal = strat.on_candle_close(symbol, candle, has_pos, pos_side)
            if signal:
                self.simulator.register_signal(signal)

        # 5. Equity snapshot (every 4H candle)
        if tf == "4h" and self.state_mgr:
            ts = candle["timestamp_utc"]
            for sid, acct in self.accounts.items():
                pos_list = self.simulator.get_all_positions(sid)
                unrealized = sum(p.unrealized_pnl for p in pos_list)
                await self.state_mgr.save_equity_snapshot(
                    sid, ts, acct.balance, unrealized, acct.peak_balance,
                )

    async def _on_price_update(self, symbol: str, price: float, ts: str):
        """Realtime price update — forced liquidation + extremes."""
        if self.simulator:
            await self.simulator.on_price_update(symbol, price, ts)

    async def _on_trade(self, trade: TradeRecord):
        """Trade-completion callback — DB save + notification."""
        if self.state_mgr:
            await self.state_mgr.save_trade(trade)
            # Update account state
            sc = next(
                (s for s in cfg.STRATEGIES if s.strategy_id == trade.strategy_id),
                None,
            )
            if sc:
                acct = self.accounts.get(trade.strategy_id)
                if acct:
                    await self.state_mgr.save_account(acct, sc)
            # Mark position CLOSED
            await self.state_mgr.save_position_closed(
                trade.strategy_id, trade.symbol
            )
        await self.alert.notify_exit(trade)

    async def _on_entry(self, strategy_id: str, pos):
        """Entry callback — DB save + notification."""
        if self.state_mgr:
            await self.state_mgr.save_position_opened(pos)
        await self.alert.notify_entry(strategy_id, pos)

    async def _on_liquidation(self, strategy_id: str, pos, price: float):
        """Forced-liquidation callback."""
        log.warning("🚨 강제청산: %s %s @ %.2f", strategy_id, pos.symbol, price)

    # ══════════════════════ Auxiliary loops ══════════════════════

    async def _funding_rate_loop(self):
        """Poll funding rates every minute."""
        while self._running:
            try:
                for symbol in cfg.MULTI_COINS:
                    rate = await self.feed.fetch_current_funding(symbol)
                    if rate is not None:
                        self.funding.update_rate(symbol, rate)
            except Exception as e:
                log.warning("펀딩률 조회 에러: %s", e)
            await asyncio.sleep(cfg.FUNDING_CHECK_INTERVAL_SEC)

    async def _db_save_loop(self):
        """Batch-save strategy state to DB every 5 minutes."""
        while self._running:
            await asyncio.sleep(300)
            try:
                if self.state_mgr:
                    await self.state_mgr.save_all_accounts(
                        self.accounts, cfg.STRATEGIES
                    )
                    # Update active positions
                    for sid, sym_map in self.simulator._positions.items():
                        for sym, pos in sym_map.items():
                            await self.state_mgr.save_position_updated(pos)
                    log.debug("DB 주기적 저장 완료")
            except Exception as e:
                log.error("DB 저장 에러: %s", e)

    # ══════════════════════ Retroactive-liquidation helper ══════════════════════

    async def _collect_missed_candles(
        self, positions: dict[str, dict[str, "Position"]],
    ) -> dict[tuple[str, str], list[dict]]:
        """
        Collect candles for the missing period of restored positions.
        Backfill the gap between the last DB candle timestamp and now via REST.
        """
        missed: dict[tuple[str, str], list[dict]] = {}
        seen_keys: set[tuple[str, str]] = set()

        for sid, sym_map in positions.items():
            sc = next(
                (s for s in cfg.STRATEGIES if s.strategy_id == sid), None
            )
            if not sc:
                continue
            for symbol in sym_map:
                key = (symbol, sc.timeframe)
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                last_ms = await self.state_mgr.get_last_candle_time(
                    symbol, sc.timeframe
                )
                if last_ms:
                    from datetime import datetime, timezone
                    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                    gap_hours = (now_ms - last_ms) / 3_600_000
                    if gap_hours > 0.5:
                        log.info(
                            "누락 캔들 보충: %s %s (%.1f시간)",
                            symbol, sc.timeframe, gap_hours,
                        )
                        candles = await self.feed._rest.fetch_klines(
                            symbol, sc.timeframe, last_ms + 1,
                        )
                        if candles:
                            missed[key] = candles
                            log.info("  → %d봉 수집", len(candles))

        return missed

    # ══════════════════════ Shutdown ══════════════════════

    async def shutdown(self):
        """Graceful shutdown."""
        if not self._running:
            return  # Guard against duplicate calls
        self._running = False
        log.info("셧다운 시작...")

        # Final state save
        if self.state_mgr:
            await self.state_mgr.save_all_accounts(self.accounts, cfg.STRATEGIES)
            if self.simulator:
                for sid, sym_map in self.simulator._positions.items():
                    for sym, pos in sym_map.items():
                        await self.state_mgr.save_position_updated(pos)

        # Shutdown notification
        await self.bot.send("🛑 Phantom Trader 종료")

        # Stop components
        if self.ws:
            await self.ws.stop()
        await self.alert.stop()
        await self.bot.stop()
        if self.feed:
            await self.feed.stop()
        await self.db.close()

        log.info("셧다운 완료")


# ══════════════════════ Entry point ══════════════════════

async def main():
    trader = PhantomTrader()

    # SIGINT/SIGTERM handling
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(trader.shutdown()))

    await trader.initialize()
    await trader.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt 수신")
