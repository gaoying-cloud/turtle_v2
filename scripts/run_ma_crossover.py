#!/usr/bin/env python
"""
MA 交叉趋势跟踪策略 · 快速回测（验证实验）

独立于 Backtrader 的快速回测，直接输出关键业绩指标。
用于验证"进场 close>MA120, 离场 close<MA60 或 close<进场价"策略。

用法：
    py scripts/run_ma_crossover.py                              # 默认 6 个核心 ETF, IS区间
    py scripts/run_ma_crossover.py --symbols 510500.SH 513100.SH  # 指定品种
    py scripts/run_ma_crossover.py --oos                        # OOS 验证模式
    py scripts/run_ma_crossover.py --ma-slow 200 --ma-fast 50   # 改参数
    py scripts/run_ma_crossover.py --verbose                    # 打印每笔交易
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

# Windows 控制台 UTF-8 编码
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from strategies.ma_crossover import MACrossoverStrategy

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


def load_data(symbol: str, start_date: str,
              end_date: str | None = None) -> pd.DataFrame:
    """加载单个品种数据。"""
    path = DATA_DIR / f"{symbol}.parquet"
    if not path.exists():
        logger.warning("文件不存在: %s", path)
        return pd.DataFrame()
    df = pd.read_parquet(path)
    mask = df["date"] >= start_date
    if end_date is not None:
        mask &= df["date"] <= end_date
    df = df[mask].copy()
    if df.empty:
        return df
    df.sort_values("date", inplace=True)
    df.reset_index(drop=True, inplace=True)
    # date 统一转 datetime
    df["date"] = pd.to_datetime(df["date"])
    return df


def compute_metrics(trades: list, initial_capital: float = 100000.0,
                    total_years: float = 12.5,
                    daily_equity: pd.Series | None = None) -> dict:
    """从交易记录计算业绩指标。

    Parameters
    ----------
    trades : list[Trade]
        交易记录列表。
    initial_capital : float
        初始本金。
    total_years : float
        回测区间总年数，用于 CAGR 计算。
    daily_equity : pd.Series, optional
        日频权益曲线（index=date）。提供时 Sharpe 和 MDD 使用日频数据
        计算，比旧版交易序列法更准确。

    Returns
    -------
    dict
        业绩指标字典。
    """
    empty_result = {
        "总交易": 0, "胜率": 0, "总盈亏": 0,
        "最大回撤": 0, "盈亏比": 0, "夏普": 0,
        "平均盈亏": 0, "CAGR": 0,
        "盈利笔数": 0, "亏损笔数": 0,
    }

    if not trades:
        return empty_result

    pnls = np.array([t.pnl for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    total_pnl = pnls.sum()

    # 胜率
    win_rate = len(wins) / len(pnls) if len(pnls) > 0 else 0

    # 盈亏比
    avg_win = wins.mean() if len(wins) > 0 else 0
    avg_loss = abs(losses.mean()) if len(losses) > 0 else 1
    profit_factor = avg_win / avg_loss if avg_loss != 0 else 0

    # ── 日频净值优先（更准确的 CAGR/Sharpe/MDD） ──
    if daily_equity is not None and len(daily_equity) > 1:
        # 年化收益率：从日频净值曲线计算
        start_eq = float(daily_equity.iloc[0])
        end_eq = float(daily_equity.iloc[-1])
        if start_eq > 0 and end_eq > 0:
            cagr = (end_eq / start_eq) ** (1 / total_years) - 1
        else:
            cagr = float(total_pnl / initial_capital)

        # 日频最大回撤
        peak = daily_equity.expanding().max()
        dd = (peak - daily_equity) / peak
        max_drawdown = float(dd.max()) if not dd.empty else 0.0

        # 日频夏普比率
        daily_returns = daily_equity.pct_change().dropna()
        if len(daily_returns) > 1 and daily_returns.std() > 0:
            sharpe = float((daily_returns.mean() / daily_returns.std()) * np.sqrt(252))
        else:
            sharpe = 0.0
    else:
        # ⚠️ 旧版回退（无日频净值时）
        total_return = total_pnl / initial_capital
        cagr = ((1 + total_return) ** (1 / total_years) - 1) if total_pnl > -initial_capital else -1
        equity = initial_capital + np.cumsum(pnls)
        peak = np.maximum.accumulate(equity)
        drawdown = (peak - equity) / peak
        max_drawdown = float(drawdown.max()) if len(drawdown) > 0 else 0.0

        # per-trade 夏普近似
        trade_returns = pnls / initial_capital
        if trade_returns.std() > 0 and len(trades) > 1:
            avg_holding_days = max(1, total_years * 252 / len(trades))
            sharpe = float((trade_returns.mean() / trade_returns.std())
                           * np.sqrt(252 / avg_holding_days))
        else:
            sharpe = 0.0

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
                  equity_curves: dict[str, pd.Series] | None = None,
                  verbose: bool = False, capital_per_symbol: float = 100000.0,
                  title: str = "MA 交叉趋势跟踪策略"):
    """打印汇总表格。"""
    equity_curves = equity_curves or {}
    rows = []
    for symbol, trades in all_trades.items():
        total_years = date_ranges.get(symbol, 12.5)
        eq = equity_curves.get(symbol)
        m = compute_metrics(trades, initial_capital=capital_per_symbol,
                            total_years=total_years, daily_equity=eq)
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
    print(f"  {title} · 快速回测结果")
    print("=" * len(header))
    print(header)
    print(sep)
    for r in rows:
        print(f"{r['品种']:<12} {r['交易']:>5} {r['胜率']:>6} {r['总盈亏']:>10} {r['最大回撤']:>8} {r['盈亏比']:>6} {r['夏普']:>6} {r['CAGR']:>8}")
    print(sep)

    # 汇总
    avg_cagr = np.mean([
        compute_metrics(
            trades, initial_capital=capital_per_symbol,
            total_years=date_ranges.get(sym, 12.5),
            daily_equity=equity_curves.get(sym),
        )["CAGR"]
        for sym, trades in all_trades.items() if trades
    ])
    total_trades = sum(len(t) for t in all_trades.values())
    all_profitable = all(
        compute_metrics(t, initial_capital=capital_per_symbol,
                        total_years=date_ranges.get(sym, 12.5),
                        daily_equity=equity_curves.get(sym))['总盈亏'] > 0
        for sym, t in all_trades.items() if t
    )
    print(f"\n📊  合计: {total_trades} 笔交易  |  "
          f"品种数: {len(all_trades)}  |  "
          f"平均 CAGR: {avg_cagr:.1%}  |  "
          f"全部盈利: {'✅' if all_profitable else '❌'}")
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


def diagnose_trades(all_trades: dict[str, list], date_ranges: dict[str, float],
                    capital_per_symbol: float = 100000.0):
    """诊断退出原因分布和持仓特征。"""
    # 按退出原因分类
    by_reason: dict[str, list] = {}
    for symbol, trades in all_trades.items():
        for t in trades:
            reason = t.exit_reason or "未知"
            if reason not in by_reason:
                by_reason[reason] = []
            by_reason[reason].append(t)

    header = (f"{'退出原因':<16} {'笔数':>5} {'胜率':>7} {'总盈亏':>10} "
              f"{'平均盈亏':>10} {'持仓(天)':>8}")
    sep = "-" * len(header)
    print(f"\n{header}\n{sep}")

    for reason, trades in sorted(by_reason.items(),
                                  key=lambda kv: sum(t.pnl for t in kv[1]),
                                  reverse=True):
        pnls = np.array([t.pnl for t in trades])
        m = compute_metrics(trades, initial_capital=capital_per_symbol,
                            total_years=12.5)
        avg_hold = np.mean([max(0, t.exit_idx - t.entry_idx) for t in trades])
        print(f"{reason:<16} {len(trades):>5} {m['胜率']:>7.1%} "
              f"{m['总盈亏']:>+10.0f} {pnls.mean():>+10.0f} {avg_hold:>8.0f}")

    print(sep)

    # 汇总
    all_t = [t for ts in all_trades.values() for t in ts]
    pnls_all = np.array([t.pnl for t in all_t])
    avg_hold_all = np.mean([max(0, t.exit_idx - t.entry_idx) for t in all_t])
    print(f"{'合计':<16} {len(all_t):>5} {'':>7} "
          f"{pnls_all.sum():>+10.0f} {pnls_all.mean():>+10.0f} {avg_hold_all:>8.0f}")

    # 盈利/亏损持仓分析
    winners = [t for t in all_t if t.pnl > 0]
    losers = [t for t in all_t if t.pnl <= 0]
    if winners and losers:
        w_hold = np.mean([max(0, t.exit_idx - t.entry_idx) for t in winners])
        l_hold = np.mean([max(0, t.exit_idx - t.entry_idx) for t in losers])
        print(f"\n  盈利交易: 平均持仓 {w_hold:.0f}天 ({len(winners)}笔) | "
              f"亏损交易: 平均持仓 {l_hold:.0f}天 ({len(losers)}笔)")
    print()


def main():
    parser = argparse.ArgumentParser(description="MA 交叉趋势跟踪策略快速回测")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS,
                        help="品种代码列表 (默认: 6 核心 ETF)")
    parser.add_argument("--start", default="2014-01-01",
                        help="起始日期")
    parser.add_argument("--end", default="2020-01-01",
                        help="截止日期 (默认: 2020-01-01, IS 6年)")
    parser.add_argument("--oos", action="store_true",
                        help="OOS 验证模式: 2020-01-01 ~ 数据末尾")
    parser.add_argument("--ma-slow", type=int, default=120,
                        help="慢线周期 (默认: 120)")
    parser.add_argument("--ma-fast", type=int, default=60,
                        help="快线周期 (默认: 60)")
    parser.add_argument("--position-pct", type=float, default=0.20,
                        help="单品种仓位比例 (默认: 0.20 = 20%%)")
    parser.add_argument("--stop-floor", type=float, default=0.0,
                        help="止损地板比例：0=保本, 0.95=5%%止损, -1=关闭 (默认: 0)")
    parser.add_argument("--slippage", type=float, default=0.001,
                        help="成交滑点 (默认: 0.001 = 0.1%%)")
    parser.add_argument("--commission", type=float, default=0.00015,
                        help="手续费率 (默认: 0.00015 = 万1.5)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="打印逐笔交易明细")
    parser.add_argument("--diagnose", action="store_true",
                        help="诊断分析：退出原因分布/持仓时长")
    args = parser.parse_args()

    stop_label = f"{args.stop_floor:.0%}" if args.stop_floor >= 0 else "关闭"
    print(f"\n[参数] MA慢线(进场)={args.ma_slow}, MA快线(出场)={args.ma_fast}, "
          f"仓位={args.position_pct:.0%}, 止损地板={stop_label}, "
          f"滑点={args.slippage:.3f}, 费率={args.commission:.4f}")

    if args.oos:
        args.start = "2020-01-01"
        args.end = None
    end_label = args.end if args.end else "数据末尾"
    mode_label = "OOS" if args.oos else "IS"
    print(f"📅  区间 [{mode_label}]: {args.start} ~ {end_label}")
    print(f"📈  品种: {', '.join(args.symbols)}")

    # 初始化策略
    strategy = MACrossoverStrategy(
        ma_slow=args.ma_slow,
        ma_fast=args.ma_fast,
        position_pct=args.position_pct,
        stop_floor=args.stop_floor,
        num_symbols=len(args.symbols),
        slippage_pct=args.slippage,
        commission_pct=args.commission,
    )

    # 逐个品种跑
    all_trades: dict[str, list] = {}
    all_equity: dict[str, pd.Series] = {}
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
        print(f"\n📥  {symbol}: {len(df)} 条数据 ({df['date'].iloc[0].date()} ~ "
              f"{df['date'].iloc[-1].date()}, {total_years:.1f}年)")

        _, trades, equity = strategy.run(df, symbol=symbol, verbose=args.verbose)
        all_trades[symbol] = trades
        all_equity[symbol] = equity
        print(f"    → {len(trades)} 笔交易")

    # 输出汇总
    cap_per_sym = strategy.capital_per_symbol
    print_summary(all_trades, date_ranges, equity_curves=all_equity,
                  verbose=args.verbose, capital_per_symbol=cap_per_sym)

    # ── 诊断分析 ──
    if args.diagnose:
        print("=" * 60)
        print("  🔬 诊断分析：退出原因分布")
        print("=" * 60)
        diagnose_trades(all_trades, date_ranges, capital_per_symbol=cap_per_sym)


if __name__ == "__main__":
    main()
