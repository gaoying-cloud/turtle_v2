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
import yaml

logger = logging.getLogger(__name__)

# ── 路径常量 ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "turtle_config.yaml"
DATA_DIR = PROJECT_ROOT / "data" / "etf_daily"
INDEX_DIR = PROJECT_ROOT / "data" / "index_daily"

# ── 算法常量 ──
SPLIT_DETECTION_THRESHOLD = 0.15  # 拆分/合并事件的价格偏离阈值

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
    前复权处理：优先使用 Tushare fund_adj 官方因子，不可用时用价格检测。
    """
    adj_df = _fetch_adj_factors(code)
    if not adj_df.empty:
        return _apply_factor_adjustment(df, adj_df)

    return _detect_and_adjust_splits(df)

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

    ratio_series = latest_factor / adj_series
    ratio_series = ratio_series.where(ratio_series > 0, 1.0)

    price_cols = ["open", "high", "low", "close"]
    for col in price_cols:
        if col in df.columns:
            # 使用 .values 避免 RangeIndex vs DatetimeIndex 索引错位导致全部 NaN
            df[col] = (df[col].values * ratio_series.values).round(4)
            df[col] = df[col].clip(lower=0.01)

    df["pre_close"] = df["close"].shift(1)
    df.loc[df.index[0], "pre_close"] = None

    # 显式强制类型转换为 float64，防止 int 推断
    df["adj_factor"] = adj_series.fillna(1.0).astype("float64")
    return df

def _detect_and_adjust_splits(df: pd.DataFrame) -> pd.DataFrame:
    """检测并修正基金拆分/合并事件（前复权降级路径）。"""
    df = df.sort_values("date").reset_index(drop=True)
    events = []

    for i in range(1, len(df)):
        prev_close = df.loc[i - 1, "close"]
        curr_pre = df.loc[i, "pre_close"]
        if prev_close <= 0:
            continue
        ratio = curr_pre / prev_close
        if abs(ratio - 1) > SPLIT_DETECTION_THRESHOLD:
            events.append((i, ratio))

    if not events:
        logger.info("[复权] 价格检测未发现 >%.0f%% 的拆分事件，视为无需复权", SPLIT_DETECTION_THRESHOLD * 100)
        # 显式强制类型转换为 float64
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

    n = len(df)
    adj_factors = [1.0] * n
    cum = 1.0
    for evt_idx in range(len(events) - 1, -1, -1):
        evt_pos, evt_factor = events[evt_idx]
        cum *= evt_factor
        for i in range(evt_pos):
            adj_factors[i] = cum
    # 显式强制类型转换为 float64
    df["adj_factor"] = pd.Series(adj_factors).astype("float64")
    return df

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

    if not force:
        local_df = _read_local_cache(code)
        if not local_df.empty:
            local_min = local_df["date"].min().strftime("%Y%m%d")
            local_max = local_df["date"].max().strftime("%Y%m%d")

            if local_min <= start_norm and local_max >= end_norm:
                mask = (local_df["date"] >= start_date) & (local_df["date"] <= end_date)
                logger.info("[%s] 缓存数据已覆盖请求区间 %s~%s，直接返回", code, start_date, end_date)
                return local_df[mask].reset_index(drop=True)

            pull_start = local_max
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

def fetch_index_daily(
    code: str, start_date: str, end_date: str, max_retries: int = 3
) -> pd.DataFrame:
    """从 Tushare index_daily 接口拉取指数日线，存入本地缓存。"""
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _index_cache_path(code)

    if cache_path.exists():
        cached = pd.read_parquet(cache_path)
        if not cached.empty:
            cached_min = cached["date"].min().strftime("%Y%m%d")
            cached_max = cached["date"].max().strftime("%Y%m%d")
            if cached_min <= start_date and cached_max >= end_date:
                mask = (cached["date"] >= start_date) & (cached["date"] <= end_date)
                return cached[mask].reset_index(drop=True)

    try:
        pro = _create_tushare_pro()
        for attempt in range(1, max_retries + 1):
            try:
                df = pro.index_daily(
                    ts_code=code, start_date=start_date, end_date=end_date,
                    fields=",".join(TUSHARE_FIELDS)
                )
                if df is None or df.empty:
                    logger.warning("[指数 %s] %s~%s 无数据返回", code, start_date, end_date)
                    return _read_existing_index(code, start_date, end_date)

                # 调用公共清洗函数
                df = _clean_raw_ohlc(df)

                if cache_path.exists():
                    existing = pd.read_parquet(cache_path)
                    df = pd.concat([existing, df], ignore_index=True)
                    df.sort_values("date", inplace=True)
                    df.drop_duplicates(subset="date", keep="last", inplace=True)
                    df.reset_index(drop=True, inplace=True)

                df.to_parquet(cache_path, index=False, compression="snappy")
                logger.info("[指数 %s] 已缓存 %d 行", code, len(df))
                mask = (df["date"] >= start_date) & (df["date"] <= end_date)
                return df[mask].reset_index(drop=True)

            except Exception as e:
                logger.warning("[指数 %s] 第 %d/%d 次请求失败: %s", code, attempt, max_retries, e)
                if attempt < max_retries:
                    sleep(attempt * 2)
        return _read_existing_index(code, start_date, end_date)
    except Exception as e:
        logger.warning("Tushare 接口初始化失败，无法拉取指数数据: %s", e)
        return _read_existing_index(code, start_date, end_date)

def _read_existing_index(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """从本地缓存读取指数数据（降级路径）。"""
    cache_path = _index_cache_path(code)
    if cache_path.exists():
        df = pd.read_parquet(cache_path)
        mask = (df["date"] >= start_date) & (df["date"] <= end_date)
        return df[mask].reset_index(drop=True)
    return pd.DataFrame()

# Backward compatibility alias
_clean_and_standardize = _clean_and_standardize_etf
