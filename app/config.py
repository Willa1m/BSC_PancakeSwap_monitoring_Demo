"""
配置：全部从环境变量 / .env 读取。凭据和地址不写死在代码里（listing 的部署运维要求）。
"""
import os
from dotenv import load_dotenv

load_dotenv()

# --- RPC ---
RPC_URL = os.getenv("RPC_URL", "https://bsc-dataseed.binance.org")
RPC_TIMEOUT = int(os.getenv("RPC_TIMEOUT", "15"))

# --- 数据库：默认 SQLite 零配置；用 Postgres 时改成
#     postgresql+psycopg2://monitor:monitor@localhost:5432/monitor
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./monitor.db")

# --- 初始监控对象（首次启动写入 monitor_tasks 表，之后通过管理接口增删）---
POOLS = [a.strip().lower() for a in os.getenv(
    "POOLS", "0xcc2bffaec373a6004bb6ccc8a62cdd66061f7c6a").split(",") if a.strip()]
WALLETS = [a.strip().lower() for a in os.getenv(
    "WALLETS", "0x42b5d65aa4dd8b4c896812a96c72f0cf89e5eae7").split(",") if a.strip()]

# --- PancakeSwap V3 固定合约（BSC 主网）---
NPM_ADDRESS = os.getenv("NPM_ADDRESS", "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364")
FACTORY_ADDRESS = os.getenv("FACTORY_ADDRESS", "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865")

# --- 采集参数 ---
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "20"))        # 池子轮询秒数
WALLET_INTERVAL = int(os.getenv("WALLET_INTERVAL", "120"))   # 钱包仓位同步秒数
CONFIRMATIONS = int(os.getenv("CONFIRMATIONS", "15"))        # 只采集这么多确认之后的区块，规避回退
LOG_CHUNK = int(os.getenv("LOG_CHUNK", "2000"))              # eth_getLogs 每次扫描的区块数（公共RPC限制）
SWAP_LOOKBACK_BLOCKS = int(os.getenv("SWAP_LOOKBACK_BLOCKS", "120000"))   # 首次启动从最新往回多少块开始扫 Swap
POSITION_LOOKBACK_BLOCKS = int(os.getenv("POSITION_LOOKBACK_BLOCKS", "400000"))  # 仓位事件回扫范围
STALE_AFTER = int(os.getenv("STALE_AFTER", "300"))           # 应高于最慢轮询周期，避免正常任务被误报过期

RPC_MIN_INTERVAL = float(os.getenv("RPC_MIN_INTERVAL", "0.05"))   # RPC 限流：两次请求最小间隔（秒）

# --- 任务4：Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")          # 空 = 不启用 Telegram
TELEGRAM_SEND_INTERVAL = float(os.getenv("TELEGRAM_SEND_INTERVAL", "1.1"))  # 限频：每条消息最小间隔
TELEGRAM_MAX_RETRY = int(os.getenv("TELEGRAM_MAX_RETRY", "5"))

# --- 任务5：管理接口 ---
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "change-me")

EXPLORER = "https://bscscan.com"
