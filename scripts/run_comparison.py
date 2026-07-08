#!/usr/bin/env python
""" 跨市场ETF海龟组合策略 · 四种基准对比 (S5)
从 data/etf_daily/ 读取 Parquet 缓存，依次运行：
    B1 买入等权持有
    B2 等权定期再平衡
    B3 ATR 等风险贡献
    B4 海龟（纯策略，国债逻辑已移除）

输出对比表格到控制台，并保存到 results/comparison/comparison_{date}.csv。

用法：
    py scripts/run_comparison.py
    py scripts/run_comparison.py --start 2023-01-01 --end 2024-12-31
    py scripts/run_comparison.py --mode B # 55日过滤模式
    py scripts/run_comparison.py --save # 保存 CSV
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import backtrader as bt
import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.benchmarks import BuyAndHold, EqualWeightRebalance, ATREqualRisk
from strategies.turtle_trading import TurtleStrategy
from scripts.run_backtest import load_data, df_to_feed, align_to_common_dates
from src.config_loader import (
    get_shortable_symbols,
    get_t_plus_one_symbols,
    get_trading_symbols,
    get_bond_symbol,
    get_all_symbols,
)

logger = logging.getLogger(__name__)

CONFIG_PATH = ROOT / "config" / "turtle_config.yaml"
DATA_DIR = ROOT / "data" / "etf_daily"
# [修复] 统一路径配置，与 gen_report.py 中的 COMPARISON_DIR 保持一致
COMPARISON_DIR = ROOT / "results" / "comparison"

# 从统一配置读取品种列表
import yaml

with open(CONFIG_PATH, "r", encoding="utf-8") as _f:
    _CONFIG = yaml.safe_load(_f)

SIX_SYMBOLS = get_trading_symbols(_CONFIG)
BOND_SYMBOL = get_bond_symbol(_CONFIG)
ALL_SYMBOLS = get_all_symbols(_CONFIG)

# ── 策略配置函数 ──

def _get_strategy(strategy_name: str, config: dict, mode: str) -> type[bt.Strategy]:
    """根据名称返回策略类及参数 dict。"""
    if strategy_name == "B1":
        return BuyAndHold
    elif strategy_name == "B2":
        return EqualWeightRebalance
    elif strategy_name == "B3":
        return ATREqualRisk
    elif strategy_name == "B4":
        return TurtleStrategy
    else:
        raise ValueError(f"未知策略: {strategy_name}")


def _get_strategy_kwargs(strategy_name: str, config: dict, mode: str) -> dict:
    """返回策略的关键字参数。"""
    if strategy_name == "B1":
        return {"symbols": SIX_SYMBOLS}
    elif strategy_name == "B2":
        return {"symbols": SIX_SYMBOLS}
    elif strategy_name == "B3":
        return {
            "symbols": SIX_SYMBOLS,
            "risk_per_unit": config["turtle"]["risk_per_unit"],
            "atr_period": config["turtle"]["atr_period"],
            "atr_change_threshold": config["weighting"]["atr_change_threshold"],
        }
    elif strategy_name == "B4":
        # [修复] 补全 B4 所需的所有参数，虽然当前 run_single 不被 B4 调用，
        # 但保持配置完整性以备后续重构使用
        return {
            "turtle_params": config["turtle"],
            "symbols": SIX_SYMBOLS,
            "use_55_filter": (mode == "B"),
            "risk_per_unit": config["turtle"]["risk_per_unit"],
            "concentration_trigger": config["risk"]["concentration_trigger"],
            "max_consecutive_losses": config["risk"]["max_consecutive_losses"],
            "max_cumulative_loss_pct": config["risk"]["max_cumulative_loss_pct"],
            "pause_days": config["risk"]["pause_days"],
            "max_portfolio_risk": config["risk"]["max_portfolio_risk"],
            "alpha": config["weighting"]["alpha"],
            "cov_lookback_days": config["weighting"]["cov_lookback_days"],
            "rebalance_quarterly": config["weighting"]["rebalance_quarterly"],
            "atr_change_threshold": config["weighting"]["atr_change_threshold"],
            "shortable_symbols": get_shortable_symbols(config),
            "t_plus_one_symbols": get_t_plus_one_symbols(config),
        }
    raise ValueError(f"未知策略: {strategy_name}")


# ── 单策略运行 ──

def run_single(
    strategy_name: str,
    feeds: dict[str, bt.feeds.PandasData],
    config: dict,
    mode: str,
    symbols: Optional[list[str]] = None,
    start_date: str = "2014-01-01",
    end_date: str = "2026-06-10",
) -> Optional[dict]:
    """运行单个基准策略回测。

    Parameters
    ----------
    symbols : list[str] or None
        用于回测的品种列表（B1/B2/B3 过滤晚上市品种）。

    Returns
    -------
    dict or None
        包含各项指标的字典，失败返回 None。
    """
    cerebro = bt.Cerebro()

    # 添加数据（过滤出仅包含指定品种的数据）
    syms = symbols or SIX_SYMBOLS
    for symbol in syms:
        cerebro.adddata(feeds[symbol], name=symbol)

    # 资金与成本
    cerebro.broker.setcash(config["initial_cash"])
    cerebro.broker.setcommission(
        commission=config["commission_pct"] + config["slippage_pct"]
    )

    # 添加策略
    strategy_class = _get_strategy(strategy_name, config, mode)
    strategy_kwargs = _get_strategy_kwargs(strategy_name, config, mode)
    strategy_kwargs["symbols"] = syms
    cerebro.addstrategy(strategy_class, **strategy_kwargs)

    # 分析器
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", timeframe=bt.TimeFrame.Years)
    cerebro.addanalyzer(bt.analyzers.AnnualReturn, _name="annual_return")
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")

    # 运行
    try:
        results = cerebro.run(runonce=False)
    except Exception as e:
        logger.error("[%s] 运行失败: %s", strategy_name, e)
        return None

    if not results:
        logger.error("[%s] 无结果", strategy_name)
        return None

    strat = results[0]

    # 提取指标
    final_value = cerebro.broker.getvalue()
    initial_cash = config["initial_cash"]
    total_return = (final_value / initial_cash - 1) * 100

    # 夏普
    sharpe_ratio = None
    sharpe = strat.analyzers.sharpe.get_analysis()
    if sharpe and "sharperatio" in sharpe:
        sharpe_ratio = sharpe["sharperatio"]

    # 年化收益率（几何 CAGR）
    n_years = (datetime.strptime(end_date, "%Y-%m-%d") - datetime.strptime(start_date, "%Y-%m-%d")).days / 365.25
    annual_ret = ((final_value / initial_cash) ** (1 / max(n_years, 0.1)) - 1) * 100 if initial_cash > 0 else 0.0

    # 最大回撤
    max_dd = None
    dd = strat.analyzers.drawdown.get_analysis()
    if dd and "max" in dd:
        max_dd = dd["max"]["drawdown"]

    # 年化波动率
    annual_vol = None
    rets = strat.analyzers.returns.get_analysis()
    if rets and "rnorm" in rets:
        annual_vol = rets["rnorm"] * 100

    # 交易统计
    total_trades = 0
    win_rate = None
    profit_factor = None

    trades = strat.analyzers.trades.get_analysis()
    if trades:
        total = trades.get("total", {})
        total_trades = total.get("total", 0)
        won = trades.get("won", {}).get("total", 0)
        lost = trades.get("lost", {}).get("total", 0)

        if total_trades > 0:
            win_rate = won / total_trades * 100

        # [修复] 计算 Profit Factor (总盈利 / 总亏损)
        won_stats = trades.get("won", {}).get("pnl", {})
        lost_stats = trades.get("lost", {}).get("pnl", {})
        total_gross_profit = abs(won_stats.get("total", 0))
        total_gross_loss = abs(lost_stats.get("total", 0))

        if total_gross_loss > 0:
            profit_factor = total_gross_profit / total_gross_loss

    return {
        "strategy": strategy_name,
        "initial_cash": initial_cash,
        "final_value": round(final_value, 2),
        "total_return_pct": round(total_return, 2),
        "annual_return_pct": round(annual_ret, 2) if annual_ret else None,
        "sharpe_ratio": round(sharpe_ratio, 4) if sharpe_ratio else None,
        "max_drawdown_pct": round(max_dd, 2) if max_dd else None,
        "annual_volatility_pct": round(annual_vol, 2) if annual_vol else None,
        "total_trades": total_trades,
        "win_rate_pct": round(win_rate, 2) if win_rate else None,
        "profit_factor": round(profit_factor, 2) if profit_factor else None,
    }


# ── 对比主函数 ──

def run_comparison(
    start_date: str = "2014-01-01",
    end_date: str = "2026-06-10",
    mode: str = "A",
    save_csv: bool = False,
) -> Optional[pd.DataFrame]:
    """运行四种基准策略对比。

    Parameters
    ----------
    start_date : str
        回测起始日期。
    end_date : str
        回测截止日期。
    mode : str
        "A" = 无55日过滤, "B" = 55日过滤。
    save_csv : bool
        是否保存结果为 CSV。

    Returns
    -------
    pd.DataFrame or None
        包含 4 行对比结果的 DataFrame。
    """
    # 加载配置
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 加载所有数据
    feeds: dict[str, bt.feeds.PandasData] = {}
    all_dfs = {}
    for symbol in ALL_SYMBOLS:
        df = load_data(symbol, start_date, end_date, data_dir=DATA_DIR)
        if df is None:
            logger.error("品种 %s 数据加载失败，终止对比", symbol)
            return None
        all_dfs[symbol] = df

    # 所有品种对齐到公共日期，消除多品种信号数组长度差异
    all_dfs = align_to_common_dates(all_dfs)

    for symbol in ALL_SYMBOLS:
        feed = df_to_feed(all_dfs[symbol], symbol)
        feed._name = symbol
        feeds[symbol] = feed

    # [修复] B1/B2/B3 基准策略在第一天买入，只包含上市日期前的长期品种
    # 过滤出在第一个公共日期有有效 close 的品种
    first_date = all_dfs[ALL_SYMBOLS[0]]["date"].iloc[0]
    active_symbols = [
        s for s in SIX_SYMBOLS
        if not pd.isna(all_dfs[s].iloc[0]["close"])
    ]
    if len(active_symbols) < len(SIX_SYMBOLS):
        logger.info("基准策略(B1/B2/B3)使用 %d/7 个上市品种: %s",
                     len(active_symbols), active_symbols)

    # 依次运行 4 个策略
    strategy_names = ["B1", "B2", "B3", "B4"]
    strategy_labels = {
        "B1": "B1 买入等权持有",
        "B2": "B2 等权再平衡",
        "B3": "B3 ATR等风险",
        "B4": "B4 海龟(纯策略)",
    }

    results = []

    for name in strategy_names:
        logger.info("=" * 50)
        logger.info("运行 %s %s", name, strategy_labels[name])
        logger.info("=" * 50)

        if name == "B4":
            # B4 策略手动运行块，为了保留详细的分品种/年度打印输出
            _cerebro4 = bt.Cerebro()
            for _s in SIX_SYMBOLS:
                _cerebro4.adddata(feeds[_s], name=_s)

            _cerebro4.broker.setcash(config["initial_cash"])
            _cerebro4.broker.setcommission(
                commission=config["commission_pct"] + config["slippage_pct"]
            )
            _cerebro4.addstrategy(
                TurtleStrategy,
                turtle_params=config["turtle"],
                symbols=SIX_SYMBOLS,
                use_55_filter=(mode == "B"),
                risk_per_unit=config["turtle"]["risk_per_unit"],
                concentration_trigger=config["risk"]["concentration_trigger"],
                max_consecutive_losses=config["risk"]["max_consecutive_losses"],
                max_cumulative_loss_pct=config["risk"]["max_cumulative_loss_pct"],
                pause_days=config["risk"]["pause_days"],
                max_portfolio_risk=config["risk"]["max_portfolio_risk"],
                alpha=config["weighting"]["alpha"],
                cov_lookback_days=config["weighting"]["cov_lookback_days"],
                rebalance_quarterly=config["weighting"]["rebalance_quarterly"],
                atr_change_threshold=config["weighting"]["atr_change_threshold"],
                shortable_symbols=get_shortable_symbols(config),
                t_plus_one_symbols=get_t_plus_one_symbols(config),
            )

            _cerebro4.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", timeframe=bt.TimeFrame.Years)
            _cerebro4.addanalyzer(bt.analyzers.AnnualReturn, _name="annual_return")
            _cerebro4.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
            _cerebro4.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
            _cerebro4.addanalyzer(bt.analyzers.Returns, _name="returns")

            try:
                _res4 = _cerebro4.run()
            except Exception as _e4:
                logger.error("[B4] 运行失败: %s", _e4)
                continue

            if not _res4:
                logger.error("[B4] 无结果")
                continue

            _strat4 = _res4[0]
            fv = _cerebro4.broker.getvalue()

            _trades4 = _strat4.analyzers.trades.get_analysis() or {}
            _total4 = _trades4.get("total", {}).get("total", 0)
            _won4 = _trades4.get("won", {}).get("total", 0)

            # 提取 B4 分析器指标（与其他策略一致）
            _sharpe4 = _strat4.analyzers.sharpe.get_analysis()
            _sharpe_ratio4 = _sharpe4.get("sharperatio") if _sharpe4 else None

            # 年化收益率（几何 CAGR）
            _n_years4 = (datetime.strptime(end_date, "%Y-%m-%d") - datetime.strptime(start_date, "%Y-%m-%d")).days / 365.25
            _annual_ret4 = ((fv / config["initial_cash"]) ** (1 / max(_n_years4, 0.1)) - 1) * 100

            # 保留年度回报分析器（用于分年度打印）
            _ann4 = _strat4.analyzers.annual_return.get_analysis()

            _dd4 = _strat4.analyzers.drawdown.get_analysis()
            _max_dd4 = _dd4["max"]["drawdown"] if _dd4 and "max" in _dd4 else None

            _rets4 = _strat4.analyzers.returns.get_analysis()
            _vol4 = _rets4.get("rnorm") * 100 if _rets4 and "rnorm" in _rets4 else None

            # [修复] 计算 B4 的 Profit Factor (总盈利 / 总亏损)
            _won_stats = _trades4.get("won", {}).get("pnl", {})
            _lost_stats = _trades4.get("lost", {}).get("pnl", {})
            _gp = abs(_won_stats.get("total", 0))
            _gl = abs(_lost_stats.get("total", 0))
            _pf = (_gp / _gl) if _gl > 0 else 0.0

            result = {
                "strategy": "B4",
                "initial_cash": config["initial_cash"],
                "final_value": round(fv, 2),
                "total_return_pct": round((fv / config["initial_cash"] - 1) * 100, 2),
                "annual_return_pct": round(_annual_ret4, 2) if _annual_ret4 else None,
                "sharpe_ratio": round(_sharpe_ratio4, 4) if _sharpe_ratio4 else None,
                "max_drawdown_pct": round(_max_dd4, 2) if _max_dd4 else None,
                "annual_volatility_pct": round(_vol4, 2) if _vol4 else None,
                "total_trades": _total4,
                "win_rate_pct": round(_won4 / _total4 * 100, 2) if _total4 > 0 else 0,
                "profit_factor": round(_pf, 2),
            }
            results.append(result)

            print(f"B4 B4 海龟(纯策略): 最终 {fv:>10.2f} | "
                  f"收益 {result['total_return_pct']:>7.2f}% | "
                  f"夏普 {result['sharpe_ratio'] or 'N/A':>8} | "
                  f"回撤 {result['max_drawdown_pct'] or 'N/A':>6}% | "
                  f"交易 {_total4:>4}次")

            # ── B4 分品种和分年度表格（从 _my_trades 聚合）──
            _my_trades4 = getattr(_strat4, "_my_trades", [])
            if _my_trades4:
                # 分品种表
                _sym_stats: dict[str, dict] = {}
                for _t in _my_trades4:
                    _s = _t["symbol"]
                    if _s not in _sym_stats:
                        _sym_stats[_s] = {"total": 0, "won": 0, "pnl": 0.0}
                    _sym_stats[_s]["total"] += 1
                    if _t["was_win"]:
                        _sym_stats[_s]["won"] += 1
                    _sym_stats[_s]["pnl"] += _t["pnl"]

                print()
                print(" B4 分品种交易统计")
                print(f" {'品种':<14} {'交易次数':>8} {'盈利次数':>8} {'胜率':>7} {'总盈亏':>12}")
                print(" " + "-" * 55)
                for _s in sorted(_sym_stats):
                    _st = _sym_stats[_s]
                    _wr = _st["won"] / _st["total"] * 100 if _st["total"] > 0 else 0
                    print(f" {_s:<14} {_st['total']:>8} {_st['won']:>8} {_wr:>6.1f}% {_st['pnl']:>+10.0f}")

                _T = sum(v["total"] for v in _sym_stats.values())
                _W = sum(v["won"] for v in _sym_stats.values())
                _P = sum(v["pnl"] for v in _sym_stats.values())
                print(f" {'合计':<14} {_T:>8} {_W:>8} {(_W/_T*100 if _T else 0):>6.1f}% {_P:>+10.0f}")

                # 分年度表
                _yr_stats: dict[str, dict] = {}
                for _t in _my_trades4:
                    _y = str(_t["exit_date"])[:4]
                    if _y not in _yr_stats:
                        _yr_stats[_y] = {"total": 0, "won": 0, "pnl": 0.0}
                    _yr_stats[_y]["total"] += 1
                    if _t["was_win"]:
                        _yr_stats[_y]["won"] += 1
                    _yr_stats[_y]["pnl"] += _t["pnl"]

                print()
                print(" B4 分年度交易统计")
                print(f" {'年份':<8} {'交易次数':>8} {'盈利次数':>8} {'胜率':>7} {'总盈亏':>12} {'年化收益':>9}")
                print(" " + "-" * 59)
                for _y in sorted(_yr_stats):
                    _yt = _yr_stats[_y]
                    _wr = _yt["won"] / _yt["total"] * 100 if _yt["total"] > 0 else 0
                    _yr_ret = _ann4.get(int(_y)) if _ann4 and int(_y) in _ann4 else None
                    _yr_str = f"{_yr_ret*100:>+7.2f}%" if _yr_ret is not None else " N/A "
                    print(f" {_y:<8} {_yt['total']:>8} {_yt['won']:>8} {_wr:>6.1f}% {_yt['pnl']:>+10.0f} {_yr_str:>9}")
                print()
            continue

        # 基准策略只包含上市初期的品种，避免操作 NaN 数据
        _bench_syms = active_symbols if name in ("B1", "B2", "B3") else None
        result = run_single(name, feeds, config, mode, symbols=_bench_syms, start_date=start_date, end_date=end_date)
        if result is None:
            logger.error("[%s] 失败，跳过", name)
            continue

        results.append(result)

        # 打印简要结果
        r = result
        print(f"{r['strategy']} {strategy_labels[r['strategy']]}: "
              f"最终 {r['final_value']:>10.2f} | "
              f"收益 {r['total_return_pct']:>7.2f}% | "
              f"夏普 {r['sharpe_ratio'] or 'N/A':>8} | "
              f"回撤 {r['max_drawdown_pct'] or 'N/A':>6}% | "
              f"交易 {r['total_trades']:>4}次")

    if not results:
        logger.error("所有策略均运行失败")
        return None

    # 组合结果
    df = pd.DataFrame(results)
    df.set_index("strategy", inplace=True)

    # 输出表格
    print()
    print("=" * 90)
    print("四种基准策略对比结果")
    print("=" * 90)
    display_cols = [
        "total_return_pct",
        "annual_return_pct",
        "sharpe_ratio",
        "max_drawdown_pct",
        "annual_volatility_pct",
        "win_rate_pct",
        "profit_factor",
        "total_trades",
        "final_value",
    ]
    display_df = df[display_cols].copy()
    display_df.columns = [
        "总收益率%",
        "年化收益%",
        "夏普",
        "最大回撤%",
        "年化波动%",
        "胜率%",
        "盈亏比",
        "交易次数",
        "最终净值",
    ]

    # 格式化
    pd.set_option("display.max_columns", 10)
    pd.set_option("display.width", 120)
    pd.set_option("display.precision", 2)
    print(display_df.to_string())
    print("=" * 90)

    # 保存 CSV
    if save_csv:
        COMPARISON_DIR.mkdir(parents=True, exist_ok=True)
        csv_path = COMPARISON_DIR / f"comparison_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        display_df.to_csv(csv_path, encoding="utf-8-sig")
        logger.info("对比结果已保存到 %s", csv_path)

    return df


# ════════════════════════════════════════════════════════════
# CLI 入口
# ════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="跨市场ETF海龟组合策略 — 四种基准对比",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--start", type=str, default="2014-01-01", help="回测起始日期 (默认: 2014-01-01)")
    parser.add_argument("--end", type=str, default="2026-06-10", help="回测截止日期 (默认: 2026-06-10)")
    parser.add_argument("--mode", "-m", type=str, choices=["A", "B"], default="A", help="模式 A=无55日过滤(默认), B=55日过滤")
    parser.add_argument("--save", action="store_true", default=False, help="保存对比结果 CSV")
    parser.add_argument("--verbose", "-v", action="store_true", default=False, help="详细日志输出")

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    result = run_comparison(
        start_date=args.start,
        end_date=args.end,
        mode=args.mode,
        save_csv=args.save,
    )

    if result is None:
        sys.exit(1)

if __name__ == "__main__":
    main()
