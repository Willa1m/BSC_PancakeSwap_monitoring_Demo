"""通知模块的可测部分：去重入队、失败重试退避、超限标记 failed。用 SQLite 内存库 + 假发送函数。"""
import os
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["TELEGRAM_SEND_INTERVAL"] = "0"
os.environ["TELEGRAM_MAX_RETRY"] = "2"
from datetime import datetime, timedelta
from app import models, notifier
from app.models import SessionLocal, Subscription, Notification
from sqlalchemy import select

def setup_module(m):
    models.init_db()
    with SessionLocal() as db:
        db.add(Subscription(chat_id="c1", topic="liquidity")); db.add(Subscription(chat_id="c2", topic="all")); db.commit()

def test_enqueue_dedup_and_topic_routing():
    with SessionLocal() as db:
        notifier.enqueue(db, "evt:tx1:0", "liquidity", "hello")
        notifier.enqueue(db, "evt:tx1:0", "liquidity", "hello")     # 同一事件再来一次
        notifier.enqueue(db, "range:1:a->b", "range", "range msg")  # c1 没订 range，只有 c2(all) 收
        rows = db.execute(select(Notification)).scalars().all()
        assert sorted((n.dedup_key, n.chat_id) for n in rows) == [
            ("evt:tx1:0|c1", "c1"), ("evt:tx1:0|c2", "c2"), ("range:1:a->b|c2", "c2")]

def test_retry_then_failed():
    calls = []
    def bad_send(chat, text): calls.append(text); raise RuntimeError("telegram down")
    with SessionLocal() as db:
        notifier.process_outbox(db, send=bad_send)
        n = db.execute(select(Notification).where(Notification.dedup_key == "evt:tx1:0|c1")).scalar()
        assert n.status == "pending" and n.attempts == 1 and n.next_try > datetime.utcnow()   # 退避，等一会再试
        n.next_try = datetime.utcnow() - timedelta(seconds=1); db.commit()
        notifier.process_outbox(db, send=bad_send)
        db.refresh(n)
        assert n.status == "failed" and "telegram down" in n.last_error

def test_success_marks_sent():
    with SessionLocal() as db:
        n = db.execute(select(Notification).where(Notification.dedup_key == "range:1:a->b|c2")).scalar()
        n.next_try = datetime.utcnow() - timedelta(seconds=1); db.commit()
        notifier.process_outbox(db, send=lambda c, t: None)
        db.refresh(n); assert n.status == "sent"
