#!/usr/bin/env python
"""
批量对比：多时段 × 多信号确认组合。
"""
from __future__ import annotations
import sys, logging
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.WARNING)

from scripts.run_backtest import run_backtest

# (名称, mc, vt, kb, p2_mode, p2_lr, p2_bw, use_sf)
COMBOS = [
    ("1.基线(全关)",      0,   0,   0, "none",           1.0,  4, False),
    ("2.仅成交量",        1, 1.5,   0, "none",           1.0,  4, False),
    ("3.仅K线",           1,   0, 0.4, "none",           1.0,  4, False),
    ("4.仅胜率",          1,   0,   0, "batting_avg",   0.75, 4, False),
    ("5.成交量+K线+胜率", 1, 1.5, 0.4, "batting_avg",   0.75, 4, False),
    ("6.成交量+胜率",     1, 1.5,   0, "batting_avg",   0.75, 4, False),
    ("7.K线+胜率",        1,   0, 0.4, "batting_avg",   0.75, 4, False),
]

PERIODS = [
    ("2020-2021", "2020-01-01", "2021-12-31"),
    ("2022-2023", "2022-01-01", "2023-12-31"),
    ("2024-2026", "2024-01-01", "2026-06-10"),
]

def run_one(name, mc, vt, kb, pm, plr, pbw, sf, start, end):
    r = run_backtest(start_date=start, end_date=end, mode="A", quiet=True,
                     min_confirmations=mc, vol_threshold=vt,
                     kline_min_body=kb, p2_mode=pm,
                     p2_loss_ratio=plr, p2_batting_window=pbw,
                     use_signal_filter=sf)
    if r is None:
        return None
    return r

print()
print("=" * (34 + len(PERIODS) * 44))
print("信号确认组合对比 · 三时段")
print("=" * (34 + len(PERIODS) * 44))

# 时段标签
hdr = f"| {'组合':>22s}"
for label, _, _ in PERIODS:
    hdr += f" | {label:>36s}"
print(hdr)

hdr2 = f"| {'':>22s}"
for _ in PERIODS:
    hdr2 += " | {:>6s} {:>7s} {:>7s} {:>5s} {:>5s}".format("Sharpe","CAGR%","MDD%","交易","SF拒")
print(hdr2)

tot_w = 24 + sum([7+8+8+6+6+3 for _ in PERIODS])
sep = "|" + ":".join(["-" * 22] + [":------:-------:-------:-----:-----:" for _ in PERIODS]) + "|"
print(sep)

for name, mc, vt, kb, pm, plr, pbw, sf in COMBOS:
    print(f"  [{name}] ", end="")
    row = f"| {name:>22s}"
    for label, start, end in PERIODS:
        r = run_one(name, mc, vt, kb, pm, plr, pbw, sf, start, end)
        if r is None:
            row += " |  N/A     N/A     N/A   N/A   N/A"
        else:
            s = f"{r['sharpe']:.2f}" if r.get('sharpe') is not None else "N/A"
            c = f"{r['total_return_pct']:.2f}"
            m = f"{r['max_drawdown']:.2f}" if r.get('max_drawdown') is not None else "N/A"
            t = str(r['total_trades'])
            # SF reject count from the return dict
            sf_in = "—"
            row += f" | {s:>6s} {c:>7s} {m:>7s} {t:>5s} {sf_in:>5s}"
    print()
    print(row)

print(sep)
print("=" * (34 + len(PERIODS) * 44))
print()
