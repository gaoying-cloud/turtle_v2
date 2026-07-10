#!/usr/bin/env python
"""N字结构参数扫描：跟踪止损倍数敏感性。"""
import subprocess, sys, re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "run_n_structure.py"

trail_values = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0]

print(f"{'trail_mult':>10} {'CAGR':>8} {'总盈亏':>10} {'胜率':>6} {'交易':>5}")
print("-" * 45)

for tv in trail_values:
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--trail_mult", str(tv)],
        capture_output=True, text=True, cwd=REPO, timeout=300
    )
    out = r.stdout
    # 解析结果
    cagr_m = re.search(r"平均 CAGR: ([\d.]+)%", out)
    pnl_m = re.search(r"全部盈利:", out)
    trades_m = re.search(r"合计: (\d+) 笔交易", out)
    # 从每品种行读胜率
    win_rates = re.findall(r"\d+\.\d+%", out)
    # 最后一行汇总的胜率
    wr_m = re.search(r"胜率 (\d+\.\d+)%", out)

    cagr = cagr_m.group(1) if cagr_m else "?"
    trades = trades_m.group(1) if trades_m else "?"
    wr = wr_m.group(1) if wr_m else "?"

    # 总盈亏：各品种之和
    pnls = re.findall(r"[+-]\d{4,}", out)
    total_pnl = "?"
    if pnls:
        nums = [int(x) for x in pnls if abs(int(x)) < 1e7]
        total_pnl = str(sum(nums)) if nums else "?"

    print(f"{tv:>10.1f} {cagr:>8} {total_pnl:>10} {wr:>6} {trades:>5}")
