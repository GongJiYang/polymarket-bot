# Polymarket Bot

一个用于 Polymarket 加密货币 Up/Down 市场的 Python 交易机器人。

目前主要围绕 BTC 5 分钟市场开发，也保留了 ETH、SOL、XRP 和 DOGE 的策略接口。

## 它怎么做

机器人每隔一段时间读取：

- BTC 实时价格和近期波动率
- Polymarket 的 Up/Down 盘口
- 市场的 Chainlink 结算规则
- 距离本轮市场结束还有多久

然后估算 Up 和 Down 的概率，和盘口价格比较。如果预估价格差足够大、盘口有足够数量、市场数据没有过期，就生成交易信号。

BTC 策略还会把波动率放大到 1.25 倍，再算一次。压力测试不过，就不交易。

简单说：

```text
估算概率 > 市场价格 + 手续费和滑点
                         ↓
                       产生信号
```

## 风控

当前 BTC 策略的默认规则：

- 止盈：净收益 5%
- 止损：净亏损 8%
- 超时：持仓 30 秒后尝试退出
- 最多提交 3 次卖单
- 买入前检查余额、授权、盘口和市场状态
- 买入和卖出都要核对真实成交记录
- 未知订单结果不自动重试
- 持仓状态写入本地文件，支持重启后恢复
- 数据异常时停止交易，不使用默认值硬算

这些规则由本地程序执行，不是交易所托管止损。程序退出、网络中断、盘口没深度或市场已经结束时，可能无法卖出。

## 现实限制

这不是一个已经证明盈利的策略。

概率模型使用短期波动率和正态分布近似，现实中的 BTC 会跳价，波动也会突然变化。盘口价格不代表一定能成交，买得到也不代表之后卖得掉。手续费、滑点、网络延迟和结算延迟都会影响结果。

之前的一次实盘运行中，买入成功后持仓管理提前退出，导致退出规则没有执行。相关的成本核算、成交确认和恢复代码已经修复，但仍然不应该直接相信程序可以自动保护资金。

先用 Shadow 模式观察，不要因为测试通过就扩大资金。

## 安装

要求 Python 3.11+ 和 `uv`：

```bash
uv sync --extra dev
```

运行测试：

```bash
uv run pytest -q
```

构建安装包：

```bash
uv build
```

## Replay

用历史 JSONL 数据重放策略，不访问账户，也不下单：

```bash
uv run polymarket-bot replay <input.jsonl> \
  --strategy chainlink_terminal_spot_v9
```

## Shadow

读取真实公开行情，只记录模拟信号，不提交订单：

```bash
uv run polymarket-bot shadow \
  --strategy chainlink_terminal_spot_v9 \
  --audit /tmp/polymarket-shadow.jsonl \
  --monitor-seconds 3600 \
  --sample-seconds 2
```

查看全部参数：

```bash
uv run polymarket-bot shadow --help
uv run polymarket-bot live --help
```

## Live

Live 模式需要手动准备新的授权 ID、授权过期时间和审计文件。密钥从 macOS Keychain 读取，不写进命令或仓库。

命令模板：

```bash
uv run polymarket-bot live \
  --strategy chainlink_terminal_spot_v9 \
  --wallet <deposit-wallet> \
  --relayer-api-key-address <signer-address> \
  --authorization-id <fresh-authorization-id> \
  --approved-by <operator> \
  --authorization-expires-at <UTC-expiry> \
  --quantity 5 \
  --max-order-debit 4 \
  --max-session-debit 4 \
  --monitor-seconds 86000 \
  --sample-seconds 2 \
  --audit /tmp/<fresh-audit-name>.jsonl \
  --compliance-confirmed \
  --submit
```

每次重启或恢复都要使用新的授权 ID。`--resume-position` 只恢复已有持仓，不会重新买入。

## 策略

| 名称 | 资产 |
|---|---|
| `chainlink_terminal_spot_v9` | BTC |
| `eth_chainlink_terminal_spot_v5` | ETH |
| `sol_chainlink_terminal_spot_v3` | SOL |
| `xrp_chainlink_terminal_spot_v3` | XRP |
| `doge_chainlink_terminal_spot_v3` | DOGE |
| `polyrec_impulse_fade_underdog_v1` | 研究策略 |

## 目录

```text
src/polymarket_bot/
  adapters/       公开数据和执行适配器
  live/           授权、下单、成交确认、持仓管理
  microstructure/ 概率和订单簿模型
  strategies/     策略实现
  cli.py          命令行入口
  runners.py      运行编排

tests/            测试
docs/             策略和运行记录
```

## 来源

项目是在 `forecasting-tools` 相关代码和思路上拆分、重写出来的，当前仓库专门放 Polymarket 策略和交易执行代码。

## 许可证

当前没有添加许可证文件。未经授权，不应把本项目当作自己的产品或交易服务使用。
