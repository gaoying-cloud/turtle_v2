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
import logging
import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)

# ── 路径 ──
DATA_DIR = ROOT / "data" / "etf_daily"
OUTPUT_DIR = ROOT / "results" / "stress_test"

# ── 品种 ──
SIX_SYMBOLS = [
    "510500.SH",  # 中证500
    "159845.SZ",  # 中证1000
    "159915.SZ",  # 创业板
    "588000.SH",  # 科创50
    "513100.SH",  # 纳指ETF
    "518880.SH",  # 黄金ETF
]


# ════════════════════════════════════════════════════════════
#  1. 数据加载
# ════════════════════════════════════════════════════════════

def load_price_matrix(
    symbols: list[str],
    start_date: str,
    end_date: str,
) -> Optional[pd.DataFrame]:
    """加载多个品种的价格数据，内连接对齐到公共交易日。

    Parameters
    ----------
    symbols : list[str]
        品种代码列表。
    start_date : str
        起始日期。
    end_date : str
        截止日期。

    Returns
    -------
    pd.DataFrame or None
        index=date, columns=symbols, values=close
    """
    price_df = None

    for symbol in symbols:
        path = DATA_DIR / f"{symbol}.parquet"
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
            price_df = price_df.merge(df, on="date", how="inner")

    if price_df is None or len(price_df) < 10:
        logger.error("数据不足，无法计算滚动相关性")
        return None

    price_df.set_index("date", inplace=True)
    price_df.sort_index(inplace=True)
    return price_df


# ════════════════════════════════════════════════════════════
#  2. 滚动相关性计算
# ════════════════════════════════════════════════════════════

def compute_rolling_correlation(
    price_df: pd.DataFrame,
    window: int = 60,
) -> pd.DataFrame:
    """计算滚动窗口内的两两相关系数的聚合统计量。

    使用对数收益率计算相关性，然后取上三角的平均值、最大值、最小值。

    Parameters
    ----------
    price_df : pd.DataFrame
        index=date, columns=symbols, values=close
    window : int
        滚动窗口天数，默认 60。

    Returns
    -------
    pd.DataFrame
        Columns: date, avg_corr, max_corr, min_corr, over_threshold, pair_count
    """
    # 计算对数收益率
    returns = np.log(price_df).diff().dropna()
    n_symbols = len(price_df.columns)
    n_pairs = n_symbols * (n_symbols - 1) // 2

    # 滚动相关性
    rolling_corr = returns.rolling(window=window).corr(pairwise=True)

    # 整理为时间序列
    results = []
    dates = returns.index[window - 1:]  # 从第一个有效窗口开始

    for dt in dates:
        # 提取该时间点的相关性矩阵（n_symbols × n_symbols）
        try:
            corr_matrix = rolling_corr.loc[dt]
        except (KeyError, TypeError):
            continue

        if corr_matrix.empty:
            continue

        # corr_matrix 是 MultiIndex DataFrame，需要提取
        # rolling().corr(pairwise=True) 返回 MultiIndex (date, symbol1)
        # 每对 (date, symbol1) 下有一个 symbol2 的 Series
        # 按日期过滤后取唯一组合
        try:
            # 如果 corr_matrix 是 DataFrame，取上三角
            if isinstance(corr_matrix, pd.DataFrame):
                upper = corr_matrix.where(
                    np.triu(np.ones(corr_matrix.shape), k=1).astype(bool)
                )
                values = upper.stack().dropna().values
            else:
                # 单品种情况
                values = np.array([])
        except Exception:
            continue

        if len(values) == 0:
            continue

        avg_corr = float(np.mean(values))
        max_corr = float(np.max(values))
        min_corr = float(np.min(values))
        over_threshold = avg_corr > 0.6

        results.append({
            "date": dt,
            "avg_corr": round(avg_corr, 4),
            "max_corr": round(max_corr, 4),
            "min_corr": round(min_corr, 4),
            "over_threshold": over_threshold,
            "pair_count": len(values),
        })

    result_df = pd.DataFrame(results)
    if not result_df.empty:
        result_df["date"] = pd.to_datetime(result_df["date"])
    return result_df


# ════════════════════════════════════════════════════════════
#  3. 预警事件检测
# ════════════════════════════════════════════════════════════

def detect_correlation_events(
    df: pd.DataFrame,
    threshold: float = 0.6,
) -> pd.DataFrame:
    """检测平均相关系数突破阈值的预警区间。

    连续处于阈值之上的交易日合并为一个事件。

    Parameters
    ----------
    df : pd.DataFrame
        compute_rolling_correlation() 的输出。
    threshold : float
        预警阈值，默认 0.6。

    Returns
    -------
    pd.DataFrame
        Columns: start_date, end_date, duration_days, peak_corr, avg_corr
    """
    if df.empty:
        return pd.DataFrame()

    # 标记连续区间
    df = df.copy()
    df["_group"] = (df["over_threshold"] != df["over_threshold"].shift()).cumsum()
    df["_group"] = df["_group"].where(df["over_threshold"], np.nan)

    events = []
    for group_id, group in df.dropna(subset=["_group"]).groupby("_group"):
        events.append({
            "start_date": group["date"].iloc[0],
            "end_date": group["date"].iloc[-1],
            "duration_days": len(group),
            "peak_corr": round(group["avg_corr"].max(), 4),
            "avg_corr": round(group["avg_corr"].mean(), 4),
        })

    result = pd.DataFrame(events)
    if not result.empty:
        result.sort_values("start_date", inplace=True)
        result.reset_index(drop=True, inplace=True)
    return result


# ════════════════════════════════════════════════════════════
#  4. 绘图（可选）
# ════════════════════════════════════════════════════════════

def plot_correlation_timeseries(
    df: pd.DataFrame,
    output_path: Path,
    threshold: float = 0.6,
):
    """绘制滚动相关性折线图。

    Parameters
    ----------
    df : pd.DataFrame
        compute_rolling_correlation() 的输出。
    output_path : Path
        输出文件路径。
    threshold : float
        预警阈值线。
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        logger.warning("matplotlib 未安装，跳过绘图")
        return

    if df.empty:
        logger.warning("无数据可绘图")
        return

    fig, ax = plt.subplots(figsize=(14, 6))

    dates = df["date"]
    ax.plot(dates, df["avg_corr"], label="平均相关系数", color="steelblue", linewidth=1.5)
    ax.fill_between(dates, 0, df["avg_corr"], where=df["over_threshold"],
                     color="red", alpha=0.15, label="预警区域")
    ax.axhline(y=threshold, color="red", linestyle="--", linewidth=1,
               label=f"阈值 = {threshold}")

    # 标注最大/最小
    max_idx = df["avg_corr"].idxmax()
    min_idx = df["avg_corr"].idxmin()
    ax.annotate(f"峰值 {df.loc[max_idx, 'avg_corr']:.3f}",
                xy=(df.loc[max_idx, "date"], df.loc[max_idx, "avg_corr"]),
                xytext=(10, 10), textcoords="offset points", fontsize=9,
                arrowprops=dict(arrowstyle="->", color="gray"))
    ax.annotate(f"谷值 {df.loc[min_idx, 'avg_corr']:.3f}",
                xy=(df.loc[min_idx, "date"], df.loc[min_idx, "avg_corr"]),
                xytext=(10, -15), textcoords="offset points", fontsize=9,
                arrowprops=dict(arrowstyle="->", color="gray"))

    ax.set_xlabel("日期")
    ax.set_ylabel("平均两两相关系数")
    ax.set_title(f"6 只 ETF 滚动 {len(df) + 60 - 1 if not df.empty else 60} 日两两相关性")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate()

    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()
    logger.info("相关性折线图已保存: %s", output_path)


# ════════════════════════════════════════════════════════════
#  5. 报告生成
# ════════════════════════════════════════════════════════════

def generate_report(
    series_df: pd.DataFrame,
    events_df: pd.DataFrame,
    window: int,
    threshold: float,
) -> str:
    """生成 Markdown 相关性监控报告。"""
    lines = [
        f"# 跨市场ETF海龟组合策略 — 滚动相关性监控报告\n",
        f"**生成日期**: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n",
        f"**参数**: 窗口={window}日, 预警阈值={threshold}\n",
        f"**品种**: {', '.join(SIX_SYMBOLS)}\n",
        "---\n",
    ]

    # 总体统计
    if not series_df.empty:
        lines.append("## 总体统计\n")
        lines.append(f"| 统计量 | 值 |")
        lines.append(f"|:--|:--:|")
        lines.append(f"| 数据区间 | {series_df['date'].min().strftime('%Y-%m-%d')} ~ "
                      f"{series_df['date'].max().strftime('%Y-%m-%d')} |")
        lines.append(f"| 有效交易天数 | {len(series_df)} |")
        lines.append(f"| 平均相关系数 | {series_df['avg_corr'].mean():.4f} |")
        lines.append(f"| 最大相关系数 | {series_df['avg_corr'].max():.4f} |")
        lines.append(f"| 最小相关系数 | {series_df['avg_corr'].min():.4f} |")
        lines.append(f"| 预警天数占比 | {series_df['over_threshold'].mean() * 100:.1f}% |")
        lines.append(f"| 相关系数标准差 | {series_df['avg_corr'].std():.4f} |\n")
    else:
        lines.append("## ❌ 无有效数据\n")

    # 预警事件
    lines.append("---\n## 预警事件\n")
    if not events_df.empty:
        lines.append("| # | 起始日期 | 结束日期 | 持续天数 | 峰值 | 均值 |")
        lines.append("|:--:|:--|:--|:--:|:--:|:--:|")
        for i, (_, event) in enumerate(events_df.iterrows(), 1):
            start = event["start_date"].strftime("%Y-%m-%d") if hasattr(event["start_date"], "strftime") else str(event["start_date"])
            end = event["end_date"].strftime("%Y-%m-%d") if hasattr(event["end_date"], "strftime") else str(event["end_date"])
            lines.append(
                f"| {i} | {start} | {end} | {event['duration_days']} | "
                f"{event['peak_corr']:.4f} | {event['avg_corr']:.4f} |"
            )
        lines.append(f"\n**总计预警事件: {len(events_df)} 次**\n")
    else:
        lines.append("> ✅ 未检测到平均相关系数突破阈值的事件。\n")

    # 结论
    lines.append("---\n## 结论\n")
    if not events_df.empty:
        max_duration = events_df["duration_days"].max()
        if max_duration >= 20:
            lines.append("⚠️ **警告**: 存在长时间（≥20 日）的高相关性区间，组合分散效果可能显著下降。\n")
            lines.append("建议：在 Dry-Run 阶段对高相关性区间内的新开仓设置额外风控条件。\n")
        else:
            lines.append("⚠️ 检测到短期相关性飙升事件，建议在 Dry-Run 阶段持续监控。\n")
    else:
        lines.append("✅ 监控区间内平均两两相关系数未突破预警阈值，组合处于正常分散状态。\n")

    lines.append(f"\n---\n*报告由 `scripts/run_correlation_monitor.py` 自动生成*\n")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════
#  6. 保存结果
# ════════════════════════════════════════════════════════════

def save_results(
    series_df: pd.DataFrame,
    events_df: pd.DataFrame,
    output_dir: Path,
    window: int,
    threshold: float,
):
    """保存所有输出文件。"""
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── 滚动相关性时间序列 ──
    series_path = output_dir / "correlation_series.csv"
    if not series_df.empty:
        series_df.to_csv(series_path, index=False, encoding="utf-8")
        logger.info("相关性时间序列已保存: %s (%d 行)", series_path, len(series_df))

    # ── 预警事件列表 ──
    events_path = output_dir / "correlation_events.csv"
    if not events_df.empty:
        events_df.to_csv(events_path, index=False, encoding="utf-8")
        logger.info("预警事件已保存: %s (%d 行)", events_path, len(events_df))
    else:
        # 写入空文件占位
        pd.DataFrame(columns=["start_date", "end_date", "duration_days", "peak_corr", "avg_corr"]).to_csv(
            events_path, index=False, encoding="utf-8"
        )
        logger.info("预警事件为空，已保存空文件: %s", events_path)

    # ── Markdown 报告 ──
    report = generate_report(series_df, events_df, window, threshold)
    report_path = output_dir / "correlation_report.md"
    report_path.write_text(report, encoding="utf-8")
    logger.info("相关性监控报告已保存: %s (%d 行)", report_path, len(report.splitlines()))


# ════════════════════════════════════════════════════════════
#  7. CLI 入口
# ════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="跨市场ETF海龟组合策略 — 滚动相关性监控 (S7)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--start", type=str, default="2020-01-01",
        help="起始日期 (默认: 2020-01-01)",
    )
    parser.add_argument(
        "--end", type=str, default="2026-06-10",
        help="截止日期 (默认: 2026-06-10)",
    )
    parser.add_argument(
        "--window", "-w", type=int, default=60,
        help="滚动窗口天数 (默认: 60)",
    )
    parser.add_argument(
        "--threshold", "-t", type=float, default=0.6,
        help="预警阈值 (默认: 0.6)",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="输出目录 (默认: results/stress_test/)",
    )
    parser.add_argument(
        "--plot", action="store_true", default=False,
        help="生成相关性折线图",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", default=False,
        help="详细日志",
    )
    args = parser.parse_args()

    # ── 日志 ──
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # ── 输出目录 ──
    output_dir = Path(args.output) if args.output else OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 50)
    logger.info("S7 滚动相关性监控")
    logger.info(f"  区间: {args.start} ~ {args.end}")
    logger.info(f"  窗口: {args.window} 日")
    logger.info(f"  阈值: {args.threshold}")
    logger.info(f"  输出目录: {output_dir}")
    logger.info("=" * 50)

    # ── 加载数据 ──
    price_df = load_price_matrix(SIX_SYMBOLS, args.start, args.end)
    if price_df is None:
        logger.error("数据加载失败，退出")
        sys.exit(1)

    logger.info("价格矩阵: %d 天 × %d 品种", len(price_df), len(price_df.columns))

    # ── 计算滚动相关性 ──
    series_df = compute_rolling_correlation(price_df, window=args.window)
    if series_df.empty:
        logger.error("滚动相关性计算未产生结果，退出")
        sys.exit(1)

    logger.info("滚动相关性: %d 个有效窗口", len(series_df))

    # ── 检测预警事件 ──
    events_df = detect_correlation_events(series_df, threshold=args.threshold)
    logger.info("预警事件: %d 次", len(events_df))

    # ── 绘图（可选） ──
    if args.plot:
        plot_path = output_dir / "correlation_plot.png"
        plot_correlation_timeseries(series_df, plot_path, args.threshold)

    # ── 保存结果 ──
    save_results(series_df, events_df, output_dir, args.window, args.threshold)

    # ── 汇总 ──
    print()
    print("=" * 60)
    print("S7 滚动相关性监控完成")
    print("=" * 60)
    print(f"  数据区间: {args.start} ~ {args.end}")
    print(f"  滚动窗口: {args.window} 日")
    avg_corr = series_df["avg_corr"].mean()
    over_pct = series_df["over_threshold"].mean() * 100
    print(f"  平均相关系数: {avg_corr:.4f}")
    print(f"  阈值 {args.threshold} 以上占比: {over_pct:.1f}%")
    print(f"  预警事件: {len(events_df)} 次")
    if not events_df.empty:
        peak_idx = events_df["peak_corr"].idxmax()
        print(f"  最严重事件: {events_df.loc[peak_idx, 'start_date'].strftime('%Y-%m-%d')} ~ "
              f"{events_df.loc[peak_idx, 'end_date'].strftime('%Y-%m-%d')}, "
              f"峰值 {events_df.loc[peak_idx, 'peak_corr']:.4f}")
    print(f"  报告: {output_dir / 'correlation_report.md'}")
    print("=" * 60)


if __name__ == "__main__":
    main()