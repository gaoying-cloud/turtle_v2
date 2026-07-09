# 项目架构手册

> 版本: S10 基线 | 更新: 2026-07-09

---

## 一、模块依赖全景

```
                     ┌──────────────────────┐
                     │     config_loader     │ ← 所有模块的统一配置入口
                     └──────────┬───────────┘
                                │
           ┌────────────────────┼────────────────────┐
           ▼                    ▼                    ▼
    ┌──────────┐        ┌───────────┐        ┌───────────┐
    │data_utils│        │turtle_core│        │market_regime│
    │(数据加载) │        │(核心计算)  │        │(市场状态)   │
    └────┬─────┘        └────┬──────┘        └─────┬─────┘
         │                   │                      │
         ▼                   ▼                      ▼
    ┌─────────────────────────────────────────────────────┐
    │              strategies/turtle_trading.py            │
    │              (Backtrader 策略层 — 核心枢纽)            │
    │  依赖: turtle_core + risk_parity + market_regime     │
    └──────────┬──────────────────────────────────────────┘
               │
    ┌──────────┼──────────┬──────────┬──────────┬──────────┐
    ▼          ▼          ▼          ▼          ▼          ▼
┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐
│backtest│ │comparison│ │grid_   │ │stress_ │ │gen_    │ │daily_  │
│.py     │ │.py      │ │search  │ │test.py │ │report  │ │signal  │
│        │ │         │ │.py     │ │        │ │.py     │ │.py     │
└────────┘ └────────┘ └────────┘ └────────┘ └────────┘ └────────┘
```

### 依赖层级

```
Layer 0: 外部库       pandas, numpy, backtrader, yaml
Layer 1: 工具模块     config_loader, data_utils, data_pipeline
Layer 2: 计算模块     turtle_core, risk_parity, market_regime, benchmarks
Layer 3: 策略层       turtle_trading.py (TurtleStrategy)
Layer 4: 脚本层       各 scripts/run_*.py
Layer 5: 数据输出     results/ 目录下的报告与 CSV
```

**核心规则**：下层模块不依赖上层。例如 `turtle_core.py` 不依赖 `turtle_trading.py`。

---

## 二、设计哲学

### 三层权重体系

仓位由三层权重逐层修正，每一层解决一个独立的问题：

```
                   第一层                         第二层                        第三层
              ATR 仓位计算                  集中度衰减                   风险平价偏移 + 权重倍率
           ┌────────────────┐          ┌─────────────────┐          ┌────────────────────────┐
           │ risk_pct = 1%  │          │ fade = 0.5~1.0  │          │ alpha=0 (当前关闭)     │
           │ per_unit_risk   │          │ 持仓越多→仓位越小│          │ weight_mult: 品种倍率   │
           │ = 2 × N × 价格 │          │ 防集中度风险    │          │ 豆粕 0.5x (S14)        │
           └────────┬───────┘          └────────┬────────┘          └───────────┬────────────┘
                    │                           │                               │
                    ▼                           ▼                               ▼
           ┌─────────────────────────────────────────────────────────────────────────┐
           │                         最终 risk_pct                                   │
           │           = base_risk × fade × weight_mult[品种]                        │
           │                                                                         │
           │           shares = equity × risk_pct / (2 × N × price)                 │
           └─────────────────────────────────────────────────────────────────────────┘
```

| 层 | 解决的问题 | 参数 | 设计取舍 |
|:--|:--|:--|:--|
| **ATR 仓位** | 波动率越高，仓位越小（防震荡） | `risk_per_unit=1%` | 缺点：低波动品种(豆粕)权重虚高 |
| **集中度衰减** | 持仓越多，新开仓越小（防集中风险） | `concentration_trigger=3` | 6品种时 3→4 持仓开始衰减 |
| **风险平价偏移** | 协方差结构调权（相关性低→权重高） | `alpha=0.0`(关闭) | 开启后 Sharpe 略降，暂不启用 |
| **权重倍率** | 人工纠正 ATR 等权的结构偏差 | `豆粕 0.5x` | 减半豆粕权重，不超配任何品种 |

### 为什么当前 α=0（关闭风险平价）

网格搜索结果显示 α=0 的 Sharpe(1.1098) 优于任何 α>0 的组合。原因：
- 6 个品种相关性已经较低（最大 ~0.6）
- 风险平价的协方差估计在 6 品种上增益有限
- 不如直接通过权重倍率（豆粕 0.5x）纠正 ATR 的结构偏差

### 为什么 weight_multipliers 放在风险平价层

`compute_alpha_weights()` 返回 `risk_pcts` 数组，weight_multipliers 在最后一步乘入——无论 α=0 还是 α>0，都能生效。这是扩展点而非 hack。

---

## 三、模块详解

### Layer 1 — 工具模块

#### `src/config_loader.py` — 配置加载

单一职责：读取 `config/turtle_config.yaml`，提供类型安全的数据访问函数。

```python
load_config()                          # 读取完整 YAML
get_trading_symbols(config)            # → ['510500.SH', ...] (6个ETF)
get_symbol_names(config)               # → {'510500.SH': '中证500', ...}
get_bond_symbol(config)                # → '511010.SH' (国债，已弃用)
get_all_symbols(config)                # → 全部品种 (含国债，已弃用)
get_shortable_symbols(config)          # → 可做空品种集合（当前全部 false）
get_t_plus_one_symbols(config)         # → T+1品种集合 {159915.SZ, 510500.SH}
get_futures_symbols(config)            # → 期货品种代码列表
```

**无外部依赖**，只读 `yaml`。

#### `src/data_utils.py` — 数据加载

```python
load_data(symbol, start, end, data_dir)       # → pd.DataFrame (从 parquet)
load_price_matrix(symbols, start, end)         # → pd.DataFrame (价格矩阵)
align_to_common_dates(df_dict)                 # → 多品种日期对齐 (outer join)
df_to_feed(df, symbol)                         # → bt.feeds.PandasData
```

**接口约定**：`load_data` 返回的 DataFrame 必须有 `date, open, high, low, close, volume` 列。日期列必须是字符串 `YYYY-MM-DD` 格式。

---

### Layer 2 — 计算模块

#### `src/turtle_core.py` — 海龟核心

这是整个策略的数学基础，**不含 Backtrader 依赖**。分为无状态计算函数和有状态管理类两层。

> ⚠️ **`stop_atr_multiple` 参数说明**：虽然参数名为"止损N倍数"，但它**不控制退出触发**。
> 退出始终使用 10 日低点突破（`exit_low_10`），与 `stop_atr_multiple` 无关。
> 该参数的实际作用是 **仓位规模控制**：`per_unit_risk = stop_mult × N`，值越小→单位风险越小→仓位越大（更激进）。
> 网格搜索搜到最优值 1.5，意味着更激进的仓位规模有利于收益。

##### 无状态计算函数

| 函数 | 算法 | 用途 |
|:--|:--|:--|
| `compute_tr()` | `max(H-L, H-PC, L-PC)` | 真实波幅 |
| `compute_atr()` | `SMA(TR, period)` | 波动率度量 |
| `donchian_high()` | `rolling_max(high, period)` | 突破入场信号 |
| `donchian_low()` | `rolling_min(low, period)` | 反向突破退出 |
| `calc_position_size()` | `equity × risk_pct / (stop_mult × N)` | 头寸规模 |
| `calc_fixed_stop()` | `entry_price - stop_mult × N` | 止损价 |
| `volume_confirmation()` | `vol > SMA(vol, 20) × 1.5` | 成交量确认 |
| `breakout_quality()` | 多指标加权评分 | 突破质量过滤 |
| `recent_batting_avg()` | 近 N 笔胜率 | 入场信心 |

##### `TurtleSignals` — 信号预计算

一次性为所有品种预计算海龟信号序列，避免在 `next()` 中重复计算。

**输出数据结构** (`signals[code]`)：

```python
{
    "n": pd.Series,              # ATR 序列
    "entry_high_20": pd.Series,  # 20 日最高价（入场参考）
    "entry_low_20": pd.Series,   # 20 日最低价（入场参考）
    "entry_high_55": pd.Series,  # 55 日最高价（55日过滤用）
    "entry_low_55": pd.Series,   # 55 日最低价（55日过滤用）
    "exit_low_10": pd.Series,    # 10 日最低价（退出参考）
    "sma20": pd.Series,          # 20日均线（退化检测用）
    "sma60": pd.Series,          # 60日均线
    "hurst": pd.Series,          # Hurst 指数
    "rsi": pd.Series,            # RSI
}
```

##### `SignalFilter` — 6 条入场拒绝规则

```python
check_entry(symbol, has_position) → (passed: bool, reason: str)
```

| 规则 | 条件 | 目的 |
|:--|:--|:--|
| **① 连续拒绝** | 连续拒绝 ≥ max_rejections 时强制放行一次 | 防永久沉默 |
| **② 已有仓位** | `has_position=True` 时拒绝 | 防重复入场 |
| **③ 日内已买** | 当日已买入同一品种时拒绝 | 防重复下单 |
| **④ 连续亏损** | 近 N 笔连续亏损时拒绝 | 亏损保护 |
| **⑤ 暂停期** | 全局暂停期内拒绝所有入场 | 风控暂停 |
| **⑥ 爆仓封禁** | 品种处于爆仓封禁状态时拒绝 | 极端风控 |

> ⚠️ **维护注意**：修改 `SignalFilter` 规则时，必须同步检查 `daily_signal.py` 中的信号逻辑是否有对应实现。

##### `SignalFilter.record_result()` — 成交后回调

```python
record_result(symbol, was_win: bool)
```

- 更新 `consecutive_rejections` 归零
- 更新连续亏损计数器
- 更新爆仓封禁状态
- **不检查现金/风险约束**（这些由上层校验）

##### 其他管理类

| 类 | 职责 | 关键属性 |
|:--|:--|:--|
| `Position` | 单品种持仓 | `entry, shares, n_at_entry, high_since_entry, half_closed, protection_activated` |
| `TurtlePositions` | 多品种持仓管理器 | `count, positions_dict, add()/reduce()/close()` |

---

#### `src/risk_parity.py` — 风险平价 + 权重倍率

三个核心函数：

```python
ledoit_wolf_cov(returns)           # 收缩协方差 → 稳定正定矩阵
risk_parity_weights(cov)           # Newton-Raphson 求解 → 等风险贡献权重
compute_alpha_weights(returns, alpha, base_risk_pct, weight_multipliers)
# → {risk_pcts, rp_weights, cov, converged}
```

**α 融合公式**：

```
fused_weight[i] = (1-α) × (1/N) + α × rp_weights[i]
risk_pcts[i] = base_risk_pct × fused_weight[i] / (1/N)
             × weight_multipliers[i]   # S14: 品种级倍率
```

**当 α=0 时**：快速路径，跳过全部协方差计算，直接返回 `risk_pcts = base_risk_pct × weight_multipliers`。

**接口约定**：
- `returns` 必须是 `(T, N)` 的 numpy 数组，T≥2，N≥2
- `weight_multipliers` 是 `{column_index: multiplier}` 字典，由上层（`TurtleStrategy`）根据 `params.symbols` 顺序构建

---

#### `src/market_regime.py` — 市场状态识别

```python
class MarketRegime:
    def is_choppy(prices, n_pct, eff_20d, n_trend) -> bool
    def is_trending(...) -> bool
    def get_regime(...) -> str  # "trending" / "choppy" / "unknown"
```

**三个子指标**：
| 指标 | 含义 | 碎步市判定 |
|:--|:--|:--|
| `n_pct` | ATR / 价格 | 值偏低 → 碎步 |
| `eff_20d` | 20日效率系数 | 接近 0 → 碎步 |
| `n_trend` | N 的趋势方向 | 频繁翻转 → 碎步 |

**当前状态**：`regime_filter: 'off'`，未启用。配置开启后会在 `_check_entry()` 中拦截碎步市的入场信号。

---

#### `src/benchmarks.py` — 基准策略

| 策略类 | 对应 | 逻辑 | 交易次数 |
|:--|:--:|:--|:--:|
| `BuyAndHold` | B1 | 第一天等权买入，一直持有 | 4 |
| `EqualWeightRebalance` | B2 | 每季度再平衡到等权 | ~200 |
| `ATREqualRisk` | B3 | 每季度按 ATR 倒数分配权重 | ~360 |

B4（海龟纯策略）直接由 `TurtleStrategy` 承担，不在 `benchmarks.py` 中。

---

### Layer 3 — 策略层

#### `strategies/turtle_trading.py` — TurtleStrategy

##### `next()` 主循环流程

```
next() 每根 K 线
  │
  ├── 检查退化 _check_degradation()   → 三规则（拦截→磨损→沉默）
  │
  ├── 检查再平衡 _should_rebalance()  → 首次/每季度/ATR变动30%
  │     └── _recalc_alpha_weights()   → 计算权重倍率
  │
  ├── 遍历各品种
  │     │
  │     ├── 无持仓 → _check_entry()
  │     │     ├── 突破判断: close > entry_high_20
  │     │     ├── SignalFilter.check_entry()  ← 6 条拒绝规则
  │     │     ├── calc_position_size()        ← 含权重倍率
  │     │     └── 风控校验: 单品种风险 ≤ 4%, 全账户 ≤ 20%
  │     │
  │     ├── 有持仓 → _should_exit()
  │     │     ├── 利润保护: 浮盈≥19N 且回撤2N → 减半
  │     │     └── 标准退出: low < exit_low_10 → 全平
  │     │
  │     └── 有持仓 → _check_pyramid()
  │           └── 价格 ≥ 入场价 + pyramid_step × N → 加一单位
  │
  └── stop() 回测结束
        └── 输出交易统计 + 半仓事件汇总
```

##### 利润保护状态机

```
状态: protection_activated = false
  │
  └── high_since_entry ≥ entry_price + 19 × N₀
        │
        ▼
  protection_activated = true (永久，不复位)
  │
  └── low ≤ max(entry_price, high_since_entry - 2 × N₀)
        │
        ▼
  _execute_half_exit()
  平掉一半仓位，剩半仓继续走标准退出
```

**设计要点**：
- 19N 阈值：足够大，只抓极端浮盈
- N₀ 固定：用开仓日 ATR，不逐日更新
- 一旦激活永久保持（修复 bug：原逻辑每日重新计算，冲高回落时跳过检查）

##### 加仓逻辑（金字塔）

```python
_check_pyramid():
  if units >= max_units: return        # 满仓
  if close < last_add_price + pyramid_step × N: return  # 未到加仓线
  → 加一单位（数量和首仓相同）
```

`pyramid_step=2.0` 意味着：价格每上涨 2N 加一单位，最多加到 max_units=4 单位。

##### 三规则退化检测

按触发时序排列：

| 触发顺序 | 规则 | 条件 | 报警内容 |
|:--:|:--|:--|:--|
| 最早 | **② 拦截型** | `信号数 ≥ min_signals` 且 `入场/信号 ≤ conv_min` | `拦截②(38信14入转化率37%≤60%)` |
| 中间 | **③ 磨损型** | 近 N 笔全亏 或 胜率<25% 且亏损>5%本金 | `磨损③(6笔全亏亏1.3%本金)` |
| 最晚 | **① 沉默型** | 年均信号数 < 2 | `沉默①(年均0.5<2)` |


### Layer 4 — 脚本层

#### `scripts/run_backtest.py` — 单次回测

最底层的回测执行器。其他脚本通过 `from scripts.run_backtest import load_data, df_to_feed, align_to_common_dates` 复用其数据加载逻辑。

**核心流程**：
```
load_data(6个ETF) → align_to_common_dates → df_to_feed → Cerebro + TurtleStrategy → run()
```

**ETF 模式**（当前）：不加载债券，6 品种对齐后回测。
**期货模式**（可通过 `--futures` 开启）：加载期货数据，使用期货参数。

#### `scripts/run_comparison.py` — 基准对比

运行 B1-B4 四个策略，输出 CSV 到 `results/comparison/`。

**接口**：输出的 CSV 必须包含 `总收益率%, 年化收益%, 夏普, 最大回撤%, 年化波动%, 胜率%, 盈亏比, 交易次数, 最终净值` 列，`gen_report.py` 按列名读取。

#### `scripts/run_stress_test.py` — 压力测试

运行 4 个历史情景（A1-COVID / A2-俄乌 / A3-二次探底 / A4-2022全年）+ 2 个合成情景（B1-同步暴跌 / B2-流动性枯竭）。

**输出**：生成 `stress_report.md` + `stress_conclusion.json` + 各场景详细 CSV。

#### `scripts/run_grid_search.py` — 网格搜索

搜索 7 个核心参数 + 可选 2 个权重倍率。
支持 `--two-stage`（粗筛+精搜）和 `--weight-search`/`--weight-only`。

**输出文件**：

```
results/grid_search/
├── grid_results_full.csv          # 样本内全量结果
├── oos_validation.csv             # 样本外验证
├── best_params.json               # 最优参数（含权重倍率）
├── weight_search_is.csv           # 权重搜索样本内
├── weight_search_oos.csv          # 权重搜索样本外
├── rolling_validation.csv         # 滚动窗口检验(可选)
└── stability_scan.csv             # 稳定性扫描(可选)
```

**参数空间**：

| 场景 | 参数 | 组合数 |
|:--|:--|:--:|
| 全量搜索 | atr×breakout×stop×mult×α×mcl×mcloss = 3×3×3×3×5×3×3 | 3645 |
| 两阶段阶段一 | 固定风控参数 | 405 |
| 两阶段阶段二 | Top-20 × 风控9组合 | 180 |
| 权重搜索 | 纳指4×豆粕4 | 16 |

#### `scripts/gen_report.py` — 综合报告

读取网格搜索 / 压力测试 / 基准对比的结果，加上一次最优参数回测，生成 `results/report.md`。

**参数来源**：
- **默认**：从 `config/turtle_config.yaml` 读取
- **`--use-best`**：从 `grid_search/best_params.json` 读取网格搜索最优参数

#### `scripts/daily_signal.py` — 每日信号

独立于 Backtrader 的信号生成器。使用 `state.json` 维护实盘持仓状态。

**state.json 数据结构**：

```python
{
    "equity": 100000.0,              # 当前账户权益
    "cash": 95000.0,                 # 当前可用现金
    "positions": [                   # 当前持仓列表
        {
            "symbol": "510500.SH",
            "shares": 9100,
            "entry_price": 3.3747,
            "n_at_entry": 0.0545,
            "units": 1,
            "direction": "long",
            "high_since_entry": 3.85,
            "half_closed": False,
            "protection_activated": False,
        }
    ],
    "trade_history": [],             # 已平仓交易记录
    "signal_filter": {               # SignalFilter 状态
        "510500.SH": {"last_reject": "2026-06-10", ...}
    },
    "consecutive_losses": {},
    "buy_today": {},
    "half_exit_events": [],          # 利润保护减半事件
}
```

**与 TurtleStrategy 的对应关系**：

| daily_signal.py | TurtleStrategy 等价逻辑 | 注意事项 |
|:--|:--|:--|
| `compute_signals()` | `TurtleSignals` 预计算 | 同用 `TurtleSignals` 类 |
| `should_enter()` | `_check_entry()` 中的突破判断 | 逻辑需一致 |
| `check_exit()` | `_should_exit()` | 利润保护 + 10日低点 |
| `should_add()` | `_check_pyramid()` | 加仓步进逻辑 |
| `calc_shares()` | `calc_position_size()` × 权重倍率 | 同用 `turtle_core.calc_position_size()` |
| `_check_risk_limits()` | 风控参数校验 | 独立实现 |

> ⚠️ **Golden Rule**: `daily_signal.py` 和 `turtle_trading.py` 是双生子。改了其中一个的入场/退出/加仓逻辑，另一个必须同步修改。

---

## 四、关键参数清单

### 海龟核心参数（`config.turtle`）

| 参数 | 当前值 | 作用 | 网格搜索范围 |
|:--|:--:|:--|:--:|
| `atr_period` | **25** | ATR 计算周期 | [15, 20, 25] |
| `breakout_period` | **20** | 唐奇安通道突破周期 | [15, 20, 25] |
| `stop_period` | **8** | 反向突破退出周期 | [8, 10, 12] |
| `stop_atr_multiple` | **1.5** | 仓位计算 N 倍数（⚠️ 不控制止损） | [1.5, 2.0, 2.5] |
| `risk_per_unit` | 0.01 | 每单位风险比例 | 固定 |
| `max_units` | 4 | 最大加仓单位数 | 固定 |
| `pyramid_step` | 2.0 | 加仓步长(N) | 固定 |
| `use_55_filter` | false | 55日过滤 | 未启用 |

### 权重参数（`config.weighting`）

| 参数 | 当前值 | 作用 |
|:--|:--:|:--|
| `alpha` | **0.0** | 风险平价偏移（0=纯 ATR 等权） |
| `weight_multipliers` | `{513100.SH: 1.0, 159985.SZ: 0.5}` | 品种级权重倍率 |

### 风控参数（`config.risk`）

| 参数 | 当前值 | 作用 |
|:--|:--:|:--|
| `max_portfolio_risk` | 0.20 | 全账户风险上限 |
| `single_max_risk` | 0.04 | 单品种风险上限 |
| `concentration_trigger` | 3 | 集中度衰减起始持仓数 |
| `max_consecutive_losses` | 8 | 连续亏损暂停阈值 |
| `pause_days` | 5 | 暂停后冷却天数 |
| `degradation` | {...} | 退化检测三规则参数 |

---

## 五、数据流全景

```
                         turtle_config.yaml
                               │
                     config_loader.py 读取
                               │
         ┌─────────────────────┼─────────────────────┐
         ▼                     ▼                     ▼
   pull_data.py          run_backtest.py       daily_signal.py
   (下载 parquet)        (加载 parquet)        (加载 parquet)
         │                     │                     │
         ▼                     ▼                     ▼
   data/etf_daily/       align_to_common_dates  compute_signals()
   *.parquet                  │                   (TurtleSignals)
                              │                     │
                              ▼                     ▼
                       TurtleSignals           should_enter()
                       (预计算信号)              calc_shares()
                              │                 含权重倍率
                              ▼                     │
                       TurtleStrategy           state.json
                       .next() 回测             (持仓状态)
                              │
                              ▼
                       analyzers(Sharpe,
                        DD, Trades, TimeReturn)
                              │
                              ▼
                       gen_report.py → results/report.md
                       run_comparison.py → results/comparison/*.csv
                       run_stress_test.py → results/stress_test/
                       run_grid_search.py → results/grid_search/
```

---

## 六、实验改动指引

修改某个文件时，需要考虑的连锁影响：

| 改这个文件 | 要同步改 | 要同步测试 |
|:--|:--|:--|
| `src/turtle_core.py` | 无（纯计算，下层模块） | `test_turtle_core.py` |
| `src/risk_parity.py` | `turtle_trading.py` | `test_risk_parity.py` |
| **`strategies/turtle_trading.py`** | **`daily_signal.py`** ⚠️ | `test_turtle_strategy.py` |
| **`scripts/daily_signal.py`** | **`turtle_trading.py`**（反向）⚠️ | 无专用测试 |
| `scripts/gen_report.py` | 无 | `test_gen_report.py` |
| `scripts/run_grid_search.py` | `gen_report.py`（结果格式） | `test_grid_search.py` |
| `config/turtle_config.yaml` | 无（自动读取） | — |

**Golden Rule**：`daily_signal.py` 和 `turtle_trading.py` 是双生子——改了其中一个的入场/退出/加仓/权重逻辑，另一个必须同步。两者的信号产生和仓位计算逻辑必须等同。

---

## 七、关键数据结构

### 模块间接口契约

| 传递者 | → | 接收者 | 数据结构 | 说明 |
|:--|:--:|:--|:--|:--|
| `config_loader` | → | 所有模块 | `config` (dict) | YAML 的 Python 表示 |
| `data_utils.load_data` | → | 回测/signal | `pd.DataFrame` | 列: date, open, high, low, close, volume |
| `data_utils.align_to_common_dates` | → | 回测 | `dict[str, pd.DataFrame]` | key=品种代码, value=对齐后的 df |
| `TurtleSignals.precompute_all` | → | `TurtleStrategy` | `dict[str, dict]` | `{code: {n: Series, entry_high_20: Series, ...}}` |
| `compute_alpha_weights` | → | `TurtleStrategy` | `dict` | `{risk_pcts, rp_weights, cov, converged}` |
| `calc_position_size` | → | `TurtleStrategy`/`daily_signal` | `int` | 股数（100 的整数倍） |
| `run_single_backtest` | → | 网格/对比/报告 | `dict` | `{sharpe, cagr, mdd, total_trades, ...}` |
| `daily_signal.run()` | → | 文件系统 | `state.json` | JSON, 见第五-4 节 |

---

## 八、未来功能占位

以下功能在代码和配置中留有接口，但**当前未启用**，后续实验可验证。

### 8.1 做空（shortable）

**配置**：每个品种 `shortable: false`（当前全部关闭）。

```yaml
symbols:
  - code: 510500.SH
    shortable: false    # → true 启用做空
```

**代码支持**：
- `TurtleStrategy.params.shortable_symbols` — 可做空品种集合
- `TurtleStrategy._check_entry()` 中有 `direction` 参数
- `TurtlePositions` 支持做空持仓
- `daily_signal.py` 的 `_default_state()` 中 `direction: "long"` 固定

**待验证**（见 `docs/experiments/S12_short_asymmetry.md`）：
- 做空时低 n_entry 加仓效应导致亏损放大 36%
- 需引入 `short_risk_factor` 参数

### 8.2 期货模式（futures）

**配置**：已有完整参数段和品种清单。

```yaml
futures:
  initial_cash: 100000
  margin_rate: 0.15
  risk_per_unit: 0.035
  futures_list:
    - {ts_code: M.DCE, name: 豆粕, multiplier: 10}
    - {ts_code: CF.ZCE, name: 棉花, multiplier: 5}
    - {ts_code: RB.SHF, name: 螺纹钢, multiplier: 10}
```

**代码支持**：
- `run_backtest.py` 支持 `--futures` 参数
- `TurtleStrategy.params.futures_mode` / `multipliers` / `min_unit`
- `calc_position_size()` 接受 `multiplier` 和 `min_unit` 参数

**当前限制**：
- 期货数据尚未通过 `pull_data.py` 下载
- `daily_signal.py` 未适配期货

### 8.3 55日过滤（`use_55_filter`）

**配置**：`turtle.use_55_filter: false`

**作用**：入场同时要求价格突破 55 日通道。网格搜索中模式 B 的 Sharpe(0.79) 低于模式 A(1.11)，当前不启用。

### 8.4 碎步市过滤（`regime_filter`）

**配置**：`turtle.regime_filter: 'off'`

**代码**：`MarketRegime` 模块已完整实现，`regime_filter` 可设为 `'on'` 或 `'strict'`。

**A/B 测试结果**（2026-07-08）：入口拦截导致 CAGR 从 14.05% 暴跌至 8.32%。

### 8.5 趋势持续时间过滤（`use_trend_duration_filter`）

**配置**：`turtle.use_trend_duration_filter: false`

**作用**：要求趋势至少持续 N 天才允许入场，减少假突破。

### 8.6 投票式信号确认（`min_confirmations`）

**配置**：`turtle.min_confirmations: 0`（关闭）

**支持的三项确认**：
| 确认项 | 条件 |
|:--|:--|
| 成交量放量 | `vol > SMA(vol, 20) × 1.5` |
| K 线实体足够 | 实体占比 ≥ 40% |
| 近期胜率高 | 近 4 笔盈利 > 亏损 |

默认 `min_confirmations=0`，三项全部通过才算确认通过。`>=1` 时使用投票制。

### 8.7 自适应退出（S13 — 不推荐）

见 `docs/experiments/S13_adaptive_exit.md`。当前 `stop_period=8` 的 OOS Sharpe=1.04 已很好，且 `regime_filter` 的 A/B 测试显著劣化。**优先级低**。

### 8.8 国债品种

**过去**：`BOND_SYMBOL=511010.SH` 作为第 7 品种加入回测。
**现在**：已移除（V6.3），ETF 模式只跑 6 个品种。
**原因**：国债的 ATR 极低导致权重过大，且策略逻辑不直接交易国债。

---

## 九、测试覆盖

```
tests/
├── test_turtle_core.py      # 海龟计算函数（ATR/仓位/信号过滤）
├── test_turtle_strategy.py  # TurtleStrategy 策略逻辑（含 alpha 权重）
├── test_risk_parity.py      # 风险平价计算
├── test_benchmarks.py       # B1/B2/B3 基准策略
├── test_gen_report.py       # 报告生成逻辑
├── test_grid_search.py      # 网格搜索评估
├── test_stress_test.py      # 压力测试
├── test_data_pipeline.py    # 数据下载管道
├── test_screening.py        # 品种筛选
└── __init__.py
```

当前总数: **205 passed**
