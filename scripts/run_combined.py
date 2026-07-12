#!/usr/bin/env python
"""
50/50 等权组合：N字结构 + 海龟系统

流程：
  1. 运行 N 字结构（6个ETF × 100k，独立交易 → 求和）
  2. 读取已导出的海龟净值（600k 组合交易）
  3. 等权合并（各50%风险预算）
  4. 计算组合指标
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.data_utils import load_data
from strategies.n_structure import NStructureStrategy

logging.basicConfig(level=logging.WARNING)

DATA_DIR = REPO / "data" / "etf_daily"
START = "2014-01-01"
END = "2026-07-09"
SYMBOLS = ["510500.SH", "159915.SZ", "513100.SH", "518880.SH", "159985.SZ", "513520.SH"]
TURTLE_EQUITY_PATH = REPO / "results" / "turtle_equity.csv"


def compute_metrics(eq: pd.Series, init: float) -> dict:
    dr = eq.pct_change().dropna()
    yrs = max(0.5, len(dr) / 252)
    tr = eq.iloc[-1] / init - 1
    cagr = (1 + tr) ** (1 / yrs) - 1
    sharpe = np.sqrt(252) * dr.mean() / dr.std() if dr.std() > 1e-10 else 0
    peak = eq.expanding().max()
    mdd = ((eq - peak) / peak).min()
    vol = dr.std() * np.sqrt(252)
    calmar = cagr / abs(mdd) if mdd != 0 else 0
    return {"CAGR": cagr, "夏普": sharpe, "MDD": mdd, "年化波动": vol,
            "Calmar": calmar, "总收益": tr, "终值": eq.iloc[-1], "天数": len(eq)}


def run_n_portfolio() -> tuple[pd.Series, float]:
    """N 字结构：逐品种运行，合并为组合净值。"""
    strategy = NStructureStrategy(
        window_size=100, atr_period=25,
        stop_mult=2.0, trail_mult=5.0,
        add_step=0.5, max_units=5,
        max_reentries=1,
        use_ma5_confirm=False,
    )

    capital_per_etf = 100000
    all_eq = {}
    total_cap = 0

    for sym in SYMBOLS:
        df = load_data(sym, START, END, DATA_DIR)
        if df is None or df.empty:
            continue

        _, trades = strategy.run(df, symbol=sym, verbose=False)

        equity = pd.Series(index=pd.to_datetime(df["date"]), dtype=float)
        equity[:] = capital_per_etf
        running = float(capital_per_etf)

        for t in trades:
            if 0 < t.exit_idx < len(df):
                running += t.pnl
                equity.iloc[t.exit_idx:] = running

        equity = equity.ffill()
        all_eq[sym] = equity
        total_cap += capital_per_etf

        n_wins = sum(1 for t in trades if t.pnl > 0)
        total_pnl = sum(t.pnl for t in trades)
        print(f"  {sym:<12} {len(trades):>2}笔 {n_wins/max(1,len(trades)):>5.0%}  "
              f"盈亏{total_pnl:>+9,.0f}  终值{equity.iloc[-1]:>9,.0f}")

    if not all_eq:
        return pd.Series(dtype=float), 0

    portfolio = pd.concat(all_eq.values(), axis=1).sum(axis=1)
    return portfolio, total_cap


def main():
    print("=" * 60)
    print("  🏆 50/50 组合回测：N字结构 + 海龟系统")
    print("=" * 60)

    # ── 1. N 字 ──
    print(f"\n  📌 N字结构（100k/品种 × 6 = 600k 总资金）")
    n_portfolio, n_capital = run_n_portfolio()
    n_metrics = compute_metrics(n_portfolio, n_capital)
    print(f"  {'─'*40}")
    print(f"  N字组合终值: {n_portfolio.iloc[-1]:>10,.0f}  初始: {n_capital:>10,.0f}")
    print(f"  CAGR: {n_metrics['CAGR']:.1%}  MDD: {n_metrics['MDD']:.1%}  "
          f"夏普: {n_metrics['夏普']:.2f}")

    # ── 2. 海龟 ──
    t_capital = 600000
    print(f"\n  📌 海龟系统（6品种组合交易 600k）")
    if not TURTLE_EQUITY_PATH.exists():
        print(f"  ❌ 未找到 {TURTLE_EQUITY_PATH}")
        print(f"  请先运行: py scripts/export_turtle_equity.py")
        return

    t_df = pd.read_csv(TURTLE_EQUITY_PATH)
    t_df["date"] = pd.to_datetime(t_df["date"])
    t_equity = t_df.set_index("date")["equity"].sort_index()
    t_metrics = compute_metrics(t_equity, t_capital)
    print(f"  海龟终值: {t_equity.iloc[-1]:>10,.0f}  初始: {t_capital:>10,.0f}")
    print(f"  CAGR: {t_metrics['CAGR']:.1%}  MDD: {t_metrics['MDD']:.1%}  "
          f"夏普: {t_metrics['夏普']:.2f}")

    # ── 3. 合并 ──
    print(f"\n  📌 50/50 组合")
    common_dates = n_portfolio.index.intersection(t_equity.index)
    n_aligned = n_portfolio.loc[common_dates]
    t_aligned = t_equity.loc[common_dates]

    # 50/50：各策略承担一半风险
    # 组合净值 = 0.5 × N字净值 + 0.5 × 海龟净值
    combined = 0.5 * n_aligned + 0.5 * t_aligned
    combined_init = n_capital // 2 + t_capital // 2
    c_metrics = compute_metrics(combined, combined_init)

    # ── 4. 三方对比 ──
    print(f"\n{'='*60}")
    print(f"  📊 三方对比")
    print(f"{'='*60}")
    print(f"\n{'指标':<15} {'N字(600k)':>10} {'海龟(600k)':>10} {'50/50组合':>10}")
    print(f"{'-'*45}")
    print(f"{'CAGR':<15} {n_metrics['CAGR']:>9.1%} {t_metrics['CAGR']:>9.1%} "
          f"{c_metrics['CAGR']:>9.1%}")
    print(f"{'夏普':<15} {n_metrics['夏普']:>9.2f} {t_metrics['夏普']:>9.2f} "
          f"{c_metrics['夏普']:>9.2f}")
    print(f"{'最大回撤':<15} {n_metrics['MDD']:>9.1%} {t_metrics['MDD']:>9.1%} "
          f"{c_metrics['MDD']:>9.1%}")
    print(f"{'年化波动':<15} {n_metrics['年化波动']:>9.1%} {t_metrics['年化波动']:>9.1%} "
          f"{c_metrics['年化波动']:>9.1%}")
    print(f"{'Calmar':<15} {n_metrics['Calmar']:>9.2f} {t_metrics['Calmar']:>9.2f} "
          f"{c_metrics['Calmar']:>9.2f}")
    print(f"{'终值':<15} {n_metrics['终值']:>9,.0f} {t_metrics['终值']:>9,.0f} "
          f"{c_metrics['终值']:>9,.0f}")

    # ── 5. 结论 ──
    print(f"\n🔍  结论")
    print(f"  {'='*50}")

    best_sharpe = max(n_metrics['夏普'], t_metrics['夏普'])
    best_mdd = min(n_metrics['MDD'], t_metrics['MDD'])
    best_cagr = max(n_metrics['CAGR'], t_metrics['CAGR'])

    checks = [
        ("夏普提升", c_metrics['夏普'] > best_sharpe,
         f"{c_metrics['夏普']:.2f} vs {best_sharpe:.2f}"),
        ("回撤降低", c_metrics['MDD'] < best_mdd,
         f"{c_metrics['MDD']:.1%} vs {best_mdd:.1%}"),
        ("收益接近最优", c_metrics['CAGR'] >= best_cagr * 0.85,
         f"{c_metrics['CAGR']:.1%} vs {best_cagr:.1%}"),
    ]

    for label, ok, detail in checks:
        print(f"  {'✅' if ok else '⚠️'} {label:<16} {detail}")

    # 保存
    out = REPO / "results" / "combined_equity.csv"
    pd.DataFrame({
        "date": common_dates,
        "n_equity": n_aligned.values,
        "t_equity": t_aligned.values,
        "combined": combined.values,
    }).to_csv(out, index=False)
    print(f"\n  组合净值已保存: {out}")
    print()


if __name__ == "__main__":
    main()
