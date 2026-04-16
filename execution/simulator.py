"""Execution simulator — order fills + next-bar + gap-through."""
from __future__ import annotations
import asyncio
import math
from dataclasses import dataclass
from typing import Optional, Callable, Awaitable

from utils.logger import log
from utils.time_utils import utc_now_iso, is_funding_time, iso_to_ms
from execution.position import Position, Account
from execution.fee_model import (
    FundingFeeManager, calc_trade_pnl, total_slippage, apply_slippage, calc_fee,
)
from execution.liquidation import (
    check_exit_long, check_exit_short, validate_position_opening,
    calc_liquidation_price, get_symbol_spec,
)
from data.candle_builder import Candle
import config as cfg


@dataclass
class Signal:
    """Signal returned by a strategy."""
    strategy_id: str
    symbol: str
    direction: str          # "LONG" | "SHORT"
    signal_price: float
    signal_time: str
    atr_at_signal: float
    signal_type: str        # "ENTRY" | "EXIT"
    stop_loss: float = 0.0
    take_profit: Optional[float] = None
    time_limit: Optional[int] = None
    trail_atr_mult: float = 0.0
    sl_atr_mult: float = 0.0
    registered_at: str = ""  # Signal registration time (for TTL checks)


@dataclass
class TradeRecord:
    """Completed exit trade record."""
    strategy_id: str
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    entry_time: str
    exit_time: str
    quantity: float
    leverage: int
    exit_reason: str
    gross_pnl: float
    total_fees: float
    funding_paid: float
    net_pnl: float
    net_pnl_pct: float
    balance_after: float
    candles_held: int
    actual_entry: float = 0.0    # Effective entry price with slippage applied
    actual_exit: float = 0.0     # Effective exit price with slippage applied


class ExecutionSimulator:
    """
    Core execution engine.
    - Next-bar execution
    - Gap-through exit priority
    - Realtime forced-liquidation checks
    - Automatic funding-fee charging
    """

    def __init__(
        self,
        accounts: dict[str, Account],
        funding_mgr: FundingFeeManager,
        on_trade: Callable[[TradeRecord], Awaitable[None]] | None = None,
        on_entry: Callable[[str, Position], Awaitable[None]] | None = None,
        on_liquidation: Callable[[str, Position, float], Awaitable[None]] | None = None,
    ):
        self._accounts = accounts
        self._funding = funding_mgr
        self._on_trade = on_trade
        self._on_entry = on_entry
        self._on_liquidation = on_liquidation

        # Active positions: strategy_id → {symbol: Position}
        self._positions: dict[str, dict[str, Position]] = {}

        # Pending signals: strategy_id_symbol → Signal
        self._pending: dict[str, Signal] = {}

        # Prevent double funding-fee charges: strategy_id_symbol → last charged timestamp
        self._last_funding_ts: dict[str, str] = {}

    # ────── Position access ──────

    def get_position(self, strategy_id: str, symbol: str) -> Position | None:
        return self._positions.get(strategy_id, {}).get(symbol)

    def get_all_positions(self, strategy_id: str) -> list[Position]:
        return list(self._positions.get(strategy_id, {}).values())

    def has_position(self, strategy_id: str, symbol: str) -> bool:
        return symbol in self._positions.get(strategy_id, {})

    # ────── Signal registration ──────

    def register_signal(self, signal: Signal):
        """When a strategy emits a signal, register it as pending (filled at the next candle open)."""
        key = f"{signal.strategy_id}_{signal.symbol}"
        signal.registered_at = signal.signal_time  # Registration time for TTL checks
        self._pending[key] = signal
        log.debug(
            "📋 Pending: %s %s %s @ %.2f",
            signal.strategy_id, signal.direction, signal.symbol, signal.signal_price,
        )

    # ────── Candle-close processing (core) ──────

    async def on_candle_close(
        self, symbol: str, timeframe: str, candle: Candle,
        strategy_configs: list[cfg.StrategyConfig],
    ) -> list[TradeRecord]:
        """
        Execution flow when a candle close is confirmed:
        1. Pending signal → fill at this candle open
        2. Check exits for existing positions (gap-through)
        3. Check time-based exits
        """
        trades: list[TradeRecord] = []
        o, c, h, l = candle["open"], candle["close"], candle["high"], candle["low"]
        ts = candle["timestamp_utc"]

        for sc in strategy_configs:
            if sc.timeframe != timeframe or symbol not in sc.symbols:
                continue

            acct = self._accounts.get(sc.strategy_id)
            if not acct or not acct.is_active or acct.balance <= 0:
                continue

            just_entered = False  # Entry flag per strategy (initialized inside the loop)

            # ── Step 1: Fill pending signals ──
            pkey = f"{sc.strategy_id}_{symbol}"
            if pkey in self._pending:
                sig = self._pending.pop(pkey)

                # TTL check: discard the signal if it is older than timeframe × 2
                if sig.registered_at and ts:
                    max_age_ms = cfg.TF_MINUTES.get(timeframe, 60) * 2 * 60 * 1000
                    age_ms = iso_to_ms(ts) - iso_to_ms(sig.registered_at)
                    if age_ms > max_age_ms:
                        log.debug(
                            "⛔ 시그널 만료: %s %s (age=%ds)",
                            sig.strategy_id, sig.symbol, age_ms // 1000,
                        )
                        sig = None  # Discard

                if sig is not None and sig.signal_type == "EXIT":
                    # Signal-based exits such as MA crossover death-cross → exit at the next candle open
                    existing = self.get_position(sc.strategy_id, symbol)
                    if existing:
                        trade = await self._execute_exit(
                            existing, o, ts, "시그널청산", sc
                        )
                        if trade:
                            trades.append(trade)
                elif sig is not None and sig.signal_type == "ENTRY":
                    pos = await self._execute_entry(sig, o, ts, sc)
                    if pos:
                        just_entered = True

            # ── Step 2: Check exits for existing positions ──
            pos = self.get_position(sc.strategy_id, symbol)
            if pos is None:
                continue

            # Do not increment candle_count on the entry candle
            if not just_entered:
                pos.candle_count += 1
            pos.update_extremes(h if pos.side == "LONG" else l)

            # ── Charge funding fees (00/08/16 UTC) ──
            if is_funding_time(ts):
                fkey = f"{sc.strategy_id}_{symbol}"
                if self._last_funding_ts.get(fkey) != ts:
                    fee = self._funding.calc_funding_fee(
                        pos.side, pos.notional, symbol
                    )
                    pos.cumulative_funding += fee
                    self._last_funding_ts[fkey] = ts
                    log.debug(
                        "💰 펀딩비: %s %s %s $%.4f (누적: $%.4f)",
                        sc.strategy_id, symbol, pos.side, fee,
                        pos.cumulative_funding,
                    )

            # Calculate trailing stop (SRS 4.2)
            # ① Activation condition: unrealized profit ≥ ATR × trail_atr_mult (3.0)
            # ② Stop distance after activation: ATR × sl_atr_mult (2.0)
            trailing = None
            is_trail_active = False
            if pos.trail_atr_mult > 0 and pos.entry_atr > 0:
                if pos.side == "LONG":
                    profit_dist = pos.highest_price - pos.entry_price
                    is_trail_active = profit_dist >= pos.entry_atr * pos.trail_atr_mult
                    if is_trail_active:
                        trailing = pos.highest_price - pos.entry_atr * pos.sl_atr_mult
                else:
                    profit_dist = pos.entry_price - pos.lowest_price
                    is_trail_active = profit_dist >= pos.entry_atr * pos.trail_atr_mult
                    if is_trail_active:
                        trailing = pos.lowest_price + pos.entry_atr * pos.sl_atr_mult
                pos.trailing_stop = trailing
                pos.trailing_activated = is_trail_active

            # Exit checks (gap-through priority)
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

            # Time-based exit
            if not reason and pos.time_limit and pos.candle_count >= pos.time_limit:
                reason, exit_price = "시간청산", c

            if reason:
                trade = await self._execute_exit(pos, exit_price, ts, reason, sc)
                if trade:
                    trades.append(trade)

        return trades

    # ────── Entry execution ──────

    async def _execute_entry(
        self, signal: Signal, open_price: float, ts: str, sc: cfg.StrategyConfig
    ) -> Position | None:
        """Fill a pending signal at the open price."""
        acct = self._accounts[signal.strategy_id]

        # ── Reject entry if SL has already been breached (blocks guaranteed-loss trades) ──
        if signal.stop_loss and signal.stop_loss > 0:
            if signal.direction == "LONG" and open_price <= signal.stop_loss:
                log.debug(
                    "⛔ 진입 스킵 (갭관통): %s LONG open=%.2f ≤ SL=%.2f",
                    signal.strategy_id, open_price, signal.stop_loss,
                )
                return None
            if signal.direction == "SHORT" and open_price >= signal.stop_loss:
                log.debug(
                    "⛔ 진입 스킵 (갭관통): %s SHORT open=%.2f ≥ SL=%.2f",
                    signal.strategy_id, open_price, signal.stop_loss,
                )
                return None

        # Skip if a position already exists (multi-coin strategies allow one per symbol)
        if self.has_position(signal.strategy_id, signal.symbol):
            if sc.strategy_id != "S8_multi_coin_4h_1x":
                return None

        # ── MA-crossover strategy: position_pct-based sizing ──
        if "position_pct" in sc.params:
            pos = await self._execute_entry_pct(signal, open_price, ts, sc)
            return pos

        # ── Multi-coin: based on allocated capital per coin ──
        equity_base = acct.balance
        if "per_coin_capital" in sc.params:
            equity_base = min(acct.balance, sc.params["per_coin_capital"])

        validated = validate_position_opening(
            equity=equity_base,
            entry_price=open_price,
            stop_price=signal.stop_loss,
            direction=signal.direction,
            leverage=sc.leverage,
            risk_pct=sc.params.get("risk_per_trade", 0.01),
            atr=signal.atr_at_signal,
            symbol=signal.symbol,
        )

        if validated is None:
            log.debug("⛔ 진입 검증 실패: %s %s", signal.strategy_id, signal.symbol)
            return None

        return await self._create_position(signal, open_price, ts, sc, validated)

    async def _execute_entry_pct(
        self, signal: Signal, open_price: float, ts: str, sc: cfg.StrategyConfig
    ) -> Position | None:
        """
        Entry for the MA-crossover strategy: fixed sizing by position_pct (10%) of capital.
        This is fixed-percentage allocation, not risk-based sizing.
        """
        acct = self._accounts[signal.strategy_id]
        pct = sc.params.get("position_pct", 0.10)
        # Apply the 30% margin cap (same safeguard as risk-based strategies)
        position_value = min(
            acct.balance * pct,
            acct.balance * cfg.MAX_MARGIN_RATIO,
        )

        slip = total_slippage(open_price, signal.atr_at_signal)
        if signal.direction == "LONG":
            actual_entry = open_price * (1 + slip)
        else:
            actual_entry = open_price * (1 - slip)

        if actual_entry <= 0:
            return None

        spec = get_symbol_spec(signal.symbol)
        lot_size = spec["lot_size"]
        min_notional = spec["min_notional"]

        qty = position_value / actual_entry
        qty = math.floor(qty / lot_size) * lot_size
        if qty <= 0 or qty * actual_entry < min_notional:
            return None

        notional = actual_entry * qty
        margin = notional / sc.leverage
        liq_price = calc_liquidation_price(
            actual_entry, sc.leverage, signal.direction, notional
        )
        # Guard against mr ≤ 0 (position cannot be opened)
        if liq_price is None:
            return None

        # Validate liquidation-price/SL placement (same as validate_position_opening)
        if signal.direction == "LONG" and liq_price >= signal.stop_loss:
            log.debug("⛔ 진입 거부: 청산가(%.2f) ≥ SL(%.2f)", liq_price, signal.stop_loss)
            return None
        if signal.direction == "SHORT" and liq_price <= signal.stop_loss:
            log.debug("⛔ 진입 거부: 청산가(%.2f) ≤ SL(%.2f)", liq_price, signal.stop_loss)
            return None

        validated = {
            "entry_price": open_price,
            "actual_entry": actual_entry,
            "quantity": qty,
            "margin": margin,
            "stop_loss": signal.stop_loss,
            "liquidation_price": liq_price or 0.0,
            "direction": signal.direction,
            "atr": signal.atr_at_signal,
            "notional": notional,
        }

        return await self._create_position(signal, open_price, ts, sc, validated)

    async def _create_position(
        self, signal: Signal, open_price: float, ts: str,
        sc: cfg.StrategyConfig, validated: dict
    ) -> Position:
        """Create a position with validated parameters (shared)."""
        entry_fee = calc_fee(validated["notional"])

        pos = Position(
            strategy_id=signal.strategy_id,
            symbol=signal.symbol,
            side=signal.direction,
            entry_price=open_price,
            actual_entry=validated["actual_entry"],
            entry_time=ts,
            quantity=validated["quantity"],
            leverage=sc.leverage,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            time_limit=signal.time_limit,
            liquidation_price=validated["liquidation_price"],
            margin_used=validated["margin"],
            notional=validated["notional"],
            entry_fee=entry_fee,
            highest_price=open_price,
            lowest_price=open_price,
            trail_atr_mult=signal.trail_atr_mult,
            sl_atr_mult=signal.sl_atr_mult,
            entry_atr=signal.atr_at_signal,
        )

        if signal.strategy_id not in self._positions:
            self._positions[signal.strategy_id] = {}
        self._positions[signal.strategy_id][signal.symbol] = pos

        log.info(
            "📈 진입: %s %s %s @ %.2f (qty=%.4f, margin=$%.2f, liq=%.2f)",
            signal.strategy_id, signal.direction, signal.symbol,
            open_price, pos.quantity, pos.margin_used, pos.liquidation_price,
        )

        if self._on_entry:
            await self._on_entry(signal.strategy_id, pos)

        return pos

    # ────── Exit execution ──────

    async def _execute_exit(
        self, pos: Position, exit_price: float, ts: str,
        reason: str, sc: cfg.StrategyConfig
    ) -> TradeRecord | None:
        """Exit a position."""
        acct = self._accounts[pos.strategy_id]

        pnl = calc_trade_pnl(
            direction=pos.side,
            actual_entry=pos.actual_entry,
            exit_price=exit_price,
            quantity=pos.quantity,
            atr=pos.entry_atr,
            entry_fee=pos.entry_fee,
            cumulative_funding=pos.cumulative_funding,
        )

        is_liq = reason == "강제청산"
        acct.apply_trade(
            pnl["net_pnl"], pnl["total_fee"], pnl["funding_paid"],
            is_liq, gross_pnl=pnl["gross_pnl"],
        )

        trade = TradeRecord(
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
            balance_after=acct.balance,
            candles_held=pos.candle_count,
            actual_entry=pos.actual_entry,
            actual_exit=pnl["actual_exit"],
        )

        # Remove the position
        if pos.strategy_id in self._positions:
            self._positions[pos.strategy_id].pop(pos.symbol, None)

        emoji = "🚨" if is_liq else ("💰" if pnl["net_pnl"] > 0 else "📉")
        log.info(
            "%s 청산: %s %s %s @ %.2f → %.2f | %s | PnL=$%.2f (%.2f%%)",
            emoji, pos.strategy_id, pos.side, pos.symbol,
            pos.entry_price, exit_price, reason,
            pnl["net_pnl"], pnl["pnl_pct"],
        )

        if self._on_trade:
            await self._on_trade(trade)

        return trade

    # ────── Realtime price checks ──────

    async def on_price_update(self, symbol: str, price: float, ts: str):
        """Called on every 1m candle update — forced-liquidation checks + extreme tracking."""
        if not cfg.REALTIME_LIQ_CHECK:
            return

        for sid, positions in list(self._positions.items()):
            pos = positions.get(symbol)
            if pos is None:
                continue

            # Forced-liquidation checks
            if pos.side == "LONG" and price <= pos.liquidation_price:
                log.warning("🚨 실시간 강제청산: %s %s @ %.2f", sid, symbol, price)
                # Simplified exit (immediate instead of waiting for formal processing on the next candle)
                acct = self._accounts.get(sid)
                if acct:
                    sc_match = None
                    for sc in cfg.STRATEGIES:
                        if sc.strategy_id == sid:
                            sc_match = sc
                            break
                    if sc_match:
                        await self._execute_exit(
                            pos, pos.liquidation_price, ts, "강제청산", sc_match
                        )
                        if self._on_liquidation:
                            await self._on_liquidation(sid, pos, price)
                continue

            if pos.side == "SHORT" and price >= pos.liquidation_price:
                log.warning("🚨 실시간 강제청산: %s %s @ %.2f", sid, symbol, price)
                acct = self._accounts.get(sid)
                if acct:
                    sc_match = None
                    for sc in cfg.STRATEGIES:
                        if sc.strategy_id == sid:
                            sc_match = sc
                            break
                    if sc_match:
                        await self._execute_exit(
                            pos, pos.liquidation_price, ts, "강제청산", sc_match
                        )
                        if self._on_liquidation:
                            await self._on_liquidation(sid, pos, price)
                continue

            # Track extremes
            pos.update_extremes(price)
            pos.update_unrealized(price)