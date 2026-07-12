#!/usr/bin/env python
"""
N字策略 · 进场质量诊断

分析每笔交易的 N字结构特征 + 市场状态，按退出原因分组对比，
找出初始止损（0%胜率）与盈利交易的结构性差异。

用法：
    py scripts/diagnose_n_entries.py                    # 默认 OOS 2020-2026
    py scripts/diagnose_n_entries.py --start 2014-01-01 --end 2020-01-01  # IS
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

# Windows 控制台 UTF-8 编码
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from strategies.n_structure import (
    NStructureStrategy, find_n_structure_in_window,
    compute_atr, compute_ma,
)

DEFAULT_SYMBOLS = [
    "510500.SH", "159915.SZ", "513100.SH",
    "518880.SH", "159985.SZ", "513520.SH",
]
DATA_DIR = REPO_DIR / "data" / "etf_daily"


def load_data(symbol: str, start: str, end: str | None = None) -> pd.DataFrame:
    path = DATA_DIR / f"{symbol}.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    mask = df["date"] >= start
    if end is not None:
        mask &= df["date"] <= end
    df = df[mask].copy()
    if df.empty:
        return df
    df.sort_values("date", inplace=True)
    df.reset_index(drop=True, inplace=True)
    df["date"] = pd.to_datetime(df["date"])
    return df


def extract_trade_features(
    df: pd.DataFrame, strategy: NStructureStrategy, trades: list,
) -> pd.DataFrame:
    """提取每笔交易的 N字结构特征 + 市场状态。"""
    rows = []
    for t in trades:
        if t.entry_idx < strategy.window_size:
            continue

        entry_i = t.entry_idx
        entry_date = df.loc[entry_i, "date"]

        # ── 重新提取该笔交易入场时的 N字结构 ──
        ns = find_n_structure_in_window(
            df, entry_i, strategy.window_size,
            confirm_k=strategy.confirm_k,
            min_advance=strategy.min_advance,
            min_gap_ad=strategy.min_gap_ad,
            min_gap_db=strategy.min_gap_db,
            local_half_window=strategy.local_half_window,
        )

        # ── 结构形态特征 ──
        if ns is not None:
            ad_advance  = (ns.d_price - ns.a_price) / ns.a_price   # A→D 涨幅
            db_retrace  = (ns.d_price - ns.b_price) / ns.d_price   # D→B 回调深度
            ab_advance  = (ns.b_price - ns.a_price) / ns.a_price   # A→B 抬高幅度
            ad_bars     = ns.d_idx - ns.a_idx                      # A→D K线数
            db_bars     = ns.b_idx - ns.d_idx                      # D→B K线数
            b_to_entry  = entry_i - ns.b_idx                       # B确认→进场 K线数
        else:
            ad_advance = db_retrace = ab_advance = np.nan
            ad_bars = db_bars = b_to_entry = np.nan

        # ── 市场状态特征 ──
        atr = df.loc[entry_i - 1, "atr"] if entry_i > 0 else np.nan
        atr_pct = atr / df.loc[entry_i, "close"] if not pd.isna(atr) else np.nan

        # MA 状态
        ma5  = df.loc[entry_i - 1, "ma5"]  if entry_i > 0 else np.nan
        ma20 = df.loc[entry_i - 1, "ma20"] if entry_i > 0 and "ma20" in df.columns else np.nan
        ma5_ma20_spread = (ma5 - ma20) / ma20 if (pd.notna(ma5) and pd.notna(ma20) and ma20 > 0) else np.nan

        # MA50/MA200 大趋势
        close_hist = df["close"].iloc[:entry_i]
        if len(close_hist) >= 50:
            ma50 = close_hist.iloc[-50:].mean()
            above_ma50 = df.loc[entry_i, "close"] > ma50
        else:
            ma50 = np.nan
            above_ma50 = np.nan
        if len(close_hist) >= 200:
            ma200 = close_hist.iloc[-200:].mean()
            above_ma200 = df.loc[entry_i, "close"] > ma200
        else:
            ma200 = np.nan
            above_ma200 = np.nan

        # 波动率特征
        if entry_i >= 20:
            returns = df["close"].iloc[entry_i - 20:entry_i].pct_change().dropna()
            vol_20d = returns.std() * np.sqrt(252) if len(returns) > 0 else np.nan
        else:
            vol_20d = np.nan

        # ── 进场价格特征 ──
        b_dist = (t.entry_price - t.b_price) / t.b_price if t.b_price > 0 else np.nan  # 超B幅度
        d_broken_at_entry = t.entry_price > t.d_price  # 进场时是否已突破D
        stop_distance = (t.entry_price - strategy.stop_mult * atr - t.b_price) / t.entry_price if (pd.notna(atr) and t.entry_price > 0) else np.nan

        rows.append({
            "品种":     t.symbol,
            "日期":     entry_date,
            "盈亏":     t.pnl,
            "退出原因": t.exit_reason,
            "持仓天数": t.exit_idx - t.entry_idx,
            "单位数":   t.units,
            # 结构形态
            "AD涨幅%":  ad_advance * 100 if pd.notna(ad_advance) else np.nan,
            "DB回调%":  db_retrace * 100 if pd.notna(db_retrace) else np.nan,
            "AB抬高%":  ab_advance * 100 if pd.notna(ab_advance) else np.nan,
            "AD_K线数": ad_bars,
            "DB_K线数": db_bars,
            "B→进场K线": b_to_entry,
            "A价":      ns.a_price if ns else np.nan,
            "D价":      ns.d_price if ns else np.nan,
            "B价":      ns.b_price if ns else np.nan,
            "进场价":   t.entry_price,
            "超B幅度%": b_dist * 100 if pd.notna(b_dist) else np.nan,
            "进场破D":  d_broken_at_entry,
            # 市场状态
            "ATR":      atr,
            "ATR%":     atr_pct * 100 if pd.notna(atr_pct) else np.nan,
            "MA5_20差%": ma5_ma20_spread * 100 if pd.notna(ma5_ma20_spread) else np.nan,
            ">MA50":    above_ma50,
            ">MA200":   above_ma200,
            "波动率20d": vol_20d,
        })

    return pd.DataFrame(rows)


def print_group_stats(df: pd.DataFrame, label: str, group_df: pd.DataFrame):
    """打印一组交易的统计摘要。"""
    if group_df.empty:
        print(f"\n  ── {label}: 0 笔 ──")
        return
    print(f"\n  ── {label}: {len(group_df)} 笔 ──")
    numeric_cols = ["盈亏", "持仓天数", "AD涨幅%", "DB回调%", "AB抬高%",
                    "AD_K线数", "DB_K线数", "B→进场K线", "超B幅度%",
                    "ATR", "ATR%", "MA5_20差%", "波动率20d"]
    for col in numeric_cols:
        if col in group_df.columns and group_df[col].notna().sum() > 0:
            vals = group_df[col].dropna()
            print(f"    {col:<12s}:  均值={vals.mean():>8.3f}  中位={vals.median():>8.3f}  "
                  f"min={vals.min():>8.3f}  max={vals.max():>8.3f}")

    # 进场破D比例
    if "进场破D" in group_df.columns:
        d_broken = group_df["进场破D"].sum()
        print(f"    进场破D比例: {d_broken}/{len(group_df)} ({d_broken/len(group_df):.0%})")

    # >MA50 / >MA200 比例
    for col in [">MA50", ">MA200"]:
        if col in group_df.columns:
            yes = group_df[col].dropna().sum()
            total = group_df[col].dropna().count()
            if total > 0:
                print(f"    {col}: {yes}/{total} ({yes/total:.0%})")


def main():
    parser = argparse.ArgumentParser(description="N字策略进场质量诊断")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--verbose", action="store_true",
                        help="打印每笔交易详情")
    args = parser.parse_args()

    strategy = NStructureStrategy(
        window_size=60, atr_period=25,             # S39
        stop_mult=1.5,
        d_timeout_days=7,                          # S40: 40→7
        add_step=1.5, max_units=4,                 # S39/S40
        add_weights=(0.5, 0.8, 1.5, 0.8),         # S40
        ma_trend=50, use_ma5_confirm=False,
        initial_capital=100000, risk_per_trade=0.01,
        max_reentries=0, num_symbols=6,
        confirm_k=3, min_advance=0.05,
        min_gap_ad=5, min_gap_db=3, local_half_window=2,
        slippage_pct=0.001, commission_pct=0.00015,
        use_dynamic_equity=True,
        max_consecutive_losses=5, pause_bars=20,
        stop_floor_pre_break=0.95,                 # S39: 0.93→0.95
        stop_floor_post_break=0.95,
        use_ma_cross=True, max_position_pct=0.25,
        entry_confirm_bars=2,
        trail_pre_d=2.5,                           # S39
        use_ma_exit=True,                          # S39
        ma_exit_period=20, ma_exit_margin=0.97,    # S39
        ma_exit_bearish=True,                      # S39
        d_exit_floor=0.95,                         # S39
    )

    all_rows = []

    for sym in args.symbols:
        df = load_data(sym, args.start, args.end)
        if df.empty:
            print(f"⚠️  {sym}: 无数据，跳过")
            continue

        _, trades, _ = strategy.run(df, symbol=sym, verbose=False)
        if not trades:
            print(f"⚠️  {sym}: 无交易")
            continue

        # compute_indicators 已在 run() 中调用，直接使用
        df = strategy.compute_indicators(df)
        features = extract_trade_features(df, strategy, trades)
        all_rows.append(features)

    if not all_rows:
        print("❌ 未产生任何交易")
        return

    full = pd.concat(all_rows, ignore_index=True)

    # ── 按退出原因分组 ──
    print("\n" + "=" * 70)
    print(f"  N字策略 · 进场质量诊断  ({args.start} ~ {args.end or '末尾'})")
    print("=" * 70)

    reasons = full["退出原因"].value_counts().index.tolist()
    for reason in reasons:
        group = full[full["退出原因"] == reason]
        print_group_stats(full, reason, group)

    # ── 主力出场方式：盈利 vs 亏损对比 ──
    # 找到交易笔数最多的非初始止损出场方式
    exit_counts = full["退出原因"].value_counts()
    primary_exit = [r for r in exit_counts.index if r != "初始止损"]
    if primary_exit:
        primary = primary_exit[0]
        print("\n" + "-" * 50)
        print(f"  {primary} 子组对比：盈利 vs 亏损")
        primary_trades = full[full["退出原因"] == primary]
        primary_wins = primary_trades[primary_trades["盈亏"] > 0]
        primary_losses = primary_trades[primary_trades["盈亏"] < 0]
        print(f"  总{len(primary_trades)}笔, 盈利{len(primary_wins)}笔, 亏损{len(primary_losses)}笔, 胜率{len(primary_wins)/max(1,len(primary_trades)):.1%}")
        print_group_stats(full, f"{primary}·盈利", primary_wins)
        print_group_stats(full, f"{primary}·亏损", primary_losses)

    # ── 关键差异总结 ──
    init_stop = full[full["退出原因"] == "初始止损"]
    if primary_exit:
        primary_wins_2 = full[(full["退出原因"] == primary_exit[0]) & (full["盈亏"] > 0)]
    else:
        primary_wins_2 = pd.DataFrame()

    print("\n" + "=" * 70)
    main_label = f"{primary_exit[0]}·盈利" if primary_exit else "跟踪止损·盈利"
    print(f"  🔍 关键差异: 初始止损 vs {main_label}")
    print("=" * 70)

    comparisons = [
        ("AD涨幅%",        "N字反弹力度"),
        ("DB回调%",        "回调深度（越小越浅）"),
        ("AB抬高%",        "B点抬高幅度"),
        ("超B幅度%",       "进场超B幅度"),
        ("ATR%",           "波动率水平"),
        ("MA5_20差%",      "短期趋势强度"),
        ("AD_K线数",       "A→D跨度"),
        ("B→进场K线",      "B确认→进场延迟"),
        ("波动率20d",      "20日波动率"),
    ]

    for col, desc in comparisons:
        init_val = init_stop[col].dropna().median() if col in init_stop.columns else np.nan
        win_val = primary_wins_2[col].dropna().median() if col in primary_wins_2.columns and len(primary_wins_2) > 0 else np.nan
        if pd.isna(init_val) or pd.isna(win_val):
            continue
        diff = init_val - win_val
        direction = "↑" if diff > 0 else "↓"
        print(f"  {desc:<16s}:  初始止损={init_val:>8.3f}  |  盈利跟踪={win_val:>8.3f}  |  "
              f"差值={diff:>+8.3f} {direction}")

    if args.verbose:
        print("\n" + "=" * 70)
        print("  逐笔交易详情")
        print("=" * 70)
        pd.set_option("display.max_columns", 20)
        pd.set_option("display.width", 200)
        pd.set_option("display.max_rows", 500)
        for _, row in full.iterrows():
            direction = "🟢" if row["盈亏"] > 0 else "🔴"
            print(f"\n{direction} {row['品种']} {str(row['日期'])[:10]}  "
                  f"盈亏={row['盈亏']:>+8.0f}  {row['退出原因']}  "
                  f"持仓={row['持仓天数']}天  "
                  f"AD涨={row['AD涨幅%']:.1f}%  "
                  f"DB回调={row['DB回调%']:.1f}%  "
                  f"超B={row['超B幅度%']:.1f}%  "
                  f"ATR%={row['ATR%']:.2f}%  "
                  f"MA5_20差={row['MA5_20差%']:.2f}%  "
                  f">MA50={row['>MA50']}  >MA200={row['>MA200']}")


if __name__ == "__main__":
    main()
