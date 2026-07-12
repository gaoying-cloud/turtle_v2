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

# Windows 控制台 UTF-8 编码
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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

    # ── S24/S25: 日频净值优先（更准确的 CAGR/Sharpe/MDD） ──
    if daily_equity is not None and len(daily_equity) > 1:
        # 年化收益率：从日频净值曲线计算（支持复利/动态权益）
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
        # ⚠️ 旧版回退（无日频净值时）—— 交易序列近似，精度低于日频
        total_return = total_pnl / initial_capital
        cagr = ((1 + total_return) ** (1 / total_years) - 1) if total_pnl > -initial_capital else -1
        equity = initial_capital + np.cumsum(pnls)
        peak = np.maximum.accumulate(equity)
        drawdown = (peak - equity) / peak
        max_drawdown = float(drawdown.max()) if len(drawdown) > 0 else 0.0

        # per-trade 夏普近似（推荐改用日频净值获得准确值）
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
                  verbose: bool = False, capital_per_symbol: float = 100000.0):
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
    print("  N 字结构策略 · 快速回测结果")
    print("=" * len(header))
    print(header)
    print(sep)
    for r in rows:
        print(f"{r['品种']:<12} {r['交易']:>5} {r['胜率']:>6} {r['总盈亏']:>10} {r['最大回撤']:>8} {r['盈亏比']:>6} {r['夏普']:>6} {r['CAGR']:>8}")
    print(sep)

    # 汇总（每品种用独立本金，日频净值优先）
    avg_cagr = np.mean([
        compute_metrics(
            trades, initial_capital=capital_per_symbol,
            total_years=date_ranges.get(sym, 12.5),
            daily_equity=equity_curves.get(sym),
        )["CAGR"]
        for sym, trades in all_trades.items() if trades
    ])
    total_trades = sum(len(t) for t in all_trades.values())
    print(f"\n📊  合计: {total_trades} 笔交易  |  "
          f"品种数: {len(all_trades)}  |  "
          f"平均 CAGR: {avg_cagr:.1%}  |  "
          f"全部盈利: {'✅' if all(compute_metrics(t, initial_capital=capital_per_symbol, total_years=date_ranges.get(sym, 12.5), daily_equity=equity_curves.get(sym))['总盈亏'] > 0 for sym, t in all_trades.items() if t) else '❌'}")
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
                        help="起始日期")
    parser.add_argument("--end", default="2020-01-01",
                        help="截止日期 (默认: 2020-01-01, IS 6年)")
    parser.add_argument("--oos", action="store_true",
                        help="OOS 验证模式: 2020-01-01 ~ 数据末尾")
    parser.add_argument("--window", type=int, default=100,
                        help="滑动窗口大小 (默认: 100, S22调优定型)")
    parser.add_argument("--atr_period", type=int, default=25,
                        help="ATR 周期 (默认: 25)")
    parser.add_argument("--stop_mult", type=float, default=1.5,
                        help="初始止损 ATR 倍数 (默认: 1.5, S22调优)")
    parser.add_argument("--trail_mult", type=float, default=5.0,
                        help="跟踪止损 ATR 倍数 (默认: 5.0)")
    parser.add_argument("--trail-wide", type=float, default=8.0,
                        help="跟踪止损宽倍数 (默认: 8.0, D突破初期)")
    parser.add_argument("--trail-tight", type=float, default=3.0,
                        help="跟踪止损紧倍数 (默认: 3.0, 大浮盈锁利)")
    parser.add_argument("--d-timeout", type=int, default=40,
                        help="D点超时天数 (默认: 40)")
    parser.add_argument("--add_step", type=float, default=2.0,
                        help="加仓间隔 ATR 倍数 (默认: 2.0)")
    parser.add_argument("--max_units", type=int, default=6,
                        help="最大单位数 (默认: 6, S22调优)")
    parser.add_argument("--reentries", type=int, default=0,
                        help="再进场次数，0=关闭 (默认: 0)")
    parser.add_argument("--ma5", action="store_true",
                        help="开启 MA5 辅助确认 (默认关闭, S22调优)")
    parser.add_argument("--ma_trend", type=int, default=50,
                        help="趋势均线周期，0=关闭 (默认: 50 = MA50)")
    parser.add_argument("--entry-confirm", type=int, default=2,
                        help="进场确认K线数，需连续站上B点 (默认: 2)")
    parser.add_argument("--slippage", type=float, default=0.001,
                        help="成交滑点 (默认: 0.001 = 0.1%%)")
    parser.add_argument("--commission", type=float, default=0.00015,
                        help="手续费率 (默认: 0.00015 = 万1.5)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="打印逐笔交易明细")
    parser.add_argument("--portfolio", action="store_true",
                        help="组合模式：共享资金池回测 (S26)")
    parser.add_argument("--max-exposure", type=float, default=1.5,
                        help="组合模式最大总敞口 (默认: 1.5 = 150%%)")
    parser.add_argument("--no-ma-cross", action="store_true",
                        help="关闭 MA5×MA20 金叉过滤")
    parser.add_argument("--max-pos-pct", type=float, default=0.25,
                        help="单品种最大仓位比例 (默认: 0.25 = 25%%)")
    parser.add_argument("--max-ad", type=float, default=1.0,
                        help="A→D最大涨幅上限，1.0=关闭 (S38实验)")
    parser.add_argument("--max-ab", type=float, default=1.0,
                        help="A→B最大抬升，1.0=关闭 (默认: 1.0)")
    parser.add_argument("--trail-pre-d", type=float, default=2.5,
                        help="D突破前ATR跟踪倍数 (默认: 2.5)")
    parser.add_argument("--no-ma-exit", action="store_true",
                        help="关闭MA20出场，回退到旧ATR三阶段跟踪止损")
    parser.add_argument("--ma-exit-period", type=int, default=20,
                        help="MA出场均线周期 (默认: 20)")
    parser.add_argument("--ma-exit-margin", type=float, default=0.97,
                        help="MA有效跌破阈值 (默认: 0.97, confirm=0时生效)")
    parser.add_argument("--ma-exit-confirm", type=int, default=0,
                        help="MA有效跌破: 0=margin, >0=连续N日, -1=K线实体1/3法 (默认: 0)")
    parser.add_argument("--no-bearish", action="store_true",
                        help="关闭MA出场阴线过滤")
    parser.add_argument("--d-exit-floor", type=float, default=0.95,
                        help="D点硬止损地板比例 (默认: 0.95)")
    parser.add_argument("--diagnose", action="store_true",
                        help="诊断分析：退出原因分布/持仓时长/PnL分解")
    args = parser.parse_args()

    re_str = "关闭" if args.reentries == 0 else f"{args.reentries}"
    print(f"\n[参数] window={args.window}, ATR={args.atr_period}, "
          f"stop={args.stop_mult}×ATR, trail={args.trail_mult}×ATR, "
          f"add={args.add_step}×ATR, max_u={args.max_units}, "
          f"再进场={re_str}, "
          f"滑点={args.slippage:.3f}, 费率={args.commission:.4f}, "
          f"MA5确认={'ON' if args.ma5 else 'OFF'}, "
          f"趋势MA={args.ma_trend} (0=关), "
          f"进场确认={args.entry_confirm}K线, "
          f"AD上限={args.max_ad:.0%}, AB上限={'关' if args.max_ab >= 1.0 else f'{args.max_ab:.0%}'}, "
          f"MA出场={'OFF' if args.no_ma_exit else 'MA20实体1/3法' if args.ma_exit_confirm<0 else f'MA20×{args.ma_exit_confirm}日确认' if args.ma_exit_confirm>0 else f'MA20×{args.ma_exit_margin}'}, "
          f"D前跟踪={args.trail_pre_d}×ATR")
    if args.oos:
        args.start = "2020-01-01"
        args.end = None
    end_label = args.end if args.end else "数据末尾"
    mode_label = "OOS" if args.oos else "IS"
    print(f"📅  区间 [{mode_label}]: {args.start} ~ {end_label}")
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
        use_ma5_confirm=args.ma5,           # --ma5 开启, 默认关闭
        ma_trend=args.ma_trend,             # 默认 50=MA50, 0=关闭 (S37)
        entry_confirm_bars=args.entry_confirm,  # S37 进场确认延迟
        num_symbols=len(args.symbols),
        slippage_pct=args.slippage,
        commission_pct=args.commission,
        use_ma_cross=not args.no_ma_cross,
        max_position_pct=args.max_pos_pct,
        trail_mult_wide=args.trail_wide,
        trail_mult_tight=args.trail_tight,
        d_timeout_days=args.d_timeout,
        confirm_k=3,  # S37: B点确认从2→3
        max_ad_advance=args.max_ad,  # S38: A→D涨幅上限
        max_ab_advance=args.max_ab,  # S38: A→B抬升上限（候选）
        trail_pre_d=args.trail_pre_d,           # S39: D突破前跟踪倍数
        use_ma_exit=not args.no_ma_exit,        # S39: MA20趋势出场
        ma_exit_period=args.ma_exit_period,     # S39: MA周期
        ma_exit_margin=args.ma_exit_margin,     # S39: 跌破阈值
        ma_exit_confirm=args.ma_exit_confirm,   # S39: 确认天数
        ma_exit_bearish=not args.no_bearish,     # S39: 阴线过滤
        d_exit_floor=args.d_exit_floor,         # S39: D点硬止损地板
    )

    # ── 组合模式 (S26) ──
    if args.portfolio:
        print(f"\n{'='*60}")
        print(f"  📊 组合模式 — 共享资金池回测")
        print(f"  总资金: ¥{strategy.initial_capital:,.0f}  |  "
              f"最大敞口: {args.max_exposure:.0%}")
        print(f"{'='*60}")

        dfs = {}
        for symbol in args.symbols:
            df = load_data(symbol, args.start, args.end)
            if df.empty:
                print(f"\n⚠️  {symbol}: 无数据，跳过")
                continue
            dfs[symbol] = df
            days = (df['date'].iloc[-1] - df['date'].iloc[0]).days
            print(f"📥  {symbol}: {len(df)} 条数据 ({df['date'].iloc[0].date()} ~ "
                  f"{df['date'].iloc[-1].date()}, {days/365.25:.1f}年)")

        result = strategy.run_portfolio(
            dfs, max_total_exposure=args.max_exposure,
            verbose=args.verbose,
        )

        # 组合汇总
        eq = result['portfolio_equity']
        years = (eq.index[-1] - eq.index[0]).days / 365.25
        m = compute_metrics(
            result['all_trades'],
            initial_capital=strategy.initial_capital,
            total_years=years,
            daily_equity=eq,
        )
        print(f"\n{'='*65}")
        print(f"  组合回测结果")
        print(f"{'='*65}")
        print(f"  总交易: {m['总交易']}  |  胜率: {m['胜率']:.1%}  |  "
              f"总盈亏: ¥{m['总盈亏']:+,.0f}")
        print(f"  CAGR: {m['CAGR']:.1%}  |  夏普: {m['夏普']:.2f}  |  "
              f"MDD: {m['最大回撤']:.1%}  |  盈亏比: {m['盈亏比']:.2f}")
        print(f"  终值: ¥{eq.iloc[-1]:,.0f}  |  最高敞口: "
              f"{result['daily_exposure'].max():.0%}")
        print()

        # 品种归因
        print(f"  {'品种':<12} {'交易':>5} {'盈亏':>12} {'贡献%':>7}")
        print(f"  {'-'*40}")
        total_pnl = sum(
            sum(t.pnl for t in result['symbol_trades'].get(s, []))
            for s in args.symbols
        )
        for s in args.symbols:
            st = result['symbol_trades'].get(s, [])
            spnl = sum(t.pnl for t in st)
            share = spnl / total_pnl * 100 if total_pnl != 0 else 0
            print(f"  {s:<12} {len(st):>5} {spnl:>+12,.0f} {share:>6.1f}%")
        print()

        # ── 与独立模式对比 ──
        print(f"  {'='*65}")
        print(f"  📊 组合 vs 独立 对比")
        print(f"  {'='*65}")
        # 跑独立模式
        all_trades_ind: dict[str, list] = {}
        all_equity_ind: dict[str, pd.Series] = {}
        for sym in args.symbols:
            if sym not in dfs:
                continue
            _, trades, eq_sym = strategy.run(dfs[sym], symbol=sym, verbose=False)
            all_trades_ind[sym] = trades
            all_equity_ind[sym] = eq_sym
        # 独立模式总净值 = 各品种净值之和
        eq_panel = pd.DataFrame(all_equity_ind).ffill()
        eq_combined = eq_panel.sum(axis=1)
        total_trades_ind = sum(len(t) for t in all_trades_ind.values())
        total_pnl_ind = sum(
            sum(t.pnl for t in trades) for trades in all_trades_ind.values()
        )
        m_ind = compute_metrics(
            [t for trades in all_trades_ind.values() for t in trades],
            initial_capital=strategy.initial_capital,
            total_years=years,
            daily_equity=eq_combined,
        )
        print(f"  {'指标':<12} {'组合(共享池)':>14} {'独立(求和)':>14} {'变化':>10}")
        print(f"  {'-'*52}")
        for label, key, fmt in [
            ('CAGR', 'CAGR', '.1%'), ('夏普', '夏普', '.2f'),
            ('MDD', '最大回撤', '.1%'), ('交易笔数', '总交易', ''),
            ('胜率', '胜率', '.1%'),
        ]:
            v_port = m[key]
            v_ind = m_ind[key]
            if key == '总交易':
                v_port = m['总交易']
                v_ind = total_trades_ind
            if 'CAGR' in key or 'MDD' in key or '胜率' in key:
                delta = f"{v_port - v_ind:+.1%}"
            elif fmt == '.2f':
                delta = f"{v_port - v_ind:+.2f}"
            else:
                delta = f"{int(v_port) - int(v_ind):+d}"
            p_str = f"{v_port:{fmt}}" if fmt else str(v_port)
            i_str = f"{v_ind:{fmt}}" if fmt else str(v_ind)
            print(f"  {label:<12} {p_str:>14} {i_str:>14} {delta:>10}")
        print()

        if args.diagnose:
            print("⚠️  --diagnose 目前仅支持独立模式，组合模式下已跳过。")

        return

    # 逐个品种跑（独立模式）
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
        print(f"\n📥  {symbol}: {len(df)} 条数据 ({df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}, "
              f"{total_years:.1f}年)")

        _, trades, equity = strategy.run(df, symbol=symbol, verbose=args.verbose)
        all_trades[symbol] = trades
        all_equity[symbol] = equity
        print(f"    → {len(trades)} 笔交易")

    # 输出汇总
    cap_per_sym = strategy.capital_per_symbol
    print_summary(all_trades, date_ranges, equity_curves=all_equity,
                  verbose=args.verbose, capital_per_symbol=cap_per_sym)

    # ── 与成功标准对比 ──
    print("=" * 60)
    print("  📋 实验 S20 成功标准检查")
    print("=" * 60)

    def _get_metric(symbol, key, all_trades, all_equity, date_ranges, cap_per_sym):
        """获取指定品种的指标，使用日频净值（如果可用）。"""
        trades = all_trades[symbol]
        eq = all_equity.get(symbol)
        return compute_metrics(trades, initial_capital=cap_per_sym,
                               total_years=date_ranges.get(symbol, 12.5),
                               daily_equity=eq)[key]

    # 检查：所有品种是否满足标准
    all_ok = True
    for symbol in all_trades:
        trades = all_trades[symbol]
        if not trades:
            continue
        m = _get_metric(symbol, "总盈亏", all_trades, all_equity, date_ranges, cap_per_sym)
        if m <= 0:
            all_ok = False
            print(f"  ❌ {symbol}: 总收益 {m:+.0f} ≤ 0")

    checks = [
        ("总收益 > 0（全部品种）", all_ok, "全部盈利" if all_ok else "有亏损品种"),
        ("总交易笔数 ≥ 20", sum(len(t) for t in all_trades.values()) >= 20,
         f"{sum(len(t) for t in all_trades.values())}"),
        ("平均胜率 > 25%", np.mean([_get_metric(sym, "胜率", all_trades, all_equity, date_ranges, cap_per_sym)
         for sym, t in all_trades.items() if t]) > 0.25,
         f"{np.mean([_get_metric(sym, '胜率', all_trades, all_equity, date_ranges, cap_per_sym) for sym, t in all_trades.items() if t]):.1%}"),
        ("平均最大回撤 < 30%", np.mean([_get_metric(sym, "最大回撤", all_trades, all_equity, date_ranges, cap_per_sym)
         for sym, t in all_trades.items() if t]) < 0.30,
         f"{np.mean([_get_metric(sym, '最大回撤', all_trades, all_equity, date_ranges, cap_per_sym) for sym, t in all_trades.items() if t]):.1%}"),
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
    trade_meta: dict[int, dict] = {}  # id(t) → {symbol, years}
    for symbol, trades in all_trades.items():
        years = date_ranges.get(symbol, 12.5)
        for t in trades:
            reason = t.exit_reason or "未知"
            if reason not in by_reason:
                by_reason[reason] = []
            trade_meta[id(t)] = {"symbol": symbol, "years": years}
            by_reason[reason].append(t)

    # 列头
    header = (f"{'退出原因':<18} {'笔数':>5} {'胜率':>7} {'总盈亏':>10} "
              f"{'平均盈亏':>10} {'持仓(天)':>8}")
    sep = "-" * len(header)
    print(f"\n{header}\n{sep}")

    for reason, trades in sorted(by_reason.items(),
                                  key=lambda kv: sum(t.pnl for t in kv[1]),
                                  reverse=True):
        pnls = np.array([t.pnl for t in trades])
        wins = pnls[pnls > 0]
        total_years = np.mean([trade_meta.get(id(t), {}).get("years", 12.5) for t in trades])
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

    # 1. D点未突破即止损 = "初始止损"（D点突破失败等价分析）
    d_fails = by_reason.get("初始止损", [])
    if d_fails:
        d_pnls = np.array([t.pnl for t in d_fails])
        d_wins = d_pnls[d_pnls > 0]
        print(f"\n  D点未突破即止损: {len(d_fails)}笔, "
              f"胜率 {len(d_wins)/len(d_fails):.1%}, "
              f"总盈亏 {d_pnls.sum():+.0f}, "
              f"平均持仓 {np.mean([max(0,t.exit_idx-t.entry_idx) for t in d_fails]):.0f}天")

    # 2. 止损分析（初始止损 + 跟踪止损）
    stops = by_reason.get("初始止损", []) + by_reason.get("跟踪止损", [])
    if stops:
        s_pnls = np.array([t.pnl for t in stops])
        s_wins = s_pnls[s_pnls > 0]
        s_losses = s_pnls[s_pnls < 0]
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

    # 4. 与海龟对比（参考值，非本次回测结果）
    print(f"\n📊 与海龟系统对比（参考值，非本次回测结果）")
    print(f"  N字结构: avg CAGR ~6%, 胜率~58%, 盈亏比~2.5")
    print(f"  海龟系统: avg CAGR ~15%, 胜率~40%, 盈亏比~4.2")
    print(f"  → N字胜率高但单笔赚得少 → 趋势没吃透")
    print(f"  → 可能原因：(1) D点30天太短 (2) 跟踪止损1.5×ATR太紧 (3) 无再进场机制")
    print()


if __name__ == "__main__":
    main()
