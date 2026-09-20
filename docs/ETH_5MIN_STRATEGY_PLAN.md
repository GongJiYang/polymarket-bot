# ETH 5 分钟 Up/Down 策略迁移文档

> 适用仓库：`/Users/gjy/polymarket-bot`
>
> 状态：ETH 独立数据、模型与 Shadow 路径已实现；2026-08-30 已接入 BTC+ETH 单会话、共享一次 POST 的小额实盘路径。
>
> BTC 基线：[`BTC_5MIN_STRATEGY_AND_FILE_MAP.md`](./BTC_5MIN_STRATEGY_AND_FILE_MAP.md)

## 1. 结论

ETH 五分钟盘口可以复用 BTC 策略的框架，但不能简单把市场 slug 从 `btc` 改成 `eth`，更不能用 BTC 的 Chainlink 信号直接交易 ETH。

可复用部分：

- 终局概率模型的数学结构；
- 双边盘口和动态手续费计算；
- 连续独立确认；
- 不可变 intent、单笔/会话上限和 FAK 提交；
- geoblock、合规、余额、allowance、freshness 和审计边界。

必须按 ETH 重建或重新校准的部分：

- ETH 市场发现、condition id 和 token id；
- ETH/USD Chainlink spot 数据流标识；
- Gamma `priceToBeat` 和结算数据源校验；
- ETH 的短时波动率估计；
- 净优势阈值、确认次数和执行储备的证据；
- ETH 盘口的成交深度、滑点和实际手续费样本。

## 2. ETH 市场事实

2026-08-28 实测当前市场命名模式：

```text
eth-updown-5m-<five-minute-epoch>
```

对应问题示例：

```text
Ethereum Up or Down - August 28, 11:20AM-11:25AM ET
```

市场描述指定的结算源：

```text
https://data.chain.link/streams/eth-usd-twap-60s-streams
```

结算条件与 BTC 相同形态，但资产不同：ETH 窗口结束价格不低于窗口起始价格则 `Up`，否则 `Down`。因此每个 ETH 决策必须以 ETH/USD 数据为准。

## 3. ETH 信号方法

### 3.1 快照

每个决策周期需要同一 ETH 窗口的：

1. Gamma 市场元数据、精确 `priceToBeat` 和结算源；
2. ETH Up 与 Down token 的新鲜 CLOB 订单簿；
3. ETH/USD 官方 spot 历史和实时值；
4. ETH 市场的动态手续费参数；
5. 账户余额、allowance 和 geoblock 状态。

禁止把 BTC spot 或 BTC benchmark 混入 ETH 快照。资产字段应成为快照 hash、授权和审计记录的一部分，避免跨资产串线。

### 3.2 概率和净优势

模型在最终 60 秒 TWAP 尚未开始时，使用当前 spot、短时压力波动率和剩余时间近似其终局分布：

```text
p_up_eth = P(ETH 终局 60 秒 TWAP >= Gamma priceToBeat | ETH spot、剩余时间、ETH 波动率)
```

方向优势：

```text
up_raw_edge   = p_up_eth       - eth_up_best_ask
down_raw_edge = (1 - p_up_eth) - eth_down_best_ask
```

净优势：

```text
net_edge = 方向概率 - 最差允许成交价 - ETH 市场 fee_per_share - execution_reserve
```

BTC 当前阈值 `0.04` 只能作为影子运行的初始观测参数，不能直接当作 ETH 已验证阈值。ETH 的 spot 波动、盘口价差、手续费曲线与深度都可能不同。

### 3.3 连续确认

第一版 ETH 影子运行应保留 BTC 的严格语义：

- 同一 market、strategy、asset、direction、token；
- 官方 ETH 源时间戳严格前进；
- 终局概率、净优势和价格漂移不越界；
- 最终快照重新计算仍是同一候选；
- 距窗口结束至少 45 秒。

可以先记录 3、4、5 次确认各自的候选存活率和最终方向，但实盘确认次数必须由 ETH 自己的 matched observation 决定，不能由 BTC 的样本替代。

## 4. 源代码改造边界

当前 BTC 专用点位主要在：

| 当前文件 | 当前 BTC 绑定 | ETH 改造要求 |
|---|---|---|
| `src/polymarket_bot/live/official_chainlink.py` | BTC feed id、product id、market id、symbol 和 WS 过滤规则硬编码 | 建立显式资产配置；每个资产绑定自己经验证的 TWAP/spot 标识和解析规则；未知标识失败关闭 |
| `src/polymarket_bot/adapters/public_data.py` | `CurrentBtcSnapshotAdapter` 和 BTC slug/解析 | 泛化为按授权资产构造的 snapshot adapter；市场描述的结算源必须与资产配置一致 |
| `src/polymarket_bot/strategies/chainlink_terminal.py` | 数学本身近似资产无关，但策略身份是 BTC v5 | 新建明确的 ETH strategy id/model version，避免 BTC/ETH 审计混淆 |
| `src/polymarket_bot/cli.py` | 授权市场族写为 `BTC Up/Down <interval>` | 增加受限 `--asset`，资产必须进入授权 hash；默认仍为 BTC 或要求显式指定 |
| `src/polymarket_bot/contracts.py` | 上下文未把资产作为独立强类型边界 | 在市场元数据/策略上下文中保留规范化资产身份 |
| `src/polymarket_bot/live/bounded_bot.py` | 授权绑定市场族和模型版本 | 资产、市场族、模型版本必须完整覆盖，不允许 BTC 授权提交 ETH intent |
| `src/polymarket_bot/runners.py` | 信号 key 未显式展示资产，但由 market/token 间接区分 | 将资产加入信号和审计身份更安全；跨资产切换必须清空确认 streak |

不应复制整套 BTC 文件形成 `*_eth.py` 平行实现。更可靠的切法是：一个严格的资产配置模型 + 共享算法；所有资产特有标识集中在配置中，算法不接受裸字符串隐式回退。

## 5. 需要新增或扩展的验证

| 文件 | ETH 验收内容 |
|---|---|
| `tests/test_official_chainlink.py` | ETH HTTP/WS payload、feed/product/market 过滤；BTC 数据不能通过 ETH 过滤器 |
| `tests/test_public_data_adapter.py` | ETH slug、结算源、token、benchmark、TWAP/spot 同资产一致性；混合 BTC/ETH 输入必须拒绝 |
| `tests/test_terminal_strategy.py` | 使用 ETH 波动与盘口向量验证 Up/Down 概率、费用和净优势 |
| `tests/test_architecture.py` | 资产切换清空 streak；授权资产、market、token、model version 完整匹配 |
| `tests/test_execution_adapter.py` | BTC 授权不能提交 ETH；ETH intent 只能使用 ETH final snapshot |
| `tests/test_live_cli.py` | 只允许白名单资产；未指定、未知或授权不覆盖时失败关闭 |

永久新增的行为契约至少包括：

1. ETH 盘口不能消费 BTC Chainlink 更新；
2. BTC 盘口不能消费 ETH Chainlink 更新；
3. ETH 实时源断开时不能退回 BTC；
4. 资产变化必须重置确认计数；
5. 单个会话授权只覆盖明确的一种资产，除非另有显式多资产总预算授权；
6. 同一资产的订单仍只能 POST 一次，异常继续失败关闭。

## 6. 上线阶段

### 阶段 A：只读数据验收

- 连续发现活跃 ETH 五分钟窗口；
- 市场结算 URL 与 ETH/USD 配置一致；
- ETH TWAP 和 spot 历史完整；
- WS 时间戳单调前进；
- Up/Down 订单簿新鲜；
- 不创建 intent，不签名，不 POST。

### 阶段 B：Shadow 信号

- 使用 ETH 自己的数据计算候选；
- 同时记录 1–5 次确认存活；
- 每个窗口结束后从 Gamma/CLOB 记录官方 winner；
- 计算方向准确率、候选覆盖率、净优势分布和盘口可成交深度；
- 不提交订单。

### 阶段 C：Dry execution

- 构造不可变 ETH intent；
- 跑完整 geoblock、市场、book、余额、allowance、风险和授权链；
- 展示精确最大损失与短 TTL；
- 不调用 POST，不自动 approve。

### 阶段 D：BTC+ETH 共享小额实盘（已启用）

- BTC 与 ETH 各自生成绑定市场族和模型版本的子授权；
- 两个资产共享同一个会话资金上限和一次 POST 容量；
- 两个资产独立完成连续确认，并在签名前重新读取自己的最终快照；
- 第一笔完成确认且通过临发单盘口复核的候选执行，随后整场停止；
- 成交后仍须核对官方回执、平均成交价、手续费、净执行优势和最终结算；
- 单个样本不用于宣称稳定收益。

## 7. BTC+ETH 运行模板

CLI 通过重复 `--strategy` 显式选择两个实盘策略；没有 `--asset` 参数：

```bash
uv run polymarket-bot live \
  --strategy chainlink_terminal_spot_v7 \
  --strategy eth_chainlink_terminal_spot_v3 \
  --wallet <deposit-wallet> \
  --relayer-api-key-address <signer-address> \
  --authorization-id <fresh-btc-eth-authorization-id> \
  --approved-by gjy \
  --authorization-expires-at <UTC-expiry> \
  --max-order-debit <shared-order-cap> \
  --max-session-debit <shared-session-cap> \
  --sample-seconds 2 \
  --audit /tmp/<fresh-btc-eth-audit>.jsonl \
  --compliance-confirmed \
  --submit
```

## 8. 不可越过的边界

- 不用 BTC 方向替代 ETH 终局概率。
- 不沿用 BTC 的 `priceToBeat`、spot、feed id 或 token。
- 不因 ETH 与 BTC 常常同涨同跌就跳过 ETH 的独立连续确认。
- 不把某个资产的旧会话授权解释为当前共享余额或预算。
- 不同时用多个机器人争抢同一总余额；BTC 与 ETH 必须共享同一个原子容量账本。
- 未完成独立数据与 Shadow 验证的资产不得进入实盘授权。
