#!/usr/bin/env python
"""N字结构参数扫描：利润保护 × 再进场 × 加仓策略。

用法：
    py scripts/scan_params.py             # 利润保护 + 再进场扫描
"""
from __future__ import annotations

import subprocess, sys, re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "run_n_structure.py"


def run_bt(**kwargs) -> dict:
    """运行一次回测，解析关键指标。"""
    cmd = [sys.executable, str(SCRIPT)]
    for k, v in kwargs.items():
        cmd.extend([f"--{k.replace('_', '-')}", str(v)])

    r = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO, timeout=300)
    out = r.stdout

    result = kwargs.copy()

    # CAGR
    cagrs = []
    for line in out.split("\n"):
        m = re.search(r'^\S{6,}\s+\d+\s+[\d.]+%\s+[+-]?\d+\s+([\d.]+)%\s+[\d.]+\s+[\d.]+\s+([\d.]+)%', line)
        if m:
            cagrs.append(float(m.group(2)))
    result["avg_cagr"] = round(sum(cagrs) / len(cagrs), 2) if cagrs else 0
    result["trades"] = int((re.search(r'合计: (\d+) 笔交易', out) or [0])[1]) if re.search(r'合计: (\d+) 笔交易', out) else 0
    result["all_ok"] = "✅" if "全部盈利: ✅" in out else "❌"

    # DD
    dds = re.findall(r'最大回撤\s+([\d.]+)%', out)
    result["avg_dd"] = round(sum(float(d) for d in dds) / len(dds), 1) if dds else 0

    # 诊断
    for reason in ["初始止损", "D点突破失败", "跟踪止损", "利润保护"]:
        pat = re.escape(reason) + r'\s+(\d+)\s+([\d.]+)%\s+([+-]?\d+)'
        dm = re.search(pat, out)
        if dm:
            result[f"{reason}_笔数"] = int(dm.group(1))
            result[f"{reason}_胜率"] = float(dm.group(2))
            result[f"{reason}_盈亏"] = int(dm.group(3))

    return result


def scan_profit_protect():
    """利润保护倍数扫描。"""
    # 基准参数：最佳组合
    base = dict(stop_mult=2.0, trail_mult=5.0, add_step=0.5,
                max_units=5, reentries=0)

    values = [0, 8, 10, 12, 15, 20]
    print(f"\n{'保护':>5} {'CAGR':>6} {'交易':>5} {'DD':>5} {'初始止损':>10} {'D失败':>8} {'跟踪':>8} {'利润保护':>8}")
    print("-" * 65)
    for v in values:
        r = run_bt(profit_protect=v, **base)
        pp_str = f"{r.get('利润保护_盈亏', 0):+}" if v > 0 else "    -"
        print(f"{v:>5} {r['avg_cagr']:>5.1f}% {r['trades']:>5} "
              f"{r['avg_dd']:>4.1f}% "
              f"{r.get('初始止损_盈亏', 0):>+8} "
              f"{r.get('D点突破失败_盈亏', 0):>+8} "
              f"{r.get('跟踪止损_盈亏', 0):>+8} "
              f"{pp_str:>8}")


def scan_reentries():
    """再进场次数扫描。"""
    base = dict(stop_mult=2.0, trail_mult=5.0, add_step=0.5,
                max_units=5, profit_protect=10)

    values = [0, 1, 2]
    print(f"\n{'再进场':>5} {'CAGR':>6} {'交易':>5} {'DD':>5} {'初始止损':>10} {'D失败':>8} {'跟踪':>8}")
    print("-" * 60)
    for v in values:
        r = run_bt(reentries=v, **base)
        print(f"{v:>5} {r['avg_cagr']:>5.1f}% {r['trades']:>5} "
              f"{r['avg_dd']:>4.1f}% "
              f"{r.get('初始止损_盈亏', 0):>+8} "
              f"{r.get('D点突破失败_盈亏', 0):>+8} "
              f"{r.get('跟踪止损_盈亏', 0):>+8}")


def scan_ultimate():
    """终极组合诊断。"""
    params = dict(stop_mult=2.0, trail_mult=5.0, add_step=0.5,
                  max_units=5, profit_protect=10, reentries=1)
    r = run_bt(diagnose=True, **params)

    print(f"\n{'='*50}")
    print(f"🏆 终极组合诊断")
    print(f"{'='*50}")
    print(f"  CAGR: {r['avg_cagr']}%  |  交易: {r['trades']}笔  |  DD: {r['avg_dd']}%")
    print(f"  全部盈利: {r['all_ok']}")
    print()
    for reason in ["初始止损", "D点突破失败", "跟踪止损", "利润保护"]:
        b = f"{reason}_笔数"
        w = f"{reason}_胜率"
        p = f"{reason}_盈亏"
        if b in r:
            print(f"  {reason}: {r[b]}笔, 胜率{r[w]:.1f}%, 盈亏{r[p]:+}")
    print()


if __name__ == "__main__":
    print("=" * 55)
    print("📊 扫描 1: 利润保护 (基准: stop=2, trail=5, add=0.5, units=5)")
    print("=" * 55)
    scan_profit_protect()

    print("\n" + "=" * 55)
    print("📊 扫描 2: 再进场 (基准: stop=2, trail=5, add=0.5, units=5, pp=10)")
    print("=" * 55)
    scan_reentries()

    print("\n" + "=" * 55)
    scan_ultimate()
