#!/usr/bin/env python
"""
Short-side 2N Hard Stop Comparison。

使用当前代码（含 2N 硬止损）跑一次完整回测，
与已提交的基线（19.24% CAGR, 38.15% MDD）对比。

用法：
    py scripts/_ad_hoc/_compare_hard_stop.py
"""
from __future__ import annotations
import sys, logging, warnings, yaml, csv
from pathlib import Path
from datetime import datetime

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)  # 完全抑制日志
logging.getLogger().setLevel(logging.CRITICAL)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import backtrader as bt
import pandas as pd
import numpy as np

from src.config_loader import get_trading_symbols, get_t_plus_one_symbols, get_bond_symbol
from strategies.turtle_trading import TurtleStrategy

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data" / "etf_daily"


def load_data(symbol: str, start: str, end: str):
    path = DATA_DIR / f"{symbol}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    df = df[(df["date"] >= start) & (df["date"] <= end)].copy()
    df.sort_values("date", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def run_one(label: str, shortable_set: set) -> dict:
    with open(ROOT / "config" / "turtle_config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    start, end = "2020-01-01", "2026-06-10"
    symbols = get_trading_symbols(config)
    bond = get_bond_symbol(config)

    feeds = {}
    for s in symbols:
        df = load_data(s, start, end)
        if df is None:
            continue
        fd = df[["date", "open", "high", "low", "close", "volume"]].copy()
        fd["date"] = pd.to_datetime(fd["date"])
        fd.set_index("date", inplace=True)
        feeds[s] = bt.feeds.PandasData(dataname=fd, plot=False)

    cerebro = bt.Cerebro()
    for s in symbols:
        if s in feeds:
            cerebro.adddata(feeds[s], name=s)
    if bond in feeds:
        cerebro.adddata(feeds[bond], name=bond)

    ic = 120000
    cerebro.broker.setcash(ic)
    cerebro.broker.setcommission(commission=0.00115)

    tp = {
        "atr_period": 20, "breakout_period": 20, "stop_period": 10,
        "stop_atr_multiple": 2.0, "risk_per_unit": 0.01, "max_units": 4,
        "unit_step": 0.5, "use_55_filter": False, "exit_period": 10,
    }
    cerebro.addstrategy(
        TurtleStrategy,
        turtle_params=tp, symbols=symbols, use_55_filter=False,
        risk_per_unit=0.01, concentration_trigger=3,
        max_consecutive_losses=8, max_cumulative_loss_pct=0.15,
        pause_days=5, max_portfolio_risk=0.20, single_max_risk=0.04,
        alpha=0.05, cov_lookback_days=252,
        rebalance_quarterly=True, atr_change_threshold=0.30,
        shortable_symbols=shortable_set,
        t_plus_one_symbols=get_t_plus_one_symbols(config),
        degradation_config=config["risk"].get("degradation", {}),
    )
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", timeframe=bt.TimeFrame.Years)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="dd")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")

    r = cerebro.run()
    strat = r[0]
    fv = cerebro.broker.getvalue()
    ny = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days / 365.25
    cagr = ((fv / ic) ** (1 / max(ny, 0.1)) - 1) * 100
    sp = strat.analyzers.sharpe.get_analysis().get("sharperatio", None)
    mdd = strat.analyzers.dd.get_analysis().get("max", {}).get("drawdown", 0)
    ta = strat.analyzers.trades.get_analysis() or {}
    total = ta.get("total", {}).get("total", 0)
    won = ta.get("won", {}).get("total", 0)
    wr = won / total * 100 if total > 0 else 0
    aw = abs(ta.get("won", {}).get("pnl", {}).get("average", 0)) if ta else 0
    al = abs(ta.get("lost", {}).get("pnl", {}).get("average", 0)) if ta else 0
    pf = aw / al if al > 0 else 0
    calmar = cagr / mdd if mdd > 0 else 0
    sp_s = f"{sp:.4f}" if sp else "N/A"

    # per-symbol detail
    ts = getattr(strat, "_trade_summary", {}) or {}
    tdf = ts.get("trades")
    sym_lines = []
    if tdf is not None and not tdf.empty:
        for sym in symbols:
            for d in ["long", "short"]:
                dd = tdf[(tdf["symbol"] == sym) & (tdf["direction"] == d)]
                if len(dd):
                    sym_lines.append(f"{sym[:6]}{d[0]}={dd['pnl'].sum():+.0f}")

    print(f"{label:>30s}: FV={fv:>8.0f} CAGR={cagr:>5.1f}% MDD={mdd:>5.1f}% Sharpe={sp_s:>7s} Calmar={calmar:.3f} WR={wr:.0f}% PF={pf:.2f} Trades={total}")
    print(f"{'':>30s}  {' '.join(sym_lines)}")

    return {
        "Config": label, "FV": round(fv, 2), "CAGR%": round(cagr, 2),
        "MDD%": round(mdd, 2), "Sharpe": round(sp, 4) if sp else "N/A",
        "Calmar": round(calmar, 4), "WR%": round(wr, 1), "PF": round(pf, 3),
        "Trades": total,
    }


def main():
    print("=" * 80)
    print("  Short-side 2N Hard Stop Comparison")
    print("=" * 80)
    print()
    print(f"{'Experiment':>30s} {'FV':>8s} {'CAGR':>6s} {'MDD':>6s} {'Sharpe':>7s} {'Calmar':>7s} {'WR':>4s} {'PF':>5s} {'Trades':>6s}")
    print("-" * 85)

    results = []

    # Group A: current code (2N stop) + shorting enabled
    r = run_one("short+2Nstop (current code)", {"513100.SH", "518880.SH"})
    results.append(r)

    # Group B: same code, no shorting (should match no-short baseline ~9.5%/12%)
    r2 = run_one("short+2Nstop (no shorting)", set())
    results.append(r2)

    print()
    print("-" * 85)
    print("  Baseline (committed b476b61, no 2N hard stop):")
    print(f"{'baseline(long+short,no2N)':>30s}: CAGR=19.24% MDD=38.15% Sharpe=0.4083 Calmar=0.5043 Trades=60")
    print(f"{'baseline(no short)':>30s}: CAGR=9.51% MDD=12.32% Sharpe=0.7154 Calmar=0.7722 Trades=44")
    print(f"{'':>30s}  510500多=+186205 159915多=+37654 513100多=+37397 513100空=-16146(4笔) 518880多=+452996 518880空=-30643(10笔)")
    print()

    # 保存 CSV
    baseline = {
        "Config": "原基线(无2N)", "FV": 372631.28, "CAGR%": 19.24,
        "MDD%": 38.15, "Sharpe": 0.4083, "Calmar": 0.5043,
        "WR%": 36.7, "PF": 1.957, "Trades": 60,
    }
    results.insert(0, baseline)

    out_path = ROOT / "results" / "short_hard_stop_comparison.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "Config", "FV", "CAGR%", "MDD%", "Sharpe", "Calmar", "WR%", "PF", "Trades",
        ])
        w.writeheader()
        w.writerows(results)
    print(f"Results saved to: {out_path}")


if __name__ == "__main__":
    main()
