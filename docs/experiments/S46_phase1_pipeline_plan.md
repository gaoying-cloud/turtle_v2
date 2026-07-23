# S46 Phase 1 — 数据管道模块施工图

**日期**: 2026-07-23
**范围**: 仅数据管道（`src/valuation_pipeline.py`），不含策略/回测/测试
**原则**: 新建独立文件，不影响现有海龟策略 dry-run

---

## 一、文件规划

### 新建文件

| 文件 | 用途 |
|:---|:---|
| `src/valuation_pipeline.py` | 估值数据管道（独立模块） |

### 复用（import，不修改）

| 来源 | 复用内容 |
|:---|:---|
| `src/data_pipeline.py` | `PROJECT_ROOT`, `CONFIG_PATH`, `_create_tushare_pro()`, `_clean_raw_ohlc()`, `_merge_into_cache()`, `_normalize_date()` |

### 不修改的文件

- `src/data_pipeline.py` — 海龟 dry-run 不受影响
- `config/turtle_config.yaml` — 今天不改，估值配置后续再加

---

## 二、Tushare 接口数据契约

### 2.1 `index_dailybasic` 字段（已验证）

```
['ts_code', 'trade_date', 'total_mv', 'float_mv', 'total_share',
 'float_share', 'free_share', 'turnover_rate', 'turnover_rate_f',
 'pe', 'pe_ttm', 'pb']
```

**策略需要的**: `trade_date`, `pe_ttm`, `pb`, `total_mv`
**缓存时保留**: 全部字段（不丢信息，后续可能需要 turnover_rate 等）

### 2.2 `index_daily` 字段（已验证）

```
['ts_code', 'trade_date', 'close', 'open', 'high', 'low',
 'pre_close', 'change', 'pct_chg', 'vol', 'amount']
```

**策略需要的**: `trade_date`, `close`, `amount`, `pct_chg`
**缓存时保留**: 全部 OHLCV + `pct_chg`

### 2.3 数据起始日期（已验证）

| 指数 | 数据起始 | 年数（至 2026-07）|
|:---|:---|:---|
| 399006.SZ 创业板指 | **2010-06-01** | ~16 年 |
| 000300.SH 沪深300 | **2005-04-08** | ~21 年 |
| 000905.SH 中证500 | 待验证（预计 ~2007） | ~19 年 |
| 000016.SH 上证50 | 待验证（预计 ~2004） | ~22 年 |

### 2.4 分页限制

- `index_dailybasic` 单次调用最多返回 **3000 行**
- 每自然年约 240-244 个交易日
- **3 年一段 ≈ 720 行**，远低于 3000 行上限
- 创业板指 2010-2026 需分 ≈ 6 段

---

## 三、模块结构（`src/valuation_pipeline.py`）

### 3.1 文件头部

```python
"""
S46 估值策略 · 数据管道

从 Tushare Pro 拉取指数估值数据（PE_TTM / PB / 总市值）+ 行情数据，
清洗后缓存为 Parquet 文件。

文件结构：data/index_valuation/{code}.parquet（每个指数独立文件）

依赖：
- tushare>=1.4.0
- pandas>=2.0.0
- pyarrow>=12.0.0

环境变量：
TUSHARE_TOKEN — Tushare Pro API token

注意：本模块独立于 data_pipeline.py，不影响海龟策略数据管道。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from time import sleep
from typing import Optional, Any

import pandas as pd
import numpy as np

# ── 复用 data_pipeline 的工具函数（不修改原文件） ──
from src.data_pipeline import (
    PROJECT_ROOT,
    CONFIG_PATH,
    _create_tushare_pro,
    _clean_raw_ohlc,
    _merge_into_cache,
    _normalize_date,
)

logger = logging.getLogger(__name__)
```

### 3.2 常量

```python
# ── 缓存路径 ──
INDEX_VALUATION_DIR = PROJECT_ROOT / "data" / "index_valuation"

# ── 默认覆盖指数 ──
DEFAULT_VALUATION_CODES = [
    "399006.SZ",   # 创业板指（主策略标的）
    "000300.SH",   # 沪深300（辅助判断）
    "000905.SH",   # 中证500（辅助判断）
    "000016.SH",   # 上证50（辅助判断）
]

# ── Tushare 字段 ──
DAILYBASIC_FIELDS = [
    "ts_code", "trade_date", "total_mv", "float_mv",
    "total_share", "float_share", "free_share",
    "turnover_rate", "turnover_rate_f",
    "pe", "pe_ttm", "pb",
]

INDEX_DAILY_FIELDS = [
    "ts_code", "trade_date", "close", "open", "high", "low",
    "pre_close", "change", "pct_chg", "vol", "amount",
]

# 分页参数
PAGINATION_SEGMENT_YEARS = 3   # 每段 ≤3 年（~720 行 < 3000 上限）
MAX_RETRIES = 3                # API 重试次数
```

### 3.3 内部函数清单

```
_fetch_dailybasic_segment(pro, ts_code, start, end) → pd.DataFrame
        │
        ▼
_fetch_dailybasic_paginated(ts_code, start, end) → pd.DataFrame
        │
        ▼
_fetch_index_daily_raw(ts_code, start, end) → pd.DataFrame
        │
        ▼
_merge_valuation_and_price(df_basic, df_daily) → pd.DataFrame
        │
        ▼
_valuation_cache_path(code) → Path
        │
        ▼
_read_valuation_cache(code) → pd.DataFrame
        │
        ▼
┌─────────────────────────────────────────────┐
│  公开接口（仅 1 个函数）                      │
│  fetch_index_valuation(codes, start, end,    │
│                        force_update)         │
│  → dict[str, pd.DataFrame]                  │
└─────────────────────────────────────────────┘
```

---

## 四、函数详细规格

### 4.1 `_fetch_dailybasic_segment()`

```python
def _fetch_dailybasic_segment(
    pro: Any,
    ts_code: str,
    start_date: str,   # "YYYYMMDD"
    end_date: str,     # "YYYYMMDD"
) -> pd.DataFrame:
    """
    拉取单段 index_dailybasic 数据（含重试）。

    Returns:
        DataFrame with DAILYBASIC_FIELDS columns, or empty on failure
    """
```

**实现要点**：
- 调用 `pro.index_dailybasic(ts_code=ts_code, start_date=start_date, end_date=end_date, fields=",".join(DAILYBASIC_FIELDS))`
- 重试逻辑：`attempt in 1..MAX_RETRIES`，失败后 `sleep(attempt * 2)`
- 返回空 DataFrame 时不抛异常（由上层拼接逻辑处理）

### 4.2 `_fetch_dailybasic_paginated()`

按 `PAGINATION_SEGMENT_YEARS` 年切段时间范围，每段调用 `_fetch_dailybasic_segment()`，最后 `pd.concat` + `drop_duplicates` + `sort_values`。

### 4.3 `_fetch_index_daily_raw()`

调用 `pro.index_daily()`，保留全部 `INDEX_DAILY_FIELDS`，含重试。

### 4.4 `_merge_valuation_and_price()`

inner join on `date`，合并估值字段和行情字段。

### 4.5 `_valuation_cache_path()` / `_read_valuation_cache()`

缓存路径辅助函数。

### 4.6 `fetch_index_valuation()` — 唯一公开接口

```python
def fetch_index_valuation(
    ts_codes: list[str] | None = None,
    start_date: str = "20100101",
    end_date: str | None = None,
    force_update: bool = False,
) -> dict[str, pd.DataFrame]:
```

**逻辑**：
1. 检查缓存是否全覆盖请求区间 → 是则直接返回切片
2. 分页拉取 `index_dailybasic` → `_clean_raw_ohlc()` 清洗
3. 拉取 `index_daily` → `_clean_raw_ohlc()` 清洗
4. inner join 合并 → `_merge_into_cache()` 落盘
5. 返回 `{code: DataFrame}`

---

## 五、无未来数据保证

| 风险 | 防护 |
|:---|:---|
| PE_TTM 的 TTM 本质 | TTM = 已发布财报的过去 12 个月数据，天然无未来数据 |
| 分位计算窗口 | 由策略层保证：`window[i] = data[0:i]`，不含未来 |
| Parquet 缓存全量 | 由策略层按回测日期切片，不泄漏未来数据 |

---

## 六、验证步骤

```bash
# 1. 导入测试
py -c "from src.valuation_pipeline import fetch_index_valuation; print('OK')"

# 2. 单指数拉取（创业板指，全量）
py -c "
from src.valuation_pipeline import fetch_index_valuation
data = fetch_index_valuation(['399006.SZ'], force_update=True)
df = data['399006.SZ']
print(f'行数: {len(df)}')
print(f'日期: {df[\"date\"].min()} ~ {df[\"date\"].max()}')
print(f'列: {df.columns.tolist()}')
print(f'PE_TTM 非空: {df[\"pe_ttm\"].notna().sum()}')
print(f'PB 非空: {df[\"pb\"].notna().sum()}')
print(f'total_mv 非空: {df[\"total_mv\"].notna().sum()}')
"

# 3. 四大指数全量
py -c "
from src.valuation_pipeline import fetch_index_valuation
data = fetch_index_valuation(force_update=True)
for code, df in data.items():
    print(f'{code}: {len(df)} rows, {df[\"date\"].min().date()} ~ {df[\"date\"].max().date()}, PE=[{df[\"pe_ttm\"].min():.1f}, {df[\"pe_ttm\"].max():.1f}]')
"

# 4. 缓存读取测试（应秒级）
py -c "
from src.valuation_pipeline import fetch_index_valuation
data = fetch_index_valuation(['399006.SZ'])
print(f'缓存读取: {len(data[\"399006.SZ\"])} rows')
"

# 5. 关键日期 PE 验证
py -c "
from src.valuation_pipeline import fetch_index_valuation
df = fetch_index_valuation(['399006.SZ'])['399006.SZ'].set_index('date')
for d in ['2024-09-19', '2024-09-24', '2024-10-08']:
    r = df.loc[d]
    print(f'{d}: PE_TTM={r[\"pe_ttm\"]:.2f}, PB={r[\"pb\"]:.2f}, close={r[\"close\"]:.1f}')
# 预期: 9/19 PE≈23.80, 10/08 PE≈38.84
"
```

---

## 七、实施步骤（今天）

1. **创建 `src/valuation_pipeline.py`** — 按上述规格写完整代码
2. **运行验证步骤** — 确保数据拉取成功、PE 值与设计文档一致
3. **记录进度** — 创建 `docs/experiments/S46_phase1_progress.md`

---

## 八、后续依赖（今天不做）

```
src/valuation_pipeline.py  (今天)
        ↓
src/valuation_core.py      (后续: PE/PB分位计算/综合评分/EPS趋势)
        ↓
strategies/valuation_strategy.py  (后续: 两阶段建仓/三层出场状态机)
        ↓
scripts/run_valuation.py   (后续: 回测脚本/基准对比)
        ↓
tests/test_valuation_core.py  (后续: 单元测试)
```
