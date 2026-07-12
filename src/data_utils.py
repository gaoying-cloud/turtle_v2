"""
数据加载工具 — 共享函数集

聚合 run_backtest.py / gen_report.py / run_correlation_monitor.py 中
重复定义的 load_data / load_price_matrix / align_to_common_dates / df_to_feed。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import backtrader as bt
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def load_data(
    symbol: str,
    start_date: str,
    end_date: str,
    data_dir: Path,
) -> Optional[pd.DataFrame]:
    """从 Parquet 缓存加载单个品种的数据。

    Parameters
    ----------
    symbol : str
        品种代码。
    start_date : str
        起始日期 "YYYY-MM-DD"。
    end_date : str
        截止日期 "YYYY-MM-DD"。
    data_dir : Path
        数据目录（ETF / 期货等）。

    Returns
    -------
    pd.DataFrame or None
        数据帧，包含 date, open, high, low, close, volume, amount。
    """
    path = data_dir / f"{symbol}.parquet"
    if not path.exists():
        logger.error("缓存文件不存在: %s", path)
        return None

    df = pd.read_parquet(path)
    if df.empty:
        logger.warning("[%s] 缓存为空", symbol)
        return None

    # 裁剪日期区间
    mask = (df["date"] >= start_date) & (df["date"] <= end_date)
    df = df[mask].copy()
    if df.empty:
        logger.warning("[%s] 在 %s~%s 区间无数据", symbol, start_date, end_date)
        return None

    # 确保按日期升序
    df.sort_values("date", inplace=True)
    df.reset_index(drop=True, inplace=True)

    logger.info("[%s] 加载 %d 行: %s ~ %s",
                symbol, len(df), df["date"].iloc[0].date(), df["date"].iloc[-1].date())
    return df


def load_price_matrix(
    symbols: list[str],
    start_date: str,
    end_date: str,
    data_dir: Path,
) -> Optional[pd.DataFrame]:
    """加载多个品种的收盘价矩阵，外连接对齐到所有交易日。

    使用 outer join 而非 inner join，避免新品种上市晚导致早期数据被截断。
    缺失日期不向前填充——调用方（滚动相关性计算）会正确处理 NaN。

    Parameters
    ----------
    symbols : list[str]
        品种代码列表。
    start_date : str
        起始日期。
    end_date : str
        截止日期。
    data_dir : Path
        数据目录。

    Returns
    -------
    pd.DataFrame or None
        index=date, columns=symbols, values=close
    """
    price_df = None

    for symbol in symbols:
        path = data_dir / f"{symbol}.parquet"
        if not path.exists():
            logger.warning("缓存文件不存在: %s", path)
            continue

        df = pd.read_parquet(path)
        if df.empty:
            logger.warning("[%s] 缓存为空", symbol)
            continue

        # 过滤日期范围
        mask = (df["date"] >= start_date) & (df["date"] <= end_date)
        df = df[mask].copy()
        if df.empty:
            logger.warning("[%s] 在 %s~%s 区间无数据", symbol, start_date, end_date)
            continue

        df = df[["date", "close"]].copy()
        df.rename(columns={"close": symbol}, inplace=True)
        df["date"] = pd.to_datetime(df["date"])

        if price_df is None:
            price_df = df
        else:
            # 使用 outer join —— 新品种上市晚不会截断较早品种的数据
            price_df = price_df.merge(df, on="date", how="outer")

    if price_df is None or len(price_df) < 10:
        logger.error("数据不足，无法计算滚动相关性")
        return None

    price_df.set_index("date", inplace=True)
    price_df.sort_index(inplace=True)
    return price_df


def align_to_common_dates(
    dataframes: dict[str, pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    """将所有品种对齐到公共日期索引（并集），消除多品种数组长度差异。

    OHLC 前向填充（非交易日沿用上一个交易日价格），volume 填 0。
    """
    all_dates = sorted(
        set.union(*(set(df["date"].dropna()) for df in dataframes.values()))
    )
    all_dates = pd.DatetimeIndex(all_dates)
    aligned = {}
    for sym, df in dataframes.items():
        df = df.set_index("date").reindex(all_dates)
        for col in ["open", "high", "low", "close", "pre_close"]:
            if col in df.columns:
                df[col] = df[col].ffill()
        df["volume"] = df["volume"].fillna(0).astype(float)
        if "amount" in df.columns:
            df["amount"] = df["amount"].fillna(0).astype(float)
        if "adj_factor" in df.columns:
            df["adj_factor"] = df["adj_factor"].ffill()
        df = df.reset_index().rename(columns={"index": "date"})
        aligned[sym] = df
    logger.info("公共交易日: %d 天", len(all_dates))
    return aligned


def df_to_feed(df: pd.DataFrame, symbol: str,
               common_dates: pd.DatetimeIndex | None = None) -> bt.feeds.PandasData:
    """将 pandas DataFrame 转换为 Backtrader PandasData feed。

    字段映射：
        date      → datetime（索引）
        open      → open
        high      → high
        low       → low
        close     → close
        volume    → volume

    若提供 common_dates，会将 DataFrame 对其到该日期索引
    （用于多品种回测时避免 Backtrader 对齐导致的索索引错位）。
    """
    feed_df = df[["date", "open", "high", "low", "close", "volume"]].copy()
    feed_df["date"] = pd.to_datetime(feed_df["date"])

    if common_dates is not None:
        feed_df = feed_df.set_index("date").reindex(common_dates)
        feed_df = feed_df.ffill()
        # 仅前向填充（ffill），不再使用 bfill() 避免未来数据回填前导 NaN
        # keep DatetimeIndex for Backtrader
    else:
        feed_df.set_index("date", inplace=True)

    return bt.feeds.PandasData(
        dataname=feed_df,
        open="open",
        high="high",
        low="low",
        close="close",
        volume="volume",
        plot=False,
    )
