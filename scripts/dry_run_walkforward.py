#!/usr/bin/env python
"""
Walk-Forward Dry-Run / 历史回放式 Paper Trading 模拟器

逐日回放历史数据，使用与 daily_signal.py 完全相同的信号/执行逻辑，
在几分钟内跑完数年数据，产出完整交易日志和绩效摘要。

目的：海龟策略信号频率极低（每品种每年 3-5 次入场），真实 dry-run
需要数月才能积累足够反馈。本脚本通过历史回放快速验证完整链路：
信号生成 → 入场 → 加仓 → 止损/利润保护 → 结算。

用法：
    py scripts/dry_run_walkforward.py                        # 全历史，标准参数
    py scripts/dry_run_walkforward.py --fast                 # 加速模式（更多信号）
    py scripts/dry_run_walkforward.py --start 2024-01-01     # 指定起始日期
    py scripts/dry_run_walkforward.py --end 2025-12-31       # 指定截止日期
    py scripts/dry_run_walkforward.py --verbose              # 逐日打印信号表
    py scripts/dry_run_walkforward.py --fast --verbose       # 加速+详细输出

加速模式参数调整（产生约 3-5x 信号量）：
    breakout_period:  20 → 10    （更敏感的入场）
    stop_period:       8 →  5    （更紧的止损，释放资金再入场）
    pyramid_step:     2.0 → 1.0  （更容易触发加仓）
    atr_pct_filter:   true → false（不跳过波动期）
"""

from __future__ import annotations

import sys, json, logging, warnings, copy
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

# ── 修复 worktree 数据路径：worktree 不含 gitignored 的 data/，需指回主仓库 ──
def _resolve_data_dir() -> Path:
    """如果当前在 git worktree 中，返回主仓库的 data/etf_daily 路径。"""
    git_file = ROOT / ".git"
    if git_file.is_file():
        # worktree: .git 是文本文件，内容为 "gitdir: <path>"
        content = git_file.read_text(encoding="utf-8").strip()
        if content.startswith("gitdir:"):
            gitdir = Path(content[7:].strip())
            main_repo = gitdir.parent.parent.parent  # .git/worktrees/<name> → .git → repo root
            data_dir = main_repo / "data" / "etf_daily"
            if data_dir.exists():
                return data_dir
    # 默认路径（非 worktree 模式）
    return ROOT / "data" / "etf_daily"

_MAIN_DATA_DIR = _resolve_data_dir()

# ── 复用 daily_signal 的核心函数 ──
import scripts.daily_signal as _ds
# 覆盖数据目录指向主仓库（worktree 中 data/ 不存在）
_ds.DATA_DIR = _MAIN_DATA_DIR

from scripts.daily_signal import (
    compute_signals, should_enter, check_exit, should_add,
    calc_shares, _check_risk_limits, calc_fade,
    record_trade_result, _get_sf,
    get_data_for, ETFS, CONFIG, TURTLE, MAX_UNITS,
    RISK_PER_UNIT, STOP_MULT, PYRAMID_STEP,
    INITIAL_CASH, COMMISSION, SLIPPAGE, MIN_UNIT,
    SINGLE_MAX_RISK, MAX_PORTFOLIO_RISK, WEIGHT_MULTIPLIERS,
)


# ════════════════════════════════════════════════════════════
#  加速模式：覆盖关键参数以产生更多信号
# ════════════════════════════════════════════════════════════

FAST_OVERRIDES = {
    "breakout_period": 10,       # 20→10, ~2x 入场信号
    "stop_period": 5,            # 8→5, 更紧止损→更快释放资金→更多再入场
    "exit_period": 5,            # 对齐 stop_period
    "pyramid_step": 1.0,         # 2.0→1.0, 更容易触发加仓
    "atr_pct_filter": False,     # 不跳过波动期
    "atr_pct_threshold": 0.90,   # 放宽 ATR 百分位阈值（仅当 filter=True 时生效）
}

# 存储原始值以便恢复
_original_turtle = None


def apply_fast_mode():
    """临时覆盖 TURTLE 配置 + 模块常量以产生更多信号。"""
    global _original_turtle
    import scripts.daily_signal as ds

    if _original_turtle is None:
        _original_turtle = copy.deepcopy(ds.TURTLE)

    for k, v in FAST_OVERRIDES.items():
        ds.TURTLE[k] = v

    # 同步更新本模块的引用
    ds.PYRAMID_STEP = FAST_OVERRIDES["pyramid_step"]


def restore_normal_mode():
    """恢复原始配置。"""
    global _original_turtle
    import scripts.daily_signal as ds

    if _original_turtle is not None:
        ds.TURTLE.clear()
        ds.TURTLE.update(_original_turtle)
        ds.PYRAMID_STEP = _original_turtle.get("pyramid_step", 2.0)
        _original_turtle = None


# ════════════════════════════════════════════════════════════
#  数据准备
# ════════════════════════════════════════════════════════════

def load_all_data(start_date: Optional[date] = None,
                  end_date: Optional[date] = None) -> dict[str, pd.DataFrame]:
    """加载所有品种的历史数据，对齐到公共日期索引。

    返回 {symbol: DataFrame}，所有 DataFrame 拥有相同的日期索引。
    """
    raw_dfs = {}
    for sym in ETFS:
        df = get_data_for(sym)
        if df is None or len(df) < 100:
            print(f"  [WARN] {sym}: 数据不足，跳过")
            continue
        if start_date:
            df = df[df["date"] >= pd.Timestamp(start_date)].reset_index(drop=True)
        if end_date:
            df = df[df["date"] <= pd.Timestamp(end_date)].reset_index(drop=True)
        if len(df) < 100:
            print(f"  [WARN] {sym}: 日期范围过滤后数据不足，跳过")
            continue
        raw_dfs[sym] = df

    if not raw_dfs:
        return {}

    # 对齐到公共日期
    all_dates = sorted(set.union(*(set(df["date"].dropna()) for df in raw_dfs.values())))
    all_dates = pd.DatetimeIndex(all_dates)

    aligned = {}
    for sym, df in raw_dfs.items():
        df = df.set_index("date").reindex(all_dates)
        df = df.ffill().bfill()
        df = df.reset_index().rename(columns={"index": "date"})
        aligned[sym] = df

    return aligned


# ════════════════════════════════════════════════════════════
#  执行模拟
# ════════════════════════════════════════════════════════════

def simulate_execution(state: dict, data_cache: dict,
                       trading_date: pd.Timestamp,
                       next_date: pd.Timestamp | None,
                       entry_actions: list[dict],
                       exit_signals: list[dict],
                       add_actions: list[dict]) -> dict:
    """模拟在 next_date 开盘价执行所有待处理操作。

    返回执行摘要 {'entries': N, 'exits': N, 'adds': N, 'half_exits': N}
    """
    summary = {"entries": 0, "exits": 0, "adds": 0, "half_exits": 0}
    today_str = trading_date.strftime("%Y-%m-%d")

    # ── 先执行退出（释放现金）──
    for es in exit_signals:
        sym = es["code"]
        if sym not in data_cache or next_date is None:
            continue
        df = data_cache[sym]
        next_rows = df[df["date"] == next_date]
        if len(next_rows) == 0:
            continue
        exec_price = float(next_rows["open"].iloc[0])

        pos = next((p for p in state["positions"] if p["code"] == sym), None)
        if pos is None:
            continue

        if es["type"] == "full":
            exit_shares = pos["shares"]
            pnl = (exec_price - pos["entry_price"]) * exit_shares
            fee = exit_shares * exec_price * (COMMISSION * 2 + SLIPPAGE)
            net_pnl = pnl - fee
            was_win = net_pnl > 0

            record_trade_result(state, sym, was_win)
            cl = state["consecutive_losses"].get(sym, 0)
            if was_win:
                state["consecutive_losses"][sym] = 0
            else:
                state["consecutive_losses"][sym] = cl + 1

            state["cash"] += exit_shares * exec_price * (1 - COMMISSION - SLIPPAGE)
            state["trade_history"].append({
                "symbol": sym, "direction": "long",
                "entry_date": pos["entry_date"], "exit_date": today_str,
                "entry_price": pos["entry_price"], "exit_price": exec_price,
                "shares": exit_shares, "pnl": round(net_pnl, 2),
                "was_win": was_win,
                "holding_days": (
                    trading_date.date() - datetime.strptime(pos["entry_date"], "%Y-%m-%d").date()
                ).days,
            })
            state["positions"].remove(pos)
            summary["exits"] += 1

        elif es["type"] == "half":
            half_shares = pos["shares"] // 2
            if half_shares <= 0:
                continue
            pnl = (exec_price - pos["entry_price"]) * half_shares
            fee = half_shares * exec_price * (COMMISSION * 2 + SLIPPAGE)
            net_pnl = pnl - fee

            state["cash"] += half_shares * exec_price * (1 - COMMISSION - SLIPPAGE)
            pos["shares"] -= half_shares
            pos["half_closed"] = True

            state.setdefault("half_exit_events", []).append({
                "symbol": sym, "direction": "long",
                "entry_date": pos["entry_date"], "exit_date": today_str,
                "entry_price": pos["entry_price"], "exit_price": exec_price,
                "shares_exited": half_shares, "shares_remaining": pos["shares"],
                "pnl": round(net_pnl, 2), "was_win": net_pnl > 0,
                "holding_days": (
                    trading_date.date() - datetime.strptime(pos["entry_date"], "%Y-%m-%d").date()
                ).days,
            })
            summary["half_exits"] += 1

    # ── 再执行加仓 ──
    for aa in add_actions:
        sym = aa["symbol"]
        if sym not in data_cache or next_date is None:
            continue
        df = data_cache[sym]
        next_rows = df[df["date"] == next_date]
        if len(next_rows) == 0:
            continue
        exec_price = float(next_rows["open"].iloc[0])

        pos = next((p for p in state["positions"] if p["code"] == sym), None)
        if pos is None:
            continue

        add_shares = aa["shares"]
        pos["shares"] += add_shares
        pos["units"] += 1
        state["cash"] -= add_shares * exec_price
        state["buy_today"][sym] = True
        summary["adds"] += 1

    # ── 最后执行入场 ──
    for en in entry_actions:
        sym = en["code"]
        if sym not in data_cache or next_date is None:
            continue
        df = data_cache[sym]
        next_rows = df[df["date"] == next_date]
        if len(next_rows) == 0:
            continue
        exec_price = float(next_rows["open"].iloc[0])

        if state.get("buy_today", {}).get(sym, False):
            continue
        if state["cash"] < en["shares"] * exec_price:
            continue

        pos = {
            "code": sym,
            "direction": "long",
            "entry_date": today_str,
            "entry_price": exec_price,
            "shares": en["shares"],
            "shares_per_unit": en["shares"],
            "units": 1,
            "n_at_entry": float(en.get("n_at_entry", 0)),
            "trail_high": exec_price,
            "high_since_entry": exec_price,
            "half_closed": False,
            "protection_activated": False,
        }
        state["positions"].append(pos)
        state["cash"] -= en["shares"] * exec_price
        state["buy_today"][sym] = True
        _get_sf(state, sym)["rejections"] = 0
        summary["entries"] += 1

    return summary


# ════════════════════════════════════════════════════════════
#  辅助函数
# ════════════════════════════════════════════════════════════

def _dry_market_equity(state: dict, data_cache: dict,
                       as_of_date: pd.Timestamp) -> float:
    """按 as_of_date 收盘价计算账户总权益。"""
    pos_value = 0.0
    for p in state["positions"]:
        sym = p["code"]
        if sym in data_cache:
            df = data_cache[sym]
            rows = df[df["date"] == as_of_date]
            if len(rows) > 0:
                px = float(rows["close"].iloc[0])
            else:
                px = float(p["entry_price"])
        else:
            px = float(p["entry_price"])
        pos_value += p["shares"] * px
    return state["cash"] + pos_value


def _dry_calc_fade(state: dict) -> float:
    """集中度衰减系数。"""
    n_pos = len(state.get("positions", []))
    n_total = len(ETFS)
    if n_total >= 7:
        tbl = {0: 1.0, 1: 1.0, 2: 1.0, 3: 1.0, 4: 0.8, 5: 0.6, 6: 0.5}
    elif n_total >= 6:
        tbl = {0: 1.0, 1: 1.0, 2: 1.0, 3: 0.85, 4: 0.7, 5: 0.6, 6: 0.5}
    else:
        tbl = {0: 1.0, 1: 1.0, 2: 1.0, 3: 0.8, 4: 0.6}
    return tbl.get(n_pos, 0.5)


def _print_day_summary(date_str: str, entry_actions, exit_signals,
                       add_actions, exec_summary, state, data_cache):
    """详细模式：打印单日信号和执行结果。"""
    parts = []
    if exec_summary["entries"] > 0:
        syms = [e["code"] for e in entry_actions]
        parts.append(f"入场 {syms}")
    if exec_summary["exits"] > 0:
        syms = [e["code"] for e in exit_signals if e["type"] == "full"]
        parts.append(f"平仓 {syms}")
    if exec_summary["half_exits"] > 0:
        syms = [e["code"] for e in exit_signals if e["type"] == "half"]
        parts.append(f"减半 {syms}")
    if exec_summary["adds"] > 0:
        syms = [a["symbol"] for a in add_actions]
        parts.append(f"加仓 {syms}")
    eq = _dry_market_equity(state, data_cache, pd.Timestamp(date_str))
    n_pos = len(state["positions"])
    print(f"  {date_str}  {' | '.join(parts)}  "
          f"权益={eq:,.0f}  持仓={n_pos}")


def _print_report(state, data_cache, all_dates, total_entries, total_exits,
                  total_adds, total_half_exits, total_days_with_action,
                  daily_log, daily_equities):
    """打印完整的 walk-forward 回放报告。

    daily_equities: 每日权益序列（含无动作日），用于准确计算夏普和最大回撤。
    """
    trades = state["trade_history"]
    wins = [t for t in trades if t["was_win"]]
    losses = [t for t in trades if not t["was_win"]]

    win_rate = len(wins) / len(trades) * 100 if trades else 0
    total_pnl = sum(t["pnl"] for t in trades)
    avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
    avg_loss = np.mean([t["pnl"] for t in losses]) if losses else 0

    final_equity = _dry_market_equity(state, data_cache, all_dates[-1])
    total_return = (final_equity / INITIAL_CASH - 1) * 100

    start_dt = all_dates[0]
    end_dt = all_dates[-1]
    years = (end_dt - start_dt).days / 365.25
    cagr = ((final_equity / INITIAL_CASH) ** (1 / years) - 1) * 100 if years > 0 else 0

    # 最大回撤（基于每日权益序列）
    if daily_equities:
        peak = daily_equities[0]
        mdd = 0.0
        for eq in daily_equities:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100
            if dd > mdd:
                mdd = dd
    else:
        mdd = 0.0

    # 夏普比率（基于每日权益序列的全部日收益率）
    if len(daily_equities) > 1:
        eq_series = pd.Series(daily_equities)
        daily_returns = eq_series.pct_change().dropna()
        if len(daily_returns) > 0 and daily_returns.std() > 0:
            sharpe = (daily_returns.mean() / daily_returns.std()) * np.sqrt(252)
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0

    # 按品种统计
    by_symbol = {}
    for t in trades:
        sym = t["symbol"]
        if sym not in by_symbol:
            by_symbol[sym] = {"trades": 0, "wins": 0, "pnl": 0.0}
        by_symbol[sym]["trades"] += 1
        if t["was_win"]:
            by_symbol[sym]["wins"] += 1
        by_symbol[sym]["pnl"] += t["pnl"]

    print(f"\n{'=' * 60}")
    print(f"  Walk-Forward Dry-Run 报告")
    print(f"{'=' * 60}")
    print(f"  回放区间: {start_dt.strftime('%Y-%m-%d')} → {end_dt.strftime('%Y-%m-%d')} ({years:.1f}年)")
    print(f"  交易日数: {len(all_dates)}")
    print(f"  有动作日: {total_days_with_action}")
    print()
    print(f"  ── 绩效摘要 ──")
    print(f"  初始资金: {INITIAL_CASH:,.0f}")
    print(f"  最终权益: {final_equity:,.0f}")
    print(f"  总收益率: {total_return:+.2f}%")
    print(f"  年化收益: {cagr:+.2f}%")
    print(f"  最大回撤: {mdd:.2f}%")
    print(f"  夏普比率: {sharpe:.2f}")
    print()
    print(f"  ── 交易统计 ──")
    print(f"  总入场:   {total_entries}")
    print(f"  总出场:   {total_exits} (含 {total_half_exits} 次减半)")
    print(f"  总加仓:   {total_adds}")
    print(f"  完整交易: {len(trades)} 笔")
    print(f"  胜率:     {win_rate:.1f}% ({len(wins)}W / {len(losses)}L)")
    print(f"  总盈亏:   {total_pnl:+,.0f}")
    print(f"  平均盈利: {avg_win:+,.0f}")
    print(f"  平均亏损: {avg_loss:+,.0f}")
    if avg_loss != 0 and avg_win != 0:
        print(f"  盈亏比:   {abs(avg_win / avg_loss):.2f}")
    if trades:
        hold_days = [t.get("holding_days", 0) for t in trades]
        print(f"  平均持仓: {np.mean(hold_days):.0f} 天 (中位数 {np.median(hold_days):.0f})")
    print()
    print(f"  ── 按品种 ──")
    print(f"  {'品种':<14} {'交易':>6} {'胜率':>8} {'盈亏':>12}")
    print(f"  " + "-" * 42)
    for sym in ETFS:
        info = by_symbol.get(sym, {"trades": 0, "wins": 0, "pnl": 0.0})
        wr = f"{info['wins']/info['trades']*100:.0f}%" if info["trades"] > 0 else "—"
        print(f"  {sym:<14} {info['trades']:>6} {wr:>8} {info['pnl']:>+12,.0f}")

    # 最近 5 笔交易
    print(f"\n  ── 最近 5 笔交易 ──")
    for t in trades[-5:]:
        emoji = "[WIN]" if t["was_win"] else "[LOSS]"
        print(f"  {emoji} {t['entry_date']} -> {t['exit_date']}  "
              f"{t['symbol']:<12} {t['shares']:>6}sh  "
              f"entry={t['entry_price']:.3f} exit={t['exit_price']:.3f}  "
              f"PnL={t['pnl']:+,.0f}")

    print(f"\n{'=' * 60}")
    print(f"  提示：以上为历史回放模拟，非真实交易结果。")
    print(f"  加速模式参数调整 → 信号量 ↑，但绩效可能与标准参数有显著差异。")
    print(f"{'=' * 60}\n")


# ════════════════════════════════════════════════════════════
#  主循环
# ════════════════════════════════════════════════════════════

def _precompute_signal_series(data_cache: dict) -> dict:
    """预计算所有品种的完整信号序列（只算一次，O(N)）。

    返回 {symbol: {key: Series}}，所有 Series 与 data_cache 的日期索引对齐。
    """
    from src.turtle_core import TurtleSignals as TS

    sig_series = {}
    for sym, df in data_cache.items():
        calcs = TS(TURTLE)
        si = calcs.precompute_all(df["high"], df["low"], df["close"])
        # precompute_all 返回的 Series index 是 0..N-1，需要对齐到 df 的日期
        # 重建为与 df 同样长度的 Series

        # 计算滚动 ATR 百分位（向量化，快）
        n_series = si.get("n_series")
        if n_series is not None and len(n_series) >= 252:
            ns = pd.Series(n_series, index=df.index)
            n_min = ns.rolling(252, min_periods=252).min()
            n_max = ns.rolling(252, min_periods=252).max()
            denom = n_max - n_min
            atr_pct = (ns - n_min) / denom.replace(0, np.nan)
            si["atr_pct_252"] = atr_pct.values
        else:
            si["atr_pct_252"] = np.full(len(df), np.nan)

        sig_series[sym] = si

    return sig_series


def _extract_signals_at(sig_series: dict, sym: str, idx: int) -> dict | None:
    """从预计算序列中提取第 idx 行的信号值。返回 None 表示数据不足。"""
    si = sig_series.get(sym)
    if si is None or idx < 50:
        return None
    sig = {}
    for k, s in si.items():
        if isinstance(s, np.ndarray):
            v = s[idx]
            sig[k] = None if (v is None or (isinstance(v, float) and np.isnan(v))) else float(v)
        elif isinstance(s, pd.Series):
            v = s.iloc[idx]
            sig[k] = None if pd.isna(v) else float(v)
        else:
            sig[k] = s
    return sig


def _get_ohlc(data_cache: dict, sym: str, trading_date: pd.Timestamp):
    """获取某品种在某交易日的 OHLC 数据。返回 (open, high, low, close) 或 None。"""
    df = data_cache.get(sym)
    if df is None:
        return None
    row = df[df["date"] == trading_date]
    if len(row) == 0:
        return None
    return (
        float(row["open"].iloc[0]),
        float(row["high"].iloc[0]),
        float(row["low"].iloc[0]),
        float(row["close"].iloc[0]),
    )


def run_walkforward(start_date: Optional[str] = None,
                    end_date: Optional[str] = None,
                    verbose: bool = False,
                    fast: bool = False):
    """逐日遍历历史数据，模拟 dry-run 决策和执行。"""
    if fast:
        apply_fast_mode()
        print("  [FAST] 加速模式：参数已调整为高频信号版本")
        print(f"     breakout={FAST_OVERRIDES['breakout_period']}, "
              f"stop={FAST_OVERRIDES['stop_period']}, "
              f"pyramid_step={FAST_OVERRIDES['pyramid_step']}, "
              f"atr_pct_filter={FAST_OVERRIDES['atr_pct_filter']}")

    sd = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
    ed = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None

    print(f"\n  加载历史数据...")
    data_cache = load_all_data(start_date=sd, end_date=ed)
    if not data_cache:
        print("  [ERROR] 无可用数据")
        return

    first_sym = next(iter(data_cache))
    all_dates = sorted(data_cache[first_sym]["date"].dropna().unique())
    all_dates = [d for d in all_dates if not pd.isna(d)]
    print(f"  数据范围: {min(all_dates).strftime('%Y-%m-%d')} → {max(all_dates).strftime('%Y-%m-%d')}")
    print(f"  交易日数: {len(all_dates)}")
    print(f"  品种数量: {len(data_cache)}")

    # ── 预计算所有信号序列（一次 O(N)，替代逐日 O(N²)）──
    print(f"  预计算信号序列...")
    sig_series = _precompute_signal_series(data_cache)
    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    print(f"  信号预计算完成")

    state = {
        "equity": INITIAL_CASH,
        "cash": INITIAL_CASH,
        "positions": [],
        "trade_history": [],
        "signal_filter": {},
        "consecutive_losses": {},
        "buy_today": {},
        "last_signal_date": "",
        "half_exit_events": [],
    }

    total_entries = 0
    total_exits = 0
    total_adds = 0
    total_half_exits = 0
    total_days_with_action = 0
    daily_log = []
    daily_equities = []  # B2 修复：每日记录权益，用于准确的夏普/MDD

    for i, trading_date in enumerate(all_dates):
        today_str = trading_date.strftime("%Y-%m-%d")
        next_date = all_dates[i + 1] if i + 1 < len(all_dates) else None
        date_idx = date_to_idx[trading_date]

        # 跨日清空 buy_today
        if state.get("last_signal_date", "") != today_str:
            state["buy_today"] = {}
            state["last_signal_date"] = today_str

        # 提取当日信号（从预计算序列索引取值，O(1)）
        sig_cache = {}
        for sym in data_cache:
            sig_cache[sym] = _extract_signals_at(sig_series, sym, date_idx)

        sizing_equity = _dry_market_equity(state, data_cache, trading_date)
        sim_cash = state["cash"]

        # ── 1. 退出检查 ──
        exit_signals = []
        for pos in list(state["positions"]):
            sym = pos["code"]
            sig = sig_cache.get(sym)
            if sig is None:
                continue
            ohlc = _get_ohlc(data_cache, sym, trading_date)
            if ohlc is None:
                continue
            _open, high, low, close = ohlc

            # 更新持仓期最高价
            if high > pos.get("high_since_entry", 0):
                pos["high_since_entry"] = high

            # B1 修复：check_exit 内部会修改 pos["protection_activated"]，
            # 但半减的 shares/closing 状态修改延迟到 simulate_execution 中执行，
            # 如果次日没有数据则跳过，不会残留半截状态。
            exit_type = check_exit(sym, low, close, sig, state, pos)
            if exit_type == "full":
                exit_signals.append({
                    "code": sym, "type": "full",
                    "shares": pos["shares"],
                    "entry_price": pos["entry_price"],
                    "exit_price": close,
                })
            elif exit_type == "half":
                half_shares = pos["shares"] // 2
                if half_shares > 0:
                    exit_signals.append({
                        "code": sym, "type": "half",
                        "shares": half_shares,
                        "entry_price": pos["entry_price"],
                        "exit_price": close,
                    })

        # ── 2. 加仓检查 ──
        add_actions = []
        exiting_symbols = {e["code"] for e in exit_signals}
        for pos in state["positions"]:
            sym = pos["code"]
            if sym in exiting_symbols:
                continue
            sig = sig_cache.get(sym)
            if sig is None:
                continue
            n = sig.get("n")
            if n is None or n <= 0:
                continue
            ohlc = _get_ohlc(data_cache, sym, trading_date)
            if ohlc is None:
                continue
            close = ohlc[3]

            if should_add(sym, close, n, pos):
                shares = pos.get("shares_per_unit", pos["shares"])
                if shares > 0:
                    add_actions.append({
                        "symbol": sym, "shares": shares,
                        "units": pos["units"] + 1, "price": close,
                    })

        # ── 3. 入场检查 ──
        entry_actions = []
        for sym in ETFS:
            if sym not in data_cache:
                continue
            if any(p["code"] == sym for p in state["positions"]):
                continue
            sig = sig_cache.get(sym)
            if sig is None:
                continue
            ohlc = _get_ohlc(data_cache, sym, trading_date)
            if ohlc is None:
                continue
            close = ohlc[3]

            if should_enter(sym, close, sig, state, trading_date.date()):
                n = sig.get("n")
                if n is None or n <= 0:
                    continue
                _fade = _dry_calc_fade(state)
                _weight_mult = WEIGHT_MULTIPLIERS.get(sym, 1.0)
                shares = calc_shares(sizing_equity, n, close,
                                     max_cash=sim_cash,
                                     fade=_fade, weight_mult=_weight_mult)
                if shares <= 0:
                    continue
                shares = _check_risk_limits(shares, n, close, sizing_equity, state, sym)
                if shares <= 0:
                    continue
                sim_cash -= shares * close
                entry_actions.append({
                    "code": sym,
                    "shares": shares,
                    "n_at_entry": n,
                    "price": close,
                })

        # ── 4. 模拟执行 ──
        day_has_action = bool(exit_signals or add_actions or entry_actions)
        if day_has_action:
            exec_summary = simulate_execution(
                state, data_cache, trading_date, next_date,
                entry_actions, exit_signals, add_actions,
            )
            total_entries += exec_summary["entries"]
            total_exits += exec_summary["exits"]
            total_adds += exec_summary["adds"]
            total_half_exits += exec_summary["half_exits"]
            total_days_with_action += 1

            daily_log.append({
                "date": today_str,
                "entries": exec_summary["entries"],
                "exits": exec_summary["exits"],
                "adds": exec_summary["adds"],
                "half_exits": exec_summary["half_exits"],
                "positions": len(state["positions"]),
                "equity": _dry_market_equity(state, data_cache, trading_date),
            })

            if verbose:
                _print_day_summary(today_str, entry_actions, exit_signals,
                                   add_actions, exec_summary, state, data_cache)

        # B2 修复：每日记录权益（含无动作日），用于准确的夏普和最大回撤
        daily_equities.append(_dry_market_equity(state, data_cache, trading_date))

        if (i + 1) % 500 == 0:
            eq = daily_equities[-1]
            print(f"  ... {i+1}/{len(all_dates)} 日  "
                  f"权益={eq:,.0f}  持仓={len(state['positions'])}  "
                  f"累计入场={total_entries} 出场={total_exits}")

    if fast:
        restore_normal_mode()

    _print_report(state, data_cache, all_dates, total_entries, total_exits,
                  total_adds, total_half_exits, total_days_with_action,
                  daily_log, daily_equities)


# ════════════════════════════════════════════════════════════
#  入口
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Walk-Forward Dry-Run — 历史回放式 Paper Trading 模拟器")
    parser.add_argument("--start", type=str, default=None,
                        help="起始日期 YYYY-MM-DD（默认：数据最早日期）")
    parser.add_argument("--end", type=str, default=None,
                        help="截止日期 YYYY-MM-DD（默认：数据最晚日期）")
    parser.add_argument("--fast", action="store_true",
                        help="加速模式：调小参数产生更多信号（3-5x）")
    parser.add_argument("--verbose", action="store_true",
                        help="逐日打印有动作的日期")
    args = parser.parse_args()

    run_walkforward(
        start_date=args.start,
        end_date=args.end,
        verbose=args.verbose,
        fast=args.fast,
    )
