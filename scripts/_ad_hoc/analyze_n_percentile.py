#!/usr/bin/env python
"""市场状态判断器历史验证 — 逐年 state 分布 vs 策略收益"""

from __future__ import annotations
import sys, logging
from pathlib import Path
import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.market_regime import MarketRegime
from src.turtle_core import compute_tr, compute_atr
from scripts.run_backtest import run_backtest

logging.basicConfig(level=logging.WARNING)

ETF_SYMBOLS = [
    ("510500.SH", "中证500"), ("159845.SZ", "中证1000"),
    ("159915.SZ", "创业板"),   ("588000.SH", "科创50"),
    ("513100.SH", "纳指ETF"),  ("518880.SH", "黄金ETF"),
]

print("=" * 100)
print("市场状态判断器 · 历史验证")
print("=" * 100)

# 对每个品种、每年验证
regime = MarketRegime()
all_results = []

for code, name in ETF_SYMBOLS:
    path = ROOT / "data" / "etf_daily" / f"{code}.parquet"
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    # 逐日计算 state
    states = []
    scores = []
    for _, r in df.iterrows():
        s = regime.update(r["date"], r["close"], r["high"], r["low"])
        states.append(s)
        scores.append(regime.score)
    df["state"] = states
    df["score"] = scores
    df["year"] = df["date"].dt.year

    # 逐年统计
    for yr in sorted(df["year"].unique()):
        yd = df[df["year"] == yr]
        total = len(yd)
        if total < 20:
            continue
        t = (yd["state"] == "trending").sum()
        c = (yd["state"] == "choppy").sum()
        tr = (yd["state"] == "transitional").sum()
        avg_score = yd["score"].mean()
        all_results.append({
            "year": yr, "symbol": name, "code": code,
            "n": total, "trending_pct": t / total,
            "choppy_pct": c / total, "transitional_pct": tr / total,
            "avg_score": avg_score,
        })

# 按年汇总所有品种均值
print(f"\n{'年份':>6s}  {'趋势%':>6s}  {'碎步%':>6s}  {'过渡%':>6s}  {'平均score':>9s}  {'策略收益':>8s}")
print("-" * 60)

regime_scores = {}
for yr in sorted(set(r["year"] for r in all_results)):
    yr_data = [r for r in all_results if r["year"] == yr]
    if not yr_data:
        continue
    avg_t = np.mean([r["trending_pct"] for r in yr_data])
    avg_c = np.mean([r["choppy_pct"] for r in yr_data])
    avg_tr = np.mean([r["transitional_pct"] for r in yr_data])
    avg_score = np.mean([r["avg_score"] for r in yr_data])
    regime_scores[yr] = {"score": avg_score, "trending": avg_t, "choppy": avg_c}

    # 获取策略收益
    ret_str = "N/A"
    if yr >= 2021:
        r = run_backtest(start_date=f"{yr}-01-01", end_date=f"{yr}-12-31", mode="A", quiet=True)
        if r:
            ret_str = f"{r['total_return_pct']:>+7.2f}%"
    print(f"{yr:>6d}  {avg_t:>5.1%}  {avg_c:>5.1%}  {avg_tr:>5.1%}  {avg_score:>8.3f}  {ret_str:>8s}")

print("-" * 60)

# 按 state 分组统计收益
print(f"\n{'=' * 60}")
print("按年度平均 score 分组 vs 策略收益")
print(f"{'-' * 60}")
for yr in sorted(regime_scores.keys()):
    if yr < 2021:
        continue
    r = run_backtest(start_date=f"{yr}-01-01", end_date=f"{yr}-12-31", mode="A", quiet=True)
    if not r:
        continue
    s = regime_scores[yr]
    print(f"{yr}:  score={s['score']:.3f}  趋势={s['trending']:.1%}  碎步={s['choppy']:.1%}  →  收益={r['total_return_pct']:>+7.2f}%")

# 相关系数
print(f"\n{'=' * 60}")
years_list = [yr for yr in regime_scores if yr >= 2021]
scores_list = [regime_scores[yr]["score"] for yr in years_list]
rets_list = []
for yr in years_list:
    r = run_backtest(start_date=f"{yr}-01-01", end_date=f"{yr}-12-31", mode="A", quiet=True)
    rets_list.append(r["total_return_pct"] if r else 0)
if len(scores_list) > 2:
    corr = np.corrcoef(scores_list, rets_list)[0, 1]
    print(f"\nRegime Score vs 策略年收益 皮尔逊相关系数: {corr:.4f}")

    # Spearman秩相关
    rx = pd.Series(scores_list).rank().values
    ry = pd.Series(rets_list).rank().values
    d = rx - ry
    n = len(rx)
    sc = 1 - (6 * sum(d**2)) / (n * (n**2 - 1)) if n > 1 else 0
    print(f"Regime Score vs 策略年收益 斯皮尔曼相关系数: {sc:.4f}")

    # 最优分割阈值
    best_gap = -999
    best_t = 0.5
    for thresh in np.arange(0.3, 0.8, 0.05):
        above = [r for s, r in zip(scores_list, rets_list) if s >= thresh]
        below = [r for s, r in zip(scores_list, rets_list) if s < thresh]
        if len(above) < 1 or len(below) < 1:
            continue
        gap = np.mean(above) - np.mean(below)
        if gap > best_gap:
            best_gap = gap
            best_t = thresh
    above_list = [r for s, r in zip(scores_list, rets_list) if s >= best_t]
    below_list = [r for s, r in zip(scores_list, rets_list) if s < best_t]
    print(f"\n最优分割阈值: score ≈ {best_t:.2f}")
    print(f"  score < {best_t:.2f}: {len(below_list)}年, 平均收益={np.mean(below_list):>+7.2f}%")
    print(f"  score > {best_t:.2f}: {len(above_list)}年, 平均收益={np.mean(above_list):>+7.2f}%")
    print(f"  两组差距: {best_gap:>+7.2f}%")

print(f"\n{'=' * 60}")
print("完成")
