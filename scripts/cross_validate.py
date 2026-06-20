#!/usr/bin/env python
"""
TickFlow <-> Tushare 数据交叉校验 (turtle_v2 适配版)
==============================================
用法：
    py scripts/cross_validate.py                    # 全品种校验
    py scripts/cross_validate.py -s 510500          # 单品种
    py scripts/cross_validate.py --export           # 导出完整差异报告
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import warnings
# Windows GBK 编码兼容：处理第三方库中的 emoji 字符
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
warnings.filterwarnings("ignore", category=UnicodeWarning)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cross_validate")

# ── 品种列表（从配置动态读取） ──
import yaml
with open(ROOT / "config" / "turtle_config.yaml", "r", encoding="utf-8") as _f:
    _CV_CONFIG = yaml.safe_load(_f)
SIX_SYMBOLS = [s["code"][:6] for s in _CV_CONFIG.get("symbols", [])]
BOND_SYMBOL = "511010"  # 国债 ETF，可选校验
ALL_SYMBOLS = SIX_SYMBOLS  # + [BOND_SYMBOL]

DATA_DIR = ROOT / "data" / "etf_daily"
RESULT_DIR = ROOT / "results"


# ════════════════════════════════════════════════════════════
#  配置
# ════════════════════════════════════════════════════════════

class ValidationLevel(Enum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class Config:
    """校验配置"""
    return_diff_warn: float = 0.005      # 收益率差异 > 0.5% → WARNING
    return_diff_error: float = 0.02      # 收益率差异 > 2% → ERROR
    volume_diff_warn: float = 0.10       # 成交量差异 > 10% → WARNING
    volume_diff_error: float = 0.30      # 成交量差异 > 30% → ERROR
    consecutive_warn_days: int = 1       # 连续超阈值天数 → 阻断新仓
    consecutive_error_days: int = 2      # 连续超阈值天数 → 阻断全部
    critical_multiplier: float = 3.0     # 单日差异 > N×阈值 → 直接 CRITICAL
    tickflow_max_count: int = 2000       # TickFlow 一次拉取最大条数
    lookback_days: int = 60              # 默认校验最近 N 个交易日

    # tickflow 时区参数
    tf_utc_hour: int = 16
    bj_offset_hours: int = 8


cfg = Config()


# ════════════════════════════════════════════════════════════
#  S1  TickFlow 数据拉取
# ════════════════════════════════════════════════════════════

def _to_tf_code(symbol: str) -> str:
    """转换为 TickFlow 代码格式"""
    # TickFlow 需要后缀
    if len(symbol) == 6:
        if symbol.startswith("6") or symbol.startswith("5"):
            return f"{symbol}.SH"
        elif symbol.startswith("0") or symbol.startswith("1") or symbol.startswith("3"):
            return f"{symbol}.SZ"
        return f"{symbol}.SH"
    return symbol


def fetch_tickflow_daily(symbol: str, count: int = 2000) -> pd.DataFrame:
    """从 TickFlow 免费版拉取日线数据"""
    import tickflow as tf

    tf_code = _to_tf_code(symbol)
    log.info("TickFlow: 拉取 %s (count=%d)", tf_code, count)

    client = tf.TickFlow.free()
    raw = client.klines.get(tf_code, period="1d", count=count)

    df = pd.DataFrame(raw)

    # 时区处理：UTC+0 16:00 → 北京时间次日
    df["timestamp_dt"] = pd.to_datetime(df["timestamp"], unit="ms")
    df["date_bj"] = (
        df["timestamp_dt"]
        .dt.tz_localize("UTC")
        .dt.tz_convert("Asia/Shanghai")
        .dt.date
    )

    df = df.sort_values("timestamp").drop_duplicates("date_bj", keep="last")
    df = df.sort_values("date_bj").reset_index(drop=True)

    result = pd.DataFrame({
        "date": pd.to_datetime(df["date_bj"]),
        "open": df["open"].astype(float),
        "high": df["high"].astype(float),
        "low": df["low"].astype(float),
        "close": df["close"].astype(float),
        "volume": df["volume"].astype(float),
    })
    log.info("TickFlow: %s → %d 条 (%s ~ %s)",
             tf_code, len(result),
             result["date"].min().date(), result["date"].max().date())
    return result


# ════════════════════════════════════════════════════════════
#  S2  Tushare 数据读取（turtle_v2 parquet 格式）
# ════════════════════════════════════════════════════════════

def load_tushare_parquet(symbol: str) -> pd.DataFrame:
    """读取 turtle_v2 的 Parquet 缓存"""
    # turtle_v2 格式：{code}.parquet，code 带交易所后缀
    path = DATA_DIR / f"{symbol}.parquet"
    # 尝试带后缀
    if not path.exists():
        for suffix in [".SH", ".SZ"]:
            p = DATA_DIR / f"{symbol}{suffix}.parquet"
            if p.exists():
                path = p
                break
        if not path.exists():
            raise FileNotFoundError(f"Parquet 缓存不存在: 已尝试 {symbol}.*.parquet")

    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    log.info("Tushare: %s → %d 条 (%s ~ %s)",
             path.name, len(df),
             df["date"].min().date(), df["date"].max().date())
    return df


# ════════════════════════════════════════════════════════════
#  S3  对比引擎（收益率比较法 — 消除复权因子）
# ════════════════════════════════════════════════════════════

@dataclass
class DayDiff:
    """单日差异"""
    date: date
    symbol: str
    close_tf: float = 0.0
    close_ts: float = 0.0
    ret_tf: float = 0.0
    ret_ts: float = 0.0
    ret_diff_abs: float = 0.0
    vol_tf: float = 0.0
    vol_ts: float = 0.0
    vol_diff_pct: float = 0.0
    level: str = "ok"
    reason: str = ""


def compare_return_based(tf_df: pd.DataFrame, ts_df: pd.DataFrame,
                         symbol: str) -> List[DayDiff]:
    """
    收益率比较模式。
    数学原理：
      TS_close = TF_close × k (复权因子)
      TS_ret = (TS_close₂/TS_close₁)-1 = (TF_close₂/TF_close₁)-1 = TF_ret
      → k 约掉，收益率为标量可比
    """
    merged = pd.merge(
        tf_df[["date", "close", "volume"]].rename(
            columns={"close": "tf_close", "volume": "tf_vol"}),
        ts_df[["date", "close", "volume"]].rename(
            columns={"close": "ts_close", "volume": "ts_vol"}),
        on="date", how="inner"
    )
    merged = merged.sort_values("date")

    merged["tf_ret"] = merged["tf_close"] / merged["tf_close"].shift(1) - 1
    merged["ts_ret"] = merged["ts_close"] / merged["ts_close"].shift(1) - 1
    merged["ret_diff"] = abs(merged["tf_ret"] - merged["ts_ret"])
    merged["vol_diff_pct"] = (
        abs(merged["tf_vol"] - merged["ts_vol"]) / merged["ts_vol"].clip(lower=1)
    )

    diffs = []
    for _, row in merged.iterrows():
        if pd.isna(row["ret_diff"]):
            continue

        ret_diff = row["ret_diff"]
        vol_diff_pct = row["vol_diff_pct"]
        reasons = []
        level = ValidationLevel.OK

        if ret_diff > cfg.return_diff_error:
            level = ValidationLevel.ERROR
            reasons.append(f"收益率差异 {ret_diff:.4%} > {cfg.return_diff_error:.1%}")
        elif ret_diff > cfg.return_diff_warn:
            level = ValidationLevel.WARNING
            reasons.append(f"收益率差异 {ret_diff:.4%} > {cfg.return_diff_warn:.1%}")

        if vol_diff_pct > cfg.volume_diff_error:
            if level.value < ValidationLevel.ERROR.value:
                level = ValidationLevel.ERROR
            reasons.append(f"成交量差异 {vol_diff_pct:.1%} > {cfg.volume_diff_error:.0%}")
        elif vol_diff_pct > cfg.volume_diff_warn:
            if level.value < ValidationLevel.WARNING.value:
                level = ValidationLevel.WARNING
            reasons.append(f"成交量差异 {vol_diff_pct:.1%} > {cfg.volume_diff_warn:.0%}")

        if ret_diff > cfg.return_diff_error * cfg.critical_multiplier:
            level = ValidationLevel.CRITICAL
            reasons.append(
                f"CRITICAL: 收益率差异 {ret_diff:.4%} > "
                f"{cfg.return_diff_error * cfg.critical_multiplier:.1%}"
            )

        diffs.append(DayDiff(
            date=row["date"].date(),
            symbol=symbol,
            close_tf=row["tf_close"],
            close_ts=row["ts_close"],
            ret_tf=row["tf_ret"],
            ret_ts=row["ts_ret"],
            ret_diff_abs=ret_diff,
            vol_tf=row["tf_vol"],
            vol_ts=row["ts_vol"],
            vol_diff_pct=vol_diff_pct,
            level=level.value,
            reason="; ".join(reasons) if reasons else "",
        ))

    return diffs


# ════════════════════════════════════════════════════════════
#  S4  报告与阻断
# ════════════════════════════════════════════════════════════

@dataclass
class ValidationReport:
    symbol: str
    total_days: int
    ok_days: int
    warn_days: int
    error_days: int
    critical_days: int
    consecutive_count: int = 0
    worst_level: str = "ok"
    worst_date: Optional[date] = None
    worst_detail: str = ""
    details: List[DayDiff] = field(default_factory=list)
    block_new_entries: bool = False
    block_all_trading: bool = False


def analyze_and_report(symbol: str, diffs: List[DayDiff]) -> ValidationReport:
    """分析逐日差异并生成报告"""
    if not diffs:
        return ValidationReport(symbol=symbol, total_days=0, ok_days=0,
                                warn_days=0, error_days=0, critical_days=0)

    total = len(diffs)
    levels = [d.level for d in diffs]
    ok_ct = levels.count("ok")
    warn_ct = levels.count("warning")
    error_ct = levels.count("error")
    critical_ct = levels.count("critical")

    # 连续性分析
    diffs_sorted = sorted(diffs, key=lambda d: d.date)
    max_consecutive = 0
    current_run = 0
    for d in diffs_sorted:
        if d.level in ("warning", "error", "critical"):
            current_run += 1
            max_consecutive = max(max_consecutive, current_run)
        else:
            current_run = 0

    # 最严重日期
    worst = None
    for d in sorted(diffs, key=lambda x: (
        0 if x.level == "critical" else 1 if x.level == "error"
        else 2 if x.level == "warning" else 3
    )):
        if d.level != "ok":
            worst = d
            break

    # 阻断判定
    block_new = max_consecutive >= cfg.consecutive_warn_days
    block_all = critical_ct > 0 or max_consecutive >= cfg.consecutive_error_days

    return ValidationReport(
        symbol=symbol,
        total_days=total,
        ok_days=ok_ct,
        warn_days=warn_ct,
        error_days=error_ct,
        critical_days=critical_ct,
        consecutive_count=max_consecutive,
        worst_level=worst.level if worst else "ok",
        worst_date=worst.date if worst else None,
        worst_detail=worst.reason if worst else "",
        details=diffs,
        block_new_entries=block_new,
        block_all_trading=block_all,
    )


# ════════════════════════════════════════════════════════════
#  S5  主流程
# ════════════════════════════════════════════════════════════

def validate_symbol(symbol: str) -> Optional[ValidationReport]:
    """单个品种交叉校验"""
    log.info("=" * 60)
    log.info("校验 %s (收益率比较法)", symbol)

    # 1. TickFlow
    try:
        tf_df = fetch_tickflow_daily(symbol, count=cfg.tickflow_max_count)
    except Exception as e:
        log.error("TickFlow 拉取失败 (%s): %s", symbol, str(e)[:200])
        return ValidationReport(
            symbol=symbol, total_days=0, ok_days=0, warn_days=0,
            error_days=0, critical_days=1,
            worst_level="critical", worst_detail=f"TickFlow 拉取失败: {str(e)[:100]}",
            block_all_trading=False,
        )

    # 2. Tushare
    try:
        ts_df = load_tushare_parquet(symbol)
    except FileNotFoundError as e:
        log.error("Tushare 缓存缺失 (%s): %s", symbol, str(e))
        return ValidationReport(
            symbol=symbol, total_days=0, ok_days=0, warn_days=0,
            error_days=0, critical_days=1,
            worst_level="critical", worst_detail=str(e),
            block_all_trading=True,
        )

    # 3. 比较
    diffs = compare_return_based(tf_df, ts_df, symbol)

    # 4. 报告
    report = analyze_and_report(symbol, diffs)
    log.info("结果 %s: %d天 | OK=%d WARN=%d ERR=%d CRIT=%d | 阻断新仓=%s 阻断全部=%s",
             symbol, report.total_days,
             report.ok_days, report.warn_days,
             report.error_days, report.critical_days,
             "⚠️" if report.block_new_entries else "✅",
             "🔴" if report.block_all_trading else "✅",
    )
    if report.worst_level != "ok":
        log.warning("  最严重: %s %s — %s",
                     report.worst_date, report.worst_level, report.worst_detail)

    return report


def validate_all() -> Dict[str, ValidationReport]:
    """全品种校验"""
    reports = {}
    for sym in ALL_SYMBOLS:
        reports[sym] = validate_symbol(sym)
    return reports


def print_summary(reports: Dict[str, ValidationReport]):
    """打印汇总"""
    print()
    print("=" * 80)
    print("  TickFlow <-> Tushare 交叉校验报告 (turtle_v2)")
    print("=" * 80)
    print(f"  {'品种':<8} {'天数':>5} {'OK':>5} {'WARN':>5} {'ERR':>5} {'CRIT':>5} "
          f"{'连续':>5} {'阻断新仓':<8} {'阻断全部'}")
    print("  " + "-" * 76)

    for sym, r in reports.items():
        block_new = "⚠️ 是" if r.block_new_entries else "✅ 否"
        block_all = "🔴 是" if r.block_all_trading else "✅ 否"
        print(f"  {sym:<8} {r.total_days:>5} {r.ok_days:>5} {r.warn_days:>5} "
              f"{r.error_days:>5} {r.critical_days:>5} {r.consecutive_count:>5} "
              f"{block_new:<8} {block_all}")

    print("=" * 80)

    any_block_all = any(r.block_all_trading for r in reports.values() if r)
    any_block_new = any(r.block_new_entries for r in reports.values() if r)

    if any_block_all:
        print("🔴 全局判定：暂停所有交易，人工检查数据源！")
    elif any_block_new:
        print("⚠️  全局判定：暂停新开仓，仅管理已有仓位。")
    else:
        print("✅ 全局判定：交叉校验通过，数据可信。")
    print()


def export_report(reports: Dict[str, ValidationReport]):
    """导出到 JSON"""
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULT_DIR / "cross_validate_report.json"

    export_data = {}
    for sym, r in reports.items():
        if r is None:
            continue
        export_data[sym] = {
            "symbol": r.symbol,
            "total_days": r.total_days,
            "ok_days": r.ok_days,
            "warn_days": r.warn_days,
            "error_days": r.error_days,
            "critical_days": r.critical_days,
            "consecutive_errors": r.consecutive_count,
            "worst_level": r.worst_level,
            "worst_date": str(r.worst_date) if r.worst_date else None,
            "worst_detail": r.worst_detail,
            "block_new_entries": r.block_new_entries,
            "block_all_trading": r.block_all_trading,
            "details": [
                {
                    "date": str(d.date), "level": d.level,
                    "reason": d.reason,
                    "ret_tf": round(d.ret_tf, 6),
                    "ret_ts": round(d.ret_ts, 6),
                    "ret_diff_abs": round(d.ret_diff_abs, 6),
                    "vol_diff_pct": round(d.vol_diff_pct, 4),
                }
                for d in r.details
                if d.level != "ok"
            ],
        }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(export_data, f, ensure_ascii=False, indent=2)
    log.info("详细报告已导出: %s", path)


# ════════════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="TickFlow <-> Tushare 数据交叉校验 (turtle_v2)"
    )
    parser.add_argument("-s", "--symbol", type=str, default=None,
                        help="单品种校验 (如 510500)")
    parser.add_argument("--export", action="store_true",
                        help="导出详细差异报告")
    parser.add_argument("--days", type=int, default=cfg.lookback_days,
                        help=f"校验最近 N 个交易日 (默认 {cfg.lookback_days})")

    args = parser.parse_args()
    cfg.lookback_days = args.days

    if args.symbol:
        reports = {args.symbol: validate_symbol(args.symbol)}
    else:
        reports = validate_all()

    print_summary(reports)

    if args.export:
        export_report(reports)


if __name__ == "__main__":
    main()
