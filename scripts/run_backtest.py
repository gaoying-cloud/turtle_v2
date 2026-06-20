#!/usr/bin/env python
"""
跨市场ETF海龟组合策略 · 回测入口 (S3)

从 data/etf_daily/ 读取 Parquet 缓存，加载到 Backtrader 运行回测。

用法：
    py scripts/run_backtest.py                     # 模式 A（无过滤）
    py scripts/run_backtest.py --mode B             # 模式 B（55日过滤）
    py scripts/run_backtest.py --start 2023-01-01   # 指定日期
    py scripts/run_backtest.py --end 2024-12-31     # 指定截止日期
    py scripts/run_backtest.py --plot               # 绘图
    py scripts/run_backtest.py --verbose            # 详细日志
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import backtrader as bt
import pandas as pd
import yaml

# 确保 src/ 和 strategies/ 在 sys.path 中
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.turtle_core import TurtleSignals
from strategies.turtle_trading import TurtleStrategy
from src.config_loader import get_shortable_symbols, get_t_plus_one_symbols, get_trading_symbols, get_bond_symbol, get_all_symbols, get_t0_symbols

logger = logging.getLogger(__name__)

# ── 配置 ──
CONFIG_PATH = ROOT / "config" / "turtle_config.yaml"
DATA_DIR = ROOT / "data" / "etf_daily"

# 从统一配置读取品种列表（消除硬编码）
with open(CONFIG_PATH, "r", encoding="utf-8") as _f:
    _CONFIG = yaml.safe_load(_f)
SIX_SYMBOLS = get_trading_symbols(_CONFIG)
BOND_SYMBOL = get_bond_symbol(_CONFIG)
ALL_SYMBOLS = get_all_symbols(_CONFIG)
T0_SYMBOLS = get_t0_symbols(_CONFIG)

# 期货品种（12 个，跨市场不相关）
FUTURES_SYMBOLS = [
    "CU.SHF",   # 沪铜
    "RB.SHF",   # 螺纹钢
    "RU.SHF",   # 橡胶
    "M.DCE",    # 豆粕
    "Y.DCE",    # 豆油
    "P.DCE",    # 棕榈油
    "JM.DCE",   # 焦煤
    "CF.ZCE",   # 棉花（郑商所主连后缀 .ZCE）
    "SR.ZCE",   # 白糖
    "TA.ZCE",   # PTA
    "I.DCE",    # 铁矿石
    "SC.INE",   # 原油
]

FUTURES_DATA_DIR = ROOT / "data" / "futures_daily"


# ════════════════════════════════════════════════════════════
#  数据加载
# ════════════════════════════════════════════════════════════

def load_data(
    symbol: str,
    start_date: str,
    end_date: str,
    data_dir: Path = DATA_DIR,
) -> Optional[pd.DataFrame]:
    """从 Parquet 缓存加载单个品种的数据。

    Parameters
    ----------
    symbol : str
        品种代码。
    start_date : str
        起始日期 "YYYY-MM-DD"。
    end_date : str
        截止日期 "YYYY-MM-DD"。
    data_dir : Path
        数据目录，默认 ETF 数据。

    Returns
    -------
    pd.DataFrame or None
        数据帧，包含 date, open, high, low, close, volume, amount。
        缓存不存在或未覆盖请求区间时返回 None。
    """
    is_futures = data_dir == FUTURES_DATA_DIR
    pull_script = "py scripts/pull_futures.py" if is_futures else "py scripts/pull_data.py"
    path = data_dir / f"{symbol}.parquet"
    if not path.exists():
        logger.error("缓存文件不存在: %s\n请先运行 %s", path, pull_script)
        return None

    df = pd.read_parquet(path)
    if df.empty:
        logger.warning("[%s] 缓存为空", symbol)
        return None

    # 裁剪日期区间
    mask = (df["date"] >= start_date) & (df["date"] <= end_date)
    df = df[mask].copy()
    if df.empty:
        logger.warning("[%s] 在 %s~%s 区间无数据", symbol, start_date, end_date)
        return None

    # 确保按日期升序
    df.sort_values("date", inplace=True)
    df.reset_index(drop=True, inplace=True)

    logger.info("[%s] 加载 %d 行: %s ~ %s",
                symbol, len(df), df["date"].iloc[0].date(), df["date"].iloc[-1].date())
    return df


def df_to_feed(df: pd.DataFrame, symbol: str) -> bt.feeds.PandasData:
    """将 pandas DataFrame 转换为 Backtrader PandasData feed。

    字段映射：
        date      → datetime（索引）
        open      → open
        high      → high
        low       → low
        close     → close
        volume    → volume
    """
    feed_df = df[["date", "open", "high", "low", "close", "volume"]].copy()
    feed_df["date"] = pd.to_datetime(feed_df["date"])
    feed_df.set_index("date", inplace=True)

    return bt.feeds.PandasData(
        dataname=feed_df,
        open="open",
        high="high",
        low="low",
        close="close",
        volume="volume",
        plot=False,
    )


# ════════════════════════════════════════════════════════════
#  回测运行
# ════════════════════════════════════════════════════════════

def run_backtest(
    *,
    start_date: str = "2020-01-01",
    end_date: str = "2026-06-10",
    mode: str = "A",
    plot: bool = False,
    verbose: bool = False,
    t0_only: bool = False,
    futures: bool = False,
    quiet: bool = False,
    min_confirmations: int = 0,
    vol_threshold: float = 1.5,
    kline_min_body: float = 0.4,
    p2_loss_ratio: float = 0.75,
    use_signal_filter: bool = True,
    entry_mode: str = "breakout",
    stop_buffer_n: float = 1.0,
    p2_mode: str = "none",
    p2_batting_window: int = 4,
) -> Optional[dict]:
    """运行海龟策略回测。

    Parameters
    ----------
    start_date : str
        回测起始日期。
    end_date : str
        回测截止日期。
    mode : str
        "A" = 无55日过滤, "B" = 55日过滤。
    plot : bool
        是否绘制图表。
    verbose : bool
        是否输出 DEBUG 级别日志。
    t0_only : bool
        仅使用 T+0 ETF 品种。
    futures : bool
        使用期货品种（覆盖 t0_only）。

    Returns
    -------
    dict or None
        TurtleStrategy._trade_summary（交易统计），失败返回 None。
    """
    # ── 加载配置 ──
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # ── 选择品种列表与数据目录 ──
    if futures:
        trading_symbols = list(FUTURES_SYMBOLS)
        data_dir = FUTURES_DATA_DIR
        # 期货不需要国债ETF
        all_symbols = list(trading_symbols)
        use_bond = False
        # 期货使用专门的资金参数
        initial_cash = config.get("futures", {}).get("initial_cash", 1000000)
    else:
        trading_symbols = T0_SYMBOLS if t0_only else SIX_SYMBOLS
        data_dir = DATA_DIR
        all_symbols = trading_symbols + [BOND_SYMBOL]
        use_bond = True
        initial_cash = config["initial_cash"]

    # ── 加载所有品种的数据 ──
    feeds: dict[str, bt.feeds.PandasData] = {}
    for symbol in all_symbols:
        df = load_data(symbol, start_date, end_date, data_dir=data_dir)
        if df is None:
            logger.error("品种 %s 数据加载失败，终止回测", symbol)
            return None
        feed = df_to_feed(df, symbol)
        feed._name = symbol  # 设置名称，供策略中识别
        feeds[symbol] = feed

    # ── 设置 Cerebro ──
    cerebro = bt.Cerebro()

    # 添加数据
    for symbol in trading_symbols:
        cerebro.adddata(feeds[symbol], name=symbol)
    # 国债ETF 仅在 ETF 模式下添加
    if use_bond:
        cerebro.adddata(feeds[BOND_SYMBOL], name=BOND_SYMBOL)

    # 资金与成本
    cerebro.broker.setcash(initial_cash)
    commission = config["commission_pct"]
    slippage = config["slippage_pct"] if not futures else 0.0005
    # 滑点通过 commission 模拟（单边）
    cerebro.broker.setcommission(commission=commission + slippage)

    # ── 品种属性 ──
    if futures:
        # 期货全部可做空，无 T+1 约束
        shortable = set(trading_symbols)
        t_plus_one = set()
    else:
        shortable = get_shortable_symbols(config)
        t_plus_one = get_t_plus_one_symbols(config)

    # ── 期货合约乘数 ──
    FUTURES_MULTIPLIERS = {
        "CU.SHF": 5, "RB.SHF": 10, "RU.SHF": 10,
        "M.DCE": 10, "Y.DCE": 10, "P.DCE": 10,
        "JM.DCE": 60, "I.DCE": 100,
        "CF.ZCE": 5, "SR.ZCE": 10, "TA.ZCE": 5,
        "SC.INE": 1000,
    }

    # ── 添加策略（含 S4 风险平价权重参数） ──
    cerebro.addstrategy(
        TurtleStrategy,
        turtle_params=config["turtle"],
        symbols=trading_symbols,  # 不含国债（国债是现金管理工具）
        use_55_filter=(mode == "B"),
        risk_per_unit=config["turtle"]["risk_per_unit"],
        concentration_trigger=config["risk"]["concentration_trigger"],
        max_consecutive_losses=config["risk"]["max_consecutive_losses"],
        max_cumulative_loss_pct=config["risk"]["max_cumulative_loss_pct"],
        pause_days=config["risk"]["pause_days"],
        single_max_risk=config["risk"]["single_max_risk"],
        max_portfolio_risk=config["risk"]["max_portfolio_risk"],
        alpha=config["weighting"]["alpha"],
        cov_lookback_days=config["weighting"]["cov_lookback_days"],
        rebalance_quarterly=config["weighting"]["rebalance_quarterly"],
        atr_change_threshold=config["weighting"]["atr_change_threshold"],
        shortable_symbols=shortable,
        t_plus_one_symbols=t_plus_one,
        futures_mode=futures,
        multipliers=FUTURES_MULTIPLIERS if futures else {},
        min_unit=1 if futures else 100,
        min_confirmations=min_confirmations,
        vol_threshold=vol_threshold,
        kline_min_body=kline_min_body,
        p2_loss_ratio=p2_loss_ratio,
        use_signal_filter=use_signal_filter,
        entry_mode=entry_mode,
        stop_buffer_n=stop_buffer_n,
        p2_mode=p2_mode,
        p2_batting_window=p2_batting_window,
    )

    # ── 添加分析器 ──
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", timeframe=bt.TimeFrame.Years)
    cerebro.addanalyzer(bt.analyzers.AnnualReturn, _name="annual_return")
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")

    # ── 运行 ──
    mode_label = "期货双向" if futures else f"ETF模式 {'T+0' if t0_only else '全品种'}"
    symbol_str = ", ".join(trading_symbols)
    logger.info("=" * 50)
    logger.info("开始回测 | %s | %s ~ %s | 初始资金 %.0f",
                mode_label, start_date, end_date, initial_cash)
    logger.info("品种: %s", symbol_str)
    logger.info("=" * 50)

    results = cerebro.run()
    if not results:
        logger.error("回测未返回结果")
        return None

    strat = results[0]

    if not quiet:
        # ── 输出分析结果 ──
        print()
        print("=" * 60)
        print("回测结果汇总")
        print("=" * 60)

        final_value = cerebro.broker.getvalue()
        print(f"初始资金: {initial_cash:>10.2f}")
        print(f"最终净值: {final_value:>10.2f}")
        print(f"总收益率: {(final_value / initial_cash - 1) * 100:>9.2f}%")

        # 夏普比率
        sharpe = strat.analyzers.sharpe.get_analysis()
        if sharpe and "sharperatio" in sharpe:
            sr = sharpe["sharperatio"]
            print(f"夏普比率: {sr:>14.4f}" if sr else "夏普比率: N/A")

        # 最大回撤
        dd = strat.analyzers.drawdown.get_analysis()
        if dd and "max" in dd:
            print(f"最大回撤: {dd['max']['drawdown']:>12.2f}%")
            print(f"最长回撤期: {dd['max']['len']:>11d} 天")

        # 交易统计
        trades = strat.analyzers.trades.get_analysis()
        if trades:
            total = trades.get("total", {}).get("total", 0)
            won = trades.get("won", {}).get("total", 0)
            lost = trades.get("lost", {}).get("total", 0)
            win_rate = won / total * 100 if total > 0 else 0
            print(f"交易次数: {total:>15d}")
            print(f"盈利/亏损: {won}/{lost}")
            print(f"胜率: {win_rate:>19.2f}%")

            # 盈亏比
            avg_win = trades.get("won", {}).get("pnl", {}).get("average", 0)
            avg_loss = abs(trades.get("lost", {}).get("pnl", {}).get("average", 0))
            if avg_loss > 0:
                profit_factor = avg_win / avg_loss
                print(f"盈亏比: {profit_factor:>16.2f}")
            print(f"平均盈利: {avg_win:>14.2f}")
            print(f"平均亏损: -{avg_loss:>13.2f}")
            _hold = trades.get("len", {}).get("average", 0)
            print(f"平均持仓: {_hold:>13.1f} 天")

        print("=" * 60)
        print()

    # ── 绘图（可选） ──
    if plot:
        cerebro.plot(style="candlestick", volume=True)

    # ── 提取分析器数据 ──
    final_value = cerebro.broker.getvalue()
    sharpe = strat.analyzers.sharpe.get_analysis()
    dd = strat.analyzers.drawdown.get_analysis()
    trades = strat.analyzers.trades.get_analysis()

    total_return_pct = (final_value / initial_cash - 1) * 100
    sharpe_ratio = sharpe.get("sharperatio") if sharpe else None
    max_drawdown = dd["max"]["drawdown"] if dd and "max" in dd else None
    max_dd_duration = dd["max"]["len"] if dd and "max" in dd else None

    # 交易统计
    total_trades = trades.get("total", {}).get("total", 0) if trades else 0
    won = trades.get("won", {}).get("total", 0) if trades else 0
    lost = trades.get("lost", {}).get("total", 0) if trades else 0
    win_rate = won / total_trades * 100 if total_trades > 0 else 0
    avg_win = trades.get("won", {}).get("pnl", {}).get("average", 0) if trades else 0
    avg_loss = abs(trades.get("lost", {}).get("pnl", {}).get("average", 0)) if trades else 0
    avg_hold_all = trades.get("len", {}).get("average", 0) if trades else 0
    profit_factor = avg_win / avg_loss if avg_loss > 0 else float("inf")

    trade_summary = getattr(strat, "_trade_summary", None)

    return {
        "trade_summary": trade_summary,
        "final_value": final_value,
        "initial_cash": initial_cash,
        "total_return_pct": round(total_return_pct, 2),
        "sharpe": round(sharpe_ratio, 4) if sharpe_ratio is not None else None,
        "max_drawdown": round(max_drawdown, 2) if max_drawdown is not None else None,
        "max_dd_duration": max_dd_duration,
        "total_trades": total_trades,
        "won": won,
        "lost": lost,
        "win_rate": round(win_rate, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "avg_hold_all": round(avg_hold_all, 1),
        "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else None,
    }


# ════════════════════════════════════════════════════════════
#  逐年度量
# ════════════════════════════════════════════════════════════

def run_yearly_breakdown(
    *,
    start_date: str = "2020-01-01",
    end_date: str = "2026-06-10",
    mode: str = "A",
    t0_only: bool = False,
    futures: bool = False,
    entry_mode: str = "breakout",
) -> None:
    """按年独立回测，输出各年度指标对比表。

    每年用独立 Cerebro 实例，资金重置为初始值，
    以保证各年绩效不受前一年盈亏影响。
    """
    start_year = int(start_date[:4])
    end_year = int(end_date[:4])

    rows = []

    print()
    print("=" * 120)
    print("各年度绩效对比")
    print("=" * 120)

    for y in range(start_year, end_year + 1):
        y_start = f"{y}-01-01"
        y_end = f"{y}-12-31"
        if y == start_year:
            y_start = start_date
        if y == end_year:
            y_end = end_date

        logger.info("按年回测: %s ~ %s", y_start, y_end)
        result = run_backtest(
            start_date=y_start,
            end_date=y_end,
            mode=mode,
            t0_only=t0_only,
            futures=futures,
            quiet=True,
            entry_mode=entry_mode,
        )
        if result is None:
            # 数据不足，跳过
            continue

        rows.append({"year": str(y), **result})

    if not rows:
        print("无可用数据")
        print("=" * 120)
        return

    # ── 打印表格 ──
    headers = ["年份", "年化收益", "夏普", "最大回撤", "回撤天数", "总交易", "胜率",
               "盈亏比", "平均持仓", "平均盈利", "平均亏损"]
    col_w = [6, 10, 8, 10, 10, 8, 6, 8, 10, 10, 10]
    sep = "|" + "|".join("-" * w for w in col_w) + "|"
    fmt = "| {:^6} | {:>8}% | {:>6} | {:>8}% | {:>8} | {:>6} | {:>4}% | {:>6} | {:>8} | {:>8} | {:>8} |"

    print(fmt.format(*headers))
    print(sep)

    total_ret = 1.0
    for d in rows:
        yr_label = d["year"]
        sharpe_str = f"{d['sharpe']:.2f}" if d["sharpe"] is not None else "N/A"
        dd_str = f"{d['max_drawdown']:.2f}" if d["max_drawdown"] is not None else "N/A"
        dd_len = d.get("max_dd_duration")
        dd_len_str = f"{dd_len}天" if dd_len is not None else "—"
        pf_str = f"{d['profit_factor']:.2f}" if d["profit_factor"] is not None else "N/A"
        hold_str = f"{d['avg_hold_all']:.0f}" if d.get("avg_hold_all") is not None else "—"
        avg_win_str = f"{d['avg_win']:.0f}" if d["avg_win"] else "—"
        avg_loss_str = f"{d['avg_loss']:.0f}" if d["avg_loss"] else "—"

        print(fmt.format(
            yr_label,
            d["total_return_pct"],
            sharpe_str,
            dd_str,
            dd_len_str,
            d["total_trades"],
            f"{d['win_rate']:.0f}",
            pf_str,
            hold_str,
            avg_win_str,
            avg_loss_str,
        ))

    print(sep)

    # ── 汇总行 ──
    total_trades = sum(d["total_trades"] for d in rows)
    # 总收益为各年复利乘积
    composite_ret = 1.0
    for d in rows:
        composite_ret *= (1 + d["total_return_pct"] / 100)
    total_ret_pct = (composite_ret - 1) * 100
    avg_sharpe = (
        sum(d["sharpe"] for d in rows if d["sharpe"] is not None)
        / sum(1 for d in rows if d["sharpe"] is not None)
        if any(d["sharpe"] is not None for d in rows)
        else None
    )
    avg_win_rate = (
        sum(d["win_rate"] for d in rows) / len(rows)
    )
    sharpe_str = f"{avg_sharpe:.2f}" if avg_sharpe is not None else "N/A"
    # 最大回撤天数为各年最大值
    max_dd_all = max((d["max_dd_duration"] for d in rows if d.get("max_dd_duration") is not None), default=None)
    max_dd_summary = f"{max_dd_all}天" if max_dd_all else "—"
    # 平均持仓为各年按交易数加权
    total_hold = sum(d.get("avg_hold_all", 0) * d["total_trades"] for d in rows)
    avg_hold_summary = f"{round(total_hold / total_trades, 1)}" if total_trades > 0 else "—"
    print(f"| 合计     | {total_ret_pct:>8.2f}% | {sharpe_str:>6} |          | {max_dd_summary:>8} | {total_trades:>6} | {avg_win_rate:>4.0f}% |        | {avg_hold_summary:>8} |          |          |")
    print("=" * 120)
    print()


# ════════════════════════════════════════════════════════════
#  CLI 入口
# ════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="跨市场ETF海龟组合策略 — 回测入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode", "-m",
        type=str,
        choices=["A", "B"],
        default="A",
        help="模式 A=无55日过滤(默认), B=55日过滤",
    )
    parser.add_argument(
        "--t0-only",
        action="store_true",
        default=False,
        help="仅使用 T+0 品种（纳指+黄金）运行双向回测，验证做空信号净收益",
    )
    parser.add_argument(
        "--futures",
        action="store_true",
        default=False,
        help="使用期货品种（12个跨市场）运行双向回测",
    )
    parser.add_argument(
        "--start",
        type=str,
        default="2020-01-01",
        help="回测起始日期 YYYY-MM-DD (默认: 2020-01-01)",
    )
    parser.add_argument(
        "--end",
        type=str,
        default="2026-06-10",
        help="回测截止日期 YYYY-MM-DD (默认: 2026-06-10)",
    )
    parser.add_argument(
        "--plot", "-p",
        action="store_true",
        default=False,
        help="绘制回测图表",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="详细日志输出 (DEBUG 级别)",
    )
    parser.add_argument(
        "--yearly",
        action="store_true",
        default=False,
        help="按年拆分回测，输出各年度指标对比表",
    )
    parser.add_argument(
        "--entry-mode",
        type=str,
        choices=["breakout", "dual"],
        default="breakout",
        help="入场模式: breakout=20日高点突破(默认), dual=突破+MA5金叉双模式",
    )

    args = parser.parse_args()

    # 日志级别
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # 按年拆分模式
    if args.yearly:
        run_yearly_breakdown(
            start_date=args.start,
            end_date=args.end,
            mode=args.mode,
            t0_only=args.t0_only,
            futures=args.futures,
            entry_mode=args.entry_mode,
        )
        return

    # 运行回测
    result = run_backtest(
        start_date=args.start,
        end_date=args.end,
        mode=args.mode,
        plot=args.plot,
        verbose=args.verbose,
        t0_only=args.t0_only,
        futures=args.futures,
        entry_mode=args.entry_mode,
    )

    if result is None:
        sys.exit(1)


if __name__ == "__main__":
    main()