"""
Strategy state persistence + crash recovery + retroactive liquidation.

SRS 6.2 state recovery process + Realism 3.8 retroactive liquidation.
"""
from __future__ import annotations
import json
from typing import TYPE_CHECKING

from storage.database import Database
from storage.models import (
    account_to_state_row, state_row_to_account,
    position_row_to_obj, candle_row_to_dict,
)
from execution.position import Position, Account
from execution.liquidation import check_exit_long, check_exit_short
from execution.fee_model import calc_trade_pnl
from utils.logger import log
from utils.time_utils import utc_now_iso, ms_to_utc_iso, is_funding_time
import config as cfg

if TYPE_CHECKING:
    from execution.simulator import ExecutionSimulator, TradeRecord


class StateManager:
    """
    Strategy state persistence manager.

    - Save to DB when trades/candles occur
    - Restore from DB when the server starts
    - Scan for retroactive liquidation after a crash
    """

    def __init__(self, db: Database):
        self._db = db

    # ══════════════════════ Save (during operation) ══════════════════════

    async def save_account(self, acct: Account, sc: cfg.StrategyConfig):
        """Save account state (on trade events / periodic calls)."""
        d = account_to_state_row(acct, sc)
        await self._db.upsert_strategy_state(**d)

    async def save_all_accounts(
        self,
        accounts: dict[str, Account],
        configs: list[cfg.StrategyConfig],
    ):
        """Save all accounts in a batch."""
        cfg_map = {sc.strategy_id: sc for sc in configs}
        for sid, acct in accounts.items():
            sc = cfg_map.get(sid)
            if sc:
                await self.save_account(acct, sc)

    async def save_position_opened(self, pos: Position):
        """Save to DB when a position is opened."""
        await self._db.insert_position(pos)
        log.debug("DB 포지션 저장: %s %s %s", pos.strategy_id, pos.side, pos.symbol)

    async def save_position_updated(self, pos: Position):
        """Update position data (extreme values, trailing, funding, etc.)."""
        await self._db.update_position(pos)

    async def save_position_closed(self, strategy_id: str, symbol: str):
        """Mark the position as CLOSED on exit."""
        await self._db.close_position(strategy_id, symbol)

    async def save_trade(self, trade: "TradeRecord"):
        """Record a completed trade exit."""
        await self._db.insert_trade(trade)
        log.debug("DB 거래 저장: %s %s %s", trade.strategy_id, trade.exit_reason, trade.symbol)

    async def save_equity_snapshot(
        self, strategy_id: str, ts: str,
        balance: float, unrealized: float,
        peak: float,
    ):
        """Equity-curve snapshot."""
        equity = balance + unrealized
        dd = ((peak - equity) / peak * 100) if peak > 0 else 0
        await self._db.insert_equity_snapshot(
            strategy_id, ts, balance, unrealized, equity, max(dd, 0)
        )

    async def save_candle(self, symbol: str, tf: str, candle: dict):
        """Save a completed candle to the DB."""
        await self._db.upsert_candle(
            symbol, tf, candle["open_time"],
            candle["open"], candle["high"], candle["low"],
            candle["close"], candle["volume"],
        )

    async def save_candles_batch(self, symbol: str, tf: str, candles: list[dict]):
        """Save candles in batch (when loading history)."""
        rows = [
            (symbol, tf, c["open_time"],
             c["open"], c["high"], c["low"], c["close"], c["volume"])
            for c in candles
        ]
        if rows:
            await self._db.upsert_candles_batch(rows)

    # ══════════════════════ Restore (server startup) ══════════════════════

    async def restore_accounts(self) -> dict[str, Account]:
        """
        Restore account state from the DB.
        Strategies missing from the DB are created with initial capital.
        """
        rows = await self._db.load_all_strategy_states()
        row_map = {r["strategy_id"]: r for r in rows}
        accounts: dict[str, Account] = {}

        for sc in cfg.STRATEGIES:
            if sc.strategy_id in row_map:
                acct = state_row_to_account(
                    row_map[sc.strategy_id], sc.initial_capital
                )
                log.info(
                    "📂 계좌 복원: %s | 잔고=$%.2f (수익률: %+.2f%%)",
                    sc.strategy_id, acct.balance, acct.return_pct,
                )
            else:
                acct = Account(
                    strategy_id=sc.strategy_id,
                    balance=sc.initial_capital,
                    initial_capital=sc.initial_capital,
                )
                log.info("🆕 계좌 초기화: %s | $%.2f", sc.strategy_id, acct.balance)
            accounts[sc.strategy_id] = acct

        return accounts

    async def restore_positions(self) -> dict[str, dict[str, Position]]:
        """
        Restore active positions from the DB.
        Returns: {strategy_id: {symbol: Position}}
        """
        rows = await self._db.load_open_positions()
        positions: dict[str, dict[str, Position]] = {}

        for row in rows:
            pos = position_row_to_obj(row)
            if pos.strategy_id not in positions:
                positions[pos.strategy_id] = {}
            positions[pos.strategy_id][pos.symbol] = pos
            log.info(
                "📂 포지션 복원: %s %s %s @ %.2f (봉수: %d)",
                pos.strategy_id, pos.side, pos.symbol,
                pos.entry_price, pos.candle_count,
            )

        total = sum(len(v) for v in positions.values())
        log.info("활성 포지션 %d개 복원", total)
        return positions

    async def restore_candles(
        self, symbol: str, tf: str, limit: int = 500
    ) -> list[dict]:
        """Restore the candle buffer from the DB."""
        rows = await self._db.load_candles(symbol, tf, limit)
        candles = [candle_row_to_dict(r) for r in reversed(rows)]
        if candles:
            log.info(
                "📂 캔들 복원: %s %s = %d봉", symbol, tf, len(candles)
            )
        return candles

    async def get_last_candle_time(self, symbol: str, tf: str) -> int | None:
        """Last saved candle timestamp (ms)."""
        return await self._db.get_last_candle_time(symbol, tf)

    # ══════════════════════ Retroactive liquidation (v1.1) ══════════════════════

    async def retroactive_liquidation_scan(
        self,
        positions: dict[str, dict[str, Position]],
        accounts: dict[str, Account],
        missed_candles: dict[tuple[str, str], list[dict]],
        funding_mgr=None,
    ) -> list["TradeRecord"]:
        """
        Iterate through candles in missing intervals in chronological order and retroactively check exit conditions.

        SRS 6.2 Step 5 + Realism 3.8.

        Args:
            positions: restored active positions {sid: {symbol: Position}}
            accounts: restored accounts {sid: Account}
            missed_candles: missing candles {(symbol, tf): [candles]}

        Returns:
            List of TradeRecords liquidated retroactively
        """
        from execution.simulator import TradeRecord

        retroactive_trades: list[TradeRecord] = []

        # Map strategy config as strategy_id → StrategyConfig
        cfg_map = {sc.strategy_id: sc for sc in cfg.STRATEGIES}

        for sid, sym_positions in list(positions.items()):
            sc = cfg_map.get(sid)
            if not sc:
                continue

            for symbol, pos in list(sym_positions.items()):
                key = (symbol, sc.timeframe)
                candles = missed_candles.get(key, [])
                if not candles:
                    continue

                trade = await self._scan_position(
                    pos, candles, accounts.get(sid), sc, funding_mgr
                )
                if trade:
                    retroactive_trades.append(trade)
                    # Remove the position
                    sym_positions.pop(symbol, None)
                    # Reflect in the DB
                    await self.save_position_closed(pos.strategy_id, symbol)
                    await self.save_trade(trade)
                    if accounts.get(sid):
                        await self.save_account(accounts[sid], sc)
                    log.warning(
                        "⚠️ 소급 청산: %s %s %s @ %.2f (%s)",
                        sid, pos.side, symbol, trade.exit_price,
                        trade.exit_reason,
                    )

        if retroactive_trades:
            log.warning(
                "소급 청산 완료: %d건 처리", len(retroactive_trades)
            )
        else:
            log.info("소급 청산: 관통 없음, 모든 포지션 유효")

        return retroactive_trades

    async def _scan_position(
        self,
        pos: Position,
        candles: list[dict],
        acct: Account | None,
        sc: cfg.StrategyConfig,
        funding_mgr=None,
    ) -> "TradeRecord | None":
        """Iterate through missing candles for a single position → retroactively check exit conditions."""
        from execution.simulator import TradeRecord

        for candle in candles:
            o = candle["open"]
            h = candle["high"]
            l = candle["low"]
            c = candle["close"]
            ts = candle.get("timestamp_utc", "")

            # Apply funding fee (00/08/16 UTC)
            if ts and is_funding_time(ts) and funding_mgr is not None:
                fee = funding_mgr.calc_funding_fee(
                    pos.side, pos.notional, pos.symbol
                )
                pos.cumulative_funding += fee

            # Calculate trailing stop (based on current state)
            trailing = None
            is_trail_active = False
            if pos.trail_atr_mult > 0 and pos.entry_atr > 0:
                if pos.side == "LONG":
                    profit = pos.highest_price - pos.entry_price
                    is_trail_active = profit >= pos.entry_atr * pos.trail_atr_mult
                    if is_trail_active:
                        trailing = pos.highest_price - pos.entry_atr * pos.sl_atr_mult
                else:
                    profit = pos.entry_price - pos.lowest_price
                    is_trail_active = profit >= pos.entry_atr * pos.trail_atr_mult
                    if is_trail_active:
                        trailing = pos.lowest_price + pos.entry_atr * pos.sl_atr_mult

            # Check gap-through exit
            if pos.side == "LONG":
                reason, exit_price = check_exit_long(
                    o, c, h, l, pos.stop_loss, pos.take_profit,
                    pos.liquidation_price, trailing, is_trail_active,
                )
            else:
                reason, exit_price = check_exit_short(
                    o, c, h, l, pos.stop_loss, pos.take_profit,
                    pos.liquidation_price, trailing, is_trail_active,
                )

            # Time exit
            if not reason and pos.time_limit and pos.candle_count >= pos.time_limit:
                reason, exit_price = "시간청산(소급)", c

            if reason:
                if "소급" not in reason:
                    reason = f"{reason}(소급)"

                pnl = calc_trade_pnl(
                    direction=pos.side,
                    actual_entry=pos.actual_entry,
                    exit_price=exit_price,
                    quantity=pos.quantity,
                    atr=pos.entry_atr,
                    entry_fee=pos.entry_fee,
                    cumulative_funding=pos.cumulative_funding,
                )

                is_liq = "강제청산" in reason
                if acct:
                    acct.apply_trade(
                        pnl["net_pnl"], pnl["total_fee"],
                        pnl["funding_paid"], is_liq,
                        gross_pnl=pnl["gross_pnl"],
                    )

                return TradeRecord(
                    strategy_id=pos.strategy_id,
                    symbol=pos.symbol,
                    side=pos.side,
                    entry_price=pos.entry_price,
                    exit_price=exit_price,
                    entry_time=pos.entry_time,
                    exit_time=ts,
                    quantity=pos.quantity,
                    leverage=pos.leverage,
                    exit_reason=reason,
                    gross_pnl=pnl["gross_pnl"],
                    total_fees=pnl["total_fee"],
                    funding_paid=pnl["funding_paid"],
                    net_pnl=pnl["net_pnl"],
                    net_pnl_pct=pnl["pnl_pct"],
                    balance_after=acct.balance if acct else 0,
                    candles_held=pos.candle_count,
                    actual_entry=pos.actual_entry,
                    actual_exit=pnl["actual_exit"],
                )

            # No penetration → update extreme values and increase bar count
            pos.update_extremes(h if pos.side == "LONG" else l)
            pos.candle_count += 1

        return None

    # ══════════════════════ Utilities ══════════════════════

    async def prune_old_data(self):
        """Clean up old candles and logs."""
        for sc in cfg.STRATEGIES:
            for sym in sc.symbols:
                await self._db.prune_candles(sym, sc.timeframe, keep=500)
        await self._db.prune_logs(days=cfg.LOG_RETENTION_DAYS)
        log.info("DB 정리 완료 (캔들 500봉/TF, 로그 %d일)", cfg.LOG_RETENTION_DAYS)
