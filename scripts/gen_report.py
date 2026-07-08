#!/usr/bin/env python
""" 跨市场ETF海龟组合策略 · 综合报告生成 (S8)
从各阶段结果文件汇总数据，运行一次最优参数回测，生成 Markdown 综合报告。
对尚未产出数据的阶段（S5/S7），优雅降级使用占位符标记。

用法：
    py scripts/gen_report.py               # 默认模式 A
    py scripts/gen_report.py --mode B      # 模式 B
    py scripts/gen_report.py --no-backtest # 仅组装已有数据
    py scripts/gen_report.py --output results/my_report.md # 指定输出路径
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import sys
import warnings
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

from src.data_utils import load_data, align_to_common_dates, df_to_feed
from strategies.turtle_trading import TurtleStrategy
from src.config_loader import get_shortable_symbols, get_t_plus_one_symbols

logger = logging.getLogger(__name__)

# ── 路径 ──
CONFIG_PATH = ROOT / "config" / "turtle_config.yaml"
DATA_DIR = ROOT / "data" / "etf_daily"
GRID_DIR = ROOT / "results" / "grid_search"
STRESS_DIR = ROOT / "results" / "stress_test"
COMPARISON_DIR = ROOT / "results" / "comparison"  # 全局定义的对比目录
DEFAULT_OUTPUT = ROOT / "results" / "report.md"

# ── 品种 ──
import yaml
from src.config_loader import get_trading_symbols, get_bond_symbol, get_all_symbols, get_symbol_names

with open(ROOT / "config" / "turtle_config.yaml", "r", encoding="utf-8") as _f:
    _CONFIG = yaml.safe_load(_f)

SIX_SYMBOLS = get_trading_symbols(_CONFIG)
BOND_SYMBOL = get_bond_symbol(_CONFIG)
ALL_SYMBOLS = get_all_symbols(_CONFIG)

# 品种名称映射 —— 从 config 动态读取，消除硬编码
SYMBOL_NAMES = get_symbol_names(_CONFIG)

# ════════════════════════════════════════════════════════════
# 1. 数据加载
# ════════════════════════════════════════════════════════════

def load_best_params(path: Optional[Path] = None) -> dict:
    """从 S6 best_params.json 加载最优参数。文件不存在时返回 config 默认值。"""
    path = path or GRID_DIR / "best_params.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            records = json.load(f)
            if records:
                logger.info("最优参数: %s", records[0])
                return records[0]

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

def load_grid_results() -> Optional[pd.DataFrame]:
    path = GRID_DIR / "grid_results_full.csv"
    if path.exists():
        df = pd.read_csv(path)
        logger.info("加载网格结果: %d 行", len(df))
        return df
    logger.warning("网格结果不存在: %s", path)
    return None

def load_oos_results() -> Optional[pd.DataFrame]:
    path = GRID_DIR / "oos_validation.csv"
    if path.exists():
        df = pd.read_csv(path)
        logger.info("加载样本外验证: %d 行", len(df))
        return df
    logger.warning("样本外验证不存在: %s", path)
    return None

def load_comparison_results() -> Optional[pd.DataFrame]:
    """从 COMPARISON_DIR 加载最新对比 CSV。"""
    # [修复] 修正路径，使用全局定义的 COMPARISON_DIR
    if not COMPARISON_DIR.exists():
        return None

    csvs = sorted(COMPARISON_DIR.glob("comparison_*.csv"))
    if not csvs:
        return None

    df = pd.read_csv(csvs[-1], index_col=0)
    return df

# ════════════════════════════════════════════════════════════
# 2. 回测运行
# ════════════════════════════════════════════════════════════

def run_backtest_with_best(params: dict, start_date: str = "2014-01-01", end_date: str = "2026-06-10", mode: str = "A") -> dict:
    """用给定参数运行一次全区间回测，返回完整指标集。"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 加载全部数据并计算公共日期
    all_dfs = {}
    for symbol in SIX_SYMBOLS:
        df = load_data(symbol, start_date, end_date, DATA_DIR)
        if df is not None:
            all_dfs[symbol] = df

    # 所有品种对齐到公共日期
    all_dfs = align_to_common_dates(all_dfs)

    # 创建对齐的 feeds
    feeds = {}
    for symbol in SIX_SYMBOLS:
        if symbol not in all_dfs:
            logger.warning("[%s] 数据不可用，跳过", symbol)
            continue
        feed = df_to_feed(all_dfs[symbol], symbol)
        feed._name = symbol
        feeds[symbol] = feed

    if len(feeds) < 2:
        logger.error("数据不足，无法回测")
        return {}

    cerebro = bt.Cerebro()
    for symbol in SIX_SYMBOLS:
        if symbol in feeds:
            cerebro.adddata(feeds[symbol], name=symbol)
    if BOND_SYMBOL in feeds:
        cerebro.adddata(feeds[BOND_SYMBOL], name=BOND_SYMBOL)

    cerebro.broker.setcash(config["initial_cash"])
    cm = config["commission_pct"] + config["slippage_pct"]
    cerebro.broker.setcommission(commission=cm)

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
        degradation_config=config["risk"].get("degradation", {}),
    )

    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", timeframe=bt.TimeFrame.Years)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
    cerebro.addanalyzer(bt.analyzers.TimeReturn, _name="timereturn", timeframe=bt.TimeFrame.Days)

    initial_cash = config["initial_cash"]

    try:
        results = cerebro.run()
    except Exception as e:
        logger.error("回测异常: %s", e)
        return {}

    if not results:
        return {}

    strat = results[0]
    final_value = cerebro.broker.getvalue()
    n_years = (datetime.strptime(end_date, "%Y-%m-%d") - datetime.strptime(start_date, "%Y-%m-%d")).days / 365.25
    total_return = (final_value / initial_cash - 1) * 100
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

    # [修复] 修正 Profit Factor 定义：使用 总盈利 / 总亏损
    won_stats = trades.get("won", {}).get("pnl", {})
    lost_stats = trades.get("lost", {}).get("pnl", {})
    total_gross_profit = abs(won_stats.get("total", 0))
    total_gross_loss = abs(lost_stats.get("total", 0))
    profit_factor = (total_gross_profit / total_gross_loss) if total_gross_loss > 0 else 0.0

    # 改用 TimeReturn 日收益率计算年化波动率（Returns.rvol100 在 backtrader 中不产出）
    timeret = strat.analyzers.timereturn.get_analysis()
    daily_rets = list(timeret.values())
    if len(daily_rets) > 1:
        av = float(np.std(daily_rets) * np.sqrt(252) * 100)
    else:
        av = 0.0

    calmar = (cagr / abs(max_dd)) if max_dd > 0 else 0.0

    risk_events = getattr(strat, "_risk_events", {})

    # 提取品种级交易统计
    my_trades = getattr(strat, "_my_trades", [])
    per_symbol = {}
    for tr in my_trades:
        sym = tr.get("symbol", "?")
        if sym not in per_symbol:
            per_symbol[sym] = {"total": 0, "won": 0, "pnl_sum": 0.0}
        per_symbol[sym]["total"] += 1
        if tr.get("was_win"):
            per_symbol[sym]["won"] += 1
        per_symbol[sym]["pnl_sum"] += tr.get("pnl", 0.0)

    for sym, v in per_symbol.items():
        v["win_rate"] = round(v["won"] / v["total"] * 100, 1) if v["total"] > 0 else 0.0
        v["pnl_sum"] = round(v["pnl_sum"], 2)

    del cerebro, strat, results, feeds
    gc.collect()

    return {
        "mode": mode,
        "start_date": start_date,
        "end_date": end_date,
        "initial_cash": initial_cash,
        "final_value": round(final_value, 2),
        "total_return": round(total_return, 2),
        "cagr": round(cagr, 2),
        "sharpe": round(sharpe_val, 4) if sharpe_val else None,
        "max_drawdown": round(max_dd, 2),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 4),
        "total_trades": total,
        "annual_vol": round(av, 2),
        "calmar": round(calmar, 4),
        "concentration_cut": risk_events.get("concentration_cut", 0),
        "dd_warning": risk_events.get("dd_warning", 0),
        "loss_pause": risk_events.get("loss_pause", 0),
        "t1_stop_delay": risk_events.get("t1_stop_delay", 0),
        "per_symbol": per_symbol,
    }

# ════════════════════════════════════════════════════════════
# 3. 报告生成
# ════════════════════════════════════════════════════════════

PASS_TARGETS = {"cagr": 15.0, "max_drawdown": 25.0, "sharpe": 0.8, "profit_factor": 1.5, "total_trades": 50}
PASS_NAMES = {"cagr": "年化收益率 (CAGR)", "max_drawdown": "最大回撤 (MDD)", "sharpe": "夏普比率", "profit_factor": "盈亏比", "total_trades": "交易次数"}
PASS_DIR = {"cagr": "gte", "max_drawdown": "lte", "sharpe": "gte", "profit_factor": "gte", "total_trades": "gte"}

def _pass_str(value, target, direction):
    if value is None:
        return "⚪ 无数据"
    ok = value >= target if direction == "gte" else value <= target
    return "✅" if ok else "❌"

def generate_summary_table(metrics: dict) -> str:
    lines = ["| 指标 | 值 | 目标 | 状态 |", "|:--|:--:|:--:|:--:|"]
    for key in ["cagr", "max_drawdown", "sharpe", "profit_factor", "total_trades"]:
        val = metrics.get(key)
        display_val = f"{val:.2f}" if val is not None else "N/A"
        status = _pass_str(val, PASS_TARGETS[key], PASS_DIR[key])
        lines.append(f"| {PASS_NAMES[key]} | {display_val} | {PASS_TARGETS[key]} | {status} |")

    passed = sum(1 for k in PASS_TARGETS if metrics.get(k) is not None and ((PASS_DIR[k] == "gte" and metrics[k] >= PASS_TARGETS[k]) or (PASS_DIR[k] == "lte" and metrics[k] <= PASS_TARGETS[k])))
    overall = "✅ 通过" if passed >= 5 else ("⚠️ 条件通过" if passed >= 3 else "❌ 不通过")
    lines.append(f"| **总体判定** | **{passed}/5** | — | **{overall}** |")
    return "\n".join(lines)

def generate_performance_table(metrics: dict) -> str:
    rows = [
        ("初始资金", f"¥{metrics.get('initial_cash', 0):,.2f}"),
        ("最终净值", f"¥{metrics.get('final_value', 0):,.2f}"),
        ("总收益率", f"{metrics.get('total_return', 'N/A')}%"),
        ("年化收益率 (CAGR)", f"{metrics.get('cagr', 'N/A')}%"),
        ("夏普比率", str(metrics.get("sharpe", "N/A"))),
        ("最大回撤", f"{metrics.get('max_drawdown', 'N/A')}%"),
        ("Calmar 比率", str(metrics.get("calmar", "N/A"))),
        ("年化波动率", f"{metrics.get('annual_vol', 'N/A')}%"),
        ("胜率", f"{metrics.get('win_rate', 'N/A')}%"),
        ("盈亏比", str(metrics.get("profit_factor", "N/A"))),
        ("总交易次数", str(metrics.get("total_trades", "N/A"))),
    ]
    return "| 指标 | 值 |\n|:--|:--:|\n" + "\n".join(f"| {k} | {v} |" for k, v in rows) + "\n### 风控统计\n| 风控事件 | 触发次数 |\n|:--|:--:|\n" + "\n".join(
        f"| {k} | {metrics.get(v, 'N/A')} |" for k, v in [("仓位集中度熔断", "concentration_cut"), ("最大回撤预警", "dd_warning"), ("连续亏损暂停", "loss_pause"), ("T+1 止损延迟", "t1_stop_delay")]
    )

def generate_params_section(df_full: Optional[pd.DataFrame], df_oos: Optional[pd.DataFrame]) -> str:
    lines = ["## 4. 最优参数组合\n"]
    best_path = GRID_DIR / "best_params.json"

    # [修复] 加载和去重逻辑提取到前面，避免重复执行
    best = []
    if best_path.exists():
        with open(best_path, "r", encoding="utf-8") as f:
            raw_records = json.load(f)

        # 去重：只按海龟核心参数去重（排除风控参数重复行）
        seen = set()
        for b in raw_records:
            key = (b.get("mode"), b.get("atr_period"), b.get("breakout_period"), b.get("stop_period"), b.get("stop_atr_multiple"), b.get("alpha"))
            if key not in seen:
                seen.add(key)
                best.append(b)

    if best:
        lines.append("| 模式 | ATR | 突破 | 止损 | 倍数 | α | Sharpe | CAGR% | MDD% | Trades | 评分 |")
        lines.append("|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|")
        for b in best[:5]:
            lines.append(f"| {b.get('mode','A')} | {b.get('atr_period','')} | {b.get('breakout_period','')} "
                         f"| {b.get('stop_period','')} | {b.get('stop_atr_multiple','')} | {b.get('alpha','')} "
                         f"| {b.get('sharpe','N/A')} | {b.get('cagr','N/A')} | {b.get('max_drawdown','N/A')} "
                         f"| {b.get('total_trades','N/A')} | {b.get('robustness_score',0):.4f} |")
    else:
        lines.append("> ⚠️ 网格搜索尚未运行。\n")

    # ── 逐参数 OOS 衰减追踪 ──
    decay_rows = []
    if df_full is not None and df_oos is not None and best:
        for b in best[:5]:
            mode_val = b.get("mode", "A")
            atr = int(b["atr_period"])
            breakout = int(b["breakout_period"])
            stop = int(b["stop_period"])
            mult = float(b["stop_atr_multiple"])
            alpha = float(b["alpha"])

            # IS 指标
            is_sharpe = b.get("sharpe")
            is_cagr = b.get("cagr")

            # OOS 指标匹配
            # [修复] 添加类型转换防止匹配失败
            if df_oos is not None and not df_oos.empty:
                mask = (
                    (df_oos["atr_period"].astype(int) == atr) &
                    (df_oos["breakout_period"].astype(int) == breakout) &
                    (df_oos["stop_period"].astype(int) == stop) &
                    (df_oos["stop_atr_multiple"].astype(float) == mult) &
                    (df_oos["alpha"].astype(float) == alpha) &
                    (df_oos["mode"].astype(str) == str(mode_val))
                )
                match = df_oos[mask]
            else:
                match = pd.DataFrame()

            if not match.empty:
                oos_sharpe = match["sharpe"].mean() if "sharpe" in match else None
                oos_cagr = match["cagr"].mean() if "cagr" in match else None
            else:
                oos_sharpe = None
                oos_cagr = None

            # 计算衰减
            if is_sharpe is not None and oos_sharpe is not None and abs(float(is_sharpe)) > 0.01:
                sh_decay = (float(is_sharpe) - float(oos_sharpe)) / abs(float(is_sharpe)) * 100
            else:
                sh_decay = None

            if is_cagr is not None and oos_cagr is not None and abs(float(is_cagr)) > 0.01:
                cagr_decay = (float(is_cagr) - float(oos_cagr)) / abs(float(is_cagr)) * 100
            else:
                cagr_decay = None

            decay_rows.append({
                "mode": mode_val,
                "params": f"atr={atr} b={breakout} s={stop} m={mult} α={alpha}",
                "is_sharpe": is_sharpe,
                "oos_sharpe": oos_sharpe,
                "sharpe_decay_pct": sh_decay,
                "is_cagr": is_cagr,
                "oos_cagr": oos_cagr,
                "cagr_decay_pct": cagr_decay,
            })

        if decay_rows:
            lines.append("\n### 样本外衰减（逐参数）\n")
            lines.append("| 模式 | 参数 | IS Sharpe | OOS Sharpe | Sharpe衰减% | IS CAGR% | OOS CAGR% | CAGR衰减% |")
            lines.append("|:--:|:--|:--:|:--:|:--:|:--:|:--:|:--:|")
            for r in decay_rows:
                sh_flag = " 🔴" if r["sharpe_decay_pct"] is not None and r["sharpe_decay_pct"] > 30 else ""
                cagr_flag = " 🔴" if r["cagr_decay_pct"] is not None and r["cagr_decay_pct"] > 30 else ""
                lines.append(
                    f"| {r['mode']} | {r['params']} "
                    f"| {r['is_sharpe']:.3f} | {r['oos_sharpe']:.3f} | {r['sharpe_decay_pct']:.0f}%{sh_flag} "
                    f"| {r['is_cagr']:.2f} | {r['oos_cagr']:.2f} | {r['cagr_decay_pct']:.0f}%{cagr_flag} |"
                )

            # 衰减分布汇总
            sh_decays = [r["sharpe_decay_pct"] for r in decay_rows if r["sharpe_decay_pct"] is not None]
            cagr_decays = [r["cagr_decay_pct"] for r in decay_rows if r["cagr_decay_pct"] is not None]
            if sh_decays:
                median_sh = np.median(sh_decays)
                high_sh = sum(1 for d in sh_decays if d > 30)
                lines.append(f"\n**衰减汇总**: Sharpe衰减中位数={median_sh:.0f}% | "
                             f"超过30%的个数={high_sh}/{len(sh_decays)}"
                             f"{' ⚠️ 强过拟合信号' if median_sh > 30 or high_sh >= len(sh_decays)//2 else ''}")
            if cagr_decays:
                median_cagr = np.median(cagr_decays)
                lines.append(f"**CAGR衰减**: 中位数={median_cagr:.0f}%")

    return "\n".join(lines)

def load_stress_conclusion() -> Optional[dict]:
    """从 S7 stress_conclusion.json 加载压力测试结论。"""
    path = STRESS_DIR / "stress_conclusion.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None

def load_stress_report() -> Optional[str]:
    """从 S7 stress_report.md 加载压力测试报告摘要。"""
    path = STRESS_DIR / "stress_report.md"
    if path.exists():
        report_text = path.read_text(encoding="utf-8")
        lines = report_text.splitlines()
        summary_lines = []

        # [修复] 改进的摘要提取逻辑：
        # 1. 提取前 60 行的标题和表格（头部）
        # 2. 查找“综合判定”部分并提取其后的几行（结论），防止被截断
        for line in lines[:60]:
            if any(line.startswith(h) for h in ["#", "|", "-", ">", "**"]):
                summary_lines.append(line)

        found_conclusion_index = -1
        for i, line in enumerate(lines):
            if "综合判定" in line:
                found_conclusion_index = i
                break

        if found_conclusion_index != -1:
            summary_lines.append("...") # 分隔符
            # 提取结论部分的上下文（例如结论行及其后5行）
            for i in range(found_conclusion_index, min(found_conclusion_index + 6, len(lines))):
                summary_lines.append(lines[i])

        return "\n".join(summary_lines) if summary_lines else None
    return None

def generate_stress_section() -> str:
    """生成压力测试章节，存在结果时内联摘要，否则优雅降级。"""
    conclusion = load_stress_conclusion()
    report_summary = load_stress_report()

    if conclusion is None:
        return (
            "## 6. 压力测试\n"
            "> ⚠️ 压力测试尚未运行。包含 A1-A4 历史情景 + B1-B2 合成情景。\n"
            "请先执行 `py scripts/run_stress_test.py` 和 `py scripts/run_correlation_monitor.py`。\n"
        )

    lines = ["## 6. 压力测试\n"]
    overall = conclusion.get("overall", {})
    status_map = {
        "pass": "✅ 全部通过",
        "conditional_pass": "⚠️ 条件通过",
        "fail": "❌ 不通过",
        "no_data": "⚪ 无数据",
    }
    status_str = status_map.get(overall.get("status", "no_data"), "⚪ 无数据")
    lines.append(f"**综合判定**: {status_str} | "
                 f"通过 {overall.get('passed', 0)}/{overall.get('total', 0)} 项检查\n")

    # 各场景摘要
    scenarios = conclusion.get("scenarios", [])
    if scenarios:
        lines.append("| 场景 | 总收益% | MDD% | Sharpe | 交易 | 通过? | 备注 |")
        lines.append("|:--|:--:|:--:|:--:|:--:|:--:|:--|")
        for sc in scenarios:
            ms = sc.get("metrics_summary", {})
            status = "✅" if sc.get("passed") else "❌"
            sharpe_str = f"{ms['sharpe']:.2f}" if ms.get('sharpe') is not None else "N/A(短区间)"
            failed_checks = [k for k, v in sc.get("checks", {}).items() if not v]
            note = ", ".join(failed_checks) if failed_checks else "—"
            lines.append(
                f"| {sc.get('scenario_name', sc['scenario'])} "
                f"| {ms.get('total_return', 'N/A')} "
                f"| {ms.get('max_drawdown', 'N/A')} "
                f"| {sharpe_str} "
                f"| {ms.get('total_trades', 'N/A')} "
                f"| {status} "
                f"| {note} |"
            )

    # 内联部分报告摘要
    if report_summary:
        lines.append("\n**报告摘要**:\n")
        lines.append(f"> 详细报告: `{STRESS_DIR / 'stress_report.md'}`\n")

    return "\n".join(lines)

def _per_symbol_table(per_symbol: dict) -> str:
    """生成品种级交易统计的 Markdown 表格。"""
    if not per_symbol:
        return ""
    lines = [
        "## 5. 品种级交易统计\n",
        "| 品种 | 交易次数 | 盈利次数 | 胜率 | 总盈亏 |",
        "|:--:|:--:|:--:|:--:|:--:|",
    ]
    for sym in sorted(per_symbol):
        v = per_symbol[sym]
        lines.append(f"| {sym} | {v['total']} | {v['won']} | {v['win_rate']}% | {v['pnl_sum']:,.0f} |")
    lines.append("")
    return "\n".join(lines)

def generate_comparison_section(df: Optional[pd.DataFrame]) -> str:
    """生成基准对比表格，数据不存在时返回占位符。"""
    STRAT_LABELS = {
        "B1": "B1 买入等权持有",
        "B2": "B2 等权再平衡",
        "B3": "B3 ATR等风险",
        "B4": "B4 海龟(纯策略)",
    }
    if df is None or df.empty:
        return (
            "## 3. 基准对比 (B1-B4)\n\n"
            "> ⚠️ 请先执行 `py scripts/run_comparison.py --save` 生成对比数据。\n\n"
            "基准定义（§4.4）：\n"
            + "\n".join(f"- {k}：{v.split(' ', 1)[1]}" for k, v in STRAT_LABELS.items())
            + "\n"
        )
    lines = ["## 3. 基准对比 (B1-B4)\n",
             "> | 代号 | 策略 |",
             "> |:--|:--|",
             "> | B1 | 买入等权持有 |",
             "> | B2 | 等权定期再平衡 |",
             "> | B3 | ATR 等风险贡献 |",
             "> | B4 | 海龟（纯策略，国债已移除） |",
             "\n"]
    lines.append("| 策略 | 总收益率% | 年化收益% | 夏普 | 最大回撤% | 年化波动% | 胜率% | 盈亏比 | 交易次数 | 最终净值 |")
    lines.append("|:--|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|")
    for idx, row in df.iterrows():
        label = STRAT_LABELS.get(idx, idx)
        lines.append(
            f"| {label} | {row.get('总收益率%', 'N/A')} | {row.get('年化收益%', 'N/A')} | "
            f"{row.get('夏普', 'N/A')} | {row.get('最大回撤%', 'N/A')} | {row.get('年化波动%', 'N/A')} | "
            f"{row.get('胜率%', 'N/A')} | {row.get('盈亏比', 'N/A')} | {row.get('交易次数', 'N/A')} | "
            f"{row.get('最终净值', 'N/A')} |"
        )
    return "\n".join(lines) + "\n"

def generate_report(metrics: dict, df_full: Optional[pd.DataFrame] = None, df_oos: Optional[pd.DataFrame] = None, mode: str = "A", start_date: str = "2014-01-01", end_date: str = "2026-06-10", per_symbol: Optional[dict] = None, comparison_df: Optional[pd.DataFrame] = None) -> str:
    mode_label = f"模式 {'A (无过滤)' if mode == 'A' else 'B (55日过滤)'}"
    sections = [
        f"# 跨市场ETF海龟组合策略 — 综合回测报告\n",
        f"**日期**: {datetime.now().strftime('%Y-%m-%d %H:%M')} | **模式**: {mode_label} | **区间**: {start_date} ~ {end_date}\n",
        "---\n",
        "## 1. 核心目标达成度\n",
        generate_summary_table(metrics),
        "\n---\n",
        "## 2. 核心绩效\n",
        generate_performance_table(metrics),
        "\n---\n",
        generate_comparison_section(comparison_df),
        "\n---\n",
        generate_params_section(df_full, df_oos),
        # ── 品种级交易统计（可选） ──
        (f"\n---\n{_per_symbol_table(per_symbol)}" if per_symbol else ""),
        "\n---\n",
        generate_stress_section(),
        "\n---\n",
        "*报告由 `scripts/gen_report.py` 自动生成*\n",
    ]
    return "\n".join(sections)

# ════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="跨市场ETF海龟组合策略 — 综合报告生成 (S8)")
    parser.add_argument("--params", type=str, default=None)
    parser.add_argument("--start", type=str, default="2014-01-01")
    parser.add_argument("--end", type=str, default="2026-06-10")
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--mode", type=str, choices=["A", "B"], default="A")
    parser.add_argument("--no-backtest", action="store_true", default=False)
    parser.add_argument("--verbose", "-v", action="store_true", default=False)

    args = parser.parse_args()
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    output_path = Path(args.output) if args.output else DEFAULT_OUTPUT
    output_path.parent.mkdir(parents=True, exist_ok=True)

    params_path = Path(args.params) if args.params else None
    best = load_best_params(params_path)
    df_full = load_grid_results()
    df_oos = load_oos_results()

    with open(CONFIG_PATH, "r", encoding="utf-8") as _cfg_f:
        _cfg = yaml.safe_load(_cfg_f)

    if args.no_backtest:
        metrics = {
            "mode": args.mode, "start_date": args.start, "end_date": args.end,
            "initial_cash": _cfg.get("initial_cash", 120000), "final_value": 0,
            "total_return": 0, "cagr": 0, "sharpe": None, "max_drawdown": 0,
            "win_rate": 0, "profit_factor": 0, "total_trades": 0, "annual_vol": 0,
            "calmar": 0, "concentration_cut": 0, "dd_warning": 0, "loss_pause": 0, "t1_stop_delay": 0
        }
    else:
        metrics = run_backtest_with_best(best, args.start, args.end, args.mode)
        if not metrics:
            logger.error("回测失败"); sys.exit(1)

    # 从指标中取出品种级数据
    per_symbol = metrics.pop("per_symbol", {})

    # 加载基准对比数据
    comparison_df = load_comparison_results()

    report = generate_report(metrics, df_full, df_oos, args.mode, args.start, args.end, per_symbol=per_symbol, comparison_df=comparison_df)

    output_path.write_text(report, encoding="utf-8")
    logger.info("报告已保存: %s (%d 行)", output_path, len(report.splitlines()))

    metrics_path = output_path.with_suffix(".json").with_stem(output_path.stem + "_metrics")
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("指标 JSON 已保存: %s", metrics_path)

    print(f"\n{'=' * 60}\nS8 综合报告已生成\n 报告: {output_path}\n 指标: {metrics_path}\n{'=' * 60}")

if __name__ == "__main__":
    main()
