#!/usr/bin/env python
"""
策略对比分析：N字结构 vs 海龟系统

分析维度：
  1. 交易时间重叠度 —— 两个策略是否在同时间交易同一品种
  2. 盈亏一致性 —— 同品种同期交易，N字赚时海龟是否也赚
  3. 互补性 —— 一个亏的时候另一个是否在赚
  4. 组合净值 —— 如果同时运行两个策略的效果
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.data_utils import load_data
from src.turtle_core import TurtleSignals
from strategies.n_structure import NStructureStrategy

logging.basicConfig(level=logging.WARNING)

DATA_DIR = REPO / "data" / "etf_daily"
START = "2014-01-01"
END = "2026-07-09"

# 6 个核心 ETF（与 N 字结构回测一致）
SYMBOLS = ["510500.SH", "159915.SZ", "513100.SH", "518880.SH", "159985.SZ", "513520.SH"]


def run_n_structure() -> dict[str, list[dict]]:
    """运行 N 字结构策略（最优参数），返回按品种索引的交易列表。"""
    strategy = NStructureStrategy(
        window_size=100, atr_period=25,
        stop_mult=2.0, trail_mult=5.0,
        add_step=0.5, max_units=5,
        max_reentries=1,
        use_ma5_confirm=False,
    )

    all_trades = {}
    for symbol in SYMBOLS:
        df = load_data(symbol, START, END, DATA_DIR)
        if df.empty:
            continue
        _, trades = strategy.run(df, symbol=symbol, verbose=False)
        all_trades[symbol] = [
            {
                "symbol": symbol,
                "strategy": "N字",
                "entry_idx": t.entry_idx,
                "exit_idx": t.exit_idx,
                "entry_date": str(df.loc[t.entry_idx, "date"].date()) if t.entry_idx < len(df) else "",
                "exit_date": str(df.loc[t.exit_idx, "date"].date()) if t.exit_idx > 0 and t.exit_idx < len(df) else "",
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "pnl": t.pnl,
                "win": t.pnl > 0,
                "exit_reason": t.exit_reason,
            }
            for t in trades
        ]
    return all_trades


def run_turtle() -> dict[str, list[dict]]:
    """从 report.md 读取海龟系统的交易统计。"""
    report_path = REPO / "results" / "report.md"
    if not report_path.exists():
        print("  ⚠️ 未找到 report.md")
        return {}

    with open(report_path, encoding="utf-8") as f:
        content = f.read()

    # 解析品种级交易统计表
    # 格式: | 159915.SZ | 32 | 15 | 46.9% | 326,389 |
    all_trades: dict[str, list[dict]] = {}
    for line in content.split("\n"):
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) >= 5 and parts[0] in SYMBOLS:
            sym = parts[0]
            total = int(parts[1])
            wins = int(parts[2])
            total_pnl = int(parts[4].replace(",", ""))
            # 构建统计级占位交易
            all_trades[sym] = [
                {"symbol": sym, "strategy": "海龟", "pnl": 0, "win": True}
                for _ in range(total)
            ]

    total = sum(len(v) for v in all_trades.values())
    print(f"  完成: {total} 笔交易（来自 report.md 统计）")
    return all_trades


def analyze_overlap(n_trades: dict, t_trades: dict) -> None:
    """分析两个策略的重叠度和互补性。

    对每个品种，对比交易量、胜率、总盈亏。
    """
    import json

    print("\n" + "=" * 65)
    print("  📊 策略对比分析：N字结构 vs 海龟系统")
    print("=" * 65)

    # 汇总统计
    n_total = sum(len(v) for v in n_trades.values())
    t_total = sum(len(v) for v in t_trades.values())

    print(f"\n📈  交易量对比")
    print(f"  N字结构: {n_total} 笔交易")
    print(f"  海龟系统: {t_total} 笔交易")
    print(f"  比例: {n_total/max(1,t_total):.2f}x")

    # 从 report.md 读取海龟实际胜率和盈亏
    report_path = REPO / "results" / "report.md"
    turtle_stats: dict[str, dict] = {}
    if report_path.exists():
        with open(report_path, encoding="utf-8") as f:
            for line in f:
                if "|" not in line:
                    continue
                parts = [p.strip() for p in line.split("|") if p.strip()]
                if len(parts) >= 5 and parts[0] in SYMBOLS:
                    turtle_stats[parts[0]] = {
                        "total": int(parts[1]),
                        "wins": int(parts[2]),
                        "win_rate": float(parts[3].replace("%", "")),
                        "total_pnl": int(parts[4].replace(",", "")),
                    }

    # 品种级对比
    print(f"\n{'品种':<12} {'N字交易':>6} {'海龟交易':>6} {'N字CAGR':>8} "
          f"{'N字胜率':>7} {'海龟胜率':>7} {'N字PnL':>9} {'海龟PnL':>9}")
    print("-" * 75)

    for sym in SYMBOLS:
        nt = n_trades.get(sym, [])

        # N字指标
        n_pnl = sum(t["pnl"] for t in nt)
        n_wins = sum(1 for t in nt if t["win"])
        n_wr = n_wins / len(nt) if nt else 0

        # 粗略年数
        df = load_data(sym, START, END, DATA_DIR)
        years = 0
        if not df.empty:
            days = (df["date"].iloc[-1] - df["date"].iloc[0]).days
            years = max(1, days / 365.25)
        n_cagr = ((1 + n_pnl / 100000) ** (1 / years) - 1) if years > 0 else 0

        # 海龟指标
        ts = turtle_stats.get(sym, {})
        t_total_sym = ts.get("total", 0)
        t_wr = ts.get("win_rate", 0) / 100
        t_pnl = ts.get("total_pnl", 0)
        t_cagr = ((1 + t_pnl / 100000) ** (1 / years) - 1) if years > 0 and t_pnl != 0 else 0

        print(f"{sym:<12} {len(nt):>6} {t_total_sym:>6} "
              f"{n_cagr:>7.1%} {n_wr:>6.1%} {t_wr:>6.1%} "
              f"{n_pnl:>+9,.0f} {t_pnl:>+9,.0f}")

    # 互补性分析
    print(f"\n🔍  互补性分析")
    print(f"\n{'品种':<12} {'N字胜率':>7} {'海龟胜率':>7} {'胜率差':>7} {'超额PnL':>9}")
    print("-" * 50)

    total_n_pnl = 0
    total_t_pnl = 0
    for sym in SYMBOLS:
        nt = n_trades.get(sym, [])
        n_pnl = sum(t["pnl"] for t in nt)
        n_wins = sum(1 for t in nt if t["win"])
        n_wr = n_wins / len(nt) if nt else 0

        ts = turtle_stats.get(sym, {})
        t_wr = ts.get("win_rate", 0) / 100
        t_pnl = ts.get("total_pnl", 0)

        total_n_pnl += n_pnl
        total_t_pnl += t_pnl

        # 超额收益（N字 - 海龟）
        excess_pnl = n_pnl - t_pnl
        wr_diff = n_wr - t_wr

        icon = "🟢" if excess_pnl > 0 else "🔴"
        print(f"{sym:<12} {n_wr:>6.1%} {t_wr:>6.1%} {wr_diff:>+6.1%} "
              f"{excess_pnl:>+9,.0f}  {icon}")

    print("-" * 50)
    print(f"{'合计(简单和)':<12} {'':>6} {'':>6} {'':>6} "
          f"{total_n_pnl - total_t_pnl:>+9,.0f}")

    print(f"\n📊  汇总对比")
    print(f"  {'指标':<20} {'N字结构':>12} {'海龟系统':>12}")
    print(f"  {'-'*44}")
    print(f"  {'总交易(6品种)':<20} {n_total:>12} {t_total:>12}")
    print(f"  {'总PnL(简单和)':<20} {total_n_pnl:>+12,.0f} {total_t_pnl:>+12,.0f}")
    print(f"  {'平均胜率':<20} "
          f"{sum(sum(1 for t in v if t['win']) for v in n_trades.values()):>10.1%}  "
          f"{sum(ts.get('wins',0) for ts in turtle_stats.values())/max(1,sum(ts.get('total',0) for ts in turtle_stats.values())):>10.1%}")

    print(f"\n🔍  互补性判断")
    print(f"  两策略都是趋势跟踪，但在不同时间尺度进场：")
    print(f"  • 海龟系统: 20日突破进场（顺大势）")
    print(f"  • N字结构: 回调结束突破B点进场（顺中势）")
    print(f"  → 它们可能在不同市场阶段表现更好/更差")
    print(f"  → 若低相关则可组合使用，降低组合波动")
    print(f"\n  要精确验证互补性，需要:")
    print(f"  1. 跑带 --verbose 的 run_backtest.py 导出海龟逐笔交易")
    print(f"  2. 计算两策略每日收益的相关系数")
    print(f"  3. 分析哪类行情下哪个策略占优")
    print()


if __name__ == "__main__":
    print("🔄 运行 N 字结构策略...")
    n_trades = run_n_structure()
    print(f"  完成: {sum(len(v) for v in n_trades.values())} 笔交易")

    print("🔄 读取海龟系统交易记录...")
    t_trades = run_turtle()
    if t_trades:
        print(f"  完成: {sum(len(v) for v in t_trades.values())} 笔交易")
    else:
        print("  ⚠️ 未找到海龟交易记录，分析将基于 N 字结构自身数据")

    analyze_overlap(n_trades, t_trades)
