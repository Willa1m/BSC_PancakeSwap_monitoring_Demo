"""
PancakeSwap/Uniswap V3 价格与仓位数学。全部用 Decimal，不用 float。

核心概念：
- 池子存的不是价格，而是 sqrtPriceX96 = sqrt(price) * 2^96（price = token1/token0 的原始单位比）。
- tick 是价格的对数刻度：price = 1.0001^tick。仓位区间 [tickLower, tickUpper]。
- 仓位里到底有多少 token0/token1 取决于当前价格相对区间的位置。
"""
from decimal import Decimal, getcontext

getcontext().prec = 60
Q96 = Decimal(2) ** 96


def sqrt_price_x96_to_price(sqrt_price_x96: int, dec0: int, dec1: int) -> Decimal:
    """返回 1 个 token0 值多少 token1（人类可读单位）"""
    s = Decimal(sqrt_price_x96) / Q96
    return s * s * Decimal(10) ** (dec0 - dec1)


def tick_to_sqrt_price_x96(tick: int) -> Decimal:
    """sqrt(1.0001^tick) * 2^96"""
    return (Decimal("1.0001") ** (Decimal(tick) / 2)) * Q96


def amounts_from_liquidity(liquidity: int, sqrt_p: Decimal, sqrt_a: Decimal, sqrt_b: Decimal):
    """
    给定流动性 L、当前 sqrt 价格 P、区间下界 A、上界 B（全部是 X96 形式），
    返回仓位当前持有的 (amount0, amount1)，原始最小单位。
    公式来自 Uniswap V3 白皮书 6.29/6.30。
    """
    L = Decimal(liquidity)
    p, a, b = sqrt_p / Q96, sqrt_a / Q96, sqrt_b / Q96
    if p <= a:                          # 价格低于区间：全是 token0
        return L * (b - a) / (a * b), Decimal(0)
    if p >= b:                          # 价格高于区间：全是 token1
        return Decimal(0), L * (b - a)
    return L * (b - p) / (p * b), L * (p - a)   # 区间内：两种都有


def human(raw, decimals: int) -> Decimal:
    return Decimal(raw) / Decimal(10) ** decimals


def range_status(tick: int, lower: int, upper: int) -> str:
    if tick < lower:
        return "below"      # 价格低于区间（仓位全是 token0）
    if tick >= upper:
        return "above"
    return "in_range"
