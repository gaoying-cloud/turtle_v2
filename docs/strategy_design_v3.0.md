---
version: "5.18"
date: "2026-06-29"
based_on: "V5.18 (2026-06-29)"
---

# 跨市场ETF海龟组合策略 — 设计文件 V5.11

**V5.18 变更**：
- 缓存数据复权缺失修复：因早期 Tushare token 不可用，`fund_adj` 降级后静默写入未复权数据。现已 `--force` 重拉，并加固降级保护——`_detect_and_adjust_splits` 无事件时返回空不再写入；`fetch_single` 复权失败时跳过保存保留旧缓存

**V5.8~V5.11 变更**：
- 数据复权深度修复：`_apply_factor_adjustment` 中 `pre_close` 改为 `close.shift(1)` 消除人工价格缺口；`_save_to_parquet` 新增 `overwrite` 参数，`force` 模式覆盖旧缓存
- `_detect_and_adjust_splits` 逆向分段调整，修复多拆分事件中间段遗漏 bug
- `_apply_factor_adjustment` 对 `fund_adj` 缺失日期 forward-fill，避免未调整行造成价格缺口
- `_pi_ij`/`_rho_ij` Ledoit-Wolf 公式修复（`_pi_ij` 原偏差 503x）
- `_check_5day_drawdown` 恢复全部品种暂停（之前仅预警不暂停）
- 6 个脚本 `SIX_SYMBOLS` 硬编码全部迁移到 `config_loader`
- 单品种敞口 `0.04` 改为 `self.params.single_max_risk`，从 config 读取
- `scripts/cross_validate.py`: TickFlow 交叉校验工具（ETF ×6 + 国债）
- `.pre-commit-config.yaml` + `.github/workflows/ci.yml`: CI/pre-commit 配置
- 全量测试 185/185 passed
- 回测基线：模式 A +127.73%，模式 B -18.36%，4 策略对比 B4 +141.78%
- 新增 `scripts/analyze_n_percentile.py` — 历史验证脚本，逐年计算 regime score 并与策略收益关联
- 数据管道新增后复权处理：Tushare `fund_adj` 获取复权因子 → 后复权 OHLC，消除 ETF 份额折算/分红导致的价格跳跃
- 备选方案：Tushare 不可用时自动降级为价格检测法（pre_close vs prev_close 跳跃检测 + 累积因子修正）
- 对应更新 §2.3 补充复权说明、新增 §5.2.1 复权处理、新增 §5.11 市场状态判断

**V5.6 变更**：
- 删除旧 P2 累计亏损金额冻结规则（近15笔亏损≥15%封禁），该逻辑对多品种返回相同亏损比例，存在 bug
- 新增投票式信号确认系统：成交量确认 `volume_confirmation()`、K线形态确认 `breakout_quality()`、近期胜率监控 `recent_batting_avg()`，由 `min_confirmations` 统一控制（0=关闭，默认关闭）
- `p2_mode` 简化：移除 `"cumulative_loss"`，仅保留 `"none"` 和 `"batting_avg"`，默认 `"none"`
- 新增 `use_signal_filter` 开关控制 SignalFilter（默认开启）
- `max_cumulative_loss_pct` 参数废弃保留，兼容网格搜索

**V5.3 变更**：
- 全品种配置从 `config/turtle_config.yaml` 统一读取，新增 `shortable`、`t_plus_one` 字段
- 新建 `src/config_loader.py` — 6 个配置读取函数，消除所有代码中的硬编码品种列表
- `strategies/turtle_trading.py`: 删除 `T_PLUS_ONE_SYMBOLS`、`SHORTABLE_SYMBOLS` 模块常量，改为 `self.params.t_plus_one_symbols`、`self.params.shortable_symbols`，从外部传入
- `_bond_switch()` 方法移除（`_in_bond`、`_bond_data` 字段一并删除）
- `_should_enter_short` 和 `_check_entry` 的空头判断改为 `code in self.params.shortable_symbols`
- 6 个脚本均改为 `from src.config_loader import load_config, get_*, ...`
- 新增 `config/` 数据源：品种属性由 yaml 声明，扩展只需追加记录
- 新增 SignalFilter 盈利过滤器：每个品种记录上次交易结果，盈利→接受下次信号，亏损→跳过，连续跳过3次后强制放行

**V5.4 变更**：
- 配置参数 `max_portfolio_risk` 从硬编码改为从 `config/turtle_config.yaml` 读取，当前值 0.20
- `max_5day_drawdown_pct` 默认值从 0.08 对齐设计文档为 0.10

**V5.2 变更**：
- P0 修复累计亏损计算错误：删除 `_cumulative_loss_pct` 废弃字段，统一从 `_my_trades` 实时计算
- P0 新增 5 日滚动回撤预警 `_check_5day_drawdown()`，超 `max_5day_drawdown_pct`（默认 8%）阈值暂停交易 5 天
- 新增参数 `max_5day_drawdown_pct` 和 `_equity_history` 净值历史缓存

**V5.1 变更**：
- P1 移除国债切换死代码：`_bond_switch()`、`_in_bond`、`_bond_data` 全部删除
- P1 ETF 禁止空头：仅 `SHORTABLE_SYMBOLS`（纳指+黄金）可做空，A 股 ETF 禁止空头入场

**V5.0 变更**：
- S6: T+0 双向回测 — 添加 `direction` 字段到交易记录 + 品种级多空明细输出
- `_execute_exit` 交易记录新增 `direction` 字段（long/short），支持按品种和方向分别统计盈亏
- 品种级多空明细输出：回测报告按品种拆分做多/做空净收益、胜率、交易次数
- 对应更新 `strategies/turtle_trading.py`（+31 行）

**V4.0 变更**：
- 退出逻辑从「仅 10 日低点退出」升级为 **三重退出保护**（2N固定止损 + 移动止损只上移 + 10日反向突破）
- 入场信号从 `high > entry_high` 改为 `close > entry_high`（收盘确认突破，过滤假突破）
- 止损触发从 `close ≤ stop_loss` 改为 `low ≤ stop_loss`（更快止损）
- 仓位公式从 `equity·risk/N/price` 改为 `equity·risk/(2·N)`（与 automated_trading 对齐）
- 绩效提升从 -1.90% → +11.17%（全对齐条件下），归因实验详见 `docs/检验执行计划.md`
- **三层风控升级**：新增三层敞口校验（单品种≤4%，全账户≤15%）+ 渐进式集中度熔断 + 滑动窗口累计亏损暂停
- 对应更新 §4.2 退出、§5.2 入场、§7.1 风控
- 全量 185 测试通过，无回归

**V3.7 变更**：
- S7 压力测试模块完整实现（§5.9 + §5.10）：
  - `scripts/run_stress_test.py` — 4 个历史情景回放 + 2 个合成情景（B1 冲击矩阵、B2 流动性枯竭）
  - `scripts/run_correlation_monitor.py` — 60 日滚动两两相关性 + 预警事件检测
  - `tests/test_stress_test.py` — 32 个测试全部通过
  - `scripts/gen_report.py` — 对接 stress_conclusion.json，消除 S8 报告中的 S7 占位符
  - 输出：`results/stress_test/` 下 5 个产物文件 + 相关性报告

**V3.6 变更**：
- B4 兼容性修复（Python 3.14 + Backtrader 内部属性冲突）：
  - `self._trades` → `self._my_trades`：避免覆盖 Backtrader 内部 `self._trades` dict
  - `_next_idx()` 增加负值和越界保护：`len(self)=0` 时返回 0
  - `next()` 增加 `len(self) < 2: return`：跳过第 0 个 bar 数据不全
  - B4 在 `scripts/run_comparison.py` 中改用独立 Cerebro 实例
- S5 四个基准全部成功运行：

  | 基准 | 最终净值 | 总收益率 |
  |:--|:--:|:--:|
  | B1 买入等权持有 | ¥360,666 | +80.33% |
  | B2 等权再平衡 | ¥409,609 | +104.80% |
  | B3 ATR等风险 | ¥201,956 | +0.98% |
  | B4 海龟+国债 | ¥196,951 | -1.52% |

**V3.5 变更**：
- 修复 data_pipeline 拉取数据后回测引擎的 4 个兼容性 bug
- `docs/MarkdownMcp 配置手册（AI 助手专用）.md` 新增文档索引

**V3.4 变更**：
- 新增 §5.11 S8 综合报告生成施工图设计（scripts/gen_report.py）

**V3.3 变更**：
- 新增 §5.9 S7 极端情景回测 + 压力测试施工图设计
- 新增 §5.10 滚动相关性监控模块设计（独立脚本 scripts/run_correlation_monitor.py）

**V3.2 变更**：
- 新增 §5.8 S6 参数网格搜索施工图设计（完整模块架构、接口、CLI、输出产物）
- α 搜索范围扩展为 [0%, 5%, 10%, 15%, 20%]，合计 405 组 × 2 模式 = 810 次回测

**V3.1 变更**：
- 新增 §2.5 ETF结算规则差异说明
- 新增 §5.7 T+0/T+1 结算规则差异及其对回测的影响
- 已知风险汇总新增 "T+1 品种止损滞后" 风险项
- 附录 A 文档索引新增 `docs/analysis/t+0_t+1_impact.md`

**V3.0 变更**：
- 策略实现从 automated_trading 子模块迁移为**独立项目 `turtle_v2/`**，物理隔离
- 回测引擎统一为 **Backtrader**（删除 VeighNa 两阶段方案）
- 工程项目隔离规则重写（物理隔离替代目录隔离）
- 文件路径与实施路线图同步更新
- 项目管控模型单独成文（`docs/governance_model.md`）

---

## 一、策略背景与设计目标

### 1.1 策略概述

本策略以 **4 只低相关性 ETF** 为投资标的，采用 **海龟交易法则** 作为核心交易逻辑，融合 ATR 仓位管理与风险平价偏移，构建多资产趋势跟踪组合。

### 1.2 核心目标

| 指标 | 目标范围 |
|:--|:--|
| 年化收益率 | ≥15% |
| 最大回撤 | ≤25% |
| 夏普比率 | ≥0.8 |
| 盈亏比 | ≥1.5 |
| 年交易次数（6品种合计） | ≥50次 |

### 1.3 核心假设

1. 金融市场存在可捕捉的趋势行情（尤其肥尾行情），海龟法则在此类市场中有效。
2. 多资产组合的分散效应在大部分市场环境下可降低组合波动。
3. 技术假设：ATR 作为波动率度量在当前市场结构下仍然有效，展期损益可预测/可管理。

> **⚠️ 已知风险**：上述假设在极端危机行情下可能全部失效——当所有资产相关性趋近于 1 时，组合分散失去意义。详见 §3.5 与 §6.3。

---

## 二、ETF组合构建

### 2.1 标的选择原则

- **跨市场**：覆盖 A 股、跨境（美股）、商品
- **低相关性**：两两相关系数 ≤ 0.5，商品类 ≤ 0.2（在正常市场环境下，详见 §3.5）
- **高流动性**：日均成交额 > 1 亿元（以 Dry-Run 阶段的实际数据为准）
- **高波动性**：年化波动率 ≥ 15%（确保足够的信号频率）
- **上市时间足够**：2018 年以前上市（≥7 年数据覆盖完整牛熊周期）
- **避开跨境障碍**：仅保留一只 QDII 品种（纳指ETF），降低溢价/额度风险敞口

### 2.2 建议组合列表

| 类别 | ETF名称（代码） | Tushare代码 | 核心价值 | 结算规则 |
|:--|:--|:--|:--|:--:|
| A股中小盘 | 中证500ETF (510500) | 510500.SH | 核心弹性品种 | T+1 |
| A股成长 | 创业板ETF (159915) | 159915.SZ | 最高波动宽基，独立行情 | T+1 |
| 跨境美股 | 纳指ETF (513100) | 513100.SH | 与A股低相关（~0.3），独立信号源 | **T+0** |
| 商品 | 黄金ETF (518880) | 518880.SH | 零相关避险，实物型无跨境风险 | **T+0** |

> **选品说明**：从 10 只候选品种中按约束条件逐层筛选。排除理由：国债ETF(511010)波动率~3%不适合海龟，转为空仓期现金管理工具；标普500(513500)与纳指相关性高功能重叠；恒生ETF(159920)相关性0.6偏高且QDII；豆粕ETF(159985)上市2019年数据不足且展期成本年化5-15%。

### 2.2 品种筛选量化框架（教训总结 V5.11）

从科创50(588000)和中证1000(159845)的回测教训中总结出以下硬门槛。**品种选择是策略的一部分，不是回测的输入。** 每新增一个候选品种，必须依次通过以下检查。任意一项不通过即淘汰。

| 序号 | 检查项 | 指标 | 阈值 | 工具 | 自动化 |
|:--:|:--|:--|:--:|:--|:--:|
| ① | **数据质量** | cross_validate 最高级别 | worst_level ≠ "critical" | `scripts/cross_validate.py` | ✅ |
| ② | **上市年限** | 覆盖至少一轮完整牛熊 | ≥ 2018 年（≥7年） | `screen_candidates.check_listing_age()` | ✅ |
| ③ | **流动性** | 日均成交额 | > 2 亿 | `screen_candidates.check_liquidity()` | ✅ |
| ④ | **品种独立盈亏能力** | 单品种海龟独立回测 | 盈亏比 ≥ 1.0, CAGR > 0 | `screen_candidates.check_standalone_backtest()` | ✅ |
| ⑤ | **低相关性** | 与已有品种的60日滚动相关系数 | ≤ 0.5 | `screen_candidates.check_correlation()` | ✅ |
| ⑥ | **信号源独立性** | 优先填充"空白象限"，非已有象限堆叠 | 参见下表 | 人工判断 | ❌ |
| ⑦ | **交易规则兼容** | T+0品种优先 | T+1 ≤ 组合总数 50% | `screen_candidates.check_t1_ratio()` | ✅ |
| ⑧ | **组合边际贡献** | N+1回测对比 | 夏普不降、回撤不恶化 > 2% | 人工 `run_backtest` 对比 | ❌ |

> **自动化覆盖**：⑧ 道中 ⑥ 道已脚本化（①②③④⑤⑦）。第⑥道（空白象限判断）和第⑧道（组合边际贡献）仍需人工决策。趋势持续性通过 Hurst 指数（`screen_candidates.check_trend_persistence()`）作为第④道的补充检查，WARN 不阻断。

**空白象限** — 当前组合（V5.11）已覆盖：

| | A股 | 非A股 |
|:--|:--|:--|
| 权益 | 中证500、创业板 | 纳指ETF |
| 非权益 | **空缺** | 黄金ETF |

优先从 **A股非权益** 象限（可转债ETF、REITs）寻找新品种，而非在已有象限堆叠更多宽基。

> 科创50的教训：2020年才上市（不满足②），与创业板相关性 r=0.81（不满足⑤），进入组合后 SignalFilter 自动拒绝所有信号，实际零贡献。**选品标准的缺失，让无贡献品种占用了组合的品种额度。**

### 2.3 数据源

| 数据源 | 状态 | 说明 |
|:--|:--|:--|
| **Tushare Pro（主力源）** | ✅ 已验证 | 全部 4 只 ETF 日线通过 `fund_daily` 接口可用 |
| Baostock（备选源） | ✅ 已验证 | A 股 ETF 日线可获取，跨境 ETF 不可用 |
| yfinance | ⏳ 待验证 | 跨境数据应急源，国内网络稳定性未知 |

> **复权处理**：Tushare `fund_daily` 返回的价格为不复权数据。数据管道在清洗后自动调用 `fund_adj` 接口获取复权因子，以最新日期为基准做**后复权**（backward adjustment），消除 ETF 份额折算/分拆/合并对历史价格的跳跃影响。若 `fund_adj` 不可用，降级为价格跳跃检测法（对比 pre_close 与昨日 close）。详见 §5.2.1。

### 2.4 交易日历方案

每个品种使用**自己的交易日历**独立推进。在 Backtrader 中按各自的时间轴加载数据，仅在信号合并/仓位计算的时刻对齐到同一时间戳。若某品种当日无交易数据（非交易日），该品种维持上一日状态，不产生信号。

### 2.5 ETF 结算规则差异

组合中 **4 只 A 股 ETF 为 T+1 结算（当日买入不可卖出）**，**3 只 ETF（纳指、黄金、国债）为 T+0 结算（当日可双向交易）**。T+1 规则对海龟策略的止损和加仓执行产生直接影响，具体分析见 §5.7。

---

## 三、权重分配与仓位管理

### 3.1 三层融合权重设计

#### 3.1.1 基准层：等权分配

初始 4 只 ETF，每只名义权重 = 25%。

#### 3.1.2 第二层：海龟式 ATR 仓位管理（调整层）

- 每只 ETF 独立计算 N 值（20 日指数平滑平均真实波幅，ATR）。
- 头寸规模（股数）= `（账户净值 × 1%）/ N`。
- 效果：高波动品种自动降低股数，低波动品种自动增加股数。每个品种对账户的日内风险贡献基本相等。

#### 3.1.3 第三层：α 风险平价偏移（优化层）

- 计算过去 **252 个交易日** 的收益率协方差矩阵。
- 使用 **Ledoit-Wolf 收缩估计** 替代简单样本协方差，提高样本外稳定性。
- 求解风险平价权重（使各资产边际风险贡献相等）。
- 最终权重 = `(1-α) × w_ATR + α × w_RP`，α 默认 5%。

> **α 参数说明**：此参数为待优化参数。强建议在回测中做消融实验，分别测试 α = 0、5%、10%、20% 对绩效的影响。

### 3.2 再平衡规则

| 触发条件 | 操作 |
|:--|:--|
| 海龟信号触发（入场/止损/退出） | 执行对应品种的调仓 |
| ATR 变动超过 30%（即使无信号） | **强制再平衡**该品种头寸规模 |
| 每季度末（即使无任何信号） | 全组合再平衡，修正权重漂移 |

### 3.3 资金规模测算

| 场景 | 说明 | 所需资金 |
|:--|:--|:--|
| **正常场景** | 4 只 ETF 平均各持有 1-2 单位 | ~5 万 - 10 万元 |
| **极端场景** | 4 只 ETF 同时满仓（各 4 单位） | ~20 万 - 36 万元 |

> **建议资金**：**20 万元**。

### 3.4 商品ETF展期损耗

组合中唯一的商品类 ETF 为黄金ETF (518880)，属于实物型黄金 ETF，无展期损耗。

### 3.5 空仓期现金管理（V5.3 已废弃）

> ⚠️ **V5.3 已移除**：国债ETF切换功能已从代码中删除。`config/turtle_config.yaml` 中仍保留 `bond` 配置用于数据拉取，但不产生实质性交易。

国债ETF (511010) **不纳入**海龟策略组合。

| 策略 | 标的 | 预期年化贡献 |
|:--|:--|:--|
| **空仓期现金管理** ⛔ 已移除 | 国债ETF (511010) | — |

**原设计（V5.2及之前）**：
- 海龟 4 品种均无持仓时买入国债ETF
- 下个海龟信号产生时优先卖出国债ETF

**移除原因**：空仓期资金自动留在现金账户中产生利息（Broker 默认），国债ETF买卖产生额外的佣金和滑点成本，且与 Backtrader 的现金管理机制重叠。

### 3.6 尾端风险与相关性时变分析

**应对措施**：

| 措施 | 说明 |
|:--|:--|
| **滚动相关性监控** | 每日计算 60 日滚动两两相关性矩阵，当平均相关系数超过 0.6 时触发预警（不得新开多头仓位） |
| **极端情景回测** | 必须回测 2020 年 3 月、2015 年 6-8 月、2018 年全年的组合表现 |
| **压力测试** | 模拟所有资产单日同步下跌 3%-5% 时，组合在 ATR 仓位下的理论损失 |

---

## 四、海龟交易信号规则

### 4.1 入场信号

- **突破入市**：价格突破过去 20 日高点（多单）或低点（空单），使用 `close > entry_high_20` 收盘价确认（V4.0 从 `high` 改为 `close`，过滤盘中假突破）。
- **盈利过滤器（SignalFilter）**：V5.0 新增。每个品种记录上次交易结果：盈利 → 接受下次信号；亏损 → 跳过，连续跳过 ≥3 次后强制放行（避免永久封禁）。首个信号无条件接受。可通过 `use_signal_filter=False` 关闭。
- **投票式信号确认（V5.6 新增，默认关闭 `min_confirmations=0`）**：在 SignalFilter 之后、仓位计算之前，提供三层独立确认规则——成交量放大（`volume_confirmation()`, 默认 1.5 倍均量）、K线实体质量（`breakout_quality()`, 默认实体占比 > 40%）、近期胜率（`recent_batting_avg()`, 近 8 笔亏损占比 < 75%）。由 `min_confirmations` 控制最少通过数（0=关闭, 1=至少一个, 2=至少两个, 3=全部）。三规则均通过函数参数独立开关，不独裁仅打分。
- **过滤条件**：**默认不使用 55 日过滤**（以保证足够的交易频率）。
- **⚠️ 必做对比实验**：在回测中必须包含以下两种模式的绩效对比：
  - 模式 A（默认）：仅 20 日突破入场，无过滤。
  - 模式 B（对照）：20 日突破 + 55 日同向过滤（海龟原版）。
- **加仓机制**：每上涨/下跌 0.5N 加仓一次，最多加至 4 个单位。

### 4.2 止损与退出

- **止损**：价格反向突破过去 10 日低点（多单）或高点（空单），即 `low ≤ stop_low_10`（多头），`high ≥ stop_high_10`（空头）。此为**唯一退出规则**。
- **退出**：止损触发时平掉全部仓位。
- **V3.8 → 方案A**：移除了原有的 2N 追尾止损规则（`close ≤ trail_high_10 - 2N`）。回测验证显示 2N 追尾止损在 A 股 ETF 震荡市中过早止盈/扩大亏损，导致盈亏比从 1.41 降至 0.93。回归经典 10 日低点退出后盈亏比恢复至 2.08，总收益率 +9.38%。方案A 进一步删除了代码中的 `_update_trailing_stop` 方法，当前仅 `_should_exit()` 一条退出线。

### 4.3 资金分配

- 每个初始单位占用账户总资金的 1% 风险（基于 ATR 计算）。
- 单品种最大总风险不超过 4%（最多 4 个单位）。
- 组合层面总风险敞口 ≤ 20%（通过 `config/turtle_config.yaml` 的 `risk.max_portfolio_risk` 配置）。

### 4.4 基准对比策略

| 对比基准 | 计算方式 | 作用 |
|:--|:--|:--|
| **买入等权持有** | 初始等权买入 4 只 ETF，除再平衡外不交易 | 衡量市场 beta |
| **等权定期再平衡** | 每季度按等权重再平衡 | 衡量简单分散化效果 |
| **ATR 等风险贡献** | 仅使用第二层（ATR 仓位），不使用海龟信号 | 衡量风险平价的独立贡献 |
| **海龟纯策略** | 海龟 4 品种（原含国债切换，V5.3 已移除） | 主策略绩效基线 |

---

## 五、回测框架与参数检验

### 5.1 回测引擎

**唯一引擎：Backtrader**（版本 ≥ 1.9.78.123）。

对比分析（回测库适配矩阵）：

| 策略需求 | Backtrader |
|:--|:--:|
| 多品种（4只ETF）同时回测 | ✅ |
| ATR(20) 内置指标 | ✅ |
| 20日突破/10日止损 | ✅ |
| 加仓（最多4单位） | ✅ |
| ATR仓位管理 | ✅ |
| 不同交易日历 | ⚠️ 需自定义 |
| 风险平价权重 | ⚠️ 需自定义 |
| 滑点/手续费 | ✅ |

### 5.2 数据获取

| 品种 | 数据源 |
|:--|:--|
| A 股 ETF | Tushare Pro (fund_daily) |
| 跨境 ETF | Tushare fund_daily |

### 5.2.1 复权处理（V5.7 新增）

ETF 日线原始数据来自 Tushare `fund_daily`，该接口返回**不复权**价格。ETF 在存续期内可能发生份额折算（拆分/合并）和分红，导致历史价格出现非交易性跳跃。

**复权方案：后复权（Backward Adjustment）**

以最新交易日为基准，将历史 OHLC 等比例缩放：

```
adjusted_price[t] = raw_price[t] × (latest_adj_factor / adj_factor[t])
```

**实现流程**（`src/data_pipeline.py`）：

1. `_fetch_adj_factors(code)` — 从 Tushare `fund_adj` 拉取复权因子序列
2. `_apply_factor_adjustment(df, adj_df)` — 应用后复权，跳过因子变化 < 0.1% 的日期
3. 若 `fund_adj` 不可用（token 缺失/接口异常），降级为 `_detect_and_adjust_splits(df)` — 检测 pre_close 与昨日 close 比值偏离 ±15% 的事件，累积因子修正
4. **降级保护**（V5.18）：若价格检测未发现 >15% 的偏差事件，`_detect_and_adjust_splits` 返回空 DataFrame，上层 `fetch_single` 跳过写入保留旧缓存，避免静默写入未复权污染数据

**品种覆盖**：全部 7 只 ETF（含国债）。期货无分红拆分，不适用。

**影响**：未复权的数据会导致大比例拆分日出现虚假涨跌，海龟策略可能产生错误的入场/止损信号，历史回测收益被拆分事件污染。

### 5.3 交易日历处理

- 各品种独立时间轴推进。
- 跨境品种在 A 股停牌日正常交易，使用该品种的实际价格。
- A 股在跨境品种停牌日使用上一交易日的填充值（前向填充）。

### 5.4 成本假设

| 成本项 | 假设值 |
|:--|:--|
| 综合滑点+手续费 | 单边 0.1%-0.2% |
| 冲击成本 | 需在 Dry-Run 中实际测量 |

### 5.5 参数敏感性分析

以下参数必须在回测中进行**网格搜索**：

| 参数 | 默认值 | 搜索范围 | 组合数 | 稳健性标准 |
|:--|:--|:--|:--:|:--|
| N 计算周期 | 20 日 | 15 / 20 / 25 日 | 3 | 夏普波动 < 0.2 |
| 突破周期 | 20 日 | 15 / 20 / 25 日 | 3 | 同上 |
| 止损周期 | 10 日 | 8 / 10 / 12 日 | 3 | 同上 |
| 2N 止损倍数 | 2N | 1.5N / 2N / 2.5N | 3 | 同上 |
| α（RP偏移） | 5% | 0% / 5% / 10% / 15% / 20% | 5 | 确认 α>0 是否优于 α=0 |

> **合计**：3×3×3×3×5 = **405 组参数组合**。每组分模式 A（无过滤）和模式 B（55日过滤），总计 **810 次回测**。

### 5.6 过拟合检验

| 方法 | 说明 | 资源需求 |
|:--|:--|:--|
| 样本内/样本外分割 | 2020-2022 训练，2023-2026 验证 | 低 |
| 滚动窗口回测 | 固定窗口 3 年，滚动推进 | 中 |

### 5.7 T+0/T+1 结算规则差异及其对回测的影响

#### 5.7.1 组合中的结算规则分布

| 品种 | 类别 | 结算规则 | 策略角色 |
|:--|:--|:--|:--:|
| 中证500ETF (510500) | A股 | T+1 | 核心持仓 |
| 创业板ETF (159915) | A股 | T+1 | 核心持仓 |
| 纳指ETF (513100) | 跨境QDII | **T+0** | 核心持仓 |
| 黄金ETF (518880) | 商品 | **T+0** | 核心持仓 |
| 国债ETF (511010) | 债券 | **T+0** | 现金管理工具 |

**4 只 T+1 + 3 只 T+0**，T+1 品种占策略核心仓位的大头。

#### 5.7.2 关键问题：T+1 品种的止损滞后

海龟策略止损规则：价格反向突破过去 10 日低点（多单）或高点（空单）。

**问题场景**：
1. Day T 09:35：T+1 品种触发 20 日突破买入，以价格 P 成交
2. Day T 10:15：价格急跌至 P - 2N，已触及 2N 止损线
3. ❌ **T+1 规则下当天无法卖出**
4. Day T+1 开盘可能进一步跳空低开，实际止损价比理论止损价差很多

**影响量化估计**：假设日波动率 ~2%，T+1 止损滞后可能额外增加 0.5%~1.5% 的滑点损失。在 2020 年 3 月、2022 年 3-4 月等剧烈波动期，回测年化收益率可能被**高估 1~3%**，最大回撤可能被**低估 2~5%**。

**回测修正要求**：

| 修正项 | 说明 | 优先级 |
|:--|:--|:--|
| **T+1 品种当日买入不可止损** | 若买入信号与止损信号同一天触发，实际无法在当日卖出。回测中应将此场景的止损推迟至下一交易日开盘执行 | **高**（否则回测会高估绩效） |
| **T+1 品种日内加仓不可逆** | 同一天多次加仓后无法反向平仓，回测应模拟此单向性 | 中 |
| **T+0 品种当日可完整循环** | 黄金ETF/纳指ETF在回测中可以假设当日即可完成完整交易循环 | 低（已是保守假设） |

#### 5.7.3 次要影响：日内加仓的流动性不对称

海龟加仓规则：每上涨 0.5N 加仓一次，最多 4 个单位。

- **T+1 品种**：买入信号密集触发但当日无法卖出已有仓位，加仓只能单向增加多头
- **T+0 品种**：如果 4 单位加完后价格急跌，当日可平掉部分仓位

在策略中，加仓基于趋势延续的假设，趋势当日反转是小概率事件。此问题影响等级为**中低**，回测中可暂不做修正，但需在 Dry-Run 阶段实测。

#### 5.7.4 现金流切换（无阻塞）

空仓期→入场切换流程：卖出国债ETF（T+0，资金实时可用）→ 买入中证500ETF（当日买入，T+1 交割）。买入不依赖卖出结算完成，现金流无阻塞。 ✅

#### 5.7.5 纳指ETF 的 T+0 + QDII 溢价叠加关注

纳指ETF 的 T+0 交易的是中国二级市场的人民币份额，其价格受底层资产 NAV 与溢价/折价双重影响。T+0 意味着日内可反复买卖"溢价"。海龟策略仅跟踪收盘价时，实际成交价与策略信号价可能偏差较大。

**应对**：严格执行 "溢价>3%暂不开仓" 风控规则（见附录 B 已知风险 #3）。

#### 5.7.6 回测实现要点

在 `strategies/turtle_trading.py` 中，对 T+1 品种需增加如下逻辑：

```
if 品种是 T+1 且 当日有买入成交:
    将该笔买入的 "最小持有期" 标记为 1 个交易日
    在止损检查中：若止损信号与买入信号同一天 → 跳过当日止损，下一日开盘执行
```

> **实现细节**：累计亏损百分比以初始资金（`self.broker.startingcash`）为固定基准计算，即 `累计亏损 ÷ 初始资金`，与设计文档 §6.2 中「累计亏损 > 总资金 15%」的固定基准语义一致。不使用变动净值作为分母，以避免因净值波动导致同金额亏损贡献不同百分比。
```

### 5.8 S6 参数网格搜索 — 施工图设计

基于 §5.5 定义的参数空间，本节给出网格搜索模块的完整架构、接口、CLI 设计。

#### 5.8.1 参数空间

| # | 参数 | config key | 默认值 | 搜索值 | 组合数 |
|:--|:--|:--|:--|:--|:--:|
| P1 | N 计算周期 | `turtle.atr_period` | 20 | [15, 20, 25] | 3 |
| P2 | 突破周期 | `turtle.breakout_period` | 20 | [15, 20, 25] | 3 |
| P3 | 止损周期 | `turtle.stop_period` | 10 | [8, 10, 12] | 3 |
| P4 | 2N 止损倍数 | `turtle.stop_atr_multiple` | 2.0 | [1.5, 2.0, 2.5] | 3 |
| P5 | α (RP偏移) | `weighting.alpha` | 0.05 | [0, 0.05, 0.10, 0.15, 0.20] | 5 |

**总计**：3×3×3×3×5 = 405 组参数组合。每组分模式 A（无过滤）和模式 B（55日过滤），**总计 810 次回测**。

#### 5.8.2 模块架构

```
scripts/run_grid_search.py
├── build_param_grid()          # 展开参数笛卡尔积 → list[dict]（405 组）
├── run_single_backtest()       # 单次回测包装器 → dict(metrics)
├── run_grid_search()           # 主循环：样本内 + 样本外分割
├── evaluate_results()          # 稳健性评估 + 最优参数选择（Top-10）
├── save_results()              # 写 CSV + JSON
├── plot_results()              # 散点图 + 热力图（可选，需 matplotlib）
└── main() / argparse CLI       # CLI 入口
```

#### 5.8.3 核心接口设计

**`build_param_grid()`** → `list[dict]`

使用 `itertools.product` 展开 5 参数的笛卡尔积，返回 405 个 dict，每个包含 `atr_period`, `breakout_period`, `stop_period`, `stop_atr_multiple`, `alpha`。

**`run_single_backtest(params, mode, start_date, end_date, run_id)`** → `dict | None`

接收参数组合 dict 和模式标识，通过 `cerebro.addstrategy(TurtleStrategy, ...)` 注入参数，运行 Backtrader 回测，返回标准化指标字典：

```python
{
    "run_id": int,          "mode": str,            # A/B
    "atr_period": int,      "breakout_period": int,
    "stop_period": int,     "stop_atr_multiple": float,
    "alpha": float,
    "total_return": float,  "cagr": float,          # 年化收益率
    "sharpe": float|None,   "max_drawdown": float,
    "win_rate": float,      "profit_factor": float,
    "total_trades": int,    "annual_vol": float,
    "calmar": float,        "final_value": float,
    "date_range": str,
}
```

参数注入方式：将 5 个搜索参数注入 `TurtleStrategy.params.turtle_params`（`atr_period`/`breakout_period`/`stop_period`/`stop_atr_multiple`），α 作为独立 param 传入。其他参数（`risk_per_unit`, `max_units` 等）使用 config 默认值。

**`evaluate_results(df, top_n=10)`** → `pd.DataFrame`

按模式分组计算稳健性评分：

```
robustness_score =
    0.25 × Sharpe(IQR标准化)    ← 收益风险平衡
  + 0.20 × Calmar(IQR标准化)   ← 回撤控制
  + 0.20 × CAGR(IQR标准化)     ← 绝对收益
  + 0.15 × (-MDD)(IQR标准化)   ← 回撤越小越好
  + 0.20 × log(trades)(IQR标准化) ← 交易频率充足
```

标准化使用中位数 + IQR 的稳健方法（对异常值不敏感）。夏普全为 NaN 时自动回退到 CAGR+MDD+trades 评分。

#### 5.8.4 样本内/外分割与过拟合检验

**默认分割**：
- 样本内：2020-01-01 ~ 2023-12-31（4 年训练）
- 样本外：2024-01-01 ~ 2026-06-10（2.5 年验证）

**流程**：
1. 样本内跑完全部 405 × 2 = 810 次回测
2. 按模式分组计算稳健性评分，选取 Top-10
3. 在样本外区间验证 Top-10 参数组合的绩效
4. 输出 `oos_validation.csv` 供人工对比衰减率

**滚动窗口检验**（可选 `--rolling`，默认关闭）：固定窗口 3 年，步长 1 年，3 个窗口（2020-2022, 2021-2023, 2022-2024），计算各指标均值和标准差。

#### 5.8.5 CLI 接口

```
用法: py scripts/run_grid_search.py [选项]

选项:
  --mode {A,B,all}        搜索模式 (默认: all)
  --start DATE            样本内起始日期 (默认: 2020-01-01)
  --split DATE            样本内/外分割日期 (默认: 2024-01-01)
  --end DATE              样本外截止日期 (默认: 2026-06-10)
  --rolling               启用滚动窗口检验 (默认关闭)
  --workers N             并行进程数 (默认: 4)
  --top N                 输出 Top-N 结果 (默认: 10)
  --quick                 快速验证 (抽样 10 组参数)
  --output PATH           输出目录 (默认: results/grid_search/)
  --plot                  生成参数敏感性散点图 + 热力图
  --verbose               详细日志
```

**并行化策略**：使用 `multiprocessing.ProcessPoolExecutor`（workers=4），每个进程独立创建 Cerebro 实例。预期在 4 核机器上将总耗时从 ~2 小时压缩到 ~35 分钟。

#### 5.8.6 输出产物

| 产物 | 路径 | 说明 |
|:--|:--|:--|
| **完整结果表** | `results/grid_search/grid_results_full.csv` | 样本内 810 行 × 18 列 |
| **样本外验证** | `results/grid_search/oos_validation.csv` | Top-10 在样本外的绩效 |
| **最优参数** | `results/grid_search/best_params.json` | 按稳健性评分排序的 Top-10（含完整绩效） |
| **散点图** | `results/grid_search/sensitivity_sharpe.png` | 5 参数 vs 夏普的散点图 |
| **热力图** | `results/grid_search/heatmap_{A,B}.png` | 突破周期 × 止损周期交互热力图 |

#### 5.8.7 风险与缓解

| 风险 | 缓解 |
|:--|:--|
| 810 次回测耗时过长 | 默认 4 workers 并行；`--quick` 抽样 10 组用于验证流程 |
| Backtrader 内存泄漏 | 每次回测后 `del cerebro + gc.collect()` |
| 数据日期边界不一致 | 所有回测使用统一的 CLI 日期参数 |
| α=0 时风险平价计算 | 策略层检查 alpha==0 时跳过 `compute_alpha_weights()` |

### 5.9 S7 极端情景回测 — 施工图设计

基于 §3.6 和 §6.3 的定义，本节给出极端情景回测与压力测试模块的完整架构设计。

#### 5.9.1 情景定义

设计文档要求回测 2020 年 3 月、2015 年 6-8 月、2018 年全年三个经典极端行情。
但因创业板(159915) 2020 年之前数据不足，2018 年和 2015 年无法全品种回测，**等价替换方案**如下：

| # | 场景 | 日期范围 | 特征 | 替代原因 |
|:--|:--|:--|:--|:--|
| A1 | COVID 熔断 | 2020-02-03 ~ 2020-04-30 | VIX>80，全球同步暴跌 | 保留原要求 |
| A2 | 俄乌冲突 | 2022-02-14 ~ 2022-04-29 | 商品暴涨 + 股市暴跌，黄金负相关 | 替代 2018 年贸易战 |
| A3 | A 股二次探底 | 2022-09-01 ~ 2022-11-30 | 持续阴跌 + 急跌交替 | 替代 2015 年股灾 |
| A4 | 完整 2022 年 | 2022-01-01 ~ 2022-12-31 | 全年熊市（创业板 -29%） | 覆盖全年压力 |

**合成压力情景**：

| # | 场景 | 触发方式 | 参数 |
|:--|:--|:--|:--|
| B1 | 单月同步暴跌 | 选定日期注入 -X% 价格冲击（每月一次） | X = 3%, 5%, 7% |
| B2 | 连续流动性枯竭 | 指定品种连续 N 天注入跌停价 | 品种 = 中证500, N=3, 每日 -10% |

B1 注入逻辑：在回测区间内**每月第一个交易日**向所有 4 品种当日收盘价注入 -X% 冲击，计算策略在该日的组合净值损失。B2 注入逻辑：事后计算，取该品种满仓 4 单位 × 连续 3 日跌停的累计不可抗损失。

#### 5.9.2 模块架构

```
scripts/run_stress_test.py
├── define_scenarios()            # 定义 A1-A4 + B1-B2 场景参数
├── load_best_params()            # 从 S6 best_params.json 加载最优参数
├── run_historical_scenario()     # 历史情景回放
├── run_synthetic_shock()         # B1: 合成单月同步暴跌
├── run_liquidity_stress()        # B2: 连续跌停不可抗损失
├── generate_report()             # 生成 Markdown 压力测试报告
├── save_results()                # 写 CSV + JSON
└── main() / argparse CLI         # CLI 入口
```

#### 5.9.3 核心接口

**`define_scenarios()`** → `dict`

返回 A1-A4 + B1-B2 共 6 个场景的完整定义参数（start_date, end_date, description, type）。

**`run_historical_scenario(scenario, params)`** → `dict`

在指定历史区间运行 Backtrader 回测（复用 S6 的 Cerebro 组装 + `TurtleStrategy`），返回含场景标签的指标字典：

```python
{
    "scenario": str,           # "A1_covid"
    "date_range": str,
    "total_return": float,     "cagr": float,
    "sharpe": float | None,    "max_drawdown": float,
    "max_dd_duration": int,    # 最大回撤持续天数
    "daily_var_95": float,     # 95% 置信度 VaR（历史模拟法）
    "daily_var_99": float,     # 99% VaR
    "total_trades": int,       "win_rate": float,
    "t1_stop_delay_hits": int, # T+1 止损延迟触发次数
    "correlation_avg": float,  # 区间内平均两两相关系数
    "final_value": float,
}
```

参数默认取自 S6 结果 `results/grid_search/best_params.json` 中排名第一的组合。

**`run_synthetic_shock(params, shock_pcts)`** → `pd.DataFrame`

在基准区间（默认 2022 年）每月第一个交易日注入同步暴跌，遍历 3%/5%/7% 三个冲击幅度。

**`run_liquidity_stress(params)`** → `dict`

事后计算：`总不可抗损失 = 满仓 4 单位 × 名义金额 × [1 - (1-0.10)^3]`。

**`generate_report(all_results)`** → `str`

生成包含通过/不通过判定的 Markdown 报告。判定标准：

| 指标 | 压力情景通过线 |
|:--|:--|
| 最大回撤 | ≤ 25% |
| 最大回撤持续时间 | ≤ 60 个交易日 |
| 99% 日 VaR | ≤ 5% |
| 月度最大亏损 | ≤ 15% |
| 连续止损暂停保护 | 至少触发 1 次 |

**综合判定**：全部 5 项 → ✅ 通过；1-2 项不达标 → ⚠️ 条件通过（需 Dry-Run 验证）；3+ 项不达标 → ❌ 不通过（需重新设计风控）。

#### 5.9.4 CLI 接口

```
用法: py scripts/run_stress_test.py [选项]

选项:
  --params PATH      最优参数 JSON (默认: results/grid_search/best_params.json)
  --scenarios {A1,A2,A3,A4,B1,B2,all}  运行场景 (默认: all)
  --output PATH      输出目录 (默认: results/stress_test/)
  --mode {A,B}       回测模式 (默认: A)
  --workers N        并行进程数 (默认: 4)
  --verbose          详细日志
```

#### 5.9.5 输出产物

| 产物 | 路径 | 说明 |
|:--|:--|:--|
| **压力测试报告** | `results/stress_test/stress_report.md` | Markdown 完整报告 |
| **场景汇总 CSV** | `results/stress_test/scenario_summary.csv` | 所有场景横向对比表 |
| **历史情景详情** | `results/stress_test/historical_{scenario}.csv` | 逐日净值序列 |
| **合成冲击结果** | `results/stress_test/synthetic_shock.csv` | B1 冲击矩阵 |
| **通过/失败结论** | `results/stress_test/stress_conclusion.json` | 结构化结论 |

### 5.10 滚动相关性监控 — 独立模块

为满足 §3.6「滚动相关性监控」要求，设计独立脚本 `scripts/run_correlation_monitor.py`。

#### 5.10.1 模块职责

```
scripts/run_correlation_monitor.py
├── compute_rolling_correlation()   # 60 日滚动两两相关系数，返回时间序列
├── detect_correlation_events()     # 检测平均相关系数 > 0.6 的预警区间
├── plot_correlation_timeseries()   # 绘制滚动相关性折线图（--plot）
├── generate_report()               # 生成 Markdown + CSV
└── main() / argparse CLI
```

#### 5.10.2 核心接口

**`compute_rolling_correlation(period, window=60)`** → `pd.DataFrame`

列：`date, avg_corr, max_corr, min_corr, over_threshold(bool), pair_count`

**`detect_correlation_events(df, threshold=0.6)`** → `pd.DataFrame`

输出相关系数突破阈值的起始/结束日期、持续天数、峰值。

#### 5.10.3 CLI 接口

```
用法: py scripts/run_correlation_monitor.py [选项]

选项:
  --start DATE        起始日期 (默认: 2020-01-01)
  --end DATE          截止日期 (默认: 2026-06-10)
  --window N          滚动窗口天数 (默认: 60)
  --threshold FLOAT   预警阈值 (默认: 0.6)
  --output PATH       输出目录 (默认: results/stress_test/)
  --plot              生成折线图
  --verbose           详细日志
```

---

### 5.11 市场状态判断（V5.7 新增）

**`src/market_regime.py`** — 实盘可用的每日市场状态判断器。

#### 设计目标

识别市场处于趋势市还是碎步市，为策略切换提供依据。所有指标均无 look-ahead，每日收盘后可立即计算。

#### 三子指标

| 指标 | 含义 | 计算方式 |
|:--|:--|:--|
| `n_pct` | N 值(ATR20)历史分位 | 当日 N 在过去 252 天中的分位 |
| `eff_20d` | 方向效率 | 近 20 日 `|净位移| / Σ|Δclose|` |
| `n_trend` | N 值趋势 | 近 60 日 N 值线性回归斜率 |

#### 融合公式

```
score = 0.4 × n_pct + 0.4 × eff_20d + 0.2 × max(n_trend_sign, 0)
```

| 状态 | score 阈值 | 含义 |
|:--|:--|:--|
| `trending` | > 0.60 | 趋势市，适合海龟 |
| `transitional` | 0.35–0.60 | 过渡态 |
| `choppy` | < 0.35 | 碎步市 |

权重和阈值均可通过 `MarketRegime.__init__` 参数调整。

#### API

```python
from src.market_regime import MarketRegime
regime = MarketRegime()
for date, close, high, low in daily_data:
    state = regime.update(date, close, high, low)
    print(regime.score, regime.state)
```

#### 历史验证

| 年份 | score | 趋势% | 碎步% | 策略收益 |
|:---:|:-----:|:----:|:-----:|:-------:|
| 2021 | 0.234 | 3.7% | 77.5% | −20.47% |
| 2022 | 0.305 | 8.4% | 65.6% | +49.08% |
| 2023 | 0.246 | 2.1% | 75.3% | −11.99% |
| 2024 | 0.430 | 19.7% | 35.2% | −8.10% |
| 2025 | 0.380 | 13.1% | 46.6% | +14.98% |
| 2026 | 0.492 | 23.1% | 18.2% | +9.21% |

Score vs 策略收益斯皮尔曼相关系数：0.49。最优分割阈值 0.30（<0.30 年均 −16.23%，>0.30 年均 +16.29%，差距 +32.52%）。

> **已知局限**：2022（碎步率高但赚大钱）和 2024（趋势倾向但亏钱）说明方向效率窗口（20日）对极端行情的灵敏度不足，权重和窗口可后续优化。

---

### 5.12 S10 品种筛选脚本 — 施工图设计（V5.11 新增）

基于 §2.2「品种筛选量化框架」定义的 8 道硬门槛，本节给出 `scripts/screen_candidates.py` 的完整架构，将筛选流程从人工检查转为可复现的自动化脚本。

#### 5.12.1 筛选流程

```
候选列表 symbol_list
   │
   ├─[1] 数据质量     ← 复用 cross_validate.validate_symbol()
   │     └─ worst_level == "critical" → ❌ REJECT
   │
   ├─[2] 上市年限     ← 读 parquet 第一条日期
   │     └─ 首日 > 2018-01-01 → ❌ REJECT
   │
   ├─[3] 流动性检查   ← 读 parquet volume 列
   │     └─ 近252日均量 < 2亿 → ⚠️ WARN
   │
   ├─[4] 趋势持续性   ← Hurst 指数（R/S 法）
   │     └─ 252 日滚动 H 中位数 < 0.50 → ⚠️ WARN
   │
   ├─[5] 独立海龟回测 ← 复用 run_backtest 管线（单品种）
   │     └─ 盈亏比 < 1.0 或 CAGR ≤ 0 → ❌ REJECT
   │
   ├─[6] 相关性 vs 现有组合 ← 复用 correlation_monitor
   │     └─ 与任一现有品种的 60 日平均 ρ > 0.5 → ⚠️ WARN
   │
   ├─[7] T+1 占比     ← config_loader.get_t_plus_one_symbols()
   │     └─ 加入后 T+1 占比 > 50% → ⚠️ WARN
   │
   └─[8] 输出报告 JSON + 控制台表格
```

核心原则：**任一 REJECT 即淘汰**，不进入后续检查；WARN 不阻断但记录。第⑥道（空白象限）和第⑧道（组合边际贡献）为人工决策，脚本输出"建议进入组合回测"的 PASS 品种供人工执行⑧。

#### 5.12.2 模块架构

```
scripts/screen_candidates.py
├── CandidateResult dataclass       # 单一候选的完整检查结果
├── ScreeningReport dataclass       # 全量报告容器
├── check_data_quality()            # [1] → SingleCheck
├── check_listing_age()             # [2] → SingleCheck
├── check_liquidity()               # [3] → SingleCheck
├── check_trend_persistence()       # [4] → SingleCheck
├── check_standalone_backtest()     # [5] → SingleCheck
├── check_correlation()             # [6] → SingleCheck
├── check_t1_ratio()                # [7] → SingleCheck
├── screen_candidate()              # 单品种全流程
├── screen_all()                    # 批量筛查
├── print_summary()                 # 控制台表格输出
├── export_report()                 # JSON 写入
└── main() / argparse CLI

src/turtle_core.py (新增)
└── hurst_exponent()                # R/S 法，纯 numpy
```

#### 5.12.3 数据类设计

```python
class CheckVerdict(Enum):
    PASS = "pass"; WARN = "warn"; REJECT = "reject"; SKIP = "skip"

@dataclass
class SingleCheck:
    stage: str              # "data_quality" | "listing_age" | ...
    verdict: CheckVerdict
    metric_name: str        # 核心指标名
    metric_value: float|str # 实际值
    threshold: str          # 阈值描述
    detail: str             # 人类可读的解释
    elapsed_sec: float = 0.0

@dataclass
class CandidateResult:
    symbol: str
    name: str
    final_verdict: CheckVerdict
    checks: list[SingleCheck] = field(default_factory=list)
    stopped_at_stage: str = ""

@dataclass
class ScreeningReport:
    timestamp: str
    existing_universe: list[str]
    candidates: list[CandidateResult]
    summary: dict
```

#### 5.12.4 各检查函数接口

| 函数 | 输入 | 逻辑 |
|:--|:--|:--|
| `check_data_quality(symbol)` | symbol | `import cross_validate.validate_symbol()`；`worst_level=="critical"` → REJECT |
| `check_listing_age(symbol)` | symbol, min_date="2018-01-01" | 读 parquet 首日日期；首日 > min_date → REJECT |
| `check_liquidity(symbol)` | symbol, min_vol=2e8 | 近 252 日 volume 均值；< min_vol → WARN |
| `check_trend_persistence(symbol)` | symbol, min_hurst=0.50 | 调用 `hurst_exponent()`；H < 0.50 → WARN |
| `check_standalone_backtest(symbol)` | symbol, start, end | 调用 `run_backtest(symbols=[symbol])`；盈亏比 < 1.0 或 CAGR ≤ 0 → REJECT |
| `check_correlation(symbol, existing)` | symbol, existing_symbols | 复用 correlation_monitor 的 60 日滚动 ρ；任一 > 0.5 → WARN |
| `check_t1_ratio(symbol, existing)` | symbol, existing_symbols | 从 config_loader 读 T+1 集合；计算加入后 T+1 占比；> 50% → WARN |

#### 5.12.5 Hurst 指数（放入 `src/turtle_core.py`）

```python
def hurst_exponent(price: np.ndarray, max_lag: int = 100) -> float:
    """重标极差法 (R/S) 计算 Hurst 指数。

    H > 0.55 → 趋势持续；H ≈ 0.5 → 随机游走；H < 0.45 → 均值回归。
    """
    returns = np.diff(np.log(price))
    n = len(returns)
    if n < max_lag:
        max_lag = max(10, n // 4)
    lags = range(2, min(max_lag, n // 2))
    rs_values = []
    for lag in lags:
        n_chunks = n // lag
        if n_chunks < 2:
            break
        chunks = returns[:n_chunks * lag].reshape(n_chunks, lag)
        mean = chunks.mean(axis=1, keepdims=True)
        Z = (chunks - mean).cumsum(axis=1)
        R = Z.max(axis=1) - Z.min(axis=1)
        S = chunks.std(axis=1, ddof=1)
        S[S == 0] = 1e-10
        rs_values.append((R / S).mean())
    if len(rs_values) < 4:
        return 0.5
    log_lags = np.log(list(range(2, 2 + len(rs_values))))
    H = np.polyfit(log_lags, np.log(np.array(rs_values)), 1)[0]
    return max(0.0, min(1.0, float(H)))
```

#### 5.12.6 CLI 接口

```
用法: py scripts/screen_candidates.py [选项]

--symbols, -s SYMBOLS    逗号分隔候选品种代码
                          默认: 自动发现 data/etf_daily/ 中非组合内的品种
--existing SYMBOLS       现有组合品种（默认从 config 读取）
--start, --end DATE      回测区间 (默认 2020-01-01 ~ 2026-06-10)
--skip-backtest          跳过第5步（快速模式）
--output PATH            输出 JSON (默认 results/screening_report.json)
--verbose, -v            详细日志
```

#### 5.12.7 输出产物

| 产物 | 路径 | 说明 |
|:--|:--|:--|
| 筛查报告 | `results/screening_report.json` | 完整结构，含每个候选的 7 道检查明细 |
| 控制台 | stdout | 表格摘要：品种 \| 结论 \| 卡在哪道 \| 关键指标 |

#### 5.12.8 对现有文件的改动清单

| 文件 | 改动 | 行数 |
|:--|:--|:--|
| `scripts/run_backtest.py` | 新增 `--symbols` 参数；将核心回测逻辑从 `main()` 抽为可 import 的函数 | ~25 |
| `src/turtle_core.py` | 新增 `hurst_exponent()` | ~40 |
| `scripts/screen_candidates.py` | **新建**，全模块 | ~280 |
| `scripts/screen_candidates.py` | 新增 `check_t1_ratio()` | ~25 |
| `tests/test_screening.py` | **新建**，覆盖各检查函数 + Hurst | ~60 |

#### 5.12.9 依赖关系

```
screen_candidates.py
  ├── import scripts.cross_validate (validate_symbol)
  ├── import scripts.run_backtest (需先重构解耦)
  ├── import scripts.run_correlation_monitor
  ├── import src.turtle_core (hurst_exponent)
  └── pandas, numpy, yaml, argparse, dataclasses
```

#### 5.12.10 风险与缓解

| 风险 | 缓解 |
|:--|:--|
| `cross_validate` 依赖 TickFlow（网络） | try/except，网络不可用时标记 SKIP |
| Hurst 对短样本不稳定 | 样本 < 252 日 → SKIP |
| 单品种回测慢（~3 秒/品种） | `--skip-backtest` 跳过；也可按数据质量→上市年限先过滤后再回测 |
| `run_correlation_monitor` 有模块级变量 | import 前 patch 或改为函数参数传入价格矩阵 |

#### 5.12.11 未来扩展

- **模式B（快速向量化）**：不经过 Backtrader，用 `TurtleSignals.precompute_all()` 直接算信号，0.5 秒/品种初筛
- **组合边际贡献自动化**（第⑧道）：自动跑 N vs N+1 回测对比
- **定期再筛查**：每季度对所有数据目录中的品种自动跑一次

---

## 六、风险控制体系

### 6.1 品种级止损（被动风控）

- 10 日反向突破止损（多单跌破 10 日低点 / 空单涨破 10 日高点）。此为**唯一**退出规则（方案A 已删除 2N 追尾止损和移动止损逻辑）。详见 §4.2。

### 6.2 组合级风控（主动风控）

| 风控层级 | 触发条件 | 执行动作 |
|:--|:--|:--|
| **仓位集中度熔断** | 持仓品种数 ≥3 时渐进降级：3只→80%、4只→60%、≥5只→50%（风险倍数） | 降低新开仓位的风险敞口（仅限新开单位） |
| **最大回撤预警** | 组合净值 5 日内回撤 > 10% | 暂停新开仓，仅处理已有头寸的止损 |
| **连续亏损暂停** | 连续亏损 8 次 或累计亏损 > 总资金 15% | 暂停交易 1 周，强制执行市场状态评估 |

### 6.3 极端行情预案

| 情景 | 应对措施 |
|:--|:--|
| 多资产同步暴跌（相关性飙升） | 暂停新开多头，仅保留盈利头寸或全部清仓观望 |
| 单一品种流动性枯竭（连续跌停无法卖出） | 启用等权重替代品种作为过渡 |
| 宏观经济重大变化（QDII额度/汇率管制变动） | 暂停跨境 ETF 开仓，待明确后再评估 |

---

## 七、工程项目隔离

本策略的代码实现为**独立项目 `turtle_v2/`**，与 `automated_trading/` 物理隔离。

### 7.1 不共享的文件

`turtle_v2/` 的所有文件（包括代码、配置、数据、结果）均独立存储，与 `automated_trading/` 无任何文件共享。

### 7.2 核心逻辑来源

`src/turtle_core.py` 从 `automated_trading/src/strategy_engine.py` 中提取 ATR 计算、唐奇安通道、止损、加仓逻辑，**独立复制**到 `turtle_v2/` 项目中。两份代码后续独立演进，互不影响。

### 7.3 文件结构与依赖关系

```
config/turtle_config.yaml        # 参数配置（独立于 automated_trading/config/）
data/etf_daily/                  # 数据缓存（独立于 automated_trading/data/）
    ↑
src/turtle_core.py               # ATR/唐奇安/止损/加仓（从 strategy_engine.py 提取）
src/risk_parity.py               # Ledoit-Wolf + α风险平价
src/data_pipeline.py             # Tushare 数据拉取
src/benchmarks.py                # 四种基准对比
src/market_regime.py             # 市场状态判断器（三指标融合）
    ↑
strategies/turtle_trading.py     # Backtrader Strategy 子类
    ↑
scripts/run_backtest.py          # 回测入口
scripts/cross_validate.py        # TickFlow <-> Tushare 数据交叉校验
scripts/run_grid_search.py       # 参数网格搜索
scripts/run_stress_test.py       # 极端情景回测
scripts/run_comparison.py        # 基准对比
scripts/analyze_n_percentile.py  # N 值分位历史验证
    ↑
results/                         # 回测输出
```

---

## 八、Dry-Run 模拟盘规范

### 8.1 周期与资金

| 参数 | 推荐值 |
|:--|:--|
| 模拟周期 | **3-6 个月** |
| 模拟资金 | **20 万元** |

### 8.2 Dry-Run 阶段必须完成的核心验证

1. **数据源可用性**：Tushare 是否可以持续稳定提供全部 4 只 ETF 的日线数据。
2. **信号逻辑一致性**：模拟盘信号生成是否与回测结果一致。
3. **真实滑点测量**：日均买卖价差与冲击成本是否在 0.2% 以内。
4. **网络与系统稳定性**：每日自动化运行是否稳定，通知机制是否正常。
5. **国债ETF现金管理验证**：空仓期资金转入/转出国债ETF 是否按规则执行。
6. **T+1 品种止损滑点测量**：T+1 品种因当日无法止损导致的额外滑点是否在可控范围内。

### 8.3 Dry-Run 通过标准

- 信号生成准确率：与回测引擎的逐笔对比一致率 ≥ 99%。
- 冲击成本：单笔 ≥ 0.2% 的交易占比 < 10%。
- 系统可用率：交易日自动化运行成功率 ≥ 95%。

---

## 九、实施路线图

| 阶段 | 任务 | 周期 | 交付物 | 状态 |
|:--|:--|:--|:--|:--:|
| **S0** | 项目骨架搭建 | 即时 | 项目结构 + 管控模型 + 配置 | ✅ |
| **S1** | 数据管道 | 0.5天 | src/data_pipeline.py + scripts/pull_data.py | ✅ 含后复权 |
| **S2** | 海龟核心移植 | 1天 | src/turtle_core.py（从 strategy_engine.py 提取） | ✅ |
| **S3** | Backtrader 策略层 | 1天 | strategies/turtle_trading.py + scripts/run_backtest.py | ✅ |
| **S4** | 风险平价权重 | 1天 | src/risk_parity.py | ✅ |
| **S5** | 四种基准对比 | 0.5天 | src/benchmarks.py + scripts/run_comparison.py | ✅ |
| **S6** | 参数网格搜索 | 0.5天 | scripts/run_grid_search.py | ✅ |
| **S7** | 极端情景回测 | 0.5天 | scripts/run_stress_test.py + scripts/run_correlation_monitor.py | ✅ |
| **S8** | 综合报告 + 测试 | 1天 | scripts/gen_report.py + tests/ 覆盖 | ✅ |
| **S9** | Dry-Run 准备 | 后续 | 信号校验脚本 | ⏳ |
| **S10** | 品种筛选脚本 | 0.5天 | `scripts/screen_candidates.py` + `hurst_exponent()` + `tests/test_screening.py` | ✅ 已完成 |

---

## 十、附录

### A. 项目文档索引

| 文件 | 内容 |
|:--|:--|
| `docs/strategy_design_v3.0.md` | 本文件 — 策略全量设计（当前版本 V3.7） |
| `docs/governance_model.md` | 项目管控模型 |
| `docs/analysis/t+0_t+1_impact.md` | T+0/T+1 结算规则差异对策略的完整影响分析 |
| `docs/MarkdownMcp 配置手册（AI 助手专用）.md` | MarkdownMcp 安装与配置指南 |
| `CHANGELOG.md` | 版本变更记录 |
| `config/turtle_config.yaml` | 回测参数配置 |

### B. 已知风险汇总

| # | 风险 | 可能性 | 影响 | 应对 |
|:--|:--|:--|:--|:--|
| 1 | 极端行情下所有资产相关性同步上升 | 中 | 🔴 高 | 仓位集中度熔断 + 暂停新开仓 |
| 2 | 海龟策略在长期震荡市中持续亏损 | 中 | 🟡 中 | 连续亏损暂停机制；55日过滤对比 |
| 3 | QDII 额度限制/纳指ETF溢价过高 | 中 | 🟡 中 | 溢价>3%暂不开仓，资金转入黄金/国债 |
| 4 | 策略因过拟合在样本外失效 | 低 | 🔴 高 | 样本内外分割 + 滚动窗口 + Dry-Run |
| 5 | ETF 流动性变化导致冲击成本不可控 | 低 | 🟡 中 | Dry-Run 中实时监控，动态调整权重 |
| 6 | **T+1 品种止损滞后**：A 股 ETF 当日买入后无法在当日止损，导致实际止损滑点大于回测假设 | **高** | 🟡 中 | 回测时强制修正 T+1 止损延迟；Dry-Run 实测滑点；关注 A 股 T+0 ETF 试点进展 |

### C. Core Module — 需从 strategy_engine.py 提取的函数

| 函数 | 源文件位置 | 用途 |
|:--|:--|:--|
| `calc_tr()` | strategy_engine.py ~L100 | 计算真实波幅 |
| `calc_atr()` | strategy_engine.py ~L130 | ATR(20) 指数平滑 |
| `donchian_channel()` | strategy_engine.py ~L170 | 唐奇安通道（20/55入场，10/20退出） |
| `calc_position_size()` | strategy_engine.py ~L250 | 仓位公式（账户×1%/N） |
| `check_stop_loss()` | strategy_engine.py ~L330 | 2N止损 + 10日反向突破 |
| `pyramid_add()` | strategy_engine.py ~L420 | 每0.5N加仓1单位，最多4单位 |

---

*本文件是策略设计的唯一权威来源。管控模型请参见 `docs/governance_model.md`。*

---

### 5.13 期货版（V5.16 新增）

**定位**：ETF 纯多头的辅助仓，趋势跟踪 + 双向交易 + 天然杠杆。与 ETF 版低相关性（<0.3），组合后可提升夏普。

### 品种（4 品种，2026-06-23 定型）

| code | 名称 | 板块 | 乘数 | 合约价值(约) | 保证金/手(约) |
|:--|:--|:--|:--:|--:|--:|
| M.DCE | 豆粕 | 农产品 | 10 | ¥29,570 | ~¥4,400 |
| CF.ZCE | 棉花 | 农产品 | 5 | ¥78,375 | ~¥11,800 |
| RB.SHF | 螺纹钢 | 黑色 | 10 | ¥31,550 | ~¥4,700 |
| TA.ZCE | PTA | 能化 | 5 | ¥31,510 | ~¥4,700 |

**选品原则**（四维筛选）：① 保证金 1 手 ≤ 资本 10%；② 年化波动率 ≥ 15%；③ Hurst 中位数 > 0.50；④ 板块不重叠、品种间相关性不冗余。

**排除逻辑**：沪铜 CU（1手保证金 ¥79,050，占 10万资本 79%）、原油 SC（¥76,530，占 77%）、白糖 SR（年化波动 13.3% < 15%）、豆油 Y（与棕榈 P 相关性 ρ=0.79）。

### 与 ETF 版的关键差异

| 项目 | ETF 版 | 期货版 |
|:--|:--|:--|
| 方向 | 纯多头 | 双向（做多 + 做空） |
| 初始资金 | ¥120,000 | ¥100,000 |
| risk_per_unit | 0.01 | **0.035** |
| single_max_risk | 0.04 | **0.10** |
| max_portfolio_risk | 0.20 | **0.35** |
| max_consecutive_losses | 8 | **12** |
| pause_days | 5 | **3** |
| 过滤器 | SignalFilter 开 | **全关** |
| 执行模式 | `close` 次日成交 | `cheat_on_close` 收盘价成交 |
| 最小单位 | 100 股 | 1 手 |

### 回测结果（2020-01 ~ 2026-06）

| 指标 | 值 |
|:--|:--:|
| 总收益 | 24.81% |
| CAGR | ~3.5% |
| MDD | 14.97% |
| 胜率 | 25% |
| 盈亏比 | 3.22 |
| 交易次数 | 188 |

### 已知局限

- 商品期货无长期向上漂移（不像股票有股权溢价），CAGR 天然低于 ETF 版
- 日线框架下不适用均值回归策略（布林带+RSI 在日线级别 6.5 年仅触发 4 笔信号）
- 选品基于 10 万资本，资金扩大后需重新评估品种容量
