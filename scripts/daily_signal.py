#!/usr/bin/env python
"""
海龟 V5.15 每日信号生成器。
每天 16:30 后运行（Tushare 日线数据约 16:00-17:00 更新），
输出次日操作清单。

用法:
    py scripts/daily_signal.py               # T日16:30 出信号
    py scripts/daily_signal.py --settle ...   # 记录成交价
    py scripts/daily_signal.py --status       # 查看持仓

节奏:
    T日 16:30  py scripts/daily_signal.py    → 看明天要做什么
    T+1日 9:30 按表手动下单，记下实际成交价
    T+1日 16:30 py scripts/daily_signal.py --settle 510500=9.05  → 结算+出后天表
"""
from __future__ import annotations
import sys, json, logging, warnings, subprocess
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

from src.turtle_core import TurtleSignals, calc_position_size

# ── 常量 ──
CONFIG_PATH = ROOT / "config" / "turtle_config.yaml"
STATE_PATH = ROOT / "data" / "daily_state.json"
DATA_DIR = ROOT / "data" / "etf_daily"
ETFS = ["510500.SH", "159915.SZ", "513100.SH", "518880.SH"]
T_PLUS_ONE = {"510500.SH", "159915.SZ"}  # T+1
T0_SYMBOLS = {"513100.SH", "518880.SH"}  # T+0
MIN_UNIT = 100
MAX_UNITS = 4
RISK_PER_UNIT = 0.01

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)
INITIAL_CASH = CONFIG["initial_cash"]


# ════════════════════════════════════════════════════════════
#  状态管理
# ════════════════════════════════════════════════════════════

def _default_state() -> dict:
    return {
        "equity": INITIAL_CASH,
        "cash": INITIAL_CASH,
        "positions": [],
        "trade_history": [],
        "signal_filter": {},
        "consecutive_losses": {},
        "buy_today": {},
    }


def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    s = _default_state()
    save_state(s)
    return s


def save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ════════════════════════════════════════════════════════════
#  数据与信号
# ════════════════════════════════════════════════════════════

def get_data_for(symbol: str) -> pd.DataFrame | None:
    """读取本地 parquet 数据."""
    path = DATA_DIR / f"{symbol}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def check_data_freshness() -> date | None:
    """检查 4 只 ETF 的缓存最新日期，打印新鲜度警告。返回最早的最新日期，或 None 表示无数据。"""
    today = date.today()
    latest_dates = []
    missing_any = False
    for sym in ETFS:
        path = DATA_DIR / f"{sym}.parquet"
        if not path.exists():
            print(f"  [WARN] {sym}: 缓存文件不存在，请先运行 py scripts/pull_data.py")
            missing_any = True
            continue
        try:
            df = pd.read_parquet(path)
            df["date"] = pd.to_datetime(df["date"])
            latest = df["date"].max().date()
            latest_dates.append(latest)
            if latest < today:
                pass  # 统一在下面打印
        except Exception as e:
            print(f"  [ERROR] {sym}: 数据文件读取失败 ({e})")
            missing_any = True

    if not latest_dates:
        return None

    common_latest = min(latest_dates)
    weekday = today.weekday()

    if common_latest == today:
        print(f"  [OK] 数据最新: {today}")
    elif common_latest < today and weekday < 5:
        print(f"  [WARN] 缓存最新日期={common_latest}，今日数据尚未更新")
        print(f"         信号基于 {common_latest} 数据生成，数据通常 16:30 后可用")
    else:
        print(f"  [OK] 使用最近交易日数据: {common_latest} (今日{'非交易日' if weekday >= 5 else '数据未更新'})")

    if missing_any:
        print("  [HINT] 运行 py scripts/pull_data.py 补拉缺失数据")
        return None

    return min(latest_dates)


def compute_signals(df: pd.DataFrame) -> dict:
    """用 TurtleSignals 计算信号序列，返回最新一行指标."""
    turtle_params = {
        "atr_period": 15, "breakout_period": 20,
        "stop_period": 12, "exit_period": 10,
        "risk_per_unit": 0.01, "max_units": 5, "unit_step": 1,
    }
    calcs = TurtleSignals(turtle_params)
    si = calcs.precompute_all(df["high"], df["low"], df["close"])
    # 提取最末值
    last = {}
    for k, s in si.items():
        if isinstance(s, pd.Series):
            v = s.iloc[-1]
            last[k] = None if pd.isna(v) else float(v)
        else:
            last[k] = s
    return last


# ════════════════════════════════════════════════════════════
#  核心判断
# ════════════════════════════════════════════════════════════

def should_enter(symbol: str, close: float, signals: dict, state: dict, today: date) -> bool:
    """入场条件：close > 20日高点，且不受 SignalFilter 阻挡."""
    entry_high = signals.get("entry_high_20")
    if entry_high is None or close <= entry_high:
        return False

    # T+1 今天已买过
    if state.get("buy_today", {}).get(symbol, False):
        return False

    # SignalFilter
    sf = state["signal_filter"].get(symbol, {"rejections": 0, "last_was_win": None})
    if sf["rejections"] >= 3:
        return False

    return True


def should_exit(symbol: str, low: float, signals: dict) -> bool:
    """退出条件：最低价 <= 10日低点."""
    stop_low = signals.get("stop_low_10")
    if stop_low is None:
        return False
    return low <= stop_low


def should_add(symbol: str, close: float, n: float, pos: dict) -> bool:
    """加仓条件：价格 >= entry_price + units * 0.5 * N_at_entry."""
    if pos["units"] >= MAX_UNITS:
        return False
    threshold = pos["entry_price"] + pos["units"] * 0.5 * pos["n_at_entry"]
    return close >= threshold


# ════════════════════════════════════════════════════════════
#  仓位计算
# ════════════════════════════════════════════════════════════

def calc_shares(equity: float, n: float, price: float) -> int:
    if n is None or n <= 0 or price is None or price <= 0:
        return 0
    raw = calc_position_size(
        equity=equity,
        n_value=n,
        price=price,
        risk_pct=RISK_PER_UNIT,
        stop_mult=1.0,
    )
    return max(MIN_UNIT, int(raw // MIN_UNIT) * MIN_UNIT)


# ════════════════════════════════════════════════════════════
#  信号过滤
# ════════════════════════════════════════════════════════════

def _get_sf(state: dict, sym: str) -> dict:
    if sym not in state["signal_filter"]:
        state["signal_filter"][sym] = {"rejections": 0, "last_was_win": None}
    return state["signal_filter"][sym]


def record_entry_rejection(state: dict, sym: str):
    sf = _get_sf(state, sym)
    sf["rejections"] += 1


def record_trade_result(state: dict, sym: str, was_win: bool):
    sf = _get_sf(state, sym)
    if was_win:
        sf["rejections"] = 0
    else:
        sf["rejections"] += 1
    sf["last_was_win"] = was_win


# ════════════════════════════════════════════════════════════
#  主流程
# ════════════════════════════════════════════════════════════

def run():
    today = date.today()
    today_str = today.isoformat()
    state = load_state()

    # 重置 buy_today
    state["buy_today"] = {}

    # 拉取最新数据
    print(f"\n{'=' * 58}")
    print(f"  海龟 V5.15 每日信号   {today_str} ({today.strftime('%A')})")
    print(f"{'=' * 58}")

    # 数据新鲜度检查
    print()
    fresh_date = check_data_freshness()
    if fresh_date is None:
        print("  [ERROR] 无可用数据，请先运行 py scripts/pull_data.py")
        return

    # 加载数据并计算信号
    data_cache = {}
    sig_cache = {}
    for sym in ETFS:
        df = get_data_for(sym)
        if df is None or len(df) < 100:
            continue
        data_cache[sym] = df
        sig_cache[sym] = compute_signals(df)

    if not data_cache:
        print("\n  [ERROR] 无可用数据，请先运行 py scripts/pull_data.py 拉取数据")
        return

    # ══════════════════════════════════════════════════════
    # 退出检查
    # ══════════════════════════════════════════════════════
    closed_positions = []
    for pos in state["positions"]:
        sym = pos["code"]
        if sym not in data_cache:
            continue
        df = data_cache[sym]
        sig = sig_cache[sym]
        low = df["low"].iloc[-1]
        if should_exit(sym, low, sig):
            close_price = df["close"].iloc[-1]
            pnl = (close_price - pos["entry_price"]) * pos["shares"]
            was_win = pnl > 0
            # 记录
            record_trade_result(state, sym, was_win)
            cl = state["consecutive_losses"].get(sym, 0)
            if was_win:
                state["consecutive_losses"][sym] = 0
            else:
                state["consecutive_losses"][sym] = cl + 1
            state["equity"] += pnl
            state["trade_history"].append({
                "symbol": sym, "direction": "long",
                "entry_date": pos["entry_date"], "exit_date": today_str,
                "entry_price": pos["entry_price"], "exit_price": close_price,
                "shares": pos["shares"], "pnl": round(pnl, 2), "was_win": was_win,
            })
            closed_positions.append(pos)

    for cp in closed_positions:
        state["positions"].remove(cp)

    # ══════════════════════════════════════════════════════
    # 加仓检查（仅对剩余持仓）
    # ══════════════════════════════════════════════════════
    add_actions = []
    for pos in state["positions"]:
        sym = pos["code"]
        if sym not in data_cache:
            continue
        df = data_cache[sym]
        sig = sig_cache[sym]
        n = sig.get("n")
        if n is None or n <= 0:
            continue
        close = df["close"].iloc[-1]
        if should_add(sym, close, n, pos):
            shares = calc_shares(state["equity"], n, close)
            if shares <= 0:
                continue
            # 更新持仓
            pos["shares"] += shares
            pos["units"] += 1
            pos["n_at_entry"] = (pos["n_at_entry"] * (pos["units"] - 1) + n) / pos["units"]
            add_actions.append({"symbol": sym, "shares": shares, "units": pos["units"], "price": close})

    # ══════════════════════════════════════════════════════
    # 入场检查
    # ══════════════════════════════════════════════════════
    entry_actions = []
    for sym in ETFS:
        if sym not in data_cache:
            continue
        # 已有持仓
        if any(p["code"] == sym for p in state["positions"]):
            continue
        df = data_cache[sym]
        sig = sig_cache[sym]
        close = df["close"].iloc[-1]
        if should_enter(sym, close, sig, state, today):
            n = sig.get("n")
            if n is None or n <= 0:
                continue
            shares = calc_shares(state["equity"], n, close)
            if shares <= 0:
                continue
            entry_high = sig.get("entry_high_20", close)
            stop_low = sig.get("stop_low_10", close * 0.9)
            pos = {
                "code": sym,
                "direction": "long",
                "entry_date": today_str,
                "entry_price": float(close),
                "shares": shares,
                "units": 1,
                "n_at_entry": float(n),
                "trail_high": float(close),
            }
            state["positions"].append(pos)
            if sym in T_PLUS_ONE:
                state["buy_today"][sym] = True
            entry_actions.append(pos)
            # SignalFilter reset on entry
            _get_sf(state, sym)["rejections"] = 0

    # ══════════════════════════════════════════════════════
    # 打印输出
    # ══════════════════════════════════════════════════════
    print()
    line_fmt = "  {:<12} {:>8} {:<8} {:<6} {:>6} {:>8} {:>8} {}"
    print(line_fmt.format("品种", "持仓", "操作", "方向", "数量", "入场价", "止损", "备注"))
    print("  " + "-" * 66)

    showing_any = False
    for sym in ETFS:
        sig = sig_cache.get(sym)
        pos = next((p for p in state["positions"] if p["code"] == sym), None)
        pos_str = f"{pos['shares']}股" if pos else "空仓"
        holding_days = (today - datetime.strptime(pos["entry_date"], "%Y-%m-%d").date()).days if pos else 0
        notes = []

        # 有持仓
        if pos:
            # 检查是否触发了退出（已在上方处理，这里显示退出信号）
            if any(cp["code"] == sym for cp in closed_positions):
                showing_any = True
                print(line_fmt.format(sym, pos_str, "平仓", "卖出",
                       f"{pos['shares']}股",
                       f"{pos['entry_price']:.3f}",
                       f"{df['close'].iloc[-1]:.3f}", "10日低点突破"))
                continue

            # 加仓了？
            add_info = next((a for a in add_actions if a["symbol"] == sym), None)
            if add_info:
                showing_any = True
                print(line_fmt.format(sym, f"{pos['shares']}股", "加仓", "买入",
                       f"{add_info['shares']}股",
                       f"{add_info['price']:.3f}", "—", f"单位{add_info['units']}/{MAX_UNITS}"))
                continue

            # 持有中
            if holding_days > 12:
                notes.append("别动")
            stop_val = sig.get("stop_low_10")
            stop_s = f"{stop_val:.2f}" if stop_val else "—"
            cl = state["consecutive_losses"].get(sym, 0)
            trail = pos.get("trail_high", 0)
            trail_s = f"高{trail:.2f}" if trail else ""
            shown = False
            showing_any = True
            print(line_fmt.format(sym, pos_str, "持有", "—", "—",
                   f"{pos['entry_price']:.3f}",
                   f"{stop_val:.2f}" if stop_val else "—",
                   f"{'⚠️'+'别动' if holding_days>12 else ''}"))

        else:
            # 空仓—入场信号
            en = next((e for e in entry_actions if e["code"] == sym), None)
            if en:
                showing_any = True
                stop_low = sig.get("stop_low_10", 0)
                print(line_fmt.format(sym, "空仓", "买入", "做多",
                       f"{en['shares']}股",
                       f"{en['entry_price']:.3f}",
                       f"{stop_low:.2f}" if stop_low else "—", ""))
            else:
                reason = "无信号"
                if sig:
                    entry_high = sig.get("entry_high_20")
                    close = data_cache[sym]["close"].iloc[-1] if sym in data_cache else 0
                    if entry_high and close <= entry_high:
                        reason = f"未突破({close:.2f}<{entry_high:.2f})"
                    sf = _get_sf(state, sym)
                    if sf["rejections"] >= 3:
                        reason += "  SignalFilter暂停"
                if not any(True for _ in state["positions"]) and not entry_actions:
                    showing_any = True
                    print(line_fmt.format(sym, "空仓", "—", "—", "—", "—", "—", reason))

    if not showing_any:
        print(f"  {'(无信号)':^66}")
    print("  " + "-" * 66)

    # 权益摘要
    n_pos = len(state["positions"])
    risk = n_pos * RISK_PER_UNIT * 100
    warn = ""
    if n_pos >= 3:
        warn += " ⚠️仓位集中"
    for sym in ETFS:
        cl = state["consecutive_losses"].get(sym, 0)
        if cl >= 5:
            warn += f" ⚠️{sym}连亏{cl}次"
    print(f"\n  权益: {state['equity']:,.0f} | 持仓: {n_pos}/4 | 风险: {risk:.1f}%{warn}")
    print(f"  下次操作: 明日 9:30 按上表执行")
    print()

    # 保存状态
    save_state(state)


def cmd_settle(state: dict, settle_str: str):
    """记录实际成交价，更新持仓的 entry_price 为真实成交价。"""
    # 格式: symbol=price, 如 513100=22.38
    for part in settle_str.split(","):
        part = part.strip()
        if "=" not in part:
            print(f"  ⚠ 格式错误: {part}，应为 symbol=price")
            continue
        sym, price_str = part.split("=", 1)
        sym = sym.strip().upper()
        if not sym.endswith((".SH", ".SZ")):
            sym = sym + ".SH" if sym in ["510500", "513100", "518880"] else sym + ".SZ"
        try:
            price = float(price_str)
        except ValueError:
            print(f"  ⚠ 价格格式错误: {price_str}")
            continue
        # 在持仓中找该品种（查找最近一笔买入信号对应的空位）
        pos = next((p for p in state["positions"] if p["code"] == sym), None)
        if pos:
            old_price = pos["entry_price"]
            pos["entry_price"] = price
            print(f"  [OK] {sym} 成交价更新: {old_price:.3f} -> {price:.3f}")
        else:
            print(f"  [WARN] {sym} 不在持仓中，忽略")
    save_state(state)


def cmd_status(state: dict):
    """查看当前持仓状态."""
    print(f"\n{'=' * 55}")
    print(f"  持仓状态  ({date.today().isoformat()})")
    print(f"{'=' * 55}")
    if not state["positions"]:
        print("  空仓")
    for pos in state["positions"]:
        print(f"  {pos['code']}: {pos['shares']}股 @ {pos['entry_price']:.3f} ({pos['units']}/{MAX_UNITS}单位)")
    print(f"  权益: {state['equity']:,.0f} | 持仓: {len(state['positions'])}/4")
    if state["trade_history"]:
        last = state["trade_history"][-1]
        print(f"  最近交易: {last['symbol']} {'赚' if last['was_win'] else '亏'} PnL={last['pnl']:+,.0f}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="海龟 V5.15 每日信号")
    parser.add_argument("--settle", type=str, default=None,
                        help="记录成交价: symbol=price[,symbol=price...]")
    parser.add_argument("--status", action="store_true", help="查看持仓状态")
    args = parser.parse_args()

    if args.settle:
        state = load_state()
        cmd_settle(state, args.settle)
    elif args.status:
        state = load_state()
        cmd_status(state)
    else:
        run()
