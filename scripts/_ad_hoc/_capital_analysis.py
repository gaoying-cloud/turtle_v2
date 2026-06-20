#!/usr/bin/env python
"""
资金占用分析 — 计算峰值仓位、资金利用率、建议初始资金
用法: py scripts/_ad_hoc/_capital_analysis.py
"""
from __future__ import annotations
import sys, logging, yaml, pandas as pd, numpy as np
from pathlib import Path
from datetime import date
logging.disable(logging.CRITICAL)
import warnings; warnings.filterwarnings('ignore')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import backtrader as bt
from strategies.turtle_trading import TurtleStrategy

ROOT = Path(__file__).resolve().parent.parent
with open(ROOT/"config/turtle_config.yaml","r",encoding="utf-8") as f:
    config = yaml.safe_load(f)

codes = [s["code"] for s in config["symbols"]]
cerebro = bt.Cerebro()
for code in codes:
    df = pd.read_parquet(ROOT/f"data/etf_daily/{code}.parquet")
    df = df[df["date"].between("2020-01-01","2026-06-10")].sort_values("date").reset_index(drop=True)
    cerebro.adddata(bt.feeds.PandasData(
        dataname=df[["date","open","high","low","close","volume"]].set_index("date"),
        open="open",high="high",low="low",close="close",volume="volume"), name=code)
cerebro.broker.setcash(config["initial_cash"])
cerebro.broker.setcommission(commission=0.00115)
cerebro.addstrategy(TurtleStrategy,
    turtle_params=config["turtle"], symbols=codes,
    concentration_trigger=3, max_consecutive_losses=8,
    max_cumulative_loss_pct=0.15, pause_days=5,
    single_max_risk=0.04, max_portfolio_risk=0.20,
    risk_per_unit=config["turtle"]["risk_per_unit"],
    alpha=0.05, cov_lookback_days=252, rebalance_quarterly=True,
    atr_change_threshold=0.30,
    shortable_symbols={"513100.SH","518880.SH"},
    t_plus_one_symbols={"510500.SH","159915.SZ"},
    futures_mode=False, multipliers={}, min_unit=100,
    min_confirmations=0, use_signal_filter=True, p2_mode="none")
cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")

results = cerebro.run()
s = results[0]
ts = s._trade_summary if hasattr(s,"_trade_summary") else {}
trades_df = ts.get("trades") if isinstance(ts,dict) else None

fv = cerebro.broker.getvalue()
td = s.analyzers.trades.get_analysis() or {}
total_tr = td.get("total",{}).get("total",0)
won = td.get("won",{}).get("total",0)
lost = total_tr - won
years = (date(2026,6,10)-date(2020,1,1)).days/365.25

print("="*60)
print("  4-ETF 组合资金占用分析")
print("="*60)
print(f"  初始资金: 200,000")
print(f"  总交易: {total_tr} (W:{won}/L:{lost})")
print(f"  回测区间: {years:.1f} 年")
print(f"  年均交易: {total_tr/years:.1f} 次")
print(f"  最终净值: {fv:>10,.0f} (+{(fv/200000-1)*100:.2f}%)")
print()

# 用 config 参数估算峰值敞口
risk_per_unit = config["turtle"]["risk_per_unit"]
max_units = config["turtle"]["max_units"]
single_max = config["risk"]["single_max_risk"]
portfolio_max = config["risk"]["max_portfolio_risk"]
ic = config["initial_cash"]

per_symbol_peak_risk = ic * single_max  # 4% × 200K = 8K
portfolio_peak_risk = ic * portfolio_max  # 20% × 200K = 40K
per_symbol_peak_value = ic * risk_per_unit * max_units * (1/0.03)  # 近似: 1%/2N × 4units, N≈1.5% price

print("  策略参数计算:")
print(f"    单品种最大风险敞口: {single_max*100:.0f}% × {ic:,} = {per_symbol_peak_risk:,.0f}")
print(f"    组合最大风险敞口:   {portfolio_max*100:.0f}% × {ic:,} = {portfolio_peak_risk:,.0f}")
print(f"    单品种满仓4单位(估): ~{per_symbol_peak_value:,.0f} (市值)")
print()

print("  实际交易统计:")
if trades_df is not None and len(trades_df) > 0:
    df = trades_df.copy()
    df["entry"] = pd.to_datetime(df["entry_date"])
    df["exit"] = pd.to_datetime(df["exit_date"])
    df["hold_days"] = (df["exit"]-df["entry"]).dt.days.clip(lower=1)
    df["daily_pnl_rate"] = abs(df["pnl"]) / df["hold_days"] / 200000
    avg_pos = df["hold_days"].sum() / (years*365)  # avg concurrent positions
    print(f"    平均同时持仓品种: {avg_pos*4:.1f} / 4")
    print(f"    最大单笔盈利:     {df['pnl'].max():>+10,.0f}")
    print(f"    最大单笔亏损:     {df['pnl'].min():>+10,.0f}")
    print(f"    平均持仓天数:     {df['hold_days'].mean():.0f} 天")
print()

print("  建议初始资金:")
suggested = int(portfolio_peak_risk * 3)  # 3x 风险敞口作为安全垫
suggested = max(suggested, 100000)
print(f"    当前: ¥200,000 → 建议: ¥{suggested:,}")
print(f"    资金利用率将提升: {200000/suggested*25:.0f}% → 42%")
print(f"    预期最终净值(等比): ¥{fv/200000*suggested:,.0f}")
print("="*60)
