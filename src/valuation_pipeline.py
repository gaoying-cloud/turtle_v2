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
      复用 data_pipeline 的 _create_tushare_pro / _clean_raw_ohlc /
      _merge_into_cache / _normalize_date 等工具函数，但不修改原文件。
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
from src.data_pipeline import (          # noqa: E402
    PROJECT_ROOT,
    _create_tushare_pro,
    _clean_raw_ohlc,
    _merge_into_cache,
    _normalize_date,
)

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════
#  常量
# ════════════════════════════════════════════════════════════

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
# index_dailybasic 估值字段（全量保留，后续可能需要 turnover_rate 等）
DAILYBASIC_FIELDS = [
    "ts_code", "trade_date", "total_mv", "float_mv",
    "total_share", "float_share", "free_share",
    "turnover_rate", "turnover_rate_f",
    "pe", "pe_ttm", "pb",
]

# index_daily 行情字段（全量保留 OHLCV + pct_chg）
INDEX_DAILY_FIELDS = [
    "ts_code", "trade_date", "close", "open", "high", "low",
    "pre_close", "change", "pct_chg", "vol", "amount",
]

# ── 分页参数 ──
PAGINATION_SEGMENT_YEARS = 3   # 每段 ≤3 年（~720 行 < 3000 上限）
MAX_RETRIES = 3                # API 重试次数

# ── 合并后输出列（策略核心字段） ──
CORE_COLUMNS = [
    "date", "close", "amount", "pct_chg",
    "pe_ttm", "pb", "total_mv",
]

# ════════════════════════════════════════════════════════════
#  内部函数：Tushare 拉取
# ════════════════════════════════════════════════════════════

def _fetch_dailybasic_segment(
    pro: Any,
    ts_code: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """拉取单段 index_dailybasic 数据（含重试）。

    Parameters
    ----------
    pro : tushare.pro_api
        Tushare Pro API 实例。
    ts_code : str
        指数代码，如 "399006.SZ"。
    start_date : str
        起始日期 "YYYYMMDD"。
    end_date : str
        截止日期 "YYYYMMDD"。

    Returns
    -------
    pd.DataFrame
        含 DAILYBASIC_FIELDS 列，失败时为空 DataFrame。
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            df = pro.index_dailybasic(
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
                fields=",".join(DAILYBASIC_FIELDS),
            )
            if df is not None and not df.empty:
                logger.info(
                    "[%s] dailybasic %s~%s: %d 行",
                    ts_code, start_date, end_date, len(df),
                )
                return df
            else:
                logger.info(
                    "[%s] dailybasic %s~%s: 无数据",
                    ts_code, start_date, end_date,
                )
                return pd.DataFrame()
        except Exception as e:
            logger.warning(
                "[%s] dailybasic 第 %d/%d 次失败: %s",
                ts_code, attempt, MAX_RETRIES, e,
            )
            if attempt < MAX_RETRIES:
                sleep(attempt * 2)

    logger.error("[%s] dailybasic 已耗尽重试次数", ts_code)
    return pd.DataFrame()


def _fetch_dailybasic_paginated(
    ts_code: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """分页拉取 index_dailybasic，突破 3000 行硬限制。

    将 [start_date, end_date] 按 PAGINATION_SEGMENT_YEARS 年切段，
    每段独立调用 _fetch_dailybasic_segment()，最后拼接去重。

    示例：
        创业板指 2010-2026 ≈ 16 年 → 切为 6 段：
        2010-2012, 2013-2015, 2016-2018, 2019-2021, 2022-2024, 2025-2026

    Parameters
    ----------
    ts_code : str
        指数代码。
    start_date : str
        起始日期 "YYYYMMDD"。
    end_date : str
        截止日期 "YYYYMMDD"。

    Returns
    -------
    pd.DataFrame
        列 = DAILYBASIC_FIELDS，按 trade_date 升序，已去重。
    """
    pro = _create_tushare_pro()

    start_year = int(start_date[:4])
    end_year = int(end_date[:4]) if end_date else date.today().year

    all_segments: list[pd.DataFrame] = []
    for seg_start_year in range(start_year, end_year + 1, PAGINATION_SEGMENT_YEARS):
        seg_end_year = min(seg_start_year + PAGINATION_SEGMENT_YEARS - 1, end_year)
        seg_start = f"{seg_start_year}0101"
        seg_end = f"{seg_end_year}1231"

        logger.debug(
            "[%s] 分段 %s ~ %s", ts_code, seg_start, seg_end,
        )
        df = _fetch_dailybasic_segment(pro, ts_code, seg_start, seg_end)
        if not df.empty:
            all_segments.append(df)

    if not all_segments:
        logger.warning("[%s] dailybasic 所有分段均无数据", ts_code)
        return pd.DataFrame()

    # 拼接 + 去重 + 排序
    result = pd.concat(all_segments, ignore_index=True)
    result["trade_date"] = pd.to_datetime(result["trade_date"], format="%Y%m%d")
    result = result.drop_duplicates(subset=["trade_date"], keep="last")
    result = result.sort_values("trade_date").reset_index(drop=True)

    logger.info(
        "[%s] dailybasic 分页完成: %d 段 → %d 行 (%s ~ %s)",
        ts_code, len(all_segments), len(result),
        result["trade_date"].min().strftime("%Y-%m-%d"),
        result["trade_date"].max().strftime("%Y-%m-%d"),
    )
    return result


def _fetch_index_daily_raw(
    ts_code: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """拉取指数日线行情原始数据（未清洗）。

    Parameters
    ----------
    ts_code : str
        指数代码。
    start_date : str
        起始日期 "YYYYMMDD"。
    end_date : str
        截止日期 "YYYYMMDD"。

    Returns
    -------
    pd.DataFrame
        含 INDEX_DAILY_FIELDS 列，失败时为空 DataFrame。
    """
    pro = _create_tushare_pro()
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            df = pro.index_daily(
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
                fields=",".join(INDEX_DAILY_FIELDS),
            )
            if df is not None and not df.empty:
                logger.info(
                    "[%s] index_daily %s~%s: %d 行",
                    ts_code, start_date, end_date, len(df),
                )
                return df
            else:
                logger.info(
                    "[%s] index_daily %s~%s: 无数据",
                    ts_code, start_date, end_date,
                )
                return pd.DataFrame()
        except Exception as e:
            logger.warning(
                "[%s] index_daily 第 %d/%d 次失败: %s",
                ts_code, attempt, MAX_RETRIES, e,
            )
            if attempt < MAX_RETRIES:
                sleep(attempt * 2)

    logger.error("[%s] index_daily 已耗尽重试次数", ts_code)
    return pd.DataFrame()


# ════════════════════════════════════════════════════════════
#  内部函数：合并与缓存
# ════════════════════════════════════════════════════════════

def _merge_valuation_and_price(
    df_basic: pd.DataFrame,
    df_daily: pd.DataFrame,
) -> pd.DataFrame:
    """合并估值数据（index_dailybasic）与行情数据（index_daily）。

    两个 DataFrame 必须先通过 _clean_raw_ohlc() 清洗，
    即 trade_date 已重命名为 date 且已转为 datetime。

    合并方式：inner join on date（只保留两个数据源都有的交易日）。

    Parameters
    ----------
    df_basic : pd.DataFrame
        已清洗的估值数据，含 date, pe_ttm, pb, total_mv 等列。
    df_daily : pd.DataFrame
        已清洗的行情数据，含 date, close, amount, pct_chg 等列。

    Returns
    -------
    pd.DataFrame
        合并后的 DataFrame。
    """
    if df_basic.empty or df_daily.empty:
        return pd.DataFrame()

    # 行情侧列（策略必需）
    daily_cols = ["date", "close", "amount", "pct_chg"]
    # 估值侧列（策略必需 + 保留字段）
    basic_cols = [
        "date", "pe_ttm", "pb", "total_mv",
        "float_mv", "turnover_rate", "turnover_rate_f",
        "pe", "total_share", "float_share",
    ]

    # 只保留实际存在的列
    daily_cols = [c for c in daily_cols if c in df_daily.columns]
    basic_cols = [c for c in basic_cols if c in df_basic.columns]

    merged = df_daily[daily_cols].merge(
        df_basic[basic_cols],
        on="date",
        how="inner",
    )

    logger.debug("合并完成: %d 行, %d 列", len(merged), len(merged.columns))
    return merged


def _valuation_cache_path(code: str) -> Path:
    """估值数据缓存路径：data/index_valuation/{code}.parquet"""
    return INDEX_VALUATION_DIR / f"{code}.parquet"


def _read_valuation_cache(code: str) -> pd.DataFrame:
    """读取本地估值缓存，不存在则返回空 DataFrame。"""
    path = _valuation_cache_path(code)
    if path.exists():
        try:
            return pd.read_parquet(path)
        except Exception as e:
            logger.warning("[%s] 缓存读取失败: %s", code, e)
    return pd.DataFrame()


# ════════════════════════════════════════════════════════════
#  公开接口
# ════════════════════════════════════════════════════════════

def fetch_index_valuation(
    ts_codes: list[str] | None = None,
    start_date: str = "20100101",
    end_date: str | None = None,
    force_update: bool = False,
) -> dict[str, pd.DataFrame]:
    """拉取指数估值与行情数据，缓存至 data/index_valuation/{code}.parquet。

    对每个指数：
    1. 若缓存已覆盖请求区间且非 force_update，直接返回缓存切片
    2. 分页拉取 index_dailybasic（PE_TTM / PB / total_mv）
    3. 拉取 index_daily（close / amount / pct_chg）
    4. 清洗 → inner join 合并 → Parquet 落盘

    Parameters
    ----------
    ts_codes : list[str], optional
        指数代码列表，默认 DEFAULT_VALUATION_CODES（四大指数）。
    start_date : str
        起始日期 "YYYYMMDD"，默认 "20100101"。
        创业板指 2010-06-01 发布，更早的日期接口自动返回空。
    end_date : str, optional
        截止日期 "YYYYMMDD"，默认今天。
    force_update : bool
        True = 跳过缓存检查，强制全量重拉并覆盖缓存。

    Returns
    -------
    dict[str, pd.DataFrame]
        {ts_code: DataFrame}，每个 DataFrame 包含核心列：
        date, close, amount, pct_chg, pe_ttm, pb, total_mv
        以及保留列：float_mv, turnover_rate, pe, total_share 等。
        拉取失败的指数不在 dict 中（跳过而非报错）。
    """
    if ts_codes is None:
        ts_codes = DEFAULT_VALUATION_CODES

    if end_date is None:
        end_date = date.today().strftime("%Y%m%d")

    start_norm = _normalize_date(start_date)
    end_norm = _normalize_date(end_date)
    start_ts = pd.to_datetime(start_norm, format="%Y%m%d")
    end_ts = pd.to_datetime(end_norm, format="%Y%m%d")

    INDEX_VALUATION_DIR.mkdir(parents=True, exist_ok=True)

    results: dict[str, pd.DataFrame] = {}

    for code in ts_codes:
        cache_path = _valuation_cache_path(code)

        # ── 缓存全覆盖 → 直接返回切片 ──
        if not force_update:
            cached = _read_valuation_cache(code)
            if not cached.empty and "date" in cached.columns:
                cached["date"] = pd.to_datetime(cached["date"])
                cached_min = cached["date"].min().strftime("%Y%m%d")
                cached_max = cached["date"].max().strftime("%Y%m%d")
                if cached_min <= start_norm and cached_max >= end_norm:
                    mask = (cached["date"] >= start_ts) & (cached["date"] <= end_ts)
                    results[code] = cached[mask].reset_index(drop=True)
                    logger.info(
                        "[%s] 缓存全覆盖 %s~%s，直接返回 %d 行",
                        code, start_norm, end_norm, len(results[code]),
                    )
                    continue

        # ── 拉取估值数据（分页突破 3000 行限制） ──
        logger.info("[%s] 拉取 index_dailybasic %s~%s ...", code, start_norm, end_norm)
        df_basic_raw = _fetch_dailybasic_paginated(code, start_norm, end_norm)
        if df_basic_raw.empty:
            logger.warning("[%s] index_dailybasic 无数据，跳过", code)
            continue

        # ── 拉取行情数据 ──
        logger.info("[%s] 拉取 index_daily %s~%s ...", code, start_norm, end_norm)
        df_daily_raw = _fetch_index_daily_raw(code, start_norm, end_norm)
        if df_daily_raw.empty:
            logger.warning("[%s] index_daily 无数据，跳过", code)
            continue

        # ── 清洗（复用 data_pipeline 的 _clean_raw_ohlc） ──
        # _clean_raw_ohlc 做：trade_date → date、类型转换、排序、去重
        df_basic = _clean_raw_ohlc(df_basic_raw)
        df_daily = _clean_raw_ohlc(df_daily_raw)

        if df_basic.empty or df_daily.empty:
            logger.warning("[%s] 清洗后为空，跳过", code)
            continue

        # ── 合并估值 + 行情 ──
        df_merged = _merge_valuation_and_price(df_basic, df_daily)
        if df_merged.empty:
            logger.warning("[%s] 合并后为空，跳过", code)
            continue

        # ── 缓存落盘（复用 data_pipeline 的 _merge_into_cache） ──
        _merge_into_cache(cache_path, df_merged)
        logger.info(
            "[%s] 已缓存 %d 行 → %s",
            code, len(df_merged), cache_path,
        )

        # ── 返回请求区间切片 ──
        cached_all = pd.read_parquet(cache_path)
        if "date" in cached_all.columns:
            cached_all["date"] = pd.to_datetime(cached_all["date"])
        mask = (cached_all["date"] >= start_ts) & (cached_all["date"] <= end_ts)
        results[code] = cached_all[mask].reset_index(drop=True)

    return results


# ════════════════════════════════════════════════════════════
#  便捷查询函数
# ════════════════════════════════════════════════════════════

def get_valuation_summary() -> pd.DataFrame:
    """检查所有默认指数估值数据的本地缓存状态。

    Returns
    -------
    pd.DataFrame
        列：code, earliest, latest, rows, pe_min, pe_max, pb_min, pb_max
    """
    rows = []
    for code in DEFAULT_VALUATION_CODES:
        path = _valuation_cache_path(code)
        if not path.exists():
            rows.append({
                "code": code,
                "earliest": None, "latest": None, "rows": 0,
                "pe_min": None, "pe_max": None,
                "pb_min": None, "pb_max": None,
            })
            continue
        try:
            df = pd.read_parquet(path)
        except Exception:
            rows.append({
                "code": code,
                "earliest": None, "latest": None, "rows": 0,
                "pe_min": None, "pe_max": None,
                "pb_min": None, "pb_max": None,
            })
            continue
        if df.empty:
            rows.append({
                "code": code,
                "earliest": None, "latest": None, "rows": 0,
                "pe_min": None, "pe_max": None,
                "pb_min": None, "pb_max": None,
            })
            continue

        rows.append({
            "code": code,
            "earliest": df["date"].min(),
            "latest": df["date"].max(),
            "rows": len(df),
            "pe_min": df["pe_ttm"].min() if "pe_ttm" in df.columns else None,
            "pe_max": df["pe_ttm"].max() if "pe_ttm" in df.columns else None,
            "pb_min": df["pb"].min() if "pb" in df.columns else None,
            "pb_max": df["pb"].max() if "pb" in df.columns else None,
        })

    return pd.DataFrame(rows)
