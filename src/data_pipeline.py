"""
跨市场ETF海龟组合策略 · 数据管道 (S1)
从 Tushare Pro 拉取 ETF 日线数据，清洗后缓存为 Parquet 文件。
文件结构： data/etf_daily/{code}.parquet (每个品种独立文件)

依赖：
- tushare>=1.4.0
- pandas>=2.0.0
- pyarrow>=12.0.0
- pyyaml>=6.0

环境变量：
TUSHARE_TOKEN — Tushare Pro API token
"""
from __future__ import annotations
import os
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, Any, Dict, List
from time import sleep
from functools import lru_cache

import pandas as pd
import numpy as np
import yaml

logger = logging.getLogger(__name__)

# ── 路径常量 ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "turtle_config.yaml"
DATA_DIR = PROJECT_ROOT / "data" / "etf_daily"
INDEX_DIR = PROJECT_ROOT / "data" / "index_daily"

# ── 算法常量 ──
SPLIT_DETECTION_THRESHOLD = 0.15  # 拆分/合并事件的价格偏离阈值
MAX_DAILY_CHANGE = 0.50           # 前复权后单日涨跌幅上限，超过即判复权失败

# Tushare 通用原始字段
TUSHARE_FIELDS = [
    "ts_code", "trade_date", "open", "high", "low", "close",
    "pre_close", "change", "pct_chg", "vol", "amount",
]

# 标准化后的 Parquet 列名（用于回测）
STD_COLUMNS = [
    "date", "open", "high", "low", "close", "volume",
    "amount", "pre_close", "adj_factor",
]

# ════════════════════════════════════════════════════════════
# 配置加载
# ════════════════════════════════════════════════════════════
@lru_cache(maxsize=None)
def _load_config() -> dict:
    """加载 turtle_config.yaml 配置（带缓存优化）。"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def get_symbols(include_bond: bool = True) -> list[dict]:
    """从配置读取交易品种列表。"""
    config = _load_config()
    symbols = list(config["symbols"])
    if include_bond:
        symbols.append(config["bond"])
    return symbols

# ════════════════════════════════════════════════════════════
# Tushare 接口层
# ════════════════════════════════════════════════════════════
def _get_tushare_token() -> str:
    """从环境变量获取 Tushare token，不存在则抛出异常。"""
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        raise ValueError("Tushare token 未设置。请通过环境变量 TUSHARE_TOKEN 配置。")
    return token

def _create_tushare_pro() -> Any:
    """创建 Tushare Pro API 实例。"""
    import tushare as ts
    ts.set_token(_get_tushare_token())
    return ts.pro_api()

def _fetch_from_tushare(
    code: str, start_date: str, end_date: str, max_retries: int = 3
) -> pd.DataFrame:
    """从 Tushare fund_daily 接口拉取数据，含自动分页与重试。"""
    pro = _create_tushare_pro()
    all_dfs = []
    current_offset = 0

    for attempt in range(1, max_retries + 1):
        try:
            df = pro.fund_daily(
                ts_code=code, start_date=start_date, end_date=end_date,
                fields=",".join(TUSHARE_FIELDS), offset=current_offset
            )
            if df is None or df.empty:
                if current_offset == 0:
                    logger.warning("[%s] %s~%s 无数据返回", code, start_date, end_date)
                break

            all_dfs.append(df)
            current_offset += len(df)

            while len(df) >= 5000:
                df = pro.fund_daily(
                    ts_code=code, start_date=start_date, end_date=end_date,
                    fields=",".join(TUSHARE_FIELDS), offset=current_offset
                )
                if df is not None and not df.empty:
                    all_dfs.append(df)
                    current_offset += len(df)
                else:
                    break

            break

        except Exception as e:
            logger.warning("[%s] 第 %d/%d 次请求失败: %s", code, attempt, max_retries, e)
            if attempt < max_retries:
                sleep(attempt * 2)
            else:
                logger.error("[%s] 已耗尽重试次数，跳过", code)
                return pd.DataFrame()

    if not all_dfs:
        return pd.DataFrame()

    result = pd.concat(all_dfs, ignore_index=True)
    logger.info("[%s] 拉取 %s~%s 共 %d 条", code, start_date, end_date, len(result))
    return result

# ════════════════════════════════════════════════════════════
# 数据清洗与标准化 (抽取公共逻辑)
# ════════════════════════════════════════════════════════════
def _clean_raw_ohlc(raw: pd.DataFrame) -> pd.DataFrame:
    """
    公共清洗函数：处理 Tushare 返回的 OHLC 原始数据。
    包含：列名重命名、日期转换、数值类型转换、排序去重。
    """
    if raw.empty:
        return raw

    df = raw.copy()
    df.rename(columns={"trade_date": "date"}, inplace=True)
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")

    numeric_cols = ["open", "high", "low", "close", "pre_close", "vol", "amount", "change", "pct_chg"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df.sort_values("date", inplace=True)
    df.drop_duplicates(subset="date", keep="last", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

def _clean_and_standardize_etf(raw: pd.DataFrame) -> pd.DataFrame:
    """将 ETF 原始数据标准化为统一 Schema。"""
    df = _clean_raw_ohlc(raw)
    if df.empty:
        return df

    # ETF 特有处理：列名与单位转换
    df.rename(columns={"vol": "volume"}, inplace=True)
    if "volume" in df.columns:
        df["volume"] = df["volume"] * 100  # 手 → 股

    if "amount" in df.columns:
        df["amount"] = df["amount"] * 1000  # 千元 → 元

    keep_cols = [c for c in STD_COLUMNS if c in df.columns]
    return df[keep_cols]

# ════════════════════════════════════════════════════════════
# 前复权处理
# ════════════════════════════════════════════════════════════
def _adjust_forward(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """
    前复权处理（组合策略）：
    1. 优先用 Tushare fund_adj 官方因子做前复权（_apply_factor_adjustment）；
    2. 若 fund_adj 不可用，或用后仍检测到 >50% 残留跳空（fund_adj 漏记早期拆分/
       合并事件，如 510500 的 2015-04 份额合并），叠加 _detect_and_adjust_splits
       用价格检测补齐；
    3. 最终 _validate_adjustment 自愈校验：若仍有 >50% 跳空，判定复权失败返回空，
       触发 fetch_single 跳过写入保留旧缓存，杜绝坏数据落盘。
    """
    if df.empty:
        return df

    adj_df = _fetch_adj_factors(code)
    if not adj_df.empty:
        result = _apply_factor_adjustment(df, adj_df)
        # fund_adj 后若仍有残留跳空，叠加价格检测补齐 fund_adj 漏记的事件
        if not result.empty and _has_residual_cliff(result):
            logger.warning("[%s] fund_adj 后仍检测到残留跳空，叠加价格检测补齐", code)
            result = _detect_and_adjust_splits(result)
    else:
        result = _detect_and_adjust_splits(df)

    # 自愈校验：仍有 >50% 跳空 → 复权失败，返回空拒绝落盘
    if not result.empty and not _validate_adjustment(result):
        logger.error("[%s] 复权后仍存在 >50%% 残留跳空，判定复权失败返回空", code)
        return pd.DataFrame()

    return result

def _has_residual_cliff(df: pd.DataFrame) -> bool:
    """检测前复权后是否仍存在 >SPLIT_DETECTION_THRESHOLD 的单日跳空。"""
    if df.empty or len(df) < 2 or "close" not in df.columns:
        return False
    closes = df["close"].dropna().to_numpy()
    if len(closes) < 2:
        return False
    prev = closes[:-1]
    curr = closes[1:]
    valid = prev > 0
    if not valid.any():
        return False
    changes = np.abs(curr[valid] / prev[valid] - 1)
    return bool((changes > SPLIT_DETECTION_THRESHOLD).any())

def _validate_adjustment(df: pd.DataFrame) -> bool:
    """
    前复权自愈校验：close 单日涨跌幅 >50% 即判定复权失败。
    返回 True 表示通过，False 表示失败（调用方应丢弃结果）。
    """
    if df.empty or len(df) < 2 or "close" not in df.columns:
        return True
    closes = df["close"].dropna().to_numpy()
    if len(closes) < 2:
        return True
    prev = closes[:-1]
    curr = closes[1:]
    valid = prev > 0
    if not valid.any():
        return True
    changes = np.abs(curr[valid] / prev[valid] - 1)
    return not bool((changes > MAX_DAILY_CHANGE).any())

def _fetch_adj_factors(code: str) -> pd.DataFrame:
    """从 Tushare fund_adj 拉取复权因子。"""
    try:
        pro = _create_tushare_pro()
        adj = pro.fund_adj(ts_code=code)
        if adj is None or adj.empty:
            return pd.DataFrame()

        adj = adj.rename(columns={"trade_date": "date"})
        adj["date"] = pd.to_datetime(adj["date"], format="%Y%m%d")
        # 确保 adj_factor 为 float64
        adj["adj_factor"] = pd.to_numeric(adj["adj_factor"], errors="coerce").astype("float64")
        adj = adj.sort_values("date").reset_index(drop=True)
        logger.info("[%s] 拉取复权因子 %d 条", code, len(adj))
        return adj
    except Exception as e:
        logger.warning("[%s] 拉取复权因子失败: %s，使用价格检测法", code, e)
        return pd.DataFrame()

def _apply_factor_adjustment(df: pd.DataFrame, adj_df: pd.DataFrame) -> pd.DataFrame:
    """用官方复权因子做前复权。"""
    if adj_df.empty or df.empty:
        return df

    df = df.sort_values("date").reset_index(drop=True)
    latest_factor = adj_df["adj_factor"].iloc[-1]

    adj_series = pd.Series(adj_df["adj_factor"].values, index=adj_df["date"])
    adj_series = adj_series.reindex(df["date"]).ffill().bfill()

    # 前复权正确公式：ratio = adj[t] / adj[latest]
    # 最新日 ratio=1.0，历史日 ratio<1.0 把旧价往下拉到最新基准。
    # 旧代码 latest/adj[t] 是后复权方向，会放大旧价，造成拆分日假跌。
    ratio_series = adj_series / latest_factor
    ratio_series = ratio_series.where(ratio_series > 0, 1.0)

    price_cols = ["open", "high", "low", "close"]
    for col in price_cols:
        if col in df.columns:
            # 使用 .values 避免 RangeIndex vs DatetimeIndex 索引错位导致全部 NaN
            df[col] = (df[col].values * ratio_series.values).round(4)
            df[col] = df[col].clip(lower=0.01)

    df["pre_close"] = df["close"].shift(1)
    df.loc[df.index[0], "pre_close"] = None

    # 存储前复权比率（最新日=1.0），与降级路径 _detect_and_adjust_splits 语义统一，
    # 便于 verify_adjustment 校验 latest_ratio≈1.0
    df["adj_factor"] = ratio_series.fillna(1.0).astype("float64").values
    return df

def _detect_and_adjust_splits(df: pd.DataFrame) -> pd.DataFrame:
    """检测并修正基金拆分/合并事件（前复权降级路径）。"""
    df = df.sort_values("date").reset_index(drop=True)
    events = []

    for i in range(1, len(df)):
        prev_close = df.loc[i - 1, "close"]
        curr_close = df.loc[i, "close"]
        if prev_close <= 0 or curr_close <= 0:
            continue
        # 实际涨跌幅 = close[t] / close[t-1]，跨跳空日也能检测
        ratio = curr_close / prev_close
        if abs(ratio - 1) > SPLIT_DETECTION_THRESHOLD:
            events.append((i, ratio))

    if not events:
        logger.info("[复权] 价格检测未发现 >%.0f%% 的拆分事件，视为无需复权", SPLIT_DETECTION_THRESHOLD * 100)
        # 组合模式下保留已有的 adj_factor（来自 fund_adj）；独立降级模式下设 1.0
        if "adj_factor" not in df.columns:
            df["adj_factor"] = 1.0
        df["adj_factor"] = df["adj_factor"].astype("float64")
        return df

    price_cols = ["open", "high", "low", "close"]
    for evt_idx in range(len(events) - 1, -1, -1):
        evt_pos, evt_factor = events[evt_idx]
        start = 0 if evt_idx == 0 else events[evt_idx - 1][0]
        end = evt_pos - 1
        if end < start:
            continue
        for col in price_cols:
            if col in df.columns:
                df.loc[start:end, col] = df.loc[start:end, col].astype(float) * evt_factor
                df.loc[start:end, col] = df.loc[start:end, col].clip(lower=0.01)

    logger.info("[复权] %s: %d 个事件, 逆向分段调整完成", df.get("ts_code", "Unknown"), len(events))

    df["pre_close"] = df["close"].shift(1)
    df.loc[df.index[0], "pre_close"] = None

    # 计算本次价格检测的累积比率（事件前段累积乘 evt_factor，最新段=1.0）
    n = len(df)
    detect_ratios = [1.0] * n
    cum = 1.0
    for evt_idx in range(len(events) - 1, -1, -1):
        evt_pos, evt_factor = events[evt_idx]
        cum *= evt_factor
        for i in range(evt_pos):
            detect_ratios[i] = cum
    detect_series = pd.Series(detect_ratios, index=df.index).astype("float64")

    # 组合模式：把检测比率乘进已有 adj_factor（fund_adj 比率），保留两路信息；
    # 独立降级模式：直接用检测比率作为 adj_factor
    if "adj_factor" in df.columns:
        df["adj_factor"] = (pd.to_numeric(df["adj_factor"], errors="coerce").fillna(1.0) * detect_series).astype("float64")
    else:
        df["adj_factor"] = detect_series
    return df

def _readjust_merged(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """
    对增量合并后的全量序列重做前复权。

    增量更新时，历史缓存段用的是上次拉取时的旧基准，新块用的是当前最新基准。
    当 Tushare 回溯更新 adj_factor（拆分/分红后必然发生），两段不在同一基准
    → 拼接处伪跳空，污染 ATR/突破信号。

    缓存存的是已前复权价 + 复权比率（latest=1.0），无法从比率还原原始因子做
    基准迁移。因此本函数重新全量拉取原始日线，对整条序列统一走 _adjust_forward，
    保证历史段与新块在同一最新基准上。代价是增量更新时多一次全量 API 请求，
    但增量更新不频繁，正确性优先。
    """
    if df.empty:
        return df

    earliest = df["date"].min().strftime("%Y%m%d")
    latest = df["date"].max().strftime("%Y%m%d")
    logger.info("[%s] 全量重做前复权：重新拉取 %s~%s 原始日线", code, earliest, latest)

    raw = _fetch_from_tushare(code, earliest, latest)
    if raw.empty:
        logger.warning("[%s] 全量重做跳过：原始日线拉取失败，保留合并结果", code)
        return df

    cleaned = _clean_and_standardize_etf(raw)
    readjusted = _adjust_forward(cleaned, code)
    if readjusted.empty:
        logger.warning("[%s] 全量重做跳过：重做复权失败，保留合并结果", code)
        return df

    logger.info("[%s] 全量重做前复权完成：%d 行", code, len(readjusted))
    return readjusted

# ════════════════════════════════════════════════════════════
# 本地 Parquet 缓存
# ════════════════════════════════════════════════════════════
def _parquet_path(code: str) -> Path:
    """获取指定品种的 Parquet 缓存路径。"""
    return DATA_DIR / f"{code}.parquet"

def _read_local_cache(code: str) -> pd.DataFrame:
    """读取本地 Parquet 缓存，不存在则返回空 DataFrame。"""
    path = _parquet_path(code)
    if path.exists():
        return pd.read_parquet(path)
    return pd.DataFrame()

def _save_to_parquet(df: pd.DataFrame, code: str):
    """将 DataFrame 写入 Parquet 文件。"""
    path = _parquet_path(code)
    path.parent.mkdir(parents=True, exist_ok=True)

    if not df.empty:
        if path.exists():
            existing = pd.read_parquet(path)
            if not existing.empty:
                min_date = df["date"].min()
                max_date = df["date"].max()
                existing = existing[(existing["date"] < min_date) | (existing["date"] > max_date)]
                df = pd.concat([existing, df], ignore_index=True)

        df.sort_values("date", inplace=True)
        df.drop_duplicates(subset="date", keep="last", inplace=True)
        df.reset_index(drop=True, inplace=True)

    df.to_parquet(path, index=False, compression="snappy")
    logger.info("[%s] 缓存已写入: %s (%d 行)", code, path, len(df))

# ════════════════════════════════════════════════════════════
# 核心拉取逻辑
# ════════════════════════════════════════════════════════════
def fetch_single(
    code: str, start_date: Optional[str] = None, end_date: Optional[str] = None, force: bool = False
) -> pd.DataFrame:
    """拉取并缓存单个品种的日线数据。"""
    config = _load_config()
    bt = config["backtest"]

    if start_date is None:
        start_date = bt["start_date"]
    if end_date is None:
        end_date = date.today().isoformat()

    start_norm = start_date.replace("-", "")
    end_norm = end_date.replace("-", "")

    # 标记是否走增量路径（有缓存但未全覆盖 → 拼接新块）。
    # 增量路径需要合并后对全量重做前复权，消除历史段旧因子与新块新因子的基准断层。
    is_incremental = False
    if not force:
        local_df = _read_local_cache(code)
        if not local_df.empty:
            local_min = local_df["date"].min().strftime("%Y%m%d")
            local_max = local_df["date"].max().strftime("%Y%m%d")

            if local_min <= start_norm and local_max >= end_norm:
                mask = (local_df["date"] >= start_date) & (local_df["date"] <= end_date)
                logger.info("[%s] 缓存数据已覆盖请求区间 %s~%s，直接返回", code, start_date, end_date)
                return local_df[mask].reset_index(drop=True)

            # 增量起点用 local_max 的下一交易日，避免与缓存最后一天重叠：
            # 1) 去除重复拉取；
            # 2) 更重要的是，_adjust_forward 只对本次新拉取的 raw 块单独前复权，
            #    若重叠日被新因子覆盖而历史段仍是旧因子，拼接处会产生伪跳空，
            #    污染 ATR/突破信号。让两块严格不重叠可缓解基准断层。
            is_incremental = True
            last_date = local_df["date"].max()
            pull_start = (last_date + timedelta(days=1)).strftime("%Y%m%d")
            logger.info("[%s] 缓存最新日期 %s，增量拉取 %s~%s", code, local_max, pull_start, end_norm)
            raw = _fetch_from_tushare(code, pull_start, end_norm)
        else:
            raw = _fetch_from_tushare(code, start_norm, end_norm)
    else:
        raw = _fetch_from_tushare(code, start_norm, end_norm)

    if raw.empty:
        cached = _read_local_cache(code)
        if not cached.empty:
            mask = (cached["date"] >= start_date) & (cached["date"] <= end_date)
            return cached[mask].reset_index(drop=True)
        return pd.DataFrame()

    cleaned = _clean_and_standardize_etf(raw)
    cleaned = _adjust_forward(cleaned, code)

    if cleaned.empty:
        logger.error("[%s] 清洗+复权后数据为空（复权失败），跳过写入，保留已有缓存", code)
        cached = _read_local_cache(code)
        if not cached.empty:
            mask = (cached["date"] >= start_date) & (cached["date"] <= end_date)
            return cached[mask].reset_index(drop=True)
        return pd.DataFrame()

    _save_to_parquet(cleaned, code)

    # 增量路径：新块（新因子基准）已与历史缓存（旧因子基准）合并，
    # 需对全量序列重做前复权基准对齐，消除拼接处断层。
    if is_incremental:
        merged = _read_local_cache(code)
        if not merged.empty:
            merged = _readjust_merged(merged, code)
            _save_to_parquet(merged, code)

    result = _read_local_cache(code)
    if not result.empty:
        mask = (result["date"] >= start_date) & (result["date"] <= end_date)
        return result[mask].reset_index(drop=True)
    return result

def pull_all(
    start_date: Optional[str] = None, end_date: Optional[str] = None, force: bool = False
) -> Dict[str, pd.DataFrame]:
    """拉取全部品种（6 海龟品种 + 国债ETF）的日线数据。"""
    symbols = get_symbols(include_bond=True)
    results = {}
    for sym in symbols:
        code = sym["code"]
        logger.info("===== 开始拉取: %s (%s) =====", sym["name"], code)
        try:
            df = fetch_single(code, start_date, end_date, force)
            results[code] = df
            logger.info(
                "[%s] 完成: %d 行, %s ~ %s", code, len(df),
                df["date"].min().date() if not df.empty else "N/A",
                df["date"].max().date() if not df.empty else "N/A",
            )
        except Exception as e:
            logger.error("[%s] 拉取失败: %s", code, e)
            results[code] = pd.DataFrame()
    return results

# ════════════════════════════════════════════════════════════
# 数据可用性检查
# ════════════════════════════════════════════════════════════
def check_status() -> pd.DataFrame:
    """检查所有品种的本地缓存状态。"""
    symbols = get_symbols(include_bond=True)
    records = []
    for sym in symbols:
        code = sym["code"]
        df = _read_local_cache(code)
        if df.empty:
            records.append({
                "code": code, "name": sym["name"], "market": sym.get("market", "债券"),
                "earliest": "—", "latest": "—", "rows": 0,
            })
        else:
            records.append({
                "code": code, "name": sym["name"], "market": sym.get("market", "债券"),
                "earliest": df["date"].min().strftime("%Y-%m-%d"),
                "latest": df["date"].max().strftime("%Y-%m-%d"),
                "rows": len(df),
            })
    return pd.DataFrame(records)

# ════════════════════════════════════════════════════════════
# 指数日线数据（用于大盘基准对比）
# ════════════════════════════════════════════════════════════
def _index_cache_path(code: str) -> Path:
    """指数 parquet 缓存路径。"""
    return INDEX_DIR / f"{code}.parquet"

def _normalize_date(d) -> str:
    """归一化日期为 8 位 YYYYMMDD 字符串。

    兼容 "2020-01-01"、"20200101"、datetime、date 等输入。
    用于 fetch_index_daily 入口归一化，使缓存命中判断的字符串比较可靠、
    Tushare 调用一致，避免调用方传非 8 位格式时静默误判。
    """
    if isinstance(d, str):
        return d.replace("-", "")
    if isinstance(d, (datetime, date)):
        return d.strftime("%Y%m%d")
    # 兜底：尝试转字符串后去分隔符（如 Timestamp）
    return str(d).replace("-", "")[:8]

def _fetch_index_from_tushare(
    code: str, start_date: str, end_date: str, max_retries: int = 3
) -> pd.DataFrame:
    """从 Tushare index_daily 接口拉取指数日线，含重试。与 ETF _fetch_from_tushare 对称。"""
    pro = _create_tushare_pro()
    for attempt in range(1, max_retries + 1):
        try:
            df = pro.index_daily(
                ts_code=code, start_date=start_date, end_date=end_date,
                fields=",".join(TUSHARE_FIELDS)
            )
            if df is None or df.empty:
                logger.warning("[指数 %s] %s~%s 无数据返回", code, start_date, end_date)
                return pd.DataFrame()
            logger.info("[指数 %s] 拉取 %s~%s 共 %d 条", code, start_date, end_date, len(df))
            return df
        except Exception as e:
            logger.warning("[指数 %s] 第 %d/%d 次请求失败: %s", code, attempt, max_retries, e)
            if attempt < max_retries:
                sleep(attempt * 2)
            else:
                logger.error("[指数 %s] 已耗尽重试次数", code)
                return pd.DataFrame()
    return pd.DataFrame()

def fetch_index_daily(
    code: str, start_date: str, end_date: str, max_retries: int = 3,
    force: bool = False,
) -> pd.DataFrame:
    """从 Tushare index_daily 接口拉取指数日线，存入本地缓存。

    - 缓存全覆盖请求区间 → 直接返回切片，不请求网络
    - 缓存部分覆盖且非 force → 增量拉取缺失尾部（cached_max+1 ~ end）
    - force 或无缓存 → 全量拉取
    指数无复权因子，增量合并只需去重，不需 _readjust_merged。
    """
    start_norm = _normalize_date(start_date)
    end_norm = _normalize_date(end_date)
    start_ts = pd.to_datetime(start_norm, format="%Y%m%d")
    end_ts = pd.to_datetime(end_norm, format="%Y%m%d")

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _index_cache_path(code)

    if not force and cache_path.exists():
        cached = pd.read_parquet(cache_path)
        if not cached.empty:
            cached_min = cached["date"].min().strftime("%Y%m%d")
            cached_max = cached["date"].max().strftime("%Y%m%d")
            if cached_min <= start_norm and cached_max >= end_norm:
                mask = (cached["date"] >= start_ts) & (cached["date"] <= end_ts)
                logger.info("[指数 %s] 缓存已覆盖 %s~%s，直接返回", code, start_norm, end_norm)
                return cached[mask].reset_index(drop=True)

            # 增量拉取缺失尾部（与 ETF 一致：cached_max+1日 起，避免重叠）
            last_date = cached["date"].max()
            pull_start = (last_date + timedelta(days=1)).strftime("%Y%m%d")
            if pull_start > end_norm:
                # 缓存已超过请求区间（cached_min > start_norm 的历史缺口不补，
                # 仅返回缓存中落在区间内的部分）
                mask = (cached["date"] >= start_ts) & (cached["date"] <= end_ts)
                return cached[mask].reset_index(drop=True)
            logger.info("[指数 %s] 缓存最新 %s，增量拉取 %s~%s", code, cached_max, pull_start, end_norm)
            raw = _fetch_index_from_tushare(code, pull_start, end_norm, max_retries)
        else:
            raw = _fetch_index_from_tushare(code, start_norm, end_norm, max_retries)
    else:
        raw = _fetch_index_from_tushare(code, start_norm, end_norm, max_retries)

    if raw.empty:
        return _read_existing_index(code, start_ts, end_ts)

    # 清洗 + 合并去重落盘
    df = _clean_raw_ohlc(raw)
    if df.empty:
        return _read_existing_index(code, start_ts, end_ts)

    if cache_path.exists():
        existing = pd.read_parquet(cache_path)
        if not existing.empty:
            df = pd.concat([existing, df], ignore_index=True)
    df.sort_values("date", inplace=True)
    df.drop_duplicates(subset="date", keep="last", inplace=True)
    df.reset_index(drop=True, inplace=True)
    df.to_parquet(cache_path, index=False, compression="snappy")
    logger.info("[指数 %s] 已缓存 %d 行", code, len(df))

    mask = (df["date"] >= start_ts) & (df["date"] <= end_ts)
    return df[mask].reset_index(drop=True)

def _read_existing_index(code: str, start_ts, end_ts) -> pd.DataFrame:
    """从本地缓存读取指数数据（降级路径）。start_ts/end_ts 为 Timestamp。"""
    cache_path = _index_cache_path(code)
    if cache_path.exists():
        df = pd.read_parquet(cache_path)
        if df.empty:
            return df
        mask = (df["date"] >= start_ts) & (df["date"] <= end_ts)
        return df[mask].reset_index(drop=True)
    return pd.DataFrame()

# Backward compatibility alias
_clean_and_standardize = _clean_and_standardize_etf
