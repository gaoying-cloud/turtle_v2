# 实验: N字结构策略完善路线图

## 元数据
- 提出: 2026-07-11
- 分支: 待创建（分阶段独立分支）
- 状态: 🔄 进行中（Phase 0 ✅ 已完成）
- 前置: S23_S22_n_structure_tune（N字参数调优定型）

## 背景

S22 将 N字结构策略的参数调优定型（CAGR 18.5%, 957 笔交易, 6 品种全部盈利），但代码审查发现了若干影响回测可信度的结构性问题。本路线图将问题按优先级分阶段推进，**每个阶段独立可验证、可合入 main**。

### 当前基线（S22 定型参数）

| 参数 | 值 |
|:--|:--:|
| window_size | 100 |
| atr_period | 25 |
| stop_mult | 1.5 |
| trail_mult | 5.0 |
| add_step | 2.0 |
| max_units | 6 |
| ma_trend | 0 (关闭) |
| use_ma5_confirm | False |
| max_reentries | 0 |

### 当前基线表现（注意：数字含有已知偏差）

| 指标 | 值 | 备注 |
|:--|:--:|:--|
| 平均 CAGR | 18.5% | ⚠️ 识别人为 optimistic |
| 平均 MDD | 8.6% | ⚠️ 按交易序列计算，非日频 |
| 总交易笔数 | 957 | 无摩擦成本 |
| 平均胜率 | 47.0% | — |

---

## 问题清单与分阶段计划

### 阶段总览

```
Phase 0: 诚实基线  ──→  修复信息泄露 + 日频净值 + 摩擦成本
         ↓                    产出：可信的基准数字
Phase 1: 风控补全  ──→  动态权益 + 总敞口 + 熔断 + 滑点
         ↓                    产出：实盘可用的风控体系
Phase 2: 组合回测  ──→  共享资金池 + 相关性约束 + 组合净值
         ↓                    产出：组合层面业绩归因
Phase 3: 策略简化  ──→  退出逻辑降维 + 参数稳健性检验
         ↓                    产出：更少参数、更稳健的策略
Phase 4: 工程化    ──→  测试 + 实盘信号 + 架构文档
         ↓                    产出：可上线运行的完整系统
Phase 5: 策略深化  ──→  与海龟融合 + 品种特化 + 多时间框架
                             产出：策略体系升级
```

---

## Phase 0: 诚实基线 — 修复回测可信度

### 实验: S24_p0_honest_baseline

**核心目标**：得到一组可信的 N字策略基准数字。

#### 子任务

##### 0.1 修复 N字结构识别中的未来信息泄露 🔴 P0

**问题**：`find_n_structure_in_window` 使用窗口内全局 `idxmin`/`idxmax` 确定 A/D/B 点，这三个点的识别依赖完整窗口数据，在实盘中无法复现。

**方案**：改为"分段增量确认"模式

```
旧逻辑（有事後信息）:
  A = window.low.idxmin()           ← 看过整个窗口才知道最低点
  D = after(A).high.idxmax()        ← 看过 A 之后全部数据
  B = after(D).low.idxmin()         ← 同上

新逻辑（实时可确认）:
  1. A点: 在窗口中找局部低点，要求:
     - 左右各 N 根 K 线未创新低（局部极小值）
     - A 出现后至少 M 根 K 线不再刷新低点（确认延迟）
  2. D点: A 之后找局部高点，要求:
     - D > A × (1 + min_advance%)  （有意义的反弹）
     - 左右各 N 根 K 线未创新高
  3. B点: D 之后找局部低点，要求:
     - A < B < D                    （更高低点，N字核心）
     - B 出现后至少 K 根 K 线未跌破（确认延迟）
     - B < D × 0.95                （显著低于 D，回调充分）
```

**改动文件**：`strategies/n_structure.py` — `find_n_structure_in_window()`

**成功标准**：
- 新识别逻辑能找出与旧逻辑 ~70%+ 重叠的有效结构（说明不是完全不同的策略）
- 新逻辑识别的结构在视觉上合理（人工抽查 10 个案例）
- 增加 `_find_local_min` / `_find_local_max` 辅助函数并测试

##### 0.2 日频净值曲线 🔴 P0

**问题**：`compute_metrics` 基于交易序列 PnL 计算 Sharpe/MDD，不是日频。

**方案**：在 `NStructureStrategy.run()` 中维护日频权益数组

```python
# run() 返回值改为 (df_result, trades, equity_curve)
# equity_curve: pd.Series, index=date, values=当日总权益

# 每日权益 = 现金 + 持仓市值
# 持仓市值 = units × shares_per_unit × close[i]
```

**改动文件**：
- `strategies/n_structure.py` — `run()` 增加日频权益追踪
- `scripts/run_n_structure.py` — `compute_metrics()` 用日频收益率重算 Sharpe/MDD

**成功标准**：
- 返回的日频 Sharpe 与旧版（交易序列）的偏差不超过 ±0.3（经验值）
- 日频 MDD 应该 ≥ 旧版 MDD（因为捕捉了持仓期间的盘中回撤）
- `compute_metrics` 新增 `daily_returns` 参数，优先使用日频数据

##### 0.3 摩擦成本模型 🔴 P1

**问题**：N字策略以精确开盘价成交，无滑点、无手续费。

**方案**：参考海龟配置，加入两个参数

```python
slippage_pct: float = 0.001    # 0.1% 滑点（买卖各一次）
commission_pct: float = 0.00015 # 0.015% 手续费

# 进场成本价 = open × (1 + slippage_pct)
# 出场成本价 = exit_price × (1 - slippage_pct)  # 卖出时市价偏低
# 每笔交易额外扣 commission
```

**改动文件**：
- `strategies/n_structure.py` — `run()` 中所有成交价加入滑点
- `scripts/run_n_structure.py` — CLI 新增 `--slippage` `--commission` 参数

**成功标准**：
- 加入 0.1% 滑点 + 0.015% 手续费后，CAGR 下降 ≤ 3pp（超过 3pp 说明策略太脆弱）
- 全部品种仍然盈利（只有 1-2 个亏损可接受）

##### 0.4 诚实基线重跑

全部三个修复合并后，重跑全区间（2014-2026），产出一份**可信的基准报告**。

**成功标准**：
- 6 品种全部跑通，无报错
- 全部品种盈利（或最多 1 个微亏）
- 产出对比表格：旧基线 vs 诚实基线，逐项标注变化来源

---

## Phase 1: 风控补全 — 向实盘靠拢

### 实验: S25_p1_risk_controls

**前置**：Phase 0 完成，诚实基线确立。

#### 子任务

##### 1.1 动态权益仓位计算

**问题**：`capital_per_symbol` 固定为 `initial_capital / num_symbols`，不随权益变化。

**方案**：每笔交易用当前组合权益计算仓位

```python
# 当前权益 = 现金 + 所有品种持仓市值之和
# 不再用固定 capital_per_symbol
# shares = calc_shares(equity=current_total_equity / active_positions_count, ...)
```

##### 1.2 总敞口上限

**问题**：无组合层面的仓位控制，可能同时满仓 6 品种。

**方案**：
```python
max_total_exposure: float = 1.5  # 总敞口 ≤ 150% 权益
# 新开仓前检查: (现有持仓市值 + 拟开仓市值) / 总权益 ≤ max_total_exposure
```

##### 1.3 连续亏损熔断

**问题**：无连续亏损保护。

**方案**：参考海龟 `SignalFilter` 和 `pause_days`
```python
max_consecutive_losses: int = 5   # 连续 5 笔亏损 → 暂停
pause_bars: int = 20              # 暂停 20 根 K 线
```

##### 1.4 加仓重算风险

**问题**：D 点突破加仓时用初始 ATR 和初始价格计算股数。

**方案**：加仓时用当前价格和当前 ATR 重算单位风险。

##### 成功标准（整体 Phase 1）
- 风控参数可配置，默认值合理
- 加入全部风控后 CAGR 不降超过 2pp（证明风控没有"杀"掉策略）
- MDD 在诚实基线基础上再降 1-2pp

---

## Phase 2: 组合回测 — 从单品种到组合

### 实验: S26_p2_portfolio_backtest

**前置**：Phase 1 完成。

#### 子任务

##### 2.1 共享资金池回测

**问题**：6 品种各自独立跑，互不知对方持仓。

**方案**：构建组合回测引擎 `run_portfolio()`

```python
def run_portfolio(self, dfs: dict[str, pd.DataFrame]) -> PortfolioResult:
    """多品种共享资金池回测。

    每日遍历所有品种，按信号优先级分配资金。
    - 资金先到先得，或按信号质量排序分配
    - 总敞口约束在组合层面检查
    """
```

**关键设计决策**：
- 信号优先级：信号同日触发时如何分配有限资金？
  - 方案 A：按信号质量（D 点距离 / ATR 大小）排序
  - 方案 B：等权分配可用资金
  - 方案 C：保持各品种独立资金池，但加总敞口检查

##### 2.2 组合净值曲线与归因

- 输出组合层面的日频净值
- 输出品种级收益贡献（哪个品种贡献了最多利润）
- 输出相关性矩阵及时间序列

##### 2.3 与海龟组合的对比

用同样的 6 品种、同一时段、同样的初始资金，对比 N字组合 vs 海龟组合的：
- 净值曲线（叠加图）
- 逐年收益
- 最大回撤区间

##### 成功标准（整体 Phase 2）
- 组合回测跑通，6 品种共享资金池
- 组合 Sharpe ≥ 单品种平均 Sharpe × 0.8（组合层面分散化应有改善）
- 组合 MDD ≤ 单品种平均 MDD

---

## Phase 3: 策略简化 — 降维 + 稳健性

### 实验: S27_p3_simplify

**前置**：Phase 2 完成。

#### 子任务

##### 3.1 退出逻辑降维

**问题**：5 种退出原因（初始止损 / 跟踪止损 / B点结构失效 / D点突破失败 / D点超时），自由度太大。

**方案**：简化为 2 种退出
1. **结构止损**：跌破 B 点或初始止损（取更高者）
2. **跟踪止损**：突破 D 点后启用 trail_mult × ATR

删除：D点突破失败（5K超时）、B点结构失效（合并到结构止损）

##### 3.2 参数稳健性扫描

**方案**：对简化后的策略做全面参数敏感性分析

```python
# 扫描参数：
#   window_size: [60, 80, 100, 120, 150, 180]
#   stop_mult: [1.0, 1.5, 2.0, 2.5, 3.0]
#   trail_mult: [3.0, 4.0, 5.0, 6.0, 8.0]
#   max_units: [3, 4, 5, 6]

# 验证方式：
#   - 滚动窗口 OOS（3年训练 / 2年验证，滚动 4 次）
#   - 逐品种参数稳健性（最优参数在不同品种上的一致性）
```

##### 3.3 过拟合检验

- 参数平原测试（最优参数附近的平坦程度）
- 随机参数 vs 最优参数的差异显著性
- OOS 区间从 1 段扩展到 3 段（2014-2015, 2016-2017, 2018-2019）

##### 成功标准（整体 Phase 3）
- 简化后策略 CAGR ≥ 诚实基线的 90%（允许小幅下降换取稳健性）
- 退出原因从 5 种减到 2-3 种
- 最优参数在 ±20% 范围内 CAGR 变化 ≤ 3pp（参数平原）
- 3 段 OOS 全部盈利

---

## Phase 4: 工程化 — 测试 + 实盘 + 文档

### 实验: S28_p4_engineering

**前置**：Phase 3 完成，策略逻辑定型。

#### 子任务

##### 4.1 测试覆盖

```python
# tests/test_n_structure.py

# 形态识别测试
test_find_local_min_basic()
test_find_local_max_basic()
test_find_n_structure_standard_case()    # 标准N字
test_find_n_structure_no_structure()     # 无结构时返回 None
test_find_n_structure_below_a()          # B < A 时 is_valid() = False
test_find_n_structure_insufficient_data() # 窗口数据不足

# 策略逻辑测试
test_entry_signal_on_breakout()          # 突破 B 进场
test_no_entry_below_b()                  # 价格未突破 B 不进场
test_stop_loss_triggered()               # 止损触发
test_d_breakout_add_position()           # D 突破加仓
test_b_structure_failure()               # 结构失效平仓

# 指标测试
test_compute_metrics_empty_trades()
test_compute_metrics_with_daily_equity() # 日频 Sharpe/MDD
test_sharpe_calculation_accuracy()       # 与手动计算对比

# 组合回测测试
test_portfolio_capital_allocation()
test_portfolio_exposure_limit()
```

##### 4.2 实盘信号生成

```python
# scripts/daily_signal_n_structure.py

# 功能对齐海龟的 daily_signal.py：
#   - compute_signals()     → 每日扫描 6 品种的 N字结构
#   - should_enter()        → 检查入场条件
#   - check_exit()          → 检查退出条件
#   - should_add()          → 检查加仓条件
#   - calc_shares()         → 计算仓位
#   - state.json            → 持仓状态持久化
```

##### 4.3 架构文档整合

- `docs/architecture.md` 加入 N字策略章节
- README 项目结构中列出 N字策略文件
- 明确海龟 vs N字的定位和关系

##### 成功标准（整体 Phase 4）
- 测试 ≥ 15 个，全部通过
- `daily_signal_n_structure.py` 生成的信号与回测逻辑一致（用同一段数据对比）
- 架构文档更新完成

---

## Phase 5: 策略深化 — 融合与演进

### 实验: S29_p5_advanced

**前置**：Phase 4 完成，N字策略已可独立实盘运行。

#### 研究方向（不做硬性成功标准，属于探索性实验）

##### 5.1 N字 + 海龟策略融合

三种可能的融合方式：
1. **信号共振**：海龟 20 日突破 AND N字结构完成 → 加大仓位（1.5×）
2. **资金分配**：60% 海龟 + 40% N字，各跑各的，组合层面分散化
3. **市场状态切换**：趋势市跑海龟（突破追入），震荡市跑 N字（回调买入），用 MarketRegime 判断

##### 5.2 品种特化参数

中证500 的 N字结构特征 vs 黄金ETF 可能差异很大：
- 股票ETF：趋势强、结构清晰 → 可以更紧的参数
- 商品ETF：震荡多、假突破多 → 需要更宽的确认窗口

为每个品种独立优化参数，对比统一参数的效果。

##### 5.3 多时间框架

日线找 N字结构，小时线找精确入场：
- 日线确定 A/D/B 结构
- 当价格接近 B 点突破位时，切到小时线
- 小时线上出现放量突破 → 精确入场
- 预期效果：提高入场精度，减少假突破

##### 5.4 做空对称性研究

- N字结构的空头版本：倒 N字（A高→D低→B更低高→向下跌破进场）
- 参考 S19 的教训：单独评估空头期望值
- 如果空头 +EV 但组合变差，研究原因并设计方案

---

## 成功标准总览

| Phase | 关键指标 | 门槛 |
|:--|:--|:--:|
| 0 — 诚实基线 | 修复后 CAGR | ≥ 12%（允许显著低于 18.5%） |
| 0 — 诚实基线 | 日频 MDD | ≥ 旧版 MDD（说明捕获了真实回撤） |
| 1 — 风控补全 | 加入风控后 CAGR | ≥ Phase 0 的 90% |
| 1 — 风控补全 | 风控后 MDD | ≤ Phase 0 的 80% |
| 2 — 组合回测 | 组合 Sharpe | ≥ 1.0（组合分散化效应） |
| 2 — 组合回测 | 全部品种盈利 | ✅ |
| 3 — 策略简化 | 参数数量 | ≤ 5 个核心参数 |
| 3 — 策略简化 | 3 段 OOS 全盈利 | ✅ |
| 4 — 工程化 | 测试覆盖 | ≥ 15 个测试通过 |
| 4 — 工程化 | 实盘信号 | 与回测逻辑一致 |
| 5 — 策略深化 | 探索性实验 | 无硬性指标 |

---

## 执行说明

### 分支策略

每个 Phase 独立分支，Phase 内可以再分子实验：

```
exp/S24_p0_honest_baseline    # Phase 0
exp/S25_p1_risk_controls      # Phase 1
exp/S26_p2_portfolio          # Phase 2
exp/S27_p3_simplify           # Phase 3
exp/S28_p4_engineering        # Phase 4
exp/S29_p5_advanced           # Phase 5
```

### 合并策略

每个 Phase 完成后 `merge --squash` 到 main，不积压。如果某个 Phase 的实验结果推翻了前提假设（例如诚实基线显示 CAGR 其实只有 5%），则重新评估后续 Phase 是否继续。

### 回退条件

- Phase 0 修复后 CAGR < 8%：N字策略核心逻辑可能不成立，考虑搁置
- Phase 3 简化后 CAGR < Phase 0 的 70%：简化方案太激进，保留原逻辑
- 任意 Phase 导致 2 个以上品种亏损：暂停，分析原因

---

## 参考

- [[S21_n_structure_density]] — N字信号密度优化实验
- [[S22_S21_n_structure_density]] — 参数调优实验
- [[S23_S22_n_structure_tune]] — 当前基线，含已知风控缺陷
- [[S19_w_bottom_entry]] — W底实验，结构识别思路可借鉴
- `docs/architecture.md` — 海龟策略架构（风控体系参考）
- `src/turtle_core.py` — SignalFilter / 仓位计算（可复用模块）
