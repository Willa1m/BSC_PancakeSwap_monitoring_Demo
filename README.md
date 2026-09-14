# BSC / PancakeSwap V3 LP 监测 Demo

监控 PancakeSwap V3 池子、钱包仓位、收益曲线、Telegram 通知及采集任务。

## 一、运行（5 分钟）

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                 # 需要的话改 RPC_URL
uvicorn app.main:app --port 8000                     # 不要加 --reload，会起两份采集线程
```
打开 http://localhost:8000 。首次启动要等 1–3 分钟：Swap 回扫默认 12 万区块，仓位事件回扫 40 万区块，公共 RPC 会比较慢。看到"过期/未更新"先别慌，看终端日志里的错误。

使用 PostgreSQL：`docker compose up -d`，然后在 `.env` 中将 `DATABASE_URL` 改成 Postgres 配置。代码不用修改。

**如果之前跑过旧版本**：删掉 `monitor.db`（表结构变了，create_all 不会加列）。

单元测试：`python -m pytest`

### Telegram
1. 找 @BotFather 建 bot，把 token 填进 `.env` 的 `TELEGRAM_BOT_TOKEN`，重启。
2. 把 bot 拉进测试群（或私聊），发 `/sub all`。
3. 命令：`/pool` `/wallet` `/sub liquidity|range|errors|all` `/unsub <topic>` `/subs` `/replay <NFT ID>`（历史回放，消息带"【历史回放】"前缀）。
4. 发件箱状态：`GET /api/admin/notifications?status=failed`（带 X-API-Key）。

### 管理接口（请求头 `X-API-Key: <ADMIN_API_KEY>`）
| 操作 | 接口 |
|---|---|
| 列表（分页、按状态过滤）| `GET /api/admin/tasks?page=1&size=20&status=active` |
| 新增 | `POST /api/admin/tasks` body `{"kind":"pool","address":"0x..."}` |
| 暂停 / 恢复 / 删除 | `POST .../{id}/pause` `POST .../{id}/resume` `DELETE .../{id}` |
| 删除历史数据（独立操作，需先暂停/删除）| `DELETE .../{id}/data` |
| 历史补采 | `POST .../{id}/backfill?from_block=A&to_block=B` |
| 完整性校验（链上 vs 库内逐条比对）| `GET .../{id}/verify?from_block=A&to_block=B` |
| 健康检查 | `GET /api/health` |
| 接口文档（FastAPI 自动生成）| `GET /docs` |

网页底部的"监控任务管理"卡片是这些接口的按钮版。

## 二、代码结构

| 文件 | 负责什么 |
|---|---|
| `app/config.py` | 全部参数从 .env 读；池子/钱包用逗号分隔，新增不改代码 |
| `app/chain.py` | 统一 RPC 模块：web3 连接、POA 中间件、ABI、事件 topic、分段 getLogs + 退避重试 |
| `app/v3math.py` | sqrtPriceX96 → 价格、tick → sqrtPrice、流动性 → 两种代币数量；全部 Decimal |
| `app/models.py` | 10 张表；金额存字符串；(tx_hash, log_index) 唯一约束去重；SyncState 记断点 |
| `app/collector.py` | 后台线程：池子轮询、Swap 增量扫描、仓位同步、事件时间线、收益快照 |
| `app/accounting.py` | 纯函数：资金流水拆分、净收益核算 |
| `app/api.py` | 查询接口，只读库不碰 RPC；每个接口返回 stale/coverage |
| `app/static/index.html` | 卡片 + 价格曲线 + Swap 列表 + 仓位表 + 收益曲线/时间线 |
| `app/notifier.py` | 发件箱入队、发送 worker、bot 命令和历史回放 |
| `app/admin.py` | 任务增删暂停恢复、补采、校验、删数据、API Key 鉴权和分页 |
| `tests/` | V3 数学、收益核算、通知去重与重试的单元测试 |

## 三、关键设计

1. **为什么金额存字符串、算用 Decimal**：uint256 超过 float 的 53 位有效数字，float 会静默丢精度；SQLite 的 Numeric 底层也是 float。
2. **去重怎么做**：数据库唯一约束 `(tx_hash, log_index)`。链上一条日志的坐标就是这两个值，重复扫描/重启后再扫，插入会撞约束被跳过，而不是靠内存里记"扫过没"。
3. **断点续采**：`sync_state` 表每个采集对象一行，每一页相关日志都成功后才推进 `last_block`。进程中断后从 `last_block+1` 继续。
4. **区块回退**：只处理 `latest - CONFIRMATIONS` 之前的区块（默认 15 个确认）。简化版，没有做 parentHash 比对回滚。
5. **RPC 超时**：`mark_error` 只标记 stale + 记错误，不动已有数据；网页显示"过期"而不是 0。
6. **减少流动性 ≠ 领取**：DecreaseLiquidity 只把代币记到 tokensOwed，Collect 才真正到钱包。手续费 = 累计 Collect − 累计 Decrease，这样本金不会被算成收益（见 `accounting.py` 和 `tests/test_accounting.py`）。
7. **检测与发送分离**：采集线程只往 `notifications` 表写一行就走，Telegram 挂了、限流了、token 错了，都只影响 sender 线程。`dedup_key` 唯一约束保证同一事件同一 chat 只入队一次。
8. **状态变化触发，不是采样触发**：区间越界只在 `range_status` 从 A 变 B 时发一条；采集异常只在"正常→异常"那一刻发一条，之后每轮采样失败不再重复发。
9. **配置表驱动**：采集循环每轮重新读 `monitor_tasks`，暂停/删除立刻生效；暂停不清 `sync_state`，恢复后从断点接着采，所以暂停期间是"有缺口"而非"补齐"。
10. **补采与实时采集不并发**：同一对象一把 `threading.Lock`，补采期间实时采集那一轮直接跳过，避免两边同时推进断点。
11. **RPC 限流**：所有请求走 `chain.py` 的节流器，多任务并发也不会把公共节点打爆。
12. **覆盖范围如实展示**：24h 成交量只统计已采集范围，`covered_from` 告诉你实际起点；事件时间线标注覆盖区块区间。

## 四、验证清单

- 抽查 3 笔 Swap：网页表里点交易链接，到 BscScan 的 Logs 页找 Swap 日志，对比 amount0/amount1、log index。
- 抽查 1 个价格点：`/api/pools/<addr>/prices` 取一个 block，BscScan 上"Read Contract → slot0"选该区块（需要归档节点；不行就用最新块对比网页最新值）。
- 模拟 RPC 超时：.env 把 `RPC_URL` 改成一个不存在的地址后重启，或运行中断网；网页应显示"过期"并保留旧值；若订阅了 errors 主题，Telegram 收到一条（只一条）异常播报。
- 模拟 Telegram 失败：把 token 改错后重启，`/api/admin/notifications` 看到 attempts 递增、最后 failed；同时池子/仓位采集照常推进。
- 模拟进程中断：Ctrl+C 再启动，`sync_state.last_block` 继续推进，`swaps` 不重复（唯一约束）。
- 补采后校验：`backfill` 一段范围，再 `verify` 同一范围，`missing` 和 `extra` 都应为空、`duplicates_in_db` 为 0。
- 暂停/恢复：暂停 5 分钟再恢复，价格曲线应有一段空白（不是补的），Swap 会从断点继续扫（这段不会缺，因为是按区块扫的）。
- 一个任务异常不阻塞其他：新增一个错误地址的 pool 任务，它会一直报错，但原池子继续更新。
- 重启不重复：停掉再启动，`select count(*) from swaps` 不变；`sync_state` 的 last_block 继续推进。
- 仓位为空：若示例钱包没有直接持有的 V3 NFT，网页会明确提示，而不是空白。

## 五、已知简化

- 估值用当前价格一个时点折算所有历史流水，没有用事件发生时的历史价格。
- 不计 Gas、兑换成本、NFT 转入时的成本基础。
- 质押在 MasterChefV3 的仓位 NFT 所有者是 MasterChef 合约，`balanceOf(wallet)` 读不到。
- `tokensOwed` 只在仓位被操作时结算，"未领取手续费"可能低估。
- 回退处理只靠确认数，没做 reorg 检测回滚。
- 没有数据库迁移工具（改表要删库重建）；线程而非 asyncio；鉴权是单一静态 API Key。
- Telegram 收益摘要只有 /wallet 按需查询，没做定时推送。
- 补采/校验只做了 Swap，仓位事件的补采靠改 `POSITION_LOOKBACK_BLOCKS` 重扫。
- 同步 web3 + 线程，够用但不是高并发方案。

## 六、合约配置核验

- `chain.py` 里的 ABI 是按 PancakeSwap V3 手写的最小版本，Swap 事件带 protocolFees 两个字段。请到 BscScan 打开池子合约的 Contract → Events 核对一下签名；如果 topic 不一致会一条 Swap 都扫不到。
- NPM / Factory 地址是 BSC 主网的 PancakeSwap V3 合约，请在 PancakeSwap 文档核对。
