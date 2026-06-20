#!/usr/bin/env python
"""
4 品种独立单兵回测 — 看每个品种在纯海龟信号下的 standalone 表现
用法: py scripts/_ad_hoc/per_symbol_backtest.py
"""
from __future__ import annotations

import sys
import logging
import warnings
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.disable(logging.CRITICAL)
warnings.filterwarnings("ignore")

import backtrader as bt
from strategies.turtle_trading import TurtleStrategy

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "turtle_config.yaml"
DATA_DIR = ROOT / "data" / "etf_daily"

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

codes = [s["code"] for s in config["symbols"]]
rows = []

for code in codes:
    df = pd.read_parquet(DATA_DIR / f"{code}.parquet")
    df = df[df["date"].between("2020-01-01", "2026-06-10")].sort_values("date").reset_index(drop=True)
    if df.empty:
        continue

    cerebro = bt.Cerebro()
    cerebro.adddata(
        bt.feeds.PandasData(
            dataname=df[["date", "open", "high", "low", "close", "volume"]].set_index("date"),
            open="open", high="high", low="low", close="close", volume="volume",
        ),
        name=code,
    )
    cerebro.broker.setcash(config["initial_cash"])
    cerebro.broker.setcommission(commission=config["commission_pct"] + config["slippage_pct"])

    shortable = {code} if code in ("513100.SH", "518880.SH") else set()
    t_plus_one = {code} if code in ("510500.SH", "159915.SZ") else set()

    cerebro.addstrategy(
        TurtleStrategy,
        turtle_params=config["turtle"],
        symbols=[code],
        use_55_filter=False,
        risk_per_unit=config["turtle"]["risk_per_unit"],
        concentration_trigger=999,
        max_consecutive_losses=config["risk"]["max_consecutive_losses"],
        max_cumulative_loss_pct=config["risk"]["max_cumulative_loss_pct"],
        pause_days=config["risk"]["pause_days"],
        single_max_risk=0.10,
        max_portfolio_risk=1.0,
        alpha=0.0,
        cov_lookback_days=252,
        rebalance_quarterly=False,
        atr_change_threshold=0.30,
        shortable_symbols=shortable,
        t_plus_one_symbols=t_plus_one,
        futures_mode=False,
        multipliers={},
        min_unit=100,
        min_confirmations=0,
        use_signal_filter=True,
        p2_mode="none",
        p2_batting_window=4,
        degradation_config={},
    )

    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", timeframe=bt.TimeFrame.Years)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="dd")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")

    results = cerebro.run()
    if not results:
        continue
    s = results[0]

    fv = cerebro.broker.getvalue()
    ic = config["initial_cash"]
    ret = (fv / ic - 1) * 100
    sr = s.analyzers.sharpe.get_analysis().get("sharperatio", None)
    dda = s.analyzers.dd.get_analysis()
    mdd = dda.get("max", {}).get("drawdown", None) if dda else None
    td = s.analyzers.trades.get_analysis() or {}
    total = td.get("total", {}).get("total", 0)
    won = td.get("won", {}).get("total", 0)
    lost = td.get("lost", {}).get("total", 0)
    wr = won / total * 100 if total > 0 else 0
    avg_w = td.get("won", {}).get("pnl", {}).get("average", 0)
    avg_l = abs(td.get("lost", {}).get("pnl", {}).get("average", 0))
    pf = avg_w / avg_l if avg_l > 0 else float("inf")
    hold = td.get("len", {}).get("average", 0)
    name = [s["name"] for s in config["symbols"] if s["code"] == code][0]
    rows.append((code, name, ret, sr, mdd, total, wr, pf, won, lost, hold, fv - ic))

# ── 输出对比表 ──
print("=" * 85)
print(f"  {'品种':<14} {'收益%':>7} {'夏普':>7} {'MDD%':>6} {'交易':>4} "
      f"{'胜率%':>5} {'盈亏比':>6} {'均值持仓'} {'净盈亏'}")
print("  " + "-" * 85)
for code, name, ret, sr, mdd, total, wr, pf, won, lost, hold, pnl in rows:
    sr_s = f"{sr:.3f}" if sr else "N/A"
    mdd_s = f"{mdd:.1f}" if mdd else "N/A"
    pf_s = f"{pf:.2f}" if pf != float("inf") else "inf"
    print(f"  {code:<6} {name:<6} {ret:>7.2f} {sr_s:>7} {mdd_s:>6} {total:>4} "
          f"{wr:>5.1f} {pf_s:>6} {hold:>5.0f}d {pnl:>+10,.0f}")
print("  " + "-" * 85)
print(f"  {'4-ETF组合':<14} {'+221.06%':>7} {'0.41':>7} {'38.3%':>6} {'60':>4} "
      f"{'36.7%':>5} {'1.97':>6} {'31d'} {'+442,117'}")
print("=" * 85)
