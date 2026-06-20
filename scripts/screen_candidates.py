#!/usr/bin/env python
"""
跨市场ETF海龟组合策略 · 品种筛选工具 (§5.12)

基于 §2.2 定义的 8 道硬门槛，对候选品种进行自动化逐道检查。
任一 REJECT 即淘汰。WARN 不阻断但记录。

用法：
    py scripts/screen_candidates.py                           # 自动发现候选，全量筛查
    py scripts/screen_candidates.py -s 588000.SH,159845.SZ    # 筛查指定候选
    py scripts/screen_candidates.py --skip-backtest           # 快速模式，跳过回测
    py scripts/screen_candidates.py --output results/my_screening.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yaml

# 确保项目根在 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config_loader import get_trading_symbols
from src.turtle_core import hurst_exponent

# 日志配置（推迟到 main() 中初始化，避免与 pytest 冲突）
log = logging.getLogger("screen_candidates")

# ── 路径 ──
CONFIG_PATH = ROOT / "config" / "turtle_config.yaml"
DATA_DIR = ROOT / "data" / "etf_daily"
RESULT_DIR = ROOT / "results"

# 加载现有组合
with open(CONFIG_PATH, "r", encoding="utf-8") as _f:
    _CONFIG = yaml.safe_load(_f)
EXISTING_SYMBOLS = get_trading_symbols(_CONFIG)


# ════════════════════════════════════════════════════════════
#  数据类
# ════════════════════════════════════════════════════════════

class CheckVerdict(Enum):
    PASS = "pass"
    WARN = "warn"
    REJECT = "reject"
    SKIP = "skip"


@dataclass
class SingleCheck:
    """单道检查结果"""
    stage: str
    verdict: str            # "pass" | "warn" | "reject" | "skip"
    metric_name: str
    metric_value: Any
    threshold: str
    detail: str
    elapsed_sec: float = 0.0


@dataclass
class CandidateResult:
    """单一候选的完整检查结果"""
    symbol: str
    name: str
    final_verdict: str      # "pass" | "warn" | "reject"
    checks: List[SingleCheck] = field(default_factory=list)
    stopped_at_stage: str = ""


@dataclass
class ScreeningReport:
    """全量报告"""
    timestamp: str
    existing_universe: List[str]
    candidates: List[CandidateResult]
    summary: Dict[str, int]


# ════════════════════════════════════════════════════════════
#  辅助函数
# ════════════════════════════════════════════════════════════

def _load_parquet(symbol: str) -> Optional[pd.DataFrame]:
    """加载单个品种的本地缓存数据。"""
    path = DATA_DIR / f"{symbol}.parquet"
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
        if df.empty:
            return None
        df["date"] = pd.to_datetime(df["date"])
        df.sort_values("date", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df
    except Exception:
        return None


def _resolve_name(symbol: str) -> str:
    """从 config 查找品种名称，找不到则返回 symbol 本身。"""
    for s in _CONFIG.get("symbols", []):
        if s["code"] == symbol:
            return s.get("name", symbol)
    return symbol


# ════════════════════════════════════════════════════════════
#  [1] 数据质量检查
# ════════════════════════════════════════════════════════════

def check_data_quality(symbol: str) -> SingleCheck:
    """复用 cross_validate 的 validate_symbol。"""
    t0 = time.time()
    try:
        from scripts.cross_validate import validate_symbol
        report = validate_symbol(symbol)
        if report is None:
            return SingleCheck(
                stage="data_quality", verdict="warn",
                metric_name="validation_report", metric_value="None",
                threshold="worst_level != critical",
                detail="校验返回 None（可能网络问题），跳过",
                elapsed_sec=round(time.time() - t0, 1),
            )
        worst = report.worst_level
        if worst == "critical":
            verdict = "reject"
            detail = f"CRITICAL: {report.worst_detail[:120]}"
        elif worst == "error":
            verdict = "warn"
            detail = f"ERROR: {report.worst_detail[:120]}"
        else:
            verdict = "pass"
            detail = f"最高级别={worst}, 交叉校验通过"
        return SingleCheck(
            stage="data_quality", verdict=verdict,
            metric_name="worst_level", metric_value=worst,
            threshold="worst_level != critical",
            detail=detail,
            elapsed_sec=round(time.time() - t0, 1),
        )
    except ImportError:
        return SingleCheck(
            stage="data_quality", verdict="skip",
            metric_name="N/A", metric_value="N/A",
            threshold="worst_level != critical",
            detail="cross_validate 模块不可用，跳过",
            elapsed_sec=0.0,
        )
    except Exception as e:
        return SingleCheck(
            stage="data_quality", verdict="warn",
            metric_name="exception", metric_value=str(e)[:80],
            threshold="worst_level != critical",
            detail=f"校验异常: {str(e)[:120]}",
            elapsed_sec=round(time.time() - t0, 1),
        )


# ════════════════════════════════════════════════════════════
#  [2] 上市年限检查
# ════════════════════════════════════════════════════════════

# 已知品种实际上市日期（用于上市年限检查）
# 数据源：交易所公开信息，格式 {code: "YYYY-MM-DD"}
_KNOWN_LISTING_DATES = {
    "510500.SH": "2013-02-06",   # 中证500ETF
    "159845.SZ": "2019-05-27",   # 中证1000ETF（2021年由原份额折算而来）
    "159915.SZ": "2011-12-09",   # 创业板ETF
    "588000.SH": "2020-09-28",   # 科创50ETF
    "513100.SH": "2014-08-08",   # 纳指ETF
    "518880.SH": "2013-11-29",   # 黄金ETF
    "511010.SH": "2013-08-26",   # 国债ETF
}


def check_listing_age(symbol: str, min_first_date: str = "2018-01-01") -> SingleCheck:
    """检查品种上市时间是否足够覆盖至少一轮牛熊。

    优先使用已知上市日期字典，其次从 Parquet 缓存数据推断。
    """
    t0 = time.time()
    # 优先查已知上市日期
    if symbol in _KNOWN_LISTING_DATES:
        first_date = pd.Timestamp(_KNOWN_LISTING_DATES[symbol])
    else:
        df = _load_parquet(symbol)
        if df is None or df.empty:
            return SingleCheck(
                stage="listing_age", verdict="reject",
                metric_name="first_date", metric_value="N/A",
                threshold=f"first_date <= {min_first_date}",
                detail="无法加载数据",
                elapsed_sec=round(time.time() - t0, 1),
            )
        first_date = df["date"].iloc[0]

    first_str = str(first_date.date())
    min_dt = pd.Timestamp(min_first_date)
    if first_date > min_dt:
        return SingleCheck(
            stage="listing_age", verdict="reject",
            metric_name="first_date", metric_value=first_str,
            threshold=f"first_date <= {min_first_date}",
            detail=f"首日 {first_str}，晚于要求 {min_first_date}（不足7年，未覆盖完整牛熊）",
            elapsed_sec=round(time.time() - t0, 1),
        )
    return SingleCheck(
        stage="listing_age", verdict="pass",
        metric_name="first_date", metric_value=first_str,
        threshold=f"first_date <= {min_first_date}",
        detail=f"首日 {first_str}，满足 ≥{min_first_date}",
        elapsed_sec=round(time.time() - t0, 1),
    )


# ════════════════════════════════════════════════════════════
#  [3] 流动性检查
# ════════════════════════════════════════════════════════════

def check_liquidity(symbol: str, min_avg_vol: float = 2e8) -> SingleCheck:
    """检查近252日日均成交额。"""
    t0 = time.time()
    df = _load_parquet(symbol)
    if df is None or df.empty:
        return SingleCheck(
            stage="liquidity", verdict="warn",
            metric_name="avg_vol_252d", metric_value="N/A",
            threshold=f"avg_vol > {min_avg_vol / 1e8:.0f}亿",
            detail="无法加载数据",
            elapsed_sec=round(time.time() - t0, 1),
        )
    if "amount" not in df.columns and "volume" not in df.columns:
        return SingleCheck(
            stage="liquidity", verdict="warn",
            metric_name="avg_vol_252d", metric_value="N/A",
            threshold=f"avg_vol > {min_avg_vol / 1e8:.0f}亿",
            detail="数据缺少 amount/volume 列",
            elapsed_sec=round(time.time() - t0, 1),
        )
    # 优先使用 amount（成交额），其次用 volume 近似
    if "amount" in df.columns:
        recent = df["amount"].tail(252)
        avg_vol = float(recent.mean()) if len(recent) > 0 else 0.0
    else:
        # volume 是手数，近似：假设均价 ~5元，每手100股
        recent = df["volume"].tail(252)
        avg_vol = float(recent.mean()) * 500 if len(recent) > 0 else 0.0

    if avg_vol < min_avg_vol:
        return SingleCheck(
            stage="liquidity", verdict="warn",
            metric_name="avg_vol_252d", metric_value=f"{avg_vol / 1e8:.2f}亿",
            threshold=f"avg_vol > {min_avg_vol / 1e8:.0f}亿",
            detail=f"近252日均成交额 {avg_vol / 1e8:.2f}亿 < {min_avg_vol / 1e8:.0f}亿",
            elapsed_sec=round(time.time() - t0, 1),
        )
    return SingleCheck(
        stage="liquidity", verdict="pass",
        metric_name="avg_vol_252d", metric_value=f"{avg_vol / 1e8:.2f}亿",
        threshold=f"avg_vol > {min_avg_vol / 1e8:.0f}亿",
        detail=f"近252日均成交额 {avg_vol / 1e8:.2f}亿",
        elapsed_sec=round(time.time() - t0, 1),
    )


# ════════════════════════════════════════════════════════════
#  [4] 趋势持续性检查（Hurst 指数）
# ════════════════════════════════════════════════════════════

def check_trend_persistence(symbol: str) -> SingleCheck:
    """计算 Hurst 指数判断趋势持续性。"""
    t0 = time.time()
    df = _load_parquet(symbol)
    if df is None or df.empty or "close" not in df.columns:
        return SingleCheck(
            stage="trend_persistence", verdict="warn",
            metric_name="hurst", metric_value="N/A",
            threshold="H >= 0.50",
            detail="无法加载数据或缺少 close 列",
            elapsed_sec=round(time.time() - t0, 1),
        )
    closes = df["close"].values
    if len(closes) < 252:
        return SingleCheck(
            stage="trend_persistence", verdict="skip",
            metric_name="hurst", metric_value="N/A",
            threshold="H >= 0.50",
            detail=f"数据点 {len(closes)} < 252，不足计算滚动 Hurst",
            elapsed_sec=round(time.time() - t0, 1),
        )

    # 计算 252 日滚动 Hurst
    window = 252
    n_windows = len(closes) - window + 1
    if n_windows < 5:
        median_H = float(hurst_exponent(closes))
    else:
        hurst_vals = []
        step = max(1, n_windows // 20)
        for i in range(0, n_windows, step):
            sub = closes[i:i + window]
            hurst_vals.append(hurst_exponent(sub))
        median_H = float(np.median(hurst_vals))

    if median_H < 0.45:
        verdict = "warn"
        detail = f"滚动 Hurst 中位数 {median_H:.3f} < 0.45，均值回归倾向，不适合趋势策略"
    elif median_H < 0.50:
        verdict = "warn"
        detail = f"滚动 Hurst 中位数 {median_H:.3f}，偏弱趋势"
    elif median_H >= 0.55:
        verdict = "pass"
        detail = f"滚动 Hurst 中位数 {median_H:.3f}，趋势持续性良好"
    else:
        verdict = "pass"
        detail = f"滚动 Hurst 中位数 {median_H:.3f}，接近随机游走，海龟仍可捕捉肥尾"

    return SingleCheck(
        stage="trend_persistence", verdict=verdict,
        metric_name="hurst_median", metric_value=round(median_H, 3),
        threshold="H >= 0.50",
        detail=detail,
        elapsed_sec=round(time.time() - t0, 1),
    )


# ════════════════════════════════════════════════════════════
#  [5] 单品种海龟独立回测
# ════════════════════════════════════════════════════════════

def check_standalone_backtest(
    symbol: str,
    start_date: str,
    end_date: str,
    mode: str = "A",
) -> SingleCheck:
    """对单个品种独立运行海龟回测，检查盈亏比和 CAGR。"""
    t0 = time.time()
    try:
        from scripts.run_backtest import run_backtest
        result = run_backtest(
            start_date=start_date,
            end_date=end_date,
            mode=mode,
            quiet=True,
            symbols_override=[symbol],
        )
    except ImportError as e:
        return SingleCheck(
            stage="standalone_backtest", verdict="skip",
            metric_name="N/A", metric_value="N/A",
            threshold="profit_factor >= 1.0, CAGR > 0",
            detail=f"无法导入 run_backtest: {e}",
            elapsed_sec=round(time.time() - t0, 1),
        )
    except Exception as e:
        return SingleCheck(
            stage="standalone_backtest", verdict="warn",
            metric_name="exception", metric_value=str(e)[:80],
            threshold="profit_factor >= 1.0, CAGR > 0",
            detail=f"回测异常: {str(e)[:120]}",
            elapsed_sec=round(time.time() - t0, 1),
        )

    if result is None:
        return SingleCheck(
            stage="standalone_backtest", verdict="warn",
            metric_name="N/A", metric_value="N/A",
            threshold="profit_factor >= 1.0, CAGR > 0",
            detail="回测返回 None（可能数据不足）",
            elapsed_sec=round(time.time() - t0, 1),
        )

    pf = result.get("profit_factor")
    total_ret = result.get("total_return_pct", 0)
    trades = result.get("total_trades", 0)
    win_rate = result.get("win_rate", 0)

    # 计算 CAGR（简单近似）
    years = (pd.Timestamp(end_date) - pd.Timestamp(start_date)).days / 365.25
    if years > 0 and total_ret > -100:
        cagr = ((1 + total_ret / 100) ** (1 / years) - 1) * 100
    else:
        cagr = 0.0

    detail_parts = [
        f"盈亏比={pf:.2f}" if pf is not None else "盈亏比=N/A",
        f"CAGR={cagr:.1f}%",
        f"交易={trades}次",
        f"胜率={win_rate:.1f}%",
    ]

    if pf is not None and pf < 1.0:
        verdict = "reject"
        detail_parts.insert(0, "REJECT:")
    elif cagr <= 0:
        verdict = "reject"
        detail_parts.insert(0, "REJECT:")
    else:
        verdict = "pass"
        detail_parts.insert(0, "PASS:")

    return SingleCheck(
        stage="standalone_backtest", verdict=verdict,
        metric_name="profit_factor", metric_value=round(pf, 3) if pf is not None else None,
        threshold="profit_factor >= 1.0, CAGR > 0",
        detail=" | ".join(detail_parts),
        elapsed_sec=round(time.time() - t0, 1),
    )


# ════════════════════════════════════════════════════════════
#  [6] 相关性 vs 现有组合
# ════════════════════════════════════════════════════════════

def check_correlation(
    symbol: str,
    existing_symbols: List[str],
    start_date: str,
    end_date: str,
) -> SingleCheck:
    """检查候选品种与现有组合的 60 日滚动平均相关性。"""
    t0 = time.time()
    if not existing_symbols:
        return SingleCheck(
            stage="correlation", verdict="pass",
            metric_name="N/A", metric_value="N/A",
            threshold="avg ρ <= 0.5 vs each existing",
            detail="现有组合为空，跳过相关性检查",
            elapsed_sec=0.0,
        )

    try:
        from scripts.run_correlation_monitor import load_price_matrix, compute_rolling_correlation
    except ImportError as e:
        return SingleCheck(
            stage="correlation", verdict="skip",
            metric_name="N/A", metric_value="N/A",
            threshold="avg ρ <= 0.5 vs each existing",
            detail=f"无法导入 correlation_monitor: {e}",
            elapsed_sec=0.0,
        )

    all_symbols = existing_symbols + [symbol]
    try:
        price_df = load_price_matrix(all_symbols, start_date, end_date)
    except Exception as e:
        return SingleCheck(
            stage="correlation", verdict="skip",
            metric_name="N/A", metric_value="N/A",
            threshold="avg ρ <= 0.5 vs each existing",
            detail=f"加载价格矩阵失败: {str(e)[:100]}",
            elapsed_sec=round(time.time() - t0, 1),
        )

    if price_df is None or price_df.empty:
        return SingleCheck(
            stage="correlation", verdict="skip",
            metric_name="N/A", metric_value="N/A",
            threshold="avg ρ <= 0.5 vs each existing",
            detail="价格矩阵为空",
            elapsed_sec=round(time.time() - t0, 1),
        )

    try:
        corr_df = compute_rolling_correlation(price_df, window=60)
    except Exception as e:
        return SingleCheck(
            stage="correlation", verdict="skip",
            metric_name="N/A", metric_value="N/A",
            threshold="avg ρ <= 0.5 vs each existing",
            detail=f"计算滚动相关性失败: {str(e)[:100]}",
            elapsed_sec=round(time.time() - t0, 1),
        )

    if corr_df is None or corr_df.empty:
        return SingleCheck(
            stage="correlation", verdict="skip",
            metric_name="N/A", metric_value="N/A",
            threshold="avg ρ <= 0.5 vs each existing",
            detail="滚动相关性结果为空",
            elapsed_sec=round(time.time() - t0, 1),
        )

    # 从 avg_corr 序列取均值作为整体度量
    avg_corr = float(corr_df["avg_corr"].mean()) if "avg_corr" in corr_df.columns else 0.5

    if avg_corr > 0.5:
        return SingleCheck(
            stage="correlation", verdict="warn",
            metric_name="avg_correlation_60d", metric_value=round(avg_corr, 3),
            threshold="avg ρ <= 0.5 vs each existing",
            detail=f"60日平均相关系数 {avg_corr:.3f} > 0.5，与现有组合相关性偏高",
            elapsed_sec=round(time.time() - t0, 1),
        )
    return SingleCheck(
        stage="correlation", verdict="pass",
        metric_name="avg_correlation_60d", metric_value=round(avg_corr, 3),
        threshold="avg ρ <= 0.5 vs each existing",
        detail=f"60日平均相关系数 {avg_corr:.3f}，低相关，合格",
        elapsed_sec=round(time.time() - t0, 1),
    )


# ════════════════════════════════════════════════════════════
#  [7] T+1 占比检查
# ════════════════════════════════════════════════════════════

def check_t1_ratio(
    symbol: str,
    existing_symbols: List[str],
) -> SingleCheck:
    """检查加入该品种后 T+1 品种占比是否超过 50%。

    T+1 品种在极端行情下的止损滞后是结构性问题，
    组合中 T+1 占比应不超过 50%。
    """
    t0 = time.time()
    from src.config_loader import get_t_plus_one_symbols
    t1_set = get_t_plus_one_symbols(_CONFIG)

    # 候选品种可能不在 config 中，用后缀推断 T+1 状态
    # A股 ETF（上交所/深交所）为 T+1，跨境/商品为 T+0
    # T+0 品种列表（已知的跨境和商品 ETF）
    T0_CODES = {"513100", "513500", "518880", "159985"}
    def _is_t1(code: str) -> bool:
        if code in t1_set:
            return True
        # 不在 config 中则通过代码推断
        prefix = code.split(".")[0][:6] if "." in code else code[:6]
        if prefix in T0_CODES:
            return False
        # 上交所 51xxxx/56xxxx/58xxxx 或 深交所 1xxxxx/3xxxxx → A股 ETF → T+1
        return True

    candidate_is_t1 = _is_t1(symbol)
    existing_t1_count = sum(1 for s in existing_symbols if _is_t1(s))
    new_t1_count = existing_t1_count + (1 if candidate_is_t1 else 0)
    total_after = len(existing_symbols) + 1
    ratio = new_t1_count / total_after if total_after > 0 else 0

    if ratio > 0.5:
        return SingleCheck(
            stage="t1_ratio", verdict="warn",
            metric_name="t1_ratio_after", metric_value=f"{ratio:.0%}",
            threshold="T+1 占比 ≤ 50%",
            detail=f"加入后 T+1={new_t1_count}/{total_after}={ratio:.0%} > 50%，止损滞后风险偏高",
            elapsed_sec=round(time.time() - t0, 1),
        )
    return SingleCheck(
        stage="t1_ratio", verdict="pass",
        metric_name="t1_ratio_after", metric_value=f"{ratio:.0%}",
        threshold="T+1 占比 ≤ 50%",
        detail=f"加入后 T+1={new_t1_count}/{total_after}={ratio:.0%}，在安全线内",
        elapsed_sec=round(time.time() - t0, 1),
    )


# ════════════════════════════════════════════════════════════
#  筛查主流程
# ════════════════════════════════════════════════════════════

def screen_candidate(
    symbol: str,
    existing_symbols: List[str],
    start_date: str,
    end_date: str,
    skip_backtest: bool = False,
    skip_cross_validate: bool = False,
) -> CandidateResult:
    """对单个候选品种执行全流程筛查。

    遵循硬门槛顺序：任一 REJECT 即停止后续检查。
    """
    name = _resolve_name(symbol)
    result = CandidateResult(symbol=symbol, name=name, final_verdict="pass")
    checks: List[SingleCheck] = []

    # [1] 数据质量 → REJECT 阻断
    if not skip_cross_validate:
        c = check_data_quality(symbol)
    else:
        c = SingleCheck(
            stage="data_quality", verdict="skip",
            metric_name="N/A", metric_value="N/A",
            threshold="worst_level != critical",
            detail="--skip-backtest 或 skip_cross_validate=True 已跳过",
        )
    checks.append(c)
    log.info("[%s] ①数据质量: %s — %s", symbol, c.verdict.upper(), c.detail[:80])
    if c.verdict == "reject":
        result.final_verdict = "reject"
        result.stopped_at_stage = "data_quality"
        result.checks = checks
        return result

    # [2] 上市年限 → REJECT 阻断
    c = check_listing_age(symbol)
    checks.append(c)
    log.info("[%s] ②上市年限: %s — %s", symbol, c.verdict.upper(), c.detail[:80])
    if c.verdict == "reject":
        result.final_verdict = "reject"
        result.stopped_at_stage = "listing_age"
        result.checks = checks
        return result

    # [3] 流动性 → WARN 不阻断
    c = check_liquidity(symbol)
    checks.append(c)
    log.info("[%s] ③流动性: %s — %s", symbol, c.verdict.upper(), c.detail[:80])
    if c.verdict == "warn":
        result.final_verdict = "warn"

    # [4] 趋势持续性 → WARN 不阻断
    c = check_trend_persistence(symbol)
    checks.append(c)
    log.info("[%s] ④趋势(Hurst): %s — %s", symbol, c.verdict.upper(), c.detail[:80])
    if c.verdict == "warn" and result.final_verdict == "pass":
        result.final_verdict = "warn"

    # [5] 单品种回测 → REJECT 阻断
    if not skip_backtest:
        c = check_standalone_backtest(symbol, start_date, end_date)
        checks.append(c)
        log.info("[%s] ⑤独立回测: %s — %s", symbol, c.verdict.upper(), c.detail[:80])
        if c.verdict == "reject":
            result.final_verdict = "reject"
            result.stopped_at_stage = "standalone_backtest"
            result.checks = checks
            return result
        if c.verdict == "warn" and result.final_verdict == "pass":
            result.final_verdict = "warn"
    else:
        checks.append(SingleCheck(
            stage="standalone_backtest", verdict="skip",
            metric_name="N/A", metric_value="N/A",
            threshold="profit_factor >= 1.0, CAGR > 0",
            detail="--skip-backtest 已跳过",
        ))

    # [6] 相关性 → WARN 不阻断
    c = check_correlation(symbol, existing_symbols, start_date, end_date)
    checks.append(c)
    log.info("[%s] ⑥相关性: %s — %s", symbol, c.verdict.upper(), c.detail[:80])
    if c.verdict == "warn" and result.final_verdict == "pass":
        result.final_verdict = "warn"

    # [7] T+1 占比 → WARN 不阻断
    c = check_t1_ratio(symbol, existing_symbols)
    checks.append(c)
    log.info("[%s] ⑦T+1占比: %s — %s", symbol, c.verdict.upper(), c.detail[:80])
    if c.verdict == "warn" and result.final_verdict == "pass":
        result.final_verdict = "warn"

    result.checks = checks
    return result


def screen_all(
    candidates: List[str],
    existing_symbols: List[str],
    start_date: str,
    end_date: str,
    skip_backtest: bool = False,
    skip_cross_validate: bool = False,
) -> ScreeningReport:
    """批量筛查。"""
    results = []
    for i, sym in enumerate(candidates, 1):
        log.info("=" * 50)
        log.info("[%d/%d] 筛查 %s", i, len(candidates), sym)
        log.info("=" * 50)
        r = screen_candidate(sym, existing_symbols, start_date, end_date,
                              skip_backtest, skip_cross_validate)
        results.append(r)

    total = len(results)
    pass_count = sum(1 for r in results if r.final_verdict == "pass")
    reject_count = sum(1 for r in results if r.final_verdict == "reject")
    warn_only = sum(1 for r in results if r.final_verdict == "warn")

    return ScreeningReport(
        timestamp=datetime.now().isoformat(),
        existing_universe=existing_symbols,
        candidates=results,
        summary={
            "total": total,
            "pass": pass_count,
            "reject": reject_count,
            "warn_only": warn_only,
        },
    )


# ════════════════════════════════════════════════════════════
#  输出
# ════════════════════════════════════════════════════════════

def print_summary(report: ScreeningReport) -> None:
    """打印控制台表格。"""
    VERDICT_MAP = {"pass": "✅ PASS", "warn": "⚠ WARN", "reject": "❌ REJECT"}

    print()
    print("═" * 92)
    print(f"  品种筛选报告  |  现有组合: {', '.join(report.existing_universe)}")
    print("═" * 92)
    print(f"  {'候选品种':<16} {'结论':<12} {'卡在':<18} {'关键指标'}")
    print("─" * 92)

    for r in report.candidates:
        stopped = r.stopped_at_stage if r.stopped_at_stage else "—"
        last = r.checks[-1] if r.checks else None
        key = f"{last.metric_name}={last.metric_value}" if last else "—"
        print(f"  {r.symbol:<16} {VERDICT_MAP.get(r.final_verdict, r.final_verdict):<12} {stopped:<18} {key}")

    print("─" * 92)
    s = report.summary
    print(f"  通过: {s['pass']}  |  淘汰: {s['reject']}  |  仅警告: {s['warn_only']}")
    print("═" * 92)

    if s["pass"] == 0:
        print("  ℹ 无候选通过全部硬门槛。建议考察 A股非权益象限（可转债/REITs）。")
    else:
        passed = [r.symbol for r in report.candidates if r.final_verdict == "pass"]
        print(f"  → 建议进入组合回测: {', '.join(passed)}")
    print()


def export_report(report: ScreeningReport, output_path: Path) -> None:
    """导出 JSON 报告。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def _to_dict(obj):
        if hasattr(obj, "__dataclass_fields__"):
            return {k: _to_dict(v) for k, v in asdict(obj).items()}
        if isinstance(obj, list):
            return [_to_dict(v) for v in obj]
        return obj

    data = _to_dict(report)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info("报告已保存: %s", output_path)


# ════════════════════════════════════════════════════════════
#  候选发现
# ════════════════════════════════════════════════════════════

def discover_candidates(existing: List[str]) -> List[str]:
    """从 data/etf_daily/ 自动发现不在现有组合中的品种。"""
    if not DATA_DIR.exists():
        return []
    existing_set = set(existing)
    candidates = []
    for f in sorted(DATA_DIR.glob("*.parquet")):
        code = f.stem
        if code not in existing_set:
            candidates.append(code)
    return candidates


# ════════════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="跨市场ETF海龟组合策略 — 品种筛选工具 (§5.12)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  py scripts/screen_candidates.py
  py scripts/screen_candidates.py -s 588000.SH,159845.SZ
  py scripts/screen_candidates.py --skip-backtest
  py scripts/screen_candidates.py --start 2022-01-01 --end 2026-06-10
        """,
    )
    parser.add_argument(
        "--symbols", "-s",
        type=str,
        default="",
        help="逗号分隔的候选品种代码。不指定则自动发现 data/etf_daily/ 中非组合品种。",
    )
    parser.add_argument(
        "--existing",
        type=str,
        default="",
        help="现有组合品种（逗号分隔）。默认从 config 读取。",
    )
    parser.add_argument(
        "--start",
        type=str,
        default="2020-01-01",
        help="回测区间起始 (默认: 2020-01-01)",
    )
    parser.add_argument(
        "--end",
        type=str,
        default="2026-06-10",
        help="回测区间截止 (默认: 2026-06-10)",
    )
    parser.add_argument(
        "--skip-backtest",
        action="store_true",
        default=False,
        help="跳过第5步独立回测（快速模式）",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default="results/screening_report.json",
        help="输出 JSON 路径 (默认: results/screening_report.json)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="详细日志",
    )

    args = parser.parse_args()

    # 配置日志
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # 确定候选列表
    if args.symbols:
        candidates = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        candidates = discover_candidates(EXISTING_SYMBOLS)
        if not candidates:
            log.info("data/etf_daily/ 中无新增候选品种。")
            return

    # 确定现有组合
    if args.existing:
        existing = [s.strip() for s in args.existing.split(",") if s.strip()]
    else:
        existing = EXISTING_SYMBOLS

    log.info("现有组合: %s", existing)
    log.info("候选品种: %s", candidates)
    log.info("回测区间: %s ~ %s", args.start, args.end)

    # 执行筛查
    report = screen_all(
        candidates=candidates,
        existing_symbols=existing,
        start_date=args.start,
        end_date=args.end,
        skip_backtest=args.skip_backtest,
    )

    # 输出
    print_summary(report)
    export_report(report, ROOT / args.output)


if __name__ == "__main__":
    main()
