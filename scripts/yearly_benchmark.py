#!/usr/bin/env python
"""
跨市场ETF海龟组合策略 · 跨市场基准逐年收益对比

输出三张表：
  表1: 策略 vs A股大盘指数
  表2: 策略 vs 6只底层ETF Buy-and-Hold
  表3: 跨市场综合对比（策略 vs 沪深300 vs 纳指ETF vs 黄金ETF）

用法：
    py scripts/yearly_benchmark.py
    py scripts/yearly_benchmark.py --mode B
    py scripts/yearly_benchmark.py --start 2021 --end 2025
    py scripts/yearly_benchmark.py --etf-only   # 仅输出 ETF 对比表
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.data_pipeline import fetch_index_daily
from scripts.run_backtest import run_backtest

logger = logging.getLogger(__name__)

# ── 路径 ──
DATA_DIR = ROOT / "data" / "etf_daily"
INDEX_DIR = ROOT / "data" / "index_daily"

# ════════════════════════════════════════════════════════════
#  基准定义
# ════════════════════════════════════════════════════════════

# A 股大盘指数（从 data/index_daily/ 读取）
A_SHARE_INDICES = [
    ("000001.SH", "上证综指", "index"),
    ("000300.SH", "沪深300",  "index"),
    ("000905.SH", "中证500",  "index"),
    ("000852.SH", "中证1000", "index"),
    ("399006.SZ", "创业板指",  "index"),
    ("000688.SH", "科创50",   "index"),
]

# 跨市场基准（从 ETF 价格代理）
CROSS_MARKET = [
    ("513100.SH", "纳指ETF",  "etf"),
    ("518880.SH", "黄金ETF",  "etf"),
]

# 策略交易的 6 只底层 ETF（buy-and-hold 对比）
STRATEGY_ETFS = [
    ("510500.SH", "中证500ETF"),
    ("159845.SZ", "中证1000ETF"),
    ("159915.SZ", "创业板ETF"),
    ("588000.SH", "科创50ETF"),
    ("513100.SH", "纳指ETF"),
    ("518880.SH", "黄金ETF"),
]


# ════════════════════════════════════════════════════════════
#  数据加载
# ════════════════════════════════════════════════════════════

def load_yearly_returns_etf(code: str, start_year: int, end_year: int) -> dict:
    """从 ETF parquet 计算逐年收益率。"""
    path = DATA_DIR / f"{code}.parquet"
    if not path.exists():
        return {}
    df = pd.read_parquet(path)
    if df.empty:
        return {}
    df["year"] = pd.to_datetime(df["date"]).dt.year.astype(str)
    yearly = {}
    for yr in range(start_year, end_year + 1):
        yr_str = str(yr)
        yr_data = df[df["year"] == yr_str]
        if len(yr_data) < 2:
            continue
        first_close = yr_data.iloc[0]["close"]
        last_close = yr_data.iloc[-1]["close"]
        if first_close > 0:
            yearly[yr_str] = round((last_close / first_close - 1) * 100, 2)
    return yearly


def load_yearly_returns_index(code: str, start_year: int, end_year: int) -> dict:
    """从指数 parquet 缓存计算逐年收益率，不存在则尝试拉取。"""
    start_dt = f"{start_year}-01-01"
    end_dt = f"{end_year}-12-31"
    df = fetch_index_daily(code, start_dt.replace("-", ""), end_dt.replace("-", ""))
    if df is not None and not df.empty:
        df["year"] = pd.to_datetime(df["date"]).dt.year.astype(str)
        yearly = {}
        for yr in range(start_year, end_year + 1):
            yr_str = str(yr)
            yr_data = df[df["year"] == yr_str]
            if len(yr_data) < 2:
                continue
            first_close = yr_data.iloc[0]["close"]
            last_close = yr_data.iloc[-1]["close"]
            if first_close > 0:
                yearly[yr_str] = round((last_close / first_close - 1) * 100, 2)
        if yearly:
            return yearly
    return {}


def load_benchmark_data(
    benchmarks: list,
    start_year: int, end_year: int,
) -> tuple[list[str], dict[str, dict], dict[str, dict]]:
    """加载多个基准的逐年收益数据。

    Returns
    -------
    (labels, data_by_name, data_by_code)
    """
    labels = []
    data_by_name: dict[str, dict] = {}
    data_by_code: dict[str, dict] = {}
    for code, label, source in benchmarks:
        loader = load_yearly_returns_index if source == "index" else load_yearly_returns_etf
        returns = loader(code, start_year, end_year)
        if returns:
            labels.append(label)
            data_by_name[label] = returns
            data_by_code[code] = returns
            logger.info("[基准 %s] %d 年数据可用", label, len(returns))
        else:
            logger.warning("[基准 %s] 无数据，跳过", label)
    return labels, data_by_name, data_by_code


def run_strategy_yearly(start_year: int, end_year: int, mode: str) -> dict[str, float]:
    """逐年运行策略回测，返回 {年份: 收益率%}。"""
    strat_returns: dict[str, float] = {}
    for y in range(start_year, end_year + 1):
        y_start = f"{y}-01-01"
        y_end = f"{y}-12-31"
        logger.info("策略逐年回测: %s ~ %s", y_start, y_end)
        result = run_backtest(start_date=y_start, end_date=y_end, mode=mode, quiet=True)
        if result is not None:
            strat_returns[str(y)] = result["total_return_pct"]
    return strat_returns


# ════════════════════════════════════════════════════════════
#  输出函数
# ════════════════════════════════════════════════════════════

def print_table(
    title: str,
    headers: list[str],
    rows: list[list[str]],
    avg_row: list[str],
    col_widths: list[int] | None = None,
):
    """打印格式化对比表。"""
    n = len(headers)
    widths = col_widths or [max(8, len(h)) for h in headers]

    # 表宽
    total_w = sum(widths) + (n + 1)
    sep_line = "=" * total_w
    dashed = "|" + "|".join("-" * w for w in widths) + "|"

    print()
    print(sep_line)
    print(f"{title:^{total_w}}")
    print(sep_line)

    # 表头
    hdr = "|"
    for h, w in zip(headers, widths):
        hdr += f" {h:^{w-1}}|"
    print(hdr)
    print(dashed)

    # 数据行
    for row in rows:
        line = "|"
        for v, w in zip(row, widths):
            line += f" {v:>{w-1}}|"
        print(line)

    print(dashed)

    # 均值行
    avg_line = "|"
    for v, w in zip(avg_row, widths):
        avg_line += f" {v:>{w-1}}|"
    print(avg_line)
    print(sep_line)


def build_year_rows(
    available_years: list[str],
    labels: list[str],
    data_by_name: dict[str, dict],
    strat_returns: dict[str, float],
    excess_label: str | None = None,
) -> tuple[list[list[str]], list[str]]:
    """构建对比表的行数据。返回 (行列表, 均值行)。"""
    rows = []
    strat_vals = []
    excess_vals = []

    for yr in available_years:
        row = [yr]
        sv = strat_returns.get(yr, 0)
        row.append(f"{sv:>+.2f}%")
        strat_vals.append(sv)
        for label in labels:
            v = data_by_name[label].get(yr)
            row.append(f"{v:>+.2f}%" if v is not None else "N/A")
        # 超额
        if excess_label and excess_label in data_by_name:
            excess_v = data_by_name[excess_label].get(yr)
            if excess_v is not None:
                excess = sv - excess_v
                row.append(f"{excess:>+.2f}%")
                excess_vals.append(excess)
            else:
                row.append("N/A")
        rows.append(row)

    # 均值
    avg = ["平均"]
    avg_strat = sum(strat_vals) / len(strat_vals) if strat_vals else 0
    avg.append(f"{avg_strat:>+.2f}%")
    for label in labels:
        vals = [v for yr in available_years if (v := data_by_name[label].get(yr)) is not None]
        avg.append(f"{sum(vals)/len(vals):>+.2f}%" if vals else "N/A")
    if excess_label and excess_label in data_by_name:
        avg_excess = sum(excess_vals) / len(excess_vals) if excess_vals else 0
        avg.append(f"{avg_excess:>+.2f}%")
    return rows, avg


def print_cumulative_summary(available_years, labels, data_by_name, strat_returns):
    """打印累计复利收益对比。"""
    print()
    print("累计复利收益对比（{}~{}）:".format(available_years[0], available_years[-1]))

    strat_cumul = 1.0
    for yr in available_years:
        strat_cumul *= (1 + strat_returns.get(yr, 0) / 100)
    print(f"  策略累计: {round((strat_cumul - 1) * 100, 2):>+.2f}%")

    for label in labels:
        cumul = 1.0
        for yr in available_years:
            v = data_by_name[label].get(yr)
            if v is not None:
                cumul *= (1 + v / 100)
        ret_pct = (cumul - 1) * 100
        print(f"  {label}累计: {ret_pct:>+.2f}%")
        if cumul > 0:
            excess = (strat_cumul - cumul) / cumul * 100
            print(f"    策略超额: {excess:>+.2f}%")


# ════════════════════════════════════════════════════════════
#  主逻辑
# ════════════════════════════════════════════════════════════

def run_yearly_comparison(
    start_year: int = 2020,
    end_year: int = 2026,
    mode: str = "A",
    etf_only: bool = False,
) -> None:
    """运行策略 vs 跨市场基准的逐年收益对比。"""
    years = list(range(start_year, end_year + 1))
    year_labels = [str(y) for y in years]

    # ── 1. 逐年运行策略回测 ──
    logger.info("回测模式: %s", "模式 A (无过滤)" if mode == "A" else "模式 B (55日过滤)")
    strat_returns = run_strategy_yearly(start_year, end_year, mode)
    available_years = [yr for yr in year_labels if yr in strat_returns]
    if not available_years:
        print("策略回测无可用数据（可能因最早年份部分品种数据不足）。")
        return
    logger.info("策略有数据的年份: %s", available_years)

    # ── 2. 加载 A 股指数基准 ──
    a_labels, a_data_by_name, _ = load_benchmark_data(A_SHARE_INDICES, start_year, end_year)

    # ── 3. 加载跨市场基准 ──
    cm_labels, cm_data_by_name, _ = load_benchmark_data(CROSS_MARKET, start_year, end_year)

    # ════════════════════════════════════════════════════════
    #  表1: A 股大盘指数对比
    # ════════════════════════════════════════════════════════
    if a_labels and not etf_only:
        headers = ["年份", "策略收益"] + a_labels
        rows, avg = build_year_rows(
            available_years, a_labels, a_data_by_name, strat_returns,
            excess_label="沪深300" if "沪深300" in a_labels else None,
        )
        print_table(
            "表1: 策略 vs A 股大盘指数 · 逐年收益对比",
            headers, rows, avg,
            col_widths=[8] + [13] + [11] * len(a_labels),
        )
        print_cumulative_summary(available_years, a_labels, a_data_by_name, strat_returns)
        print()

    # ════════════════════════════════════════════════════════
    #  表2: 6 只底层 ETF Buy-and-Hold 对比
    # ════════════════════════════════════════════════════════
    if not etf_only:
        etf_labels = []
        etf_data: dict[str, dict] = {}
        for code, label in STRATEGY_ETFS:
            returns = load_yearly_returns_etf(code, start_year, end_year)
            if returns:
                etf_labels.append(label)
                etf_data[label] = returns

        if etf_labels:
            headers = ["年份", "策略收益"] + etf_labels
            rows, avg = build_year_rows(
                available_years, etf_labels, etf_data, strat_returns,
            )
            print_table(
                "表2: 策略 vs 底层 ETF Buy-and-Hold · 逐年收益对比",
                headers, rows, avg,
                col_widths=[8] + [13] + [11] * len(etf_labels),
            )
            print_cumulative_summary(available_years, etf_labels, etf_data, strat_returns)
            print()

    # ════════════════════════════════════════════════════════
    #  表3: 跨市场综合对比
    # ════════════════════════════════════════════════════════
    all_market_labels = []
    all_market_data: dict[str, dict] = {}
    # A 股代表: 沪深300
    for label in a_labels:
        if "沪深300" in label:
            all_market_labels.append(label)
            all_market_data[label] = a_data_by_name[label]
            break
    # 如果没找到沪深300，用第一个
    if not all_market_labels and a_labels:
        all_market_labels.append(a_labels[0])
        all_market_data[a_labels[0]] = a_data_by_name[a_labels[0]]
    # 跨市场
    for label in cm_labels:
        all_market_labels.append(label)
        all_market_data[label] = cm_data_by_name[label]

    if all_market_labels and not etf_only:
        # 超额基准选第一个（沪深300 或对应A股）
        excess_label = all_market_labels[0]
        headers = ["年份", "策略收益"] + all_market_labels + [f"超额({excess_label})"]
        rows, avg = build_year_rows(
            available_years, all_market_labels, all_market_data, strat_returns,
            excess_label=excess_label,
        )
        print_table(
            "表3: 跨市场综合对比 · 逐年收益",
            headers, rows, avg,
            col_widths=[8] + [13] + [11] * len(all_market_labels) + [15],
        )
        print_cumulative_summary(
            available_years, all_market_labels, all_market_data, strat_returns,
        )


# ════════════════════════════════════════════════════════════
#  CLI 入口
# ════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="跨市场ETF海龟组合策略 · 跨市场基准逐年收益对比",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode", "-m", type=str, choices=["A", "B"], default="A",
        help="策略模式 A=无55日过滤(默认), B=55日过滤",
    )
    parser.add_argument(
        "--start", type=int, default=2020, help="起始年份 (默认: 2020)",
    )
    parser.add_argument(
        "--end", type=int, default=2026, help="截止年份 (默认: 2026)",
    )
    parser.add_argument(
        "--etf-only", action="store_true", default=False,
        help="仅输出 ETF 对比表（不跑策略回测，快查用）",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", default=False,
        help="详细日志输出",
    )

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    run_yearly_comparison(
        start_year=args.start, end_year=args.end,
        mode=args.mode, etf_only=args.etf_only,
    )


if __name__ == "__main__":
    main()
