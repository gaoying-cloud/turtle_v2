# Turtle_trading.py 策略核心逻辑审查报告

**审查范围**：`strategies/turtle_trading.py`（629行）+ `src/turtle_core.py` 全部核心函数  
**审查方法**：逐行审查信号生成、止损、仓位管理、边界条件四大模块  
**审查日期**：2026-06-16

---

## 🔴 严重缺陷（2项，需立即修复）

### 缺陷 #1：累计亏损百分比永不复位 → 新开仓被永久冻结

**位置**：`turtle_trading.py` line 464 & line 373

**根因**：
```
_execute_exit()  →  self._cumulative_loss_pct += abs(pnl) / self._equity()   ← 只增不减
_enter_pause()   →  仅重置 self._consecutive_losses = 0                      ← 从未重置 cumulative
_check_entry()   →  if self._cumulative_loss_pct >= max_cumulative_loss_pct: return   ← 永久阻断
```

`_cumulative_loss_pct` 是一个从回测启动到结束的**单向累加器**。海龟策略胜率约 40-50%，亏损交易必然不断累积百分比，最终突破 15% 阈值。一旦触发，所有 `_check_entry()` 调用都会在 line 373 被拦截，**此后永远无法再开新仓**。回测收益将被严重低估。

**设计文档要求**（§6.2）：连续亏损暂停应为临时性措施，暂停期结束应恢复交易。

**修复方案**：在 `_enter_pause()` 中增加 `self._cumulative_loss_pct = 0.0`，或在 `next()` 暂停期结束恢复时（line 281）重置。

---

### 缺陷 #2：移动止损首次激活后冻结 → 止损线不再上移

**位置**：`turtle_trading.py` line 531-533

**根因**：
```python
def _update_trailing_stop(self, code, pos):
    if pos.stop_type == "trailing" and pos.trail_high > 0:
        return  # ← 第一天激活后，此后每一天都直接返回！
    
    # 下面的更新逻辑只在第一天执行一次
    trail_high = si["trail_high_10"].iloc[idx]
    self._positions.update_trail_high(code, trail_high)
    new_stop = calc_trailing_stop(trail_high, n, pos.stop_loss)
    self._positions.update_stop_loss(code, new_stop, "trailing")
```

**逻辑追踪**：
| 天数 | stop_type | trail_high | 条件判断 | 是否更新止损 |
|:--|:--|:--|:--|:--|
| Day T (入场) | `"fixed"` | 0 | `stop_type != "trailing"` → 通过 | ✅ 首次计算 |
| Day T+1 | `"trailing"` | N.xxx | **两者条件都满足 → 返回！** | ❌ 冻结 |
| Day T+2 ~ T+N | `"trailing"` | N.xxx | 同上 | ❌ 永远冻结 |

**影响**：海龟策略的核心优势是移动止损随价格上涨逐步上移以保护利润。当前实现将止损线冻结在第一天计算的值，实际效果等同于固定止损。在趋势行情中会过早被震出，严重降低盈亏比和收益率。

**修复方案**：删除提前返回逻辑（line 531-533），让每次 `_update_trailing_stop` 都重新计算。底层的 `calc_trailing_stop` 已内置 `max(raw, prev)` 保护，天然保证止损线只上移不下移。

---

## 🟡 中等问题（3项）

### 缺陷 #3：出场盈亏百分比使用变动分母

**位置**：`turtle_trading.py` line 464  
**内容**：`self._cumulative_loss_pct += abs(pnl) / self._equity()`  
**问题**：分母 `self._equity()` 随时变化，同样金额的亏损在不同时间贡献不同的百分比。设计文档 §6.2 表述"累计亏损 > 总资金 15%"，语义偏向固定基准（初始资金）。  
**建议**：明确选择固定基准（初始资金）或变动基准（当前净值），并在文档中标注。

### 缺陷 #4：`_enter_pause` 运行时动态导入

**位置**：`turtle_trading.py` line 594  
**内容**：`__import__("datetime").timedelta(days=pause_days)`  
**问题**：顶部已导入 `from datetime import datetime, date`，唯独遗漏 `timedelta`。应在顶部补充导入。  
**修复**：顶部增加 `from datetime import timedelta`，line 594 改为 `datetime.timedelta(days=pause_days)`。

### 缺陷 #5：同日新建仓位后的出场检查被跳过

**位置**：`turtle_trading.py` line 300-317  
**内容**：`next()` 中的循环分支——`if not has_position → _check_entry → 流程结束`，不会继续对该品种执行 `_should_exit`。  
**影响**：对于 T+0 品种（纳指、黄金），理论上存在"入场价即当日最高，收盘价已跌破止损"的极端情形，但因分支结构无法当日止损。  
**建议**：这是一个日线 bar 粒度的架构局限（无法模拟日内反转），不是 bug，但需在设计文档中标注。

---

## 🟢 低等/信息类发现（4项）

| # | 内容 | 说明 |
|:--|:--|:--|
| 6 | **入场信号三重价格不一致**：突破判断用 `data.high[0]`，仓位计算用 `data.close[0]`，Backtrader 实际成交用次日 open | 波动大时仓位规模偏差 1-5%，Dry-Run 阶段实测 |
| 7 | **SignalFilter 中 `has_position` 参数防御性冗余** | 策略层已通过 `has_position()` 分流，不影响正确性 |
| 8 | **T+1 止损延迟** | 设计文档已知风险 #6，代码处理正确（line 387 / line 432） |
| 9 | **国债切换与入场信号同 bar 执行** | `_check_entry` 买入后立即在 `_bond_switch` 卖出，次日同时执行；需确认 cerebro 配置容错 |

---

## 逐模块评估

### 信号生成（入场/加仓/离场）

| 检查项 | 状态 | 备注 |
|:--|:--:|:--|
| 20日突破入场 | ✅ | `donchian_high` 使用 `shift(1)` 排除当日，正确 |
| 55日过滤 | ✅ | 可选择启用/禁用 |
| 加仓触发价计算 | ✅ | `calc_pyramid_trigger` 公式正确：`base_price + units × 0.5N` |
| 加仓上限 4 单位 | ✅ | `pyramid_add` 和 `_check_pyramid` 双重检查 |
| 10日反向突破出场 | ✅ | `donchian_low` 使用 `shift(1)`，正确 |
| **信号闪烁** | ✅ 无 | 所有通道值不含当日数据 |
| **信号漏发** | ✅ 无 | 突破判断逻辑正确，无漏发 |

### 止损逻辑

| 检查项 | 状态 | 备注 |
|:--|:--:|:--|
| 固定止损（2N） | ✅ | `calc_fixed_stop` 正确 |
| 移动止损公式 | ✅ | `calc_trailing_stop` 公式正确，含 NaN 保护 |
| **止损每日更新** | 🔴 | **缺陷 #2：移动止损首次激活后冻结** |
| **止损不执行** | 🟡 | 不执行 bug 不存在，但止损线更新 bug 使其执行不到位 |

### 仓位管理

| 检查项 | 状态 | 备注 |
|:--|:--:|:--|
| N值计算（ATR指数平滑）| ✅ | `compute_atr` 初始为简单平均，后续指数平滑，正确 |
| 仓位公式 `equity × risk_pct / N` | ✅ | 100股整数倍取整 |
| 仓位集中度熔断（≥4品种 → 风险减半）| ✅ | line 364 |
| **累计亏损暂停后恢复** | 🔴 | **缺陷 #1：永久阻断新开仓** |
| 风险平价 α 权重注入 | ✅ | `compute_alpha_weights` 集成正确 |

### 边界条件

| 检查项 | 状态 | 备注 |
|:--|:--:|:--|
| T+1 品种当日买入不可卖出 | ✅ | `_buy_today` 标记正确 |
| T+1 品种当日买入不可同日止损 | ✅ | line 432 检查 `_buy_today` |
| T+0 品种可完整循环 | ✅ | 无 `_buy_today` 拦截 |
| 国债ETF 现金管理 | ✅ | 空仓→买入/有信号→卖出 |
| 除权除息 | ⚠️ | 依赖 Tushare 后复权数据，未单独验证 |
| 涨跌停 | ⚠️ | 无显式处理，Backtrader 默认行为 |

---

## 修复优先级

| 优先级 | 缺陷 | 影响 | 预计修复工作量 |
|:--:|:--|:--|:--:|
| **P0** | #1 累计亏损永不复位 | 回测收益被严重低估 | 1 行代码 |
| **P0** | #2 移动止损冻结 | 趋势行情中过早退出，降低盈亏比 | 删除 1 个 if 块 |
| P1 | #4 运行时动态导入 | 编码规范 | 1 行修改 |
| P2 | #3 盈亏分母语义 | 精确度 | 文档 + 可选代码调整 |
| P3 | #5 同日入场后出场 | 极端情形（文档标注即可） | 0 行 |

两项 P0 缺陷修复后，预期回测绩效将显著改善——特别是移动止损修复后，趋势跟踪策略的利润保护能力将从"几乎不存在"恢复到完整功能。

## 最终修复方案确认

审查报告已获认可，汇总用户决策后的待修复项如下：

### 代码修复（4项）

| # | 文件 | 修复内容 | 行 |
|:--|:--|:--|:--|
| P0-1 | `strategies/turtle_trading.py` | `_enter_pause()` 中增加 `self._cumulative_loss_pct = 0.0` | +1 行 |
| P0-2 | `strategies/turtle_trading.py` | `_update_trailing_stop()` 删除提前返回逻辑（line 531-533） | -3 行 |
| P1-4 | `strategies/turtle_trading.py` | 顶部 `from datetime import datetime, date` → 增加 `timedelta`；line 594 去掉 `__import__` | 改 2 行 |
| #3 | `strategies/turtle_trading.py` | `self._cumulative_loss_pct += abs(pnl) / self._equity()` 改为除以 `self.broker.startingcash`，固定基准 | 改 1 行 |

### 文档更新（1项）

| # | 文件 | 内容 |
|:--|:--|:--|
| #5 | `docs/strategy_design_v3.0.md` | §5.7.2 补充说明：日线 bar 粒度下同日入场后无法在同 bar 执行出场检查，属架构局限非 bug |

### 修复后预期

- **P0-1**：暂停期结束后恢复新开仓能力
- **P0-2**：移动止损每日随价格上涨上移，趋势利润得到完整保护
- **P1-4**：消除运行时动态导入
- **#3**：盈亏百分比使用固定分母（初始资金），语义与设计文档一致

全部修复通过后，运行 `py -m pytest tests/test_turtle_strategy.py -q` 验证无回归。

---

请切换到 **ACT MODE** 以执行上述修复。