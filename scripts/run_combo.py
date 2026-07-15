#!/usr/bin/env python
"""
双策略组合回测入口 — S43 Phase 2c

整合 N 字结构策略 + 海龟趋势策略 → ComboEngine → 组合绩效报告。

用法:
    py scripts/run_combo.py                          # 默认：等权 50/50，OOS 2020-2026
    py scripts/run_combo.py --start 2020-01-01       # 指定起始日
    py scripts/run_combo.py --end 2026-07-09         # 指定截止日
    py scripts/run_combo.py --weights 0.6,0.4        # 自定义权重 N=60% 海龟=40%
    py scripts/run_combo.py --n-only                 # 仅 N 字基准
    py scripts/run_combo.py --turtle-only            # 仅海龟基准
    py scripts/run_combo.py --save                   # 保存净值 CSV
    py scripts/run_combo.py --report                 # 输出 md 绩效报告
    py scripts/run_combo.py --rebalance none         # 无再平衡模式（权重自然漂移）
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.data_utils import load_data
from strategies.n_structure import NStructureStrategy
from strategies.combo_engine import ComboEngine, compute_metrics

# ── 常量 ──
DATA_DIR = REPO / "data" / "etf_daily"
CONFIG_PATH = REPO / "config" / "turtle_config.yaml"
RESULTS_DIR = REPO / "results"
DEFAULT_SYMBOLS = [
    "510500.SH", "159915.SZ", "513100.SH",
    "518880.SH", "159985.SZ", "513520.SH",
]
DEFAULT_START = "2020-01-01"
DEFAULT_END = "2026-07-09"
CAPITAL_PER_ETF = 100_000  # 每个 ETF 10万
TOTAL_CAPITAL = CAPITAL_PER_ETF * len(DEFAULT_SYMBOLS)  # 60万

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
)
logger = logging.getLogger("run_combo")


# ════════════════════════════════════════════════════════════
#  N 字结构净值
# ════════════════════════════════════════════════════════════

def run_n_equity(
    symbols: list[str],
    start: str,
    end: str,
    verbose: bool = False,
) -> tuple[pd.Series, float]:
    """逐品种运行 N 字结构，合并为组合净值曲线。

    Returns
    -------
    (portfolio_equity, total_capital)
        portfolio_equity: date-indexed Series, 各品种独立池求和
        total_capital: 总初始资金
    """
    strategy = NStructureStrategy(
        initial_capital=CAPITAL_PER_ETF,  # 单品种独立 10 万（S43 修复：消除资金被 6 等分失真）
        num_symbols=1,                     # 逐品种独立运行，不共享资金池
        window_size=100,
        atr_period=25,
        stop_mult=2.0,
        trail_mult=5.0,
        add_step=0.5,
        max_units=5,
        max_reentries=1,
        use_ma5_confirm=False,
    )

    all_eq: dict[str, pd.Series] = {}
    total_cap = 0.0

    for sym in symbols:
        df = load_data(sym, start, end, DATA_DIR)
        if df is None or df.empty:
            logger.warning("[WARN] %s: 无数据，跳过", sym)
            continue

        _, trades, equity = strategy.run(df, symbol=sym, verbose=verbose)

        if verbose:
            n_wins = sum(1 for t in trades if t.pnl > 0)
            total_pnl = sum(t.pnl for t in trades)
            logger.info(
                "  %s  %d笔  胜率%.0f%%  盈亏%+.0f  终值%.0f",
                sym, len(trades),
                n_wins / max(1, len(trades)) * 100,
                total_pnl, equity.iloc[-1],
            )

        all_eq[sym] = equity
        total_cap += CAPITAL_PER_ETF

    if not all_eq:
        return pd.Series(dtype=float), 0.0

    # 合并各品种独立净值
    portfolio = pd.concat(all_eq.values(), axis=1).sum(axis=1)
    return portfolio, total_cap


# ════════════════════════════════════════════════════════════
#  海龟净值
# ════════════════════════════════════════════════════════════

def get_turtle_equity(
    symbols: list[str],
    start: str,
    end: str,
    capital: float = TOTAL_CAPITAL,
    cache_path: Optional[Path] = None,
) -> pd.Series:
    """获取海龟策略净值曲线（优先缓存，否则运行回测）。

    Parameters
    ----------
    symbols : list[str]
        ETF 品种列表。
    start : str
        起始日期。
    end : str
        截止日期。
    capital : float
        初始资金。
    cache_path : Path, optional
        缓存 CSV 路径（默认 results/turtle_equity.csv）。

    Returns
    -------
    pd.Series
        date-indexed 净值曲线。
    """
    if cache_path is None:
        cache_path = RESULTS_DIR / "turtle_equity.csv"

    # ── 优先读取缓存 ──
    if cache_path.exists():
        df = pd.read_csv(cache_path)
        df["date"] = pd.to_datetime(df["date"])
        mask = (df["date"] >= start) & (df["date"] <= end)
        df = df[mask]
        if not df.empty:
            logger.info("[Cache] 海龟净值缓存: %s (%d 天)", cache_path, len(df))
            return pd.Series(df["equity"].values, index=df["date"])

    # ── 运行海龟回测 ──
    logger.info("[Wait] 海龟缓存未覆盖区间，运行回测...")
    try:
        from scripts.export_turtle_equity import export_equity
        turtle_df = export_equity(
            symbols=symbols,
            start_date=start,
            end_date=end,
            capital=capital,
            output_path=str(cache_path),
        )
        if turtle_df is not None and not turtle_df.empty:
            return pd.Series(
                turtle_df["equity"].values,
                index=pd.to_datetime(turtle_df["date"]),
            )
    except ImportError:
        logger.error("[ERR] 无法导入 export_turtle_equity，请先运行: py scripts/export_turtle_equity.py")

    return pd.Series(dtype=float)


# ════════════════════════════════════════════════════════════
#  报告输出
# ════════════════════════════════════════════════════════════

def print_comparison_table(
    n_metrics: dict,
    t_metrics: dict,
    combo_metrics: dict,
    weights: dict,
) -> None:
    """打印三方对比表。"""
    w_n = weights.get("n", 0.5)
    w_t = weights.get("turtle", 0.5)

    print(f"\n{'='*65}")
    print(f"  [Stats]  双策略组合绩效对比  (N字={w_n:.0%} / 海龟={w_t:.0%})")
    print(f"{'='*65}")
    print(f"\n{'指标':<16} {'N字结构':>12} {'海龟趋势':>12} {'组合':>12}")
    print(f"{'-'*52}")

    rows = [
        ("CAGR", "cagr", ".1%"),
        ("年化波动率", "vol", ".1%"),
        ("Sharpe", "sharpe", ".2f"),
        ("最大回撤", "mdd", ".1%"),
        ("Calmar", "calmar", ".2f"),
    ]
    for label, key, fmt in rows:
        n_val = n_metrics[key]
        t_val = t_metrics[key]
        c_val = combo_metrics[key]
        print(
            f"  {label:<16} {n_val:>{fmt if fmt else ''}}"
            f"{'':>4} {t_val:>{fmt if fmt else ''}}"
            f"{'':>4} {c_val:>{fmt if fmt else ''}}"
            if fmt
            else f"  {label:<16} {n_val:>12} {t_val:>12} {c_val:>12}"
        )

    print()


def print_yearly_table(yearly_df: pd.DataFrame) -> None:
    """打印逐年收益表。"""
    print(f"  {'─'*52}")
    print(f"  [Year]  逐年收益")
    print(f"  {'年份':<8} {'N字':>8} {'海龟':>8} {'组合':>8}")
    print(f"  {'─'*36}")
    for _, row in yearly_df.iterrows():
        yr = int(row["year"])
        n_r = row["n_return"]
        t_r = row["t_return"]
        c_r = row["combo_return"]
        print(f"  {yr:<8} {n_r:>7.1%} {t_r:>7.1%} {c_r:>7.1%}")
    print()


def print_correlation(engine: ComboEngine) -> None:
    """打印相关性摘要。"""
    try:
        corr_series = engine.rolling_correlation(window=252)
        if corr_series.dropna().empty:
            return
        avg_corr = corr_series.mean()
        min_corr = corr_series.min()
        max_corr = corr_series.max()
        print(f"  [Corr]  滚动相关性 (252日): 均值={avg_corr:+.3f}  "
              f"最小={min_corr:+.3f}  最大={max_corr:+.3f}")
        print()
    except Exception as e:
        logger.debug("[Corr] 滚动相关性计算跳过: %s", e)


def save_results(
    df: pd.DataFrame,
    yearly: pd.DataFrame,
    output_dir: Path,
) -> None:
    """保存净值 CSV 和逐年收益 CSV。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 净值曲线
    equity_path = output_dir / f"combo_equity_{timestamp}.csv"
    cols = ["date", "n_equity_norm", "t_equity_norm", "combo_equity"]
    df[cols].to_csv(equity_path, index=False)
    logger.info("[Save]  净值已保存: %s", equity_path)

    # 逐年收益
    yearly_path = output_dir / f"combo_yearly_{timestamp}.csv"
    yearly.to_csv(yearly_path, index=False)
    logger.info("[Save]  逐年收益已保存: %s", yearly_path)


def generate_md_report(
    df: pd.DataFrame,
    yearly: pd.DataFrame,
    engine: ComboEngine,
    start: str,
    end: str,
    output_path: Path,
) -> None:
    """生成标准化 Markdown 绩效报告。"""
    attrs = df.attrs
    nm = attrs["n_metrics"]
    tm = attrs["t_metrics"]
    cm = attrs["combo_metrics"]
    w = attrs["weights"]

    rebalance = attrs.get("rebalance_schedule", "daily")
    rebalance_desc = {
        "daily": "每日权重复位（净值层面加权）",
        "none": "无再平衡（收益率加权，权重自然漂移）",
    }.get(rebalance, rebalance)

    lines = [
        f"# 双策略组合绩效报告",
        f"",
        f"**回测区间**: {start} ~ {end}",
        f"**交易日数**: {attrs['n_trading_days']} 天 ({attrs['total_years']} 年)",
        f"**权重模式**: {attrs['weight_mode']} (N字={w['n']:.0%}, 海龟={w['turtle']:.0%})",
        f"**再平衡**: {rebalance_desc}",
        f"",
        f"## 核心指标",
        f"",
        f"| 指标 | N字结构 | 海龟趋势 | 组合 |",
        f"|------|---------|----------|------|",
        f"| CAGR | {nm['cagr']:.1%} | {tm['cagr']:.1%} | {cm['cagr']:.1%} |",
        f"| 年化波动率 | {nm['vol']:.1%} | {tm['vol']:.1%} | {cm['vol']:.1%} |",
        f"| Sharpe | {nm['sharpe']:.2f} | {tm['sharpe']:.2f} | {cm['sharpe']:.2f} |",
        f"| 最大回撤 | {nm['mdd']:.1%} | {tm['mdd']:.1%} | {cm['mdd']:.1%} |",
        f"| Calmar | {nm['calmar']:.2f} | {tm['calmar']:.2f} | {cm['calmar']:.2f} |",
        f"",
        f"## 逐年收益",
        f"",
        f"| 年份 | N字 | 海龟 | 组合 |",
        f"|------|-----|------|------|",
    ]
    for _, row in yearly.iterrows():
        lines.append(
            f"| {int(row['year'])} "
            f"| {row['n_return']:.1%} "
            f"| {row['t_return']:.1%} "
            f"| {row['combo_return']:.1%} |"
        )

    lines.append("")
    lines.append("## 组合优势分析")
    lines.append("")

    # 夏普提升
    best_sharpe = max(nm["sharpe"], tm["sharpe"])
    lines.append(f"- 夏普比率: {cm['sharpe']:.2f} "
                 f"({'[OK] 优于单一' if cm['sharpe'] > best_sharpe else '[WARN] 未提升'})")

    # 回撤降低
    best_mdd = max(nm["mdd"], tm["mdd"])  # mdd 是负数，max = 较不严重
    lines.append(f"- 最大回撤: {cm['mdd']:.1%} "
                 f"({'[OK] 降低' if cm['mdd'] > best_mdd else '[WARN] 未改善'})")

    # 波动率
    best_vol = min(nm["vol"], tm["vol"])
    lines.append(f"- 波动率: {cm['vol']:.1%} "
                 f"({'[OK] 降低' if cm['vol'] < best_vol else '[WARN] 未降低'})")

    lines.append("")
    lines.append(f"*报告生成时间: {datetime.now().isoformat()}*")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("[Save]  报告已保存: %s", output_path)


# ════════════════════════════════════════════════════════════
#  主入口
# ════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="双策略组合回测 — N字结构 + 海龟趋势",
    )
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS,
                        help="ETF 品种列表")
    parser.add_argument("--start", default=DEFAULT_START, help="起始日期")
    parser.add_argument("--end", default=DEFAULT_END, help="截止日期")
    parser.add_argument("--weights", default=None,
                        help="自定义权重 N,turtle (如 0.6,0.4)")
    parser.add_argument("--n-only", action="store_true", help="仅 N 字策略")
    parser.add_argument("--turtle-only", action="store_true", help="仅海龟策略")
    parser.add_argument("--rebalance", default="daily", choices=["daily", "none"],
                        help="再平衡模式: daily=每日权重复位(默认), none=无再平衡")
    parser.add_argument("--save", action="store_true", help="保存净值 CSV")
    parser.add_argument("--report", action="store_true", help="输出 Markdown 报告")
    parser.add_argument("--quiet", action="store_true", help="仅输出组合指标")
    args = parser.parse_args()

    # ── 权重解析 ──
    custom_weights = None
    if args.weights:
        parts = args.weights.split(",")
        if len(parts) == 2:
            custom_weights = {"n": float(parts[0]), "turtle": float(parts[1])}

    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)

    print("=" * 65)
    print("  [Combo]  双策略组合回测: N字结构 + 海龟趋势")
    print("=" * 65)

    # ── 1. N 字净值 ──
    print(f"\n  [Step] N字结构策略 (10万/品种 × {len(args.symbols)} = {TOTAL_CAPITAL/1e4:.0f}万)")
    n_portfolio, n_capital = run_n_equity(
        args.symbols, args.start, args.end,
        verbose=False,  # 精简输出，避免 N 字策略 emoji 编码问题
    )
    if n_portfolio.empty:
        print("  [ERR] N字策略无有效数据")
        return
    n_metrics_raw = compute_metrics(n_portfolio)
    print(f"  {'─'*40}")
    print(f"  N字终值: {n_portfolio.iloc[-1]:>10,.0f}  |  初始: {n_capital:>10,.0f}")
    print(f"  CAGR: {n_metrics_raw['cagr']:.1%}  |  MDD: {n_metrics_raw['mdd']:.1%}  |  "
          f"Sharpe: {n_metrics_raw['sharpe']:.2f}")

    # ── 2. 海龟净值 ──
    print(f"\n  [Step] 海龟趋势策略 ({TOTAL_CAPITAL/1e4:.0f}万组合)")
    t_equity = get_turtle_equity(
        args.symbols, args.start, args.end,
        capital=TOTAL_CAPITAL,
    )
    if t_equity.empty:
        print("  [ERR] 海龟策略无有效数据")
        return
    t_metrics_raw = compute_metrics(t_equity)
    print(f"  海龟终值: {t_equity.iloc[-1]:>10,.0f}  |  初始: {t_equity.iloc[0]:>10,.0f}")
    print(f"  CAGR: {t_metrics_raw['cagr']:.1%}  |  MDD: {t_metrics_raw['mdd']:.1%}  |  "
          f"Sharpe: {t_metrics_raw['sharpe']:.2f}")

    # ── 3. 组合引擎 ──
    print(f"\n  [Step] 组合引擎")
    engine = ComboEngine(
        weight_mode="equal",
        weights=custom_weights,
        rebalance_schedule=args.rebalance,
        enable_n=not args.turtle_only,
        enable_turtle=not args.n_only,
    )
    engine.feed_equity_curves(n_equity=n_portfolio, turtle_equity=t_equity)
    df = engine.combine()

    attrs = df.attrs
    nm = attrs["n_metrics"]
    tm = attrs["t_metrics"]
    cm = attrs["combo_metrics"]

    # ── 4. 对比表 ──
    print_comparison_table(nm, tm, cm, attrs["weights"])

    # ── 5. 逐年收益 ──
    yearly = engine.yearly_returns
    print_yearly_table(yearly)

    # ── 6. 相关性 ──
    print_correlation(engine)

    # ── 7. 结论 ──
    print(f"  {'='*52}")
    print(f"  [Summary]  结论")
    best_sharpe = max(nm["sharpe"], tm["sharpe"])
    best_mdd = max(nm["mdd"], tm["mdd"])
    best_cagr = max(nm["cagr"], tm["cagr"])

    checks = [
        ("夏普提升", cm["sharpe"] > best_sharpe,
         f"{cm['sharpe']:.2f} vs {best_sharpe:.2f}"),
        ("回撤降低", cm["mdd"] > best_mdd,
         f"{cm['mdd']:.1%} vs {best_mdd:.1%}"),
        ("收益接近最优", cm["cagr"] >= best_cagr * 0.85,
         f"{cm['cagr']:.1%} vs {best_cagr:.1%}"),
    ]
    for label, ok, detail in checks:
        print(f"  {'[OK]' if ok else '[WARN]'} {label:<16} {detail}")
    print()

    # ── 8. 保存 ──
    if args.save:
        save_results(df, yearly, RESULTS_DIR / "combo")

    if args.report:
        report_path = RESULTS_DIR / "combo" / "combo_report.md"
        generate_md_report(df, yearly, engine, args.start, args.end, report_path)


if __name__ == "__main__":
    main()
