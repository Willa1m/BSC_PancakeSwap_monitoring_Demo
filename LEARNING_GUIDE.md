# 技术能力学习与源码阅读指南

这份指南对应 `listing.pdf` 中的技术能力要求。目标不是记住文件名，而是能够回答：

1. 数据从 BSC 到网页经过了哪些步骤？
2. 为什么这样设计？
3. 发生 RPC 限流、进程重启或重复扫描时，系统如何保证数据可靠？
4. 当前实现有哪些边界，生产环境还需要补什么？

## 一、先建立项目全貌

### 推荐阅读顺序

1. `README.md`：先理解需求、验收标准和已知简化。
2. `app/main.py`：了解服务如何装配和启动。
3. `app/config.py`：了解环境变量和运行参数。
4. `app/chain.py`：理解 BSC RPC、ABI 和事件日志。
5. `app/models.py`：理解数据最终如何保存。
6. `app/collector.py`：串起链上采集、断点和后台任务。
7. `app/v3math.py`、`app/accounting.py`：理解价格、仓位和收益。
8. `app/api.py`、`app/admin.py`：理解查询及任务管理接口。
9. `app/notifier.py`：理解 Telegram 发件箱。
10. `app/static/index.html`：理解网页如何消费 API。
11. `tests/`：用测试确认自己是否真正理解。

### 数据流

```text
BSC RPC
  │
  ▼
app/chain.py（合约调用、日志、节流、重试）
  │
  ▼
app/collector.py（轮询、增量扫描、断点、快照）
  │
  ▼
app/models.py（SQLite / PostgreSQL）
  ├── app/api.py ──► 网页
  ├── app/admin.py ──► 任务管理
  └── notifications 表 ──► app/notifier.py ──► Telegram
```

## 二、按 listing 技能逐项学习

## 1. 后端开发：模块、异常和配置

### 查看文件

- `app/main.py`
- `app/config.py`
- `app/collector.py`
- `app/notifier.py`

### 重点代码

- `main._startup()`：初始化数据库并启动采集和通知线程。
- `collector.start()`：启动 pool 与 wallet 两类后台循环。
- `run_pool_task()`、`run_wallet_task()`：一个任务一个异常边界。
- `config.py`：从 `.env` 读取 RPC、地址、轮询周期和确认数。

### 实现思路

系统按配置、链、采集、计算、存储、API 和通知拆分。单个池子或钱包任务使用独立 `try/except`，一个地址失败不会终止整个循环。敏感配置从 `.env` 读取，仓库只提交 `.env.example`。

### 动手验证

1. 启动服务后访问 `/api/health`。
2. 新增一个错误的 pool 地址。
3. 确认错误任务被标记异常，而原池仍继续更新。

### 面试时要能回答

> 为什么不能把 RPC、计算、数据库和 API 全写在一个文件里？

因为各层的失败模式不同。RPC 超时不应阻塞网页查询，Telegram 失败也不应回滚链上数据。模块拆分使故障可以隔离，也让纯计算逻辑能够独立测试。

## 2. API 开发：查询、分页、错误和鉴权

### 查看文件

- `app/api.py`
- `app/admin.py`
- `app/main.py`

### 重点代码

- `health()`、`pool_summary()`、`pool_prices()`、`pool_swaps()`
- `wallet_positions()`、`position_timeline()`、`position_curve()`
- `admin.auth()`、`list_tasks()`、`add_task()`、`pause()`、`resume()`
- `backfill()`、`verify()`

### 实现思路

公开接口只查询数据库，不直接请求 RPC。管理接口要求请求头 `X-API-Key`。任务列表支持分页和状态筛选。响应会附带 `stale`、`from_block` 和 `last_block`，避免把“没有数据”“数值为零”和“旧数据”混在一起。

### 动手验证

```powershell
Invoke-RestMethod http://localhost:8000/api/health
Invoke-RestMethod http://localhost:8000/docs
```

分别使用正确和错误的 `X-API-Key` 请求 `/api/admin/tasks`，确认错误密钥返回 401。

### 当前边界

- 使用单一静态 API Key，不是完整用户权限系统。
- 没有 API 版本前缀、限流和 WebSocket。
- Swap 查询使用 `limit`，不是完整的游标分页。

## 3. 数据库与 SQL：模型、索引、事务和幂等

### 查看文件

- `app/models.py`
- `app/collector.py`
- `app/notifier.py`
- `docker-compose.yml`

### 重点模型

- `Pool`、`PoolSnapshot`、`Swap`
- `Position`、`PositionEvent`、`PositionSnapshot`
- `SyncState`、`MonitorTask`
- `Subscription`、`Notification`

### 实现思路

- 链上 `uint256` 可能超过浮点数精度，因此原始金额使用字符串保存。
- `Swap` 和 `PositionEvent` 使用 `(tx_hash, log_index)` 唯一约束。
- `SyncState.last_block` 保存每个采集对象的同步断点。
- 每页日志成功写入后才推进断点。
- SQLite 用于本地演示；修改 `DATABASE_URL` 后可切换 PostgreSQL。

### 动手验证

1. 查询 `sync_state`，记录一个任务的 `last_block`。
2. 重启服务并再次查询，确认从原断点继续。
3. 比较重启前后的重复记录数，应为 0。
4. 阅读捕获 `IntegrityError` 的位置，解释为什么数据库约束比内存集合可靠。

### 当前边界

- 没有 Alembic 数据库迁移。
- 没有完整 ORM relationship。
- PostgreSQL 配置存在，但本轮没有做生产部署验证。

## 4. BSC 与 EVM：区块、ABI、日志和 Token

### 查看文件

- `app/chain.py`
- `app/config.py`

### 重点代码

- `ERC20_ABI`、`POOL_ABI`、`NPM_ABI`、`FACTORY_ABI`
- `TOPIC`
- `pool_contract()`、`npm_contract()`、`factory_contract()`
- `get_logs_chunked()`、`token_id_topic()`

### 实现思路

`chain.py` 使用 Web3.py 连接 BSC JSON-RPC，并注入 PoA 中间件。系统只保留实际需要的最小 ABI，通过事件签名的 Keccak 哈希生成 topic，再使用 `eth_getLogs` 按地址、topic 和区块范围检索日志。

PancakeSwap V3 的 Swap 事件比标准 Uniswap V3 多两个 protocol fee 字段，所以不能直接照搬标准 Uniswap ABI。

### 动手验证

1. 在 BscScan 打开池合约 `0xcc2bffaec373a6004bb6ccc8a62cdd66061f7c6a`。
2. 找一笔 Swap，比较 topic、`amount0`、`amount1` 和 `logIndex`。
3. 查看 NPM 的 Transfer、IncreaseLiquidity、DecreaseLiquidity 和 Collect 日志。
4. 核对 `NPM_ADDRESS` 与 `FACTORY_ADDRESS`。

### 面试时要能回答

> 为什么一笔交易可以有多条事件，为什么不能只用 tx hash 去重？

一笔交易可能调用多个合约并产生多条日志；`logIndex` 表示日志在交易回执中的位置。因此链上事件的稳定坐标是 `(tx_hash, log_index)`。

## 5. 链上数据采集：历史补采和持续更新

### 查看文件

- `app/collector.py`
- `app/chain.py`

### 重点代码

- `poll_pool()`：读取池状态并生成快照。
- `scan_swaps()`：增量扫描 Swap。
- `sync_positions()`：读取钱包拥有的 Position NFT。
- `scan_position_events()`：扫描仓位事件时间线。
- `get_state()`、`mark_ok()`、`mark_error()`：同步状态。
- `backfill_swaps()`、`verify_swaps()`：补采与完整性核验。

### 实现思路

实时采集并不是只盯最新块，而是记录上次成功区块，下轮从 `last_block + 1` 扫到 `latest - CONFIRMATIONS`。历史区间通过 `LOG_CHUNK` 分页，避免一次 `eth_getLogs` 范围过大。

仓位事件有两种 topic 位置：

- Increase、Decrease、Collect 的 token ID 在 `topic[1]`。
- ERC-721 Transfer 的 token ID 在 `topic[3]`。

因此代码分两组查询，但必须在同一页两组都成功后才能推进断点。

### 动手验证

1. 记录 `last_block` 后强制停止进程。
2. 重启并确认从下一块继续。
3. 对一个小区块范围执行 backfill 和 verify。
4. 检查 `missing`、`extra` 与 `duplicates_in_db`。

### 真实调试案例：断点粒度

原实现只在整个大范围完成后保存一次断点。公共 RPC 在中途返回 429 时，即使已经扫描绝大部分区块，重启仍会从头开始。

修复后按页处理：同一页的两组事件都成功写入后，才将 `last_block` 更新为该页末尾。这使断点真正表示“到该区块为止数据完整”。

## 6. PancakeSwap V3：Pool、tick 与 Position NFT

### 查看文件

- `app/chain.py`
- `app/v3math.py`
- `app/collector.py`

### 重点代码

- Pool：`slot0()`、`liquidity()`、Swap 事件。
- NPM：`balanceOf()`、`tokenOfOwnerByIndex()`、`positions()`。
- Factory：`getPool()`。
- 数学：`sqrt_price_x96_to_price()`、`tick_to_sqrt_price_x96()`、`amounts_from_liquidity()`、`range_status()`。

### 实现思路

V3 仓位不是同质化 LP Token，而是 ERC-721 NFT。每个 token ID 保存 token0、token1、费率、tick 下限、tick 上限、流动性和待领取金额。当前 tick 与价格区间决定仓位主要由哪种代币构成。

### 动手验证

选择 NFT `7390304`：

1. 从 NPM `positions(7390304)` 读取 tickLower、tickUpper 和 liquidity。
2. 对比网页仓位表。
3. 在 BscScan 查看它的 Transfer、Increase、Decrease 和 Collect。
4. 用 `range_status()` 判断当前价格位于区间内还是区间外。

### 当前边界

- `balanceOf(wallet)` 读取不到质押在 MasterChefV3 中的 NFT。
- 没有实现完整的 fee growth 计算。
- `tokensOwed` 只在仓位发生操作时结算，可能低估实时未领取手续费。

## 7. 金额与收益：精度、估值和资金流水

### 查看文件

- `app/v3math.py`
- `app/accounting.py`
- `app/collector.py` 中的 `snapshot_position()`
- `tests/test_v3math.py`
- `tests/test_accounting.py`

### 核心公式

```text
净收益 = 已取回本金 + 已领取手续费 + 当前仓位价值 - 累计投入
```

### 实现思路

- 不使用二进制浮点数处理链上金额，而使用 `int` 和 `Decimal`。
- IncreaseLiquidity 表示资金投入。
- DecreaseLiquidity 只是把资产从流动性变为 `tokensOwed`，不代表已经进入钱包。
- Collect 才表示资产真正领取。
- 为避免本金被重复当作手续费，手续费使用 Collect 与 Decrease 的差额识别。

### 动手验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_accounting.py -v
.\.venv\Scripts\python.exe -m pytest tests/test_v3math.py -v
```

逐行手算测试中的投入、取回、手续费、当前价值和净收益，不要只看测试通过。

### 当前边界

- 历史资金流水统一使用当前价格折算，而不是事件发生时的历史价格。
- 未扣除 Gas、滑点和兑换成本。
- 未处理 NFT 转入时的成本基础。

## 8. 后台任务与可靠性：并发、重试和故障隔离

### 查看文件

- `app/chain.py` 中的 `_throttled()` 与 `get_logs_chunked()`
- `app/collector.py` 中的循环、锁和状态函数
- `app/notifier.py` 中的发送 worker

### 实现思路

- RPC 请求指数退避重试。
- 所有 RPC 调用经过统一节流器。
- 同一任务使用 `threading.Lock`，避免补采与实时采集同时推进断点。
- 每个任务独立捕获异常。
- RPC 失败只更新错误和 stale 状态，不把旧数据写成 0。
- Telegram 检测与发送分离。

### 真实调试案例：请求间隔不等于并发控制

原节流器只在计算等待时间时持锁，真正的网络请求在锁外执行。多个线程虽然错开了请求开始时间，但仍可能存在多个同时进行的请求。

修复后锁覆盖实际 RPC 请求，并在请求结束后记录时间。这样下一次请求需要等上一次完成并满足最小间隔，公共节点的并发压力明显降低。

### 当前边界

- 没有多 RPC 自动切换。
- 同步 Web3 + 线程不适合高并发规模。
- daemon 线程没有完整的优雅停止流程。

## 9. 缓存与查询优化

### 查看文件

- `app/api.py`
- `app/models.py`
- `app/chain.py` 中的时间戳缓存
- `app/static/index.html`

### 实现思路

最大的缓存策略是“采集写库、查询读库”：网页请求不直接访问 RPC，即使 RPC 暂时失败，API 仍能返回最后成功数据并标记 stale。时间曲线按时间范围和索引查询，区块时间戳在进程内缓存。

`log_timestamp()` 优先使用日志自带的 `blockTimestamp`，缺失时才额外查询区块，减少大量重复 RPC。

### 动手验证

1. 服务已采到数据后断开 RPC。
2. 确认页面仍显示旧价格，而不是 0。
3. 确认健康接口显示 stale 和错误信息。

### 当前边界

- 无 Redis。
- 无 HTTP Cache-Control。
- 长时间曲线没有降采样或预聚合。

## 10. Telegram Bot：订阅、队列、去重和限频

### 查看文件

- `app/notifier.py`
- `app/models.py` 中的 `Subscription`、`Notification`
- `app/collector.py` 中的 `notifier.enqueue()` 调用
- `tests/test_notifier.py`

### 重点代码

- `enqueue()`：按订阅关系创建发件箱记录。
- `process_outbox()`：发送、重试并更新状态。
- `sender_loop()`：独立发送线程。
- `bot_loop()`、`handle_command()`：命令长轮询。

### 实现思路

采集线程只向 `notifications` 表写入消息，不直接调用 Telegram。发送失败只影响 sender，不影响链上采集。`dedup_key` 防止同一事件重复入队，发送失败采用退避并最终标记 failed。

### 动手验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_notifier.py -v
```

配置测试 Bot 后依次验证 `/sub all`、`/subs`、`/pool`、`/wallet` 和 `/replay 7390304`。

### 当前状态

本轮没有配置 `TELEGRAM_BOT_TOKEN`，因此只验证了通知逻辑单元测试，没有验证真实消息发送。

## 11. 基础网页：接口、表格、图表和状态

### 查看文件

- `app/static/index.html`
- `app/api.py`
- `app/main.py`

### 重点代码

- `loadPool()`、`loadWallet()`、`loadDetail()`、`loadTasks()`
- `stateTag()`：显示正常或过期。
- Chart.js 价格曲线和收益曲线。

### 实现思路

这是一个无构建步骤的单页界面，通过 `fetch` 调用 FastAPI，展示价格、流动性、Swap、仓位、事件和收益曲线。前端每 20 秒轮询 API，但不会直接调用 BSC RPC。

### 当前边界

- 只展示配置列表中的第一个池和钱包。
- Chart.js 依赖 CDN。
- 加载和错误状态比较简单。
- `#admin` 卡片目前位于 HTML 的 `<head>` 中，浏览器虽然会容错，但结构不规范。

## 12. 测试与数据核验

### 查看文件

- `tests/test_v3math.py`
- `tests/test_accounting.py`
- `tests/test_notifier.py`
- `README.md` 的“验收自查”
- `collector.verify_swaps()`

### 当前测试范围

- V3 数学：4 个测试。
- 收益核算：2 个测试。
- 通知去重与重试：3 个测试。

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```

当前结果为 9 个测试全部通过。还需要理解真实链上核验与单元测试的区别：单元测试证明给定输入下逻辑正确，BscScan 抽查证明 ABI、地址、topic 和真实数据解析正确。

### 当前边界

- 缺少 chain、collector、API 和 admin 的自动化集成测试。
- 缺少模拟 reorg、RPC 超时和 429 的固定测试。
- 没有 CI 与覆盖率报告。

## 13. Git、依赖与部署运维

### 查看文件

- `requirements.txt`
- `.env.example`
- `docker-compose.yml`
- `README.md`
- `.gitignore`

### 应掌握内容

- 使用虚拟环境隔离 Python 依赖。
- 使用 `.env` 管理环境配置。
- `.env`、数据库和虚拟环境不能提交到 GitHub。
- 能从日志区分应用错误、RPC 限流和配置问题。
- `/api/health` 用于检查采集断点和 stale 状态。

### Windows PowerShell 7 启动

```powershell
cd "D:\Learn\Computing skill\BSC_t1\bsc-lp-monitor"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

不要添加 `--reload`，否则开发重载进程可能重复启动后台采集线程。

### 当前边界

- `docker-compose.yml` 只提供 PostgreSQL，不包含应用 Dockerfile。
- 没有 CI/CD、Linux service 或生产反向代理配置。
- 本轮主要验证 SQLite 路径，PostgreSQL 应作为下一阶段单独验收。

## 三、真实链上证据

以下是 2026-09-14 调试时保存的样本。数据库数据会继续增长，所以数量不是固定业务指标。

### Swap

- 区块 `121762676`，交易 `0x03c1f347d19714f0a932311dfca597589e6d7a215eb7298794f6408dc3eb058a`
- 区块 `121762459`，交易 `0xa999413674911d439655b50626031edd962f1cce19d7a34095247826f3f56497`
- 区块 `121762348`，交易 `0xf36ed300032f7fd6cd69d40782083d2aaf0bf553cf5056ecfa4e7d0da2b5ea3c`

### Position NFT

- NFT `7390304`，区块 `120854773`：Transfer + IncreaseLiquidity，交易 `0xc61b38b543fe686f9e34ec5f0f6c4a4482eff5f1b05ab5a248357a08cc523c1f`
- NFT `7390304`，区块 `120939915`：DecreaseLiquidity + Collect，交易 `0xf8477587396eb1f92d311fe7832987a49b24c4667ff750c661772e5902bc8043`
- NFT `7396868`，区块 `120941368`：Transfer + IncreaseLiquidity，交易 `0x932336759d104f4657327863a7e5b405ee3268b09963e4c6a4247d69b6b6093c`

本轮最终数据库快照包含：

- 814 笔 Swap
- 4 个 Position NFT
- 10 条仓位事件
- 28 个收益曲线点
- Swap 与仓位事件重复数均为 0

## 四、建议的学习计划

### 第一阶段：先讲清数据流

阅读 `main.py → chain.py → models.py → collector.py → api.py`，然后不看代码画出完整数据流。

完成标准：能够解释一次 Swap 从 BSC 日志进入数据库并显示在网页上的全过程。

### 第二阶段：吃透两个真实 Bug

1. RPC 请求间隔与请求并发的区别。
2. 为什么断点必须在每页完整成功后推进。

完成标准：能够脱稿讲清症状、根因、修改和正确性依据。

### 第三阶段：手算收益

阅读 `v3math.py`、`accounting.py` 和对应测试，手算至少三个曲线点。

完成标准：能够解释 Decrease 与 Collect 为什么不能都算收益。

### 第四阶段：主动讲边界

整理当前没有实现的内容，例如历史价格成本、Gas、reorg 回滚、MasterChef 仓位、数据库迁移、集成测试和真实 Telegram 验证。

完成标准：面试时既能说明实现，也不会把 Demo 描述成生产级系统。

## 五、HR 与技术面试表达

### HR 版本

> 我基于一个未经过真实网络验证的 BSC 监控原型，完成了主网联调和可靠性修复。系统能够监控 PancakeSwap V3 池子、钱包 LP NFT、链上事件和收益曲线。我重点解决了公共 RPC 限流、多线程请求并发、历史扫描断点以及重复数据问题，并使用真实交易和 BscScan 进行了核验。

### 技术面试版本

> 系统将 RPC 采集与 API 查询解耦：后台任务分段扫描确认区块，将日志和断点事务化保存到数据库，API 只读数据库。幂等依赖 `(tx_hash, log_index)` 唯一约束，失败时保留最后有效值并标记 stale。仓位核算中将 DecreaseLiquidity 视为待领取本金变化，只有 Collect 才视为实际到账，从而避免本金与手续费重复计算。

### 不应夸大的部分

- 不说“从零独立完成”，应说明是在离线原型上进行真实主网联调和修复。
- 不说 Telegram 已完全验收，因为没有配置真实 Token。
- 不说已经生产部署，因为目前没有 CI/CD、应用容器和完整监控。
- 不说已经解决所有链重组问题，目前只使用确认数降低风险。
