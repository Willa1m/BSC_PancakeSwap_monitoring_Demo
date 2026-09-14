"""
数据库表设计（SQLAlchemy 2.x）。

设计要点：
1. 链上金额是超大整数（uint256），SQLite 的 Numeric 会退化成 float 丢精度，
   所以原始金额一律以字符串保存，计算时 int()/Decimal() 转回，不用 float。
2. 事件表用 (tx_hash, log_index) 唯一约束去重：链上一条日志只会被写入一次，
   重复扫描 / 重启后再扫不会重复记账。
3. SyncState 记录每个采集任务扫到了哪个区块，用于断点续采和"实际覆盖范围"展示。
"""
from datetime import datetime
from sqlalchemy import (Column, Integer, String, Boolean, DateTime, Float,
                        UniqueConstraint, create_engine, Index)
from sqlalchemy.orm import declarative_base, sessionmaker
from . import config

Base = declarative_base()


class Pool(Base):
    __tablename__ = "pools"
    address = Column(String, primary_key=True)
    token0 = Column(String); token1 = Column(String)
    symbol0 = Column(String); symbol1 = Column(String)
    dec0 = Column(Integer); dec1 = Column(Integer)
    fee = Column(Integer)   # 单位 1e-6，2500 = 0.25%


class PoolSnapshot(Base):
    """每次轮询保存一个价格点（价格曲线的数据来源）"""
    __tablename__ = "pool_snapshots"
    id = Column(Integer, primary_key=True)
    pool = Column(String, index=True)
    block = Column(Integer)
    ts = Column(DateTime)
    sqrt_price_x96 = Column(String)
    price = Column(Float)            # token0 以 token1 计价，仅用于展示，核算不用它
    liquidity = Column(String)
    __table_args__ = (Index("ix_snap_pool_ts", "pool", "ts"),)


class Swap(Base):
    __tablename__ = "swaps"
    id = Column(Integer, primary_key=True)
    pool = Column(String, index=True)
    block = Column(Integer)
    ts = Column(DateTime)
    tx_hash = Column(String)
    log_index = Column(Integer)
    sender = Column(String); recipient = Column(String)
    amount0 = Column(String)   # 有符号：正=池子收到，负=池子付出
    amount1 = Column(String)
    price = Column(Float)
    __table_args__ = (UniqueConstraint("tx_hash", "log_index", name="uq_swap_log"),)


class SyncState(Base):
    __tablename__ = "sync_state"
    key = Column(String, primary_key=True)   # 例如 "swaps:<pool>" / "events:<tokenId>" / "pool:<pool>"
    from_block = Column(Integer)             # 覆盖范围起点
    last_block = Column(Integer)             # 已成功处理到的区块
    updated_at = Column(DateTime)            # 最后一次成功时间
    last_error = Column(String)
    stale = Column(Boolean, default=False)


class Position(Base):
    __tablename__ = "positions"
    token_id = Column(Integer, primary_key=True)
    wallet = Column(String, index=True)
    pool = Column(String)
    token0 = Column(String); token1 = Column(String)
    symbol0 = Column(String); symbol1 = Column(String)
    dec0 = Column(Integer); dec1 = Column(Integer)
    fee = Column(Integer)
    tick_lower = Column(Integer); tick_upper = Column(Integer)
    liquidity = Column(String)
    tokens_owed0 = Column(String); tokens_owed1 = Column(String)   # 已结算、未领取
    range_status = Column(String)   # in_range / below / above，用于"进入/离开区间"按状态变化触发
    updated_at = Column(DateTime)


class PositionEvent(Base):
    """仓位操作时间线：INCREASE / DECREASE / COLLECT / TRANSFER 是四种不同事件"""
    __tablename__ = "position_events"
    id = Column(Integer, primary_key=True)
    token_id = Column(Integer, index=True)
    block = Column(Integer)
    ts = Column(DateTime)
    tx_hash = Column(String)
    log_index = Column(Integer)
    kind = Column(String)
    amount0 = Column(String); amount1 = Column(String)
    liquidity = Column(String)      # INCREASE/DECREASE 的流动性变化量
    extra = Column(String)          # TRANSFER 的 from->to
    __table_args__ = (UniqueConstraint("tx_hash", "log_index", name="uq_pos_log"),)


class PositionSnapshot(Base):
    """收益曲线的数据点：仓位价值 / 累计手续费 / 净收益（以 token1 计价）"""
    __tablename__ = "position_snapshots"
    id = Column(Integer, primary_key=True)
    token_id = Column(Integer, index=True)
    block = Column(Integer)
    ts = Column(DateTime)
    price = Column(Float)
    value_t1 = Column(String)          # 当前仓位价值（含未领取手续费）
    fees_collected_t1 = Column(String) # 累计已领取手续费
    invested_t1 = Column(String)       # 累计投入
    returned_t1 = Column(String)       # 累计取回本金
    net_pnl_t1 = Column(String)        # 净收益 = 取回 + 当前价值 + 已领取手续费 - 投入


class MonitorTask(Base):
    """任务5：监控对象配置持久化。删除任务只停采集，历史数据独立删除。"""
    __tablename__ = "monitor_tasks"
    id = Column(Integer, primary_key=True)
    kind = Column(String)        # pool / wallet
    address = Column(String)
    status = Column(String, default="active")   # active / paused / deleted
    created_at = Column(DateTime)
    __table_args__ = (UniqueConstraint("kind", "address", name="uq_task"),)


class Subscription(Base):
    """任务4：chat 订阅了哪些主题。topic: liquidity / range / errors / all"""
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True)
    chat_id = Column(String)
    topic = Column(String)
    __table_args__ = (UniqueConstraint("chat_id", "topic", name="uq_sub"),)


class Notification(Base):
    """任务4：消息发件箱。事件检测只写这张表，发送 worker 单独消费——检测与发送分离。"""
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True)
    dedup_key = Column(String, unique=True)   # 同一事件只入队一次，不刷屏
    topic = Column(String)
    chat_id = Column(String)
    text = Column(String)
    status = Column(String, default="pending")  # pending / sent / failed
    attempts = Column(Integer, default=0)
    next_try = Column(DateTime)
    last_error = Column(String)
    created_at = Column(DateTime)
    sent_at = Column(DateTime)


engine = create_engine(config.DATABASE_URL, future=True,
                       connect_args={"check_same_thread": False, "timeout": 30} if config.DATABASE_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db():
    Base.metadata.create_all(engine)
