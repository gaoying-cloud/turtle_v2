#!/usr/bin/env python
"""
跨市场ETF海龟组合策略 · 参数网格搜索 (S6)

在 5 个核心参数 + 2 个风控参数上做笛卡尔积网格搜索，覆盖模式 A/B，
包含样本内/样本外分割验证和稳健性评分。
支持 multiprocessing 并行加速。

用法：
  py scripts/run_grid_search.py             # 全量搜索（3645 组 × 2 模式 = 7290 次）
  py scripts/run_grid_search.py --mode A    # 仅模式 A
  py scripts/run_grid_search.py --quick     # 快速验证（抽样 10 组）
  py scripts/run_grid_search.py --workers 4 # 4 核并行
  py scripts/run_grid_search.py --rolling   # 滚动窗口检验
  py scripts/run_grid_search.py --stability # 参数稳定性面扫描
  py scripts/run_grid_search.py --plot      # 生成敏感性图
  py scripts/run_grid_search.py --two-stage # 两阶段搜索（粗筛+精搜，大幅缩短时间）
  py scripts/run_grid_search.py --weight-search # 品种级权重参数搜索
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
from datetime import datetime
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

from strategies.turtle_trading import TurtleStrategy
from src.config_loader import get_shortable_symbols, get_t_plus_one_symbols, get_trading_symbols

from src.data_utils import align_to_common_dates

logger = logging.getLogger(__name__)

# ── 路径 ──
CONFIG_PATH = ROOT / "config" / "turtle_config.yaml"
DATA_DIR = ROOT / "data" / "etf_daily"
OUTPUT_DIR = ROOT / "results" / "grid_search"

# 从统一配置读取品种列表
with open(CONFIG_PATH, "r", encoding="utf-8") as _f:
    _CONFIG_INIT = yaml.safe_load(_f)

SIX_SYMBOLS = get_trading_symbols(_CONFIG_INIT)

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

# ── 品种级权重倍率参数空间（用于 Stage-2 最优权重搜索）──
# key = symbol_code, values = [multiplier candidates]
WEIGHT_PARAM_GRID = {
    "513100.SH": [1.0, 1.5, 2.0, 2.5],   # 纳指ETF 超配
    "159985.SZ": [0.2, 0.3, 0.5, 1.0],   # 豆粕ETF 低配
}

# ════════════════════════════════════════════════════════════
# 0. 缓存与初始化 (性能优化核心)
# ════════════════════════════════════════════════════════════
_DATA_CACHE: dict[str, pd.DataFrame] = {}
_CONFIG_CACHE: dict = {}

def _get_config() -> dict:
    """读取 YAML 配置（带缓存，避免每次回测重复读盘解析）"""
    if not _CONFIG_CACHE:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            _CONFIG_CACHE.update(yaml.safe_load(f))
    return _CONFIG_CACHE

def _load_data_cached(symbol: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
    """带缓存的数据加载：同进程内只读一次 Parquet，后续按日期切片"""
    if symbol not in _DATA_CACHE:
        path = DATA_DIR / f"{symbol}.parquet"
        if not path.exists():
            logger.error("缓存文件不存在: %s\n请先运行 py scripts/pull_data.py", path)
            return None
        _DATA_CACHE[symbol] = pd.read_parquet(path)

    df = _DATA_CACHE[symbol]
    if df.empty:
        return None

    mask = (df["date"] >= start_date) & (df["date"] <= end_date)
    df_slice = df[mask].copy()
    if df_slice.empty:
        logger.warning("[%s] 在 %s~%s 区间无数据", symbol, start_date, end_date)
        return None

    df_slice.sort_values("date", inplace=True)
    df_slice.reset_index(drop=True, inplace=True)
    return df_slice

def _worker_init():
    """子进程初始化函数：预加载数据和配置到进程内存"""
    warnings.filterwarnings("ignore", category=UserWarning)
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    logging.getLogger("strategies.turtle_trading").setLevel(logging.WARNING)
    logging.getLogger("src.risk_parity").setLevel(logging.WARNING)

    # 强制预加载所有数据到该子进程的内存
    for symbol in SIX_SYMBOLS:
        _load_data_cached(symbol, "2014-01-01", "2026-12-31")

# ════════════════════════════════════════════════════════════
# 1. 参数网格构建
# ════════════════════════════════════════════════════════════
def build_param_grid(use_two_stage: bool = False) -> List[dict]:
    """生成参数笛卡尔积。

    Parameters
    ----------
    use_two_stage : bool
        如果为 True，返回第一阶段粗筛网格（固定风控参数，405组）。
        如果为 False，返回全量网格（3645组）。
    """
    if use_two_stage:
        grid_dict = {
            "atr_period": PARAM_GRID["atr_period"],
            "breakout_period": PARAM_GRID["breakout_period"],
            "stop_period": PARAM_GRID["stop_period"],
            "stop_atr_multiple": PARAM_GRID["stop_atr_multiple"],
            "alpha": PARAM_GRID["alpha"],
            "max_cumulative_loss_pct": [0.15],  # 固定
            "max_consecutive_losses": [8],      # 固定
        }
    else:
        grid_dict = PARAM_GRID

    keys = list(grid_dict.keys())
    values = list(grid_dict.values())
    grid = []
    for combo in itertools.product(*values):
        grid.append(dict(zip(keys, combo)))
    return grid

# ════════════════════════════════════════════════════════════
# 2. 单次回测运行
# ════════════════════════════════════════════════════════════
def df_to_feed(df: pd.DataFrame, symbol: str,
               common_dates: pd.DatetimeIndex | None = None) -> bt.feeds.PandasData:
    """将 pandas DataFrame 转换为 Backtrader PandasData feed。"""
    feed_df = df[["date", "open", "high", "low", "close", "volume"]].copy()
    feed_df["date"] = pd.to_datetime(feed_df["date"])
    if common_dates is not None:
        feed_df = feed_df.set_index("date").reindex(common_dates).ffill().bfill()
    else:
        feed_df.set_index("date", inplace=True)
    return bt.feeds.PandasData(
        dataname=feed_df, open="open", high="high", low="low",
        close="close", volume="volume", plot=False,
    )

def run_single_backtest(
    params: dict, mode: str, start_date: str, end_date: str, run_id: int = 0
) -> Optional[dict]:
    """运行单次回测，返回标准化指标字典。"""
    for key in ["atr_period", "breakout_period", "stop_period", "stop_atr_multiple", "alpha", "max_cumulative_loss_pct", "max_consecutive_losses"]:
        if key not in params:
            logger.error("参数缺少 %s", key)
            return None

    config = _get_config()

    # 使用缓存加载数据
    df_dict: dict[str, pd.DataFrame] = {}
    for symbol in SIX_SYMBOLS:
        df = _load_data_cached(symbol, start_date, end_date)
        if df is None:
            logger.error("[run_id=%d] 品种 %s 数据加载失败", run_id, symbol)
            return None
        df_dict[symbol] = df
    df_dict = align_to_common_dates(df_dict)
    feeds: dict[str, bt.feeds.PandasData] = {}
    for symbol in SIX_SYMBOLS:
        feed = df_to_feed(df_dict[symbol], symbol)
        feed._name = symbol
        feeds[symbol] = feed

    cerebro = bt.Cerebro()
    for symbol in SIX_SYMBOLS:
        cerebro.adddata(feeds[symbol], name=symbol)

    cerebro.broker.setcash(config["initial_cash"])
    commission = config["commission_pct"]
    slippage = config["slippage_pct"]
    cerebro.broker.setcommission(commission=commission + slippage)

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

    # 构建品种级权重倍率（从 params 中的 weight_* 键转换）
    weight_multipliers = {}
    for key, val in params.items():
        if key.startswith("weight_") and isinstance(val, (int, float)):
            symbol_code = key[len("weight_"):].replace("_", ".")
            weight_multipliers[symbol_code] = float(val)

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
        weight_multipliers=weight_multipliers,
    )

    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", timeframe=bt.TimeFrame.Days, annualize=True)
    cerebro.addanalyzer(bt.analyzers.AnnualReturn, _name="annual_return")
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
    cerebro.addanalyzer(bt.analyzers.VWR, _name="vwr")
    cerebro.addanalyzer(bt.analyzers.TimeReturn, _name="timereturn")

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
    final_value = cerebro.broker.getvalue()
    total_return = (final_value / initial_cash - 1) * 100

    n_years = (datetime.strptime(end_date, "%Y-%m-%d") - datetime.strptime(start_date, "%Y-%m-%d")).days / 365.25
    cagr = ((final_value / initial_cash) ** (1 / max(n_years, 0.1)) - 1) * 100 if n_years > 0 else 0.0

    sharpe = strat.analyzers.sharpe.get_analysis()
    sharpe_val = sharpe.get("sharperatio", None) if sharpe else None

    dd = strat.analyzers.drawdown.get_analysis()
    max_dd = dd.get("max", {}).get("drawdown", 0.0) if dd else 0.0

    trades = strat.analyzers.trades.get_analysis()
    total = trades.get("total", {}).get("total", 0) if trades else 0
    won = trades.get("won", {}).get("total", 0) if trades else 0
    lost = trades.get("lost", {}).get("total", 0) if trades else 0
    win_rate = (won / total * 100) if total > 0 else 0.0
    avg_win = abs(trades.get("won", {}).get("pnl", {}).get("average", 0)) if trades else 0
    avg_loss = abs(trades.get("lost", {}).get("pnl", {}).get("average", 0)) if trades else 0
    profit_factor = (avg_win / avg_loss) if avg_loss > 0 else 0.0

    # 年化波动率：从 TimeReturn 日收益率计算（rvol100 在 backtrader 中不产出）
    timeret = strat.analyzers.timereturn.get_analysis()
    daily_rets = list(timeret.values())
    if len(daily_rets) > 1:
        annual_vol = float(np.std(daily_rets) * np.sqrt(252) * 100)
    else:
        annual_vol = 0.0

    calmar = (cagr / abs(max_dd)) if max_dd > 0 else 0.0

    del cerebro, strat, results, feeds
    gc.collect()

    result = {
        "run_id": run_id, "mode": mode,
        "atr_period": params["atr_period"], "breakout_period": params["breakout_period"],
        "stop_period": params["stop_period"], "stop_atr_multiple": params["stop_atr_multiple"],
        "alpha": params["alpha"], "max_cumulative_loss_pct": params["max_cumulative_loss_pct"],
        "max_consecutive_losses": params["max_consecutive_losses"],
        "total_return": round(total_return, 4), "cagr": round(cagr, 4),
        "sharpe": round(sharpe_val, 4) if sharpe_val is not None else None,
        "max_drawdown": round(max_dd, 4), "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 4), "total_trades": total,
        "annual_vol": round(annual_vol, 4), "calmar": round(calmar, 4),
        "final_value": round(final_value, 2), "date_range": f"{start_date}~{end_date}",
    }
    # 记录权重倍率参数（weight_*），使权重搜索结果可追溯
    for key, val in params.items():
        if key.startswith("weight_"):
            result[key] = float(val) if isinstance(val, (int, float)) else val
    return result

# ════════════════════════════════════════════════════════════
# 3. 多进程 Worker
# ════════════════════════════════════════════════════════════
def _worker(task: tuple) -> Optional[dict]:
    """多进程 worker"""
    params, mode, start_date, end_date, run_id = task
    return run_single_backtest(params, mode, start_date, end_date, run_id)

# ════════════════════════════════════════════════════════════
# 4. 网格搜索主循环
# ════════════════════════════════════════════════════════════
def run_grid_search(
    *, modes: List[str] = MODES, start_date: str = "2014-01-01",
    split_date: str = "2024-01-01", end_date: str = "2026-06-10",
    workers: int = 1, quick: bool = False, verbose: bool = False,
    two_stage: bool = False,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """运行完整网格搜索。"""

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    param_grid = build_param_grid(use_two_stage=two_stage)

    if quick:
        rng = np.random.default_rng(42)
        indices = rng.choice(len(param_grid), size=min(10, len(param_grid)), replace=False)
        param_grid = [param_grid[i] for i in indices]
        logger.info("快速模式：抽样 %d 组参数", len(param_grid))

    logger.info("参数组合: %d 组 × %d 模式 = %d 次回测", len(param_grid), len(modes), len(param_grid) * len(modes))

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
    top_n = 20 if two_stage else 10  # 两阶段搜索取 Top-20 进入阶段二
    df_best = evaluate_results(df_full, top_n=top_n)

    # 如果是两阶段搜索，执行阶段二：风控参数精搜
    if two_stage and not df_best.empty:
        logger.info("=" * 50)
        logger.info("执行两阶段搜索 [阶段二]：对 Top-%d 组合精搜风控参数", top_n)
        logger.info("=" * 50)

        stage2_tasks = []
        run_id = 90000  # 避免与之前冲突
        for _, row in df_best.iterrows():
            base_params = {
                "atr_period": int(row["atr_period"]),
                "breakout_period": int(row["breakout_period"]),
                "stop_period": int(row["stop_period"]),
                "stop_atr_multiple": float(row["stop_atr_multiple"]),
                "alpha": float(row["alpha"]),
            }
            # 遍历风控参数组合
            for mcl in PARAM_GRID["max_cumulative_loss_pct"]:
                for mcl_loss in PARAM_GRID["max_consecutive_losses"]:
                    p = {**base_params, "max_cumulative_loss_pct": mcl, "max_consecutive_losses": mcl_loss}
                    for mode in modes:
                        stage2_tasks.append((p, mode, start_date, split_date, run_id))
                        run_id += 1

        logger.info("阶段二: %d 次回测", len(stage2_tasks))
        stage2_results = _run_tasks(stage2_tasks, workers, verbose)
        df_stage2 = pd.DataFrame(stage2_results)

        # 合并阶段一和阶段二结果，重新评估
        df_full = pd.concat([df_full, df_stage2], ignore_index=True)
        df_best = evaluate_results(df_full, top_n=10)

        full_path2 = OUTPUT_DIR / "grid_results_full_with_stage2.csv"
        df_full.to_csv(full_path2, index=False, encoding="utf-8")
        logger.info("含阶段二的全量结果已保存: %s", full_path2)

    best_path = OUTPUT_DIR / "best_params.json"
    _save_best_params_json(df_best, best_path)
    logger.info("最优参数已保存: %s", best_path)

    # ── 样本外验证 Top-N ──
    logger.info("=" * 50)
    logger.info("样本外验证: %s ~ %s", split_date, end_date)
    logger.info("=" * 50)

    oos_tasks = []
    run_id = 100000
    for _, row in df_best.iterrows():
        params = {
            "atr_period": int(row["atr_period"]), "breakout_period": int(row["breakout_period"]),
            "stop_period": int(row["stop_period"]), "stop_atr_multiple": float(row["stop_atr_multiple"]),
            "alpha": float(row["alpha"]), "max_cumulative_loss_pct": float(row.get("max_cumulative_loss_pct", 0.15)),
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
        # 关键优化：使用 initializer 在子进程启动时预加载数据
        with ProcessPoolExecutor(max_workers=workers, initializer=_worker_init) as executor:
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
                    logger.info(" 进度: %d / %d (%.0f%%)", done, n_total, done / n_total * 100)
    else:
        logger.info("串行执行")
        _worker_init()  # 串行时在主进程预加载
        for i, task in enumerate(tasks):
            result = _worker(task)
            if result is not None:
                results.append(result)
            if (i + 1) == 1 or (i + 1) % 50 == 0 or (i + 1) == n_total:
                logger.info(" 进度: %d / %d (%.0f%%)", i + 1, n_total, (i + 1) / n_total * 100)

    logger.info("完成 %d / %d 次回测", len(results), n_total)
    return results

# ════════════════════════════════════════════════════════════
# 5. 结果评估
# ════════════════════════════════════════════════════════════
def evaluate_results(df: pd.DataFrame, top_n: int = 10) -> pd.DataFrame:
    """评估网格搜索结果，按稳健性评分排序。"""
    if df.empty:
        return pd.DataFrame()

    df_valid = df.dropna(subset=["sharpe"]).copy()
    all_sharpe_nan = False
    if df_valid.empty:
        df_valid = df.copy()
        all_sharpe_nan = True

    scaler = _robust_scaler
    if all_sharpe_nan:
        df_valid["_cagr_score"] = scaler(df_valid["cagr"].astype(float))
        df_valid["_trade_score"] = scaler(np.log1p(df_valid["total_trades"].astype(float)))
        df_valid["_dd_score"] = scaler(-df_valid["max_drawdown"].astype(float))
        df_valid["robustness_score"] = 0.55 * df_valid["_cagr_score"] + 0.15 * df_valid["_trade_score"] + 0.30 * df_valid["_dd_score"]
    else:
        df_valid["_sharpe_score"] = scaler(df_valid["sharpe"].astype(float))
        df_valid["_calmar_score"] = scaler(df_valid["calmar"].astype(float))
        df_valid["_cagr_score"] = scaler(df_valid["cagr"].astype(float))
        df_valid["_dd_score"] = scaler(-df_valid["max_drawdown"].astype(float))
        df_valid["_trade_score"] = scaler(np.log1p(df_valid["total_trades"].astype(float)))
        df_valid["robustness_score"] = 0.35 * df_valid["_sharpe_score"] + 0.25 * df_valid["_calmar_score"] + 0.20 * df_valid["_cagr_score"] + 0.15 * df_valid["_dd_score"] + 0.05 * df_valid["_trade_score"]

    best_groups = []
    for mode_val, group in df_valid.groupby("mode"):
        top = group.nlargest(top_n, "robustness_score")
        best_groups.append(top)

    df_best = pd.concat(best_groups, ignore_index=True)
    df_best.sort_values(["mode", "robustness_score"], ascending=[True, False], inplace=True)

    cols = ["run_id", "mode", "atr_period", "breakout_period", "stop_period", "stop_atr_multiple", "alpha", "max_cumulative_loss_pct", "max_consecutive_losses", "total_return", "cagr", "sharpe", "max_drawdown", "win_rate", "profit_factor", "total_trades", "annual_vol", "calmar", "robustness_score"]
    df_best = df_best[[c for c in cols if c in df_best.columns]]
    df_best.reset_index(drop=True, inplace=True)

    logger.info("最优参数组合（Top-%d）：", top_n)
    for _, row in df_best.iterrows():
        logger.info(" [%s] atr=%d breakout=%d stop=%d mult=%.1f α=%.2f | sharpe=%.3f cagr=%.2f%% mdd=%.2f%% trades=%d score=%.4f",
            row["mode"], int(row["atr_period"]), int(row["breakout_period"]), int(row["stop_period"]),
            float(row["stop_atr_multiple"]), float(row["alpha"]),
            row["sharpe"] if pd.notna(row["sharpe"]) else 0, row["cagr"], row["max_drawdown"],
            int(row["total_trades"]), row["robustness_score"])

    return df_best

def _robust_scaler(series: pd.Series) -> pd.Series:
    med = series.median()
    iqr = series.quantile(0.75) - series.quantile(0.25)
    if iqr == 0 or np.isnan(iqr):
        return series * 0.0
    return (series - med) / iqr

def _save_best_params_json(df_best: pd.DataFrame, path: Path):
    records = df_best.to_dict(orient="records")
    output = []
    for r in records:
        output.append({
            "mode": r.get("mode"), "max_cumulative_loss_pct": float(r.get("max_cumulative_loss_pct", 0.15)),
            "max_consecutive_losses": int(r.get("max_consecutive_losses", 8)),
            "atr_period": int(r.get("atr_period", 20)), "breakout_period": int(r.get("breakout_period", 20)),
            "stop_period": int(r.get("stop_period", 10)), "stop_atr_multiple": float(r.get("stop_atr_multiple", 2.0)),
            "alpha": float(r.get("alpha", 0.05)), "sharpe": r.get("sharpe"), "cagr": r.get("cagr"),
            "max_drawdown": r.get("max_drawdown"), "total_trades": int(r.get("total_trades", 0)),
            "robustness_score": r.get("robustness_score", 0),
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

# ════════════════════════════════════════════════════════════
# 6. 图表（可选）
# ════════════════════════════════════════════════════════════
def plot_results(df: pd.DataFrame, output_dir: Path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return

    if df.empty: return
    param_keys = ["atr_period", "breakout_period", "stop_period", "stop_atr_multiple", "alpha"]
    fig, axes = plt.subplots(2, 3, figsize=(15, 8))
    axes = axes.flatten()
    for i, key in enumerate(param_keys):
        ax = axes[i]
        for mode_val, group in df.groupby("mode"):
            ax.scatter(group[key], group["sharpe"], label=f"模式 {mode_val}", alpha=0.5, s=10)
        ax.set_xlabel(key); ax.set_ylabel("Sharpe"); ax.legend(fontsize=8); ax.grid(True, alpha=0.3)
    axes[-1].axis("off")
    plt.suptitle("参数敏感性分析", fontsize=14); plt.tight_layout()
    plt.savefig(output_dir / "sensitivity_sharpe.png", dpi=150); plt.close()

# ════════════════════════════════════════════════════════════
# 7. 滚动窗口验证
# ════════════════════════════════════════════════════════════
def run_rolling_validation(df_best: pd.DataFrame, *, modes: List[str] = MODES, base_start: str = "2014-01-01", base_end: str = "2026-06-10", workers: int = 1) -> pd.DataFrame:
    windows = [
        ("W1", "2014-01-01", "2022-12-31", "2023-01-01", "2023-12-31"),
        ("W2", "2021-01-01", "2023-12-31", "2024-01-01", "2024-12-31"),
        ("W3", "2022-01-01", "2024-12-31", "2025-01-01", base_end),
    ]

    all_tasks, task_meta, run_counter = [], {}, 20000

    for idx, (_, row) in enumerate(df_best.iterrows()):
        params = {
            "atr_period": int(row["atr_period"]), "breakout_period": int(row["breakout_period"]),
            "stop_period": int(row["stop_period"]), "stop_atr_multiple": float(row["stop_atr_multiple"]),
            "alpha": float(row["alpha"]), "max_cumulative_loss_pct": float(row.get("max_cumulative_loss_pct", 0.15)),
            "max_consecutive_losses": int(row.get("max_consecutive_losses", 8)),
        }
        for mode in modes:
            for wname, is_s, is_e, oos_s, oos_e in windows:
                rid = run_counter; run_counter += 1; task_meta[rid] = (params, mode, idx, wname, "is")
                all_tasks.append((params, mode, is_s, is_e, rid))
                rid = run_counter; run_counter += 1; task_meta[rid] = (params, mode, idx, wname, "oos")
                all_tasks.append((params, mode, oos_s, oos_e, rid))

    raw_results = _run_tasks(all_tasks, workers, verbose=False)
    roll_map = {}
    for result in raw_results:
        if result is None: continue
        rid = result.get("run_id")
        if rid in task_meta:
            params, mode, idx, wname, phase = task_meta[rid]
            key = (idx, mode, wname)
            if key not in roll_map: roll_map[key] = {"params": params}
            roll_map[key][f"{phase}_result"] = result

    all_rows = []
    for (idx, mode, wname), data in roll_map.items():
        is_r, oos_r = data.get("is_result"), data.get("oos_result")
        if is_r is None or oos_r is None: continue
        all_rows.append({
            "window": wname, "mode": mode, "param_idx": idx,
            "atr_period": data["params"]["atr_period"], "breakout_period": data["params"]["breakout_period"],
            "stop_period": data["params"]["stop_period"], "stop_atr_multiple": data["params"]["stop_atr_multiple"],
            "alpha": data["params"]["alpha"], "is_sharpe": is_r.get("sharpe"), "is_cagr": is_r.get("cagr"),
            "is_mdd": is_r.get("max_drawdown"), "is_trades": is_r.get("total_trades"),
            "oos_sharpe": oos_r.get("sharpe"), "oos_cagr": oos_r.get("cagr"),
            "oos_mdd": oos_r.get("max_drawdown"), "oos_trades": oos_r.get("total_trades"),
        })

    if not all_rows: return pd.DataFrame()
    return pd.DataFrame(all_rows)

# ════════════════════════════════════════════════════════════
# 8. 参数稳定性面扫描 (±1 邻域)
# ════════════════════════════════════════════════════════════
def run_stability_scan(df_best: pd.DataFrame, *, modes: List[str] = MODES, start_date: str = "2014-01-01", end_date: str = "2026-06-10", workers: int = 1) -> pd.DataFrame:
    all_tasks, task_map, run_counter = [], {}, 10000

    for idx, (_, row) in enumerate(df_best.iterrows()):
        atr_c, break_c, stop_c = int(row["atr_period"]), int(row["breakout_period"]), int(row["stop_period"])
        mult_c, alpha_c = float(row["stop_atr_multiple"]), float(row["alpha"])
        max_loss, max_consec = float(row.get("max_cumulative_loss_pct", 0.15)), int(row.get("max_consecutive_losses", 8))

        atr_vals = sorted(set([max(1, atr_c-1), atr_c, atr_c+1]))
        break_vals = sorted(set([max(1, break_c-1), break_c, break_c+1]))
        stop_vals = sorted(set([max(1, stop_c-1), stop_c, stop_c+1]))

        for mode in modes:
            for a in atr_vals:
                for b in break_vals:
                    for s in stop_vals:
                        params = {"atr_period": a, "breakout_period": b, "stop_period": s, "stop_atr_multiple": mult_c, "alpha": alpha_c, "max_cumulative_loss_pct": max_loss, "max_consecutive_losses": max_consec}
                        rid = run_counter; run_counter += 1; task_map[rid] = (params, mode, idx)
                        all_tasks.append((params, mode, start_date, end_date, rid))

    raw_results = _run_tasks(all_tasks, workers, verbose=False)
    all_rows = []
    for result in raw_results:
        if result is None: continue
        rid = result.get("run_id")
        if rid in task_map:
            params, mode, idx = task_map[rid]
            all_rows.append({
                "param_idx": idx, "mode": mode, "atr_period": params["atr_period"], "breakout_period": params["breakout_period"],
                "stop_period": params["stop_period"], "stop_atr_multiple": params["stop_atr_multiple"], "alpha": params["alpha"],
                "sharpe": result.get("sharpe"), "cagr": result.get("cagr"), "max_drawdown": result.get("max_drawdown"),
                "total_trades": result.get("total_trades"), "calmar": result.get("calmar"), "annual_vol": result.get("annual_vol"),
            })

    if not all_rows: return pd.DataFrame()
    return pd.DataFrame(all_rows)

# ════════════════════════════════════════════════════════════
# 9. 品种级权重倍率搜索（Stage-2）
# ════════════════════════════════════════════════════════════
def run_weight_search(
    best_params: dict, modes: List[str] = MODES,
    start_date: str = "2014-01-01", split_date: str = "2024-01-01",
    end_date: str = "2026-06-10", workers: int = 1,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """在最优核心参数基础上，搜索品种级权重倍率。

    固定核心参数（atr_period / breakout_period / stop_period 等），
    仅搜索 WEIGHT_PARAM_GRID 中定义的品种倍率组合。

    Returns
    -------
    (df_is, df_oos) — 样本内 + 样本外结果 DataFrame
    """
    base = {
        "atr_period": int(best_params["atr_period"]),
        "breakout_period": int(best_params["breakout_period"]),
        "stop_period": int(best_params["stop_period"]),
        "stop_atr_multiple": float(best_params["stop_atr_multiple"]),
        "alpha": float(best_params["alpha"]),
        "max_cumulative_loss_pct": float(best_params.get("max_cumulative_loss_pct", 0.15)),
        "max_consecutive_losses": int(best_params.get("max_consecutive_losses", 8)),
    }

    # 构建权重倍率笛卡尔积
    weight_keys = list(WEIGHT_PARAM_GRID.keys())
    weight_values = list(WEIGHT_PARAM_GRID.values())
    weight_combos = list(itertools.product(*weight_values))

    logger.info("=" * 50)
    logger.info("Stage-2 权重搜索: %d 种倍率组合 × %d 模式 = %d 次回测",
                len(weight_combos), len(modes), len(weight_combos) * len(modes))
    logger.info("  倍率参数: %s", WEIGHT_PARAM_GRID)
    logger.info("  固定核心参数: atr=%d b=%d s=%d m=%.1f α=%.2f",
                base["atr_period"], base["breakout_period"], base["stop_period"],
                base["stop_atr_multiple"], base["alpha"])
    logger.info("=" * 50)

    # ── 样本内 ──
    is_tasks = []
    run_id = 50000
    for combo in weight_combos:
        params = dict(base)
        for i, sym in enumerate(weight_keys):
            key = f"weight_{sym.replace('.', '_')}"
            params[key] = combo[i]
        for mode in modes:
            is_tasks.append((params, mode, start_date, split_date, run_id))
            run_id += 1

    is_results = _run_tasks(is_tasks, workers, verbose=False)
    df_is = pd.DataFrame(is_results)

    # 选择最优倍率组合
    scorer = lambda r: r.get("sharpe", 0) or 0
    is_results_sorted = sorted(is_results, key=scorer, reverse=True)
    best_weight_result = is_results_sorted[0] if is_results_sorted else None

    if best_weight_result is None:
        logger.error("权重搜索未产生有效结果")
        return pd.DataFrame(), pd.DataFrame()

    # 输出最优倍率
    best_combo = {}
    for key, val in best_weight_result.items():
        if key.startswith("weight_"):
            best_combo[key] = val
    logger.info("最优权重倍率: %s (Sharpe=%.4f, CAGR=%.2f%%, MDD=%.2f%%)",
                best_combo, best_weight_result.get("sharpe", 0),
                best_weight_result.get("cagr", 0), best_weight_result.get("max_drawdown", 0))

    # ── 样本外验证 ──
    oos_tasks = []
    run_id = 60000
    for combo in weight_combos:
        params = dict(base)
        for i, sym in enumerate(weight_keys):
            key = f"weight_{sym.replace('.', '_')}"
            params[key] = combo[i]
        for mode in modes:
            oos_tasks.append((params, mode, split_date, end_date, run_id))
            run_id += 1

    oos_results = _run_tasks(oos_tasks, workers, verbose=False)
    df_oos = pd.DataFrame(oos_results)

    # 保存
    is_path = OUTPUT_DIR / "weight_search_is.csv"
    oos_path = OUTPUT_DIR / "weight_search_oos.csv"
    df_is.to_csv(is_path, index=False, encoding="utf-8")
    df_oos.to_csv(oos_path, index=False, encoding="utf-8")
    logger.info("权重搜索样本内结果: %s (%d 行)", is_path, len(df_is))
    logger.info("权重搜索样本外结果: %s (%d 行)", oos_path, len(df_oos))

    return df_is, df_oos


# ════════════════════════════════════════════════════════════
# CLI 入口
# ════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="跨市场ETF海龟组合策略 — 参数网格搜索 (S6)", formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", type=str, choices=["A", "B", "all"], default="all", help="搜索模式 (默认: all)")
    parser.add_argument("--start", type=str, default="2014-01-01", help="样本内起始日期")
    parser.add_argument("--split", type=str, default="2024-01-01", help="样本内/样本外分割日期")
    parser.add_argument("--end", type=str, default="2026-06-10", help="样本外截止日期")
    parser.add_argument("--rolling", action="store_true", default=False, help="启用滚动窗口检验")
    parser.add_argument("--stability", action="store_true", default=False, help="启用参数稳定性面扫描")
    parser.add_argument("--weight-search", action="store_true", default=False, help="启用 Stage-2 品种权重倍率搜索")
    parser.add_argument("--weight-only", action="store_true", default=False, help="仅运行权重倍率搜索（读取已有 best_params.json）")
    parser.add_argument("--workers", "-w", type=int, default=4, help="并行进程数 (默认: 4)")
    parser.add_argument("--top", type=int, default=10, help="输出 Top-N 结果")
    parser.add_argument("--quick", action="store_true", default=False, help="快速验证模式")
    parser.add_argument("--two-stage", action="store_true", default=False, help="启用两阶段搜索(粗筛+精搜)，大幅缩短时间")
    parser.add_argument("--output", type=str, default=None, help="输出目录")
    parser.add_argument("--plot", action="store_true", default=False, help="生成参数敏感性散点图")
    parser.add_argument("--verbose", "-v", action="store_true", default=False, help="详细日志")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    modes = ["A", "B"] if args.mode == "all" else [args.mode]

    global OUTPUT_DIR
    if args.output:
        OUTPUT_DIR = Path(args.output)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 修正参数空间显示
    grid_size = len(build_param_grid(use_two_stage=args.two_stage))
    grid_desc = "3×3×3×3×5×3×3" if not args.two_stage else "3×3×3×3×5 (阶段一) + 3×3 (阶段二)"

    # ── --weight-only：跳过主搜索，仅运行权重倍率搜索 ──
    if args.weight_only:
        logger.info("=" * 50)
        logger.info("权重倍率搜索（独立模式）")
        logger.info("  读取: %s", OUTPUT_DIR / "best_params.json")
        logger.info("=" * 50)

        best_path = OUTPUT_DIR / "best_params.json"
        if not best_path.exists():
            logger.error("不存在 best_params.json，请先运行网格搜索")
            sys.exit(1)

        with open(best_path, "r", encoding="utf-8") as f:
            best_records = json.load(f)
        if not best_records:
            logger.error("best_params.json 为空")
            sys.exit(1)

        best_row = best_records[0]
        logger.info("最优核心参数: atr=%d b=%d s=%d m=%.1f α=%.2f",
                     best_row["atr_period"], best_row["breakout_period"],
                     best_row["stop_period"], best_row["stop_atr_multiple"],
                     best_row["alpha"])
        logger.info("权重倍率搜索空间: %s", WEIGHT_PARAM_GRID)

        df_w_is, df_w_oos = run_weight_search(
            best_row, modes=modes,
            start_date=args.start, split_date=args.split, end_date=args.end,
            workers=args.workers,
        )

        if not df_w_is.empty:
            top_w = df_w_is.loc[df_w_is["sharpe"].idxmax()] if "sharpe" in df_w_is.columns else df_w_is.iloc[0]
            weight_summary = {k: v for k, v in top_w.to_dict().items() if k.startswith("weight_")}
            # 更新 best_params.json
            best_records[0].update(weight_summary)
            with open(best_path, "w", encoding="utf-8") as f:
                json.dump(best_records, f, ensure_ascii=False, indent=2)
            logger.info("最优权重倍率已合并到 %s", best_path)
            logger.info("最优权重: %s | Sharpe=%.4f CAGR=%.2f%% MDD=%.2f%%",
                        weight_summary, top_w.get("sharpe", 0),
                        top_w.get("cagr", 0), top_w.get("max_drawdown", 0))

        print("\n" + "=" * 60)
        print("权重搜索完成")
        print(f" 样本内: {OUTPUT_DIR / 'weight_search_is.csv'}")
        print(f" 样本外: {OUTPUT_DIR / 'weight_search_oos.csv'}")
        print("=" * 60)
        return

    logger.info("=" * 50)
    logger.info("S6 参数网格搜索")
    logger.info(f" 参数空间: {grid_desc} = {grid_size} 组")
    logger.info(f" 模式: {modes}")
    logger.info(f" 输出目录: {OUTPUT_DIR}")
    logger.info(f" Workers: {args.workers}")
    logger.info(f" 日期: 样本内 {args.start}~{args.split}, 样本外 {args.split}~{args.end}")
    logger.info("=" * 50)

    df_full, df_oos, df_best = run_grid_search(
        modes=modes, start_date=args.start, split_date=args.split, end_date=args.end,
        workers=args.workers, quick=args.quick, verbose=args.verbose, two_stage=args.two_stage
    )

    if df_full.empty:
        logger.error("网格搜索未产生任何结果")
        sys.exit(1)

    if args.plot:
        plot_results(pd.concat([df_full, df_oos], ignore_index=True), OUTPUT_DIR)

    if args.rolling:
        logger.info("\n" + "=" * 60)
        logger.info("运行滚动窗口检验...")
        df_roll = run_rolling_validation(df_best, modes=modes, base_start=args.start, base_end=args.end, workers=args.workers)
        if not df_roll.empty:
            df_roll.to_csv(OUTPUT_DIR / "rolling_validation.csv", index=False, encoding="utf-8")

    if args.stability:
        logger.info("\n" + "=" * 60)
        logger.info("运行参数稳定性面扫描...")
        df_stab = run_stability_scan(df_best, modes=modes, start_date=args.start, end_date=args.end, workers=args.workers)
        if not df_stab.empty:
            df_stab.to_csv(OUTPUT_DIR / "stability_scan.csv", index=False, encoding="utf-8")

    if args.weight_search and not df_best.empty:
        logger.info("\n" + "=" * 60)
        logger.info("运行 Stage-2 品种权重倍率搜索...")
        best_row = df_best.iloc[0].to_dict()
        df_w_is, df_w_oos = run_weight_search(
            best_row, modes=modes,
            start_date=args.start, split_date=args.split, end_date=args.end,
            workers=args.workers,
        )
        if not df_w_is.empty:
            # 将最优权重倍率写入 best_params.json
            top_w = df_w_is.loc[df_w_is["sharpe"].idxmax()] if "sharpe" in df_w_is.columns else df_w_is.iloc[0]
            weight_summary = {k: v for k, v in top_w.to_dict().items() if k.startswith("weight_")}
            best_path = OUTPUT_DIR / "best_params.json"
            if best_path.exists():
                with open(best_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if existing:
                    existing[0].update(weight_summary)
                    with open(best_path, "w", encoding="utf-8") as f:
                        json.dump(existing, f, ensure_ascii=False, indent=2)
                    logger.info("最优权重倍率已合并到 %s", best_path)

    print("\n" + "=" * 60)
    print("S6 网格搜索完成")
    print(f" 样本内结果: {OUTPUT_DIR / 'grid_results_full.csv'}")
    print(f" 样本外验证: {OUTPUT_DIR / 'oos_validation.csv'}")
    print(f" 最优参数: {OUTPUT_DIR / 'best_params.json'}")
    print("=" * 60)

if __name__ == "__main__":
    main()
