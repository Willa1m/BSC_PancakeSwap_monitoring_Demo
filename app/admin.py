"""
任务5：监控任务管理接口。所有接口需要请求头 X-API-Key（基本鉴权）。

语义：
- 新增：写 monitor_tasks，采集循环下一轮自动带上，不改代码。
- 暂停/恢复：只改 status；SyncState 保留，恢复后从上次断点继续，所以暂停期间的数据是"有缺口"而不是"补齐"，可解释。
- 删除：status=deleted，只停后续采集；历史数据仍在，删数据是独立操作 DELETE /tasks/{id}/data。
- 补采：后台线程跑指定区块范围，幂等；校验接口比对链上与库内。
"""
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, delete

from . import config, collector
from .models import (SessionLocal, MonitorTask, SyncState, Swap, PoolSnapshot,
                     Position, PositionEvent, PositionSnapshot, Notification)
from .api import _state


def auth(x_api_key: str = Header(default="")):
    if x_api_key != config.ADMIN_API_KEY:
        raise HTTPException(401, "bad api key")


router = APIRouter(prefix="/api/admin", dependencies=[Depends(auth)])


class NewTask(BaseModel):
    kind: str      # pool / wallet
    address: str


def _task_view(db, t: MonitorTask):
    keys = ([f"pool:{t.address}", f"swaps:{t.address}", f"backfill:swaps:{t.address}"] if t.kind == "pool"
            else [f"wallet:{t.address}"])
    if t.kind == "wallet":
        keys += [f"events:{p.token_id}" for p in db.execute(select(Position).where(Position.wallet == t.address)).scalars()]
    return {"id": t.id, "kind": t.kind, "address": t.address, "status": t.status, "created_at": t.created_at,
            "sync": {k: _state(db, k) for k in keys}}


@router.get("/tasks")
def list_tasks(page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), status: str | None = None):
    with SessionLocal() as db:
        q = select(MonitorTask)
        if status:
            q = q.where(MonitorTask.status == status)
        rows = db.execute(q.order_by(MonitorTask.id).offset((page - 1) * size).limit(size)).scalars().all()
        return {"page": page, "size": size, "tasks": [_task_view(db, t) for t in rows]}


@router.post("/tasks")
def add_task(body: NewTask):
    if body.kind not in ("pool", "wallet"):
        raise HTTPException(422, "kind must be pool or wallet")
    addr = body.address.lower()
    if not (addr.startswith("0x") and len(addr) == 42):
        raise HTTPException(422, "bad address")
    with SessionLocal() as db:
        t = db.execute(select(MonitorTask).where(MonitorTask.kind == body.kind, MonitorTask.address == addr)).scalar()
        if t:
            t.status = "active"
        else:
            t = MonitorTask(kind=body.kind, address=addr, status="active",
                            created_at=datetime.now(timezone.utc).replace(tzinfo=None))
            db.add(t)
        db.commit()
        return _task_view(db, t)


def _set_status(task_id, status):
    with SessionLocal() as db:
        t = db.get(MonitorTask, task_id)
        if not t:
            raise HTTPException(404)
        t.status = status; db.commit()
        return _task_view(db, t)


@router.post("/tasks/{task_id}/pause")
def pause(task_id: int):  return _set_status(task_id, "paused")


@router.post("/tasks/{task_id}/resume")
def resume(task_id: int): return _set_status(task_id, "active")


@router.delete("/tasks/{task_id}")
def remove(task_id: int): return _set_status(task_id, "deleted")


@router.delete("/tasks/{task_id}/data")
def remove_data(task_id: int):
    """独立操作：删除该对象的历史数据（先确认任务已删除或暂停）"""
    with SessionLocal() as db:
        t = db.get(MonitorTask, task_id)
        if not t:
            raise HTTPException(404)
        if t.status == "active":
            raise HTTPException(409, "pause or delete the task first")
        a = t.address
        if t.kind == "pool":
            db.execute(delete(Swap).where(Swap.pool == a))
            db.execute(delete(PoolSnapshot).where(PoolSnapshot.pool == a))
            db.execute(delete(SyncState).where(SyncState.key.in_([f"pool:{a}", f"swaps:{a}", f"backfill:swaps:{a}"])))
        else:
            ids = [p.token_id for p in db.execute(select(Position).where(Position.wallet == a)).scalars()]
            db.execute(delete(PositionEvent).where(PositionEvent.token_id.in_(ids)))
            db.execute(delete(PositionSnapshot).where(PositionSnapshot.token_id.in_(ids)))
            db.execute(delete(Position).where(Position.wallet == a))
            db.execute(delete(SyncState).where(SyncState.key.in_([f"wallet:{a}"] + [f"events:{i}" for i in ids])))
        db.commit()
        return {"deleted": True}


@router.post("/tasks/{task_id}/backfill")
def backfill(task_id: int, from_block: int, to_block: int):
    with SessionLocal() as db:
        t = db.get(MonitorTask, task_id)
        if not t or t.kind != "pool":
            raise HTTPException(404, "only pool tasks support backfill in this demo")
        if to_block - from_block > 2_000_000:
            raise HTTPException(422, "range too large")
    threading.Thread(target=collector.backfill_swaps, args=(t.address, from_block, to_block), daemon=True).start()
    return {"started": True, "progress_key": f"backfill:swaps:{t.address}"}


@router.get("/tasks/{task_id}/verify")
def verify(task_id: int, from_block: int, to_block: int):
    with SessionLocal() as db:
        t = db.get(MonitorTask, task_id)
        if not t or t.kind != "pool":
            raise HTTPException(404)
        if to_block - from_block > 50_000:
            raise HTTPException(422, "verify at most 50000 blocks at a time")
    return collector.verify_swaps(t.address, from_block, to_block)


@router.get("/notifications")
def notifications(status: str | None = None, limit: int = Query(50, le=200)):
    with SessionLocal() as db:
        q = select(Notification)
        if status:
            q = q.where(Notification.status == status)
        rows = db.execute(q.order_by(Notification.id.desc()).limit(limit)).scalars().all()
        return [{"id": n.id, "status": n.status, "topic": n.topic, "chat_id": n.chat_id, "attempts": n.attempts,
                 "last_error": n.last_error, "created_at": n.created_at, "sent_at": n.sent_at, "text": n.text} for n in rows]
