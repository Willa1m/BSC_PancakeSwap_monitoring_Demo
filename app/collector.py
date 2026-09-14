"""
后台采集：在独立线程里持续运行，网页关了也在更新。

两个循环：
  pool_loop   —— 任务1：每 POLL_INTERVAL 秒读池子状态存快照 + 增量扫描 Swap 日志
  wallet_loop —— 任务2/3：每 WALLET_INTERVAL 秒同步钱包仓位、扫描仓位事件、写收益快照

可靠性设计：
- 每个采集对象一个 SyncState，成功才推进 last_block（断点续采）。
- 只处理 latest - CONFIRMATIONS 之前的区块，规避链上回退。
- 任何一步抛异常：记录 last_error、标记 stale，保留数据库里最后有效值，不写零。
- 一个池子/钱包出错不影响其他对象（try/except 包在单个对象级别）。
"""
import logging
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import chain, config, v3math, accounting, notifier
from .models import (SessionLocal, Pool, PoolSnapshot, Swap, SyncState,
                     Position, PositionEvent, PositionSnapshot, MonitorTask)

log = logging.getLogger("collector")


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------- SyncState 工具 ----------------
def get_state(db, key, default_from=None):
    st = db.get(SyncState, key)
    if st is None:
        st = SyncState(key=key, from_block=default_from, last_block=default_from, stale=False)
        db.add(st); db.commit()
    return st


def mark_ok(db, st, last_block=None):
    if last_block is not None:
        st.last_block = last_block
    st.updated_at = now(); st.stale = False; st.last_error = None
    db.commit()


def mark_error(db, st, err):
    was_ok = not st.stale
    st.stale = True; st.last_error = str(err)[:500]
    db.commit()
    log.warning("%s error: %s", st.key, err)
    if was_ok:   # 只在"正常→异常"的状态变化时播报一次，不是每次采样都发
        notifier.enqueue(db, f"err:{st.key}:{int(time.time())}", "errors",
                         f"⚠ 采集异常｜{st.key}｜{str(err)[:120]}｜最后成功区块 {st.last_block}")


# ---------------- 任务1：池子 ----------------
def ensure_pool(db, addr) -> Pool:
    p = db.get(Pool, addr)
    if p:
        return p
    c = chain.pool_contract(addr)
    t0, t1 = c.functions.token0().call(), c.functions.token1().call()
    e0, e1 = chain.erc20_contract(t0), chain.erc20_contract(t1)
    p = Pool(address=addr, token0=t0.lower(), token1=t1.lower(),
             symbol0=e0.functions.symbol().call(), symbol1=e1.functions.symbol().call(),
             dec0=e0.functions.decimals().call(), dec1=e1.functions.decimals().call(),
             fee=c.functions.fee().call())
    db.add(p); db.commit()
    return p


def poll_pool(db, addr):
    pool = ensure_pool(db, addr)
    c = chain.pool_contract(addr)
    st = get_state(db, f"pool:{addr}")
    try:
        blk = chain.latest_block()
        slot0 = c.functions.slot0().call(block_identifier=blk)
        liq = c.functions.liquidity().call(block_identifier=blk)
        price = v3math.sqrt_price_x96_to_price(slot0[0], pool.dec0, pool.dec1)
        db.add(PoolSnapshot(pool=addr, block=blk, ts=now(), sqrt_price_x96=str(slot0[0]),
                            price=float(price), liquidity=str(liq)))
        mark_ok(db, st, blk)
    except Exception as e:
        mark_error(db, st, e)


def scan_swaps(db, addr):
    pool = ensure_pool(db, addr)
    safe_head = chain.latest_block() - config.CONFIRMATIONS
    st = get_state(db, f"swaps:{addr}", default_from=safe_head - config.SWAP_LOOKBACK_BLOCKS)
    if st.last_block >= safe_head:
        return
    c = chain.pool_contract(addr)
    try:
        for chunk_end, logs in chain.get_logs_chunked(addr, [chain.TOPIC["Swap"]], st.last_block + 1, safe_head):
            for lg in logs:
                ev = c.events.Swap().process_log(lg)   # 用 ABI 把日志解码成字段
                a = ev["args"]
                row = Swap(pool=addr, block=lg["blockNumber"], ts=chain.log_timestamp(lg),
                           tx_hash=chain._hex(lg["transactionHash"]), log_index=lg["logIndex"],
                           sender=a["sender"].lower(), recipient=a["recipient"].lower(),
                           amount0=str(a["amount0"]), amount1=str(a["amount1"]),
                           price=float(v3math.sqrt_price_x96_to_price(a["sqrtPriceX96"], pool.dec0, pool.dec1)))
                db.add(row)
                try:
                    db.commit()
                except IntegrityError:      # 唯一约束命中 = 重复日志，跳过
                    db.rollback()
            mark_ok(db, st, chunk_end)      # 每块成功才推进断点
    except Exception as e:
        mark_error(db, st, e)


# ---------------- 任务2：钱包仓位 ----------------
def sync_positions(db, wallet):
    """
    读取钱包直接持有的 Position NFT。
    注意：质押到 MasterChefV3 挖矿的仓位，NFT 所有者是 MasterChef 合约，这里读不到——属于已知限制。
    """
    npm = chain.npm_contract()
    fac = chain.factory_contract()
    st = get_state(db, f"wallet:{wallet}")
    try:
        blk = chain.latest_block()
        n = npm.functions.balanceOf(chain.cs(wallet)).call(block_identifier=blk)
        seen = []
        for i in range(n):
            tid = npm.functions.tokenOfOwnerByIndex(chain.cs(wallet), i).call(block_identifier=blk)
            pos = npm.functions.positions(tid).call(block_identifier=blk)
            (_, _, t0, t1, fee, tl, tu, liq, _, _, owed0, owed1) = pos
            pool_addr = fac.functions.getPool(t0, t1, fee).call().lower()
            e0, e1 = chain.erc20_contract(t0), chain.erc20_contract(t1)
            p = db.get(Position, tid) or Position(token_id=tid)
            p.wallet = wallet; p.pool = pool_addr
            p.token0, p.token1, p.fee = t0.lower(), t1.lower(), fee
            p.symbol0, p.symbol1 = e0.functions.symbol().call(), e1.functions.symbol().call()
            p.dec0, p.dec1 = e0.functions.decimals().call(), e1.functions.decimals().call()
            p.tick_lower, p.tick_upper = tl, tu
            p.liquidity, p.tokens_owed0, p.tokens_owed1 = str(liq), str(owed0), str(owed1)
            p.updated_at = now()
            db.merge(p); db.commit()
            seen.append(tid)
        mark_ok(db, st, blk)
        return seen
    except Exception as e:
        mark_error(db, st, e)
        return None


def scan_position_events(db, token_ids):
    """批量按 tokenId 过滤 NPM 的四种事件，写入时间线（唯一约束去重）。"""
    token_ids = list(dict.fromkeys(token_ids))
    if not token_ids:
        return
    npm = chain.npm_contract()
    safe_head = chain.latest_block() - config.CONFIRMATIONS
    states = {
        token_id: get_state(db, f"events:{token_id}",
                            default_from=safe_head - config.POSITION_LOOKBACK_BLOCKS)
        for token_id in token_ids
    }
    pending = {token_id: st for token_id, st in states.items() if st.last_block < safe_head}
    if not pending:
        return
    from_block = min(st.last_block for st in pending.values()) + 1
    tid_topics = [chain.token_id_topic(token_id) for token_id in pending]
    # 三种事件 tokenId 在 topic[1]，Transfer 的 tokenId 在 topic[3]，所以分两次查
    specs = [
        ([[chain.TOPIC["IncreaseLiquidity"], chain.TOPIC["DecreaseLiquidity"], chain.TOPIC["Collect"]], tid_topics], "liq"),
        ([chain.TOPIC["Transfer"], None, None, tid_topics], "transfer"),
    ]
    try:
        start = from_block
        while start <= safe_head:
            chunk_end = min(start + config.LOG_CHUNK - 1, safe_head)
            for topics, group in specs:
                for _, logs in chain.get_logs_chunked(
                        config.NPM_ADDRESS, topics, start, chunk_end, chunk=config.LOG_CHUNK):
                    for lg in logs:
                        t0 = chain._hex(lg["topics"][0])
                        topic_index = 3 if group == "transfer" else 1
                        token_id = int(chain._hex(lg["topics"][topic_index]), 16)
                        if group == "transfer":
                            ev = npm.events.Transfer().process_log(lg)
                            kind, a0, a1, liq = "TRANSFER", "0", "0", "0"
                            extra = f'{ev["args"]["from"]} -> {ev["args"]["to"]}'
                        else:
                            name = {chain.TOPIC["IncreaseLiquidity"]: "IncreaseLiquidity",
                                    chain.TOPIC["DecreaseLiquidity"]: "DecreaseLiquidity",
                                    chain.TOPIC["Collect"]: "Collect"}[t0]
                            ev = getattr(npm.events, name)().process_log(lg)
                            args = ev["args"]
                            kind = {"IncreaseLiquidity": "INCREASE", "DecreaseLiquidity": "DECREASE", "Collect": "COLLECT"}[name]
                            a0, a1 = str(args["amount0"]), str(args["amount1"])
                            liq = str(args.get("liquidity", 0)); extra = None
                        ev_row = PositionEvent(token_id=token_id, block=lg["blockNumber"],
                                               ts=chain.log_timestamp(lg),
                                               tx_hash=chain._hex(lg["transactionHash"]), log_index=lg["logIndex"],
                                               kind=kind, amount0=a0, amount1=a1, liquidity=liq, extra=extra)
                        db.add(ev_row)
                        try:
                            db.commit()
                        except IntegrityError:
                            db.rollback(); continue      # 已存在的事件不再播报
                        # 新事件 → 入发件箱（链上操作消息按确认策略触发：这里已经是 CONFIRMATIONS 之后的区块）
                        p = db.get(Position, token_id)
                        if p and kind != "TRANSFER":
                            notifier.enqueue(
                                db, f"evt:{ev_row.tx_hash}:{ev_row.log_index}", "liquidity",
                                notifier.fmt_event(p, ev_row, notifier.remaining_liquidity_after(db, token_id, ev_row)))
            # 同一页的流动性事件和 Transfer 都成功后才推进，失败可从当前页恢复。
            for st in pending.values():
                mark_ok(db, st, chunk_end)
            start = chunk_end + 1
    except Exception as e:
        for st in pending.values():
            mark_error(db, st, e)


# ---------------- 任务3：收益快照 ----------------
def snapshot_position(db, p: Position):
    """用当前池子价格给仓位估值，并按事件流水算净收益，存一个曲线点"""
    try:
        c = chain.pool_contract(p.pool)
        blk = chain.latest_block()
        slot0 = c.functions.slot0().call(block_identifier=blk)
        price = v3math.sqrt_price_x96_to_price(slot0[0], p.dec0, p.dec1)
        amt0, amt1 = v3math.amounts_from_liquidity(
            int(p.liquidity), Decimal(slot0[0]),
            v3math.tick_to_sqrt_price_x96(p.tick_lower), v3math.tick_to_sqrt_price_x96(p.tick_upper))
        events = db.execute(select(PositionEvent).where(PositionEvent.token_id == p.token_id)).scalars().all()
        flows = accounting.split_flows([{"kind": e.kind, "amount0": e.amount0, "amount1": e.amount1} for e in events])
        r = accounting.net_pnl(flows, amt0, amt1, int(p.tokens_owed0), int(p.tokens_owed1), price, p.dec0, p.dec1)
        # 进入/离开区间：按状态变化触发，不是每次采样都发
        status = v3math.range_status(slot0[1], p.tick_lower, p.tick_upper)
        if p.range_status and status != p.range_status:
            lo = v3math.sqrt_price_x96_to_price(int(v3math.tick_to_sqrt_price_x96(p.tick_lower)), p.dec0, p.dec1)
            hi = v3math.sqrt_price_x96_to_price(int(v3math.tick_to_sqrt_price_x96(p.tick_upper)), p.dec0, p.dec1)
            notifier.enqueue(db, f"range:{p.token_id}:{p.range_status}->{status}:{blk}", "range",
                             f"BSC｜仓位 #{p.token_id}｜{p.range_status} → {status}｜当前价 {price:.6g}｜"
                             f"区间 {lo:.6g} ~ {hi:.6g} {p.symbol1}/{p.symbol0}｜观察时间 {now():%Y-%m-%d %H:%M} UTC 区块 {blk}")
        p.range_status = status
        db.add(PositionSnapshot(token_id=p.token_id, block=blk, ts=now(), price=float(price),
                                value_t1=str(r["value_t1"]), fees_collected_t1=str(r["fees_collected_t1"]),
                                invested_t1=str(r["invested_t1"]), returned_t1=str(r["returned_t1"]),
                                net_pnl_t1=str(r["net_pnl_t1"])))
        db.commit()
    except Exception as e:
        log.warning("snapshot %s failed: %s", p.token_id, e)
        db.rollback()


# ---------------- 任务5：任务表驱动的主循环 ----------------
_task_locks = {}
_locks_guard = threading.Lock()


def task_lock(key):
    """并发控制：同一个对象同一时间只有一个线程在采（例如补采和实时采集不会同时写同一断点）"""
    with _locks_guard:
        return _task_locks.setdefault(key, threading.Lock())


def seed_tasks_from_env():
    db = SessionLocal()
    try:
        for kind, addrs in (("pool", config.POOLS), ("wallet", config.WALLETS)):
            for a in addrs:
                if not db.execute(select(MonitorTask).where(MonitorTask.kind == kind, MonitorTask.address == a)).scalar():
                    db.add(MonitorTask(kind=kind, address=a, status="active", created_at=now()))
        db.commit()
    finally:
        db.close()


def active_tasks(kind):
    db = SessionLocal()
    try:
        return [t.address for t in db.execute(select(MonitorTask).where(MonitorTask.kind == kind, MonitorTask.status == "active")).scalars()]
    finally:
        db.close()


def run_pool_task(addr):
    lock = task_lock(f"pool:{addr}")
    if not lock.acquire(blocking=False):
        return                      # 上一轮还没跑完（或正在补采），跳过这轮
    db = SessionLocal()
    try:
        poll_pool(db, addr)
        scan_swaps(db, addr)
    except Exception as e:           # 单个池子的意外错误不影响其他池子
        log.exception("pool %s: %s", addr, e)
    finally:
        db.close(); lock.release()


def run_wallet_task(w):
    lock = task_lock(f"wallet:{w}")
    if not lock.acquire(blocking=False):
        return
    db = SessionLocal()
    try:
        ids = sync_positions(db, w)
        scan_position_events(db, ids or [])
        for tid in ids or []:
            snapshot_position(db, db.get(Position, tid))
    except Exception as e:
        log.exception("wallet %s: %s", w, e)
    finally:
        db.close(); lock.release()


def pool_loop():
    while True:
        for addr in active_tasks("pool"):     # 每轮重新读任务表：暂停/删除立即生效
            run_pool_task(addr)
        time.sleep(config.POLL_INTERVAL)


def wallet_loop():
    while True:
        for w in active_tasks("wallet"):
            run_wallet_task(w)
        time.sleep(config.WALLET_INTERVAL)


def backfill_swaps(addr, from_block, to_block):
    """历史补采：指定区块范围重扫 Swap。幂等（唯一约束），与实时采集共用锁不并发。"""
    lock = task_lock(f"pool:{addr}")
    with lock:
        db = SessionLocal()
        try:
            pool = ensure_pool(db, addr)
            c = chain.pool_contract(addr)
            st = get_state(db, f"backfill:swaps:{addr}", default_from=from_block)
            st.from_block = from_block; db.commit()
            n = 0
            for chunk_end, logs in chain.get_logs_chunked(addr, [chain.TOPIC["Swap"]], from_block, to_block):
                for lg in logs:
                    a = c.events.Swap().process_log(lg)["args"]
                    db.add(Swap(pool=addr, block=lg["blockNumber"], ts=chain.log_timestamp(lg),
                                tx_hash=chain._hex(lg["transactionHash"]), log_index=lg["logIndex"],
                                sender=a["sender"].lower(), recipient=a["recipient"].lower(),
                                amount0=str(a["amount0"]), amount1=str(a["amount1"]),
                                price=float(v3math.sqrt_price_x96_to_price(a["sqrtPriceX96"], pool.dec0, pool.dec1))))
                    try:
                        db.commit(); n += 1
                    except IntegrityError:
                        db.rollback()
                mark_ok(db, st, chunk_end)
            # 补采范围早于原覆盖起点时，把覆盖起点往前挪，网页显示的覆盖范围才真实
            main = get_state(db, f"swaps:{addr}")
            if main.from_block is None or from_block < main.from_block:
                main.from_block = from_block; db.commit()
            log.info("backfill %s %s-%s: %s new swaps", addr, from_block, to_block, n)
        except Exception as e:
            log.exception("backfill %s: %s", addr, e)
        finally:
            db.close()


def verify_swaps(addr, from_block, to_block):
    """
    补采完成后的完整性检查：重新从 RPC 拉该范围的 Swap 日志，和数据库逐条比对。
    返回缺失（链上有库里没有）和多余（库里有链上没有）的 (tx, log_index) 列表。
    """
    onchain = set()
    for _, logs in chain.get_logs_chunked(addr, [chain.TOPIC["Swap"]], from_block, to_block):
        for lg in logs:
            onchain.add((chain._hex(lg["transactionHash"]), lg["logIndex"]))
    db = SessionLocal()
    try:
        rows = db.execute(select(Swap.tx_hash, Swap.log_index).where(
            Swap.pool == addr, Swap.block >= from_block, Swap.block <= to_block)).all()
        indb = set((r[0], r[1]) for r in rows)
        dup = len(rows) - len(indb)
    finally:
        db.close()
    return {"range": [from_block, to_block], "onchain": len(onchain), "in_db": len(indb),
            "missing": sorted(onchain - indb)[:50], "extra": sorted(indb - onchain)[:50], "duplicates_in_db": dup}


def start():
    seed_tasks_from_env()
    for fn in (pool_loop, wallet_loop):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
