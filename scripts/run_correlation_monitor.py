#!/usr/bin/env python
"""
跨市场ETF海龟组合策略 · 滚动相关性监控 (S7)

基于 §5.10 施工图设计，计算 60 日滚动两两相关性，
检测平均相关系数 > 0.6 的预警区间。

输出：
    results/stress_test/correlation_series.csv   — 完整滚动相关性时间序列
    results/stress_test/correlation_events.csv   — 预警事件列表
    results/stress_test/correlation_plot.png     — 相关性折线图（--plot 时生成）

用法：
    py scripts/run_correlation_monitor.py                                   # 默认全区间
    py scripts/run_correlation_monitor.py --start 2022-01-01 --end 2023-12-31
    py scripts/run_correlation_monitor.py --window 90 --threshold 0.7
    py scripts/run_correlation_monitor.py --plot
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml

# Windows GBK stdout 兼容
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

warnings.filterwarnings("ignore", category=UserWarning)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)

# ── 路径 ──
DATA_DIR = ROOT / "data" / "etf_daily"
OUTPUT_DIR = ROOT / "results" / "stress_test"
CONFIG_PATH = ROOT / "config" / "turtle_config.yaml"

# 从统一配置读取品种列表
with open(CONFIG_PATH, "r", encoding="utf-8") as _f:
    _CONFIG = yaml.safe_load(_f)
from src.config_loader import get_trading_symbols
SIX_SYMBOLS = get_trading_symbols(_CONFIG)


# ════════════════════════════════════════════════════════════
#  1. 数据加载
# ════════════════════════════════════════════════════════════

def load_price_matrix(
    symbols: list[str],
    start_date: str,
    end_date: str,
) -> Optional[pd.DataFrame]:
    """加载多个品种的价格数据，外连接对齐到公共交易日（S8: inner→outer 修复）。

    使用 src.data_utils 的统一数据加载模块，避免多份内联代码。
    外连接保留全部交易日，新品种上市晚时不会截断老品种的早期数据。
    """
    from src.data_utils import load_data, align_to_common_dates

    all_dfs = {}
    for sym in symbols:
        df = load_data(sym, start_date, end_date, DATA_DIR)
        if df is not None and not df.empty:
            all_dfs[sym] = df[["date", "close"]].copy()
            all_dfs[sym] = all_dfs[sym].set_index("date")

    if not all_dfs:
        return None

    price_df = pd.concat(all_dfs, axis=1)
    price_df.columns = list(all_dfs.keys())
    # 前向填充缺失日（非交易日沿用上一个交易日价格）
    price_df = price_df.ffill().dropna()
    return price_df


# ════════════════════════════════════════════════════════════
#  2. 滚动相关性计算
# ════════════════════════════════════════════════════════════

def compute_rolling_correlation(
    price_df: pd.DataFrame,
    window: int = 60,
) -> pd.DataFrame:
    """计算窗口内的滚动相关系数序列。

    对每个交易日计算过去 window 天的两两品种相关系数矩阵，
    输出各品种与组合的平均相关系数 + 整体平均。

    Parameters
    ----------
    price_df : pd.DataFrame
        index=date, columns=symbols, values=close。
    window : int
        滚动窗口大小（交易日数），默认 60。

    Returns
    -------
    pd.DataFrame
        index=date, columns=[symbol_avg, ..., avg_corr]。
    """
    if price_df is None or price_df.empty or len(price_df.columns) < 2:
        return pd.DataFrame()

    # 使用对数收益率计算相关性（避免价格趋势的 spurious correlation）
    returns = np.log(price_df).diff().dropna()
    avg_corr_series = {}
    for i in range(window, len(returns) + 1):
        window_rets = returns.iloc[i - window : i]
        corr = window_rets.corr()
        # 各品种与组合的平均相关性
        n = len(corr)
        row = {}
        for col in corr.columns:
            others = corr[col].drop(col)
            row[col] = others.mean() if not others.empty else 0.0
        row["avg_corr"] = np.mean(list(row.values())) if row else 0.0
        avg_corr_series[returns.index[i - 1]] = row

    return pd.DataFrame.from_dict(avg_corr_series, orient="index")


# ════════════════════════════════════════════════════════════
#  3. 预警检测
# ════════════════════════════════════════════════════════════

def detect_alerts(
    corr_df: pd.DataFrame,
    threshold: float = 0.6,
) -> list[dict]:
    """从滚动相关性时序中检测预警区间。

    连续 5 个交易日 avg_corr > threshold 记为一段预警。
    返回事件列表，每个事件包含起始日期、结束日期、峰值、持续天数。
    """
    if corr_df is None or corr_df.empty:
        return []

    above = corr_df["avg_corr"] > threshold
    events = []
    in_event = False
    start_date = None
    peak = 0.0

    for dt, is_above in above.items():
        if is_above and not in_event:
            in_event = True
            start_date = dt
            peak = corr_df.loc[dt, "avg_corr"]
        elif is_above and in_event:
            peak = max(peak, corr_df.loc[dt, "avg_corr"])
        elif not is_above and in_event:
            in_event = False
            duration = (dt - start_date).days
            if duration >= 5:
                events.append({
                    "start": str(start_date.date()),
                    "end": str(dt.date()),
                    "peak": round(peak, 3),
                    "duration_days": duration,
                })

    if in_event:
        duration = (above.index[-1] - start_date).days
        if duration >= 5:
            events.append({
                "start": str(start_date.date()),
                "end": str(above.index[-1].date()),
                "peak": round(peak, 3),
                "duration_days": duration,
            })

    return events


# ════════════════════════════════════════════════════════════
#  4. 主流程
# ════════════════════════════════════════════════════════════

def run(
    start_date: str = "2014-01-01",
    end_date: str = "2026-06-10",
    window: int = 60,
    threshold: float = 0.6,
    plot: bool = False,
) -> dict:
    """全流程：加载数据 → 计算滚动相关性 → 预警检测 → 输出。

    Parameters
    ----------
    start_date, end_date : str
        回测区间。
    window : int
        滚动窗口（默认 60）。
    threshold : float
        预警阈值（默认 0.6）。
    plot : bool
        是否生成相关性折线图。

    Returns
    -------
    dict
        {"correlation_series": pd.DataFrame, "alerts": list[dict]}
    """
    price_df = load_price_matrix(SIX_SYMBOLS, start_date, end_date)
    if price_df is None or price_df.empty:
        logger.error("无法加载价格数据，请先运行 py scripts/pull_data.py")
        return {"correlation_series": pd.DataFrame(), "alerts": []}

    corr_df = compute_rolling_correlation(price_df, window=window)
    if corr_df.empty:
        logger.warning("滚动相关性计算无结果（可能数据不足）")
        return {"correlation_series": pd.DataFrame(), "alerts": []}

    alerts = detect_alerts(corr_df, threshold=threshold)

    # 输出
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    series_path = OUTPUT_DIR / "correlation_series.csv"
    corr_df.to_csv(series_path)
    logger.info("相关性时序已保存: %s", series_path)

    events_path = OUTPUT_DIR / "correlation_events.csv"
    events_df = pd.DataFrame(alerts)
    events_df.to_csv(events_path, index=False) if not events_df.empty else None
    logger.info("预警事件已保存: %s (共 %d 次)", events_path, len(alerts))

    if plot:
        _generate_plot(corr_df, threshold)

    return {"correlation_series": corr_df, "alerts": alerts}


def _generate_plot(corr_df: pd.DataFrame, threshold: float = 0.6):
    """生成相关性折线图（依赖 matplotlib）。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        logger.warning("matplotlib 未安装，跳过绘图")
        return

    fig, ax = plt.subplots(figsize=(14, 6))

    # 各品种平均相关系数
    for col in corr_df.columns:
        if col != "avg_corr":
            ax.plot(corr_df.index, corr_df[col], alpha=0.5, linewidth=0.8, label=col)

    # 整体平均
    ax.plot(corr_df.index, corr_df["avg_corr"],
            color="black", linewidth=1.5, label="avg_corr")

    ax.axhline(y=threshold, color="red", linestyle="--", alpha=0.7,
               label=f"threshold={threshold}")
    ax.axhline(y=0.5, color="orange", linestyle=":", alpha=0.5,
               label="ρ=0.5 (关注线)")

    ax.set_title("60日滚动相关性 (对数收益率)")
    ax.set_ylabel("相关系数 ρ")
    ax.legend(loc="upper left", fontsize=8)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.xaxis.set_major_locator(mdates.YearLocator())
    fig.tight_layout()

    plot_path = OUTPUT_DIR / "correlation_plot.png"
    fig.savefig(plot_path, dpi=150)
    logger.info("相关性折线图已保存: %s", plot_path)
    plt.close(fig)


# ════════════════════════════════════════════════════════════
#  CLI 入口
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="跨市场ETF海龟组合策略 · 滚动相关性监控 (S7)")
    parser.add_argument("--start", default="2014-01-01", help="起始日期 (默认 2014-01-01)")
    parser.add_argument("--end", default="2026-06-10", help="截止日期 (默认 2026-06-10)")
    parser.add_argument("--window", type=int, default=60, help="滚动窗口 (默认 60)")
    parser.add_argument("--threshold", type=float, default=0.6, help="预警阈值 (默认 0.6)")
    parser.add_argument("--plot", action="store_true", help="生成相关性折线图")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    run(
        start_date=args.start,
        end_date=args.end,
        window=args.window,
        threshold=args.threshold,
        plot=args.plot,
    )
