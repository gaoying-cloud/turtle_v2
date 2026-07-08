# 做空风险不对称修正 — 待验证

## 来源
2026-07-08 讨论，基于风险平价（α=0.0 最优）与海龟趋势跟踪的范式冲突分析。

## 核心思路
当前系统长空复用同一套 `risk_per_unit` 和 `pyramid_ratios`，但做空时的**方向性不对称**（上行 drift、short squeeze 风险、低价位加仓的单位亏损更大）使低 n_entry 加仓机制从"歪打正着"变成了系统性风险放大器。

**量化结论**（100k equity，做空入场价 10.00，n_entry=0.50，等额加仓）：
- 做空时低 n_entry 效应导致亏损放大 **×1.36（36%）**
- 做多镜像场景的绝对亏损仅做空的 **50%**
- 原因：做空在更低价格加仓（9.00、8.00），反弹到止损线（11.00）时每单位亏损更大

## 提案

### 方案 B（推荐）
引入一个参数 `short_risk_factor`，做空的风险预算 = 做多的 `short_risk_factor`，同时在加仓时用当前 n 重算（消除低 n_entry 效应）。

### 改动范围

#### 1. 新增参数
```python
# strategies/turtle_trading.py params 中
("short_risk_factor", 0.70),  # 做空风险预算比例，1.0=和做多一样
("short_recalc_pyramid", True), # 加仓时用当前 n 重算（消除低 n_entry 效应）
```

#### 2. 入场时缩放 risk
```python
# turtle_trading.py 约 677-681 行
base_risk = float(self._alpha_risk_pcts[i]) if self._alpha_risk_pcts is not None else self.params.risk_per_unit
if direction == "short":
    base_risk *= self.params.short_risk_factor
```

#### 3. 加仓时用当前 n 重算（仅做空）
```python
# turtle_trading.py _check_pyramid 约 970 行
if pos.direction == "short" and self.params.short_recalc_pyramid:
    current_n = si["n"].iloc[idx]
    if pd.notna(current_n) and current_n > 0:
        risk = self.params.risk_per_unit * self.params.short_risk_factor
        shares = calc_position_size(
            self._equity(), current_n, data.close[0],
            risk_pct=risk,
            stop_mult=2.0,
            min_unit=self.params.min_unit,
            multiplier=self.params.multipliers.get(code, 1)
        )
    else:
        shares = pos.shares_per_unit
else:
    shares = pos.shares_per_unit
```

### 其他备选方案
- **方案 A**：完全拆分长空参数（`long_risk_per_unit` / `short_risk_per_unit` 等 6+ 个参数）— 参数空间太大，不推荐
- **方案 C**：仅修正加仓时用当前 n 重算，不引入 `short_risk_factor` — 不能解决入场风险不对称

## 待验证

### 预期效果
| 指标 | 做多 | 做空（修正前） | 做空（修正后） |
|---|---|---|---|
| 入场风险 | 1% | 1% | 0.7% |
| +2N 加仓 vs 公平值 | 60% 超配 ✅ | 60% 超配 ⚠️ | 用当前 n 重算 ✅ |
| 极限反弹亏损 | 3.0% | 6.0% | ~3.5% |

### 验证步骤
1. 在 `config/turtle_config_short_test.yaml` 基础上修改
2. 网格搜索 `short_risk_factor ∈ [0.5, 0.6, 0.7, 0.8, 0.9, 1.0]`
3. 对比做空信号的 Sharpe、最大回撤、profit factor
4. 与纯做多的 baseline 做组合对比

### 需要测试的极端行情
- A 股 2015 年股灾（做多无效，做空可能有效但反弹极快）
- 2020 年 3 月流动性危机（做空可能被挤）
- 2024 年 2 月微盘股流动性危机
- 纳指 ETF（513100）的长期上行 drift 中做空的生存率

## 状态
📦 **已归档，待以后开启做空时验证**。当前主配置 `shortable: false`，问题不影响现有回测。
