"""
Phantom Trader Dashboard — FastAPI backend.
Reads phantom_trader.db (SQLite) and exposes it through a REST API.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import FastAPI, Query, Depends, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ──────────────────────────── Config ────────────────────────────

DB_PATH = os.getenv("PHANTOM_DB_PATH", str(Path(__file__).parent.parent / "phantom_trader.db"))
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8080"))
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip()
SECRET_KEY = os.getenv("DASHBOARD_SECRET", "").strip() or secrets.token_hex(32)
TOKEN_TTL_SECONDS = int(os.getenv("DASHBOARD_TOKEN_TTL_SECONDS", str(12 * 60 * 60)))
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("DASHBOARD_ALLOWED_ORIGINS", "").split(",") if o.strip()]

# ──────────────────────────── App ────────────────────────────

app = FastAPI(title="Phantom Trader Dashboard", docs_url=None, redoc_url=None)

if ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

# ──────────────────────────── Auth ────────────────────────────

def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")

def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + ("=" * (-len(data) % 4)))

def _issue_token() -> str:
    payload = {
        "exp": int((datetime.now(timezone.utc) + timedelta(seconds=TOKEN_TTL_SECONDS)).timestamp())
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64encode(hmac.new(SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"

def _verify_token(token: str) -> bool:
    if not token or "." not in token:
        return False
    body, sig = token.rsplit(".", 1)
    expected_sig = _b64encode(hmac.new(SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected_sig):
        return False
    try:
        payload = json.loads(_b64decode(body).decode())
    except Exception:
        return False
    return int(payload.get("exp", 0)) >= int(datetime.now(timezone.utc).timestamp())

@app.post("/api/auth/login")
async def login(request: Request):
    if not DASHBOARD_PASSWORD:
        raise HTTPException(503, "Dashboard password is not configured")
    body = await request.json()
    pw = body.get("password", "")
    if _hash(pw) != _hash(DASHBOARD_PASSWORD):
        raise HTTPException(401, "Invalid password")
    return {"token": _issue_token()}

def require_auth(request: Request):
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "")
    if not _verify_token(token):
        raise HTTPException(401, "Unauthorized")

# ──────────────────────────── DB Helper ────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def query(sql: str, params: tuple = ()) -> list[dict]:
    db = get_db()
    try:
        rows = db.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()

def query_one(sql: str, params: tuple = ()) -> dict | None:
    db = get_db()
    try:
        row = db.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        db.close()

# ──────────────────────────── API: Overview ────────────────────────────

@app.get("/api/overview", dependencies=[Depends(require_auth)])
async def overview():
    """Overall strategy summary — main dashboard KPIs."""
    strategies = query("SELECT * FROM strategy_state ORDER BY strategy_id")
    
    total_balance = 0
    total_initial = 0
    total_pnl = 0
    total_fees = 0
    total_funding = 0
    total_trades_count = 0
    total_wins = 0
    total_losses = 0
    total_liquidations = 0
    
    items = []
    for s in strategies:
        balance = s["account_balance"]
        # Estimate initial capital from strategy_id pattern
        initial = 10000.0
        pnl = s.get("total_pnl", 0) or 0
        fees = s.get("total_fees", 0) or 0
        funding = s.get("total_funding", 0) or 0
        wins = s.get("wins", 0) or 0
        losses = s.get("losses", 0) or 0
        liqs = s.get("liquidations", 0) or 0
        trades = wins + losses
        peak = s.get("peak_balance", balance) or balance
        mdd = s.get("max_drawdown_pct", 0) or 0
        gw = s.get("gross_wins_sum", 0) or 0
        gl = s.get("gross_losses_sum", 0) or 0
        
        pf = round(gw / gl, 2) if gl > 0 else (None if gw > 0 else 0)
        wr = round(wins / trades * 100, 1) if trades > 0 else 0
        ret = round((balance - initial) / initial * 100, 2) if initial > 0 else 0
        
        total_balance += balance
        total_initial += initial
        total_pnl += pnl
        total_fees += fees
        total_funding += funding
        total_trades_count += trades
        total_wins += wins
        total_losses += losses
        total_liquidations += liqs
        
        items.append({
            "strategy_id": s["strategy_id"],
            "balance": round(balance, 2),
            "return_pct": ret,
            "total_pnl": round(pnl, 2),
            "total_fees": round(fees, 2),
            "total_funding": round(funding, 2),
            "wins": wins,
            "losses": losses,
            "trades": trades,
            "win_rate": wr,
            "profit_factor": pf,
            "max_drawdown_pct": round(mdd, 2),
            "peak_balance": round(peak, 2),
            "liquidations": liqs,
            "is_active": bool(s.get("is_active", 1)),
        })
    
    total_ret = round((total_balance - total_initial) / total_initial * 100, 2) if total_initial > 0 else 0
    total_wr = round(total_wins / total_trades_count * 100, 1) if total_trades_count > 0 else 0
    
    return {
        "summary": {
            "total_balance": round(total_balance, 2),
            "total_initial": round(total_initial, 2),
            "total_return_pct": total_ret,
            "total_pnl": round(total_pnl, 2),
            "total_fees": round(total_fees, 2),
            "total_funding": round(total_funding, 2),
            "total_trades": total_trades_count,
            "total_wins": total_wins,
            "total_losses": total_losses,
            "total_win_rate": total_wr,
            "total_liquidations": total_liquidations,
        },
        "strategies": items,
    }

# ──────────────────────────── API: Positions ────────────────────────────

@app.get("/api/positions", dependencies=[Depends(require_auth)])
async def positions():
    """Currently active positions."""
    rows = query("SELECT * FROM positions WHERE status='OPEN' ORDER BY entry_time DESC")
    return {"positions": rows}

# ──────────────────────────── API: Equity Curve ────────────────────────────

@app.get("/api/equity", dependencies=[Depends(require_auth)])
async def equity_curve(
    strategy_id: str | None = Query(None),
    days: int = Query(30),
):
    """Equity-curve data."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    
    if strategy_id:
        rows = query(
            """SELECT timestamp, balance, unrealized_pnl, total_equity, drawdown_pct
               FROM equity_snapshots
               WHERE strategy_id=? AND timestamp>=?
               ORDER BY timestamp""",
            (strategy_id, cutoff),
        )
    else:
        # Aggregate across all strategies
        rows = query(
            """SELECT timestamp, 
                      SUM(balance) as balance,
                      SUM(unrealized_pnl) as unrealized_pnl,
                      SUM(total_equity) as total_equity,
                      MAX(drawdown_pct) as drawdown_pct
               FROM equity_snapshots
               WHERE timestamp>=?
               GROUP BY timestamp
               ORDER BY timestamp""",
            (cutoff,),
        )
    
    return {"data": rows}

# ──────────────────────────── API: Trades ────────────────────────────

@app.get("/api/trades", dependencies=[Depends(require_auth)])
async def trades(
    strategy_id: str | None = Query(None),
    symbol: str | None = Query(None),
    exit_reason: str | None = Query(None),
    days: int = Query(90),
    limit: int = Query(500),
):
    """Trade log (filterable)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conditions = ["exit_time >= ?"]
    params: list = [cutoff]
    
    if strategy_id:
        conditions.append("strategy_id = ?")
        params.append(strategy_id)
    if symbol:
        conditions.append("symbol = ?")
        params.append(symbol)
    if exit_reason:
        conditions.append("exit_reason LIKE ?")
        params.append(f"%{exit_reason}%")
    
    where = " AND ".join(conditions)
    rows = query(
        f"SELECT * FROM trades WHERE {where} ORDER BY exit_time DESC LIMIT ?",
        tuple(params + [limit]),
    )
    return {"trades": rows, "count": len(rows)}

# ──────────────────────────── API: Calendar PnL ────────────────────────────

@app.get("/api/calendar", dependencies=[Depends(require_auth)])
async def calendar_pnl(
    strategy_id: str | None = Query(None),
    days: int = Query(90),
):
    """Daily P&L heatmap data."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    
    if strategy_id:
        rows = query(
            """SELECT DATE(exit_time) as date,
                      COUNT(*) as trades,
                      SUM(net_pnl) as pnl,
                      SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins,
                      SUM(gross_pnl) as gross,
                      SUM(total_fees) as fees
               FROM trades
               WHERE exit_time >= ? AND strategy_id = ?
               GROUP BY DATE(exit_time)
               ORDER BY date""",
            (cutoff, strategy_id),
        )
    else:
        rows = query(
            """SELECT DATE(exit_time) as date,
                      COUNT(*) as trades,
                      SUM(net_pnl) as pnl,
                      SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins,
                      SUM(gross_pnl) as gross,
                      SUM(total_fees) as fees
               FROM trades
               WHERE exit_time >= ?
               GROUP BY DATE(exit_time)
               ORDER BY date""",
            (cutoff,),
        )
    
    return {"data": rows}

# ──────────────────────────── API: Strategy Detail ────────────────────────────

@app.get("/api/strategy/{strategy_id}", dependencies=[Depends(require_auth)])
async def strategy_detail(strategy_id: str):
    """Detailed strategy analysis."""
    state = query_one("SELECT * FROM strategy_state WHERE strategy_id=?", (strategy_id,))
    if not state:
        raise HTTPException(404, "Strategy not found")
    
    # Recent trades
    recent = query(
        "SELECT * FROM trades WHERE strategy_id=? ORDER BY exit_time DESC LIMIT 20",
        (strategy_id,),
    )
    
    # Statistics by exit reason
    by_reason = query(
        """SELECT exit_reason,
                  COUNT(*) as count,
                  SUM(net_pnl) as total_pnl,
                  AVG(net_pnl) as avg_pnl
           FROM trades WHERE strategy_id=?
           GROUP BY exit_reason""",
        (strategy_id,),
    )
    
    # Monthly profit
    monthly = query(
        """SELECT strftime('%Y-%m', exit_time) as month,
                  COUNT(*) as trades,
                  SUM(net_pnl) as pnl,
                  SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins
           FROM trades WHERE strategy_id=?
           GROUP BY month ORDER BY month""",
        (strategy_id,),
    )
    
    # Holding-period distribution
    holding = query(
        """SELECT 
             CASE 
               WHEN candles_held <= 5 THEN '1-5'
               WHEN candles_held <= 10 THEN '6-10'
               WHEN candles_held <= 20 THEN '11-20'
               WHEN candles_held <= 50 THEN '21-50'
               ELSE '50+'
             END as range,
             COUNT(*) as count,
             SUM(net_pnl) as pnl
           FROM trades WHERE strategy_id=?
           GROUP BY range""",
        (strategy_id,),
    )
    
    # PnL distribution (for histogram)
    pnl_dist = query(
        "SELECT net_pnl, net_pnl_pct FROM trades WHERE strategy_id=? ORDER BY exit_time",
        (strategy_id,),
    )
    
    return {
        "state": state,
        "recent_trades": recent,
        "by_exit_reason": by_reason,
        "monthly": monthly,
        "holding_distribution": holding,
        "pnl_distribution": [{"pnl": r["net_pnl"], "pct": r["net_pnl_pct"]} for r in pnl_dist],
    }

# ──────────────────────────── API: Strategy Comparison ────────────────────────────

@app.get("/api/compare", dependencies=[Depends(require_auth)])
async def compare_strategies():
    """Comparison matrix across strategies."""
    strategies = query("SELECT * FROM strategy_state ORDER BY strategy_id")
    
    result = []
    for s in strategies:
        sid = s["strategy_id"]
        balance = s["account_balance"]
        initial = 10000.0
        wins = s.get("wins", 0) or 0
        losses = s.get("losses", 0) or 0
        trades = wins + losses
        gw = s.get("gross_wins_sum", 0) or 0
        gl = s.get("gross_losses_sum", 0) or 0
        
        # Average winning/losing amount
        avg_win = query_one(
            "SELECT AVG(net_pnl) as v FROM trades WHERE strategy_id=? AND net_pnl > 0",
            (sid,),
        )
        avg_loss = query_one(
            "SELECT AVG(net_pnl) as v FROM trades WHERE strategy_id=? AND net_pnl <= 0",
            (sid,),
        )
        
        # Maximum winning/losing streak
        all_trades = query(
            "SELECT net_pnl FROM trades WHERE strategy_id=? ORDER BY exit_time",
            (sid,),
        )
        max_streak, max_losing = _calc_streaks([t["net_pnl"] for t in all_trades])
        
        # Monthly return rate
        monthly = query(
            """SELECT strftime('%Y-%m', exit_time) as month, SUM(net_pnl) as pnl
               FROM trades WHERE strategy_id=? GROUP BY month ORDER BY month""",
            (sid,),
        )
        
        pf = round(gw / gl, 2) if gl > 0 else None
        
        result.append({
            "strategy_id": sid,
            "balance": round(balance, 2),
            "return_pct": round((balance - initial) / initial * 100, 2),
            "trades": trades,
            "win_rate": round(wins / trades * 100, 1) if trades > 0 else 0,
            "profit_factor": pf,
            "max_drawdown_pct": round(s.get("max_drawdown_pct", 0) or 0, 2),
            "avg_win": round((avg_win or {}).get("v", 0) or 0, 2),
            "avg_loss": round((avg_loss or {}).get("v", 0) or 0, 2),
            "max_win_streak": max_streak,
            "max_loss_streak": max_losing,
            "liquidations": s.get("liquidations", 0) or 0,
            "monthly_pnl": monthly,
        })
    
    return {"strategies": result}

def _calc_streaks(pnls: list[float]) -> tuple[int, int]:
    max_win = max_loss = cur_win = cur_loss = 0
    for p in pnls:
        if p > 0:
            cur_win += 1
            cur_loss = 0
        else:
            cur_loss += 1
            cur_win = 0
        max_win = max(max_win, cur_win)
        max_loss = max(max_loss, cur_loss)
    return max_win, max_loss

# ──────────────────────────── API: Risk ────────────────────────────

@app.get("/api/risk", dependencies=[Depends(require_auth)])
async def risk_metrics():
    """Risk metrics."""
    positions = query("SELECT * FROM positions WHERE status='OPEN'")
    strategies = query("SELECT * FROM strategy_state")
    
    strat_map = {s["strategy_id"]: s for s in strategies}
    
    risk_items = []
    total_margin = 0
    total_notional = 0
    
    for p in positions:
        sid = p["strategy_id"]
        s = strat_map.get(sid, {})
        balance = s.get("account_balance", 10000)
        margin = p.get("margin_used", 0) or 0
        notional = p.get("notional", 0) or 0
        liq = p.get("liquidation_price", 0) or 0
        entry = p.get("actual_entry", 0) or p.get("entry_price", 0) or 0
        
        # Distance to liquidation
        if entry > 0 and liq > 0:
            if p["side"] == "LONG":
                dist_pct = round((entry - liq) / entry * 100, 2)
            else:
                dist_pct = round((liq - entry) / entry * 100, 2)
        else:
            dist_pct = 0
        
        margin_ratio = round(margin / balance * 100, 2) if balance > 0 else 0
        total_margin += margin
        total_notional += notional
        
        risk_items.append({
            "strategy_id": sid,
            "symbol": p["symbol"],
            "side": p["side"],
            "entry_price": entry,
            "liquidation_price": liq,
            "liq_distance_pct": dist_pct,
            "margin_used": round(margin, 2),
            "margin_ratio_pct": margin_ratio,
            "notional": round(notional, 2),
            "leverage": p.get("leverage", 1),
            "unrealized_pnl": round(p.get("unrealized_pnl", 0) or 0, 2),
            "candle_count": p.get("candle_count", 0),
        })
    
    total_balance = sum(s.get("account_balance", 0) for s in strategies)
    
    return {
        "positions": risk_items,
        "total_margin": round(total_margin, 2),
        "total_notional": round(total_notional, 2),
        "total_balance": round(total_balance, 2),
        "overall_margin_pct": round(total_margin / total_balance * 100, 2) if total_balance > 0 else 0,
    }

# ──────────────────────────── API: Activity Feed ────────────────────────────

@app.get("/api/feed", dependencies=[Depends(require_auth)])
async def activity_feed(limit: int = Query(50)):
    """Recent trade activity feed."""
    trades = query(
        "SELECT * FROM trades ORDER BY exit_time DESC LIMIT ?",
        (limit,),
    )
    return {"feed": trades}

# ──────────────────────────── Static Files ────────────────────────────

static_dir = Path(__file__).parent / "static"

@app.get("/")
async def root():
    return FileResponse(static_dir / "index.html")

@app.get("/favicon.ico")
async def favicon():
    return JSONResponse({})

if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# ──────────────────────────── Entry Point ────────────────────────────

if __name__ == "__main__":
    print(f"Dashboard: http://0.0.0.0:{DASHBOARD_PORT}")
    print(f"DB: {DB_PATH}")
    uvicorn.run(app, host="0.0.0.0", port=DASHBOARD_PORT)
