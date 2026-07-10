#!/usr/bin/env python
"""
海龟系统 -> 导出每日净值曲线

用法：
    py scripts/export_turtle_equity.py                          # 默认
    py scripts/export_turtle_equity.py --output results/turtle_equity.csv
    py scripts/export_turtle_equity.py --start 2015-01-01 --end 2026-07-09
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import backtrader as bt
import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.data_utils import load_data, align_to_common_dates
from strategies.turtle_trading import TurtleStrategy

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

DATA_DIR = ROOT / "data" / "etf_daily"
CONFIG_PATH = ROOT / "config" / "turtle_config.yaml"
DEFAULT_SYMBOLS = ["510500.SH", "159915.SZ", "513100.SH", "518880.SH", "159985.SZ", "513520.SH"]


class DailyEquityAnalyzer(bt.Analyzer):
    """记录每日组合净值。"""

    def start(self):
        self.equity = []
        self.dates = []

    def next(self):
        self.equity.append(self.strategy.broker.getvalue())
        # 取第一个数据源的日期
        dt = self.strategy.datas[0].datetime.date(0)
        self.dates.append(dt.isoformat())


def export_equity(
    symbols: list[str] = None,
    start_date: str = "2015-01-01",
    end_date: str = "2026-07-09",
    output_path: str | None = None,
    capital: float = 600000,  # 等权分配给 6 个品种
) -> pd.DataFrame:
    """运行海龟回测，导出每日净值。"""
    if symbols is None:
        symbols = DEFAULT_SYMBOLS

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    t_config = config.get("turtle", {})
    commission = config.get("commission_pct", 0.00015)
    slippage = config.get("slippage_pct", 0.001)

    # ── 加载数据（仅品种，不含国债） ──
    etf_data = {}
    for sym in symbols:
        df = load_data(sym, start_date, end_date, DATA_DIR)
        if df is not None and not df.empty:
            etf_data[sym] = df
    if not etf_data:
        logger.error("无有效数据")
        return pd.DataFrame()

    # 对齐到公共日期
    aligned = align_to_common_dates(etf_data)
    all_dates = sorted(set.union(
        *(set(df["date"].dropna()) for df in aligned.values())
    ))
    common_dates = pd.DatetimeIndex(all_dates)

    # ── Cerebro ──
    cerebro = bt.Cerebro(stdstats=False)
    cerebro.broker.setcash(capital)
    cerebro.broker.setcommission(commission=commission, mult=1.0, margin=None)
    cerebro.broker.set_slippage_perc(slippage)

    # 添加数据
    for sym in sorted(aligned.keys()):
        feed_df = aligned[sym][["date", "open", "high", "low", "close", "volume"]].copy()
        feed_df.set_index("date", inplace=True)
        feed_df = feed_df.reindex(common_dates).ffill().bfill()
        data = bt.feeds.PandasData(
            dataname=feed_df,
            open="open", high="high", low="low",
            close="close", volume="volume",
            plot=False,
        )
        cerebro.adddata(data, name=sym)

    # 分析器
    cerebro.addanalyzer(DailyEquityAnalyzer, _name="daily_eq")
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", riskfreerate=0.02)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")

    # 策略参数（与 run_backtest.py 一致）
    cerebro.addstrategy(
        TurtleStrategy,
        turtle_params=t_config,
        symbols=sorted(aligned.keys()),
        use_55_filter=False,
        alpha=0.0,  # 跳过风险平价避免协方差问题
        cov_lookback_days=252,
        risk_per_unit=t_config.get("risk_per_unit", 0.01),
        max_units=t_config.get("max_units", 4),
        pyramid_step=t_config.get("pyramid_step", 2.0),
        entry_mode=t_config.get("entry_mode", "breakout"),
        use_atr_pct_filter=False,
        shortable_symbols=set(),
        t_plus_one_symbols={"510500.SH", "159915.SZ"},
        futures_mode=False,
        multipliers={},
        min_unit=100,
        degradation_config=config.get("risk", {}).get("degradation", {}),
    )

    # ── 运行 ──
    logger.info("运行海龟回测...")
    results = cerebro.run()
    strat = results[0]

    # ── 提取每日净值 ──
    analyzer = strat.analyzers.daily_eq
    dates = pd.to_datetime(analyzer.dates)
    equity = np.array(analyzer.equity, dtype=float)

    df = pd.DataFrame({"date": dates, "equity": equity})
    df.sort_values("date", inplace=True)
    df.reset_index(drop=True, inplace=True)

    # 报告
    sharpe = strat.analyzers.sharpe.get_analysis()
    dd = strat.analyzers.drawdown.get_analysis()
    trades = strat.analyzers.trades.get_analysis()

    sharpe_ratio = sharpe.get("sharperatio") if sharpe else None
    max_dd = dd["max"]["drawdown"] if dd and "max" in dd else None
    total_trades = trades.get("total", {}).get("total", 0) if trades else 0

    final_value = equity[-1]
    total_return = (final_value / capital - 1) * 100
    years = len(equity) / 252
    cagr = (1 + total_return / 100) ** (1 / years) - 1 if years > 0 else 0

    print(f"\n📊  海龟回测结果")
    print(f"  初始资金: {capital:>10,.0f}")
    print(f"  最终净值: {final_value:>10,.0f}")
    print(f"  总收益:   {total_return:>9.2f}%")
    print(f"  CAGR:     {cagr:>9.1%}")
    print(f"  夏普:     {sharpe_ratio:>9.4f}" if sharpe_ratio else "  夏普:     N/A")
    print(f"  最大回撤: {max_dd:>9.2f}%" if max_dd else "  最大回撤: N/A")
    print(f"  交易次数: {total_trades:>9}")
    print(f"  数据点:   {len(df):>9} 天")

    # 保存
    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(output_path, index=False)
        print(f"  已保存: {output_path}")

    return df


def main():
    parser = argparse.ArgumentParser(description="导出海龟每日净值")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--start", default="2015-01-01")
    parser.add_argument("--end", default="2026-07-09")
    parser.add_argument("--capital", type=float, default=600000,
                        help="总资金（等权分配给所有品种）")
    parser.add_argument("--output", default=None,
                        help="输出 CSV 路径")
    args = parser.parse_args()

    export_equity(
        symbols=args.symbols,
        start_date=args.start,
        end_date=args.end,
        capital=args.capital,
        output_path=args.output,
    )


if __name__ == "__main__":
    main()
