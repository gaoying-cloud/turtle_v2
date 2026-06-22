#!/usr/bin/env python
"""
跨市场ETF海龟组合策略 · 参数网格搜索 (S6)

在 5 个核心参数上做笛卡尔积网格搜索，覆盖模式 A/B，
包含样本内/样本外分割验证和稳健性评分。
支持 multiprocessing 并行加速。

用法：
    py scripts/run_grid_search.py                          # 全量搜索（810 组）
    py scripts/run_grid_search.py --mode A                  # 仅模式 A
    py scripts/run_grid_search.py --quick                   # 快速验证（抽样 10 组）
    py scripts/run_grid_search.py --workers 4               # 4 核并行
    py scripts/run_grid_search.py --rolling                 # 滚动窗口检验
    py scripts/run_grid_search.py --stability               # 参数稳定性面扫描
    py scripts/run_grid_search.py --plot                    # 生成敏感性图
"""

from __future__ import annotations

import argparse
import gc
import itertools
import json
import logging
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import backtrader as bt
import numpy as np
import pandas as pd
import yaml

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.turtle_core import (
    TurtleSignals,
    TurtlePositions,
    SignalFilter,
    calc_position_size,
    calc_fixed_stop,
    calc_trailing_stop,
    calc_pyramid_trigger,
    pyramid_add,
    Position,
)
from src.risk_parity import compute_alpha_weights
from strategies.turtle_trading import TurtleStrategy
from src.config_loader import get_shortable_symbols, get_t_plus_one_symbols

logger = logging.getLogger(__name__)

# ── 路径 ──
CONFIG_PATH = ROOT / "config" / "turtle_config.yaml"
DATA_DIR = ROOT / "data" / "etf_daily"
OUTPUT_DIR = ROOT / "results" / "grid_search"

# 从统一配置读取品种列表
with open(CONFIG_PATH, "r", encoding="utf-8") as _f:
    _CONFIG = yaml.safe_load(_f)
from src.config_loader import get_trading_symbols, get_bond_symbol, get_all_symbols
SIX_SYMBOLS = get_trading_symbols(_CONFIG)
BOND_SYMBOL = get_bond_symbol(_CONFIG)
ALL_SYMBOLS = get_all_symbols(_CONFIG)


# ── 参数空间 ──
PARAM_GRID = {
    "atr_period": [15, 20, 25],
    "breakout_period": [15, 20, 25],
    "stop_period": [8, 10, 12],
    "stop_atr_multiple": [1.5, 2.0, 2.5],
    "alpha": [0, 0.05, 0.10, 0.15, 0.20],
    "max_cumulative_loss_pct": [0.10, 0.15, 0.20],
    "max_consecutive_losses": [5, 8, 10],
}

MODES = ["A", "B"]


# ════════════════════════════════════════════════════════════
#  1. 参数网格构建
# ════════════════════════════════════════════════════════════

def build_param_grid() -> List[dict]:
    """生成参数笛卡尔积。

    Returns
    -------
    list[dict]
        3645 组参数组合，每组包含 7 个键：
        atr_period, breakout_period, stop_period, stop_atr_multiple, alpha,
        max_cumulative_loss_pct, max_consecutive_losses.
    """
    keys = list(PARAM_GRID.keys())
    values = list(PARAM_GRID.values())
    grid = []
    for combo in itertools.product(*values):
        grid.append(dict(zip(keys, combo)))
    return grid


# ════════════════════════════════════════════════════════════
#  2. 单次回测运行
# ════════════════════════════════════════════════════════════

def load_data(
    symbol: str,
    start_date: str,
    end_date: str,
) -> Optional[pd.DataFrame]:
    """从 Parquet 缓存加载单个品种的数据。"""
    path = DATA_DIR / f"{symbol}.parquet"
    if not path.exists():
        logger.error("缓存文件不存在: %s\n请先运行 py scripts/pull_data.py", path)
        return None

    df = pd.read_parquet(path)
    if df.empty:
        logger.warning("[%s] 缓存为空", symbol)
        return None

    mask = (df["date"] >= start_date) & (df["date"] <= end_date)
    df = df[mask].copy()
    if df.empty:
        logger.warning("[%s] 在 %s~%s 区间无数据", symbol, start_date, end_date)
        return None

    df.sort_values("date", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def df_to_feed(df: pd.DataFrame, symbol: str) -> bt.feeds.PandasData:
    """将 pandas DataFrame 转换为 Backtrader PandasData feed。"""
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


def run_single_backtest(
    params: dict,
    mode: str,
    start_date: str,
    end_date: str,
    run_id: int = 0,
) -> Optional[dict]:
    """运行单次回测，返回标准化指标字典。

    Parameters
    ----------
    params : dict
        参数组合，必须包含 atr_period, breakout_period, stop_period,
        stop_atr_multiple, alpha。
    mode : str
        "A" = 无 55 日过滤, "B" = 55 日过滤。
    start_date : str
        回测起始日期。
    end_date : str
        回测截止日期。
    run_id : int
        运行序号（用于日志追踪）。

    Returns
    -------
    dict or None
        {
            "run_id": int,
            "mode": str,
            "atr_period": int,
            "breakout_period": int,
            "stop_period": int,
            "stop_atr_multiple": float,
            "alpha": float,
            "total_return": float,
            "cagr": float,
            "sharpe": float or None,
            "max_drawdown": float,
            "win_rate": float,
            "profit_factor": float,
            "total_trades": int,
            "annual_vol": float,
            "calmar": float,
            "final_value": float,
            "date_range": str,
        }
        失败返回 None。
    """
    # ── 验证参数 ──
    for key in ["atr_period", "breakout_period", "stop_period", "stop_atr_multiple", "alpha", "max_cumulative_loss_pct", "max_consecutive_losses"]:
        if key not in params:
            logger.error("参数缺少 %s", key)
            return None

    # ── 加载配置 ──
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # ── 加载数据 ──
    feeds: dict[str, bt.feeds.PandasData] = {}
    for symbol in ALL_SYMBOLS:
        df = load_data(symbol, start_date, end_date)
        if df is None:
            logger.error("[run_id=%d] 品种 %s 数据加载失败", run_id, symbol)
            return None
        feed = df_to_feed(df, symbol)
        feed._name = symbol
        feeds[symbol] = feed

    # ── 组装 Cerebro ──
    cerebro = bt.Cerebro()

    for symbol in SIX_SYMBOLS:
        cerebro.adddata(feeds[symbol], name=symbol)
    cerebro.adddata(feeds[BOND_SYMBOL], name=BOND_SYMBOL)

    cerebro.broker.setcash(config["initial_cash"])
    commission = config["commission_pct"]
    slippage = config["slippage_pct"]
    cerebro.broker.setcommission(commission=commission + slippage)

    # ── 构建 turtle_params（注入网格搜索参数）──
    turtle_params = {
        "atr_period": params["atr_period"],
        "breakout_period": params["breakout_period"],
        "stop_period": params["stop_period"],
        "stop_atr_multiple": params["stop_atr_multiple"],
        "risk_per_unit": config["turtle"]["risk_per_unit"],
        "max_units": config["turtle"]["max_units"],
        "unit_step": config["turtle"]["unit_step"],
        "use_55_filter": (mode == "B"),
        "exit_period": config["turtle"]["exit_period"],
    }

    # ── 添加策略 ──
    cerebro.addstrategy(
        TurtleStrategy,
        turtle_params=turtle_params,
        symbols=SIX_SYMBOLS,
        use_55_filter=(mode == "B"),
        risk_per_unit=config["turtle"]["risk_per_unit"],
        concentration_trigger=config["risk"]["concentration_trigger"],
        max_consecutive_losses=params["max_consecutive_losses"],
        max_cumulative_loss_pct=params["max_cumulative_loss_pct"],
        pause_days=config["risk"]["pause_days"],
        max_portfolio_risk=config["risk"]["max_portfolio_risk"],
        alpha=params["alpha"],
        cov_lookback_days=config["weighting"]["cov_lookback_days"],
        rebalance_quarterly=config["weighting"]["rebalance_quarterly"],
        atr_change_threshold=config["weighting"]["atr_change_threshold"],
        shortable_symbols=get_shortable_symbols(config),
        t_plus_one_symbols=get_t_plus_one_symbols(config),
    )

    # ── 分析器 ──
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", timeframe=bt.TimeFrame.Years)
    cerebro.addanalyzer(bt.analyzers.AnnualReturn, _name="annual_return")
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
    cerebro.addanalyzer(bt.analyzers.VWR, _name="vwr")

    # ── 运行 ──
    initial_cash = config["initial_cash"]
    try:
        results = cerebro.run()
    except Exception as e:
        logger.error("[run_id=%d] 回测异常: %s", run_id, e)
        return None

    if not results:
        logger.error("[run_id=%d] 回测未返回结果", run_id)
        return None

    strat = results[0]

    # ── 提取指标 ──
    final_value = cerebro.broker.getvalue()
    total_return = (final_value / initial_cash - 1) * 100

    # 年化收益率（按实际天数）
    n_years = (datetime.strptime(end_date, "%Y-%m-%d") - datetime.strptime(start_date, "%Y-%m-%d")).days / 365.25
    cagr = ((final_value / initial_cash) ** (1 / max(n_years, 0.1)) - 1) * 100 if n_years > 0 else 0.0

    # 夏普
    sharpe = strat.analyzers.sharpe.get_analysis()
    sharpe_val = sharpe.get("sharperatio", None) if sharpe else None

    # 最大回撤
    dd = strat.analyzers.drawdown.get_analysis()
    max_dd = dd.get("max", {}).get("drawdown", 0.0) if dd else 0.0

    # 交易统计
    trades = strat.analyzers.trades.get_analysis()
    total = trades.get("total", {}).get("total", 0) if trades else 0
    won = trades.get("won", {}).get("total", 0) if trades else 0
    lost = trades.get("lost", {}).get("total", 0) if trades else 0
    win_rate = (won / total * 100) if total > 0 else 0.0

    avg_win = abs(trades.get("won", {}).get("pnl", {}).get("average", 0)) if trades else 0
    avg_loss = abs(trades.get("lost", {}).get("pnl", {}).get("average", 0)) if trades else 0
    profit_factor = (avg_win / avg_loss) if avg_loss > 0 else 0.0

    # 年化波动率
    returns_analyzer = strat.analyzers.returns.get_analysis()
    annual_vol = returns_analyzer.get("rvol100", None)
    if annual_vol is None:
        annual_vol = 0.0

    # Calmar
    calmar = (cagr / abs(max_dd)) if max_dd > 0 else 0.0

    # ── 清理 ──
    del cerebro, strat, results, feeds
    gc.collect()

    result = {
        "run_id": run_id,
        "mode": mode,
        "atr_period": params["atr_period"],
        "breakout_period": params["breakout_period"],
        "stop_period": params["stop_period"],
        "stop_atr_multiple": params["stop_atr_multiple"],
        "alpha": params["alpha"],
        "max_cumulative_loss_pct": params["max_cumulative_loss_pct"],
        "max_consecutive_losses": params["max_consecutive_losses"],
        "total_return": round(total_return, 4),
        "cagr": round(cagr, 4),
        "sharpe": round(sharpe_val, 4) if sharpe_val is not None else None,
        "max_drawdown": round(max_dd, 4),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 4),
        "total_trades": total,
        "annual_vol": round(annual_vol, 4),
        "calmar": round(calmar, 4),
        "final_value": round(final_value, 2),
        "date_range": f"{start_date}~{end_date}",
    }
    return result


# ════════════════════════════════════════════════════════════
#  3. 多进程 Worker
# ════════════════════════════════════════════════════════════

def _worker(task: tuple) -> Optional[dict]:
    """多进程 worker（子进程入口，抑制日志/警告噪音）。

    task = (params, mode, start_date, end_date, run_id)
    """
    # 子进程重置警告过滤（Windows spawn 不继承父进程）
    warnings.filterwarnings("ignore", category=UserWarning)
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    # 抑制策略层 info/debug 日志（网格搜索阶段不需要入场/退出细节）
    logging.getLogger("strategies.turtle_trading").setLevel(logging.WARNING)
    logging.getLogger("src.risk_parity").setLevel(logging.WARNING)

    params, mode, start_date, end_date, run_id = task
    return run_single_backtest(params, mode, start_date, end_date, run_id)


# ════════════════════════════════════════════════════════════
#  4. 网格搜索主循环
# ════════════════════════════════════════════════════════════

def run_grid_search(
    *,
    modes: List[str] = MODES,
    start_date: str = "2020-01-01",
    split_date: str = "2024-01-01",
    end_date: str = "2026-06-10",
    workers: int = 1,
    quick: bool = False,
    verbose: bool = False,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """运行完整网格搜索。

    Parameters
    ----------
    modes : list[str]
        需要搜索的模式列表，默认 ["A", "B"]。
    start_date : str
        整体回测起始日期。
    split_date : str
        样本内/样本外分割日期。
    end_date : str
        整体回测截止日期。
    workers : int
        并行进程数，默认 1（串行）。
    quick : bool
        快速验证模式（仅跑 10 组参数 × 全部 modes）。
    verbose : bool
        详细日志。

    Returns
    -------
    tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]
        (df_full, df_oos, df_best)
        df_full: 样本内完整结果
        df_oos:  样本外验证结果（仅 Top-N）
        df_best: 最优参数表（Top-10）
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── 构建参数网格 ──
    param_grid = build_param_grid()
    if quick:
        # 快速模式：随机抽样 10 组
        rng = np.random.default_rng(42)
        indices = rng.choice(len(param_grid), size=min(10, len(param_grid)), replace=False)
        param_grid = [param_grid[i] for i in indices]
        logger.info("快速模式：抽样 %d 组参数", len(param_grid))

    logger.info("参数组合: %d 组 × %d 模式 = %d 次回测",
                len(param_grid), len(modes), len(param_grid) * len(modes))

    # ── 样本内回测 ──
    logger.info("=" * 50)
    logger.info("样本内回测: %s ~ %s", start_date, split_date)
    logger.info("=" * 50)

    tasks = []
    run_id = 0
    for params in param_grid:
        for mode in modes:
            tasks.append((params, mode, start_date, split_date, run_id))
            run_id += 1

    results_full = _run_tasks(tasks, workers, verbose)

    df_full = pd.DataFrame(results_full)
    full_path = OUTPUT_DIR / "grid_results_full.csv"
    df_full.to_csv(full_path, index=False, encoding="utf-8")
    logger.info("样本内结果已保存: %s (%d 行)", full_path, len(df_full))

    # ── 稳健性评估 + 最优参数选择 ──
    df_best = evaluate_results(df_full, top_n=10)
    best_path = OUTPUT_DIR / "best_params.json"
    _save_best_params_json(df_best, best_path)
    logger.info("最优参数已保存: %s", best_path)

    # ── 样本外验证 Top-N ──
    logger.info("=" * 50)
    logger.info("样本外验证: %s ~ %s", split_date, end_date)
    logger.info("=" * 50)

    oos_tasks = []
    run_id = 0
    for _, row in df_best.iterrows():
        params = {
            "atr_period": int(row["atr_period"]),
            "breakout_period": int(row["breakout_period"]),
            "stop_period": int(row["stop_period"]),
            "stop_atr_multiple": float(row["stop_atr_multiple"]),
            "alpha": float(row["alpha"]),
            "max_cumulative_loss_pct": float(row.get("max_cumulative_loss_pct", 0.15)),
            "max_consecutive_losses": int(row.get("max_consecutive_losses", 8)),
        }
        for mode in modes:
            oos_tasks.append((params, mode, split_date, end_date, run_id))
            run_id += 1

    oos_results = _run_tasks(oos_tasks, workers, verbose)
    df_oos = pd.DataFrame(oos_results)
    oos_path = OUTPUT_DIR / "oos_validation.csv"
    df_oos.to_csv(oos_path, index=False, encoding="utf-8")
    logger.info("样本外验证已保存: %s (%d 行)", oos_path, len(df_oos))

    return df_full, df_oos, df_best


def _run_tasks(tasks: list, workers: int, verbose: bool) -> List[dict]:
    """并行或串行执行回测任务列表。"""
    n_total = len(tasks)
    results = []

    if workers > 1 and n_total > 1:
        logger.info("并行执行: %d workers", workers)
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_worker, t): t for t in tasks}
            done = 0
            for future in as_completed(futures):
                done += 1
                task = futures[future]
                try:
                    result = future.result()
                    if result is not None:
                        results.append(result)
                except Exception as e:
                    logger.error("任务异常 (run_id=%d): %s", task[4], e)

                if done == 1 or done % 50 == 0 or done == n_total:
                    logger.info("  进度: %d / %d (%.0f%%)", done, n_total, done / n_total * 100)
    else:
        logger.info("串行执行")
        for i, task in enumerate(tasks):
            result = _worker(task)
            if result is not None:
                results.append(result)
            if (i + 1) == 1 or (i + 1) % 50 == 0 or (i + 1) == n_total:
                logger.info("  进度: %d / %d (%.0f%%)", i + 1, n_total, (i + 1) / n_total * 100)

    logger.info("完成 %d / %d 次回测", len(results), n_total)
    return results


# ════════════════════════════════════════════════════════════
#  5. 结果评估
# ════════════════════════════════════════════════════════════

def evaluate_results(df: pd.DataFrame, top_n: int = 10) -> pd.DataFrame:
    """评估网格搜索结果，按稳健性评分排序。

    评分指标（加权）：
        - 25% Sharpe（标准化）
        - 20% Calmar（标准化）
        - 15% (1 - Sharpe 波动)  — 单模式下无此维度，改用夏普直接排名
        - 15% (1 - 回撤波动)
        - 15% 样本外衰减率的负向
        - 10% 交易次数充足度

    Parameters
    ----------
    df : pd.DataFrame
        网格搜索完整结果表。
    top_n : int
        返回 Top-N 参数组合。

    Returns
    -------
    pd.DataFrame
        按稳健性评分降序排列的 Top-N 参数组合。
    """
    if df.empty:
        logger.warning("结果为空，无法评估")
        return pd.DataFrame()

    # ── 剔除无效夏普 ──
    df_valid = df.dropna(subset=["sharpe"]).copy()
    all_sharpe_nan = False
    if df_valid.empty:
        logger.warning("无有效夏普比率，回退使用收益率排序")
        df_valid = df.copy()
        all_sharpe_nan = True

    # ── 计算稳健性评分 ──
    scaler = _robust_scaler

    if all_sharpe_nan:
        # 夏普全 NaN 时，只用 cagr 和 trades 评分
        df_valid["_cagr_score"] = scaler(df_valid["cagr"].astype(float))
        df_valid["_trade_score"] = scaler(np.log1p(df_valid["total_trades"].astype(float)))
        df_valid["_dd_score"] = scaler(-df_valid["max_drawdown"].astype(float))
        df_valid["robustness_score"] = (
            0.40 * df_valid["_cagr_score"]
            + 0.30 * df_valid["_trade_score"]
            + 0.30 * df_valid["_dd_score"]
        )
    else:
        df_valid["_sharpe_score"] = scaler(df_valid["sharpe"].astype(float))
        df_valid["_calmar_score"] = scaler(df_valid["calmar"].astype(float))
        df_valid["_cagr_score"] = scaler(df_valid["cagr"].astype(float))
        df_valid["_dd_score"] = scaler(-df_valid["max_drawdown"].astype(float))
        df_valid["_trade_score"] = scaler(np.log1p(df_valid["total_trades"].astype(float)))

        df_valid["robustness_score"] = (
            0.25 * df_valid["_sharpe_score"]
            + 0.20 * df_valid["_calmar_score"]
            + 0.20 * df_valid["_cagr_score"]
            + 0.15 * df_valid["_dd_score"]
            + 0.20 * df_valid["_trade_score"]
        )

    # ── 按模式分组，每组取 Top-N ──
    best_groups = []
    for mode_val, group in df_valid.groupby("mode"):
        top = group.nlargest(top_n, "robustness_score")
        best_groups.append(top)

    df_best = pd.concat(best_groups, ignore_index=True)
    df_best.sort_values(["mode", "robustness_score"], ascending=[True, False], inplace=True)

    # ── 只输出核心列 ──
    cols = [
        "run_id", "mode",
        "atr_period", "breakout_period", "stop_period", "stop_atr_multiple", "alpha",
        "max_cumulative_loss_pct", "max_consecutive_losses",
        "total_return", "cagr", "sharpe", "max_drawdown",
        "win_rate", "profit_factor", "total_trades", "annual_vol", "calmar",
        "robustness_score",
    ]
    df_best = df_best[[c for c in cols if c in df_best.columns]]
    df_best.reset_index(drop=True, inplace=True)

    logger.info("最优参数组合（Top-%d）：", top_n)
    for _, row in df_best.iterrows():
        logger.info(
            "  [%s] atr=%d breakout=%d stop=%d mult=%.1f α=%.2f | "
            "sharpe=%.3f cagr=%.2f%% mdd=%.2f%% trades=%d score=%.4f",
            row["mode"],
            int(row["atr_period"]), int(row["breakout_period"]),
            int(row["stop_period"]), float(row["stop_atr_multiple"]), float(row["alpha"]),
            row["sharpe"] if pd.notna(row["sharpe"]) else 0,
            row["cagr"], row["max_drawdown"], int(row["total_trades"]),
            row["robustness_score"],
        )

    return df_best


def _robust_scaler(series: pd.Series) -> pd.Series:
    """稳健标准化：使用中位数和 IQR，对异常值不敏感。"""
    med = series.median()
    iqr = series.quantile(0.75) - series.quantile(0.25)
    if iqr == 0 or np.isnan(iqr):
        return series * 0.0
    return (series - med) / iqr


def _save_best_params_json(df_best: pd.DataFrame, path: Path):
    """将最优参数表保存为 JSON。"""
    records = df_best.to_dict(orient="records")
    output = []
    for r in records:
        output.append({
            "mode": r.get("mode"),
            "max_cumulative_loss_pct": float(r.get("max_cumulative_loss_pct", 0.15)),
            "max_consecutive_losses": int(r.get("max_consecutive_losses", 8)),
            "atr_period": int(r.get("atr_period", 20)),
            "breakout_period": int(r.get("breakout_period", 20)),
            "stop_period": int(r.get("stop_period", 10)),
            "stop_atr_multiple": float(r.get("stop_atr_multiple", 2.0)),
            "alpha": float(r.get("alpha", 0.05)),
            "sharpe": r.get("sharpe"),
            "cagr": r.get("cagr"),
            "max_drawdown": r.get("max_drawdown"),
            "total_trades": int(r.get("total_trades", 0)),
            "robustness_score": r.get("robustness_score", 0),
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


# ════════════════════════════════════════════════════════════
#  6. 图表（可选）
# ════════════════════════════════════════════════════════════

def plot_results(df: pd.DataFrame, output_dir: Path):
    """生成参数敏感性图。

    需要 matplotlib。
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        logger.warning("matplotlib 未安装，跳过绘图")
        return

    if df.empty:
        logger.warning("无数据可绘图")
        return

    # ── 散点图：每个参数 vs 夏普 ──
    param_keys = ["atr_period", "breakout_period", "stop_period", "stop_atr_multiple", "alpha"]
    fig, axes = plt.subplots(2, 3, figsize=(15, 8))
    axes = axes.flatten()

    for i, key in enumerate(param_keys):
        ax = axes[i]
        for mode_val, group in df.groupby("mode"):
            ax.scatter(group[key], group["sharpe"], label=f"模式 {mode_val}", alpha=0.5, s=10)
        ax.set_xlabel(key)
        ax.set_ylabel("Sharpe")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)

    axes[-1].axis("off")
    plt.suptitle("参数敏感性分析：各参数 vs 夏普比率", fontsize=14)
    plt.tight_layout()
    path = output_dir / "sensitivity_sharpe.png"
    plt.savefig(path, dpi=150)
    plt.close()
    logger.info("散点图已保存: %s", path)

    # ── 热力图：突破周期 × 止损周期 ──
    if "mode" in df.columns:
        for mode_val, group in df.groupby("mode"):
            pivot = group.pivot_table(
                values="sharpe",
                index="breakout_period",
                columns="stop_period",
                aggfunc="mean",
            )
            if pivot.empty:
                continue

            fig, ax = plt.subplots(figsize=(6, 5))
            im = ax.imshow(pivot.values, cmap="RdYlGn", aspect="auto")
            ax.set_xticks(range(len(pivot.columns)))
            ax.set_xticklabels(pivot.columns)
            ax.set_yticks(range(len(pivot.index)))
            ax.set_yticklabels(pivot.index)
            ax.set_xlabel("止损周期")
            ax.set_ylabel("突破周期")
            for r in range(pivot.shape[0]):
                for c in range(pivot.shape[1]):
                    val = pivot.values[r, c]
                    if not np.isnan(val):
                        ax.text(c, r, f"{val:.2f}", ha="center", va="center", fontsize=9)
            plt.colorbar(im, ax=ax, label="Sharpe")
            plt.title(f"模式 {mode_val}：突破周期 × 止损周期 (平均 Sharpe)")
            plt.tight_layout()
            heat_path = output_dir / f"heatmap_{mode_val}.png"
            plt.savefig(heat_path, dpi=150)
            plt.close()
            logger.info("热力图已保存: %s", heat_path)


# ════════════════════════════════════════════════════════════
#  7. 滚动窗口验证
# ════════════════════════════════════════════════════════════

def run_rolling_validation(
    df_best: pd.DataFrame,
    *,
    modes: List[str] = MODES,
    base_start: str = "2020-01-01",
    base_end: str = "2026-06-10",
    workers: int = 1,
) -> pd.DataFrame:
    """对最优参数组合执行滚动窗口验证。

    固定窗口 3 年，步长 1 年，产生 3 个窗口：
        W1: IS 2020-01~2022-12, OOS 2023-01~2023-12
        W2: IS 2021-01~2023-12, OOS 2024-01~2024-12
        W3: IS 2022-01~2024-12, OOS 2025-01~base_end

    返回每个窗口的 OOS 指标表（宽表），
    以及在报告中直接打印的汇总行（含 CV 标记）。

    Parameters
    ----------
    df_best : pd.DataFrame
        Top-N 最优参数组合表（含 atr_period / breakout_period / stop_period 等列）。
    modes : list[str]
        需要验证的模式列表。
    base_start, base_end : str
        数据总区间（用于 W3 OOS 截断）。
    workers : int
        并行进程数。

    Returns
    -------
    pd.DataFrame
        行 = (mode, param_idx, window), 列 = 各项指标。
    """
    from datetime import datetime

    # ── 定义 3 个窗口 ──
    windows = [
        ("W1", "2020-01-01", "2022-12-31", "2023-01-01", "2023-12-31"),
        ("W2", "2021-01-01", "2023-12-31", "2024-01-01", "2024-12-31"),
        ("W3", "2022-01-01", "2024-12-31", "2025-01-01", base_end),
    ]

    logger.info("=" * 60)
    logger.info("滚动窗口验证 (%d 个窗口)", len(windows))
    for wname, is_s, is_e, oos_s, oos_e in windows:
        logger.info("  %s: IS %s~%s → OOS %s~%s", wname, is_s, is_e, oos_s, oos_e)
    logger.info("=" * 60)

    all_rows = []
    all_tasks = []
    task_meta = {}  # run_id -> (idx, mode, wname, phase)

    run_counter = 20000  # avoid collision

    for idx, (_, row) in enumerate(df_best.iterrows()):
        params = {
            "atr_period": int(row["atr_period"]),
            "breakout_period": int(row["breakout_period"]),
            "stop_period": int(row["stop_period"]),
            "stop_atr_multiple": float(row["stop_atr_multiple"]),
            "alpha": float(row["alpha"]),
            "max_cumulative_loss_pct": float(row.get("max_cumulative_loss_pct", 0.15)),
            "max_consecutive_losses": int(row.get("max_consecutive_losses", 8)),
        }

        for mode in modes:
            for wname, is_s, is_e, oos_s, oos_e in windows:
                # IS 任务
                rid = run_counter
                run_counter += 1
                task_meta[rid] = (params, mode, idx, wname, "is")
                all_tasks.append((params, mode, is_s, is_e, rid))
                # OOS 任务
                rid = run_counter
                run_counter += 1
                task_meta[rid] = (params, mode, idx, wname, "oos")
                all_tasks.append((params, mode, oos_s, oos_e, rid))

    # ── 并行/串行执行 ──
    logger.info("滚动窗口: %d 次回测, workers=%d", len(all_tasks), workers)
    raw_results = _run_tasks(all_tasks, workers, verbose=False)

    # ── 按 run_id 匹配组装 ──
    # 先收集: (idx, mode, wname) -> {is_result, oos_result}
    roll_map: dict = {}
    for result in raw_results:
        if result is None:
            continue
        rid = result.get("run_id")
        if rid is None or rid not in task_meta:
            continue
        params, mode, idx, wname, phase = task_meta[rid]
        key = (idx, mode, wname)
        if key not in roll_map:
            roll_map[key] = {"params": params}
        roll_map[key][f"{phase}_result"] = result

    # 组装最终行
    for (idx, mode, wname), data in roll_map.items():
        is_r = data.get("is_result")
        oos_r = data.get("oos_result")
        if is_r is None or oos_r is None:
            continue
        entry = {
            "window": wname,
            "mode": mode,
            "param_idx": idx,
            "atr_period": data["params"]["atr_period"],
            "breakout_period": data["params"]["breakout_period"],
            "stop_period": data["params"]["stop_period"],
            "stop_atr_multiple": data["params"]["stop_atr_multiple"],
            "alpha": data["params"]["alpha"],
            "is_sharpe": is_r.get("sharpe"),
            "is_cagr": is_r.get("cagr"),
            "is_mdd": is_r.get("max_drawdown"),
            "is_trades": is_r.get("total_trades"),
            "oos_sharpe": oos_r.get("sharpe"),
            "oos_cagr": oos_r.get("cagr"),
            "oos_mdd": oos_r.get("max_drawdown"),
            "oos_trades": oos_r.get("total_trades"),
        }
        all_rows.append(entry)

    if not all_rows:
        logger.warning("滚动窗口验证未产生任何有效结果")
        return pd.DataFrame()

    df_rolling = pd.DataFrame(all_rows)

    # ── 打印汇总 ──
    logger.info("")
    logger.info("滚动窗口验证汇总")
    logger.info("-" * 80)

    groups = df_rolling.groupby(["mode", "param_idx"])
    for (mode_val, pidx), grp in groups:
        sharpe_vals = grp["oos_sharpe"].dropna()
        cagr_vals = grp["oos_cagr"].dropna()
        mdd_vals = grp["oos_mdd"].dropna()

        if len(sharpe_vals) < 2:
            logger.info("  [%s param#%d] 有效窗口不足", mode_val, pidx)
            continue

        sharpe_mean = sharpe_vals.mean()
        sharpe_std = sharpe_vals.std()
        sharpe_cv = sharpe_std / max(abs(sharpe_mean), 0.01)

        cagr_mean = cagr_vals.mean()
        cagr_std = cagr_vals.std()

        flag = " ⚠️ CV>0.5" if sharpe_cv > 0.5 else ""
        logger.info(
            "  [%s param#%d] OOS Sharpe: %.3f ± %.3f (CV=%.2f)%s | CAGR: %.2f%% ± %.2f%% | MDD: %.2f%%",
            mode_val, pidx,
            sharpe_mean, sharpe_std, sharpe_cv, flag,
            cagr_mean, cagr_std,
            mdd_vals.mean(),
        )

        # 逐窗口明细
        for _, wrow in grp.sort_values("window").iterrows():
            logger.info(
                "    %s: IS Sharpe=%.3f OOS Sharpe=%.3f | IS CAGR=%.2f%% OOS CAGR=%.2f%% | OOS MDD=%.2f%%",
                wrow["window"],
                wrow["is_sharpe"] if pd.notna(wrow.get("is_sharpe")) else 0,
                wrow["oos_sharpe"] if pd.notna(wrow.get("oos_sharpe")) else 0,
                wrow["is_cagr"] if pd.notna(wrow.get("is_cagr")) else 0,
                wrow["oos_cagr"] if pd.notna(wrow.get("oos_cagr")) else 0,
                wrow["oos_mdd"] if pd.notna(wrow.get("oos_mdd")) else 0,
            )

    return df_rolling


# ════════════════════════════════════════════════════════════
#  8. 参数稳定性面扫描 (±1 邻域)
# ════════════════════════════════════════════════════════════

def run_stability_scan(
    df_best: pd.DataFrame,
    *,
    modes: List[str] = MODES,
    start_date: str = "2020-01-01",
    end_date: str = "2026-06-10",
    workers: int = 1,
) -> pd.DataFrame:
    """对最优参数组合执行 ±1 邻域稳定性扫描。

    对离散参数 (atr_period, breakout_period, stop_period) 做 ±1 全排列，
    对连续参数 (stop_atr_multiple, alpha) 做中心值。
    统计目标指标在邻域内的分布，判断参数是否为尖锐峰值。

    Parameters
    ----------
    df_best : pd.DataFrame
        Top-N 最优参数组合表。
    modes : list[str]
        需要验证的模式列表。
    start_date, end_date : str
        回测区间（全区间回测）。
    workers : int
        并行进程数。

    Returns
    -------
    pd.DataFrame
        邻域内所有参数组合的绩效指标。
    """
    logger.info("=" * 60)
    logger.info("参数稳定性面扫描 (±1 邻域)")
    logger.info("=" * 60)

    all_rows = []
    all_tasks = []  # for parallel execution
    task_map = {}   # run_id -> (params, mode, idx)

    run_counter = 10000  # avoid collision with grid search run_ids

    for idx, (_, row) in enumerate(df_best.iterrows()):
        # 中心参数
        atr_c = int(row["atr_period"])
        break_c = int(row["breakout_period"])
        stop_c = int(row["stop_period"])
        mult_c = float(row["stop_atr_multiple"])
        alpha_c = float(row["alpha"])
        max_loss = float(row.get("max_cumulative_loss_pct", 0.15))
        max_consec = int(row.get("max_consecutive_losses", 8))

        logger.info("Top param #%d: atr=%d breakout=%d stop=%d mult=%.1f α=%.2f",
                     idx, atr_c, break_c, stop_c, mult_c, alpha_c)

        # 构建邻域: 离散参数 ±1，确保 ≥ 1
        atr_vals = [max(1, atr_c - 1), atr_c, atr_c + 1]
        break_vals = [max(1, break_c - 1), break_c, break_c + 1]
        stop_vals = [max(1, stop_c - 1), stop_c, stop_c + 1]

        # 去重
        atr_vals = sorted(set(atr_vals))
        break_vals = sorted(set(break_vals))
        stop_vals = sorted(set(stop_vals))

        n_combo = len(atr_vals) * len(break_vals) * len(stop_vals)
        logger.info("  邻域: atr=%s, breakout=%s, stop=%s (%d 组合 × %d 模式 = %d 次回测)",
                     atr_vals, break_vals, stop_vals, n_combo, len(modes), n_combo * len(modes))

        for mode in modes:
            for a in atr_vals:
                for b in break_vals:
                    for s in stop_vals:
                        params = {
                            "atr_period": a,
                            "breakout_period": b,
                            "stop_period": s,
                            "stop_atr_multiple": mult_c,
                            "alpha": alpha_c,
                            "max_cumulative_loss_pct": max_loss,
                            "max_consecutive_losses": max_consec,
                        }
                        rid = run_counter
                        run_counter += 1
                        task_map[rid] = (params, mode, idx)
                        all_tasks.append((params, mode, start_date, end_date, rid))

    # ── 并行/串行执行所有回测 ──
    logger.info("稳定性扫描: %d 次回测, workers=%d", len(all_tasks), workers)
    raw_results = _run_tasks(all_tasks, workers, verbose=False)

    # ── 按 run_id 匹配回参数组装 DataFrame ──
    for result in raw_results:
        if result is None:
            continue
        rid = result.get("run_id")
        if rid is None or rid not in task_map:
            continue
        params, mode, idx = task_map[rid]
        entry = {
            "param_idx": idx,
            "mode": mode,
            "atr_period": params["atr_period"],
            "breakout_period": params["breakout_period"],
            "stop_period": params["stop_period"],
            "stop_atr_multiple": params["stop_atr_multiple"],
            "alpha": params["alpha"],
            "sharpe": result.get("sharpe"),
            "cagr": result.get("cagr"),
            "max_drawdown": result.get("max_drawdown"),
            "total_trades": result.get("total_trades"),
            "calmar": result.get("calmar"),
            "annual_vol": result.get("annual_vol"),
        }
        all_rows.append(entry)

    if not all_rows:
        logger.warning("稳定性扫描未产生任何有效结果")
        return pd.DataFrame()

    df_stab = pd.DataFrame(all_rows)

    # ── 打印汇总 ──
    logger.info("")
    logger.info("稳定性扫描汇总")
    logger.info("-" * 80)

    groups = df_stab.groupby(["mode", "param_idx"])
    for (mode_val, pidx), grp in groups:
        sharpe_vals = grp["sharpe"].dropna()
        cagr_vals = grp["cagr"].dropna()
        mdd_vals = grp["max_drawdown"].dropna()

        if len(sharpe_vals) < 2:
            continue

        # 从 df_best 查找中心参数
        best_row = df_best.iloc[pidx]
        bc_atr = int(best_row["atr_period"])
        bc_break = int(best_row["breakout_period"])
        bc_stop = int(best_row["stop_period"])

        # 中心点指标
        center = grp[
            (grp["atr_period"] == bc_atr)
            & (grp["breakout_period"] == bc_break)
            & (grp["stop_period"] == bc_stop)
        ]
        center_sharpe = center["sharpe"].values[0] if len(center) > 0 and pd.notna(center["sharpe"].values[0]) else 0
        center_cagr = center["cagr"].values[0] if len(center) > 0 and pd.notna(center["cagr"].values[0]) else 0

        # 邻域分布
        sharpe_p25 = sharpe_vals.quantile(0.25)
        sharpe_p75 = sharpe_vals.quantile(0.75)
        sharpe_min = sharpe_vals.min()
        sharpe_max = sharpe_vals.max()
        sharpe_iqr = sharpe_p75 - sharpe_p25

        # 峰值判断: 邻域中 ≥50% 组合 Sharpe < 中心Sharpe × 0.7 → 尖锐
        poor_count = (sharpe_vals < center_sharpe * 0.7).sum()
        poor_pct = poor_count / len(sharpe_vals) * 100
        sharp_flag = " 🔴 尖锐峰值" if poor_pct >= 50 else ""

        logger.info(
            "  [%s param#%d] 邻域 %d 组 | 中心Sharpe=%.3f CAGR=%.2f%%",
            mode_val, pidx, len(sharpe_vals), center_sharpe, center_cagr,
        )
        logger.info(
            "    Sharpe 分布: P25=%.3f P50=%.3f P75=%.3f 区间=[%.3f, %.3f] IQR=%.3f",
            sharpe_p25, sharpe_vals.median(), sharpe_p75,
            sharpe_min, sharpe_max, sharpe_iqr,
        )
        logger.info(
            "    CAGR 分布: P25=%.2f%% P50=%.2f%% P75=%.2f%% 区间=[%.2f%%, %.2f%%]",
            cagr_vals.quantile(0.25), cagr_vals.median(), cagr_vals.quantile(0.75),
            cagr_vals.min(), cagr_vals.max(),
        )
        logger.info(
            "    MDD 分布: P25=%.2f%% P50=%.2f%% P75=%.2f%%",
            mdd_vals.quantile(0.25), mdd_vals.median(), mdd_vals.quantile(0.75),
        )
        logger.info(
            "    邻域劣化(Sharpe<70%%中心): %d/%d (%.0f%%)%s",
            poor_count, len(sharpe_vals), poor_pct, sharp_flag,
        )

    return df_stab


# ════════════════════════════════════════════════════════════
#  CLI 入口
# ════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="跨市场ETF海龟组合策略 — 参数网格搜索 (S6)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode", type=str, choices=["A", "B", "all"], default="all",
        help="搜索模式 (默认: all, AB 都跑)",
    )
    parser.add_argument(
        "--start", type=str, default="2020-01-01",
        help="样本内起始日期 (默认: 2020-01-01)",
    )
    parser.add_argument(
        "--split", type=str, default="2024-01-01",
        help="样本内/样本外分割日期 (默认: 2024-01-01)",
    )
    parser.add_argument(
        "--end", type=str, default="2026-06-10",
        help="样本外截止日期 (默认: 2026-06-10)",
    )
    parser.add_argument(
        "--rolling", action="store_true", default=False,
        help="启用滚动窗口检验 (默认关闭)",
    )
    parser.add_argument(
        "--stability", action="store_true", default=False,
        help="启用参数稳定性面扫描 (±1 邻域) (默认关闭)",
    )
    parser.add_argument(
        "--workers", "-w", type=int, default=4,
        help="并行进程数 (默认: 4)",
    )
    parser.add_argument(
        "--top", type=int, default=10,
        help="输出 Top-N 结果 (默认: 10)",
    )
    parser.add_argument(
        "--quick", action="store_true", default=False,
        help="快速验证模式 (仅抽样 10 组参数)",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="输出目录 (默认: results/grid_search/)",
    )
    parser.add_argument(
        "--plot", action="store_true", default=False,
        help="生成参数敏感性散点图",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", default=False,
        help="详细日志",
    )

    args = parser.parse_args()

    # ── 日志 ──
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # ── 模式 ──
    modes = ["A", "B"] if args.mode == "all" else [args.mode]

    # ── 输出目录 ──
    global OUTPUT_DIR
    if args.output:
        OUTPUT_DIR = Path(args.output)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 50)
    logger.info("S6 参数网格搜索")
    logger.info(f"  参数空间: {len(PARAM_GRID['atr_period'])}×{len(PARAM_GRID['breakout_period'])}"
                f"×{len(PARAM_GRID['stop_period'])}×{len(PARAM_GRID['stop_atr_multiple'])}"
                f"×{len(PARAM_GRID['alpha'])} = {len(build_param_grid())} 组")
    logger.info(f"  模式: {modes}")
    logger.info(f"  输出目录: {OUTPUT_DIR}")
    logger.info(f"  Workers: {args.workers}")
    logger.info(f"  日期: 样本内 {args.start}~{args.split}, 样本外 {args.split}~{args.end}")
    logger.info("=" * 50)

    # ── 运行网格搜索 ──
    df_full, df_oos, df_best = run_grid_search(
        modes=modes,
        start_date=args.start,
        split_date=args.split,
        end_date=args.end,
        workers=args.workers,
        quick=args.quick,
        verbose=args.verbose,
    )

    if df_full.empty:
        logger.error("网格搜索未产生任何结果")
        sys.exit(1)

    # ── 绘图（可选） ──
    if args.plot:
        plot_results(pd.concat([df_full, df_oos], ignore_index=True), OUTPUT_DIR)

    # ── 滚动窗口检验（可选） ──
    if args.rolling:
        logger.info("")
        logger.info("=" * 60)
        logger.info("运行滚动窗口检验...")
        df_roll = run_rolling_validation(
            df_best,
            modes=modes,
            base_start=args.start,
            base_end=args.end,
            workers=args.workers,
        )
        if not df_roll.empty:
            roll_path = OUTPUT_DIR / "rolling_validation.csv"
            df_roll.to_csv(roll_path, index=False, encoding="utf-8")
            logger.info("滚动窗口结果已保存: %s", roll_path)

    # ── 参数稳定性面扫描（可选） ──
    if args.stability:
        logger.info("")
        logger.info("=" * 60)
        logger.info("运行参数稳定性面扫描...")
        df_stab = run_stability_scan(
            df_best,
            modes=modes,
            start_date=args.start,
            end_date=args.end,
            workers=args.workers,
        )
        if not df_stab.empty:
            stab_path = OUTPUT_DIR / "stability_scan.csv"
            df_stab.to_csv(stab_path, index=False, encoding="utf-8")
            logger.info("稳定性扫描结果已保存: %s", stab_path)

    # ── 汇总 ──
    print()
    print("=" * 60)
    print("S6 网格搜索完成")
    print("=" * 60)
    print(f"  样本内结果: {OUTPUT_DIR / 'grid_results_full.csv'}")
    print(f"  样本外验证: {OUTPUT_DIR / 'oos_validation.csv'}")
    print(f"  最优参数:   {OUTPUT_DIR / 'best_params.json'}")
    if args.rolling:
        print(f"  滚动窗口:   {OUTPUT_DIR / 'rolling_validation.csv'}")
    if args.stability:
        print(f"  稳定性扫描: {OUTPUT_DIR / 'stability_scan.csv'}")
    print()
    print("  推荐手动步骤：")
    print("    1. 查看 best_params.json 选取最终参数")
    print("    2. 用最终参数运行 S7 压力测试")
    print("    3. 对比 S9 Dry-Run 实际表现")
    print("=" * 60)


if __name__ == "__main__":
    main()
