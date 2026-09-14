from decimal import Decimal
from app import accounting

def test_net_pnl_example():
    """投入1000 + 追加200，取回300，剩余估值950 → 净收益 50。全部以 token1 计价（token0 为 0）"""
    events = [
        {"kind": "INCREASE", "amount0": 0, "amount1": 1000},
        {"kind": "INCREASE", "amount0": 0, "amount1": 200},
        {"kind": "DECREASE", "amount0": 0, "amount1": 300},
        {"kind": "COLLECT",  "amount0": 0, "amount1": 300},
    ]
    flows = accounting.split_flows(events)
    assert flows["fees1"] == 0            # Collect 没超过 Decrease，说明没领手续费
    r = accounting.net_pnl(flows, 0, 950, 0, 0, price=Decimal(1), dec0=0, dec1=0)
    assert r["net_pnl_t1"] == Decimal(50)

def test_collect_splits_fees_from_principal():
    """减少流动性 300 后领取 320：其中 300 是本金，20 是手续费，不能都算收益"""
    events = [
        {"kind": "INCREASE", "amount0": 0, "amount1": 1000},
        {"kind": "DECREASE", "amount0": 0, "amount1": 300},
        {"kind": "COLLECT",  "amount0": 0, "amount1": 320},
    ]
    f = accounting.split_flows(events)
    assert f["fees1"] == 20 and f["principal_returned1"] == 300
