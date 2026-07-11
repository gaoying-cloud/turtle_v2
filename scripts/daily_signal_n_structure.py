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
    # S30 信号过滤 + 仓位管理
    use_ma_cross=True, max_position_pct=0.25,
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


def check_circuit_breaker(state: dict, params: dict,
                          target_date: Optional[date] = None) -> tuple[bool, str]:
    """检查是否在熔断期（对齐回测 paused_until_bar 逻辑）。

    Returns (is_paused, reason)
    """
    paused_until = state.get("paused_until")
    if paused_until is None:
        return False, ""
    ref_date = target_date or date.today()
    try:
        until_date = date.fromisoformat(paused_until)
        if ref_date < until_date:
            return True, f"熔断中 (至 {paused_until})"
    except (ValueError, TypeError):
        pass
    return False, ""


def check_entry_signal(df_ind: pd.DataFrame, idx: int,
                       strategy: NStructureStrategy,
                       state: dict,
                       sizing_equity: float | None = None) -> dict | None:
    """检查单个品种的入场信号。

    对齐回测 _check_entry_from_prev() 的全部逻辑。

    Parameters
    ----------
    sizing_equity : float | None
        用于仓位计算的权益。None 时使用 state["equity"]（向后兼容）。
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

    # 4b. MA5×MA20 金叉过滤（S30）
    if strategy.use_ma_cross:
        ma5_val = df_ind.loc[prev, 'ma5']
        ma20_val = df_ind.loc[prev, 'ma20']
        if pd.isna(ma5_val) or pd.isna(ma20_val) or ma5_val <= ma20_val:
            return None

    # 5. ATR 有效性
    atr = df_ind.loc[prev, 'atr']
    if pd.isna(atr) or atr <= 0:
        return None

    # 6. 计算入场价（含滑点）、止损、仓位
    entry_price = strategy._buy_price(df_ind.loc[idx, 'open'])
    stop = min(ns.b_price - strategy.stop_mult * atr,
               ns.b_price * strategy.stop_floor_post_break)

    if sizing_equity is not None:
        equity_for_sizing = sizing_equity
    else:
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


def update_position(df_ind: pd.DataFrame, idx: int,
                    pos: dict, strategy: NStructureStrategy) -> dict:
    """统一持仓管理：止损→退出→D突破→加仓（对齐 _manage_position 全部逻辑）。

    直接修改 pos dict（更新止损/d_broken/units/total_cost），
    返回触发的操作摘要。

    Returns
    -------
    dict
        {'action': 'exit'|'d_break_add'|'pyramid_add'|'hold',
         'detail': {...}  # 操作详情}
    """
    low = df_ind.loc[idx, 'low']
    high = df_ind.loc[idx, 'high']
    close = df_ind.loc[idx, 'close']
    atr = df_ind.loc[idx, 'atr']

    stop_loss = pos.get("stop_loss", 0)
    b_price = pos.get("b_price", 0)
    d_price = pos.get("d_price", 0)
    d_broken = pos.get("d_broken", False)
    units = pos.get("units", 1)
    entry_price = pos.get("entry_price", 0)
    shares_per_unit = pos.get("shares_per_unit", 0)
    total_cost = pos.get("total_cost", 0)

    # ── 1. 止损检查 ──
    if low <= stop_loss:
        exit_price = strategy._sell_price(min(close, stop_loss))
        total_shares = units * shares_per_unit
        avg_cost = total_cost / total_shares if total_shares > 0 else entry_price
        gross_pnl = (exit_price - avg_cost) * total_shares
        entry_comm = strategy._commission_cost(avg_cost, total_shares)
        exit_comm = strategy._commission_cost(exit_price, total_shares)
        pnl = gross_pnl - entry_comm - exit_comm
        reason = "跟踪止损" if d_broken else "初始止损"
        pos["_exit"] = {
            "exit_price": round(exit_price, 3),
            "pnl": round(pnl, 2),
            "reason": reason,
        }
        return {"action": "exit", "detail": {"reason": reason, "pnl": round(pnl, 2)}}

    # ── 2. D 点突破前 ──
    if not d_broken:
        if close > d_price:
            # D 点突破
            pos["d_broken"] = True
            if not pd.isna(atr) and atr > 0:
                new_stop = max(
                    stop_loss,
                    min(d_price - strategy.stop_mult * atr,
                        b_price * strategy.stop_floor_post_break)
                )
                pos["stop_loss"] = round(new_stop, 3)
                if units < strategy.max_units:
                    pos["units"] = units + 1
                    pos["total_cost"] = round(total_cost + close * shares_per_unit, 2)
                    pos["stop_loss"] = round(
                        max(pos["stop_loss"], close * strategy.stop_floor_post_break), 3)
                    add_cost = round(close * shares_per_unit, 2)
                    return {"action": "d_break_add",
                            "detail": {"type": "D点突破加仓", "price": round(close, 3),
                                       "new_units": units + 1,
                                       "new_stop": pos["stop_loss"],
                                       "add_cost": add_cost}}
                return {"action": "hold",
                        "detail": {"d_broken": True, "new_stop": pos["stop_loss"]}}
            # ATR 无效时仅标记 d_broken，不更新止损
            return {"action": "hold", "detail": {"d_broken": True}}
        else:
            # B 点止损地板：上移止损
            b_floor = round(b_price * strategy.stop_floor_pre_break, 3)
            if stop_loss < b_floor:
                pos["stop_loss"] = b_floor
        return {"action": "hold", "detail": {}}

    # ── 3. D 突破后：跟踪止损 + 金字塔加仓 ──
    if not pd.isna(atr) and atr > 0:
        new_stop = round(high - strategy.trail_mult * atr, 3)
        pos["stop_loss"] = round(max(stop_loss, new_stop), 3)

    if units < strategy.max_units:
        next_level = entry_price + units * strategy.add_step * atr
        if close >= next_level:  # 对齐回测：用 close 而非 high
            pos["units"] = units + 1
            pos["total_cost"] = round(total_cost + close * shares_per_unit, 2)
            pos["stop_loss"] = round(
                max(pos["stop_loss"], close * strategy.stop_floor_pre_break), 3)
            add_cost = round(close * shares_per_unit, 2)
            return {"action": "pyramid_add",
                    "detail": {"type": "金字塔加仓", "price": round(close, 3),
                               "new_units": units + 1,
                               "new_stop": pos["stop_loss"],
                               "add_cost": add_cost}}

    return {"action": "hold", "detail": {}}


def scan_signals(symbols: list[str], state: dict,
                 target_date: Optional[date] = None) -> dict:
    """扫描所有品种的 N 字结构信号。

    对齐回测 run() 的完整逻辑：熔断→持仓管理→入场。
    直接修改 state dict（权益/持仓/连续亏损/熔断）。
    """
    strategy = NStructureStrategy(**DEFAULT_PARAMS)
    results = {}

    # ── 熔断检查 ──
    is_paused, pause_reason = check_circuit_breaker(state, DEFAULT_PARAMS,
                                                     target_date=target_date)

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

        # ── 持仓管理：统一处理退出/止损更新/加仓 ──
        positions = state.get("positions", {})
        pos_action = {"action": "hold", "detail": {}}
        if sym in positions and not is_paused:
            pos_action = update_position(
                df_ind, target_idx, positions[sym], strategy,
            )
            # 退出：更新权益和连续亏损计数
            if pos_action["action"] == "exit":
                exit_detail = pos_action["detail"]
                state["equity"] = round(
                    float(state.get("equity", strategy.initial_capital))
                    + exit_detail["pnl"], 2)
                state["consecutive_losses"] = (
                    state.get("consecutive_losses", 0) + 1
                    if exit_detail["pnl"] < 0 else 0)
                # 熔断检查
                if state["consecutive_losses"] >= strategy.max_consecutive_losses:
                    from datetime import timedelta
                    pause_date = (target_date or date.today()) + timedelta(
                        days=strategy.pause_bars)
                    state["paused_until"] = pause_date.isoformat()
                # 移除持仓
                pos_snapshot = positions.pop(sym)
                state["trade_history"].append({
                    "symbol": sym,
                    "entry_price": pos_snapshot.get("entry_price"),
                    "exit_price": pos_snapshot.get("_exit", {}).get("exit_price"),
                    "pnl": exit_detail["pnl"],
                    "reason": exit_detail["reason"],
                })

        # ── 入场信号（无持仓 + 非熔断） ──
        entry_info = None
        if sym not in positions and not is_paused:
            # 用 per-symbol 权益计算仓位（对齐回测 capital_per_symbol）
            per_symbol_equity = float(state.get("equity", strategy.initial_capital))
            entry_info = check_entry_signal(
                df_ind, target_idx, strategy, state,
                sizing_equity=per_symbol_equity / max(1, len(symbols)),
            )

        # ── 应用入场到 state ──
        if entry_info:
            positions[sym] = {
                "entry_price": float(entry_info["entry_price"]),
                "stop_loss": float(entry_info["stop_loss"]),
                "shares_per_unit": int(entry_info["shares"]),
                "total_cost": float(entry_info["total_cost"]),
                "units": 1,
                "d_broken": bool(entry_info["entry_price"] > entry_info["d_price"]),
                "b_price": float(entry_info["b_price"]),
                "d_price": float(entry_info["d_price"]),
                "a_price": float(entry_info["a_price"]),
            }
            state["positions"] = positions

        # ── N 字结构参考信息（用 i-1 避免未来信息泄露） ──
        ref_idx = max(0, target_idx - 1)
        ns = find_n_structure_in_window(
            df_ind, ref_idx, strategy.window_size,
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
            "exit": pos_action["detail"] if pos_action["action"] == "exit" else None,
            "add": pos_action["detail"] if pos_action["action"] in ("d_break_add", "pyramid_add") else None,
            "paused": is_paused,
        }

    return results


def print_signals(signals: dict, state: dict,
                  target_date: Optional[date] = None):
    """打印信号汇总。"""
    ref_date = target_date or date.today()
    today = ref_date.isoformat()
    positions = state.get("positions", {})

    is_paused, pause_reason = check_circuit_breaker(state, DEFAULT_PARAMS,
                                                     target_date=target_date)

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
            cost = pos.get("total_cost", 0)
            print(f"     {sym}: {pos.get('units', 1)}单位 "
                  f"@{pos.get('entry_price', 0):.3f}  "
                  f"止损={pos.get('stop_loss', 0):.3f}  "
                  f"成本=¥{cost:,.0f}{flags}")
        print()

    # 出场信号汇总
    exits = [(s, sig.get("exit")) for s, sig in signals.items()
             if sig.get("exit")]
    if exits:
        print(f"  🔴 出场信号 ({len(exits)}):")
        for sym, info in exits:
            print(f"     {sym}: {info.get('reason', '')}  "
                  f"盈亏=¥{info.get('pnl', 0):+,.0f}")
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
    print_signals(signals, state, target_date=target)

    save_state(state, state_path)
    print(f"💾 状态已保存: {state_path}")


if __name__ == "__main__":
    main()
