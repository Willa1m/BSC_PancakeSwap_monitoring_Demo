"""
统一 RPC 模块：所有链上读取都经过这里。
- 负责 web3 连接、POA 中间件（BSC 需要）、ABI、事件 topic、分段 getLogs。
- 其他模块不直接碰 web3，方便以后换 RPC / 加缓存 / 加重试。
"""
import time
from web3 import Web3
from . import config

# BSC 是 POA 链，区块 extraData 超长，web3.py 需要这个中间件才能解析区块
w3 = Web3(Web3.HTTPProvider(config.RPC_URL, request_kwargs={"timeout": config.RPC_TIMEOUT}))
try:                                   # web3.py v7
    from web3.middleware import ExtraDataToPOAMiddleware
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
except ImportError:                    # web3.py v6
    from web3.middleware import geth_poa_middleware
    w3.middleware_onion.inject(geth_poa_middleware, layer=0)

# ---------- 最小化 ABI（只放用到的函数/事件；请在 BscScan 的 Contract 页对照核实） ----------
ERC20_ABI = [
    {"name": "decimals", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint8"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "string"}]},
]

POOL_ABI = [
    {"name": "token0", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"name": "token1", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"name": "fee", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint24"}]},
    {"name": "liquidity", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint128"}]},
    {"name": "slot0", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [
        {"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"},
        {"name": "observationIndex", "type": "uint16"}, {"name": "observationCardinality", "type": "uint16"},
        {"name": "observationCardinalityNext", "type": "uint16"}, {"name": "feeProtocol", "type": "uint32"},
        {"name": "unlocked", "type": "bool"}]},
    # PancakeSwap V3 的 Swap 事件比 Uniswap 多两个 protocolFees 字段
    {"name": "Swap", "type": "event", "anonymous": False, "inputs": [
        {"indexed": True, "name": "sender", "type": "address"},
        {"indexed": True, "name": "recipient", "type": "address"},
        {"indexed": False, "name": "amount0", "type": "int256"},
        {"indexed": False, "name": "amount1", "type": "int256"},
        {"indexed": False, "name": "sqrtPriceX96", "type": "uint160"},
        {"indexed": False, "name": "liquidity", "type": "uint128"},
        {"indexed": False, "name": "tick", "type": "int24"},
        {"indexed": False, "name": "protocolFeesToken0", "type": "uint128"},
        {"indexed": False, "name": "protocolFeesToken1", "type": "uint128"}]},
]

NPM_ABI = [  # NonfungiblePositionManager：每个 LP 仓位是一个 ERC-721 NFT
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "tokenOfOwnerByIndex", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "uint256"}]},
    {"name": "ownerOf", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "uint256"}], "outputs": [{"type": "address"}]},
    {"name": "positions", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "uint256"}], "outputs": [
        {"name": "nonce", "type": "uint96"}, {"name": "operator", "type": "address"},
        {"name": "token0", "type": "address"}, {"name": "token1", "type": "address"},
        {"name": "fee", "type": "uint24"}, {"name": "tickLower", "type": "int24"},
        {"name": "tickUpper", "type": "int24"}, {"name": "liquidity", "type": "uint128"},
        {"name": "feeGrowthInside0LastX128", "type": "uint256"},
        {"name": "feeGrowthInside1LastX128", "type": "uint256"},
        {"name": "tokensOwed0", "type": "uint128"}, {"name": "tokensOwed1", "type": "uint128"}]},
    {"name": "IncreaseLiquidity", "type": "event", "anonymous": False, "inputs": [
        {"indexed": True, "name": "tokenId", "type": "uint256"},
        {"indexed": False, "name": "liquidity", "type": "uint128"},
        {"indexed": False, "name": "amount0", "type": "uint256"},
        {"indexed": False, "name": "amount1", "type": "uint256"}]},
    {"name": "DecreaseLiquidity", "type": "event", "anonymous": False, "inputs": [
        {"indexed": True, "name": "tokenId", "type": "uint256"},
        {"indexed": False, "name": "liquidity", "type": "uint128"},
        {"indexed": False, "name": "amount0", "type": "uint256"},
        {"indexed": False, "name": "amount1", "type": "uint256"}]},
    {"name": "Collect", "type": "event", "anonymous": False, "inputs": [
        {"indexed": True, "name": "tokenId", "type": "uint256"},
        {"indexed": False, "name": "recipient", "type": "address"},
        {"indexed": False, "name": "amount0", "type": "uint256"},
        {"indexed": False, "name": "amount1", "type": "uint256"}]},
    {"name": "Transfer", "type": "event", "anonymous": False, "inputs": [
        {"indexed": True, "name": "from", "type": "address"},
        {"indexed": True, "name": "to", "type": "address"},
        {"indexed": True, "name": "tokenId", "type": "uint256"}]},
]

FACTORY_ABI = [
    {"name": "getPool", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}, {"type": "address"}, {"type": "uint24"}], "outputs": [{"type": "address"}]},
]

# 事件签名 -> topic0（日志的第一个 topic 就是事件签名的 keccak）
TOPIC = {
    "Swap": Web3.keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)").hex(),
    "IncreaseLiquidity": Web3.keccak(text="IncreaseLiquidity(uint256,uint128,uint256,uint256)").hex(),
    "DecreaseLiquidity": Web3.keccak(text="DecreaseLiquidity(uint256,uint128,uint256,uint256)").hex(),
    "Collect": Web3.keccak(text="Collect(uint256,address,uint256,uint256)").hex(),
    "Transfer": Web3.keccak(text="Transfer(address,address,uint256)").hex(),
}


def _hex(h):
    h = h if isinstance(h, str) else h.hex()
    return h if h.startswith("0x") else "0x" + h


TOPIC = {k: _hex(v) for k, v in TOPIC.items()}


def cs(addr: str) -> str:
    return Web3.to_checksum_address(addr)


def pool_contract(addr):    return w3.eth.contract(address=cs(addr), abi=POOL_ABI)
def erc20_contract(addr):   return w3.eth.contract(address=cs(addr), abi=ERC20_ABI)
def npm_contract():         return w3.eth.contract(address=cs(config.NPM_ADDRESS), abi=NPM_ABI)
def factory_contract():     return w3.eth.contract(address=cs(config.FACTORY_ADDRESS), abi=FACTORY_ABI)


def token_id_topic(token_id: int) -> str:
    """indexed uint256 在 topic 里是 32 字节左补零的十六进制"""
    return "0x" + token_id.to_bytes(32, "big").hex()


def latest_block() -> int:
    return w3.eth.block_number


_block_ts_cache = {}


def block_timestamp(block: int):
    """区块时间戳，带简单缓存（同一区块多条日志只查一次 RPC）"""
    from datetime import datetime, timezone
    if block not in _block_ts_cache:
        if len(_block_ts_cache) > 5000:
            _block_ts_cache.clear()
        _block_ts_cache[block] = datetime.fromtimestamp(w3.eth.get_block(block)["timestamp"], tz=timezone.utc).replace(tzinfo=None)
    return _block_ts_cache[block]


def log_timestamp(log):
    """优先使用 BSC getLogs 返回的时间戳，缺失时再查询区块。"""
    from datetime import datetime, timezone
    raw = log.get("blockTimestamp")
    if raw is not None:
        seconds = int(raw, 16) if isinstance(raw, str) else int(raw)
        return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)
    return block_timestamp(log["blockNumber"])


def get_logs_chunked(address, topics, from_block, to_block, chunk=None, retries=3):
    """
    分段扫描日志：公共 RPC 通常限制单次范围/返回条数，所以按 chunk 切块，
    每块失败重试，全部失败则抛出（由调用方决定保留断点还是标记异常）。
    生成器逐块产出 (chunk_end, logs)，调用方每处理完一块就可以推进断点。
    """
    chunk = chunk or config.LOG_CHUNK
    start = from_block
    while start <= to_block:
        end = min(start + chunk - 1, to_block)
        for attempt in range(retries):
            try:
                logs = w3.eth.get_logs({"address": cs(address), "topics": topics,
                                        "fromBlock": start, "toBlock": end})
                break
            except Exception as e:      # 超时 / 限流：退避重试
                if attempt == retries - 1:
                    raise
                time.sleep(2 ** attempt)
        yield end, logs
        start = end + 1


# ---------- RPC 限流：所有请求经过同一个节流器，多个任务并发时不会把公共节点打爆 ----------
import threading as _th
_rpc_lock = _th.Lock()
_rpc_last = [0.0]
_orig_make_request = w3.provider.make_request


def _throttled(method, params):
    with _rpc_lock:
        wait = config.RPC_MIN_INTERVAL - (time.time() - _rpc_last[0])
        if wait > 0:
            time.sleep(wait)
        try:
            return _orig_make_request(method, params)
        finally:
            _rpc_last[0] = time.time()


w3.provider.make_request = _throttled
