#!/usr/bin/env python
"""
50/50 等权组合回测：N字结构 + 海龟系统

方法：对每个 ETF，两策略各分配 50k，独立交易。
将各品种每日净值汇总为全组合净值，计算组合级指标。
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
from strategies.n_structure import (
    NStructureStrategy, find_n_structure_in_window,
    compute_atr as ns_compute_atr, compute_ma,
)

logging.basicConfig(level=logging.WARNING)

DATA_DIR = REPO / "data" / "etf_daily"
START = "2015-01-01"
END = "2026-07-09"
SYMBOLS = ["510500.SH", "159915.SZ", "513100.SH", "518880.SH", "159985.SZ", "513520.SH"]


# ════════════════════════════════════════════════════════════
#  N 字结构每日净值
# ════════════════════════════════════════════════════════════

def run_n_daily_equity(symbol: str, capital: float = 50000) -> pd.DataFrame:
    """N 字策略 + 每日净值追踪。"""
    df = load_data(symbol, START, END, DATA_DIR)
    if df is None or df.empty:
        return pd.DataFrame()

    strategy = NStructureStrategy(
        window_size=100, atr_period=25,
        stop_mult=2.0, trail_mult=5.0,
        add_step=0.5, max_units=5,
        profit_protect_mult=15, max_reentries=1,
        use_ma5_confirm=False,
        initial_capital=capital,
    )
    df_ind = strategy.compute_indicators(df)
    n = len(df_ind)
    equity_arr = np.full(n, np.nan)

    # 策略状态
    from strategies.n_structure import PositionState as PS
    pos = PS()
    cash = float(capital)
    shares = 0
    last_eq = float(capital)

    for i in range(strategy.window_size, n):
        if pos.active:
            low, high, close = (df_ind.loc[i, c] for c in ["low", "high", "close"])
            total_shares = pos.units * pos.shares_per_unit

            # 止损
            if low <= pos.stop_loss:
                exit_p = min(close, pos.stop_loss)
                cash += total_shares * exit_p
                pos.active = False
                shares = 0
                equity_arr[i] = cash
                continue

            # D 点突破失败
            if not pos.d_broken:
                if close > pos.d_price:
                    pos.d_broken = True
                elif i - pos.entry_idx > 30:
                    cash += total_shares * close
                    pos.active = False
                    shares = 0
                    equity_arr[i] = cash
                    continue

            # 已突破 D
            if pos.d_broken:
                atr_v = df_ind.loc[i, "atr"]
                if not pd.isna(atr_v) and atr_v > 0:
                    pos.stop_loss = max(pos.stop_loss, high - 5.0 * atr_v)

                    # 加仓
                    if pos.units < 5 and close >= pos.next_add_level:
                        new_shares = pos.shares_per_unit
                        cash -= new_shares * close
                        pos.units += 1
                        shares = pos.units * pos.shares_per_unit
                        pos.next_add_level = (pos.entry_price
                                              + pos.units * 0.5 * atr_v)

                    # 利润保护
                    if (not pos.profit_protected and pos.units > 1
                            and (close - pos.entry_price) / atr_v > 15):
                        exit_u = max(1, pos.units // 2)
                        cash += exit_u * pos.shares_per_unit * close
                        pos.units -= exit_u
                        pos.profit_protected = True

            # 当日净值
            shares = pos.units * pos.shares_per_unit
            equity_arr[i] = cash + shares * close

        else:
            # 检查进场
            if i > 1:
                prev = i - 1
                ns = find_n_structure_in_window(df_ind, prev, 100)
                if ns is not None:
                    pc = df_ind.loc[prev, "close"]
                    m250 = df_ind.loc[prev, "ma250"]
                    atr_v = df_ind.loc[prev, "atr"]
                    if (not pd.isna(m250) and pc > ns.b_price and pc > m250
                            and not pd.isna(atr_v) and atr_v > 0):
                        entry_p = df_ind.loc[i, "open"]
                        spu = strategy._calc_shares(capital, entry_p, atr_v)
                        if spu > 0:
                            stop = min(ns.b_price - 2.0 * atr_v, ns.b_price * 0.95)
                            cost = spu * entry_p
                            cash = last_eq - cost
                            pos.active = True
                            pos.entry_idx = i
                            pos.entry_price = entry_p
                            pos.stop_loss = stop
                            pos.d_price = ns.d_price
                            pos.b_price = ns.b_price
                            pos.a_price = ns.a_price
                            pos.units = 1
                            pos.shares_per_unit = spu
                            pos.next_add_level = entry_p + 0.5 * atr_v
                            pos.d_broken = entry_p > ns.d_price
                            pos.profit_protected = False
                            shares = spu
                            equity_arr[i] = cash + shares * entry_p
                            continue

            # 无持仓
            equity_arr[i] = last_eq
            shares = 0

        last_eq = equity_arr[i] if not np.isnan(equity_arr[i]) else last_eq

    equity_s = pd.Series(equity_arr).ffill()
    return pd.DataFrame({"date": df_ind["date"], "equity": equity_s})


# ════════════════════════════════════════════════════════════
#  海龟简化版每日净值
# ════════════════════════════════════════════════════════════

def compute_tr(h, l, c):
    pc = c.shift(1)
    return pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def run_t_daily_equity(symbol: str, capital: float = 50000) -> pd.DataFrame:
    """简化海龟每日净值。"""
    df = load_data(symbol, START, END, DATA_DIR)
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    df["atr"] = ns_compute_atr(df["high"], df["low"], df["close"], 25)
    df["entry_h"] = df["high"].shift(1).rolling(20).max()
    df["exit_l"] = df["low"].shift(1).rolling(10).min()
    df["ma250"] = compute_ma(df["close"], 250)

    n = len(df)
    eq = np.full(n, np.nan)
    eq[:50] = float(capital)

    active = False
    entry_p = 0.0
    stop = 0.0
    spu = 0
    units = 0
    shares = 0
    cash = float(capital)

    for i in range(50, n):
        if active:
            lo, hi, cl = df.loc[i, ["low", "high", "close"]]
            atr_v = df.loc[i, "atr"]

            # 止损
            if lo <= stop:
                cash += shares * min(cl, stop)
                active = False
                shares = 0
                eq[i] = cash
                continue

            # 10日反向突破
            if not pd.isna(df.loc[i, "exit_l"]) and cl < df.loc[i, "exit_l"]:
                cash += shares * cl
                active = False
                shares = 0
                eq[i] = cash
                continue

            # 加仓
            if units < 4 and not pd.isna(atr_v) and atr_v > 0:
                nxt = entry_p + units * 0.5 * atr_v
                if hi >= nxt:
                    units += 1
                    ns = spu
                    cash -= ns * cl
                    shares += ns
                    stop = max(stop, hi - 2.0 * atr_v)

            # 跟踪止损
            if not pd.isna(atr_v) and atr_v > 0:
                stop = max(stop, hi - 2.0 * atr_v)

            eq[i] = cash + shares * cl

        else:
            if pd.isna(df.loc[i, "entry_h"]):
                eq[i] = eq[i-1] if i > 0 and not np.isnan(eq[i-1]) else capital
                continue

            cl = df.loc[i, "close"]
            if cl > df.loc[i, "entry_h"]:
                m250 = df.loc[i, "ma250"]
                atr_v = df.loc[i, "atr"]
                if not pd.isna(m250) and cl > m250 and not pd.isna(atr_v) and atr_v > 0:
                    risk_amt = capital * 0.01
                    per_risk = 2.0 * atr_v
                    theoretical = risk_amt / per_risk
                    spu = max(100, int(theoretical / 100) * 100)
                    entry_p = cl
                    units = 1
                    shares = spu
                    stop = entry_p - 2.0 * atr_v
                    cost = spu * entry_p
                    cash = eq[i-1] - cost if i > 0 and not np.isnan(eq[i-1]) else capital - cost
                    active = True
                    eq[i] = cash + shares * cl
                    continue

            eq[i] = eq[i-1] if i > 0 and not np.isnan(eq[i-1]) else capital

    return pd.DataFrame({"date": df["date"], "equity": pd.Series(eq).ffill()})


# ════════════════════════════════════════════════════════════
#  组合 + 指标
# ════════════════════════════════════════════════════════════

def metrics(eq: pd.Series, init: float) -> dict:
    dr = eq.pct_change().dropna()
    yrs = len(dr) / 252
    tr = eq.iloc[-1] / init - 1
    cagr = (1 + tr) ** (1 / yrs) - 1 if yrs > 0 else 0
    sharpe = np.sqrt(252) * dr.mean() / dr.std() if dr.std() > 0 else 0
    peak = eq.expanding().max()
    mdd = ((eq - peak) / peak).min()
    vol = dr.std() * np.sqrt(252)
    return {"CAGR": cagr, "夏普": sharpe, "MDD": mdd, "波动": vol,
            "总收益": tr, "终值": eq.iloc[-1]}


def main():
    cap = 50000  # 每个策略每个品种的资金
    n_results = {}
    t_results = {}

    print("🔄 运行两个策略...")
    for sym in SYMBOLS:
        n_eq = run_n_daily_equity(sym, cap)
        t_eq = run_t_daily_equity(sym, cap)
        if not n_eq.empty:
            n_results[sym] = n_eq
        if not t_eq.empty:
            t_results[sym] = t_eq

    print("\n" + "=" * 65)
    print("  品种级组合表现（各 50k N字 + 50k 海龟 = 100k/品种）")
    print("=" * 65)
    print(f"{'品种':<10} {'N字终值':>10} {'海龟终值':>10} {'组合终值':>10} "
          f"{'组合CAGR':>8} {'组合夏普':>7} {'组合MDD':>7}")
    print("-" * 65)

    total_capital = 0
    total_final_n = 0
    total_final_t = 0
    total_final = 0

    for sym in SYMBOLS:
        if sym not in n_results or sym not in t_results:
            continue
        n_eq = n_results[sym]
        t_eq = t_results[sym]
        m = pd.merge(n_eq, t_eq, on="date", suffixes=("_n", "_t"), how="inner")
        m["combined"] = m["equity_n"] + m["equity_t"]
        m_cap = cap * 2
        met = metrics(m["combined"], m_cap)
        nv = n_eq["equity"].iloc[-1]
        tv = t_eq["equity"].iloc[-1]
        cv = m["combined"].iloc[-1]

        total_capital += m_cap
        total_final_n += nv
        total_final_t += tv
        total_final += cv

        print(f"{sym:<10} {nv:>10,.0f} {tv:>10,.0f} {cv:>10,.0f} "
              f"{met['CAGR']:>7.1%} {met['夏普']:>6.2f} {met['MDD']:>6.1%}")

    # ── 全组合 ──
    all_n = [n_results[s]["equity"] for s in SYMBOLS if s in n_results]
    all_t = [t_results[s]["equity"] for s in SYMBOLS if s in t_results]

    if all_n and all_t:
        # 对齐日期后汇总
        n_panel = pd.concat({s: n_results[s].set_index("date")["equity"]
                             for s in SYMBOLS if s in n_results}, axis=1)
        t_panel = pd.concat({s: t_results[s].set_index("date")["equity"]
                             for s in SYMBOLS if s in t_results}, axis=1)
        common_idx = n_panel.index.intersection(t_panel.index)
        n_sum = n_panel.loc[common_idx].sum(axis=1)
        t_sum = t_panel.loc[common_idx].sum(axis=1)
        portfolio = n_sum + t_sum

        met = metrics(portfolio, total_capital)

        print("-" * 65)
        print(f"{'全组合':<10} {total_final_n:>10,.0f} {total_final_t:>10,.0f} "
              f"{total_final:>10,.0f} {met['CAGR']:>7.1%} {met['夏普']:>6.2f} "
              f"{met['MDD']:>6.1%}")

        # 单策略全组合
        n_met = metrics(n_sum, cap * len(SYMBOLS))
        t_met = metrics(t_sum, cap * len(SYMBOLS))

        print(f"\n{'='*60}")
        print(f"  📊 三方最终对比")
        print(f"{'='*60}")
        print(f"\n{'指标':<15} {'N字单策略':>10} {'海龟单策略':>10} {'50/50组合':>10}")
        print(f"{'-'*45}")
        print(f"{'CAGR':<15} {n_met['CAGR']:>9.1%} {t_met['CAGR']:>9.1%} "
              f"{met['CAGR']:>9.1%}")
        print(f"{'夏普':<15} {n_met['夏普']:>9.2f} {t_met['夏普']:>9.2f} "
              f"{met['夏普']:>9.2f}")
        print(f"{'最大回撤':<15} {n_met['MDD']:>9.1%} {t_met['MDD']:>9.1%} "
              f"{met['MDD']:>9.1%}")
        print(f"{'年化波动':<15} {n_met['波动']:>9.1%} {t_met['波动']:>9.1%} "
              f"{met['波动']:>9.1%}")
        print(f"{'最终净值':<15} {n_sum.iloc[-1]:>9,.0f} {t_sum.iloc[-1]:>9,.0f} "
              f"{portfolio.iloc[-1]:>9,.0f}")

        print(f"\n🔍  分析")
        print(f"  组合 CAGR: {met['CAGR']:.1%} | N字: {n_met['CAGR']:.1%} | 海龟: {t_met['CAGR']:.1%}")
        print(f"  组合 MDD:  {met['MDD']:.1%} | N字: {n_met['MDD']:.1%} | 海龟: {t_met['MDD']:.1%}")
        print(f"  组合夏普:  {met['夏普']:.2f} | N字: {n_met['夏普']:.2f} | 海龟: {t_met['夏普']:.2f}")

        # 综合判断
        dd_improve = (met['MDD'] / min(n_met['MDD'], t_met['MDD']) - 1)
        sharp_improve = (met['夏普'] / max(n_met['夏普'], t_met['夏普']) - 1)
        print()
        if met['夏普'] > max(n_met['夏普'], t_met['夏普']):
            print(f"  ✅ 组合夏普优于两者 → 分散化有效")
        elif met['夏普'] > min(n_met['夏普'], t_met['夏普']):
            print(f"  ⚠️ 组合夏普介于两者之间")
        else:
            print(f"  ❌ 组合夏普劣于两者")
        if met['MDD'] < min(n_met['MDD'], t_met['MDD']):
            print(f"  ✅ 组合回撤低于两者 → 风险分散成功")
        print()


if __name__ == "__main__":
    main()
