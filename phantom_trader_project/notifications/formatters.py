"""
Telegram message formatter — SRS Section 7.1.

Five alert formats + response formats for commands such as /status and /strategy.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

from utils.time_utils import utc_to_kst_str

if TYPE_CHECKING:
    from execution.position import Position, Account
    from execution.simulator import TradeRecord


def fmt_price(v: float, decimals: int = 2) -> str:
    return f"${v:,.{decimals}f}"


def fmt_pct(v: float) -> str:
    return f"{v:+.2f}%"


def fmt_qty(v: float, symbol: str = "BTC") -> str:
    return f"{v:.4f} {symbol.replace('USDT', '')}"


# ══════════════════════ Entry alert ══════════════════════

def format_entry(strategy_name: str, pos: "Position") -> str:
    emoji = "📈" if pos.side == "LONG" else "📉"
    liq_line = f"청산가: {fmt_price(pos.liquidation_price)}\n" if pos.leverage > 1 else ""
    tl = f"시간제한: {pos.time_limit}봉" if pos.time_limit else "시간제한: 없음"
    trail = ""
    if pos.trail_atr_mult > 0:
        trail = f"트레일링: ATR×{pos.trail_atr_mult} 도달 시 활성화\n"
    tp_line = f"익절: {fmt_price(pos.take_profit)}\n" if pos.take_profit else ""

    # Also show the effective entry price if slippage is meaningful
    slip_line = ""
    if pos.actual_entry and abs(pos.actual_entry - pos.entry_price) > 0.005:
        slip_line = f"실효진입: {fmt_price(pos.actual_entry)} (슬리피지 반영)\n"

    return (
        f"{emoji} [{strategy_name}] {pos.side} 진입\n"
        f"━━━━━━━━━━━━━━━\n"
        f"코인: {pos.symbol}\n"
        f"진입가: {fmt_price(pos.entry_price)}\n"
        f"{slip_line}"
        f"수량: {fmt_qty(pos.quantity, pos.symbol)} ({fmt_price(pos.notional)})\n"
        f"레버리지: {pos.leverage}x\n"
        f"증거금: {fmt_price(pos.margin_used)}\n"
        f"손절: {fmt_price(pos.stop_loss)}\n"
        f"{tp_line}"
        f"{trail}"
        f"{tl}\n"
        f"{liq_line}"
        f"━━━━━━━━━━━━━━━\n"
        f"수수료: {fmt_price(pos.entry_fee)}\n"
        f"⏰ {utc_to_kst_str(pos.entry_time)}"
    )


# ══════════════════════ Exit alert ══════════════════════

def format_exit(strategy_name: str, trade: "TradeRecord") -> str:
    if trade.net_pnl > 0:
        emoji = "💰"
    elif "강제청산" in trade.exit_reason:
        emoji = "🚨🚨🚨"
    else:
        emoji = "📉"

    pnl_sign = "+" if trade.net_pnl >= 0 else ""

    # Show the effective price with slippage applied
    actual_line = ""
    if trade.actual_entry and trade.actual_exit:
        actual_line = (
            f"실효가: {fmt_price(trade.actual_entry)} → "
            f"{fmt_price(trade.actual_exit)}\n"
        )

    # Funding-fee sign handling: positive=cost (LONG pays), negative=income (SHORT receives)
    if trade.funding_paid >= 0:
        funding_str = f"-{fmt_price(trade.funding_paid)}"
    else:
        funding_str = f"+{fmt_price(abs(trade.funding_paid))}"

    return (
        f"{emoji} [{strategy_name}] {trade.side} 청산 — {trade.exit_reason}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"코인: {trade.symbol}\n"
        f"진입가: {fmt_price(trade.entry_price)} → 청산가: {fmt_price(trade.exit_price)}\n"
        f"{actual_line}"
        f"총손익: {pnl_sign}{fmt_price(trade.gross_pnl)}\n"
        f"수수료: -{fmt_price(trade.total_fees)} | 펀딩비: {funding_str}\n"
        f"순수익: {pnl_sign}{fmt_price(trade.net_pnl)} ({fmt_pct(trade.net_pnl_pct)})\n"
        f"보유: {trade.candles_held}봉\n"
        f"━━━━━━━━━━━━━━━\n"
        f"잔고: {fmt_price(trade.balance_after)}\n"
        f"⏰ {utc_to_kst_str(trade.exit_time)}"
    )


# ══════════════════════ Daily report ══════════════════════

def format_daily_report(
    date_str: str,
    accounts: dict[str, "Account"],
    strategy_names: dict[str, str],
    btc_price: float | None = None,
    active_count: int = 0,
    today_trades: list[dict] | None = None,
) -> str:
    # Daily PnL aggregation by strategy
    daily_by_strategy: dict[str, float] = {}
    if today_trades:
        for t in today_trades:
            sid = t.get("strategy_id", "")
            daily_by_strategy[sid] = daily_by_strategy.get(sid, 0) + t.get("net_pnl", 0)

    today_count = len(today_trades) if today_trades else 0

    lines = [
        f"📊 일일 리포트 — {date_str}",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f" {'#':>2}  {'전략':<14} {'잔고':>10}  {'일수익':>9}  {'총수익률':>8}",
    ]

    total_balance = 0
    total_initial = 0
    total_daily = 0

    for i, (sid, acct) in enumerate(sorted(accounts.items()), 1):
        name = strategy_names.get(sid, sid)[:14]
        daily_pnl = daily_by_strategy.get(sid, 0)
        total_daily += daily_pnl

        daily_str = f"{'+' if daily_pnl >= 0 else ''}{fmt_price(daily_pnl)}"
        lines.append(
            f" {i:>2}  {name:<14} {fmt_price(acct.balance):>10}  "
            f"{daily_str:>9}  {fmt_pct(acct.return_pct):>8}"
        )
        total_balance += acct.balance
        total_initial += acct.initial_capital

    total_return = (total_balance - total_initial) / total_initial * 100 if total_initial > 0 else 0
    total_daily_str = f"{'+' if total_daily >= 0 else ''}{fmt_price(total_daily)}"
    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━━━━",
        f"합계{' ':>14} {fmt_price(total_balance):>10}  {total_daily_str:>9}  {fmt_pct(total_return):>8}",
    ])

    if btc_price:
        lines.append(f"BTC 가격: {fmt_price(btc_price)}")
    lines.append(f"활성 포지션: {active_count}개 | 오늘 거래: {today_count}건")

    return "\n".join(lines)


# ══════════════════════ /status response ══════════════════════

def format_status(
    accounts: dict[str, "Account"],
    strategy_names: dict[str, str],
    positions: dict[str, list["Position"]],
) -> str:
    lines = ["📊 현재 상태", ""]

    for sid, acct in sorted(accounts.items()):
        name = strategy_names.get(sid, sid)
        pos_list = positions.get(sid, [])
        status = "🟢" if acct.is_active else "🔴"
        pos_info = ""
        if pos_list:
            p = pos_list[0]
            pos_info = f" | {p.side} {p.symbol} @ {fmt_price(p.entry_price)}"
        lines.append(
            f"{status} {name}: {fmt_price(acct.balance)} ({fmt_pct(acct.return_pct)})"
            f"{pos_info}"
        )

    total = sum(a.balance for a in accounts.values())
    lines.extend(["", f"합계: {fmt_price(total)}"])
    return "\n".join(lines)


# ══════════════════════ /strategy N response ══════════════════════

def format_strategy_detail(
    name: str, acct: "Account",
    positions: list["Position"],
    recent_trades: list[dict],
) -> str:
    pf = acct.profit_factor
    pf_str = f"{pf:.2f}" if pf is not None else "∞"

    lines = [
        f"📋 {name}",
        f"━━━━━━━━━━━━━━━",
        f"잔고: {fmt_price(acct.balance)} ({fmt_pct(acct.return_pct)})",
        f"승률: {acct.win_rate:.1f}% ({acct.wins}W/{acct.losses}L)",
        f"PF: {pf_str}",
        f"총거래: {acct.total_trades}건 | 청산: {acct.liquidations}건",
        f"MDD: {acct.max_drawdown_pct:.2f}%",
        f"수수료: {fmt_price(acct.total_fees)} | 펀딩: {fmt_price(acct.total_funding)}",
    ]

    if positions:
        lines.append("\n활성 포지션:")
        for p in positions:
            lines.append(
                f"  {p.side} {p.symbol} @ {fmt_price(p.entry_price)} "
                f"(PnL: {fmt_price(p.unrealized_pnl)}, {p.candle_count}봉)"
            )
    else:
        lines.append("\n활성 포지션: 없음")

    if recent_trades:
        lines.append("\n최근 거래:")
        for t in recent_trades[:5]:
            lines.append(
                f"  {t['side']} {t['exit_reason']}: "
                f"{fmt_price(t['net_pnl'])} ({fmt_pct(t['net_pnl_pct'])})"
            )

    return "\n".join(lines)


# ══════════════════════ /performance response ══════════════════════

def format_performance(
    accounts: dict[str, "Account"],
    strategy_names: dict[str, str],
) -> str:
    lines = [
        "📊 전략별 성과",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f" {'#':>2}  {'전략':<12} {'승률':>6} {'PF':>6} {'MDD':>7} {'수익률':>8}",
    ]

    for i, (sid, acct) in enumerate(sorted(accounts.items()), 1):
        name = strategy_names.get(sid, sid)[:12]
        pf = acct.profit_factor
        pf_str = f"{pf:.2f}" if pf is not None else "∞"

        lines.append(
            f" {i:>2}  {name:<12} {acct.win_rate:>5.1f}% {pf_str:>6} "
            f"{acct.max_drawdown_pct:>6.2f}% {fmt_pct(acct.return_pct):>8}"
        )

    return "\n".join(lines)


# ══════════════════════ /health response ══════════════════════

def format_health(summary: str) -> str:
    return f"🏥 시스템 상태\n━━━━━━━━━━━━━━━\n{summary}"


# ══════════════════════ System warning ══════════════════════

def format_system_alert(message: str) -> str:
    return f"⚠️ 시스템 경고\n{message}"
