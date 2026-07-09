# 实验: 10 日上轨占比 — 市场状态信号及其预测力

## 元数据
- 提出: 2026-07-09
- 分支: —
- 状态: 📦 待验证

## 假设

### 核心命题
在 N 天内，收盘价站上 10 日唐奇安通道上轨的占比（简称 **UBR_N**，Upper Band Ratio），可以反映市场趋势状态的质量，并预测入场后的胜率。

### 直觉推导
从 [`S13_adaptive_exit.md`](S13_adaptive_exit.md) 的持仓时长分析出发：

```
持仓时长（holding_days）≈ 出场后已知的"趋势持续天数"
                          ↓ (向前追溯到入场前)
10 日上轨占比（UBR_N）  ≈ 入场前已知的"短线动能密度"
```

持仓时长之所以是极强的盈亏区分特征（effect = -0.91），是因为它捕捉了"价格在 10 日通道上下轨之间的持续存活能力"。而 UBR 作为入场前就可计算的指标，有可能提前捕捉同样的信号——**如果价格在过去 N 天中频繁站上 10 日上轨，说明近期上涨动能充足，入场后趋势延续的概率更高**。

### 需要验证的问题

| # | 问题 | 验证方式 |
|:--|:--|:--|
| Q1 | UBR_N 在不同市场状态下是否有显著差异？（趋势市 vs 震荡市 vs 下跌市） | 按已知趋势期/震荡期回测 UBR 均值 |
| Q2 | UBR_N 在入场日的高低，是否能区分后续交易的盈亏？ | Mann-Whitney U 检验（同 holding_days） |
| Q3 | UBR_N 的最佳回溯窗口 N 是多少？（5/10/20/40/60 天？） | 遍历 N，取区分度最高的窗口 |
| Q4 | UBR_N 与 holding_days 的相关性有多强？是替代关系还是互补关系？ | Spearman 秩相关 + 双变量回归 |
| Q5 | UBR_N 能否作为自适应退出的入场前预判信号？持仓第 X 天后更新 UBR 能否提高预测力？ | 多时间点 UBR 滚动计算 |

### 与 S13 的区别
- **S13 自适应退出**：用的是 **入场后** 的 `holding_days`（后验信号），动态调整出场规则
- **S15 本实验**：用的是 **入场前** 的 `UBR_N`（先验信号），判断市场状态并可能用于入场过滤/仓位大小/出场参数预设

两者互补：UBR 决定"要不要进/进多少"，holding_days 决定"什么时候出"。

---

## 成功标准

### 主标准（UBR 的区分力）
- **Q2 Mann-Whitney 检验**：`UBR_N` 的 |effect| ≥ 0.5（与 holding_days 的 0.91 对比，了解其相对强度）
- **Q2 统计显著**：p < 0.05
- **Q3 最佳窗口**：存在至少一个 N 使得 UBR_N 的区分力显著优于随机（effect > 0.3）

### 预测力标准
- **预测准确率**：UBR_N 高于某个阈值时，入场后盈利的概率 ≥ 65%（高于整体胜率 60%）
- **预测 AUC**：用 UBR_N 做入场盈亏预测的 ROC-AUC ≥ 0.65

### 补充标准
- **Q4 相关性**：与 holding_days 的 Spearman ρ < 0.8（若过高则说明是冗余指标）

---

## 实验设计

### 方法一：U 检验复现（最简，可复用在现有诊断框架中）

**步骤**：
1. 在 `run_trade_diagnostics.py` 的 `compute_entry_features()` 中，为每笔交易计算入场日的 `UBR_20`（过去 20 天中收盘价 ≥ 10 日通道上轨的天数比例）
2. 将 `UBR_20` 加入 Mann-Whitney U 检验的特征列表
3. 比较其 effect size 与其他 10 个特征

**数据来源**：已有 84 笔交易 + 4 品种 + 2014-2026 日线，无需新增回测。

**预期输出**：
| Feature | Win Median | Loss Median | p-value | Effect | Sig? |
|:--|:--:|:--:|:--:|:--:|:--:|
| holding_days | 33.0 | 12.0 | 0.0 | -0.91 | Y |
| **UBR_20** | ? | ? | ? | ? | ? |

### 方法二：预测力评估（在方法一基础上扩展）

**步骤**：
1. 将 UBR_20 按阈值分桶（如 0-0.2, 0.2-0.4, 0.4-0.6, 0.6-0.8, 0.8-1.0）
2. 统计每桶的胜率、平均盈亏、盈亏比
3. 计算 ROC-AUC（以 UBR 为 score，was_win 为 label）
4. 确定最优阈值（通过最大化 F1-score 或最小化信息损失）

**输出示例**：
| UBR_20 区间 | 交易数 | 胜率 | 平均盈亏 | 盈亏比 |
|:--:|:--:|:--:|:--:|:--:|
| 0-0.2 | ? | ?% | ? | ? |
| 0.2-0.4 | ? | ?% | ? | ? |
| 0.4-0.6 | ? | ?% | ? | ? |
| 0.6-0.8 | ? | ?% | ? | ? |
| 0.8-1.0 | ? | ?% | ? | ? |

### 方法三：N 窗口遍历（找最优回溯期）

**步骤**：
1. 对 N ∈ {5, 10, 20, 30, 40, 60}，分别计算 UBR_N
2. 对每个 N 重复方法一的 U 检验
3. 比较各 N 的 effect size，取最大值对应的窗口

**预期输出**：
| N | effect | p-value | Sig? |
|:--:|:--:|:--:|:--:|
| 5 | ? | ? | ? |
| 10 | ? | ? | ? |
| 20 | ? | ? | ? |
| 30 | ? | ? | ? |
| 40 | ? | ? | ? |
| 60 | ? | ? | ? |

可能的结果模式：
- **短期 (N=5)**：噪音大，effect 低
- **中期 (N=20)**：最佳平衡点，effect 最高
- **长期 (N=60)**：过于平滑，滞后性强，区分力下降

### 方法四：关键阈值——极端场景的预测价值

**步骤**：
对几个特殊阈值做条件概率分析：
- `UBR_20 = 0`（过去 20 天从未站上 10 日上轨）→ 入场后的胜率？
- `UBR_20 = 1`（过去 20 天每天都站上 10 日上轨）→ 入场后的胜率？
- 这两个极端情况是否比中间值有更强的预测力？

**假设**：极端值（0 或 1）比中间值有更强信号，因为"绝对弱势"或"绝对强势"本身就是一种稳定状态。

### 方法五：入场后滚动 UBR（与 holding_days 的相互作用）

**步骤**：
对每笔交易，不仅计算入场日的 UBR_20，还计算：
- 持仓第 5 天时的 UBR_5（过去 5 天）
- 持仓第 10 天时的 UBR_10（过去 10 天）
- 持仓第 20 天时的 UBR_20（过去 20 天）
然后检验：**随着持仓进行，更新后的 UBR 是否提供了额外的预测力？**

这直接回答 Q5，并可能打开"滚动自适应退出"的设计空间。

---

## 实现方案

### 修改 `run_trade_diagnostics.py`

不需要新文件，在现有框架中新增一个计算函数即可：

```python
def compute_ubr(close: pd.Series, donchian_upper: pd.Series, lookback: int) -> pd.Series:
    """计算过去 lookback 天中 close ≥ donchian_upper 的天数占比"""
    above = (close >= donchian_upper).astype(float)
    return above.rolling(lookback, min_periods=max(1, lookback//2)).mean()
```

在 `compute_entry_features()` 中增加 UBR 的计算逻辑：

```python
# 在 signal_cache 构建时，增加 stop_high_10 的缓存
stop_high_10 = si.get("stop_high_10", pd.Series(index=dates_idx, dtype=float))

# 对每个 N 计算 UBR_N
for N in [5, 10, 20, 30, 40, 60]:
    ubr = compute_ubr(close, stop_high_10, N)
    # entry 日查值
    ubr_val = ubr.iloc[idx] if idx < len(ubr) else np.nan
```

### 修改 `run_mann_whitney()`

```python
# 在 numeric_cols 中增加
"ubr_5", "ubr_10", "ubr_20", "ubr_30", "ubr_40", "ubr_60"
```

### 新增预测力评估函数

```python
def predictivity_analysis(features_df: pd.DataFrame, ubr_col: str = "ubr_20") -> dict:
    """评估 UBR 作为胜率预测指标的表现"""
    from sklearn.metrics import roc_auc_score, roc_curve

    df = features_df.dropna(subset=[ubr_col, "was_win"]).copy()

    # 分桶统计
    bins = [0, 0.2, 0.4, 0.6, 0.8, 1.0]
    labels = ["0-20%", "20-40%", "40-60%", "60-80%", "80-100%"]
    df["bucket"] = pd.cut(df[ubr_col], bins=bins, labels=labels, include_lowest=True)
    bucket_stats = df.groupby("bucket", observed=False).agg(
        trades=("was_win", "count"),
        win_rate=("was_win", "mean"),
        avg_pnl=("pnl", "mean"),
    ).round(2)

    # ROC-AUC
    auc = roc_auc_score(df["was_win"], df[ubr_col])

    return {"bucket_stats": bucket_stats, "roc_auc": auc}
```

---

## 结果

### ⚠️ 假设不成立：UBR 不具有预测力

**测试日期**：2026-07-09
**基线参数**：ATR=25, Breakout=20, Stop=8, 1.5xATR, 6 品种, 144 笔交易

### U 检验结果（最佳窗口 ubr_60）

| Feature | Win Median | Loss Median | p-value | Effect | Sig? |
|:--|:--:|:--:|:--:|:--:|:--:|
| **holding_days** | **23.0** | **8.0** | **0.0000** | **-0.8597** | **Y** |
| atr_percentile_252 | 0.2936 | 0.5316 | 0.0028 | 0.3030 | **Y** |
| **ubr_60** | **0.0** | **0.0167** | **0.1122** | **0.1450** | **N** |
| ubr_40 | 0.0 | 0.0 | 0.3198 | 0.0870 | N |
| ubr_20 | 0.0 | 0.0 | 0.5683 | 0.0468 | N |
| ubr_5 | 0.0 | 0.0 | 0.7353 | 0.0231 | N |

**所有 UBR_N 均不显著**（p > 0.1, effect < 0.15）。

### 预测力评估（ubr_5）

| UBR 区间 | 交易数 | 胜率 | 平均盈亏 |
|:--:|:--:|:--:|:--:|
| 0-20% | 140 | 44.0% | +5,225 |
| 20-40% | 4 | 25.0% | +1,702 |
| 40-60% | 0 | NaN | NaN |
| 60-80% | 0 | NaN | NaN |
| 80-100% | 0 | NaN | NaN |

**ROC-AUC：0.488**（随机水平，最差窗口 ubr_60 仅 0.427）

### N 窗口遍历对比

| N | Effect | p-value | AUC | Sig? |
|:--:|:--:|:--:|:--:|:--:|
| 5 | 0.0231 | 0.7353 | 0.488 | N |
| 10 | 0.0796 | 0.2845 | 0.460 | N |
| 20 | 0.0468 | 0.5683 | 0.477 | N |
| 30 | 0.0502 | 0.5519 | 0.475 | N |
| 40 | 0.0870 | 0.3198 | 0.457 | N |
| 60 | 0.1450 | 0.1122 | 0.428 | N |

**所有窗口均无明显区分力，窗口越长 AUC 反而越差。**

### 关键诊断数据

| 指标 | 值 |
|:--|:--:|
| 总交易数 | 144（6 品种） |
| 胜率 | 43.8% (63/81) |
| CAGR | 9.23% |
| 盈利中位数持仓 | 23.0 天 |
| 亏损中位数持仓 | 8.0 天 |
| holding_days effect | -0.86 (p<0.001) ✅ |

### 结论

❌ **UBR 假设不成立**。原因：

1. **零偏态分布**：144 笔交易中 140 笔（97%）的 UBR_5 落在 0-20% 区间，中位数为 0。这意味着入场前的价格行为中，几乎从未站上 10 日通道上轨。

2. **与入场逻辑冲突**：海龟策略的突破入场条件是 `close > 20 日唐奇安上轨`。在突破发生之前，价格通常处于 20 日通道内部，自然低于 10 日通道上轨。UBR 为 0 是常态，不是信号。

3. **根本原因**：UBR 衡量的"短线动能密度"与 breakout 策略的入场时点存在结构性的错配。突破入场本身已要求价格创新高，此时再去回溯价格是否曾站上更低级别的通道上轨，没有增量信息。

### 副产品发现

`atr_percentile_252`（ATR 在 252 日中的分位数）首次通过 Mann-Whitney 检验：
- Win 中位数：0.2936（低波动期入场）
- Loss 中位数：0.5316（高波动期入场）
- Effect：0.3030 (p=0.0028)
- 含义：**低波动期入场更容易盈利**——此时突破信号更可靠，假突破更少。

这可能是另一个值得追踪的方向（可参考 `docs/experiments/S11_volatility_threshold.md`）。

---

## 参考
- [`S13_adaptive_exit.md`](S13_adaptive_exit.md) — 持仓时长与自适应退出的完整分析
- [`diagnostic_report.md`](../../results/diagnostics/diagnostic_report.md) — 当前诊断基线
- [`run_trade_diagnostics.py`](../../scripts/run_trade_diagnostics.py) — 诊断代码（特征计算 + U 检验）
