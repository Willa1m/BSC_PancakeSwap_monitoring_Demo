"""
任务 3：仓位资金流水与净收益核算。纯函数，不碰数据库，方便单元测试。

口径（简化版，需要跟面试官讲清楚的部分）：
- 投入   = 所有 IncreaseLiquidity 的 amount0/amount1（建仓和追加都算投入，追加不算收益）
- 减少流动性 (DecreaseLiquidity) 只是把代币记到 tokensOwed，还没到钱包，不算取回。
- 领取   (Collect) 才是真正拿到手的钱。Collect 里既包含减少流动性释放的本金，也包含手续费。
  拆分方法：手续费 = 累计 Collect - 累计 Decrease（每个 token 分别算，下限 0）；
            取回本金 = 累计 Collect - 手续费。
  这就是"区分减少流动性与实际领取资产，避免重复记账"的具体实现。
- 当前价值 = 仓位当前持有的两种代币 + 已结算未领取的 tokensOwed，按当前价格折成 token1。
- 净收益   = 取回本金 + 已领取手续费 + 当前价值 - 投入
  （对应任务表算例：300 + 950 - 1200 = 50，其中 950 已含未领取手续费）
- 明确忽略：Gas、兑换滑点、NFT 转入时的成本基础。所有金额用"当前价格"这一个时点折算，
  没有用事件发生时的历史价格——这是简化，不是漏掉。
"""
from decimal import Decimal


def split_flows(events):
    """
    events: 可迭代的 dict，字段 kind / amount0 / amount1（原始整数或字符串）。
    返回各累计量（原始最小单位，Decimal）。
    """
    inc0 = inc1 = dec0 = dec1 = col0 = col1 = Decimal(0)
    for e in events:
        a0, a1 = Decimal(int(e["amount0"] or 0)), Decimal(int(e["amount1"] or 0))
        if e["kind"] == "INCREASE":
            inc0 += a0; inc1 += a1
        elif e["kind"] == "DECREASE":
            dec0 += a0; dec1 += a1
        elif e["kind"] == "COLLECT":
            col0 += a0; col1 += a1
    fee0 = max(col0 - dec0, Decimal(0))
    fee1 = max(col1 - dec1, Decimal(0))
    return {
        "invested0": inc0, "invested1": inc1,
        "decreased0": dec0, "decreased1": dec1,
        "collected0": col0, "collected1": col1,
        "fees0": fee0, "fees1": fee1,
        "principal_returned0": col0 - fee0, "principal_returned1": col1 - fee1,
    }


def to_token1(amount0, amount1, price, dec0, dec1) -> Decimal:
    """把 (原始 amount0, 原始 amount1) 按价格折算成人类可读的 token1 数量"""
    a0 = Decimal(amount0) / Decimal(10) ** dec0
    a1 = Decimal(amount1) / Decimal(10) ** dec1
    return a0 * Decimal(price) + a1


def net_pnl(flows, current_amount0, current_amount1, owed0, owed1, price, dec0, dec1):
    """返回以 token1 计价的核算结果"""
    invested = to_token1(flows["invested0"], flows["invested1"], price, dec0, dec1)
    returned = to_token1(flows["principal_returned0"], flows["principal_returned1"], price, dec0, dec1)
    fees = to_token1(flows["fees0"], flows["fees1"], price, dec0, dec1)
    value = to_token1(Decimal(current_amount0) + Decimal(owed0),
                      Decimal(current_amount1) + Decimal(owed1), price, dec0, dec1)
    return {
        "invested_t1": invested,
        "returned_t1": returned,
        "fees_collected_t1": fees,
        "value_t1": value,
        "net_pnl_t1": returned + fees + value - invested,
    }
