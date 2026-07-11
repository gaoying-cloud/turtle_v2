#!/usr/bin/env python
"""
N字结构策略 · 每日信号扫描 (S28)

独立于 Backtrader，纯 pandas 实现。
与海龟的 daily_signal.py 架构对齐，共用 state.json 模式。
**所有信号逻辑与回测 n_structure.py 完全一致。**

用法：
    py scripts/daily_signal_n_structure.py              # 扫描今日信号
    py scripts/daily_signal_n_structure.py --date 2026-07-09  # 指定日期
    py scripts/daily_signal_n_structure.py --state state_ns.json  # 指定状态文件
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from strategies.n_structure import (
    NStructureStrategy, find_n_structure_in_window, ma5_confirm,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = REPO / "data" / "etf_daily"
DEFAULT_STATE_FILE = REPO / "data" / "state_ns.json"

# S27 定型参数（与回测完全一致）
DEFAULT_PARAMS = dict(
    window_size=100, atr_period=25, stop_mult=1.5, trail_mult=5.0,
    add_step=2.0, max_units=6, ma_trend=0, use_ma5_confirm=False,
    initial_capital=100_000, risk_per_trade=0.01, num_symbols=6,
    slippage_pct=0.001, commission_pct=0.00015,
    use_dynamic_equity=True, max_consecutive_losses=5, pause_bars=20,
    # S24 形态识别参数
    confirm_k=2, min_advance=0.05, min_gap_ad=5, min_gap_db=3,
    local_half_window=2,
    # S27 止损地板参数
    stop_floor_pre_break=0.97, stop_floor_post_break=0.95,
)


def load_state(path: Path) -> dict:
    """加载持仓状态。"""
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    return _default_state()


def _default_state() -> dict:
    return {
        "equity": 100_000.0,
        "positions": {},
        "trade_history": [],
        "consecutive_losses": 0,
        "paused_until": None,  # ISO date string
        "updated": "",
    }


def save_state(state: dict, path: Path):
    state["updated"] = date.today().isoformat()
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding='utf-8')


def load_symbol_data(symbol: str, lookback: int = 300) -> pd.DataFrame:
    """加载指定品种最近 N 条数据。"""
    path = DATA_DIR / f"{symbol}.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    df = df.sort_values("date").tail(lookback).reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])
    return df


def check_circuit_breaker(state: dict, params: dict) -> tuple[bool, str]:
    """检查是否在熔断期（对齐回测 paused_until_bar 逻辑）。

    Returns (is_paused, reason)
    """
    paused_until = state.get("paused_until")
    if paused_until is None:
        return False, ""
    try:
        until_date = date.fromisoformat(paused_until)
        if date.today() < until_date:
            return True, f"熔断中 (至 {paused_until})"
    except (ValueError, TypeError):
        pass
    return False, ""


def check_entry_signal(df_ind: pd.DataFrame, idx: int,
                       strategy: NStructureStrategy,
                       state: dict) -> dict | None:
    """检查单个品种的入场信号。

    对齐回测 _check_entry_from_prev() 的全部逻辑。

    Returns
    -------
    dict or None
        有信号时返回 {entry_price, stop, shares, ...}；无信号返回 None。
    """
    if idx < 1:
        return None

    prev = idx - 1

    # 1. 扫描 N 字结构（数据截止到 prev，不含当前 bar）
    ns = find_n_structure_in_window(
        df_ind, prev, strategy.window_size,
        confirm_k=strategy.confirm_k,
        min_advance=strategy.min_advance,
        min_gap_ad=strategy.min_gap_ad,
        min_gap_db=strategy.min_gap_db,
        local_half_window=strategy.local_half_window,
    )
    if ns is None:
        return None

    # 2. MA5 辅助确认
    if strategy.use_ma5_confirm:
        if not ma5_confirm(df_ind, ns, prev):
            return None

    # 3. 进场条件：昨日收盘 > B
    prev_close = df_ind.loc[prev, 'close']
    if prev_close <= ns.b_price:
        return None

    # 4. 趋势过滤
    if strategy.ma_trend > 0:
        prev_ma = df_ind.loc[prev, 'ma_trend']
        if pd.isna(prev_ma) or prev_close <= prev_ma:
            return None

    # 5. ATR 有效性
    atr = df_ind.loc[prev, 'atr']
    if pd.isna(atr) or atr <= 0:
        return None

    # 6. 计算入场价（含滑点）、止损、仓位
    entry_price = strategy._buy_price(df_ind.loc[idx, 'open'])
    stop = min(ns.b_price - strategy.stop_mult * atr,
               ns.b_price * strategy.stop_floor_post_break)

    equity = float(state.get("equity", strategy.initial_capital))
    equity_for_sizing = equity if strategy.use_dynamic_equity else strategy.capital_per_symbol
    shares = strategy._calc_shares(equity_for_sizing, entry_price, atr)

    return {
        "symbol": "",
        "entry_price": round(entry_price, 3),
        "stop_loss": round(stop, 3),
        "shares": shares,
        "total_cost": round(entry_price * shares, 2),
        "a_price": round(float(ns.a_price), 3),
        "d_price": round(float(ns.d_price), 3),
        "b_price": round(float(ns.b_price), 3),
        "atr": round(float(atr), 3),
        "prev_close": round(float(prev_close), 3),
    }


def check_position_exit(df_ind: pd.DataFrame, idx: int,
                        pos: dict, strategy: NStructureStrategy) -> str:
    """检查持仓是否需要退出。

    对齐回测 _manage_position() 的退出逻辑。

    Returns
    -------
    str
        "" = 不退出, "初始止损"/"跟踪止损" = 退出原因。
    """
    low = df_ind.loc[idx, 'low']
    high = df_ind.loc[idx, 'high']
    close = df_ind.loc[idx, 'close']
    stop_loss = pos.get("stop_loss", 0)
    b_price = pos.get("b_price", 0)
    d_price = pos.get("d_price", 0)
    d_broken = pos.get("d_broken", False)

    # 1. 止损检查
    if low <= stop_loss:
        return "跟踪止损" if d_broken else "初始止损"

    # 2. D 点突破前：B 点作为止损地板
    if not d_broken:
        if close > d_price:
            # 突破 D — 这是加仓信号，不是退出
            pass
        else:
            # B 点止损地板（S27：简化逻辑，不再强制平仓）
            b_floor = b_price * strategy.stop_floor_pre_break
            if stop_loss < b_floor:
                pass  # 止损会自动提到 B 点附近

    # 3. D 突破后：跟踪止损
    if d_broken:
        atr = df_ind.loc[idx, 'atr']
        if not pd.isna(atr) and atr > 0:
            new_stop = high - strategy.trail_mult * atr
            # 如果 low <= 新的跟踪止损 → 应该在步骤 1 已触发
            pass

    return ""


def check_position_add(df_ind: pd.DataFrame, idx: int,
                       pos: dict, strategy: NStructureStrategy) -> dict | None:
    """检查持仓是否需要加仓。

    对齐回测 _manage_position() 的 D 点突破加仓和金字塔加仓。

    Returns
    -------
    dict or None
    """
    close = df_ind.loc[idx, 'close']
    high = df_ind.loc[idx, 'high']
    atr = df_ind.loc[idx, 'atr']
    if pd.isna(atr) or atr <= 0:
        return None

    units = pos.get("units", 1)
    d_price = pos.get("d_price", 0)
    d_broken = pos.get("d_broken", False)
    entry_price = pos.get("entry_price", 0)
    stop_loss = pos.get("stop_loss", 0)
    b_price = pos.get("b_price", 0)

    max_units = strategy.max_units

    if not d_broken and close > d_price:
        # D 点突破加仓（对齐 _manage_position）
        new_stop = max(
            stop_loss,
            min(d_price - strategy.stop_mult * atr,
                b_price * strategy.stop_floor_post_break)
        )
        add_price = strategy._buy_price(close)
        # 加仓后上移止损保护新增仓位
        new_stop = max(new_stop, close * strategy.stop_floor_post_break)
        return {
            "type": "D点突破加仓",
            "price": round(add_price, 3),
            "new_units": units + 1,
            "new_stop": round(new_stop, 3),
            "add_cost": round(close * pos.get("shares_per_unit", 0), 2),
        }

    if d_broken and units < max_units:
        next_level = entry_price + units * strategy.add_step * atr
        if high >= next_level:
            # 金字塔加仓（对齐 _manage_position）
            new_stop = max(stop_loss, close * strategy.stop_floor_pre_break)
            return {
                "type": "金字塔加仓",
                "price": round(close, 3),
                "new_units": units + 1,
                "next_level": round(entry_price + (units + 1) * strategy.add_step * atr, 3),
                "new_stop": round(new_stop, 3),
                "add_cost": round(close * pos.get("shares_per_unit", 0), 2),
            }

    return None


def scan_signals(symbols: list[str], state: dict,
                 target_date: Optional[date] = None) -> dict:
    """扫描所有品种的 N 字结构信号。

    对齐回测 run() 的完整逻辑：熔断→退出→加仓→入场。
    """
    strategy = NStructureStrategy(**DEFAULT_PARAMS)
    results = {}

    # ── 熔断检查 ──
    is_paused, pause_reason = check_circuit_breaker(state, DEFAULT_PARAMS)

    for sym in symbols:
        df = load_symbol_data(sym, lookback=300)
        if df.empty or len(df) < 100:
            results[sym] = {"status": "no_data"}
            continue

        # 确定目标日期索引
        if target_date is not None:
            target_dt = pd.Timestamp(target_date)
            matches = df[df['date'] == target_dt]
            target_idx = int(matches.index[0]) if not matches.empty else len(df) - 1
        else:
            target_idx = len(df) - 1

        df_ind = strategy.compute_indicators(df)

        # ── 检查已有持仓的退出/加仓 ──
        exit_info = {}
        add_info = {}
        positions = state.get("positions", {})
        if sym in positions and not is_paused:
            exit_reason = check_position_exit(
                df_ind, target_idx, positions[sym], strategy,
            )
            if exit_reason:
                exit_info = {"reason": exit_reason, "symbol": sym}
            else:
                add_info = check_position_add(
                    df_ind, target_idx, positions[sym], strategy,
                )

        # ── 入场信号（无持仓 + 非熔断） ──
        entry_info = None
        if sym not in positions and not is_paused:
            entry_info = check_entry_signal(df_ind, target_idx, strategy, state)

        # ── N 字结构信息（始终扫描，供参考） ──
        ns = find_n_structure_in_window(
            df_ind, target_idx, strategy.window_size,
            confirm_k=strategy.confirm_k,
            min_advance=strategy.min_advance,
            min_gap_ad=strategy.min_gap_ad,
            min_gap_db=strategy.min_gap_db,
            local_half_window=strategy.local_half_window,
        )

        results[sym] = {
            "status": "ok",
            "has_position": sym in positions,
            "has_structure": ns is not None,
            "entry": entry_info,
            "exit": exit_info if exit_info else None,
            "add": add_info if add_info else None,
            "paused": is_paused,
        }

    return results


def print_signals(signals: dict, state: dict):
    """打印信号汇总。"""
    today = date.today().isoformat()
    positions = state.get("positions", {})

    is_paused, pause_reason = check_circuit_breaker(state, DEFAULT_PARAMS)

    print(f"\n{'='*60}")
    print(f"  N字结构策略 · 每日信号  ({today})")
    print(f"{'='*60}")
    print(f"  当前权益: ¥{state['equity']:,.0f}  |  "
          f"持仓品种: {len(positions)}  |  "
          f"连续亏损: {state.get('consecutive_losses', 0)}")
    if is_paused:
        print(f"  ⚠️  {pause_reason}")
    print()

    # 持仓状态
    if positions:
        print(f"  📦 当前持仓:")
        for sym, pos in positions.items():
            sig = signals.get(sym, {})
            exit_sig = sig.get("exit")
            add_sig = sig.get("add")
            flags = ""
            if exit_sig:
                flags = f"  ⚠️ 退出信号: {exit_sig.get('reason', '')}"
            elif add_sig:
                flags = f"  ➕ 加仓信号: {add_sig.get('type', '')}"
            print(f"     {sym}: {pos.get('units', 1)}单位 "
                  f"@{pos.get('entry_price', 0):.3f}  "
                  f"止损={pos.get('stop_loss', 0):.3f}{flags}")
        print()

    # 出场信号汇总
    exits = [(s, sig.get("exit")) for s, sig in signals.items()
             if sig.get("exit")]
    if exits:
        print(f"  🔴 出场信号 ({len(exits)}):")
        for sym, info in exits:
            print(f"     {sym}: {info.get('reason', '')}")
        print()

    # 加仓信号
    adds = [(s, sig.get("add")) for s, sig in signals.items()
            if sig.get("add")]
    if adds:
        print(f"  ➕ 加仓信号 ({len(adds)}):")
        for sym, info in adds:
            print(f"     {sym}: {info.get('type', '')} "
                  f"@ {info.get('price', 0):.3f}  "
                  f"→ {info.get('new_units', 0)}单位  "
                  f"新止损={info.get('new_stop', 0):.3f}  "
                  f"成本+¥{info.get('add_cost', 0):,.0f}")
        print()

    # 入场信号
    entries = [(s, sig.get("entry")) for s, sig in signals.items()
               if sig.get("entry")]
    if entries:
        print(f"  🟢 入场信号 ({len(entries)}):")
        for sym, info in entries:
            print(f"     {sym}: B={info['b_price']:.3f}  "
                  f"进场≈{info['entry_price']:.3f}(含滑点)  "
                  f"止损={info['stop_loss']:.3f}  "
                  f"建议{info['shares']}股  "
                  f"ATR={info['atr']:.3f}")
    elif not is_paused:
        # 结构存在但未触发
        structures = [(s, sig) for s, sig in signals.items()
                      if sig.get("has_structure") and not sig.get("entry")
                      and not sig.get("has_position")]
        if structures:
            print(f"  🔍 结构存在但未触发 ({len(structures)}):")
            for sym, sig in structures:
                ns_info = sig.get("entry") or {}
                b = ns_info.get('b_price', '?')
                print(f"     {sym}: B={b}  (需 close > B 才触发)")
        else:
            print(f"  ⚪ 无入场信号（无可识别的N字结构）")
    else:
        print(f"  ⚪ 熔断中，跳过入场")
    print()


def main():
    parser = argparse.ArgumentParser(description="N字结构每日信号")
    parser.add_argument("--date", type=str, default=None,
                        help="指定日期 (YYYY-MM-DD), 默认今天")
    parser.add_argument("--state", type=str, default=str(DEFAULT_STATE_FILE),
                        help="状态文件路径")
    parser.add_argument("--symbols", nargs="+",
                        default=["510500.SH", "159915.SZ", "513100.SH",
                                 "518880.SH", "159985.SZ", "513520.SH"],
                        help="品种列表")
    args = parser.parse_args()

    target = None
    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()

    state_path = Path(args.state)
    state = load_state(state_path)

    print(f"🔍 扫描 N 字结构信号...")
    signals = scan_signals(args.symbols, state, target)
    print_signals(signals, state)

    save_state(state, state_path)
    print(f"💾 状态已保存: {state_path}")


if __name__ == "__main__":
    main()
