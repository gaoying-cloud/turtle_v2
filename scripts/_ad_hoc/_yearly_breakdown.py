#!/usr/bin/env python
"""
逐年回测：输出完整绩效明细表
用法: py scripts/_ad_hoc/_yearly_breakdown.py
"""
from __future__ import annotations
import sys, logging, yaml, pandas as pd, numpy as np
from pathlib import Path
logging.disable(logging.CRITICAL)
import warnings; warnings.filterwarnings('ignore')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import backtrader as bt
from strategies.turtle_trading import TurtleStrategy

ROOT = Path(__file__).resolve().parent.parent
with open(ROOT/"config/turtle_config.yaml","r",encoding="utf-8") as f:
    config = yaml.safe_load(f)

codes = [s["code"] for s in config["symbols"]]
ic = config["initial_cash"]

def run_yearly(year):
    y_start = f"{year}-01-01"
    y_end = f"{year}-12-31" if year < 2026 else "2026-06-10"
    cerebro = bt.Cerebro()
    for code in codes:
        df = pd.read_parquet(ROOT/f"data/etf_daily/{code}.parquet")
        df = df[df["date"].between(y_start,y_end)].sort_values("date").reset_index(drop=True)
        if df.empty: return None
        cerebro.adddata(bt.feeds.PandasData(
            dataname=df[["date","open","high","low","close","volume"]].set_index("date"),
            open="open",high="high",low="low",close="close",volume="volume"), name=code)
    cerebro.broker.setcash(ic)
    cerebro.broker.setcommission(commission=config["commission_pct"]+config["slippage_pct"])
    cerebro.addstrategy(TurtleStrategy,
        turtle_params=config["turtle"], symbols=codes, use_55_filter=False,
        risk_per_unit=config["turtle"]["risk_per_unit"],
        concentration_trigger=3, max_consecutive_losses=8, max_cumulative_loss_pct=0.15,
        pause_days=5, single_max_risk=0.04, max_portfolio_risk=0.20,
        alpha=0.05, cov_lookback_days=252, rebalance_quarterly=True, atr_change_threshold=0.30,
        shortable_symbols={"513100.SH","518880.SH"}, t_plus_one_symbols={"510500.SH","159915.SZ"},
        futures_mode=False, multipliers={}, min_unit=100,
        min_confirmations=0, use_signal_filter=True, p2_mode="none", p2_batting_window=4,
        degradation_config={})
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", timeframe=bt.TimeFrame.Years)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="dd")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    results = cerebro.run()
    if not results: return None
    s = results[0]
    fv = cerebro.broker.getvalue()
    ret = (fv/ic-1)*100
    sr = s.analyzers.sharpe.get_analysis().get("sharperatio",None)
    dda = s.analyzers.dd.get_analysis()
    mdd = dda.get("max",{}).get("drawdown",None) if dda else None
    mdd_dur = dda.get("max",{}).get("len",None) if dda else None
    td = s.analyzers.trades.get_analysis() or {}
    total = td.get("total",{}).get("total",0)
    won = td.get("won",{}).get("total",0)
    lost = total-won; wr = won/total*100 if total>0 else 0
    avg_w = td.get("won",{}).get("pnl",{}).get("average",0)
    avg_l = abs(td.get("lost",{}).get("pnl",{}).get("average",0))
    pf = avg_w/avg_l if avg_l>0 else float("inf")
    ts = s._trade_summary if hasattr(s,"_trade_summary") else {}
    trades_df = ts.get("trades") if isinstance(ts,dict) else None
    max_hold, util = 0, 0
    if trades_df is not None and len(trades_df)>0:
        df_t = trades_df.copy()
        df_t["hold"] = (pd.to_datetime(df_t["exit_date"])-pd.to_datetime(df_t["entry_date"])).dt.days
        max_hold = int(df_t["hold"].max())
        total_slot = len(pd.bdate_range(y_start,y_end)) * len(codes)
        pos_days = df_t["hold"].clip(lower=1).sum()
        util = pos_days/total_slot*100 if total_slot>0 else 0
    return {"year":year,"ret":ret,"sr":sr,"mdd":mdd,"mdd_dur":mdd_dur,
            "pf":pf,"wr":wr,"total":total,"won":won,"lost":lost,
            "max_hold":max_hold,"util":util}

rows = [run_yearly(y) for y in range(2020,2027)]
rows = [r for r in rows if r]

print("="*105)
print(f"  4-ETF 组合逐年绩效明细 (initial_cash={ic:,})")
print("="*105)
print(f"  {'年份':>4} {'收益%':>7} {'夏普':>6} {'MDD%':>6} {'DD天':>4} {'盈亏比':>6} {'胜率%':>5} {'交易':>3} {'最大持仓':>5} {'资金利用':>6}")
print("  "+"-"*80)
for r in rows:
    sr_s = f'{r["sr"]:.2f}' if r["sr"] else "N/A"
    mdd_s = f'{r["mdd"]:.1f}' if r["mdd"] else "N/A"
    mdd_d = f'{r["mdd_dur"]}' if r["mdd_dur"] else "-"
    pf_s = f'{r["pf"]:.2f}' if r["pf"]!=float("inf") else "inf"
    print(f'  {r["year"]:>4} {r["ret"]:>+7.2f} {sr_s:>6} {mdd_s:>6} {mdd_d:>4} {pf_s:>6} {r["wr"]:>5.1f} {r["total"]:>3} {r["max_hold"]:>4}d {r["util"]:>5.1f}%')
print("  "+"-"*80)
avg_ret = (np.prod([1+r["ret"]/100 for r in rows])-1)*100
avg_wr = np.mean([r["wr"] for r in rows])
total_tr = sum(r["total"] for r in rows)
avg_pf = np.mean([r["pf"] for r in rows if r["pf"]!=float("inf")])
max_dd_all = max((r["mdd"] for r in rows if r["mdd"]), default=0)
max_hold_all = max(r["max_hold"] for r in rows)
avg_util = np.mean([r["util"] for r in rows])
print(f'  {"合计":>4} {avg_ret:>+7.2f} {"N/A":>6} {max_dd_all:>6.1f} {"-":>4} {avg_pf:>6.2f} {avg_wr:>5.1f} {total_tr:>3} {max_hold_all:>4}d {avg_util:>5.1f}%')
print("="*105)
