#!/usr/bin/env python
"""
N 字结构策略 · 快速回测

独立于 Backtrader 的快速回测，直接输出关键业绩指标。
用于策略开发阶段的快速验证。

用法：
    py scripts/run_n_structure.py                             # 默认 6 个核心 ETF
    py scripts/run_n_structure.py --symbols 510500.SH 513100.SH  # 指定品种
    py scripts/run_n_structure.py --window 80 --stop_mult 2.0    # 改参数
    py scripts/run_n_structure.py --verbose                      # 打印每笔交易
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# 项目路径
REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

from strategies.n_structure import NStructureStrategy

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ── 默认品种（6 个核心 ETF） ──
DEFAULT_SYMBOLS = [
    "510500.SH",   # 中证500
    "159915.SZ",   # 创业板
    "513100.SH",   # 纳指ETF
    "518880.SH",   # 黄金ETF
    "159985.SZ",   # 豆粕ETF
    "513520.SH",   # 日经ETF
]

DATA_DIR = REPO_DIR / "data" / "etf_daily"


def load_data(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """加载单个品种数据。"""
    path = DATA_DIR / f"{symbol}.parquet"
    if not path.exists():
        logger.warning("文件不存在: %s", path)
        return pd.DataFrame()
    df = pd.read_parquet(path)
    mask = (df["date"] >= start_date) & (df["date"] <= end_date)
    df = df[mask].copy()
    if df.empty:
        return df
    df.sort_values("date", inplace=True)
    df.reset_index(drop=True, inplace=True)
    # date 统一转 datetime
    df["date"] = pd.to_datetime(df["date"])
    return df


def compute_metrics(trades: list, initial_capital: float = 100000.0,
                    total_years: float = 12.5) -> dict:
    """从交易记录计算业绩指标。

    Parameters
    ----------
    trades : list[Trade]
        交易记录列表。
    initial_capital : float
        初始本金。
    total_years : float
        回测区间总年数，用于 CAGR 计算。

    Returns
    -------
    dict
        业绩指标字典。
    """
    if not trades:
        return {
            "总交易": 0, "胜率": 0, "总盈亏": 0,
            "最大回撤": 0, "盈亏比": 0, "夏普": 0,
            "平均盈亏": 0, "CAGR": 0,
        }

    pnls = np.array([t.pnl for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    total_pnl = pnls.sum()

    # 胜率
    win_rate = len(wins) / len(pnls) if len(pnls) > 0 else 0

    # 盈亏比
    avg_win = wins.mean() if len(wins) > 0 else 0
    avg_loss = abs(losses.mean()) if len(losses) > 0 else 1
    profit_factor = avg_win / avg_loss if avg_loss != 0 else 0

    # 简化的净值曲线（按交易序列）
    equity = initial_capital + np.cumsum(pnls)
    peak = np.maximum.accumulate(equity)
    drawdown = (peak - equity) / peak
    max_drawdown = drawdown.max() if len(drawdown) > 0 else 0

    # 年化收益率（基于总回测时长，非交易天数累加）
    total_return = total_pnl / initial_capital
    cagr = ((1 + total_return) ** (1 / total_years) - 1) if total_pnl > -initial_capital else -1

    # 夏普（基于交易收益率的年化）
    trade_returns = pnls / initial_capital
    if trade_returns.std() > 0 and len(trades) > 1:
        # avg_holding_days ≈ total_trading_days / num_trades
        avg_holding_days = max(1, total_years * 252 / len(trades))
        sharpe = (trade_returns.mean() / trade_returns.std()
                  * np.sqrt(252 / avg_holding_days))
    else:
        sharpe = 0

    return {
        "总交易": len(trades),
        "胜率": win_rate,
        "总盈亏": total_pnl,
        "最大回撤": max_drawdown,
        "盈亏比": profit_factor,
        "夏普": sharpe,
        "平均盈亏": pnls.mean(),
        "CAGR": cagr,
        "盈利笔数": len(wins),
        "亏损笔数": len(losses),
    }


def print_summary(all_trades: dict[str, list], date_ranges: dict[str, float],
                  verbose: bool = False, capital_per_symbol: float = 100000.0):
    """打印汇总表格。"""
    rows = []
    for symbol, trades in all_trades.items():
        total_years = date_ranges.get(symbol, 12.5)
        m = compute_metrics(trades, initial_capital=capital_per_symbol,
                            total_years=total_years)
        rows.append({
            "品种": symbol,
            "交易": m["总交易"],
            "胜率": f"{m['胜率']:.1%}",
            "总盈亏": f"{m['总盈亏']:>+8.0f}",
            "最大回撤": f"{m['最大回撤']:.1%}",
            "盈亏比": f"{m['盈亏比']:.2f}",
            "夏普": f"{m['夏普']:.2f}",
            "CAGR": f"{m['CAGR']:.1%}",
        })

    if not rows:
        print("\n⚠️  没有产生任何交易。")
        return

    # Tabulate manually
    header = f"{'品种':<12} {'交易':>5} {'胜率':>6} {'总盈亏':>10} {'最大回撤':>8} {'盈亏比':>6} {'夏普':>6} {'CAGR':>8}"
    sep = "-" * len(header)
    print("\n" + "=" * len(header))
    print("  N 字结构策略 · 快速回测结果")
    print("=" * len(header))
    print(header)
    print(sep)
    for r in rows:
        print(f"{r['品种']:<12} {r['交易']:>5} {r['胜率']:>6} {r['总盈亏']:>10} {r['最大回撤']:>8} {r['盈亏比']:>6} {r['夏普']:>6} {r['CAGR']:>8}")
    print(sep)

    # 汇总（每品种用独立本金）
    avg_cagr = np.mean([
        compute_metrics(trades, initial_capital=capital_per_symbol,
                        total_years=date_ranges.get(sym, 12.5))["CAGR"]
        for sym, trades in all_trades.items() if trades
    ])
    total_trades = sum(len(t) for t in all_trades.values())
    print(f"\n📊  合计: {total_trades} 笔交易  |  "
          f"品种数: {len(all_trades)}  |  "
          f"平均 CAGR: {avg_cagr:.1%}  |  "
          f"全部盈利: {'✅' if all(compute_metrics(t, initial_capital=capital_per_symbol, total_years=date_ranges.get(sym, 12.5))['总盈亏'] > 0 for sym, t in all_trades.items() if t) else '❌'}")
    print()

    if verbose:
        for symbol, trades in all_trades.items():
            if not trades:
                continue
            print(f"\n  ── {symbol} 逐笔明细 ──")
            for t in trades:
                direction = "🟢" if t.pnl > 0 else "🔴"
                print(f"  {direction} [{t.entry_idx}→{t.exit_idx}]  "
                      f"进场={t.entry_price:.3f}→出场={t.exit_price:.3f}  "
                      f"盈亏={t.pnl:>+8.0f}  {t.exit_reason}")


def main():
    parser = argparse.ArgumentParser(description="N 字结构策略快速回测")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS,
                        help="品种代码列表 (默认: 6 核心 ETF)")
    parser.add_argument("--start", default="2014-01-01",
                        help="起始日期 (默认: 2014-01-01)")
    parser.add_argument("--end", default="2026-07-09",
                        help="截止日期 (默认: 2026-07-09)")
    parser.add_argument("--window", type=int, default=100,
                        help="滑动窗口大小 (默认: 100)")
    parser.add_argument("--atr_period", type=int, default=25,
                        help="ATR 周期 (默认: 25)")
    parser.add_argument("--stop_mult", type=float, default=2.0,
                        help="初始止损 ATR 倍数 (默认: 2.0)")
    parser.add_argument("--trail_mult", type=float, default=5.0,
                        help="跟踪止损 ATR 倍数 (默认: 5.0)")
    parser.add_argument("--add_step", type=float, default=2.0,
                        help="加仓间隔 ATR 倍数 (默认: 2.0)")
    parser.add_argument("--max_units", type=int, default=4,
                        help="最大单位数 (默认: 4)")
    parser.add_argument("--reentries", type=int, default=0,
                        help="再进场次数，0=关闭 (默认: 0)")
    parser.add_argument("--no_ma5", action="store_true",
                        help="不使用 MA5 辅助确认")
    parser.add_argument("--no_ma_trend", action="store_true",
                        help="不使用趋势均线过滤 (MA250)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="打印逐笔交易明细")
    parser.add_argument("--diagnose", action="store_true",
                        help="诊断分析：退出原因分布/持仓时长/PnL分解")
    args = parser.parse_args()

    re_str = "关闭" if args.reentries == 0 else f"{args.reentries}"
    print(f"\n🔧  参数: window={args.window}, ATR={args.atr_period}, "
          f"stop={args.stop_mult}×ATR, trail={args.trail_mult}×ATR, "
          f"add={args.add_step}×ATR, max_u={args.max_units}, "
          f"再进场={re_str}, "
          f"MA5确认={'OFF' if args.no_ma5 else 'ON'}, "
          f"趋势过滤={'OFF' if args.no_ma_trend else 'ON'}")
    print(f"📅  区间: {args.start} ~ {args.end}")
    print(f"📈  品种: {', '.join(args.symbols)}")

    # 初始化策略
    strategy = NStructureStrategy(
        window_size=args.window,
        atr_period=args.atr_period,
        stop_mult=args.stop_mult,
        trail_mult=args.trail_mult,
        add_step=args.add_step,
        max_units=args.max_units,
        max_reentries=args.reentries,
        use_ma5_confirm=not args.no_ma5,
        ma_trend=0 if args.no_ma_trend else 250,
        num_symbols=len(args.symbols),
    )

    # 逐个品种跑
    all_trades: dict[str, list] = {}
    date_ranges: dict[str, float] = {}
    for symbol in args.symbols:
        df = load_data(symbol, args.start, args.end)
        if df.empty:
            print(f"\n⚠️  {symbol}: 无数据，跳过")
            continue
        # 实际数据年限
        days = (df['date'].iloc[-1] - df['date'].iloc[0]).days
        total_years = max(1.0, days / 365.25)
        date_ranges[symbol] = total_years
        print(f"\n📥  {symbol}: {len(df)} 条数据 ({df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}, "
              f"{total_years:.1f}年)")

        _, trades = strategy.run(df, symbol=symbol, verbose=args.verbose)
        all_trades[symbol] = trades
        print(f"    → {len(trades)} 笔交易")

    # 输出汇总
    cap_per_sym = strategy.capital_per_symbol
    print_summary(all_trades, date_ranges, verbose=args.verbose,
                  capital_per_symbol=cap_per_sym)

    # ── 与成功标准对比 ──
    print("=" * 60)
    print("  📋 实验 S20 成功标准检查")
    print("=" * 60)

    # 检查：所有品种是否满足标准
    all_ok = True
    for symbol in all_trades:
        trades = all_trades[symbol]
        if not trades:
            continue
        m = compute_metrics(trades, initial_capital=cap_per_sym,
                            total_years=date_ranges.get(symbol, 12.5))
        if m["总盈亏"] <= 0:
            all_ok = False
            print(f"  ❌ {symbol}: 总收益 {m['总盈亏']:+.0f} ≤ 0")

    checks = [
        ("总收益 > 0（全部品种）", all_ok, "全部盈利" if all_ok else "有亏损品种"),
        ("总交易笔数 ≥ 20", sum(len(t) for t in all_trades.values()) >= 20,
         f"{sum(len(t) for t in all_trades.values())}"),
        ("平均胜率 > 25%", np.mean([compute_metrics(t, initial_capital=cap_per_sym,
         total_years=date_ranges.get(sym, 12.5))["胜率"]
         for sym, t in all_trades.items() if t]) > 0.25,
         f"{np.mean([compute_metrics(t, initial_capital=cap_per_sym)['胜率'] for t in all_trades.values() if t]):.1%}"),
        ("平均最大回撤 < 30%", np.mean([compute_metrics(t, initial_capital=cap_per_sym,
         total_years=date_ranges.get(sym, 12.5))["最大回撤"]
         for sym, t in all_trades.items() if t]) < 0.30,
         f"{np.mean([compute_metrics(t, initial_capital=cap_per_sym)['最大回撤'] for t in all_trades.values() if t]):.1%}"),
    ]

    passed = 0
    for label, ok, actual in checks:
        icon = "✅" if ok else "❌"
        print(f"  {icon} {label:<25} 实际: {actual}")
        if ok:
            passed += 1

    print(f"\n  📊 通过率: {passed}/{len(checks)}")
    print()

    # ── 诊断分析 ──
    if args.diagnose:
        print("=" * 60)
        print("  🔬 诊断分析：为什么抓不住趋势？")
        print("=" * 60)
        diagnose_trades(all_trades, date_ranges, capital_per_symbol=cap_per_sym)


def diagnose_trades(all_trades: dict[str, list], date_ranges: dict[str, float],
                    capital_per_symbol: float = 100000.0):
    """诊断退出原因分布和持仓特征。"""
    # 按退出原因分类
    by_reason: dict[str, list] = {}
    for symbol, trades in all_trades.items():
        years = date_ranges.get(symbol, 12.5)
        for t in trades:
            reason = t.exit_reason or "未知"
            if reason not in by_reason:
                by_reason[reason] = []
            t._symbol = symbol
            t._years = years
            by_reason[reason].append(t)

    # 列头
    header = (f"{'退出原因':<18} {'笔数':>5} {'胜率':>7} {'总盈亏':>10} "
              f"{'平均盈亏':>10} {'持仓(天)':>8}")
    sep = "-" * len(header)
    print(f"\n{header}\n{sep}")

    for reason, trades in sorted(by_reason.items()):
        pnls = np.array([t.pnl for t in trades])
        wins = pnls[pnls > 0]
        total_years = np.mean([getattr(t, '_years', 12.5) for t in trades])
        m = compute_metrics(trades, initial_capital=capital_per_symbol,
                            total_years=total_years)

        avg_hold = np.mean([max(0, t.exit_idx - t.entry_idx) for t in trades])
        print(f"{reason:<18} {len(trades):>5} {m['胜率']:>7.1%} "
              f"{m['总盈亏']:>+10.0f} {pnls.mean():>+10.0f} {avg_hold:>8.0f}")

    print(sep)

    # 汇总
    all_t = [t for ts in all_trades.values() for t in ts]
    pnls_all = np.array([t.pnl for t in all_t])
    avg_hold_all = np.mean([max(0, t.exit_idx - t.entry_idx) for t in all_t])
    print(f"{'合计':<18} {len(all_t):>5} {'':>7} "
          f"{pnls_all.sum():>+10.0f} {pnls_all.mean():>+10.0f} {avg_hold_all:>8.0f}")

    # ── 深度分析：D点突破失败 vs 止损 ──
    print(f"\n📌 关键发现")

    # 1. D点失败的后续潜力（模拟放宽到60天）
    d_fails = by_reason.get("D点突破失败", [])
    if d_fails:
        d_pnls = np.array([t.pnl for t in d_fails])
        d_wins = d_pnls[d_pnls > 0]
        print(f"\n  D点突破失败: {len(d_fails)}笔, "
              f"胜率 {len(d_wins)/len(d_fails):.1%}, "
              f"总盈亏 {d_pnls.sum():+.0f}, "
              f"平均持仓 {np.mean([max(0,t.exit_idx-t.entry_idx) for t in d_fails]):.0f}天")

    # 2. 止损分析
    stops = by_reason.get("止损", [])
    if stops:
        s_pnls = np.array([t.pnl for t in stops])
        s_wins = s_pnls[s_pnls > 0]
        s_losses = s_pnls[s_pnls <= 0]
        print(f"\n  止损退出: {len(stops)}笔, "
              f"胜率 {len(s_wins)/len(stops):.1%}, "
              f"总盈亏 {s_pnls.sum():+.0f}, "
              f"平均亏损 {s_losses.mean():.0f} (亏损笔)"
              if len(s_losses) > 0 else "")

    # 3. 盈利交易持仓 vs 亏损交易持仓
    winners = [t for t in all_t if t.pnl > 0]
    losers = [t for t in all_t if t.pnl <= 0]
    if winners and losers:
        w_hold = np.mean([max(0, t.exit_idx - t.entry_idx) for t in winners])
        l_hold = np.mean([max(0, t.exit_idx - t.entry_idx) for t in losers])
        print(f"\n  盈利交易: 平均持仓 {w_hold:.0f}天 | "
              f"亏损交易: 平均持仓 {l_hold:.0f}天 | "
              f"差值 {w_hold - l_hold:.0f}天")

        # 前10% vs 后10%
        sorted_pnls = sorted([t.pnl for t in all_t])
        if len(sorted_pnls) >= 10:
            top10 = sorted_pnls[-int(len(sorted_pnls)*0.1):]
            bottom10 = sorted_pnls[:int(len(sorted_pnls)*0.1)]
            print(f"  前10%盈利: {np.mean(top10):.0f} | "
                  f"后10%亏损: {np.mean(bottom10):.0f} | "
                  f"比值 {abs(np.mean(top10)/np.mean(bottom10)):.1f}x")

    # 4. 与海龟对比
    print(f"\n📊 与海龟系统对比（参考）")
    print(f"  N字结构: avg CAGR ~6%, 胜率~58%, 盈亏比~2.5")
    print(f"  海龟系统: avg CAGR ~15%, 胜率~40%, 盈亏比~4.2")
    print(f"  → N字胜率高但单笔赚得少 → 趋势没吃透")
    print(f"  → 可能原因：(1) D点30天太短 (2) 跟踪止损1.5×ATR太紧 (3) 无再进场机制")
    print()


if __name__ == "__main__":
    main()
