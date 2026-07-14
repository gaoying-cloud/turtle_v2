#!/usr/bin/env python
"""
S43 海龟干净重验证 — IS-only 两阶段参数扫描

对标 N 字 Clean 重验证方法：
  Stage 1: 4 核心机械参数全网格 (81 组合) — 捕获交互效应
  Stage 2: 4 辅助参数独立扫描 (8 组合)  — 独立维度逐个确认
  汇总最优 → OOS 单次纯净验证

用法:
  py scripts/scan_s43_turtle.py                     # 完整两阶段扫描 + OOS 验证
  py scripts/scan_s43_turtle.py --stage 1           # 仅 Stage 1
  py scripts/scan_s43_turtle.py --stage 2           # 仅 Stage 2（需 Stage 1 结果）
  py scripts/scan_s43_turtle.py --workers 4         # 4 核并行
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 复用网格搜索的单次回测和并行执行器
from scripts.run_grid_search import (
    run_single_backtest,
    _run_tasks,
    _get_config,
    OUTPUT_DIR as GRID_OUTPUT_DIR,
)

logger = logging.getLogger(__name__)

# ── S43 专属输出目录 ──
S43_OUTPUT = ROOT / "results" / "s43_turtle_clean"
S43_OUTPUT.mkdir(parents=True, exist_ok=True)

# ════════════════════════════════════════════════════════════
#  Stage 1: 核心机械参数网格
# ════════════════════════════════════════════════════════════

# 4 个有交互效应的机械参数，每个 3 档
STAGE1_GRID = {
    "atr_period": [15, 20, 25],
    "breakout_period": [15, 20, 25],
    "stop_period": [8, 10, 12],
    "stop_atr_multiple": [1.5, 2.0, 2.5],
}

# Stage 1 期间固定的保守默认值（其余参数）
STAGE1_FIXED = {
    "alpha": 0.05,                 # 回退保守值：微风险平价偏移
    "max_cumulative_loss_pct": 0.15,  # 固定（已废弃，保留兼容）
    "max_consecutive_losses": 5,   # 回退保守值：更严格的风控
    "pyramid_step": 0.5,           # 回退保守值：经典海龟加仓步长
    "atr_pct_threshold": "off",    # 回退保守值：关闭 ATR 百分位过滤
}


def build_stage1_tasks(start_date: str, end_date: str) -> List[Tuple]:
    """构建 Stage 1 的 81 组回测任务列表。

    笛卡尔积: 3×3×3×3 = 81 组合，仅扫描 Mode A（55过滤已关闭）。

    Returns
    -------
    list of (params, mode, start, end, run_id)
    """
    import itertools

    keys = list(STAGE1_GRID.keys())
    values = list(STAGE1_GRID.values())

    tasks = []
    run_id = 0
    for combo in itertools.product(*values):
        params = dict(zip(keys, combo))
        params.update(STAGE1_FIXED)  # 合并固定保守值
        # 仅 Mode A（use_55_filter 在配置中已关闭）
        tasks.append((params, "A", start_date, end_date, run_id))
        run_id += 1

    return tasks


# ════════════════════════════════════════════════════════════
#  Stage 2: 辅助参数独立扫描
# ════════════════════════════════════════════════════════════

# 每个辅助参数的候选值（不含 Stage 1 中已测试的 baseline）
STAGE2_PARAMS = {
    "pyramid_step": [1.0, 2.0],     # baseline: 0.5 (STAGE1_FIXED)
    "alpha": [0.0, 0.10],           # baseline: 0.05 (STAGE1_FIXED)
    "atr_pct_threshold": [0.5, 0.75],  # baseline: "off" (STAGE1_FIXED)
    "max_consecutive_losses": [8, 10],  # baseline: 5 (STAGE1_FIXED)
}


def build_stage2_tasks(
    best_stage1_params: dict,
    start_date: str,
    end_date: str,
) -> List[Tuple]:
    """构建 Stage 2 的 8 组独立扫描任务。

    固定 Stage 1 最优的 4 个机械参数 + 其余保守值，
    逐个扫描 4 个辅助参数的 2 个额外候选值。

    Parameters
    ----------
    best_stage1_params : dict
        Stage 1 产出的最优参数（含 atr_period/breakout_period/stop_period/stop_atr_multiple）。

    Returns
    -------
    list of (params, mode, start, end, run_id)
    """
    tasks = []
    run_id = 1000  # 与 Stage 1 区分

    for param_name, candidate_values in STAGE2_PARAMS.items():
        for val in candidate_values:
            # 以 Stage 1 最优参数为基底，仅替换当前扫描的参数
            params = dict(best_stage1_params)
            # 保留 STAGE1_FIXED 中未被 best_stage1_params 覆盖的固定值
            for k, v in STAGE1_FIXED.items():
                if k not in params:
                    params[k] = v
            params[param_name] = val
            tasks.append((params, "A", start_date, end_date, run_id))
            run_id += 1

    return tasks


# ════════════════════════════════════════════════════════════
#  结果评估（简化版：以 Sharpe 为主要排序指标）
# ════════════════════════════════════════════════════════════

def select_best(df: pd.DataFrame) -> dict:
    """从扫描结果 DataFrame 中选出最优参数组合。

    排序优先级：Sharpe > CAGR > MDD（趋势跟踪策略 Sharpe 最反映风险调整收益）。

    Returns
    -------
    dict
        最优行的参数字典（不含 run_id/date_range 等元信息）。
    """
    if df.empty:
        return {}

    df_valid = df.dropna(subset=["sharpe"]).copy()
    if df_valid.empty:
        # 全部 Sharpe 为 NaN，回退到 CAGR
        df_valid = df.copy()
        df_valid["_score"] = df_valid["cagr"].astype(float)
    else:
        # 综合评分：Sharpe 为主（0.5），CAGR（0.3），MDD 惩罚（0.2）
        from scripts.run_grid_search import _robust_scaler
        df_valid["_sharpe_s"] = _robust_scaler(df_valid["sharpe"].astype(float))
        df_valid["_cagr_s"] = _robust_scaler(df_valid["cagr"].astype(float))
        df_valid["_dd_s"] = _robust_scaler(-df_valid["max_drawdown"].astype(float))
        df_valid["_score"] = (
            0.5 * df_valid["_sharpe_s"] +
            0.3 * df_valid["_cagr_s"] +
            0.2 * df_valid["_dd_s"]
        )

    best_idx = df_valid["_score"].idxmax()
    best_row = df_valid.loc[best_idx]

    # 提取参数字典（仅保留扫描参数 + 固定参数）
    param_keys = list(STAGE1_GRID.keys()) + list(STAGE2_PARAMS.keys()) + list(STAGE1_FIXED.keys())
    best_params = {}
    for k in param_keys:
        if k in best_row.index:
            val = best_row[k]
            # 保持原始类型：整数参数还原为 int
            if k in ("atr_period", "breakout_period", "stop_period", "max_consecutive_losses"):
                best_params[k] = int(val)
            elif k in ("stop_atr_multiple", "alpha", "pyramid_step", "max_cumulative_loss_pct"):
                best_params[k] = float(val)
            elif k == "atr_pct_threshold":
                best_params[k] = val if val == "off" else float(val)
            else:
                best_params[k] = val

    return best_params


def print_param_table(df: pd.DataFrame, title: str, top_n: int = 10):
    """打印参数扫描结果表格。"""
    if df.empty:
        print(f"\n  {title}: (无结果)")
        return

    print(f"\n  {title} (Top-{min(top_n, len(df))})")
    print(f"  {'Rank':<5} {'atr':<5} {'brk':<5} {'stop':<5} {'mult':<6} {'pyr':<5} {'α':<6} {'ATR%':<6} {'loss':<5} {'Sharpe':<8} {'CAGR':<8} {'MDD':<8} {'交易':<5}")
    print(f"  {'-' * 80}")

    df_disp = df.dropna(subset=["sharpe"]).nlargest(top_n, "sharpe") if "sharpe" in df.columns else df.head(top_n)
    for rank, (_, row) in enumerate(df_disp.iterrows(), 1):
        print(f"  {rank:<5} "
              f"{int(row.get('atr_period',0)):<5} "
              f"{int(row.get('breakout_period',0)):<5} "
              f"{int(row.get('stop_period',0)):<5} "
              f"{float(row.get('stop_atr_multiple',0)):<6.1f} "
              f"{float(row.get('pyramid_step', STAGE1_FIXED['pyramid_step'])):<5.1f} "
              f"{float(row.get('alpha', STAGE1_FIXED['alpha'])):<6.2f} "
              f"{str(row.get('atr_pct_threshold', STAGE1_FIXED['atr_pct_threshold'])):<6} "
              f"{int(row.get('max_consecutive_losses', STAGE1_FIXED['max_consecutive_losses'])):<5} "
              f"{float(row.get('sharpe',0)):<8.4f} "
              f"{float(row.get('cagr',0)):<8.2f}% "
              f"{float(row.get('max_drawdown',0)):<8.2f}% "
              f"{int(row.get('total_trades',0)):<5}")


# ════════════════════════════════════════════════════════════
#  OOS 单次验证
# ════════════════════════════════════════════════════════════

def run_oos_verification(
    best_params: dict,
    oos_start: str,
    oos_end: str,
) -> Optional[dict]:
    """用最优参数在 OOS 区间做单次纯净验证。

    不做参数调整，与 N 字 Clean 方法完全一致。
    """
    params = dict(best_params)
    # 确保所有必要参数都在 params 中
    for k, v in STAGE1_FIXED.items():
        if k not in params:
            params[k] = v

    logger.info("=" * 60)
    logger.info("OOS 纯净验证: %s ~ %s", oos_start, oos_end)
    logger.info("参数: atr=%s breakout=%s stop=%s mult=%s pyramid=%s α=%s atr_pct=%s loss=%s",
                params.get("atr_period"), params.get("breakout_period"),
                params.get("stop_period"), params.get("stop_atr_multiple"),
                params.get("pyramid_step"), params.get("alpha"),
                params.get("atr_pct_threshold"), params.get("max_consecutive_losses"))
    logger.info("=" * 60)

    result = run_single_backtest(params, "A", oos_start, oos_end, run_id=99999)

    if result:
        logger.info("OOS 结果: Sharpe=%.4f CAGR=%.2f%% MDD=%.2f%% 交易=%d 胜率=%.1f%%",
                    result.get("sharpe", 0), result.get("cagr", 0),
                    result.get("max_drawdown", 0), result.get("total_trades", 0),
                    result.get("win_rate", 0))

    return result


# ════════════════════════════════════════════════════════════
#  主流程
# ════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="S43 海龟干净重验证 — IS-only 两阶段参数扫描"
    )
    parser.add_argument("--stage", type=int, choices=[1, 2], default=0,
                        help="仅运行指定阶段 (0=全部)")
    parser.add_argument("--workers", "-w", type=int, default=4,
                        help="并行进程数 (默认: 4)")
    parser.add_argument("--is-start", type=str, default="2014-01-01",
                        help="IS 起始日期 (默认: 2014-01-01)")
    parser.add_argument("--is-end", type=str, default="2020-01-01",
                        help="IS 截止日期 (默认: 2020-01-01)")
    parser.add_argument("--oos-start", type=str, default="2020-01-01",
                        help="OOS 起始日期 (默认: 2020-01-01)")
    parser.add_argument("--oos-end", type=str, default="2026-07-10",
                        help="OOS 截止日期 (默认: 2026-07-10)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="详细日志")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    is_start, is_end = args.is_start, args.is_end
    oos_start, oos_end = args.oos_start, args.oos_end

    print("\n" + "=" * 70)
    print("  S43 海龟干净重验证 — IS-only 两阶段参数扫描")
    print(f"  IS 区间: {is_start} ~ {is_end}  |  OOS 区间: {oos_start} ~ {oos_end}")
    print(f"  输出目录: {S43_OUTPUT}")
    print("=" * 70)

    best_stage1_path = S43_OUTPUT / "best_stage1.json"

    # ════════════════════════════════════════════════════════
    #  Stage 1: 核心机械参数网格 (81 组合)
    # ════════════════════════════════════════════════════════
    if args.stage in (0, 1):
        print(f"\n{'─' * 70}")
        print("  Stage 1: 核心机械参数网格扫描")
        print(f"  参数: atr_period × breakout_period × stop_period × stop_atr_multiple")
        print(f"  固定: pyramid_step={STAGE1_FIXED['pyramid_step']}, "
              f"α={STAGE1_FIXED['alpha']}, "
              f"ATR%过滤={STAGE1_FIXED['atr_pct_threshold']}, "
              f"连亏熔断={STAGE1_FIXED['max_consecutive_losses']}")
        print(f"  规模: 3×3×3×3 = 81 组合")
        print(f"{'─' * 70}")

        tasks = build_stage1_tasks(is_start, is_end)
        logger.info("Stage 1: %d 个回测任务，%d 核并行", len(tasks), args.workers)

        raw_results = _run_tasks(tasks, args.workers, verbose=args.verbose)
        df_s1 = pd.DataFrame(raw_results)

        if df_s1.empty:
            logger.error("Stage 1 未产生任何有效结果，终止")
            sys.exit(1)

        # 保存 Stage 1 完整结果
        s1_path = S43_OUTPUT / "stage1_full.csv"
        df_s1.to_csv(s1_path, index=False, encoding="utf-8")
        logger.info("Stage 1 完整结果: %s (%d 行)", s1_path, len(df_s1))

        # 选出最优参数
        best_s1 = select_best(df_s1)
        if not best_s1:
            logger.error("无法选出 Stage 1 最优参数，终止")
            sys.exit(1)

        # 保存最优参数供 Stage 2 使用
        with open(best_stage1_path, "w", encoding="utf-8") as f:
            json.dump(best_s1, f, ensure_ascii=False, indent=2)

        print_param_table(df_s1, "Stage 1 扫描结果")
        print(f"\n  Stage 1 最优参数:")
        for k, v in best_s1.items():
            if k in STAGE1_GRID:
                print(f"    {k}: {v}")
        best_row = df_s1.loc[
            (df_s1["atr_period"] == best_s1["atr_period"]) &
            (df_s1["breakout_period"] == best_s1["breakout_period"]) &
            (df_s1["stop_period"] == best_s1["stop_period"]) &
            (df_s1["stop_atr_multiple"] == best_s1["stop_atr_multiple"])
        ].iloc[0]
        print(f"    → Sharpe={best_row['sharpe']:.4f}  CAGR={best_row['cagr']:.2f}%  "
              f"MDD={best_row['max_drawdown']:.2f}%  交易={int(best_row['total_trades'])}")

    # ════════════════════════════════════════════════════════
    #  Stage 2: 辅助参数独立扫描 (8 组合)
    # ════════════════════════════════════════════════════════
    if args.stage in (0, 2):
        # 加载 Stage 1 最优参数
        if not best_stage1_path.exists():
            logger.error("Stage 1 最优参数文件不存在: %s，请先运行 --stage 1", best_stage1_path)
            sys.exit(1)

        with open(best_stage1_path, "r", encoding="utf-8") as f:
            best_s1 = json.load(f)

        print(f"\n{'─' * 70}")
        print("  Stage 2: 辅助参数独立扫描")
        print(f"  固定 Stage 1 最优: atr={best_s1['atr_period']} "
              f"breakout={best_s1['breakout_period']} "
              f"stop={best_s1['stop_period']} "
              f"mult={best_s1['stop_atr_multiple']}")
        print(f"  扫描参数: pyramid_step[{STAGE2_PARAMS['pyramid_step']}]  "
              f"alpha[{STAGE2_PARAMS['alpha']}]  "
              f"atr_pct_threshold[{STAGE2_PARAMS['atr_pct_threshold']}]  "
              f"max_consecutive_losses[{STAGE2_PARAMS['max_consecutive_losses']}]")
        print(f"  规模: 4 × 2 = 8 组合")
        print(f"{'─' * 70}")

        tasks = build_stage2_tasks(best_s1, is_start, is_end)
        logger.info("Stage 2: %d 个回测任务，%d 核并行", len(tasks), args.workers)

        raw_results = _run_tasks(tasks, args.workers, verbose=args.verbose)
        df_s2 = pd.DataFrame(raw_results)

        if df_s2.empty:
            logger.error("Stage 2 未产生任何有效结果")
            sys.exit(1)

        # 合并 baseline（Stage 1 最优）到 Stage 2 结果中
        baseline_params = dict(best_s1)
        for k, v in STAGE1_FIXED.items():
            if k not in baseline_params:
                baseline_params[k] = v
        baseline_result = run_single_backtest(baseline_params, "A", is_start, is_end, run_id=999)
        if baseline_result:
            df_all = pd.concat([df_s2, pd.DataFrame([baseline_result])], ignore_index=True)
        else:
            df_all = df_s2

        s2_path = S43_OUTPUT / "stage2_full.csv"
        df_all.to_csv(s2_path, index=False, encoding="utf-8")
        logger.info("Stage 2 完整结果: %s (%d 行)", s2_path, len(df_all))

        # 选出各辅助参数的最优值（独立选择，无交互风险）
        best_params = dict(best_s1)
        param_baselines = {
            "pyramid_step": STAGE1_FIXED["pyramid_step"],
            "alpha": STAGE1_FIXED["alpha"],
            "atr_pct_threshold": STAGE1_FIXED["atr_pct_threshold"],
            "max_consecutive_losses": STAGE1_FIXED["max_consecutive_losses"],
        }

        print(f"\n  Stage 2 各参数最优选择:")
        for param_name in STAGE2_PARAMS:
            candidates = [param_baselines[param_name]] + list(STAGE2_PARAMS[param_name])
            best_val = None
            best_sharpe = -999

            for val in candidates:
                if param_name == "atr_pct_threshold":
                    match = df_all[df_all[param_name].astype(str) == str(val)]
                elif param_name in ("alpha", "pyramid_step", "max_cumulative_loss_pct"):
                    match = df_all[df_all[param_name].astype(float).round(6) == round(float(val), 6)]
                else:
                    match = df_all[df_all[param_name].astype(int) == int(val)]

                if not match.empty:
                    sh = match["sharpe"].mean()
                    if sh > best_sharpe:
                        best_sharpe = sh
                        best_val = val

            if best_val is not None:
                best_params[param_name] = best_val
                print(f"    {param_name:<25}: {best_val} (Sharpe={best_sharpe:.4f})")
            else:
                best_params[param_name] = param_baselines[param_name]
                print(f"    {param_name:<25}: {param_baselines[param_name]} (无有效结果，保持 baseline)")

        # 保存最终最优参数
        final_params_path = S43_OUTPUT / "best_params_s43.json"
        # 需要将 numpy 类型转为 Python 原生类型
        clean_params = {}
        for k, v in best_params.items():
            if isinstance(v, (np.integer,)):
                clean_params[k] = int(v)
            elif isinstance(v, (np.floating,)):
                clean_params[k] = float(v)
            elif isinstance(v, np.ndarray):
                clean_params[k] = v.tolist()
            else:
                clean_params[k] = v

        with open(final_params_path, "w", encoding="utf-8") as f:
            json.dump(clean_params, f, ensure_ascii=False, indent=2)
        logger.info("最终最优参数: %s", final_params_path)

        print_param_table(df_all, "Stage 2 所有候选结果")

    # ════════════════════════════════════════════════════════
    #  OOS 单次纯净验证
    # ════════════════════════════════════════════════════════
    final_params_path = S43_OUTPUT / "best_params_s43.json"
    if final_params_path.exists():
        with open(final_params_path, "r", encoding="utf-8") as f:
            final_params = json.load(f)

        print(f"\n{'═' * 70}")
        print("  OOS 单次纯净验证")
        print(f"{'═' * 70}")

        oos_result = run_oos_verification(final_params, oos_start, oos_end)

        if oos_result:
            oos_path = S43_OUTPUT / "oos_verification.json"
            with open(oos_path, "w", encoding="utf-8") as f:
                json.dump(oos_result, f, ensure_ascii=False, indent=2, default=str)

            print(f"\n  ┌─────────────────────────────────────────────┐")
            print(f"  │ OOS 验证结果                                  │")
            print(f"  ├─────────────────────────────────────────────┤")
            print(f"  │ Sharpe:  {oos_result.get('sharpe', 0):>8.4f}                            │")
            print(f"  │ CAGR:    {oos_result.get('cagr', 0):>8.2f}%                           │")
            print(f"  │ MDD:     {oos_result.get('max_drawdown', 0):>8.2f}%                           │")
            print(f"  │ 交易:    {oos_result.get('total_trades', 0):>8}                              │")
            print(f"  │ 胜率:    {oos_result.get('win_rate', 0):>8.1f}%                           │")
            print(f"  └─────────────────────────────────────────────┘")

    print(f"\n{'=' * 70}")
    print(f"  S43 扫描完成")
    print(f"  结果目录: {S43_OUTPUT}")
    print(f"  Stage 1 全量:   {S43_OUTPUT / 'stage1_full.csv'}")
    print(f"  Stage 2 全量:   {S43_OUTPUT / 'stage2_full.csv'}")
    print(f"  最优参数:       {S43_OUTPUT / 'best_params_s43.json'}")
    print(f"  OOS 验证:       {S43_OUTPUT / 'oos_verification.json'}")
    print(f"{'=' * 70}\n")


if __name__ == "__main__":
    main()
