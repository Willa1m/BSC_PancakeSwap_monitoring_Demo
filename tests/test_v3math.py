from decimal import Decimal
from app import v3math

def test_tick_zero_is_price_one():
    assert abs(v3math.tick_to_sqrt_price_x96(0) - v3math.Q96) < 1

def test_price_conversion_with_decimals():
    # sqrtPrice = 2^96 → 原始单位比 1:1；token0 18 位、token1 6 位时，1 个 token0(1e18 raw) 换 1e18 raw token1 = 1e12 token1
    p = v3math.sqrt_price_x96_to_price(int(v3math.Q96), 18, 6)
    assert p == Decimal("1e12")

def test_amounts_below_range_all_token0():
    a0, a1 = v3math.amounts_from_liquidity(10**18, v3math.tick_to_sqrt_price_x96(-100),
                                           v3math.tick_to_sqrt_price_x96(0), v3math.tick_to_sqrt_price_x96(100))
    assert a1 == 0 and a0 > 0

def test_amounts_in_range_both():
    a0, a1 = v3math.amounts_from_liquidity(10**18, v3math.tick_to_sqrt_price_x96(50),
                                           v3math.tick_to_sqrt_price_x96(0), v3math.tick_to_sqrt_price_x96(100))
    assert a0 > 0 and a1 > 0
