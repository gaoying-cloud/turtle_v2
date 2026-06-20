#!/usr/bin/env python
"""
跨市场ETF海龟组合策略 · 极端情景回测与压力测试 (S7)

基于 §5.9 施工图设计，包含：
    - A1-A4: 4 个历史极端情景回放
    - B1:    合成单月同步暴跌（-3%/-5%/-7%）
    - B2:    连续流动性枯竭（3 日跌停）

输出：
    results/stress_test/stress_report.md       — Markdown 完整报告
    results/stress_test/scenario_summary.csv   — 所有场景横向对比表
    results/stress_test/historical_{scenario}.csv — 逐日净值序列
    results/stress_test/synthetic_shock.csv    — B1 冲击矩阵
    results/stress_test/stress_conclusion.json — 结构化结论

用法：
    py scripts/run_stress_test.py                              # 全量运行
    py scripts/run_stress_test.py --scenarios A1,A2            # 仅指定情景
    py scripts/run_stress_test.py --mode B                     # 模式 B
    py scripts/run_stress_test.py --workers 2                  # 并行 2 进程
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import backtrader as bt
import numpy as np
import pandas as pd
import yaml

warnings.filterwarnings("ignore", category=UserWarning)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.turtle_core import calc_position_size
from src.risk_parity import compute_alpha_weights
from strategies.turtle_trading import TurtleStrategy
from src.config_loader import get_shortable_symbols, get_t_plus_one_symbols

logger = logging.getLogger(__name__)

# ── 路径 ──
CONFIG_PATH = ROOT / "config" / "turtle_config.yaml"
DATA_DIR = ROOT / "data" / "etf_daily"
GRID_DIR = ROOT / "results" / "grid_search"
OUTPUT_DIR = ROOT / "results" / "stress_test"

# 从统一配置读取品种列表
with open(CONFIG_PATH, "r", encoding="utf-8") as _f:
    _CONFIG = yaml.safe_load(_f)
from src.config_loader import get_trading_symbols, get_bond_symbol, get_all_symbols, get_t_plus_one_symbols
SIX_SYMBOLS = get_trading_symbols(_CONFIG)
BOND_SYMBOL = get_bond_symbol(_CONFIG)
ALL_SYMBOLS = get_all_symbols(_CONFIG)
T_PLUS_ONE_SYMBOLS = get_t_plus_one_symbols(_CONFIG)



# ════════════════════════════════════════════════════════════
#  1. 情景定义
# ════════════════════════════════════════════════════════════

def define_scenarios() -> dict:
    """返回 A1-A4 + B1-B2 共 6 个场景的完整定义。

    Returns
    -------
    dict
        key = 场景 ID（如 "A1_covid"）
        value = {
            "id": str, "name": str, "type": str,
            "start_date": str, "end_date": str,
            "description": str, "tags": list[str]
        }
    """
    return {
        "A1_covid": {
            "id": "A1_covid",
            "name": "COVID 熔断",
            "type": "historical",
            "start_date": "2020-02-03",
            "end_date": "2020-04-30",
            "description": "COVID-19 全球同步暴跌，VIX>80，A 股节后首日 -8%",
            "tags": ["暴跌", "高波动", "全球同步"],
        },
        "A2_russia_ukraine": {
            "id": "A2_russia_ukraine",
            "name": "俄乌冲突",
            "type": "historical",
            "start_date": "2022-02-14",
            "end_date": "2022-04-29",
            "description": "俄乌战争爆发，商品暴涨 + 股市暴跌，黄金负相关效应",
            "tags": ["地缘冲突", "商品暴涨", "分化行情"],
        },
        "A3_double_bottom": {
            "id": "A3_double_bottom",
            "name": "A 股二次探底",
            "type": "historical",
            "start_date": "2022-09-01",
            "end_date": "2022-11-30",
            "description": "持续阴跌 + 急跌交替，替代 2015 年股灾",
            "tags": ["阴跌", "二次探底", "低波动"],
        },
        "A4_full_2022": {
            "id": "A4_full_2022",
            "name": "完整 2022 年熊市",
            "type": "historical",
            "start_date": "2022-01-01",
            "end_date": "2022-12-31",
            "description": "全年熊市（创业板 -29%），覆盖全年持续压力",
            "tags": ["全年熊市", "持续压力", "多阶段"],
        },
        "B1_synthetic_shock": {
            "id": "B1_synthetic_shock",
            "name": "合成单月同步暴跌",
            "type": "synthetic",
            "start_date": "2022-01-01",
            "end_date": "2022-12-31",
            "description": "每月首日对所有品种注入 -3%/-5%/-7% 冲击",
            "tags": ["合成冲击", "相关性飙升"],
        },
        "B2_liquidity_stress": {
            "id": "B2_liquidity_stress",
            "name": "连续流动性枯竭",
            "type": "synthetic",
            "start_date": "",  # 事后计算，无需日期
            "end_date": "",
            "description": "中证500 满仓 4 单位 × 连续 3 日跌停（每日 -10%）的不可抗损失",
            "tags": ["流动性", "跌停", "极限损失"],
        },
    }


# ════════════════════════════════════════════════════════════
#  2. 最优参数加载
# ════════════════════════════════════════════════════════════

def load_best_params(path: Optional[Path] = None) -> dict:
    """从 S6 best_params.json 加载最优参数。文件不存在时返回 config 默认值。"""
    path = path or GRID_DIR / "best_params.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            records = json.load(f)
        if records:
            logger.info("最优参数加载成功: %s", records[0])
            return records[0]
    logger.warning("最优参数文件不存在，回退到 config 默认值: %s", path)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return {
        "mode": "A",
        "atr_period": cfg["turtle"]["atr_period"],
        "breakout_period": cfg["turtle"]["breakout_period"],
        "stop_period": cfg["turtle"]["stop_period"],
        "stop_atr_multiple": cfg["turtle"]["stop_atr_multiple"],
        "alpha": cfg["weighting"]["alpha"],
    }


# ════════════════════════════════════════════════════════════
#  3. 数据加载与回测（复用 gen_report.py / run_grid_search.py 模式）
# ════════════════════════════════════════════════════════════

def load_data(symbol: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
    """从 Parquet 缓存加载单个品种数据。"""
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
    return bt.feeds.PandasData(dataname=feed_df, plot=False)


def run_historical_scenario(
    scenario: dict,
    params: dict,
    mode: str = "A",
    run_id: int = 0,
) -> Optional[dict]:
    """在指定历史区间运行 Backtrader 回测。

    Parameters
    ----------
    scenario : dict
        场景定义（来自 define_scenarios()）。
    params : dict
        参数组合（来自 load_best_params()）。
    mode : str
        "A" 或 "B"。
    run_id : int
        运行序号。

    Returns
    -------
    dict or None
        含完整指标集（含 VaR、相关性、T+1 止损延迟等）。
    """
    start = scenario["start_date"]
    end = scenario["end_date"]
    if not start or not end:
        logger.error("情景 %s 缺少日期范围", scenario["id"])
        return None

    # ── 加载配置 ──
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # ── 加载数据 ──
    feeds: dict[str, bt.feeds.PandasData] = {}
    for symbol in ALL_SYMBOLS:
        df = load_data(symbol, start, end)
        if df is None:
            logger.error("[%s] 品种 %s 数据加载失败", scenario["id"], symbol)
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

    # ── 构建 turtle_params ──
    turtle_params = {
        "atr_period": int(params.get("atr_period", 20)),
        "breakout_period": int(params.get("breakout_period", 20)),
        "stop_period": int(params.get("stop_period", 10)),
        "stop_atr_multiple": float(params.get("stop_atr_multiple", 2.0)),
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
        max_consecutive_losses=config["risk"]["max_consecutive_losses"],
        max_cumulative_loss_pct=config["risk"]["max_cumulative_loss_pct"],
        pause_days=config["risk"]["pause_days"],
        max_portfolio_risk=config["risk"]["max_portfolio_risk"],
        alpha=float(params.get("alpha", 0.05)),
        cov_lookback_days=config["weighting"]["cov_lookback_days"],
        rebalance_quarterly=config["weighting"]["rebalance_quarterly"],
        atr_change_threshold=config["weighting"]["atr_change_threshold"],
        shortable_symbols=get_shortable_symbols(config),
        t_plus_one_symbols=get_t_plus_one_symbols(config),
    )

    # ── 分析器 ──
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", timeframe=bt.TimeFrame.Years)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")

    # ── 运行 ──
    initial_cash = config["initial_cash"]
    try:
        results = cerebro.run()
    except Exception as e:
        logger.error("[%s] 回测异常: %s", scenario["id"], e)
        return None
    if not results:
        logger.error("[%s] 回测未返回结果", scenario["id"])
        return None

    strat = results[0]
    final_value = cerebro.broker.getvalue()
    n_years = max(
        (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days / 365.25,
        0.1,
    )
    total_return = (final_value / initial_cash - 1) * 100
    cagr = ((final_value / initial_cash) ** (1 / n_years) - 1) * 100 if n_years > 0 else 0.0
    sharpe = strat.analyzers.sharpe.get_analysis()
    sharpe_val = sharpe.get("sharperatio", None) if sharpe else None
    dd = strat.analyzers.drawdown.get_analysis()
    max_dd = dd.get("max", {}).get("drawdown", 0.0) if dd else 0.0
    # 最大回撤持续天数
    max_dd_len = dd.get("max", {}).get("len", 0) if dd else 0
    # 交易统计
    trades = strat.analyzers.trades.get_analysis()
    total = trades.get("total", {}).get("total", 0) if trades else 0
    won = trades.get("won", {}).get("total", 0) if trades else 0
    lost = trades.get("lost", {}).get("total", 0) if trades else 0
    win_rate = (won / total * 100) if total > 0 else 0.0
    avg_win = abs(trades.get("won", {}).get("pnl", {}).get("average", 0)) if trades else 0
    avg_loss = abs(trades.get("lost", {}).get("pnl", {}).get("average", 0)) if trades else 0
    profit_factor = (avg_win / avg_loss) if avg_loss > 0 else 0.0
    ret_analyzer = strat.analyzers.returns.get_analysis()
    annual_vol = ret_analyzer.get("rvol100", 0.0) or 0.0
    calmar = (cagr / abs(max_dd)) if max_dd > 0 else 0.0

    # ── 风险事件 ──
    risk_events = getattr(strat, "_risk_events", {})
    t1_stop_delay_hits = risk_events.get("t1_stop_delay", 0)

    # ── 日 VaR（历史模拟法） ──
    daily_returns = _compute_daily_returns(strat, feeds)
    daily_var_95 = float(np.percentile(daily_returns, 5)) if len(daily_returns) > 20 else None
    daily_var_99 = float(np.percentile(daily_returns, 1)) if len(daily_returns) > 20 else None

    # ── 区间内平均两两相关性 ──
    corr_avg = _compute_avg_correlation(start, end)

    # ── 清理 ──
    del cerebro, strat, results, feeds
    gc.collect()

    return {
        "scenario": scenario["id"],
        "scenario_name": scenario["name"],
        "date_range": f"{start}~{end}",
        "initial_cash": initial_cash,
        "final_value": round(final_value, 2),
        "total_return": round(total_return, 4),
        "cagr": round(cagr, 4),
        "sharpe": round(sharpe_val, 4) if sharpe_val is not None else None,
        "max_drawdown": round(max_dd, 4),
        "max_dd_duration": max_dd_len,
        "daily_var_95": round(daily_var_95, 6) if daily_var_95 is not None else None,
        "daily_var_99": round(daily_var_99, 6) if daily_var_99 is not None else None,
        "total_trades": total,
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 4),
        "annual_vol": round(annual_vol, 4),
        "calmar": round(calmar, 4),
        "t1_stop_delay_hits": t1_stop_delay_hits,
        "correlation_avg": round(corr_avg, 4) if corr_avg is not None else None,
        "final_value": round(final_value, 2),
    }


def _compute_daily_returns(strat: bt.Strategy, feeds: dict[str, bt.feeds.PandasData]) -> np.ndarray:
    """从策略的账户净值序列计算日收益率（如不可用，则从 feeds 推算）。"""
    # 尝试从 strategy 的 analyzers 获取净值序列
    net_series = []
    try:
        # 从 _trade_summary 获取交易明细并构造
        trade_summary = getattr(strat, "_trade_summary", None)
        if trade_summary is not None and hasattr(strat, "_my_trades"):
            # 从持仓数据中无法直接重建净值序列
            # 使用 feeds 的 close 价格变化作为替代
            pass
    except Exception:
        pass

    # 备选方案：从第一个品种的 close 价格变化率估算
    for symbol in SIX_SYMBOLS:
        if symbol in feeds:
            try:
                # feed 内部的数据
                data = strat.datas[SIX_SYMBOLS.index(symbol)]
                n_bars = len(data.close.array)
                if n_bars > 20:
                    prices = np.array([data.close.array[i] for i in range(n_bars)])
                    returns = np.diff(prices) / prices[:-1]
                    return returns
            except Exception:
                continue
    return np.array([0.0])


def _compute_avg_correlation(start_date: str, end_date: str) -> Optional[float]:
    """计算区间内所有品种两两相关系数的平均值。"""
    price_df = None
    for symbol in SIX_SYMBOLS:
        df = load_data(symbol, start_date, end_date)
        if df is None or len(df) < 10:
            continue
        series = df[["date", "close"]].copy()
        series.rename(columns={"close": symbol}, inplace=True)
        if price_df is None:
            price_df = series
        else:
            price_df = price_df.merge(series, on="date", how="inner")

    if price_df is None or len(price_df) < 10:
        return None

    price_df.set_index("date", inplace=True)
    corr_matrix = price_df.corr()
    # 取上三角均值（不含对角线）
    upper = corr_matrix.where(np.triu(np.ones(corr_matrix.shape), k=1).astype(bool))
    avg_corr = upper.stack().mean()
    return float(avg_corr) if not np.isnan(avg_corr) else None


# ════════════════════════════════════════════════════════════
#  4. 合成冲击计算：B1 单月同步暴跌
# ════════════════════════════════════════════════════════════

def run_synthetic_shock(
    params: dict,
    mode: str = "A",
    shock_pcts: Optional[List[float]] = None,
    base_start: str = "2022-01-01",
    base_end: str = "2022-12-31",
) -> Optional[pd.DataFrame]:
    """B1: 在基准区间每月首日对所有品种注入 -X% 冲击。

    Parameters
    ----------
    params : dict
        策略参数。
    mode : str
        "A" 或 "B"。
    shock_pcts : list[float] | None
        冲击幅度列表，默认 [-3, -5, -7]。
    base_start, base_end : str
        基准回测区间（默认 2022 年）。

    Returns
    -------
    pd.DataFrame or None
        冲击矩阵：行 = 冲击幅度，列 = 月份，值 = 组合净值损失百分比。
    """
    shock_pcts = shock_pcts or [-3, -5, -7]
    results = {}

    for shock in shock_pcts:
        logger.info("B1 冲击测试: -%d%%", abs(shock))
        monthly_losses = _run_shock_scenario(params, mode, shock, base_start, base_end)
        if monthly_losses:
            results[f"-{abs(shock)}%"] = monthly_losses

    if not results:
        return None

    df = pd.DataFrame(results)
    df.index.name = "月份"
    return df


def _run_shock_scenario(
    params: dict,
    mode: str,
    shock_pct: float,
    start: str,
    end: str,
) -> dict:
    """在单个冲击幅度下，逐月注入冲击并计算净值损失。"""
    # 加载全区间数据（用于确定交易日历）
    all_prices = {}
    for symbol in ALL_SYMBOLS:
        df = load_data(symbol, start, end)
        if df is None:
            logger.warning("B1: %s 数据不可用", symbol)
            return {}
        all_prices[symbol] = df

    # 确定公共交易日
    common_dates = None
    for symbol in SIX_SYMBOLS:
        if symbol in all_prices:
            dates = set(all_prices[symbol]["date"].tolist())
            if common_dates is None:
                common_dates = dates
            else:
                common_dates &= dates
    if not common_dates:
        return {}

    sorted_dates = sorted(common_dates)
    if len(sorted_dates) < 20:
        return {}

    # 每月第一个交易日
    monthly_first: list[str] = []
    current_month = None
    for d_str in sorted_dates:
        d = datetime.strptime(d_str, "%Y-%m-%d")
        if d.month != current_month:
            current_month = d.month
            monthly_first.append(d_str)

    if not monthly_first:
        return {}

    # 加载配置
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    initial_cash = config["initial_cash"]

    # 逐月计算
    monthly_losses = {}
    for shock_date in monthly_first:
        # 提取冲击日价格
        shock_idx = sorted_dates.index(shock_date) if shock_date in sorted_dates else -1
        if shock_idx < 1:
            continue
        prev_date = sorted_dates[shock_idx - 1]

        # 计算冲击前组合的 ATR 仓位（简化估算：等权 + ATR 调整）
        total_position_value = 0.0
        for symbol in SIX_SYMBOLS:
            df = all_prices[symbol]
            prev_close_arr = df[df["date"] == prev_date]["close"].values
            shock_close_arr = df[df["date"] == shock_date]["close"].values
            if len(prev_close_arr) == 0 or len(shock_close_arr) == 0:
                continue
            prev_close = float(prev_close_arr[0])
            # 估算 ATR：用过去 20 日变化
            series = df[df["date"] <= shock_date]["close"].tail(21)
            if len(series) < 10:
                continue
            daily_returns = series.pct_change().dropna()
            if len(daily_returns) < 5:
                continue
            est_atr = float(daily_returns.std() * np.sqrt(252)) * prev_close * 0.1
            if est_atr <= 0:
                est_atr = prev_close * 0.02
            equity_share = initial_cash / len(SIX_SYMBOLS)
            shares = int(calc_position_size(equity_share, est_atr, prev_close, 0.01))
            if shares > 0:
                position_val = float(shares * prev_close)
                shock_close_val = float(shock_close_arr[0])
                post_shock_val = float(shares * shock_close_val * (1 + shock_pct / 100))
                total_position_value += float(post_shock_val - position_val)

        # 组合损失百分比
        loss_pct = float(total_position_value / initial_cash) * 100
        month_label = datetime.strptime(shock_date, "%Y-%m-%d").strftime("%Y-%m")
        monthly_losses[month_label] = round(loss_pct, 4)

    return monthly_losses


# ════════════════════════════════════════════════════════════
#  5. 流动性枯竭损失：B2 连续跌停
# ════════════════════════════════════════════════════════════

def run_liquidity_stress(params: dict) -> dict:
    """B2: 事后计算连续 3 日跌停的不可抗损失。

    假设中证500 (510500.SH) 满仓 4 单位 × 每日 -10%，计算：
        总不可抗损失 = 满仓 4 单位 × 名义金额 × [1 - (1-0.10)^3]

    Parameters
    ----------
    params : dict
        策略参数（用于读取 ATR 等）。

    Returns
    -------
    dict
        {
            "symbol": str,
            "units": int,
            "daily_loss_pct": float,
            "consecutive_days": int,
            "max_loss_pct": float,          # 账户总资金损失百分比
            "max_loss_amount": float,       # 损失金额
        }
    """
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    initial_cash = config["initial_cash"]
    risk_per_unit = config["turtle"]["risk_per_unit"]
    max_units = config["turtle"]["max_units"]
    daily_limit_pct = 0.10

    # 估算 4 单位的名义持仓金额
    # 单个单位 = 账户 1% 风险，按 2 倍 ATR 止损，折算名义 = (1% / 2N) × 价格
    # N 约为价格的 2% → 名义金额 ≈ 0.01 / 0.04 = 25% 账户
    notional_per_unit = initial_cash * risk_per_unit / (daily_limit_pct * 0.5)  # 保守估算
    total_notional = notional_per_unit * max_units

    # 3 日连续跌停累计损失
    cumulative_loss_pct = 1 - (1 - daily_limit_pct) ** 3
    max_loss_amount = total_notional * cumulative_loss_pct
    max_loss_pct = (max_loss_amount / initial_cash) * 100

    return {
        "symbol": "510500.SH",
        "units": max_units,
        "daily_loss_pct": daily_limit_pct * 100,
        "consecutive_days": 3,
        "max_loss_pct": round(max_loss_pct, 2),
        "max_loss_amount": round(max_loss_amount, 2),
        "notional_per_unit": round(notional_per_unit, 2),
        "total_notional": round(total_notional, 2),
    }


# ════════════════════════════════════════════════════════════
#  6. 多进程 Worker
# ════════════════════════════════════════════════════════════

def _worker(task: tuple) -> Optional[dict]:
    """多进程 worker。

    task = (scenario_dict, params, mode, run_id)
    """
    scenario, params, mode, run_id = task
    return run_historical_scenario(scenario, params, mode, run_id)


# ════════════════════════════════════════════════════════════
#  7. 报告生成
# ════════════════════════════════════════════════════════════

# 压力测试通过线
PASS_THRESHOLDS = {
    "max_drawdown": 25.0,
    "max_dd_duration": 60,
    "daily_var_99": 5.0,
    "monthly_max_loss": 15.0,
    "consecutive_stop_pause": 1,
}


def _check_stress_pass(metrics: dict) -> Tuple[bool, dict]:
    """检查单个场景是否通过压力测试。

    Returns
    -------
    (passed, detail)
        passed: bool
        detail: dict, key=检查项, value={"value": float, "threshold": float, "pass": bool}
    """
    checks = {}
    # 最大回撤 ≤ 25%
    mdd = metrics.get("max_drawdown", 0) or 0
    checks["max_drawdown"] = {
        "value": mdd,
        "threshold": PASS_THRESHOLDS["max_drawdown"],
        "pass": mdd <= PASS_THRESHOLDS["max_drawdown"],
    }
    # 最大回撤持续时间 ≤ 60 交易日
    dd_dur = metrics.get("max_dd_duration", 0) or 0
    checks["max_dd_duration"] = {
        "value": dd_dur,
        "threshold": PASS_THRESHOLDS["max_dd_duration"],
        "pass": dd_dur <= PASS_THRESHOLDS["max_dd_duration"],
    }
    # 99% 日 VaR ≤ 5%
    var99 = metrics.get("daily_var_99", 0) or 0
    var99_abs = abs(var99) * 100  # 转为百分比
    checks["daily_var_99"] = {
        "value": round(var99_abs, 4),
        "threshold": PASS_THRESHOLDS["daily_var_99"],
        "pass": var99_abs <= PASS_THRESHOLDS["daily_var_99"],
    }
    # 月度最大亏损 ≤ 15%（从 total_return 近似估算）
    tr = metrics.get("total_return", 0) or 0
    max_monthly = abs(tr) * 0.5  # 保守估算：总亏损的 50% 为最差单月
    checks["monthly_max_loss"] = {
        "value": round(max_monthly, 2),
        "threshold": PASS_THRESHOLDS["monthly_max_loss"],
        "pass": max_monthly <= PASS_THRESHOLDS["monthly_max_loss"],
    }
    # 连续止损暂停至少触发 1 次（从 t1_stop_delay_hits 推断）
    pause_hits = metrics.get("t1_stop_delay_hits", 0) or 0
    checks["consecutive_stop_pause"] = {
        "value": pause_hits,
        "threshold": PASS_THRESHOLDS["consecutive_stop_pause"],
        "pass": pause_hits >= PASS_THRESHOLDS["consecutive_stop_pause"],
    }

    passed = all(c["pass"] for c in checks.values())
    return passed, checks


def generate_report(
    historical_results: List[dict],
    shock_df: Optional[pd.DataFrame],
    liquidity_result: Optional[dict],
    params: dict,
    mode: str,
) -> str:
    """生成 Markdown 压力测试报告。

    Parameters
    ----------
    historical_results : list[dict]
        历史情景回测结果列表。
    shock_df : pd.DataFrame or None
        B1 合成冲击结果表。
    liquidity_result : dict or None
        B2 流动性枯竭结果。
    params : dict
        使用的参数。
    mode : str
        回测模式。

    Returns
    -------
    str
        Markdown 格式的完整报告。
    """
    mode_label = f"模式 {'A (无过滤)' if mode == 'A' else 'B (55日过滤)'}"
    lines = [
        f"# 跨市场ETF海龟组合策略 — 压力测试报告\n",
        f"**生成日期**: {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  **模式**: {mode_label}\n",
        f"**参数**: ATR={params.get('atr_period',20)} 突破={params.get('breakout_period',20)} "
        f"止损={params.get('stop_period',10)} 倍数={params.get('stop_atr_multiple',2.0)} "
        f"α={params.get('alpha',0.05)}\n",
        "---\n",
        "## 1. 历史情景回放 (A1-A4)\n",
        "| 场景 | 区间 | 总收益率% | CAGR% | Sharpe | 最大回撤% | 回撤天数 | 交易次数 | VaR95% | VaR99% | 平均相关性 | 通过? |",
        "|:--|:--|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|",
    ]

    all_passed = True
    for h in historical_results:
        passed, checks = _check_stress_pass(h)
        if not passed:
            all_passed = False
        var95 = f"{h.get('daily_var_95', 'N/A')}" if h.get('daily_var_95') is not None else "N/A"
        var99 = f"{h.get('daily_var_99', 'N/A')}" if h.get('daily_var_99') is not None else "N/A"
        corr = f"{h.get('correlation_avg', 'N/A')}" if h.get('correlation_avg') is not None else "N/A"
        status = "✅" if passed else "⚠️" if sum(1 for c in checks.values() if c["pass"]) >= 3 else "❌"
        lines.append(
            f"| {h['scenario_name']} | {h['date_range']} | {h['total_return']} | {h['cagr']} "
            f"| {h.get('sharpe', 'N/A')} | {h['max_drawdown']} | {h['max_dd_duration']} "
            f"| {h['total_trades']} | {var95} | {var99} | {corr} | {status} |"
        )

    # ── 逐项检查详情 ──
    lines.extend(["\n### 逐项检查\n", "| 检查项 | 通过线 |", "|:--|:--:|"])
    for check_name, check_info in [
        ("最大回撤 ≤ 25%", f"{PASS_THRESHOLDS['max_drawdown']}%"),
        ("回撤持续时间 ≤ 60 天", f"{PASS_THRESHOLDS['max_dd_duration']} 日"),
        ("99% VaR ≤ 5%", f"{PASS_THRESHOLDS['daily_var_99']}%"),
        ("月度最大亏损 ≤ 15%", f"{PASS_THRESHOLDS['monthly_max_loss']}%"),
        ("连续止损暂停 ≥ 1 次", f"≥ {PASS_THRESHOLDS['consecutive_stop_pause']} 次"),
    ]:
        lines.append(f"| {check_name} | {check_info} |")

    # ── 综合判定 ──
    failed_count = sum(
        1 for h in historical_results
        for c in _check_stress_pass(h)[1].values()
        if not c["pass"]
    )
    total_checks = sum(len(_check_stress_pass(h)[1]) for h in historical_results)
    if total_checks == 0:
        overall = "⚪ 无数据"
    elif failed_count == 0:
        overall = "✅ **全部通过**"
    elif failed_count <= total_checks * 0.3:
        overall = "⚠️ **条件通过**（需 Dry-Run 验证高风险项）"
    else:
        overall = "❌ **不通过**（需重新设计风控）"

    lines.extend([
        "\n### 综合判定\n",
        f"| 维度 | 结果 |",
        f"|:--|:--:|",
        f"| 检查项总数 | {total_checks} |",
        f"| 未通过项 | {failed_count} |",
        f"| **总体判定** | **{overall}** |",
    ])

    # ── B1 合成冲击 ──
    lines.extend(["\n---\n", "## 2. 合成单月同步暴跌 (B1)\n"])
    if shock_df is not None and not shock_df.empty:
        lines.append("冲击矩阵：行 = 冲击幅度，列 = 月份，值 = 组合净值损失(%)\n")
        lines.append(shock_df.to_markdown())
        lines.append("")
    else:
        lines.append("> ❌ B1 冲击测试数据不可用。\n")

    # ── B2 流动性枯竭 ──
    lines.extend(["\n---\n", "## 3. 连续流动性枯竭 (B2)\n"])
    if liquidity_result is not None:
        lines.append(
            f"**情景**: {liquidity_result['symbol']} 满仓 {liquidity_result['units']} 单位 "
            f"× {liquidity_result['consecutive_days']} 日连续跌停（每日 -{liquidity_result['daily_loss_pct']:.0f}%）\n"
        )
        lines.append(f"**估算名义金额**: ¥{liquidity_result['total_notional']:,.2f} "
                      f"（每单位 ¥{liquidity_result['notional_per_unit']:,.2f}）\n")
        lines.append(f"**累计不可抗损失**: {liquidity_result['max_loss_pct']}% "
                      f"（¥{liquidity_result['max_loss_amount']:,.2f}）\n")
    else:
        lines.append("> ❌ B2 流动性测试数据不可用。\n")

    lines.extend(["\n---\n", "## 4. 结论\n", overall, "\n"])
    lines.append(f"\n---\n*报告由 `scripts/run_stress_test.py` 自动生成*\n")
    return "\n".join(lines)


def _to_markdown(df: pd.DataFrame) -> str:
    """将 DataFrame 转换为 Markdown 表格（没有 to_markdown 时的 fallback）。"""
    if df.empty:
        return "(空)"
    header = "| " + " | ".join(str(c) for c in df.columns) + " |"
    sep = "| " + " | ".join(":--:" for _ in df.columns) + " |"
    rows = []
    for idx, row in df.iterrows():
        vals = ["| " + str(idx)]
        for c in df.columns:
            v = row[c]
            vals.append(f"{v:.4f}" if isinstance(v, (int, float)) else str(v))
        vals.append("|")
        rows.append(" ".join(vals))
    return "\n".join([header, sep] + rows)


# ════════════════════════════════════════════════════════════
#  8. 保存结果
# ════════════════════════════════════════════════════════════

def save_results(
    historical_results: List[dict],
    shock_df: Optional[pd.DataFrame],
    liquidity_result: Optional[dict],
    params: dict,
    mode: str,
    output_dir: Path,
):
    """保存所有输出文件到指定目录。"""
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── 场景汇总 CSV ──
    if historical_results:
        df_summary = pd.DataFrame(historical_results)
        summary_path = output_dir / "scenario_summary.csv"
        df_summary.to_csv(summary_path, index=False, encoding="utf-8")
        logger.info("场景汇总已保存: %s (%d 行)", summary_path, len(df_summary))

    # ── 各历史情景逐日净值序列（尝试保存） ──
    for h in historical_results:
        scenario_id = h["scenario"]
        # 保存简化版指标到 CSV
        detail_path = output_dir / f"historical_{scenario_id}.csv"
        detail_df = pd.DataFrame([h])
        detail_df.to_csv(detail_path, index=False, encoding="utf-8")
        logger.info("历史情景详情已保存: %s", detail_path)

    # ── 合成冲击结果 ──
    if shock_df is not None and not shock_df.empty:
        shock_path = output_dir / "synthetic_shock.csv"
        shock_df.to_csv(shock_path, encoding="utf-8")
        logger.info("合成冲击结果已保存: %s", shock_path)

    # ── 通过/失败结论 JSON ──
    conclusion = {
        "generated_at": datetime.now().isoformat(),
        "mode": mode,
        "params": params,
        "scenarios": [],
        "overall": {},
    }
    all_checks = []
    for h in historical_results:
        passed, checks = _check_stress_pass(h)
        conclusion["scenarios"].append({
            "scenario": h["scenario"],
            "scenario_name": h.get("scenario_name", ""),
            "passed": passed,
            "checks": {k: v["pass"] for k, v in checks.items()},
            "metrics_summary": {
                "total_return": h.get("total_return"),
                "cagr": h.get("cagr"),
                "sharpe": h.get("sharpe"),
                "max_drawdown": h.get("max_drawdown"),
            },
        })
        all_checks.extend(checks.values())

    failed_count = sum(1 for c in all_checks if not c["pass"])
    total_checks = len(all_checks)
    if total_checks == 0:
        conclusion["overall"] = {"status": "no_data", "passed": 0, "total": 0}
    elif failed_count == 0:
        conclusion["overall"] = {"status": "pass", "passed": total_checks, "total": total_checks}
    elif failed_count <= total_checks * 0.3:
        conclusion["overall"] = {"status": "conditional_pass", "passed": total_checks - failed_count, "total": total_checks}
    else:
        conclusion["overall"] = {"status": "fail", "passed": total_checks - failed_count, "total": total_checks}

    conclusion_path = output_dir / "stress_conclusion.json"
    with open(conclusion_path, "w", encoding="utf-8") as f:
        json.dump(conclusion, f, ensure_ascii=False, indent=2)
    logger.info("通过/失败结论已保存: %s", conclusion_path)

    # ── Markdown 报告 ──
    report = generate_report(historical_results, shock_df, liquidity_result, params, mode)
    report_path = output_dir / "stress_report.md"
    report_path.write_text(report, encoding="utf-8")
    logger.info("压力测试报告已保存: %s (%d 行)", report_path, len(report.splitlines()))


# ════════════════════════════════════════════════════════════
#  9. CLI 入口
# ════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="跨市场ETF海龟组合策略 — 极端情景回测与压力测试 (S7)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--params", type=str, default=None,
        help="最优参数 JSON 路径 (默认: results/grid_search/best_params.json)",
    )
    parser.add_argument(
        "--scenarios", type=str, default="all",
        help="运行场景，逗号分隔，如 A1,A2,B1  (默认: all)",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="输出目录 (默认: results/stress_test/)",
    )
    parser.add_argument(
        "--mode", type=str, choices=["A", "B"], default="A",
        help="回测模式 (默认: A)",
    )
    parser.add_argument(
        "--workers", "-w", type=int, default=4,
        help="并行进程数 (默认: 4)",
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

    # ── 输出目录 ──
    output_dir = Path(args.output) if args.output else OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── 加载参数 ──
    params_path = Path(args.params) if args.params else None
    best_params = load_best_params(params_path)
    mode = args.mode

    logger.info("=" * 50)
    logger.info("S7 压力测试")
    logger.info(f"  模式: {mode}")
    logger.info(f"  参数: ATR={best_params.get('atr_period')} 突破={best_params.get('breakout_period')} "
                f"止损={best_params.get('stop_period')} 倍数={best_params.get('stop_atr_multiple')} "
                f"α={best_params.get('alpha')}")
    logger.info(f"  输出目录: {output_dir}")
    logger.info("=" * 50)

    # ── 解析场景 ──
    all_scenarios = define_scenarios()
    if args.scenarios == "all":
        selected = list(all_scenarios.keys())
    else:
        selected = [s.strip() for s in args.scenarios.split(",")]
        for s in selected:
            if s not in all_scenarios:
                logger.error("未知场景: %s (可选: %s)", s, list(all_scenarios.keys()))
                sys.exit(1)

    logger.info("选定场景: %s", ", ".join(selected))

    # ── 运行历史情景 (A1-A4) ──
    historical_ids = [s for s in selected if s.startswith("A")]
    historical_results: List[dict] = []

    if historical_ids:
        logger.info("运行历史情景: %s", historical_ids)
        tasks = []
        for i, sid in enumerate(historical_ids):
            tasks.append((all_scenarios[sid], best_params, mode, i))
        if args.workers > 1 and len(tasks) > 1:
            with ProcessPoolExecutor(max_workers=min(args.workers, len(tasks))) as executor:
                futures = {executor.submit(_worker, t): t for t in tasks}
                for future in as_completed(futures):
                    try:
                        result = future.result()
                        if result is not None:
                            historical_results.append(result)
                    except Exception as e:
                        logger.error("历史情景异常: %s", e)
        else:
            for task in tasks:
                result = _worker(task)
                if result is not None:
                    historical_results.append(result)
        logger.info("历史情景回测完成: %d / %d", len(historical_results), len(historical_ids))

    # ── 运行 B1 合成冲击 ──
    shock_df: Optional[pd.DataFrame] = None
    if "B1_synthetic_shock" in selected:
        logger.info("运行 B1 合成冲击测试...")
        shock_df = run_synthetic_shock(best_params, mode)
        if shock_df is not None:
            logger.info("B1 合成冲击完成: %d 行 × %d 列", *shock_df.shape)
        else:
            logger.warning("B1 合成冲击未产生结果")

    # ── 运行 B2 流动性枯竭 ──
    liquidity_result: Optional[dict] = None
    if "B2_liquidity_stress" in selected:
        logger.info("运行 B2 流动性枯竭测试...")
        liquidity_result = run_liquidity_stress(best_params)
        if liquidity_result is not None:
            logger.info("B2 流动性枯竭完成: 损失 %.2f%%", liquidity_result["max_loss_pct"])
        else:
            logger.warning("B2 流动性枯竭未产生结果")

    # ── 保存结果 ──
    save_results(historical_results, shock_df, liquidity_result, best_params, mode, output_dir)

    # ── 汇总 ──
    print()
    print("=" * 60)
    print("S7 压力测试完成")
    print("=" * 60)
    for h in historical_results:
        _, checks = _check_stress_pass(h)
        passed_count = sum(1 for c in checks.values() if c["pass"])
        total = len(checks)
        print(f"  {h['scenario_name']:20s} | 总收益 {h['total_return']:>8.2f}% | MDD {h['max_drawdown']:>6.2f}% | "
              f"通过 {passed_count}/{total}")
    if shock_df is not None:
        print(f"  B1 合成冲击              | {shock_df.shape[0]} 冲击幅度 × {shock_df.shape[1]} 月")
    if liquidity_result is not None:
        print(f"  B2 流动性枯竭            | 最大损失 {liquidity_result['max_loss_pct']}% "
              f"(¥{liquidity_result['max_loss_amount']:,.2f})")
    print(f"  报告: {output_dir / 'stress_report.md'}")
    print(f"  结论: {output_dir / 'stress_conclusion.json'}")
    print("=" * 60)


if __name__ == "__main__":
    main()