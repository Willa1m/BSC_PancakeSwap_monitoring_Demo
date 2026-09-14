"""
Telegram 实时播报。

结构（检测与发送分离）：
  enqueue()      —— 采集模块检测到事件后调用，只往 notifications 表写一行（dedup_key 唯一 → 不刷屏）
  sender_loop()  —— 独立线程消费发件箱：限频、失败退避重试、超过次数标 failed 并记错误
  bot_loop()     —— 独立线程长轮询 Telegram getUpdates，处理 /pool /wallet /sub /unsub /replay 命令

Telegram 挂了只影响这两个线程；采集线程完全不依赖它们（发件箱是唯一耦合点）。
不用第三方 bot 库，直接调 Bot API 的两个 HTTP 接口：sendMessage / getUpdates。
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, desc
from sqlalchemy.exc import IntegrityError

from . import config
from .models import (SessionLocal, Subscription, Notification, Position, PositionEvent,
                     PositionSnapshot, Pool, PoolSnapshot, SyncState, MonitorTask)

log = logging.getLogger("notifier")
API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def enabled():
    return bool(config.TELEGRAM_BOT_TOKEN)


# ---------------- 入队 ----------------
def enqueue(db, dedup_key, topic, text, chat_id=None):
    """
    给所有订阅了 topic（或 all）的 chat 各入一条；chat_id 指定时只发给它。
    dedup_key + chat_id 组成唯一键：同一事件对同一 chat 只入队一次。
    """
    if not enabled():
        return
    if chat_id:
        chats = [chat_id]
    else:
        chats = [s.chat_id for s in db.execute(
            select(Subscription).where(Subscription.topic.in_([topic, "all"]))).scalars()]
    for c in set(chats):
        db.add(Notification(dedup_key=f"{dedup_key}|{c}", topic=topic, chat_id=c, text=text,
                            status="pending", attempts=0, next_try=now(), created_at=now()))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()          # 已入队过，跳过


# ---------------- 发送 ----------------
def _send(chat_id, text):
    r = httpx.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text,
                                                "disable_web_page_preview": True}, timeout=10)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(body)


def process_outbox(db, send=_send):
    """处理一轮待发消息。send 参数可替换，方便测试和模拟失败。"""
    rows = db.execute(select(Notification).where(Notification.status == "pending",
                                                 Notification.next_try <= now())
                      .order_by(Notification.id).limit(20)).scalars().all()
    for n in rows:
        try:
            send(n.chat_id, n.text)
            n.status, n.sent_at, n.last_error = "sent", now(), None
        except Exception as e:
            n.attempts += 1
            n.last_error = str(e)[:300]
            if n.attempts >= config.TELEGRAM_MAX_RETRY:
                n.status = "failed"
                log.error("notification %s failed permanently: %s", n.id, e)
            else:
                n.next_try = now() + timedelta(seconds=5 * 2 ** n.attempts)   # 指数退避 10s,20s,40s...
        db.commit()
        time.sleep(config.TELEGRAM_SEND_INTERVAL)   # 限频


def sender_loop():
    while True:
        db = SessionLocal()
        try:
            process_outbox(db)
        except Exception as e:
            log.exception("sender: %s", e)
        finally:
            db.close()
        time.sleep(2)


# ---------------- 命令处理 ----------------
def fmt_pool(db, addr):
    p = db.get(Pool, addr)
    snap = db.execute(select(PoolSnapshot).where(PoolSnapshot.pool == addr).order_by(desc(PoolSnapshot.id)).limit(1)).scalar()
    st = db.get(SyncState, f"pool:{addr}")
    if not p or not snap:
        return f"池子 {addr[:10]}… 尚无数据"
    stale = "⚠过期" if (st and st.stale) else "✓"
    return (f"BSC｜{p.symbol0}/{p.symbol1} {p.fee/10000}%｜价格 {snap.price:.6g} {p.symbol1}/{p.symbol0}｜"
            f"流动性 {snap.liquidity}｜区块 {snap.block}｜{snap.ts:%m-%d %H:%M} UTC {stale}")


def fmt_wallet(db, wallet):
    rows = db.execute(select(Position).where(Position.wallet == wallet)).scalars().all()
    if not rows:
        return f"钱包 {wallet[:10]}… 未直接持有 V3 仓位（或尚未同步）"
    lines = [f"BSC｜钱包 {wallet[:10]}…｜{len(rows)} 个仓位"]
    for p in rows:
        snap = db.execute(select(PositionSnapshot).where(PositionSnapshot.token_id == p.token_id)
                          .order_by(desc(PositionSnapshot.id)).limit(1)).scalar()
        pnl = f"{float(snap.net_pnl_t1):.6g} {p.symbol1}" if snap else "—"
        lines.append(f"#{p.token_id} {p.symbol0}/{p.symbol1} {p.fee/10000}%｜{p.range_status or '?'}｜净收益 {pnl}")
    return "\n".join(lines)


def fmt_event(p: Position, e: PositionEvent, remaining_liq, replay=False):
    from .v3math import human
    kind = {"INCREASE": "增加流动性", "DECREASE": "减少流动性", "COLLECT": "领取", "TRANSFER": "NFT转移"}[e.kind]
    prefix = "【历史回放】" if replay else ""
    return (f"{prefix}BSC｜仓位 #{p.token_id}｜{kind}｜本次 {human(e.amount0, p.dec0):.6g} {p.symbol0} + "
            f"{human(e.amount1, p.dec1):.6g} {p.symbol1}｜剩余流动性 {remaining_liq}｜"
            f"区块 {e.block} {e.ts:%Y-%m-%d %H:%M} UTC｜{config.EXPLORER}/tx/{e.tx_hash}"
            + (f"｜{e.extra}" if e.extra else ""))


def remaining_liquidity_after(db, token_id, upto_event: PositionEvent):
    """按事件顺序累加流动性，得到某事件之后的剩余流动性（DECREASE 后能看到变化）"""
    total = 0
    for ev in db.execute(select(PositionEvent).where(PositionEvent.token_id == token_id)
                         .order_by(PositionEvent.block, PositionEvent.log_index)).scalars():
        if ev.kind == "INCREASE":
            total += int(ev.liquidity or 0)
        elif ev.kind == "DECREASE":
            total -= int(ev.liquidity or 0)
        if ev.id == upto_event.id:
            break
    return total


def handle_command(db, chat_id, text):
    parts = text.strip().split()
    cmd = parts[0].lower().split("@")[0]
    if cmd == "/start" or cmd == "/help":
        return ("命令：/pool [地址]  /wallet [地址]  /sub <liquidity|range|errors|all>  /unsub <topic>  "
                "/replay <NFT ID>  /subs")
    if cmd == "/pool":
        tasks = [t.address for t in db.execute(select(MonitorTask).where(MonitorTask.kind == "pool", MonitorTask.status == "active")).scalars()]
        addr = parts[1].lower() if len(parts) > 1 else (tasks[0] if tasks else None)
        return fmt_pool(db, addr) if addr else "没有配置池子"
    if cmd == "/wallet":
        tasks = [t.address for t in db.execute(select(MonitorTask).where(MonitorTask.kind == "wallet", MonitorTask.status == "active")).scalars()]
        w = parts[1].lower() if len(parts) > 1 else (tasks[0] if tasks else None)
        return fmt_wallet(db, w) if w else "没有配置钱包"
    if cmd == "/sub" and len(parts) > 1:
        db.add(Subscription(chat_id=str(chat_id), topic=parts[1]))
        try:
            db.commit(); return f"已订阅 {parts[1]}"
        except IntegrityError:
            db.rollback(); return "已经订阅过了"
    if cmd == "/unsub" and len(parts) > 1:
        for s in db.execute(select(Subscription).where(Subscription.chat_id == str(chat_id), Subscription.topic == parts[1])).scalars():
            db.delete(s)
        db.commit(); return f"已取消 {parts[1]}"
    if cmd == "/subs":
        subs = [s.topic for s in db.execute(select(Subscription).where(Subscription.chat_id == str(chat_id))).scalars()]
        return "当前订阅：" + (", ".join(subs) or "无")
    if cmd == "/replay" and len(parts) > 1:
        tid = int(parts[1]); p = db.get(Position, tid)
        if not p:
            return f"仓位 #{tid} 不在监控范围"
        evs = db.execute(select(PositionEvent).where(PositionEvent.token_id == tid)
                         .order_by(PositionEvent.block, PositionEvent.log_index)).scalars().all()
        if not evs:
            return f"仓位 #{tid} 在已采集范围内没有事件"
        for e in evs:
            enqueue(db, f"replay:{e.tx_hash}:{e.log_index}:{int(time.time())}", "replay",
                    fmt_event(p, e, remaining_liquidity_after(db, tid, e), replay=True), chat_id=str(chat_id))
        return f"已排队回放 {len(evs)} 条历史事件"
    return "不认识的命令，/help 看帮助"


def bot_loop():
    offset = None
    while True:
        try:
            r = httpx.get(f"{API}/getUpdates", params={"timeout": 25, "offset": offset}, timeout=35)
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                if "text" in msg and msg["text"].startswith("/"):
                    db = SessionLocal()
                    try:
                        reply = handle_command(db, msg["chat"]["id"], msg["text"])
                        enqueue(db, f"reply:{upd['update_id']}", "reply", reply, chat_id=str(msg["chat"]["id"]))
                    finally:
                        db.close()
        except Exception as e:
            log.warning("bot poll error: %s", e)
            time.sleep(5)


def start():
    if not enabled():
        log.info("TELEGRAM_BOT_TOKEN 未配置，Telegram 功能关闭")
        return
    for fn in (sender_loop, bot_loop):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
