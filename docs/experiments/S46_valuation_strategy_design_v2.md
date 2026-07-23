# S46: PE/PB 估值回归策略 — 设计文档 V2.0

**状态**：Phase 1 进行中 — 数据管道已完成，代码审查修复已合入（2026-07-23）
**基于**：S46 V1 设计讨论 + 924 创业板行情分析 + 多源实证研究 + Tushare 数据验证
**上一篇**：S46_valuation_strategy_design.md（V1，已废弃）
**下一篇**：Phase 1 编码实施

---

## 零、文档导航

- [一、策略本质与定位](#一策略本质与定位)
- [二、数据架构](#二数据架构)
- [三、核心信号体系](#三核心信号体系)
- [四、两阶段建仓架构](#四两阶段建仓架构)
- [五、出场信号体系](#五出场信号体系)
- [六、资金管理](#六资金管理)
- [七、风控机制](#七风控机制)
- [八、辅助信号：汇金/平准基金](#八辅助信号汇金平准基金)
- [九、模块架构与数据流](#九模块架构与数据流)
- [十、回测计划](#十回测计划)
- [十一、V1 实施范围](#十一v1-实施范围)
- [十二、关键参数汇总](#十二关键参数汇总)
- [十三、待解问题与 V2 方向](#十三待解问题与-v2-方向)
- [十四、讨论记录](#十四讨论记录)

---

## 一、策略本质与定位

### 1.1 行情本质

924 行情是一次**纯粹的"估值脉冲修复"**（详见 `924_chiNext_rally_analysis.md`）：

| 特征 | 数据 |
|:---|:---|
| 爆发速度 | 6 个交易日 PE 从 23.80 → 38.84（+63.2%）|
| 起点 | PE 0.66% 分位（历史级极端低估）|
| 终点 | PE 28% 分位（未泡沫化，仅"恐慌修复"）|
| 盈利参与度 | **全程缺席**——纯拔估值 + 均值回归 |

### 1.2 实盘策略体系（双策略）

```
海龟策略 → 趋势跟随（追涨），信号 = 价格突破唐奇安通道
估值策略 → 价值回归（抄底），信号 = 估值分位 + 价格确认 ← NEW
```

> **注**：N 字结构策略（`strategies/n_structure.py`）保留代码和回测能力，但**不纳入实盘资金体系**，仅作为独立研究方向。

### 1.3 核心原则

1. **独立策略，不做融合**：估值与海龟信号时间轴天然错位——"PE 高估时往往是海龟进场的时间"，强行融合 = 互相掐架
2. **独立资金池**：通过风险预算共享制从总现金池动态划拨，无专属固定资金
3. **估值策略交易频率最低**：几年一次完整建仓→减仓周期，0.66% 分位级别的机会不是每年都有
4. **实盘仅海龟 + 估值双策略**：N 字结构因实盘可行性限制，保留为研究策略

---

## 二、数据架构

### 2.1 数据源

| 数据 | 来源 | 字段 | 频率 |
|:---|:---|:---|:---|
| PE_TTM | Tushare `index_dailybasic` | `pe_ttm` | 日频 |
| PB | Tushare `index_dailybasic` | `pb` | 日频 |
| 总市值 | Tushare `index_dailybasic` | `total_mv` | 日频 |
| 收盘价 | Tushare `index_daily` | `close` | 日频 |
| 成交额 | Tushare `index_daily` | `amount` | 日频 |
| 涨跌幅 | Tushare `index_daily` | `pct_chg` | 日频 |

**数据起始**：
- 创业板指：**2010-06-01**（指数发布日），约 16 年，~3900 个交易日
- 沪深300：**2005-04-08**，约 21 年
- 中证500：待验证
- 上证50：待验证

**缓存格式**：Parquet + Snappy，`data/index_valuation/` 目录

> **技术注**：`index_dailybasic` 单次调用限 3000 行。需按年份分段分页拉取（每段 ≤ 3 年），才能获取 2014 年之前的完整数据。官方文档确认数据从 2004 年开始。

### 2.2 覆盖指数

| 指数代码 | 名称 | 用途 | 优先级 |
|:---|:---|:---|:---|
| 399006.SZ | 创业板指 | 主策略标的（弹性最大）| P0 |
| 000300.SH | 沪深300 | 辅助判断全市场估值水位 | P1 |
| 000905.SH | 中证500 | 辅助判断中小盘估值 | P1 |
| 000016.SH | 上证50 | 辅助判断大盘价值估值 | P2 |

### 2.3 数据清洗规范

```python
def clean_valuation_data(df):
    """
    估值数据清洗流水线

    步骤：
    1. 异常值裁剪：PE < 1 或 PE > 300 → 标记为异常
    2. 短缺口插值：≤5 天的缺失用线性插值
    3. 长缺口填充：>5 天的缺失用 20 日滚动中位数
    4. 价格交叉验证：PE 变动与价格变动背离 > 15% → 标记可疑
    """
    # Step 1: 裁剪
    pe = df['pe_ttm'].clip(lower=1, upper=300)
    pb = df['pb'].clip(lower=0.1, upper=50)

    # Step 2-3: 缺口处理
    pe = pe.interpolate(method='linear', limit=5)
    pe = pe.fillna(pe.rolling(20, min_periods=5).median())

    # Step 4: 价格交叉验证
    pe_change = pe.pct_change()
    price_change = df['close'].pct_change()
    suspicious = (pe_change - price_change).abs() > 0.15

    return pe, pb, suspicious
```

### 2.4 数据质量监控（实盘必备）

#### 2.4.1 数据新鲜度检查

```python
def check_data_freshness(last_data_date, today, max_trading_days_behind=3):
    """
    数据新鲜度检查

    若最新数据落后超过 N 个交易日 → 策略自动暂停。
    区分"交易日差距"和"自然日差距"——长假休市不应触发报警。
    """
    trading_days_behind = count_trading_days(last_data_date, today)

    if trading_days_behind > max_trading_days_behind:
        return {
            'status': 'SUSPEND',
            'reason': f'数据落后 {trading_days_behind} 个交易日（阈值 {max_trading_days_behind}），接口可能异常'
        }
    return {'status': 'OK'}
```

#### 2.4.2 PE 跳变熔断

```python
def pe_jump_circuit_breaker(pe_current, pe_prev, close_current, close_prev,
                             calendar_days_since_last_trade):
    """
    PE 跳变熔断

    核心逻辑：不是因为 PE 跳变大就报警，而是 PE 跳变与价格跳变不一致才报警。
    PE = Price / EPS，如果 Price 同步涨了 30%，PE 涨 30% 是真实行情。

    区分常规间隔和长假间隔：
    - 常规周末（≤3天）：PE 变化 > 25% 且与价格背离 > 20pp → 数据错误
    - 长假（4-10天）：PE 变化 > 50% 且与价格背离 > 20pp → 数据错误
    - 超长停牌（>10天）：不做判断，标记后人工审核
    """
    pe_change = abs(pe_current / pe_prev - 1)
    price_change = abs(close_current / close_prev - 1)

    if calendar_days_since_last_trade <= 3:
        threshold = 0.25
    elif calendar_days_since_last_trade <= 10:
        threshold = 0.50
    else:
        return {'status': 'WARN', 'reason': '超长间隔，人工审核'}

    if pe_change > threshold:
        divergence = abs(pe_change - price_change)
        if divergence > 0.20:
            return {
                'status': 'DATA_ERROR',
                'action': 'SUSPEND',
                'reason': f'PE跳变 {pe_change:.1%} 与价格跳变 {price_change:.1%} 背离 {divergence:.1%}pp'
            }
        else:
            return {
                'status': 'WARN',
                'action': 'CONTINUE',
                'reason': f'PE跳变 {pe_change:.1%} 与价格同步，判定为真实行情'
            }

    return {'status': 'OK'}
```

#### 2.4.3 每日数据健康检查入口

```python
def daily_health_check(data_dict, today):
    """
    每日数据健康检查（在策略信号计算前调用）

    Returns:
        {'status': 'OK' | 'SUSPEND' | 'WARN', 'reasons': [...]}
    """
    results = []

    for code, df in data_dict.items():
        # 1. 新鲜度
        fresh = check_data_freshness(df['date'].max(), today)
        results.append(fresh)

        # 2. 跳变
        if len(df) >= 2:
            pe_curr, pe_prev = df['pe_ttm'].iloc[-1], df['pe_ttm'].iloc[-2]
            close_curr, close_prev = df['close'].iloc[-1], df['close'].iloc[-2]
            cal_days = (df['date'].iloc[-1] - df['date'].iloc[-2]).days
            jump = pe_jump_circuit_breaker(pe_curr, pe_prev, close_curr, close_prev, cal_days)
            results.append(jump)

    if any(r['status'] == 'DATA_ERROR' for r in results):
        return {'status': 'SUSPEND', 'reasons': results}
    elif any(r['status'] == 'SUSPEND' for r in results):
        return {'status': 'SUSPEND', 'reasons': results}
    return {'status': 'OK', 'reasons': results}
```

### 2.5 数据管道实现

**实现文件**：`src/valuation_pipeline.py`（独立文件，不复用 `data_pipeline.py`，避免影响海龟策略 dry-run）

**复用项**（import，不修改原文件）：
- `src/data_pipeline` 的 `_create_tushare_pro`, `_clean_raw_ohlc`, `_merge_into_cache`, `_normalize_date`, `PROJECT_ROOT`

**公开接口**：

```python
def fetch_index_valuation(
    ts_codes: list[str] | None = None,   # 默认四大指数
    start_date: str = "20100101",         # 创业板指 2010-06-01 发布
    end_date: str | None = None,          # 默认最新交易日
    force_update: bool = False            # True = 跳过缓存，强制全量重拉
) -> dict[str, pd.DataFrame]:
    """
    拉取指数估值 + 行情数据，缓存至 data/index_valuation/{code}.parquet

    每指数独立 try/except 隔离 + API 空返回自动缓存回退。
    估值列（pe_ttm/pb/total_mv 等）在清洗后转为 float64。
    合并后校验核心列存在性（pe_ttm/pb/total_mv/close）。

    Returns:
        {ts_code: DataFrame}, 拉取失败的指数不在 dict 中
    """

def get_valuation_summary() -> pd.DataFrame:
    """检查所有默认指数估值缓存的本地状态（code/earliest/latest/rows/pe_min/pe_max/pb_min/pb_max）"""
```

**内部函数**：

```
_fetch_dailybasic_segment(pro, ts_code, start, end) → pd.DataFrame   # 单段 + 重试
_fetch_dailybasic_paginated(ts_code, start, end) → pd.DataFrame      # 分页包装（3年/段）
_fetch_index_daily_segment(pro, ts_code, start, end) → pd.DataFrame   # 单段 + 重试
_fetch_index_daily_raw(ts_code, start, end) → pd.DataFrame            # 分页包装（与 dailybasic 对称）
_merge_valuation_and_price(df_basic, df_daily) → pd.DataFrame         # inner join 合并
_valuation_cache_path(code) → Path                                    # 缓存路径
_read_valuation_cache(code) → pd.DataFrame                            # 读缓存
```

**分页策略**：`index_dailybasic` 单次限 3000 行，按 3 年一段（~720 行）切分。`index_daily` 同样分页防潜在截断。首/末段自动裁剪到调用方日期范围。

---

## 三、核心信号体系

### 3.1 关键实证发现

基于 Tushare 实盘数据（2014-2026，3000 个交易日）的量化分析：

**发现 1：极端值对买入区信号几乎无影响**
```
PE=23.80 (924前夕), 原始分位 99.4%, 剔除 PE>60 后分位 99.2% → 差异仅 0.2pp
PE=38.84 (10/8尖峰), 原始分位 75.6%, 剔除 PE>60 后分位 67.3% → 差异 8.3pp
```
→ 在 **PE < 35** 的买入区，原始分位是可靠的；极端值只扭曲中间区和高估区的分位。

**发现 2：PE 中枢存在系统性六级漂移**
```
2014-15: 中位数 68.8 | 2016-17: 43.9 | 2018: 38.7
2019-21: 58.5 | 2022-23: 39.3 | 2024-26: 33.7
```
→ 这是比极端值更根本的问题——10 年窗口混合了不同中枢时期的数据。

**发现 3：单调变换（Log/Winsorization）完全无效**
→ 数学上 `(x <= x[-1]).mean()` 只依赖秩，任何单调变换不改变排序，分位不变。

### 3.2 三区三层分位计算体系

```
┌─────────────────────────────────────────────────────────────┐
│                    PE 估值区域划分                            │
├──────────────┬──────────────────┬───────────────────────────┤
│  买入区       │  中间区           │  高估区                    │
│  PE < 35     │  PE 35 ~ 50      │  PE > 50                  │
│              │                  │                           │
│  分位算法:    │  分位算法:        │  分位算法:                  │
│  原始等权分位  │  max(原始, 衰减)  │  时间衰减加权分位            │
│  (极端值影响  │  (保守估计)       │  (修正中枢漂移)             │
│   仅 0.2pp)  │                  │                           │
└──────────────┴──────────────────┴───────────────────────────┘
```

#### Layer 1: 原始等权分位（在 PE < 35 时使用）

```python
def raw_percentile(pe_window):
    """简单、可靠——在低 PE 区极端值影响仅 0.2pp"""
    return (pe_window <= pe_window.iloc[-1]).mean()
```

**适用区间**：`PE_TTM < 35`
**预热**：5 年（1260 个交易日）
**窗口**：滚动 10 年（2520 个交易日）

#### Layer 2: 时间衰减加权分位（在 PE > 35 时使用）

```python
def time_decay_percentile(pe_window, halflife_years=3.0):
    """
    时间衰减加权分位

    半衰期 3 年：
    - 2015 年数据权重约 8%（2026 年视角）
    - 最近 3 年数据权重占 50%

    这是唯一被实证验证有效的方法——
    同时解决尾部污染和中枢漂移两个问题。
    """
    n = len(pe_window)
    lam = np.log(2) / (halflife_years * 252)
    weights = np.exp(-lam * np.arange(n)[::-1])
    weights /= weights.sum()
    return weights[pe_window <= pe_window.iloc[-1]].sum()
```

**适用区间**：`PE_TTM >= 35`
**半衰期**：3 年（可配置参数 `pct_halflife_years`）

#### Layer 3: 绝对 PE 阈值校验（全局生效）

```python
# 双过滤：分位低 + PE 绝对值低 → 才是真便宜
# 防止"分位漏洞"：10 年窗口中 PE 中枢下移导致分位虚高
PE_ABS_CHEAP = 35       # 创业板指：PE_TTM < 35 才算绝对便宜
PE_ABS_EXPENSIVE = 50   # PE_TTM > 50 进入高估警戒区
```

### 3.3 PE 分位计算函数（完整实现规格）

```python
def compute_pe_percentile(pe_series: pd.Series,
                          window_days: int = 2520,
                          min_periods: int = 1260,
                          halflife_years: float = 3.0) -> pd.Series:
    """
    三区三层 PE 分位计算

    Args:
        pe_series: PE_TTM 日频序列（已清洗）
        window_days: 滚动窗口天数，默认 2520（10年）
        min_periods: 最小预热天数，默认 1260（5年）
        halflife_years: 时间衰减半衰期，默认 3 年

    Returns:
        percentile: 分位值（0-1），值越大表示越低估
                   > 0.90 → 极端低估（买入区）
                   0.30-0.70 → 正常区间
                   < 0.15 → 高估（卖出区）
    """
    pe_current = pe_series.iloc[-1]

    if pe_current < 35:
        # 买入区：原始分位可靠
        return (pe_series <= pe_current).mean()
    else:
        # 中间区/高估区：时间衰减分位（取与原始分位的保守值）
        n = len(pe_series)
        lam = np.log(2) / (halflife_years * 252)
        weights = np.exp(-lam * np.arange(n)[::-1])
        weights /= weights.sum()
        td_pct = weights[pe_series <= pe_current].sum()
        raw_pct = (pe_series <= pe_current).mean()
        return max(raw_pct, td_pct)  # 取更贵的估计 → 保守
```

### 3.4 PB 动态加权方案

**设计逻辑**：
- PB 在 A 股比 PE 更稳定（雪球回测：PB 分位年化 7.00% > PE 6.40%）
- 创业板 PB 天然偏高（轻资产高 ROE），不适合固定高权重
- 在 PB 极端低估时放大权重，捕捉 PB-ROE 错配机会

```python
def compute_pb_weight(pb_pct: float, pe_pct: float) -> float:
    """
    PB 动态权重计算

    双层逻辑：
    Layer 1: PB 极端低估 → PB 权重 0.7（捕捉 PB-ROE 错配）
    Layer 2: PB 分位显著低于 PE 分位(差 > 15pp) → PB 权重 0.6（错配预警）
    默认:   PE/PB 等权 0.5
    """
    if pb_pct > 0.90:                    # PB < 10% 分位 → PB 极端低估
        return 0.7
    elif pb_pct - pe_pct > 0.15:         # PB 比 PE 更低估 15pp+
        return 0.6
    else:
        return 0.5                        # 等权
```

### 3.5 综合估值分数

```python
def compute_valuation_score(pe_pct: float, pb_pct: float, pe_abs: float) -> dict:
    """
    综合估值评分

    Returns:
        {
            'score': float,           # 0-1，越高越值得买入
            'zone': str,              # 'buy' | 'hold' | 'reduce' | 'sell'
            'pb_weight': float,       # PB 实际权重
            'pe_abs_cheap': bool,     # PE 绝对值 < 35
            'signal_confidence': str  # 'high' | 'medium' | 'low'
        }
    """
    pb_w = compute_pb_weight(pb_pct, pe_pct)
    pe_w = 1.0 - pb_w

    # 加权综合分位
    composite_pct = pe_w * pe_pct + pb_w * pb_pct

    # 区域判定
    if composite_pct > 0.90 and pe_abs < 35:
        zone = 'buy'
        confidence = 'high'
    elif composite_pct > 0.85 and pe_abs < 35:
        zone = 'buy'
        confidence = 'medium'
    elif composite_pct > 0.85 and pe_abs >= 35:
        # 分位低但绝对值不低 → 置信度降级
        zone = 'buy'
        confidence = 'low'
    elif composite_pct < 0.15:
        zone = 'sell'
        confidence = 'high' if pe_abs > 50 else 'medium'
    elif composite_pct < 0.30:
        zone = 'reduce'
        confidence = 'medium'
    else:
        zone = 'hold'
        confidence = 'medium'

    return {
        'score': composite_pct,
        'zone': zone,
        'pb_weight': pb_w,
        'pe_abs_cheap': pe_abs < 35,
        'signal_confidence': confidence
    }
```

---

## 四、两阶段建仓架构

### 4.1 设计理念

S46 V1 的核心缺陷：**"等待反弹确认再做左侧交易实为追涨"**——924 行情中，等站上 MA20 再建仓，PE 已从 23.80 涨到 28.82（+21%），错过了最深度的估值修复。

V2 修正：**左侧看动能耗尽，右侧看趋势启动**。

### 4.2 阶段一：左侧底仓（潜伏型）

```
┌────────────────────────────────────────────────────────────┐
│                    阶段一：左侧底仓                          │
│                                                            │
│  触发条件（ALL）：                                          │
│  ✅ PE 综合分位 > 90%  AND                                  │
│  ✅ PE_TTM < 35  AND                                       │
│  ✅ EPS 隐含环比（3个月）> -3%                               │
│                                                            │
│  价格确认：梯度权重制（不是 AND）                             │
│  ├─ PE 分位 > 97%（< 3% 分位）：1/3 条件满足即可            │
│  ├─ PE 分位 95-97%（3-5% 分位）：2/3 条件满足即可           │
│  └─ PE 分位 90-95%（5-10% 分位）：3/3 条件全部满足          │
│                                                            │
│  三个价格确认条件：                                          │
│  ① 缩量：成交量 < 20日均量 × 0.5                            │
│  ② 波动收敛：ATR(10) / 收盘价 ≤ 2.2%                        │
│  ③ 不创新低：连续 3 日最低价 ≥ 前 3 日最低价                 │
│                                                            │
│  仓位映射：                                                  │
│  ├─ PE 分位 > 99%（< 1% 分位）：策略资金的 50%               │
│  ├─ PE 分位 97-99%：策略资金的 40%                           │
│  ├─ PE 分位 95-97%：策略资金的 35%                           │
│  └─ PE 分位 90-95%：策略资金的 30%                           │
└────────────────────────────────────────────────────────────┘
```

**价格确认的梯度权重逻辑**：
- PE 分位越极端 → 估值信号本身置信度越高 → 需要更少的价格确认
- 924 前 PE=23.80 对应 0.66% 分位 → 仅需 1/3 条件满足即可建仓
- 核心认知：**极端低估时等待过多的价格确认 = 确定性更高地买在更高的价格**

#### 流动性陷阱检测（左侧建仓前置检查）

**问题**：缩量 + 波动收敛 + 不创新低——这三个条件在跌停板上会同时满足。但此时不是"底部企稳"，而是"流动性枯竭"。

**创业板 ETF（159915）涨跌停限制**：±20%（注册制改革后同步调整）。

```python
def check_liquidity_trap(etf_pct_chg, index_pct_chg, volume, ma_volume):
    """
    流动性陷阱检测 —— 区分"缩量企稳"和"缩量跌停"

    三层检查：
    1. ETF 自身是否触及跌停（-20%）
    2. 指数是否处于恐慌抛售（单日跌幅 > 5%）
    3. 缩量是否伴随显著下跌（卖方枯竭 ≠ 买方进场）

    Returns:
        {'allow': bool, 'reason': str}
    """
    # Layer 1: ETF 跌停板（创业板 ETF = ±20%）
    if etf_pct_chg <= -0.198:
        return {'allow': False, 'reason': 'ETF触及跌停(-20%)，流动性锁死，禁止建仓'}

    # Layer 2: 指数级恐慌抛售
    if index_pct_chg <= -0.05:
        return {'allow': False, 'reason': f'指数单日跌{index_pct_chg:.1%}，恐慌抛售中，缩量信号不可靠'}

    # Layer 3: 缩量 + 显著下跌（非跌停但跌幅可观）
    # 核心区分：缩量横盘/微跌 = 供给枯竭（底部信号 ✓）
    #           缩量大跌 = 流动性枯竭（陷阱 ✗）
    if volume < ma_volume * 0.5 and etf_pct_chg <= -0.03:
        return {'allow': False, 'reason': f'缩量({volume/ma_volume:.1%})伴随下跌({etf_pct_chg:.1%})，卖方枯竭≠买方进场'}

    # 正常缩量企稳
    if volume < ma_volume * 0.5:
        return {'allow': True, 'reason': '缩量企稳，供给耗尽'}

    return {'allow': True, 'reason': '正常'}
```

### 4.3 阶段二：右侧加仓（确认型）

```
┌────────────────────────────────────────────────────────────┐
│                    阶段二：右侧加仓                          │
│                                                            │
│  前置条件：阶段一已执行（已有底仓）                            │
│                                                            │
│  触发条件（ALL）：                                          │
│  ✅ 放量突破 MA20（成交量 > 20日均量 × 1.2）                 │
│  ✅ 周涨幅 > 5%                                             │
│  ✅ PE 分位 < 30%（估值容忍上限——PE 已不便宜则不追）          │
│  ✅ MA20 突破质量检查通过（见下方）                           │
│                                                            │
│  仓位：策略资金的 20-30%                                     │
│  总仓位上限：策略资金的 70%（左侧底仓 + 右侧加仓）             │
└────────────────────────────────────────────────────────────┘
```

**MA20 突破质量检查（防"吻别"假突破）**：

暴跌反弹初期，价格远离 MA20，第一次突破 MA20 往往是"反弹到均线压力位"而非"趋势启动"。核心区分方法不是乖离率绝对值，而是**此前价格在 MA20 下方停留了多久**。

```python
def check_ma20_breakout_quality(close_series, ma20_series, lookback=20):
    """
    MA20 突破质量检查

    核心逻辑：
    - 首次从长期压制下突破 MA20 → 假突破概率高（均线是阻力）
    - 二次/三次回踩后突破 → 可靠的趋势启动（均线已转为支撑）

    Returns:
        {'quality': 'LOW' | 'MEDIUM' | 'HIGH', 'action': str, 'reason': str}
    """
    days_below = (close_series[-lookback:] < ma20_series[-lookback:]).sum()

    if days_below >= 15:
        # 过去 20 天有 15+ 天在 MA20 下方 → 首次触及 MA20
        # 这更像是"均值回归触及压力位"，不是"趋势突破"
        return {
            'quality': 'LOW',
            'action': 'WAIT',
            'reason': f'首次触及MA20（此前{days_below}/{lookback}日在下方），等待回踩确认后再加仓'
        }
    elif days_below >= 8:
        # 8-14 天在下方 → 可能是二次测试，半仓加注
        return {
            'quality': 'MEDIUM',
            'action': 'ADD_HALF',
            'reason': f'MA20附近震荡中（此前{days_below}/{lookback}日在下方），半仓加注'
        }
    else:
        # 价格已围绕 MA20 运行多日 → MA20 已从阻力变为支撑
        return {
            'quality': 'HIGH',
            'action': 'ADD_FULL',
            'reason': f'MA20已确认支撑（此前仅{days_below}/{lookback}日在下方），正常加仓'
        }
```

### 4.4 建仓状态机

```
                    ┌──────────┐
                    │  WAITING │  等待估值信号触发
                    └────┬─────┘
                         │ PE分位>90% + PE<35 + EPS>-3%
                         ▼
                    ┌──────────┐
                    │ PHASE_1  │  左侧底仓已建
                    │  ACTIVE  │  监控右侧触发条件
                    └────┬─────┘
                         │ 放量破MA20 + 周涨>5% + PE分位<30%
                         ▼
                    ┌──────────┐
                    │ PHASE_2  │  右侧加仓已建
                    │  ACTIVE  │  满仓持有，等待出场信号
                    └────┬─────┘
                         │ 出场条件触发
                         ▼
                    ┌──────────┐
                    │ EXITING  │  逐步减仓
                    └──────────┘
```

### 4.5 EPS 隐含趋势计算

```python
def compute_implied_eps(total_mv: pd.Series, pe_ttm: pd.Series) -> pd.Series:
    """
    隐含 EPS = 总市值 / PE_TTM

    比 Price/PE 更准确：使用 total_mv（总市值）消除了
    成分股变更导致的价格-PE 不同步问题。

    Tushare index_dailybasic 提供 total_mv 字段。
    """
    return total_mv / pe_ttm

def compute_eps_trend(implied_eps: pd.Series, lookback_days: int = 63) -> float:
    """
    EPS 隐含趋势（3 个月环比，约 63 个交易日）

    Returns:
        float: EPS 环比变动率（-1.0 ~ +∞）
               < -0.10 → 盈利严重恶化（价值陷阱警报）
               < -0.03 → 轻微恶化（降低仓位）
               > -0.03 → 稳定（正常建仓）
    """
    if len(implied_eps) < lookback_days:
        return 0.0  # 数据不足，不做判断
    return implied_eps.iloc[-1] / implied_eps.iloc[-lookback_days] - 1
```

---

## 五、出场信号体系

### 5.1 三层出场机制

```
┌─────────────────────────────────────────────────────────────┐
│                     出场信号优先级                            │
├──────────────────┬──────────────────┬───────────────────────┤
│  优先级 1 (紧急)  │  优先级 2 (位移)  │  优先级 3 (常规)       │
│  纯拔估值脉冲     │  分位位移出场     │  分位阈值出场          │
│  立即响应        │  捕捉急速修复     │  捕捉慢牛过估          │
└──────────────────┴──────────────────┴───────────────────────┘
```

### 5.2 优先级 1：紧急脉冲出场

```python
# 触发条件
if (PE周涨幅 > 0.15) and (EPS周涨幅 < 0.02):
    # PE 暴涨但 EPS 几乎不动 → 纯拔估值脉冲
    # 案例：924 行情中 9.27→10.8，PE 跳升 34.8%，EPS 隐含 -17%
    action = "紧急减仓至 ≤ 30%"
```

### 5.3 优先级 2：分位位移出场（核心创新，带基线重置）

**设计理由**：
- 924 行情 PE 最高 28% 分位，传统 > 70% 分位出场**完全不会触发**
- +30pp 规则：入场 0.66% → 10/8 达 28%，触发减仓 → 完美捕捉急速修复
- 也覆盖 2018 底→2020（慢修复，2 年涨 30pp）和 2020 疫情底（V 型，3 个月涨 30pp）

**基线重置机制**（防反复触发）：

29pp → 31pp → 29pp → 31pp 的震荡会把仓位反复削减。解决方案不是冷却期，而是**出场后重置基线为出场时分位**——使后续判断基于新基线，而非原始入场基线。

```python
def displacement_exit_with_reset(entry_pct, current_pct, last_exit_pct=None):
    """
    分位位移出场（带基线重置）

    关键：每次出场后，将基线重置为出场时的分位。
    这样后续的位移判断基于新基线，不会在 30pp 附近反复摩擦。

    效果对比：
    - 冷却期方案：33%→28%→33%，两次触发 → 仓位只剩25%（过度减仓）
    - 基线重置方案：33%触发（基线重置至33%），28%→33%仅5pp位移，不触发（正确）
    - 持续上涨：33%触发后，继续涨至63%，位移30pp+，再次触发（合理）
    """
    if last_exit_pct is not None:
        # 已出过场：用上次出场时分位作为新基线
        baseline = last_exit_pct
    else:
        # 首次出场：用入场时分位
        baseline = entry_pct

    displacement = current_pct - baseline

    if displacement > 0.30:
        return {
            'action': 'REDUCE_50%',
            'new_baseline': current_pct,   # 重置基线
            'displacement': displacement,
            'reason': f'分位位移 {displacement:.1%} > 30pp，减仓50%并重置基线'
        }

    return {
        'action': 'HOLD',
        'displacement': displacement
    }

# 重置条件：当 PE 分位回到低估区（composite_pct > 0.50）时，清除 last_exit_pct
# 此时可以重新开始一个完整的建仓→出场周期
def should_reset_cycle(current_pct):
    return current_pct > 0.50  # PE 回到历史中位以下 = 便宜区间
```

### 5.4 优先级 3：常规阈值出场

```python
if pe_composite_pct < 0.30:    # PE 分位 < 30%（即 PE 处于历史较高水平）
    action = "减仓 50%"

if pe_composite_pct < 0.15:    # PE 分位 < 15%（即 PE 处于历史很高水平）
    action = "清仓"
```

**注意**：此处使用时间衰减加权分位（Layer 2），因为 PE > 35 区域原始分位被极端值扭曲约 8pp。

### 5.5 出场条件汇总

| 条件 | 动作 | 优先级 | 防抖机制 |
|:---|:---|:---|:---|
| PE 周涨 > 15% AND EPS 周涨 < 2% | 紧急减仓至 ≤ 30% | 1 | 无（紧急响应，不需防抖）|
| 入场后 PE 分位位移 > +30pp | 减仓 50%，基线重置为当前分位 | 2 | 基线重置：后续判断基于新基线 |
| PE 分位 < 30% | 减仓 50% | 3 | 分位回到 > 50% 后才解锁下一轮 |
| PE 分位 < 15% | 清仓 | 3 | 同上 |
| EPS 隐含环比 < -10% | 减仓 50%（价值陷阱）| 3 | 连续 2 个季度确认后执行 |
| PE > 50 AND PE 分位 < 10% | 禁止开仓（数据偏差防护）| 全局 | — |
| 数据新鲜度异常 / PE 跳变背离 | 策略暂停 | 全局 | — |
| ETF 跌停(-20%) / 指数恐慌(-5%) | 禁止左侧建仓 | 全局 | — |

---

## 六、资金管理

### 6.1 风险预算共享制

估值策略**不设固定专属资金池**，改为从组合总现金池动态申请。

```
总现金池 = 海龟未使用资金 + 现金储备

估值策略触发信号时：
├─ 单次申请上限：总资金的 15%
├─ 累计持仓上限：总资金的 21%（对应策略资金 70%）
├─ 实际获得：min(申请额, 现金池 × 0.7, 总资金 × 0.15)
└─ 现金保留：总资金的 30% 作为应急储备，不被估值策略占用
```

### 6.2 信号时序与冲突解决

**时序规则**（消除"未来函数"歧义）：

```
T 日 15:00  收盘，数据就绪
T 日 15:30  同时计算海龟信号和估值信号（基于同一批 T 日数据）
T 日 16:00  资金分配决策（按以下优先级规则）
T+1 日 09:30  开盘执行
```

**不存在未来函数问题**——两个策略的信号基于同一批 T 日数据计算，时序完全确定。

**同日触发冲突解决**（梯度优先级）：

```python
def resolve_same_day_conflict(turtle_request, valuation_request, cash_pool):
    """
    T 日收盘后：海龟和估值同时触发时的资金分配

    梯度优先级（PE 分位越极端 → 估值优先权越高）：
    """
    pe_pct = valuation_request.pe_percentile

    if pe_pct > 0.97:                         # PE < 3% 分位
        # 极端低估 → 估值绝对优先
        val_alloc = min(valuation_request.amount, cash_pool * 0.7)
        turtle_alloc = min(turtle_request.total, cash_pool - val_alloc)

    elif pe_pct > 0.90:                       # PE 3-10% 分位
        # 正常低估 → 对半分配
        val_alloc = min(valuation_request.amount, cash_pool * 0.5)
        turtle_alloc = min(turtle_request.total, cash_pool * 0.5)

    else:
        # 估值信号不会在 PE > 10% 时产生
        turtle_alloc = min(turtle_request.total, cash_pool * 0.7)
        val_alloc = 0

    return {
        'valuation': val_alloc,
        'turtle': turtle_alloc,
        'cash_remaining': cash_pool - val_alloc - turtle_alloc,
        'rule': 'extreme_priority' if pe_pct > 0.97 else 'equal_split'
    }
```

### 6.3 简化备选方案

如果不希望引入复杂的优先级逻辑，可用更简洁的**权重上限法**：

```python
# 估值策略最大仓位 = 30% - 已被海龟占用的部分
valuation_max_position = min(0.30, 1.0 - turtle_position)
```

此方案让估值策略"只拿别人不用的钱"，不设优先级规则，避免了回测中的时序歧义。V1 建议先用简化方案，验证估值策略独立有效性后再决定是否需要复杂的优先级逻辑。

> **实盘视角**：双策略体系（海龟+估值）比三策略体系更简洁——估值策略的资金来源只有海龟的闲置资金和现金储备，冲突场景减少一半。

---

## 七、风控机制

### 7.1 价值陷阱防护

```python
def value_trap_check(implied_eps, entry_date, current_date):
    """
    价值陷阱三级检测

    Merrill Lynch 三因子框架：
    - Earnings Yield 低 → PE 分位低，看起来便宜
    - Price Momentum 负 → 价格持续下跌
    - Earnings Momentum 负 → 盈利持续恶化
    → 同时满足 = 价值陷阱
    """
    eps_change = implied_eps.iloc[-1] / implied_eps.loc[entry_date] - 1
    price_change = close.iloc[-1] / close.loc[entry_date] - 1

    # Level 1: EPS 严重恶化
    if eps_change < -0.10:
        return {'action': 'REDUCE_50%', 'reason': '盈利崩塌型低估'}

    # Level 2: 价格 + 盈利双杀
    if eps_change < -0.03 and price_change < -0.10:
        return {'action': 'REDUCE_30%', 'reason': '可能的价值陷阱'}

    # Level 3: "飞刀不接"原则
    if price_change < -0.20 and len(持仓) == 0:
        return {'action': 'DELAY_ENTRY', 'reason': '价格仍在加速下跌'}

    return {'action': 'OK'}
```

### 7.2 时间止损

```python
def time_stop_check(holding_days, implied_eps, pe_pct_start, pe_pct_current):
    """
    时间止损：持有 > 6 个月无进展时审视

    分层处理（避免在底部横盘+盈利缓慢改善时误割）：
    """
    if holding_days < 126:  # 6 个月 ≈ 126 个交易日
        return 'OK'

    pe_pct_change = pe_pct_current - pe_pct_start

    if abs(pe_pct_change) < 0.05:  # 分位无实质变化
        eps_change = compute_eps_trend(implied_eps)

        if eps_change > 0.03:
            return 'OK'          # 盈利改善 → 等待戴维斯双击
        elif eps_change > -0.03:
            return 'WARN'        # 横盘等风来 → 保持警惕但不行动
        else:
            return 'REDUCE_50%'  # 盈利恶化 → 价值陷阱，减仓
```

### 7.3 数据偏差防护

```python
# 反向风控：防止 Tushare 数据源系统性偏差
if pe_ttm_current > 50 and pe_pct < 0.10:
    # Tushare PE > 50 且分位显示 < 10%（即 PE 处于历史高位）
    # 但若分位计算有偏差，这可能不准确
    # 用价格反推 PE 做交叉验证
    pe_price_implied = close[-1] / close[-252] * pe_ttm[-252]
    if abs(pe_ttm_current - pe_price_implied) / pe_ttm_current > 0.30:
        flag = 'DATA_SUSPICIOUS'
        action = 'PROHIBIT_OPENING'  # 禁止开仓
    else:
        action = 'ALLOW'  # PE 与价格变动一致，数据可信
```

---

## 八、辅助信号：汇金/平准基金

### 8.1 定位

**宏观层面的策略开关，非微观交易触发器。**

### 8.2 数据可获得性

| 数据 | 频率 | 滞后 | 可用性 |
|:---|:---|:---|:---|
| 汇金季报持仓 | 季度 | 2-3 个月 | 不可做实时信号 |
| 汇金紧急公告 | 不定期 | 即时 | 仅极端行情（历史上 5 次）|
| **ETF 场内份额变动** | **每日** | **T+1** | ✅ **准实时可用** |

### 8.3 实现方案

```python
def national_team_signal(etf_share_change: float, pe_pct: float) -> dict:
    """
    汇金/平准基金动向监测

    监测标的：4 只汇金主力沪深 300 ETF 的日频份额变动
    - 华泰柏瑞 510300
    - 易方达 510310
    - 华夏 510330
    - 嘉实 159919

    仅在 PE 综合分位 > 95%（极端低估区）生效
    """
    if pe_pct < 0.95:
        return {'active': False, 'reason': '非极端低估区，平准信号不生效'}

    if etf_share_change > 0.02:    # ETF 份额日增超 2%
        return {
            'active': True,
            'bias': +1,             # 加分：可降低价格确认要求 1 级
            'interpretation': '汇金大概率在买，政策底确认'
        }
    elif etf_share_change < -0.02:
        return {
            'active': True,
            'bias': -1,             # 减分：需要更多价格确认
            'interpretation': '汇金可能在减，需警惕'
        }

    return {'active': True, 'bias': 0}
```

### 8.4 使用方式

不产生独立交易信号，仅作为建仓确认的**加/减分层**：

```
PE 分位 > 97% + 汇金偏向 +1 → 价格确认要求降为 0/3（直接建仓）
PE 分位 > 97% + 汇金偏向 -1 → 价格确认要求升为 2/3
```

---

## 九、模块架构与数据流

### 9.1 模块层级

```
Layer 1 (工具):  config_loader → valuation_config 配置节
                src/valuation_pipeline.py → fetch_index_valuation()  ← NEW（独立文件，Phase 1 已完成）

Layer 2 (计算):  src/valuation_core.py  ← NEW
                ├─ clean_valuation_data()
                ├─ compute_pe_percentile()        # 三区三层分位
                ├─ compute_pb_percentile()
                ├─ compute_valuation_score()      # PB 动态加权综合
                ├─ compute_implied_eps()          # EPS 隐含趋势
                ├─ compute_eps_trend()
                └─ value_trap_check()

Layer 3 (策略):  strategies/valuation_strategy.py  ← NEW
                ├─ ValuationStrategy (纯 pandas 实现)
                ├─ 两阶段建仓状态机
                ├─ 三层出场机制
                ├─ 资金申请接口
                └─ 每日信号输出

Layer 4 (脚本):  scripts/run_valuation.py  ← NEW
                ├─ 独立回测引擎
                ├─ 参数扫描
                └─ 极端行情穿越测试
```

### 9.2 数据流

```
Tushare API
  │
  ├─ index_dailybasic (PE_TTM, PB, total_mv)
  └─ index_daily (close, amount, pct_chg)
      │
      ▼
  valuation_pipeline.fetch_index_valuation()
      │
      ├─ 分页拉取 + 缓存 Parquet
      ├─ 估值列数值类型转换
      └─ 核心列存在性校验
          │
          ▼
  valuation_core.py
      │
      ├─ PE 三区三层分位计算
      ├─ PB 分位计算
      ├─ 综合估值分数 (PB 动态加权)
      ├─ EPS 隐含趋势
      └─ 价值陷阱检测
          │
          ▼
  valuation_strategy.py
      │
      ├─ 两阶段建仓状态机
      ├─ 出场信号判断
      ├─ 资金申请 (风险预算共享)
      └─ 每日信号输出
          │
          ▼
  run_valuation.py → 回测结果 (results/valuation/)
```

### 9.3 配置文件扩展

```yaml
# config/turtle_config.yaml 新增节

valuation:
  # 数据
  indices:
    - code: "399006.SZ"
      name: "创业板指"
      role: "primary"
    - code: "000300.SH"
      name: "沪深300"
      role: "auxiliary"
    - code: "000905.SH"
      name: "中证500"
      role: "auxiliary"
    - code: "000016.SH"
      name: "上证50"
      role: "auxiliary"

  # 分位计算
  percentile:
    window_days: 2520        # 10 年滚动窗口
    min_periods: 1260        # 5 年预热
    halflife_years: 3.0      # 时间衰减半衰期
    pe_abs_cheap: 35         # PE 绝对低估阈值
    pe_abs_expensive: 50     # PE 绝对高估阈值

  # 建仓参数
  entry:
    pe_pct_threshold: 0.90   # PE 分位阈值（> 90% = 低估）
    pb_pct_threshold: 0.85   # PB 分位阈值
    eps_deterioration_max: -0.03  # EPS 最大允许恶化

  # 左侧底仓
  phase1:
    positions:               # PE 分位 → 策略资金仓位映射
      - pe_pct: 0.99         # > 99% 分位 → 50%
        position: 0.50
      - pe_pct: 0.97         # > 97% 分位 → 40%
        position: 0.40
      - pe_pct: 0.95         # > 95% 分位 → 35%
        position: 0.35
      - pe_pct: 0.90         # > 90% 分位 → 30%
        position: 0.30
    price_confirmations:     # 价格确认条件
      volume_contraction: 0.5     # 成交量 < 20日均量 × 0.5
      atr_threshold: 0.022        # ATR(10)/收盘价 ≤ 2.2%
      no_new_low_days: 3          # 连续 N 日不创新低
    gradient:                 # 梯度权重（PE分位 → 需要满足的条件数）
      - pe_pct: 0.97
        required: 1           # PE < 3% 分位: 1/3 条件即可
      - pe_pct: 0.95
        required: 2           # PE 3-5% 分位: 2/3 条件
      - pe_pct: 0.90
        required: 3           # PE 5-10% 分位: 3/3 条件

  # 右侧加仓
  phase2:
    ma_breakout: 20           # 放量突破 MA20
    volume_multiplier: 1.2    # 成交量 > MA(vol,20) × 1.2
    weekly_return_min: 0.05   # 周涨幅 > 5%
    pe_pct_tolerance: 0.30    # PE 分位容忍上限（< 30% 才加仓）
    position: 0.25            # 右侧加仓比例（策略资金的 25%）
    total_position_cap: 0.70  # 总仓位上限

  # 出场
  exit:
    pulse_pe_weekly: 0.15     # PE 周涨 > 15%
    pulse_eps_weekly: 0.02    # EPS 周涨 < 2%
    pulse_reduce_to: 0.30     # 脉冲出场减至 30%
    displacement_pp: 0.30     # 分位位移 > 30pp 减仓
    displacement_reduce: 0.50 # 减仓比例
    overvalued_pct: 0.30      # 分位 < 30% 减仓 50%
    extreme_overvalued_pct: 0.15  # 分位 < 15% 清仓

  # 风控
  risk:
    eps_collapse: -0.10       # EPS 环比恶化 > 10% → 减半仓
    eps_warning: -0.03        # EPS 环比恶化 > 3% → 降仓位
    time_stop_days: 126       # 6 个月时间止损线
    max_drawdown_pause: -0.20 # 建仓后浮亏 > 20% → 暂停加仓
    suspicious_pe_divergence: 0.30  # PE 与价格变动背离阈值

  # 资金管理
  capital:
    single_application_cap: 0.15   # 单次申请上限（总资金 %）
    cumulative_position_cap: 0.21  # 累计持仓上限（总资金 %）
    cash_reserve: 0.30             # 应急现金保留比例

  # 辅助信号
  national_team:
    etf_monitor_codes:        # 汇金主力 ETF
      - "510300.SH"
      - "510310.SH"
      - "510330.SH"
      - "159919.SZ"
    share_change_threshold: 0.02   # ETF 份额日变动阈值
    active_only_extreme: true      # 仅在 PE > 95% 分位时生效

  # 数据质量监控
  data_health:
    max_staleness_trading_days: 3  # 数据新鲜度最大落后交易日
    pe_jump_normal_interval: 0.25  # PE跳变熔断（常规间隔 ≤3天）
    pe_jump_holiday_interval: 0.50 # PE跳变熔断（长假间隔 4-10天）
    pe_price_divergence_max: 0.20  # PE-价格背离最大容忍

  # 执行可行性
  execution:
    liquidity_trap:
      etf_limit_down: -0.198       # 创业板 ETF 跌停线（±20%）
      index_panic: -0.05           # 指数恐慌阈值
      volume_decline_panic: -0.03  # 缩量+下跌判定线
    ma20_breakout:
      quality_lookback: 20         # 突破质量回溯天数
      first_touch_days: 15         # 首次触及判定线（N天在MA20下方）
      second_test_days: 8          # 二次测试判定线
    slippage_low_volume: 0.01      # 缩量建仓额外滑点
    drawdown_fuse: -0.15           # 组合净值回撤熔断（单策略）
```

---

## 十、回测计划

### 10.1 分段验证策略

PE/PB 数据从 **2010-06-01** 开始（约 16 年），5 年分位窗口在 **2015 年中**即有效，10 年分位窗口在 **2020 年中**有效：

| 回测区间 | 类型 | 测试目标 |
|:---|:---|:---|
| **2015 全年** | **完整回测** | **核心：PE 从 137 崩溃到 55，唯一覆盖极限牛→熊完整周期的回测** |
| 2016-2017 | 完整回测 | 估值回落中的策略静默期行为 |
| 2018 全年 | 完整回测 | 全年阴跌中策略分批节奏 |
| 2020.01-2020.06 | 完整回测 | V 型反转中建仓时机 |
| 2021 全年 | 完整回测 | PE 达 74 倍高点，出场信号验证 |
| 2022-2024.09 | 完整回测 | 长期底部横盘中的持仓管理 |
| **2024.09-2025.01** | **完整回测** | **核心：924 行情穿越测试** |
| 2025.01-2026.07 | 完整回测 | 估值修复后的策略行为 |

### 10.2 参数敏感性扫描

| 参数 | 扫描范围 | 步长 |
|:---|:---|:---|
| PE 分位阈值 | 0.85, 0.88, 0.90, 0.92, 0.95 | — |
| PE 绝对低估阈值 | 30, 32, 35, 38, 40 | — |
| 分位位移出场 | +20pp, +25pp, +30pp, +35pp, +40pp | — |
| 时间衰减半衰期 | 2, 3, 4, 5 年 | — |
| 左侧仓位比例 | 30/35/40/50%, 25/30/35/40%, 20/25/30/35% | — |
| 左侧价格确认梯度 | 1/2/3, 1/2/2, 2/2/3 | — |
| 右侧 PE 容忍上限 | 20%, 25%, 30%, 35% | — |

### 10.3 基准对比

| 基准 | 说明 |
|:---|:---|
| B1 买入持有 | 全时段满仓创业板 ETF |
| B1-50 半仓持有 | 50% 创业板 ETF + 50% 国债 |
| B2 定投 | 每月定额买入创业板 ETF |
| 纯估值（无价格确认）| PE 分位 < 10% 即买，> 70% 即卖 |
| **本文方案** | 两阶段建仓 + 三区三层分位 + 三层出场 |

### 10.4 评估指标

- 年化收益率 (CAGR)
- 最大回撤 (MDD) 及持续时间
- 夏普比率 / Sortino 比率
- 胜率（盈利交易占比）
- 盈亏比
- 资金利用率（平均仓位）
- 924 行情捕捉率：建仓时机是否在 9/19 附近？是否有右侧加仓？

---

## 十一、V1 实施范围

### ✅ V1 做

1. **数据管道** ✅（Phase 1 已完成）：`src/valuation_pipeline.py` — 4 指数 PE/PB/总市值日频数据 + Parquet 缓存
   - `fetch_index_valuation()` 公开接口 + `get_valuation_summary()` 便捷查询
   - 分页拉取（3年/段）+ try/except 隔离 + 缓存回退 + 估值列类型转换 + 核心列校验
   - 缓存目录 `data/index_valuation/{code}.parquet`，独立于海龟数据管道
2. **数据质量监控**（实盘必备，后续 Phase）：
   - 数据新鲜度检查（落后 > 3 个交易日 → 暂停）
   - PE 跳变熔断（PE-价格背离检测，区分常规间隔和长假间隔）
3. **估值核心模块** (`src/valuation_core.py`)：
   - 数据清洗流水线
   - 三区三层 PE 分位计算（原始 + 时间衰减）
   - PB 动态加权综合估值分数
   - EPS 隐含趋势计算
   - 价值陷阱检测
4. **估值策略** (`strategies/valuation_strategy.py`)：
   - 两阶段建仓状态机
   - 三层出场机制 + 基线重置（防反复触发）
   - 价格确认梯度权重
   - 时间止损
   - **执行可行性检查**：
     - 流动性陷阱检测（ETF 跌停/指数恐慌/缩量下跌三层）
     - MA20 突破质量检查（首次触及 vs 多次确认）
5. **回测脚本** (`scripts/run_valuation.py`)：
   - 独立回测引擎（纯 pandas，不耦合 Backtrader）
   - 参数配置化
   - **滑点模拟**：缩量建仓时滑点 > 1%（流动性折价）
   - **组合净值回撤熔断**：单策略回撤 > 总资金 15% → 暂停
6. **极端行情穿越测试**：2018/2020/2022-2024/2024-924/2025-2026
7. **基准对比**（买入持有、定投、纯估值无确认）
8. **单元测试**：`tests/test_valuation_core.py`（分位计算/评分/EPS趋势）

### ❌ V1 不做（V2 迭代）

- 完整 PB-ROE 错配检测（残差法，ROE 数据源待解决）
- 与海龟策略的集成回测（先独立验证估值策略有效性）
- 汇金/平准基金信号集成（V1 只做 ETF 份额数据管道，不做信号融合）
- MA250 过滤（S44 已否定 MA250 均线过滤有效性）
- Kalman 滤波 PE 中枢追踪
- PEG 估值框架
- 实盘部署
- 期货/跨境品种估值

---

## 十二、关键参数汇总

| 参数 | 默认值 | 说明 | 可配置 |
|:---|:---|:---|:---|
| `data_start` | 2010-06-01 | PE/PB 数据起始（创业板指发布日）| — |
| `min_periods` | 1260 | 最小预热天数（5 年）| ✅ |
| `halflife_years` | 3.0 | 时间衰减半衰期 | ✅ |
| `pe_abs_cheap` | 35 | 创业板 PE 绝对低估阈值 | ✅ |
| `pe_abs_expensive` | 50 | PE 绝对高估警戒线 | ✅ |
| `pe_pct_threshold` | 0.90 | PE 分位建仓阈值 | ✅ |
| `pb_pct_threshold` | 0.85 | PB 分位辅助阈值 | ✅ |
| `eps_deterioration_max` | -0.03 | EPS 环比最大允许恶化 | ✅ |
| `phase1_positions` | [0.50, 0.40, 0.35, 0.30] | PE分位→仓位映射 | ✅ |
| `phase1_gradient` | [1, 2, 3] | 价格确认梯度 | ✅ |
| `phase2_position` | 0.25 | 右侧加仓比例 | ✅ |
| `phase2_pe_tolerance` | 0.30 | 右侧 PE 容忍上限 | ✅ |
| `exit_displacement_pp` | 0.30 | 分位位移出场阈值 | ✅ |
| `exit_overvalued_pct` | 0.30 | 常规减仓阈值 | ✅ |
| `exit_extreme_pct` | 0.15 | 清仓阈值 | ✅ |
| `single_application_cap` | 0.15 | 单次资金申请上限 | ✅ |
| `cumulative_position_cap` | 0.21 | 累计持仓上限 | ✅ |
| `data_max_staleness_days` | 3 | 数据新鲜度最大落后交易日 | ✅ |
| `pe_jump_threshold_normal` | 0.25 | PE 跳变熔断（常规间隔）| ✅ |
| `pe_jump_threshold_holiday` | 0.50 | PE 跳变熔断（长假间隔）| ✅ |
| `pe_price_divergence_max` | 0.20 | PE-价格背离最大容忍 | ✅ |
| `liquidity_trap_index_panic` | -0.05 | 指数恐慌阈值（单日跌幅）| ✅ |
| `ma20_breakout_quality_lookback` | 20 | MA20 突破质量回溯天数 | ✅ |
| `displacement_baseline_reset` | true | 分位位移出场基线重置 | ✅ |
| `slippage_low_volume` | 0.01 | 缩量建仓额外滑点 | ✅ |
| `drawdown_fuse` | -0.15 | 组合净值回撤熔断线 | ✅ |

---

## 十三、待解问题与 V2 方向

### 13.1 已知待解问题

| 问题 | 影响 | V2 方向 | 状态 |
|:---|:---|:---|:---|
| 2015 年完整回测 | ~~缺少极限行情验证~~ | ~~万得/理杏仁~~ → Tushare 分页拉取 | ✅ **已解决**（2010-06-01 起）|
| ROE 日频数据 | V1 EPS 隐含不如真实 ROE | `fina_indicator` + `index_weight` 构造（季度） | ⏳ V2 |
| PE 中枢漂移加剧 | 10 年后分位含义下降 | Kalman 滤波追踪时变中枢 | ⏳ V2 |
| 时间衰减半衰期最优值 | 需实测确定 | 参数扫描确定 | ⏳ V2 |
| 创业板注册制后估值逻辑变化 | 35 倍阈值可能偏保守 | PEG 框架补充 | ⏳ V2 |
| 未盈利企业上市 | PE 对未盈利企业无意义 | 引入 PB 或 PS 辅助 | ⏳ V2 |

### 13.2 V2 升级方向（按优先级）

1. **Kalman 滤波 PE 中枢追踪**：替代固定 3 年半衰期，自适应调整衰减速度
2. **PEG 估值框架**：PE/growth，解决绝对 PE 阈值在不同增速环境下的合理性问题
3. **PB-ROE 残差法**：华泰证券 2021 框架，更精确的错配检测
4. **ERP（股权风险溢价）替代 PE**：`1/PE - 10Y国债收益率`，含利率环境信息
5. **汇金 ETF 份额信号融合**：从定性辅助升级为定量权重调节
6. **跨品种估值轮动**：在多个指数间动态选择最便宜的

---

## 十四、讨论记录

### 2026-07-21：V1 设计讨论（详见 S46_valuation_strategy_design.md）

- 策略定位：从"海龟附属"→"独立策略"
- PE 分位 + 价格行为双重确认
- V1 范围界定

### 2026-07-22：V2 升级讨论（本文档）

**参与**：用户 + Claude

**关键决策节点**：

1. **两阶段建仓**：S46 V1 的"反弹确认"被否定 → 改为"左侧动能耗尽 + 右侧趋势启动"

2. **PB 动态加权**：三层逻辑 → 极端低估 PB 权重 0.7 / PB-PE 错配 0.6 / 默认等权

3. **绝对 PE 阈值**：V1 固定 35 倍，实证验证了必要性（中枢下移导致分位虚高）

4. **分位位移出场**：+30pp 规则，实证覆盖了 924（6 天触发）、2018 底→2020（2 年触发）、2020 疫情底（3 个月触发）

5. **极端值处理的实证修正**（重要）：
   - 初始方案：Winsorization (median×3)
   - 实证发现：单调变换完全无效（秩不变）；极端值对买入区分位影响仅 0.2pp
   - 最终方案：买入区原始分位 → 中间区时间衰减 → 高估区时间衰减 + 绝对阈值
   - 时间衰减加权分位是唯一被证实有效的方法

6. **资金池设计**：从"固定 20%"→"风险预算共享制"→ 建议 V1 先用简化权重上限法

7. **PE 数据清洗**：
   - 裁剪范围从 [0, 200] 修正为 [1, 300]
   - ffill() 改为 interpolate(limit=5) + rolling_median fallback
   - 新增价格交叉验证

8. **三区三层分位体系**：买入区/中间区/高估区使用不同分位算法，匹配极端值的差异化影响

**产出**：本文档（S46 估值策略设计 V2.0）

### 2026-07-22（续）：逻辑漏洞修正 + 实盘防护

**参与**：用户 + Claude

**本轮内容**：

1. **N 字结构退出实盘体系**：保留代码和回测能力，实盘仅海龟 + 估值双策略

2. **数据异常兜底**：
   - 数据新鲜度检查：落后 > 3 个交易日 → 策略暂停
   - PE 跳变熔断：按 PE-价格背离判断（而非固定阈值），区分常规间隔和长假间隔

3. **逻辑漏洞修正**：
   - **漏洞 1（流动性陷阱）**：缩量 + 跌停 ≠ 企稳。修正：创业板 ETF ±20% 跌停检测 + 指数恐慌（-5%）+ 缩量下跌三层过滤
   - **漏洞 2（乖离率陷阱）**：首次突破 MA20 ≠ 趋势启动。修正：用"MA20 下方持续天数"替代乖离率，首次触及等待回踩
   - **漏洞 3（反复触发）**：分位在 30pp 上下震荡 → 仓位被反复削减。修正：基线重置——出场后将基线重置为出场时分位
   - **漏洞 4（时序歧义）**：明确 T 日收盘计算 → T+1 开盘执行；同日触发用梯度优先级

4. **实盘防护补充**：
   - 缩量建仓滑点 > 1%
   - 组合净值回撤熔断（单策略 > -15%）

5. **创业板 ETF（159915）涨跌停修正**：±20%（非 ±10%），与创业板注册制改革同步

**产出**：设计文档 V2.0 最终版

### 2026-07-22（续二）：数据源扩展验证

**参与**：用户 + Claude

**内容**：

1. **PE 数据追溯**：Tushare 官方文档确认 `index_dailybasic` 数据从 2004 年开始。之前只拉到 2014 年的原因是单次调用 3000 行硬限制截断了早期数据。**通过按年份分页拉取（每段 ≤3 年），成功获取创业板指 2010-06-01 的完整 PE/PB 历史**。

2. **ROE 数据源**：`fina_indicator` 接口提供个股 ROE（2000 积分），`index_weight` 提供成分股权重（2000 积分）。理论上可以构造指数级 ROE，但涉及成分股变更/财报频率对齐/月度权重等复杂问题。V1 坚持 EPS 隐含方案，V2 再投入。

3. **回测覆盖度升级**：2015 年从"极端压力测试"→"完整回测"——16 年数据覆盖了 PE 135→23 的完整牛熊周期。

**产出**：设计文档 V2.0 最终版（修订）

---

**下一篇**：Phase 1 编码实施（数据管道 → 估值核心模块 → 策略模块 → 回测脚本）

---

*最后更新：2026-07-22*
