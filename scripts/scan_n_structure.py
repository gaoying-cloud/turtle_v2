#!/usr/bin/env python
"""
N 字结构参数扫描（集成版）

直接调用 NStructureStrategy 做回测，避免子进程通信问题。
训练区间 2020-01 ~ 2026-06，样本外 2014-01 ~ 2019-12。

用法：
    py scripts/scan_n_structure.py                         # 全参数扫描
    py scripts/scan_n_structure.py --param trail_mult      # 只看某个参数
    py scripts/scan_n_structure.py --param stop_mult --values "2.0,3.0,4.0"
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

from strategies.n_structure import NStructureStrategy
from run_n_structure import compute_metrics

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ── 数据配置 ──
DATA_DIR = REPO_DIR / "data" / "etf_daily"
SYMBOLS = ["510500.SH", "159915.SZ", "513100.SH", "518880.SH", "159985.SZ", "513520.SH"]

IS_START = "2020-01-01"
IS_END = "2026-06-30"
OOS_START = "2014-01-01"
OOS_END = "2019-12-31"


def load_data(symbol: str, start: str, end: str) -> pd.DataFrame:
    path = DATA_DIR / f"{symbol}.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    mask = (df["date"] >= start) & (df["date"] <= end)
    df = df[mask].copy()
    if df.empty:
        return df
    df.sort_values("date", inplace=True)
    df.reset_index(drop=True, inplace=True)
    df["date"] = pd.to_datetime(df["date"])
    return df


def run_backtest(params: dict, start: str, end: str) -> dict:
    """用给定参数跑回测，返回汇总指标。"""
    strategy = NStructureStrategy(**params)
    all_trades: dict[str, list] = {}
    all_equity: dict[str, pd.Series] = {}
    date_ranges: dict[str, float] = {}

    for symbol in SYMBOLS:
        df = load_data(symbol, start, end)
        if df.empty:
            continue
        days = (df['date'].iloc[-1] - df['date'].iloc[0]).days
        total_years = max(1.0, days / 365.25)
        date_ranges[symbol] = total_years
        _, trades, equity = strategy.run(df, symbol=symbol, verbose=False)
        all_trades[symbol] = trades
        all_equity[symbol] = equity

    # 汇总
    cagrs, sharpes, win_rates, mdds, pnls, trade_counts = [], [], [], [], [], []
    all_profitable = True
    for symbol, trades in all_trades.items():
        years = date_ranges.get(symbol, 6.0)
        eq = all_equity.get(symbol)
        m = compute_metrics(trades, total_years=years, daily_equity=eq)
        cagrs.append(m["CAGR"])
        sharpes.append(m["夏普"])
        win_rates.append(m["胜率"])
        mdds.append(m["最大回撤"])
        pnls.append(m["总盈亏"])
        trade_counts.append(m["总交易"])
        if m["总盈亏"] <= 0:
            all_profitable = False

    avg_cagr = np.mean(cagrs) if cagrs else 0.0
    avg_sharpe = np.mean(sharpes) if sharpes else 0.0
    avg_win_rate = np.mean(win_rates) if win_rates else 0.0
    avg_mdd = np.mean(mdds) if mdds else 0.0
    total_pnl = sum(pnls)
    total_trades = sum(trade_counts)

    return {
        "cagr": avg_cagr,
        "sharpe": avg_sharpe,
        "win_rate": avg_win_rate,
        "mdd": avg_mdd,
        "total_pnl": total_pnl,
        "trades": total_trades,
        "all_profitable": all_profitable,
    }


# ── 基线参数（S22 调优版本） ──
BASELINE = dict(
    window_size=100,
    stop_mult=1.5,
    trail_mult=5.0,
    add_step=2.0,
    max_units=6,
    use_ma5_confirm=False,
    num_symbols=6,
)

# ── 各参数扫描范围 ──
SCAN_RANGES: dict[str, list] = {
    "trail_mult":       [3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0],
    "add_step":         [1.0, 1.5, 2.0, 2.5, 3.0],
    "stop_mult":        [1.5, 2.0, 2.5, 3.0, 3.5],
    "window_size":      [60, 80, 100, 120, 150],
    "max_units":        [3, 4, 5, 6],
    "use_ma5_confirm":  [True, False],
}


def make_params(param_name: str, param_value) -> dict:
    """构建参数字典：基线 + 当前扫描值。"""
    p = dict(BASELINE)
    p[param_name] = param_value
    return p


def print_table(results: list[dict], title: str):
    """打印结果表格，按 CAGR 降序排列。"""
    valid = [r for r in results if r["cagr"] > 0]

    print(f"\n{'=' * 85}")
    print(f"  {title}")
    print(f"{'=' * 85}")
    header = (f"{'参数':>14} {'CAGR':>7} {'Sharpe':>7} "
              f"{'胜率':>6} {'交易':>5} {'MDD':>6} {'总盈亏':>10} {'盈利':>6}")
    print(header)
    print("-" * 85)

    for r in sorted(valid, key=lambda x: x["cagr"], reverse=True):
        pv = r["_value"]
        if isinstance(pv, bool):
            pv_str = "ON" if pv else "OFF"
        elif isinstance(pv, float):
            pv_str = f"{pv:.1f}"
        else:
            pv_str = str(pv)
        label = f"{r['_param']}={pv_str}"

        print(f"{label:>14} {r['cagr']*100:>7.1f}% {r['sharpe']:>7.2f} "
              f"{r['win_rate']*100:>6.1f}% {r['trades']:>5} "
              f"{r['mdd']*100:>6.1f}% {r['total_pnl']:>+10.0f} "
	              f"{'✅' if r['all_profitable'] else '❌':>6}")

    print("-" * 85)
    if valid:
        best = max(valid, key=lambda x: x["cagr"])
        print(f"  🏆 最优: {best['_param']}={best['_value']}  "
              f"CAGR={best['cagr']*100:.1f}%  Sharpe={best['sharpe']:.2f}  "
              f"盈利={'✅' if best['all_profitable'] else '❌'}")
    print()

    return valid


def scan_param(param_name: str):
    """扫描单个参数的所有值。"""
    values = SCAN_RANGES[param_name]
    print(f"\n{'─' * 60}")
    print(f"  🔍 {param_name}  (训练: {IS_START}~{IS_END})")
    print(f"{'─' * 60}")

    is_results = []
    for v in values:
        params = make_params(param_name, v)
        label = f"{param_name}={v}"
        print(f"    ▶ {label:<20} ... ", end="", flush=True)
        tick = time.time()
        r = run_backtest(params, IS_START, IS_END)
        elapsed = time.time() - tick
        print(f"CAGR={r['cagr']*100:.1f}%  Sharpe={r['sharpe']:.2f}  ({elapsed:.0f}s)")
        r["_param"] = param_name
        r["_value"] = v
        is_results.append(r)

    valid = print_table(is_results, f"📊 训练集 — {param_name}")

    # 对最优 TOP3 跑 OOS
    top_n = min(3, len(valid))
    if top_n > 0:
        print(f"\n  🧪 OOS 验证 (最优 {top_n} 个):")
        oos_results = []
        for r in valid[:top_n]:
            v = r["_value"]
            params = make_params(param_name, v)
            label = f"{param_name}={v}"
            print(f"    ▶ OOS {label:<16} ... ", end="", flush=True)
            tick = time.time()
            oos_r = run_backtest(params, OOS_START, OOS_END)
            elapsed = time.time() - tick
            print(f"CAGR={oos_r['cagr']*100:.1f}%  Sharpe={oos_r['sharpe']:.2f}  ({elapsed:.0f}s)")
            oos_r["_param"] = param_name
            oos_r["_value"] = v
            oos_results.append(oos_r)

        oos_valid = print_table(oos_results, f"📊 样本外 (OOS) — {param_name}")

        # IS vs OOS 对比
        print(f"\n  📊 IS vs OOS 对比:")
        print(f"  {'参数':>14} {'IS-CAGR':>8} {'OOS-CAGR':>9} {'IS-Sharpe':>10} {'OOS-Sharpe':>11} {'衰减':>6}")
        print(f"  {'-'*60}")
        for r in valid[:top_n]:
            pv = r["_value"]
            oos_r = next((o for o in oos_results if o["_value"] == pv), None)
            if oos_r and oos_r["cagr"] > 0:
                decay = (oos_r["cagr"] / r["cagr"] - 1) * 100 if r["cagr"] > 0 else 0
                label = f"{param_name}={pv}"
                if isinstance(pv, bool):
                    label = f"{param_name}={('ON' if pv else 'OFF')}"
                elif isinstance(pv, float):
                    label = f"{param_name}={pv:.1f}"
                print(f"  {label:>14} {r['cagr']*100:>8.1f}% {oos_r['cagr']*100:>9.1f}% "
                      f"{r['sharpe']:>10.2f} {oos_r['sharpe']:>11.2f} "
                      f"{decay:>+5.0f}%")


def main():
    parser = argparse.ArgumentParser(description="N 字结构参数扫描")
    parser.add_argument("--param", choices=list(SCAN_RANGES.keys()),
                        help="指定参数 (默认: 全部)")
    parser.add_argument("--values", type=str,
                        help="自定义参数值列表，逗号分隔 (需配合 --param 使用)")
    args = parser.parse_args()

    print(f"{'=' * 85}")
    print(f"  N 字结构参数扫描")
    print(f"  品种: {', '.join(SYMBOLS)}")
    print(f"  训练集 (IS):  {IS_START} ~ {IS_END}")
    print(f"  样本外 (OOS): {OOS_START} ~ {OOS_END}")
    print(f"  基线: {BASELINE}")
    print(f"{'=' * 85}")

    if args.param:
        if args.values:
            vals = [float(x.strip()) for x in args.values.split(",")]
            SCAN_RANGES[args.param] = vals
        scan_param(args.param)
    else:
        for param in SCAN_RANGES:
            scan_param(param)
            print()


if __name__ == "__main__":
    main()
