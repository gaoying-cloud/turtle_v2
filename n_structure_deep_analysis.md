# N字结构策略深度分析报告

**日期**: 2026-07-12  
**版本**: S40 (当前 HEAD: `c735681`)  
**方法**: 4 路并行 Agent 深度审查 — 参数同步 / 扫描覆盖 / 死代码 / 性能分析

---

## 一、总览

| 维度 | 结论 |
|:--|:--|
| 策略质量 | 核心逻辑设计精良，"先紧后松"出场理念正确 |
| 参数同步 | 🔴 **实盘信号脚本严重落后回测** |
| 扫描覆盖 | 🔴 **仅覆盖 6/42 参数，S39/S40 完全不可扫描** |
| 数据泄露 | 🔴 **S39 在 OOS 上调参，9.4% CAGR 被高估** |
| 代码卫生 | 🟡 死代码残留 + 4 个脚本存在运行时 bug |

---

## 二、🔴 严重问题

### 2.1 数据泄露：S39 在 OOS 数据上调参

S39 的两个核心调优提交直接在 commit message 中引用 OOS 结果：

```
cc920c3 S39: 加仓步长 2.0→1.5，OOS CAGR 8.1%→9.4%
1203ffb S39: 滑动窗口 100→60，OOS CAGR 7.1%→8.1%
```

BASELINE.md 明确声明 IS=2014-2020（训练）、OOS=2020-2026（纯净验证），但调参过程违背了这一分割。

**影响**: BASELINE 记录的 OOS CAGR 9.4% 不可信。按 S34 的干净基线（6.8%），S39 的 2.6pp 改善中有多少是真实提升、多少是 OOS 过拟合，无法区分。

**S40 同样可疑**: max_units 6→4、d_timeout 40→7 等改动发生在 S39 之后，同样基于 OOS 结果。

### 2.2 实盘信号脚本与回测严重不同步

`scripts/daily_signal_n_structure.py` 冻结在 S28/S30 时代：

| 类别 | 数量 | 示例 |
|:--|:--|:--|
| 参数值不同 | 7 个 | `window_size=100`(应为60), `d_timeout=40`(应为7), `ma_trend=0`(应为50) |
| 参数完全缺失 | 14 个 | `add_weights`, `trail_pre_d`, `use_ma_exit`, `entry_confirm_bars` 等 |
| 入场逻辑缺失 | 3 项 | 无 `entry_confirm_bars`、无 S38 质量过滤 |
| 出场逻辑不同 | 全套 | D 突破后仍用旧版 ATR 跟踪止损，非 MA20 趋势出场 |
| 加仓逻辑不同 | 全套 | 无 `add_weights` 加权，固定单位 |

**这意味着：实盘信号和回测产生的是两套完全不同的交易！** `daily_signal_n_structure.py` 中声称"所有信号逻辑与回测完全一致"的注释已失效。

### 2.3 参数扫描覆盖严重不足

`scripts/scan_n_structure.py` 的 BASELINE 和 SCAN_RANGES 停留在 S22 时代：

- **BASELINE**: 15 个参数中 5 个值与策略默认值不同（`window_size=100` vs 60, `d_timeout=40` vs 7 等）
- **SCAN_RANGES**: 仅 6 个轴，S39/S40 核心参数**全部不可扫描**：
  - ❌ `trail_pre_d` (S39 核心)
  - ❌ `use_ma_exit` (S39 核心)
  - ❌ `ma_exit_margin` (S39)
  - ❌ `ma_exit_bearish` (S39)
  - ❌ `entry_confirm_bars` (S37)
  - ❌ `ma_trend` (S37)
  - ❌ `confirm_k` (S37)
  - ❌ `d_timeout_days` (S40)
  - ❌ `add_weights` (S40)

---

## 三、🟡 代码卫生问题

### 3.1 死代码：旧版出场路径（`n_structure.py:968-985`）

S39 commit message 明确说"删除旧三段式 ATR 跟踪止损"，但代码只是推到 `else` 分支。由于默认 `use_ma_exit=True`，此分支**从未被执行**。

### 3.2 运行时 Bug：4 个脚本传递不存在的参数

以下脚本向 `NStructureStrategy()` 传递了不存在的 `profit_protect_mult=15`：

| 脚本 | 行号 | 后果 |
|:--|:--|:--|
| `scripts/combined_backtest.py` | 48 | `TypeError` — 脚本无法运行 |
| `scripts/run_combined.py` | 55 | `TypeError` |
| `scripts/correlation_analysis.py` | 42 | `TypeError` |
| `scripts/compare_strategies.py` | 45 | `TypeError` |

此外 `combined_backtest.py:104` 引用 `pos.profit_protected`（`PositionState` 中不存在的属性），会造成二次崩溃。

### 3.3 `run_portfolio` 与 `run` 的入口逻辑重复

`run_portfolio()` 独立重写了入场扫描逻辑（~100 行），缺少 `_check_entry_from_prev` 中的 3 项过滤：
- S38 质量过滤 (`max_ad_advance`/`max_ab_advance`)
- MA5 确认 (`use_ma5_confirm`)
- 进场确认延迟 (`entry_confirm_bars`)

每次向 `_check_entry_from_prev` 新增过滤器，都需要手动同步到 `run_portfolio`，是维护陷阱。

### 3.4 模块文档字符串过时

`n_structure.py` 头部参数表仍写 `ma_trend: int = 0`（实际 50），`window_size: int = 100`（实际 60），缺少所有 S24-S40 参数。

---

## 四、性能深度分析

### 4.1 出场归因

| 出场原因 | 笔数 | 胜率 | 总盈亏 | 均盈 | 均持 |
|:--|:--:|:--:|:--|:--|:--|
| MA20 出场 | 89 | 36.0% | +68,871 | +774 | 54天 |
| 初始止损 | 33 | **0.0%** | -6,878 | -208 | 13天 |
| D 点地板 | 2 | 0.0% | -734 | -367 | 18天 |

**关键发现**: 初始止损 100% 亏损。这 33 笔交易全部在突破 D 点前被止损，说明入场时的 N 字结构是假突破。如果能在入场前过滤掉其中的 10 笔即可提升 +2,080 净盈亏。

### 4.2 赢家 vs 输家持仓天数

赢家均值 101 天 vs 输家 27 天。MA20 出场机制有效区分了真实趋势和假突破——趋势成立时让利润奔跑，趋势失败时快速止损。

### 4.3 IS vs OOS

| 指标 | IS (2014-2020) | OOS (2020-2026) | 变化 |
|:--|:--|:--|:--|
| CAGR | 11.2% | 9.4% | -1.8pp |
| 胜率 | 24.7% | 31.0% | +6.3pp |
| MDD | 28.2% | 27.0% | -1.2pp |

胜率在 OOS 反而提升，说明 IS 期间 A 股为主的震荡市对策略更不友好，OOS 期间黄金/纳指/豆粕的趋势更持续。CAGR 衰减 16% 在可接受范围——**但因为 S39/S40 在 OOS 上调参，这些数字不可信**。

### 4.4 品种间极端分化

OOS 中黄金 16.6% CAGR vs 中证500 仅 3.8%。策略无法为趋势弱的品种创造 alpha——它只能捕捉已经存在的趋势。这不是缺陷，但说明品种选择（或品种级仓位管理）对最终收益影响巨大。

---

## 五、问题优先级总览

| # | 严重度 | 问题 | 影响 |
|:--|:--|:--|:--|
| 1 | 🔴 P0 | S39/S40 在 OOS 上调参 | BASELINE 9.4% CAGR 不可信 |
| 2 | 🔴 P0 | 实盘信号脚本与回测不同步 | 实盘产生错误信号 |
| 3 | 🔴 P1 | 扫描网格缺失 S39/S40 参数 | 无法对新参数做网格搜索 |
| 4 | 🔴 P1 | 4 个脚本 `profit_protect_mult` TypeError | 脚本无法运行 |
| 5 | 🟡 P2 | 旧版出场死代码 (968-985行) | 代码膨胀 |
| 6 | 🟡 P2 | `run_portfolio` 入口逻辑重复 | 维护陷阱 |
| 7 | 🟡 P2 | 文档字符串过时 | 误导读者 |
| 8 | 🟡 P2 | 初始止损 0% 胜率 | 业绩拖累(33笔全亏) |

---

## 六、建议行动

1. **立即**: 修复 4 个脚本的 `profit_protect_mult` TypeError（删除参数即可）
2. **立即**: 删除旧版出场死代码（968-985 行）
3. **高优先**: 重新做 S39 参数的干净 IS/OOS 验证，产出可信基线
4. **高优先**: 将实盘信号脚本同步到 S40 参数和逻辑
5. **高优先**: 更新扫描脚本的 BASELINE + SCAN_RANGES
6. **中优先**: 重构 `run_portfolio` 复用 `_check_entry_from_prev`
7. **中优先**: 研究入场过滤方案（降低初始止损率）

---

*报告完成。*
