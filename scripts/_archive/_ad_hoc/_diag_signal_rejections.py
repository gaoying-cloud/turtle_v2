#!/usr/bin/env python
"""
信号拒绝逐笔追踪诊断 — 纯 pandas 状态机模拟 B4 策略。

输出 results/diagnostics/signal_rejection_trace.csv + 控制台汇总。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.turtle_core import (  # noqa: E402
    TurtleSignals, TurtlePositions, SignalFilter,
    calc_position_size, calc_pyramid_trigger,
)
from src.risk_parity import compute_alpha_weights  # noqa: E402

DATA_DIR = ROOT / "data" / "etf_daily"
OUT_DIR = ROOT / "results" / "diagnostics"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── 配置（与 turtle_config.yaml 一致） ──
COMMISSION_PCT = 0.00015
TURTLE_PARAMS = {
    "breakout_period": 20, "atr_period": 20,
    "stop_period": 10, "stop_atr_multiple": 2.0,
}
RISK_PER_UNIT = 0.01
SINGLE_MAX_RISK = 0.04
MAX_PORTFOLIO_RISK = 0.20
MAX_CONSEC_LOSSES = 8
PAUSE_DAYS = 5
MAX_5DAY_DD_PCT = 0.10
ALPHA = 0.05
COV_LOOKBACK = 252
REBALANCE_QUARTERLY = True
ATR_CHANGE_THRESHOLD = 0.30
MIN_UNIT = 100
STOP_MULT = 2.0
CONCENTRATION_TRIGGER = 3
CONCENTRATED_RISK = 0.005

SYMBOLS = ["510500.SH", "159915.SZ", "513100.SH", "518880.SH"]
T_PLUS_ONE = {"510500.SH", "159915.SZ"}

# ── 加载数据 ──
def load_all() -> dict[str, pd.DataFrame]:
    dfs = {}
    for sym in SYMBOLS:
        path = DATA_DIR / f"{sym}.parquet"
        df = pd.read_parquet(path)
        df = df.sort_values("date").reset_index(drop=True)
        dfs[sym] = df
    return dfs

# ── 预计算信号 ──
def precompute_all(dfs: dict[str, pd.DataFrame]) -> dict[str, dict[str, pd.Series]]:
    signal_calc = TurtleSignals(TURTLE_PARAMS)
    result = {}
    for sym, df in dfs.items():
        result[sym] = signal_calc.precompute_all(df["high"], df["low"], df["close"])
    return result

# ── 风险平价缓存 ──
_last_rebalance_day: date | None = None
_alpha_risk_pcts: np.ndarray | None = None
_last_n_values: dict[str, float] = {}

def maybe_rebalance(dt: date, dfs: dict, signals: dict, equity: float):
    global _last_rebalance_day, _alpha_risk_pcts, _last_n_values
    # 季度检查
    rebalance = False
    if REBALANCE_QUARTERLY:
        q = (dt.month - 1) // 3 + 1
        if _last_rebalance_day is None or (
            _last_rebalance_day.year != dt.year
            or (_last_rebalance_day.month - 1) // 3 + 1 != q
        ):
            rebalance = True
    # ATR 变动检查
    if not rebalance and _last_n_values:
        for sym in SYMBOLS:
            idx = dfs[sym].index[dfs[sym]["date"] == pd.Timestamp(dt)]
            if len(idx):
                i = idx[0]
                n_now = signals[sym]["n"].iloc[i] if i < len(signals[sym]["n"]) else np.nan
                n_old = _last_n_values.get(sym)
                if pd.notna(n_now) and n_old and n_old > 0:
                    chg = abs(n_now - n_old) / n_old
                    if chg >= ATR_CHANGE_THRESHOLD:
                        rebalance = True
                        break
    if not rebalance:
        return
    # 构建收益率矩阵
    n_sym = len(SYMBOLS)
    returns_list = []
    for sym in SYMBOLS:
        df = dfs[sym]
        idx = df.index[df["date"] == pd.Timestamp(dt)]
        if not len(idx):
            return
        i = idx[0]
        start = max(0, i - COV_LOOKBACK + 1)
        close_series = df.loc[start:i, "close"].values
        if len(close_series) < 10:
            returns_list.append(pd.Series(dtype=float))
            continue
        ret = pd.Series(np.diff(close_series) / close_series[:-1])
        returns_list.append(ret)
    if len(returns_list) != n_sym:
        return
    # 对齐长度
    non_empty = [r for r in returns_list if len(r) > 0]
    if not non_empty:
        return
    min_len = min(len(r) for r in non_empty)
    if min_len < 5:
        return
    returns_matrix = np.column_stack([
        r.iloc[-min_len:].values if len(r) >= min_len
        else np.full(min_len, np.nan)
        for r in returns_list
    ])
    if np.isnan(returns_matrix).any():
        return
    try:
        w = compute_alpha_weights(returns_matrix, alpha=ALPHA, base_risk_pct=RISK_PER_UNIT)
        _alpha_risk_pcts = w["risk_pcts"]
    except Exception:
        pass
    # 更新 last_n
    for sym in SYMBOLS:
        idx = dfs[sym].index[dfs[sym]["date"] == pd.Timestamp(dt)]
        if len(idx):
            i = idx[0]
            nv = signals[sym]["n"].iloc[i] if i < len(signals[sym]["n"]) else np.nan
            if pd.notna(nv):
                _last_n_values[sym] = nv
    _last_rebalance_day = dt


# ── 主诊断 ──
def main():
    global _last_rebalance_day, _alpha_risk_pcts, _last_n_values
    dfs = load_all()
    signals = precompute_all(dfs)

    # 构建日期索引（取所有品种的并集）
    all_dates = pd.DatetimeIndex([])
    for sym in SYMBOLS:
        all_dates = all_dates.union(dfs[sym]["date"])
    all_dates = all_dates.sort_values()

    # 状态机
    cash = 120_000.0
    positions = TurtlePositions(max_units=4)
    signal_filter = SignalFilter(max_rejections=3)
    paused_until: dict[str, date | None] = {s: None for s in SYMBOLS}
    consec_losses: dict[str, int] = {s: 0 for s in SYMBOLS}
    buy_today: dict[str, bool] = {s: False for s in SYMBOLS}
    equity_history: list[tuple[date, float]] = []

    records: list[dict] = []
    rejected_total: dict[str, int] = {s: 0 for s in SYMBOLS}
    rejected_detail: dict[str, dict[str, int]] = {
        s: {"executed": 0, "paused": 0, "signal_filter": 0, "zero_shares": 0,
            "risk_single_max": 0, "risk_portfolio_max": 0, "t_plus_one": 0}
        for s in SYMBOLS
    }

    def write_record(sym, dt, close, entry_h, cash_val, n_pos, pos_detail,
                     risk_budget, sf_state, rejection, detail):
        rejected_total[sym] += 1
        if rejection in rejected_detail[sym]:
            rejected_detail[sym][rejection] += 1
        records.append({
            "date": dt.isoformat(),
            "symbol": sym,
            "close": round(close, 4),
            "entry_high_20": round(entry_h, 4) if pd.notna(entry_h) else "",
            "cash": round(cash_val, 2),
            "n_positions": n_pos,
            "positions_detail": pos_detail,
            "risk_budget_used_pct": risk_budget,
            "signal_filter_state": sf_state,
            "rejection_point": rejection,
            "detail": detail,
        })

    def equity() -> float:
        val = cash
        for sym in SYMBOLS:
            pos = positions.get(sym)
            if pos is None:
                continue
            df = dfs[sym]
            mask = df["date"] == pd.Timestamp(dt)
            if mask.any():
                val += pos.total_shares * float(df.loc[mask, "close"].iloc[0])
        return val

    def get_val(sym: str, col: str, i: int) -> float:
        if i < len(dfs[sym]):
            return float(dfs[sym].loc[i, col])
        return np.nan

    def get_sig(sym: str, key: str, i: int) -> float:
        s = signals[sym].get(key)
        if s is not None and i < len(s):
            v = s.iloc[i]
            return float(v) if pd.notna(v) else np.nan
        return np.nan

    # 只从所有品种都有数据的起始日开始
    start_date = max(dfs[sym]["date"].min() for sym in SYMBOLS)
    mask = all_dates >= start_date
    iter_dates = all_dates[mask]

    print(f"开始逐日回放: {iter_dates[0].date()} ~ {iter_dates[-1].date()} ({len(iter_dates)} 天)")

    for dt_ts in iter_dates:
        dt = dt_ts.date()

        # ── 日初维护 ──
        # 新日重置 buy_today
        if dt_ts.hour == 0:  # 每天一次
            buy_today = {s: False for s in SYMBOLS}

        # 更新权益 & 5日回撤
        eq = equity()
        equity_history.append((dt, eq))
        if len(equity_history) > 6:
            equity_history.pop(0)

        # 5日回撤暂停
        if len(equity_history) >= 5:
            peak = max(v for _, v in equity_history)
            if peak > 0:
                dd = (peak - eq) / peak
                if dd >= MAX_5DAY_DD_PCT:
                    for sym in SYMBOLS:
                        paused_until[sym] = dt + timedelta(days=PAUSE_DAYS)

        # 暂停到期
        for sym in SYMBOLS:
            if paused_until[sym] is not None and dt >= paused_until[sym]:
                paused_until[sym] = None

        # 更新持仓天数
        for pos in positions.all_positions():
            pos.holding_days += 1

        # 风险平价重新平衡
        maybe_rebalance(dt, dfs, signals, eq)

        # 获取当日索引
        idx_map: dict[str, int] = {}
        for sym in SYMBOLS:
            m = dfs[sym]["date"] == dt_ts
            if m.any():
                idx_map[sym] = int(m.idxmax())

        if not idx_map:
            continue

        # ── 先处理退出（所有品种） ──
        for sym in SYMBOLS:
            if sym not in idx_map:
                continue
            i = idx_map[sym]
            pos = positions.get(sym)
            if pos is None:
                continue

            # T+1 检查
            if sym in T_PLUS_ONE and buy_today.get(sym, False):
                continue

            low = get_val(sym, "low", i)
            close = get_val(sym, "close", i)
            stop_low = get_sig(sym, "stop_low_10", i)

            if pd.notna(stop_low) and pd.notna(low) and low <= stop_low:
                # 执行退出
                pnl = (close - pos.entry_price) * pos.total_shares
                was_win = pnl > 0
                cash += pos.total_shares * close * (1 - COMMISSION_PCT)
                signal_filter.record_result(sym, was_win)
                if was_win:
                    consec_losses[sym] = 0
                else:
                    consec_losses[sym] += 1
                    if consec_losses[sym] >= MAX_CONSEC_LOSSES:
                        paused_until[sym] = dt + timedelta(days=PAUSE_DAYS)
                positions.close(sym)

        # ── 再处理入场（所有品种） ──
        for sym in SYMBOLS:
            if sym not in idx_map:
                continue
            i = idx_map[sym]
            pos = positions.get(sym)
            if pos is not None:
                # 有持仓 → 检查加仓
                if i > 0:
                    high = get_val(sym, "high", i)
                    close = get_val(sym, "close", i)
                    n = get_sig(sym, "n", i)
                    if pd.notna(n) and n > 0:
                        trigger = calc_pyramid_trigger(
                            base_price=pos.entry_price,
                            current_units=pos.units,
                            n_at_entry=pos.n_at_entry,
                            direction="long",
                        )
                        if pd.notna(trigger) and high >= trigger:
                            add_shares = pos.shares_per_unit
                            cost = add_shares * trigger * (1 + COMMISSION_PCT)
                            if cash >= cost:
                                cash -= cost
                                positions.add_unit(sym, 0.0)
                continue

            # 无持仓 → 检查入场
            close = get_val(sym, "close", i)
            entry_h = get_sig(sym, "entry_high_20", i)
            n = get_sig(sym, "n", i)

            # 突破条件
            if pd.isna(entry_h) or pd.isna(close) or not (close > entry_h):
                continue

            # ── 记录一次信号 ──
            eq_now = equity()
            n_pos = positions.count
            pos_detail = ",".join(
                f"{p.symbol}:{p.total_shares}" for p in positions.all_positions()
            ) if n_pos > 0 else ""
            risk_budget = round(
                sum(p.total_shares * STOP_MULT * p.n_at_entry for p in positions.all_positions()) / eq_now * 100, 2
            ) if eq_now > 0 and n_pos > 0 else 0.0

            # SignalFilter 状态
            sf = signal_filter._states.get(sym)
            sf_state = f"last_win={sf.last_trade_was_win},consec_rej={sf.consecutive_rejections}" if sf else "no_state"

            rejection = "executed"
            detail = ""

            # 1) 暂停检查
            if paused_until[sym] is not None:
                rejection = "paused"
                detail = f"暂停至{paused_until[sym]}"
                write_record(sym, dt, close, entry_h, cash, n_pos,
                            pos_detail, risk_budget, sf_state, rejection, detail)
                continue

            # 2) SignalFilter
            ok, reason = signal_filter.check_entry(sym, has_position=False)
            if not ok:
                rejection = "signal_filter"
                detail = reason
                write_record(sym, dt, close, entry_h, cash, n_pos,
                            pos_detail, risk_budget, sf_state, rejection, detail)
                continue

            # 3) 风险权重 & 仓位计算
            sym_idx = SYMBOLS.index(sym)
            base_risk = float(_alpha_risk_pcts[sym_idx]) if _alpha_risk_pcts is not None else RISK_PER_UNIT
            fade = {0: 1.0, 1: 1.0, 2: 1.0, 3: 0.8, 4: 0.6}.get(n_pos, 0.5)
            risk = base_risk * fade

            if pd.isna(n) or n <= 0:
                rejection = "zero_shares"
                detail = f"n={n}"
                write_record(sym, dt, close, entry_h, cash, n_pos,
                            pos_detail, risk_budget, sf_state, rejection, detail)
                continue

            shares = calc_position_size(eq_now, n, close, risk,
                                        stop_mult=STOP_MULT, min_unit=MIN_UNIT)
            if shares == 0:
                rejection = "zero_shares"
                detail = f"risk={risk:.4f} n={n:.4f} eq={eq_now:.0f}"
                write_record(sym, dt, close, entry_h, cash, n_pos,
                            pos_detail, risk_budget, sf_state, rejection, detail)
                continue

            # 4) 单品种风险
            per_share_risk = STOP_MULT * n
            existing_risk = 0.0
            pos_existing = positions.get(sym)
            if pos_existing is not None:
                existing_risk = pos_existing.total_shares * STOP_MULT * pos_existing.n_at_entry
            total_sym_risk_pct = (existing_risk + shares * per_share_risk) / eq_now if eq_now > 0 else 1
            if total_sym_risk_pct > SINGLE_MAX_RISK:
                max_new = eq_now * SINGLE_MAX_RISK - existing_risk
                adjusted = int(max_new / per_share_risk / 100) * 100
                if adjusted <= 0:
                    rejection = "risk_single_max"
                    detail = f"risk_pct={total_sym_risk_pct*100:.2f}%>{SINGLE_MAX_RISK*100:.0f}%"
                    write_record(sym, dt, close, entry_h, cash, n_pos,
                                pos_detail, risk_budget, sf_state, rejection, detail)
                    continue
                shares = adjusted

            # 5) 全账户风险
            total_existing = sum(
                p.total_shares * STOP_MULT * p.n_at_entry
                for p in positions.all_positions()
            )
            total_new_pct = (total_existing + shares * per_share_risk) / eq_now if eq_now > 0 else 1
            if total_new_pct > MAX_PORTFOLIO_RISK:
                max_new = eq_now * MAX_PORTFOLIO_RISK - total_existing
                adjusted = int(max_new / per_share_risk / 100) * 100
                if adjusted <= 0:
                    rejection = "risk_portfolio_max"
                    detail = f"total_risk={total_new_pct*100:.2f}%>{MAX_PORTFOLIO_RISK*100:.0f}%"
                    write_record(sym, dt, close, entry_h, cash, n_pos,
                                pos_detail, risk_budget, sf_state, rejection, detail)
                    continue
                shares = adjusted

            # 6) T+1
            if sym in T_PLUS_ONE and buy_today.get(sym, False):
                rejection = "t_plus_one"
                detail = "当日已买入同品种"
                write_record(sym, dt, close, entry_h, cash, n_pos,
                            pos_detail, risk_budget, sf_state, rejection, detail)
                continue

            # ── 全部通过，执行 ──
            cost = shares * close * (1 + COMMISSION_PCT)
            if cash < cost:
                rejection = "zero_shares"
                detail = f"现金不足: {cash:.0f} < {cost:.0f}"
                write_record(sym, dt, close, entry_h, cash, n_pos,
                            pos_detail, risk_budget, sf_state, rejection, detail)
                continue

            cash -= cost
            positions.open(sym, entry_price=close, shares=shares, n_at_entry=n)
            buy_today[sym] = True

            rejection = "executed"
            write_record(sym, dt, close, entry_h, cash, positions.count,
                        pos_detail, risk_budget, sf_state, rejection, f"shares={shares}")

    # ── 输出 CSV ──
    out_path = OUT_DIR / "signal_rejection_trace.csv"
    if records:
        df_out = pd.DataFrame(records)
        df_out.to_csv(out_path, index=False, encoding="utf-8-sig")
        print(f"\nCSV 已保存: {out_path} ({len(df_out)} 行)")
    else:
        print("\n无记录输出")
        return

    # ── 汇总 ──
    print("\n=== 信号拒绝汇总 ===")
    header = f"{'symbol':<14} {'signals':>8} {'executed':>10} {'paused':>8} {'filter':>8} {'zero_sh':>8} {'single':>8} {'portfolio':>8} {'T+1':>6}"
    print(header)
    print("-" * len(header))
    for sym in SYMBOLS:
        sig_count = rejected_total[sym]
        d = rejected_detail[sym]
        print(f"{sym:<14} {sig_count:>8} {d['executed']:>10} {d['paused']:>8} "
              f"{d['signal_filter']:>8} {d['zero_shares']:>8} {d['risk_single_max']:>8} "
              f"{d['risk_portfolio_max']:>8} {d['t_plus_one']:>6}")

    total = sum(rejected_total.values())
    td = {k: sum(rejected_detail[s][k] for s in SYMBOLS) for k in rejected_detail[SYMBOLS[0]]}
    print(f"{'合计':<14} {total:>8} {td['executed']:>10} {td['paused']:>8} "
          f"{td['signal_filter']:>8} {td['zero_shares']:>8} {td['risk_single_max']:>8} "
          f"{td['risk_portfolio_max']:>8} {td['t_plus_one']:>6}")

    # 最终状态
    print(f"\n最终现金: {cash:.0f}")
    print(f"最终权益: {equity():.0f}")
    print(f"持仓数: {positions.count}")



if __name__ == "__main__":
    main()
