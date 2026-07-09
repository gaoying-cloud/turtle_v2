# 实验: 自适应退出机制 — 持仓时长路径

## 元数据
- 提出: 2026-07-08（初始）；2026-07-09（v2~v4 多轮迭代）
- 分支: —
- 状态: ❌ 已归档（自适应退出各方案均未跑赢入口过滤基线）

---

## 实验方向变更说明

本实验初始验证的是 **MarketRegime 路径**（外部市场状态 → 切换出场参数），已测试失败。详见下方 "历史记录" 节。

**当前实验方向已切换为持仓时长路径**——不依赖外部市场状态判断，而是根据持仓已持续天数动态调整出场规则。该路径源于诊断数据的实证发现（详见下方"持仓时长分析"节）。

---

## 假设

### 主假设：持仓时长 → 动态退出
持仓天数本身就是一个极强的"趋势存在"代理指标。持仓第 1-10 天时胜率极低（假突破期），持仓超过 20 天后胜率接近 100%（趋势确认期）。据此：

> **持仓≥20天后放宽止损，让利润奔跑。**

**v3 方案（2026-07-09 修订）**：ATR 移动止损替代 Donchian 低点宽松档。
`stop_low_12` 在上涨趋势中会"遗忘"——12天前的低点随着价格上涨越来越远，止损位跟不上价格。
改用 `high_since_entry - 2.5N` 的 ATR 移动止损，棘轮上移锁定利润。

| 持仓阶段 | 止损方式 | 说明 |
|:--|:--|:--|
| 0-19 天 | **stop_low_10**（标准，同基线） | 不收紧，避免砍掉趋势早期 |
| 20+ 天 | **ATR 移动止损**（high_since_entry - 2.5N） | 棘轮上移，自适应波动，锁定利润 |

### 辅助假设：低波动 → 入口过滤（atr_percentile_252）
诊断数据的 Mann-Whitney 检验显示 `atr_percentile_252` 显著区分盈亏（effect=0.30, p=0.003）：

| 组别 | ATR 分位数中位数 | 含义 |
|:--|:--:|:--|
| 盈利交易 | 0.29（低波动期） | 弹簧压紧 → 突破趋势可信 |
| 亏损交易 | 0.53（中等波动期） | 市场已活跃 → 假突破概率高 |

> **低波动期入场，高波动期不入场，减少假突破损耗。**

辅助过滤与主假设作用在不同环节，无冲突：
- 入口：`atr_percentile_252 > 0.7 → 不入场`
- 出口：根据 `holding_days` 切换 `stop_period`

---

## 基线（当前 main）

| 指标 | 值 |
|:--|:--:|
| 最终净值 | ¥568,489 |
| 总收益率 | +468.49% |
| 夏普比率 | 0.88 |
| 最大回撤 | 18.24% |
| 胜率 | 46.29% |
| 盈亏比 | 3.44 |
| 平均持仓 | 16.7 天 |
| 交易次数 | 175 |

**回测参数**：ATR=25, Breakout=20, Stop=8, 1.5xATR, alpha=0, 6 ETF 品种

---

## 成功标准

### 主标准（持仓时长退出）
| 指标 | 基线 | 目标 | 说明 |
|:--|:--:|:--:|:--|
| 夏普 | 0.88 | ≥ 0.88 | 不恶化 |
| 最大回撤 | 18.24% | ≤ 16% | 降回撤（主要目标） |
| CAGR | ~14.7% | ≥ 13% | 可接受小幅下降（降回撤的代价） |

### 辅助标准（入口过滤）
| 指标 | 基线 | 目标 | 说明 |
|:--|:--:|:--:|:--|
| 胜率 | 46.29% | ≥ 50% | 减少假突破 |
| 交易次数 | 175 | ≥ 140 | 过滤不应过度减少机会 |
| CAGR | ~14.7% | ≥ 14% | 入口过滤不应明显损害收益 |

### 扩展目标（组合效果）
- 持仓时长退出 + atr_percentile 入口过滤同时启用时，效果是否 1+1 > 2？

---

## 实验设计

### Phase 1：持仓时长退出（独立验证）

修改 `strategies/turtle_trading.py` 的 `_should_exit()` 方法：

```python
# 当前代码（一行固定 stop_period）：
stop_low = self.signals[sym]["stop_low_10"][0]

# 改为 ATR 移动止损：
hold = pos.holding_days
if hold >= 20:
    n = si["n"].iloc[idx]
    if pd.notna(n) and n > 0:
        new_stop = calc_trailing_stop(
            trail_price=pos.high_since_entry,
            n_value=n,
            prev_stop=pos.trailing_stop,    # 棘轮：只上移不下移
            stop_mult=2.5,                  # 2.5N 给已确认趋势留呼吸空间
            direction="long",
        )
        pos.trailing_stop = new_stop
        stop_low = new_stop
    else:
        stop_low = si["stop_low_10"].iloc[idx]
else:
    stop_low = si["stop_low_10"].iloc[idx]
```

**前置修改**：在 `Position` 类中增加 `trailing_stop: float = 0.0` 字段。

**参数**：`adaptive_exit_mult = 2.5`（可通过参数调整宽松程度）。

### Phase 2：入口过滤（独立验证）

修改 `strategies/turtle_trading.py` 的 `_check_entry()`，在入场条件中加入：

```python
# 获取 atr_percentile_252
n_arr = self.signals[sym].get("n_series", None)  # 需预存 ATR 序列
if n_arr is not None and idx >= 252:
    n_window = n_arr[idx-252:idx]
    n_pct = (n_val - n_window.min()) / (n_window.max() - n_window.min() + 1e-10)
    if n_pct > 0.7:  # 高波动期，不进场
        return
```

### Phase 3：组合验证

同时启用 Phase 1 + Phase 2，看组合效果。

### 实验流程

```bash
# 1. 开分支
py scripts/experiment.py start S13_adaptive_exit

# 2. Phase 1 改代码 → 回测 → 检查
# 3. Phase 2 改代码 → 回测 → 检查  
# 4. Phase 3 组合 → 回测 → 检查
# 5. 通过则合并，失败则记录
```

---

## 持仓时长分析（实验依据）

诊断数据（144 笔交易，6 品种，2020-2026）显示：

| 持仓天数 | 交易数 | 总盈亏 | 胜率 |
|:--:|:--:|:--:|:--:|
| 1-5d | 18 | -27,496 | 6.0% |
| 6-10d | 48 | -73,563 | 8.0% |
| 11-20d | 41 | +123,326 | 54.0% |
| 21-30d | 21 | +295,596 | 95.0% |
| 31-50d | 13 | +315,370 | 100.0% |
| 51-100d | 3 | +105,020 | 100.0% |

Mann-Whitney U 检验中 `holding_days` 是区分盈亏最强的特征（effect = -0.86, p < 0.001），盈利中位数 23 天 vs 亏损中位数 8 天。详见 [`diagnostic_report.md`](../../results/diagnostics/diagnostic_report.md)。

**核心逻辑**：持仓时长本质上是"价格维持在 10 日通道下轨之上的连续天数"。持仓 < 10 天的交易几乎全是亏损，持仓 > 20 天的交易几乎全是盈利。这是一个 **入场后才知道的实时信号**，天然适合作为动态退出的依据。

---

## 参考
- [`diagnostic_report.md`](../../results/diagnostics/diagnostic_report.md) — 当前诊断基线
- [`S15_upper_band_ratio.md`](S15_upper_band_ratio.md) — UBR 入场前信号验证（失败）
- [`_archive/ideas/adaptive_exit.md`](../../_archive/ideas/adaptive_exit.md) — 初始提案（MarketRegime 路径）

---

## 历史记录

### 原方案：MarketRegime 路径
原假设：趋势向上时宽容出场（stop_period=10），非趋势时严格出场（stop_period=6）。

**A/B 测试（2026-07-08）**：
- CAGR: 14.05% → 8.32%（-5.74pp）
- 夏普: 1.04 → 0.60
- 终值: ¥517k → ¥271k

**结论**：❌ 路径失败。`regime_filter` 入口拦截显著劣化。虽然出口切换未测试，但 MarketRegime 子指标本身精度不够，增量空间有限。

### 相关实验：UBR 入场前信号
`S15_upper_band_ratio` 测试了"入场前 10 日上轨占比"作为市场状态信号的可行性，结果不具有预测力（ROC-AUC ≈ 0.49）。该结论不影响本实验的**持仓时长**路径——持仓时长是入场后的实时信号，性质不同。

---

## 最终结论（2026-07-09）

经过多轮迭代（S13 → S16 → S13v2 stop_low_12 → v3 ATR移动止损 → v4 百分比跟踪止损），自适应退出各方案总结：

| 方案 | 净值 vs 基线 | 夏普 | MDD | 结论 |
|:--|:--:|:--:|:--:|:--:|
| stop_low_12（v2） | **+23%** | 0.80 | 18.25% 🔴 | 净值最高但MDD翻倍 |
| ATR 3.0N（v3） | -13% | 0.78 | 13.57% | 不如图利 |
| 跟踪5%（v4） | -12% | 0.88 | 13.59% | 勉强但不优 |
| 跟踪8%+（v4） | ❌崩溃 | — | — | 止损失效 |

**结论**：持仓天数自适应退出各方案均不如单独使用 ATR 百分位入口过滤。当前基线已定型：
- ✅ 入口：`atr_pct_filter: true, atr_pct_threshold: 0.75`（S17）
- ✅ 出口：`stop_low_10`（不变）
- ❌ 自适应退出：关闭（`use_adaptive_exit: false`）

实验归档。
