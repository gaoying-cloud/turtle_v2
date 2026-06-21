#!/usr/bin/env python
"""
SMA20<SMA60 趋势方向过滤对比实验。

在开启做空的前提下，对比：
  - 已知基线（无方向过滤）
  - 当前代码（SMA20<SMA60 趋势方向过滤）

用法：
    py scripts/_ad_hoc/_compare_trend_filter.py
"""
from __future__ import annotations
import sys, logging, warnings, csv
from pathlib import Path
from datetime import datetime

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import yaml
import backtrader as bt
import pandas as pd

from src.config_loader import get_trading_symbols, get_t_plus_one_symbols, get_bond_symbol
from strategies.turtle_trading import TurtleStrategy

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data" / "etf_daily"


def load_data(symbol, start, end):
    path = DATA_DIR / f"{symbol}.parquet"
    df = pd.read_parquet(path)
    df = df[(df["date"] >= start) & (df["date"] <= end)].copy()
    df.sort_values("date", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def run_one(label, short_set) -> dict:
    with open(ROOT / "config" / "turtle_config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    start, end = "2020-01-01", "2026-06-10"
    symbols = get_trading_symbols(config)
    bond = get_bond_symbol(config)

    feeds = {}
    for s in symbols:
        df = load_data(s, start, end)
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

    tp = {"atr_period": 20, "breakout_period": 20, "stop_period": 10,
          "stop_atr_multiple": 2.0, "risk_per_unit": 0.01, "max_units": 4,
          "unit_step": 0.5, "use_55_filter": False, "exit_period": 10}
    cerebro.addstrategy(
        TurtleStrategy,
        turtle_params=tp, symbols=symbols, use_55_filter=False,
        risk_per_unit=0.01, concentration_trigger=3,
        max_consecutive_losses=8, max_cumulative_loss_pct=0.15,
        pause_days=5, max_portfolio_risk=0.20, single_max_risk=0.04,
        alpha=0.05, cov_lookback_days=252,
        rebalance_quarterly=True, atr_change_threshold=0.30,
        shortable_symbols=short_set,
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
    sym_parts = []
    if tdf is not None and not tdf.empty:
        for sym in symbols:
            for d in ["long", "short"]:
                dd = tdf[(tdf["symbol"] == sym) & (tdf["direction"] == d)]
                if len(dd):
                    sym_parts.append(f"{sym[:6]}{d[0]}={dd['pnl'].sum():+.0f}")

    print(f"{label:>35s}: FV={fv:>8.0f} CAGR={cagr:>5.1f}% MDD={mdd:>5.1f}% Sharpe={sp_s:>7s} Calmar={calmar:.3f} WR={wr:.0f}% PF={pf:.2f} Trades={total}")
    print(f"{'':>35s}  {' '.join(sym_parts)}")

    return {"Config": label, "FV": round(fv, 2), "CAGR%": round(cagr, 2),
            "MDD%": round(mdd, 2), "Sharpe": round(sp, 4) if sp else "N/A",
            "Calmar": round(calmar, 4), "WR%": round(wr, 1), "PF": round(pf, 3), "Trades": total}


def main():
    print("=" * 85)
    print("  SMA20<SMA60 Trend Direction Filter Comparison")
    print("=" * 85)
    print()

    results = []

    # A: Current code (SMA20<SMA60 filter) + shorting enabled
    r = run_one("SMA20<SMA60 filter + shorting", {"513100.SH", "518880.SH"})
    results.append(r)

    # B: Current code, no shorting (verification)
    r2 = run_one("SMA20<SMA60 filter, no short", set())
    results.append(r2)

    print()
    print("-" * 85)
    print("  Known baselines (from historical runs):")
    print("    baseline(long+short,noFilter): CAGR=19.24% MDD=38.15% Calmar=0.504 FV=372631 Trades=60")
    print("    baseline(no short):             CAGR=9.51%  MDD=12.32% Calmar=0.772 FV=215399 Trades=44")
    print()

    # Save CSV
    out_path = ROOT / "results" / "trend_filter_comparison.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "Config", "FV", "CAGR%", "MDD%", "Sharpe", "Calmar", "WR%", "PF", "Trades",
        ])
        w.writeheader()
        w.writerows(results)
    print(f"Results saved to {out_path}")


if __name__ == "__main__":
    main()
