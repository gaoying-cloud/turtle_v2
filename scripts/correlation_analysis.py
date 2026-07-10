#!/usr/bin/env python
"""
策略信号相关性分析：N字结构 vs 海龟系统

分析维度：
  1. 每日持仓重叠率 —— 同一品种上，两者同时持仓的天数占比
  2. 每日收益相关性 —— 日收益率序列的 Pearson 相关系数
  3. 多空方向一致性 —— 同方向持仓的天数占比
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
START = "2015-01-01"  # 留 250 bar 给 MA250 预热
END = "2026-07-09"
SYMBOLS = ["510500.SH", "159915.SZ", "513100.SH", "518880.SH", "159985.SZ", "513520.SH"]


def generate_n_daily_positions() -> dict[str, pd.DataFrame]:
    """生成 N 字结构策略每日持仓记录。

    返回值：{symbol: DataFrame}，带 date, position(0/1) 列
    """
    strategy = NStructureStrategy(
        window_size=100, atr_period=25,
        stop_mult=2.0, trail_mult=5.0,
        add_step=0.5, max_units=5,
        profit_protect_mult=15, max_reentries=1,
        use_ma5_confirm=False,
    )

    all_positions = {}
    for symbol in SYMBOLS:
        df = load_data(symbol, START, END, DATA_DIR)
        if df is None or df.empty:
            continue

        # 在 compute_indicators 后的 df 上跑策略，同时记录每日持仓
        df_ind = strategy.compute_indicators(df)
        n = len(df_ind)
        position = np.zeros(n, dtype=int)
        active = False

        for i in range(strategy.window_size, n):
            if active:
                # 持仓管理（简化：只跟踪状态变化）
                low = df_ind.loc[i, 'low']
                high = df_ind.loc[i, 'high']
                close = df_ind.loc[i, 'close']

                # 止损
                if low <= getattr(strategy, '_last_stop', 0):
                    active = False
                    position[i] = 0
                    continue

                # D 点突破失败
                if (not getattr(strategy, '_d_broken', False)
                        and i - getattr(strategy, '_entry_idx', i) > 30
                        and close <= getattr(strategy, '_d_price', float('inf'))):
                    active = False
                    position[i] = 0
                    continue

                position[i] = 1

                # 更新最高价
                if close > getattr(strategy, '_highest', 0):
                    strategy._highest = close

                # D 点突破
                if (not getattr(strategy, '_d_broken', False)
                        and close > getattr(strategy, '_d_price', float('inf'))):
                    strategy._d_broken = True

                continue

            # 无持仓：检查进场
            prev = i - 1
            if prev < 1:
                continue

            ns = find_n_structure_in_window(df_ind, prev, 100)
            if ns is None:
                continue

            prev_close = df_ind.loc[prev, 'close']
            prev_ma250 = df_ind.loc[prev, 'ma250']
            if pd.isna(prev_ma250):
                continue
            if prev_close <= ns.b_price or prev_close <= prev_ma250:
                continue

            # 进场
            atr = df_ind.loc[prev, 'atr']
            if pd.isna(atr) or atr <= 0:
                continue

            entry_price = df_ind.loc[i, 'open']
            stop = min(ns.b_price - 2.0 * atr, ns.b_price * 0.95)

            active = True
            position[i] = 1
            strategy._last_stop = stop
            strategy._d_broken = entry_price > ns.d_price
            strategy._d_price = ns.d_price
            strategy._entry_idx = i
            strategy._highest = entry_price

        pos_df = pd.DataFrame({
            "date": df_ind["date"],
            "position": position,
        })
        all_positions[symbol] = pos_df

    return all_positions


def find_n_structure_in_window(df, end_idx, window_size=100):
    """快速版 N 字结构检测，复用策略模块的函数。"""
    from strategies.n_structure import find_n_structure_in_window as find_ns
    return find_ns(df, end_idx, window_size)


def generate_turtle_simple_positions() -> dict[str, pd.DataFrame]:
    """简化海龟策略：20日突破进场 + 10日反向突破出场 + 2×ATR止损。

    不涉及 Backtrader、风险平价、退化检测等复杂逻辑。
    只生成每日持仓状态，不计算 PnL。
    """
    from src.turtle_core import compute_atr, donchian_high, donchian_low

    all_positions = {}
    for symbol in SYMBOLS:
        df = load_data(symbol, START, END, DATA_DIR)
        if df is None or df.empty:
            continue

        # 计算信号
        df = df.copy()
        df["atr"] = compute_atr(compute_tr(df["high"], df["low"], df["close"]), 25)
        df["entry_channel"] = donchian_high(df["high"], 20)  # 20日突破进场
        df["exit_channel"] = donchian_low(df["low"], 10)     # 10日反向突破出场

        n = len(df)
        position = np.zeros(n, dtype=int)
        active = False
        entry_price = 0.0
        stop_loss = 0.0
        units = 0
        max_units = 4
        unit_step = 0.5
        pyramid_level = 0.0

        for i in range(50, n):  # 50 根预热
            if active:
                # 止损
                if df.loc[i, "low"] <= stop_loss:
                    active = False
                    position[i] = 0
                    continue

                # 10日反向突破出场
                if df.loc[i, "close"] < df.loc[i, "exit_channel"]:
                    active = False
                    position[i] = 0
                    continue

                position[i] = units

                # 加仓：每涨 0.5×ATR 加一单位
                if units < max_units:
                    atr = df.loc[i, "atr"]
                    if not pd.isna(atr) and atr > 0:
                        next_level = entry_price + (units * unit_step * atr)
                        if df.loc[i, "high"] >= next_level:
                            units += 1
                            stop_loss = max(stop_loss,
                                            df.loc[i, "high"] - 2.0 * atr)

                # 跟踪止损
                atr = df.loc[i, "atr"]
                if not pd.isna(atr) and atr > 0:
                    new_stop = df.loc[i, "high"] - 2.0 * atr
                    stop_loss = max(stop_loss, new_stop)

                continue

            # 无持仓：检查 20 日突破进场
            if pd.isna(df.loc[i, "entry_channel"]):
                continue

            if df.loc[i, "close"] > df.loc[i, "entry_channel"]:
                atr = df.loc[i, "atr"]
                if pd.isna(atr) or atr <= 0:
                    continue

                active = True
                entry_price = df.loc[i, "close"]
                units = 1
                stop_loss = entry_price - 2.0 * atr
                pyramid_level = entry_price + unit_step * atr
                position[i] = 1

        all_positions[symbol] = pd.DataFrame({
            "date": df["date"],
            "position": position,
        })

    return all_positions


def compute_tr(high, low, close):
    """简化版 TR 计算。"""
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def compute_correlation(n_pos: dict[str, pd.DataFrame],
                        t_pos: dict[str, pd.DataFrame]) -> None:
    """计算两个策略的持仓相关性和收益相关性。"""
    print("\n" + "=" * 65)
    print("  📊 策略信号相关性分析")
    print("=" * 65)

    print(f"\n{'品种':<12} {'N字持仓%':>8} {'海龟持仓%':>8} {'重叠率':>8} "
          f"{'方向一致%':>8} {'日收益相关':>8}")
    print("-" * 60)

    all_overlaps = []
    all_corrs = []

    for sym in SYMBOLS:
        np_df = n_pos.get(sym)
        tp_df = t_pos.get(sym)
        if np_df is None or tp_df is None or np_df.empty or tp_df.empty:
            print(f"{sym:<12} {'无数据':>8}")
            continue

        # 对齐日期
        merged = pd.merge(np_df, tp_df, on="date", how="inner", suffixes=("_n", "_t"))
        if merged.empty:
            continue

        n_pos_pct = merged["position_n"].mean()
        t_pos_pct = merged["position_t"].mean()

        # 重叠率：两者都持仓的天数占比
        both_in = (merged["position_n"] == 1) & (merged["position_t"] == 1)
        overlap = both_in.sum() / len(merged)

        # 方向一致率：同方向（都持仓或都空仓）的天数占比
        same_dir = (merged["position_n"] == merged["position_t"])
        direction_agree = same_dir.mean()

        # 日收益相关性（从原始数据加载 close 计算）
        df = load_data(sym, START, END, DATA_DIR)
        if df is not None and not df.empty:
            close_df = df[["date", "close"]].copy()
            close_df["date"] = pd.to_datetime(close_df["date"])
            merged2 = pd.merge(merged, close_df, on="date", how="left")

            # 计算每日收益（仅限持仓日）
            merged2["return"] = merged2["close"].pct_change()
            merged2["ret_n"] = merged2["return"] * merged2["position_n"]
            merged2["ret_t"] = merged2["return"] * merged2["position_t"]

            # 只计算有持仓的交易日
            trading_days = merged2[(merged2["position_n"] == 1) | (merged2["position_t"] == 1)]
            if len(trading_days) > 10:
                corr = trading_days["ret_n"].corr(trading_days["ret_t"])
            else:
                corr = 0
        else:
            corr = 0

        print(f"{sym:<12} {n_pos_pct:>7.1%} {t_pos_pct:>7.1%} "
              f"{overlap:>7.1%} {direction_agree:>7.1%} {corr:>7.3f}")

        all_overlaps.append(overlap)
        all_corrs.append(corr)

    # 汇总
    if all_overlaps:
        print("-" * 60)
        print(f"{'平均':<12} {'':>8} {'':>8} "
              f"{np.mean(all_overlaps):>7.1%} {'':>8} {np.mean(all_corrs):>7.3f}")

    print(f"\n🔍  解读")
    avg_overlap = np.mean(all_overlaps) if all_overlaps else 0
    avg_corr = np.mean(all_corrs) if all_corrs else 0

    if avg_overlap < 0.2:
        print(f"  重叠率 {avg_overlap:.1%} → ✅ 低重叠，两者交易时间差异大")
        print(f"  → 组合可平滑收益曲线，降低集中度风险")
    elif avg_overlap < 0.5:
        print(f"  重叠率 {avg_overlap:.1%} → ⚠️ 中等重叠，部分时间同步")
        print(f"  → 组合有一定分散效果")
    else:
        print(f"  重叠率 {avg_overlap:.1%} → ❌ 高重叠，两者几乎同步")
        print(f"  → 组合意义有限")

    if avg_corr < 0.3:
        print(f"  日收益相关 {avg_corr:.3f} → ✅ 低相关，收益来源不同")
    elif avg_corr < 0.7:
        print(f"  日收益相关 {avg_corr:.3f} → ⚠️ 中等相关")
    else:
        print(f"  日收益相关 {avg_corr:.3f} → ❌ 高相关")

    print(f"\n  建议: 需要计算组合 Sharpe 来决定是否合并")
    print()


def main():
    # 生成 N 字持仓
    print("🔄 生成 N 字结构每日持仓...")
    n_positions = generate_n_daily_positions()
    print(f"  完成: {len(n_positions)} 品种")

    # 生成海龟持仓（简化版）
    print("🔄 生成海龟策略每日持仓（简化版）...")
    t_positions = generate_turtle_simple_positions()
    print(f"  完成: {len(t_positions)} 品种")

    # 计算相关性
    compute_correlation(n_positions, t_positions)


if __name__ == "__main__":
    main()
