"""
查询接口。网页只读数据库，不直接访问 RPC（缓存与查询优化的最简实现）。
每个接口都带 stale / coverage 信息，让前端能区分"零值 / 缺失 / 过期"。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select, func, desc

from . import config, v3math
from .models import (SessionLocal, Pool, PoolSnapshot, Swap, SyncState,
                     Position, PositionEvent, PositionSnapshot)

router = APIRouter(prefix="/api")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _state(db, key):
    st = db.get(SyncState, key)
    if not st:
        return {"exists": False, "stale": True, "last_error": "not started"}
    age = (_now() - st.updated_at).total_seconds() if st.updated_at else None
    return {"exists": True, "from_block": st.from_block, "last_block": st.last_block,
            "updated_at": st.updated_at, "age_seconds": age,
            "stale": bool(st.stale or age is None or age > config.STALE_AFTER),
            "last_error": st.last_error}


@router.get("/health")
def health():
    with SessionLocal() as db:
        return {"ok": True, "states": {s.key: _state(db, s.key) for s in db.execute(select(SyncState)).scalars()}}


@router.get("/pools")
def pools():
    from .models import MonitorTask
    with SessionLocal() as db:
        ts = db.execute(select(MonitorTask).where(MonitorTask.status == "active")).scalars().all()
        return {"pools": [t.address for t in ts if t.kind == "pool"],
                "wallets": [t.address for t in ts if t.kind == "wallet"]}


@router.get("/pools/{addr}")
def pool_summary(addr: str):
    addr = addr.lower()
    with SessionLocal() as db:
        p = db.get(Pool, addr)
        if not p:
            raise HTTPException(404, "pool not collected yet")
        snap = db.execute(select(PoolSnapshot).where(PoolSnapshot.pool == addr)
                          .order_by(desc(PoolSnapshot.id)).limit(1)).scalar()
        swap_state = _state(db, f"swaps:{addr}")
        # 24h 成交量：以 token1 计价，只统计已采集范围；范围不足 24h 就如实报告覆盖区间
        since = _now() - timedelta(hours=24)
        rows = db.execute(select(Swap.amount1, Swap.ts).where(Swap.pool == addr, Swap.ts >= since)).all()
        vol = sum(abs(Decimal(r[0])) for r in rows)
        earliest = min((r[1] for r in rows), default=None)
        return {
            "pair": f"{p.symbol0}/{p.symbol1}", "fee_pct": p.fee / 10000,
            "price": snap.price if snap else None,
            "price_unit": f"{p.symbol1} per {p.symbol0}",
            "liquidity": snap.liquidity if snap else None,
            "block": snap.block if snap else None,
            "updated_at": snap.ts if snap else None,
            "pool_state": _state(db, f"pool:{addr}"),
            "volume_24h": {"value": str(v3math.human(vol, p.dec1)), "unit": p.symbol1,
                           "swaps": len(rows), "covered_from": earliest,
                           "note": "仅统计已采集到的 Swap；若 covered_from 距今不足24h，则不是完整24h数据"},
            "swap_sync": swap_state,
        }


@router.get("/pools/{addr}/prices")
def pool_prices(addr: str, hours: int = Query(24, ge=1, le=720)):
    with SessionLocal() as db:
        since = _now() - timedelta(hours=hours)
        rows = db.execute(select(PoolSnapshot).where(PoolSnapshot.pool == addr.lower(), PoolSnapshot.ts >= since)
                          .order_by(PoolSnapshot.ts)).scalars().all()
        return [{"ts": r.ts, "block": r.block, "price": r.price} for r in rows]


@router.get("/pools/{addr}/swaps")
def pool_swaps(addr: str, limit: int = Query(30, ge=1, le=200)):
    with SessionLocal() as db:
        p = db.get(Pool, addr.lower())
        if not p:
            raise HTTPException(404)
        rows = db.execute(select(Swap).where(Swap.pool == addr.lower())
                          .order_by(desc(Swap.block), desc(Swap.log_index)).limit(limit)).scalars().all()
        out = []
        for s in rows:
            a0, a1 = int(s.amount0), int(s.amount1)
            # amount0 > 0 表示交易者把 token0 卖给池子（池子收到 token0），即 卖 token0 / 买 token1
            direction = f"sell {p.symbol0}" if a0 > 0 else f"buy {p.symbol0}"
            out.append({"ts": s.ts, "block": s.block, "direction": direction,
                        "amount0": str(v3math.human(abs(a0), p.dec0)), "amount1": str(v3math.human(abs(a1), p.dec1)),
                        "price": s.price, "tx": f"{config.EXPLORER}/tx/{s.tx_hash}", "log_index": s.log_index})
        return out


@router.get("/wallets/{wallet}/positions")
def wallet_positions(wallet: str):
    wallet = wallet.lower()
    with SessionLocal() as db:
        st = _state(db, f"wallet:{wallet}")
        rows = db.execute(select(Position).where(Position.wallet == wallet)).scalars().all()
        out = []
        for p in rows:
            snap = db.execute(select(PositionSnapshot).where(PositionSnapshot.token_id == p.token_id)
                              .order_by(desc(PositionSnapshot.id)).limit(1)).scalar()
            out.append({
                "token_id": p.token_id, "pair": f"{p.symbol0}/{p.symbol1}", "fee_pct": p.fee / 10000,
                "pool": p.pool, "tick_lower": p.tick_lower, "tick_upper": p.tick_upper,
                "price_lower": str(v3math.sqrt_price_x96_to_price(int(v3math.tick_to_sqrt_price_x96(p.tick_lower)), p.dec0, p.dec1)),
                "price_upper": str(v3math.sqrt_price_x96_to_price(int(v3math.tick_to_sqrt_price_x96(p.tick_upper)), p.dec0, p.dec1)),
                "liquidity": p.liquidity,
                "unclaimed_fees": {"token0": str(v3math.human(p.tokens_owed0, p.dec0)),
                                   "token1": str(v3math.human(p.tokens_owed1, p.dec1)),
                                   "note": "tokensOwed 只在链上仓位被触碰(增减/领取)时结算，可能小于实际累计"},
                "value_t1": snap.value_t1 if snap else None,
                "net_pnl_t1": snap.net_pnl_t1 if snap else None,
                "current_price": snap.price if snap else None,
                "in_range": (None if not snap else
                             float(out_range_check(snap.price, p, v3math))),
                "events_sync": _state(db, f"events:{p.token_id}"),
            })
        return {"wallet": wallet, "wallet_sync": st, "positions": out,
                "note": None if out else "该钱包当前未直接持有 PancakeSwap V3 仓位 NFT（质押在 MasterChefV3 的仓位不在此范围）"}


def out_range_check(price, p, m):
    lo = m.sqrt_price_x96_to_price(int(m.tick_to_sqrt_price_x96(p.tick_lower)), p.dec0, p.dec1)
    hi = m.sqrt_price_x96_to_price(int(m.tick_to_sqrt_price_x96(p.tick_upper)), p.dec0, p.dec1)
    return 1 if lo <= Decimal(price) < hi else 0


@router.get("/positions/{token_id}/timeline")
def position_timeline(token_id: int):
    with SessionLocal() as db:
        p = db.get(Position, token_id)
        if not p:
            raise HTTPException(404)
        rows = db.execute(select(PositionEvent).where(PositionEvent.token_id == token_id)
                          .order_by(PositionEvent.block, PositionEvent.log_index)).scalars().all()
        return {"coverage": _state(db, f"events:{token_id}"),
                "events": [{"ts": e.ts, "block": e.block, "kind": e.kind,
                            "amount0": str(v3math.human(e.amount0, p.dec0)), "amount1": str(v3math.human(e.amount1, p.dec1)),
                            "liquidity_delta": e.liquidity, "extra": e.extra,
                            "tx": f"{config.EXPLORER}/tx/{e.tx_hash}", "log_index": e.log_index} for e in rows]}


@router.get("/positions/{token_id}/curve")
def position_curve(token_id: int, hours: int = Query(168, ge=1, le=2000)):
    with SessionLocal() as db:
        since = _now() - timedelta(hours=hours)
        rows = db.execute(select(PositionSnapshot).where(PositionSnapshot.token_id == token_id, PositionSnapshot.ts >= since)
                          .order_by(PositionSnapshot.ts)).scalars().all()
        events = db.execute(select(PositionEvent).where(PositionEvent.token_id == token_id,
                                                        PositionEvent.kind.in_(["INCREASE", "DECREASE"]))).scalars().all()
        return {"points": [{"ts": r.ts, "value": r.value_t1, "fees": r.fees_collected_t1,
                            "net_pnl": r.net_pnl_t1, "invested": r.invested_t1, "returned": r.returned_t1} for r in rows],
                "markers": [{"ts": e.ts, "kind": e.kind} for e in events]}
