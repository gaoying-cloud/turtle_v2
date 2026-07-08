#!/usr/bin/env python
"""
海龟 V6.2 每日信号生成器。
每天 16:30 后运行（Tushare 日线数据约 16:00-17:00 更新），
输出次日操作清单。

用法:
    py scripts/daily_signal.py                     # T日16:30 出信号
    py scripts/daily_signal.py --settle ...         # 记录成交价（入场/全平）
    py scripts/daily_signal.py --settle-half ...    # 记录利润保护减半成交价
    py scripts/daily_signal.py --settle-add ...     # 记录加仓成交价
    py scripts/daily_signal.py --status             # 查看持仓

节奏:
    T日 16:30  py scripts/daily_signal.py                  → 看明天要做什么
    T+1日 9:30 按表手动下单，记下实际成交价
    T+1日 16:30 py scripts/daily_signal.py --settle ...    → 结算+出后天表
    (减半日)   py scripts/daily_signal.py --settle-half ...→ 减半结算（不关闭持仓）
    (加仓日)   py scripts/daily_signal.py --settle-add ... → 加仓结算

注意 ── 可选过滤未实现（与回测的差异）:
    以下回测中的可选过滤在 daily_signal.py 中未实现，当前配置均为关闭状态，
    不影响现有行为。若未来在 config 中开启，需同步实现：
      - use_55_filter       （55日过滤）
      - regime_filter       （MarketRegime 碎步市过滤）
      - use_hurst_filter    （Hurst 指数过滤）
      - use_rsi_filter      （RSI/布林带过滤）
      - use_trend_duration_filter  （趋势持续时间过滤）
      - min_confirmations   （投票式信号确认：成交量/K线/近期胜率）
      - entry_mode="dual"   （MA10金叉入场模式 → 对应 MA20 退出模式）
      - shortable_symbols   （空头信号）
"""

from __future__ import annotations
import sys, json, logging, warnings
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config_loader import load_config, get_trading_symbols
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

from src.turtle_core import TurtleSignals, calc_position_size

# ── 常量 ──
STATE_PATH = ROOT / "data" / "daily_state.json"
DATA_DIR = ROOT / "data" / "etf_daily"
MIN_UNIT = 100

CONFIG = load_config()
INITIAL_CASH = CONFIG["initial_cash"]
TURTLE = CONFIG["turtle"]
MAX_UNITS = TURTLE["max_units"]
RISK_PER_UNIT = TURTLE["risk_per_unit"]
STOP_MULT = float(TURTLE.get("stop_atr_multiple", 2.0))  # V5.16: 从配置读取，对齐策略核心
PYRAMID_STEP = float(TURTLE.get("pyramid_step", 2.0))    # V6.2: 加仓步长(N倍数)，对齐策略默认2.0
COMMISSION = CONFIG["commission_pct"]
SLIPPAGE = CONFIG["slippage_pct"]
ETFS = get_trading_symbols(CONFIG)

# V6.2: 风控阈值，与 config.risk 对齐
RISK = CONFIG.get("risk", {})
SINGLE_MAX_RISK = float(RISK.get("single_max_risk", 0.04))       # 单品种风险上限 4%
MAX_PORTFOLIO_RISK = float(RISK.get("max_portfolio_risk", 0.20)) # 全账户风险上限 20%

# S14: 品种权重倍率，与 config.weighting.weight_multipliers 对齐
WEIGHT_MULTIPLIERS = CONFIG.get("weighting", {}).get("weight_multipliers", {})


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
        "last_signal_date": "",          # V5.16: 用于跨日清空 buy_today
        "half_exit_events": [],
    }


def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
        # V5.16: 向后兼容旧 state（无 last_signal_date / shares_per_unit / half_exit_events）
        s.setdefault("last_signal_date", "")
        s.setdefault("half_exit_events", [])
        for pos in s.get("positions", []):
            if "shares_per_unit" not in pos:
                pos["shares_per_unit"] = pos.get("shares", 0)
        return s
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
                pass
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
    calcs = TurtleSignals(TURTLE)
    si = calcs.precompute_all(df["high"], df["low"], df["close"])
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
    """入场条件：close > 20日高点，且通过 SignalFilter（对齐 turtle_core.SignalFilter）。

    SignalFilter 规则：
        规则1: 该品种首个信号 → 无条件接受
        规则2: 同品种已持仓 → 拒绝（主流程已处理）
        规则3: 上次同品种交易亏损 → 跳过下一次入场
        规则4: 连续拒绝 ≥ 3 次 → 强制放行
    """
    entry_high = signals.get("entry_high_20")
    if entry_high is None or close <= entry_high:
        return False

    # T+1 约束：当日已买入同品种，不可再入场
    if state.get("buy_today", {}).get(symbol, False):
        return False

    # ── SignalFilter ──
    sf = _get_sf(state, symbol)
    last_was_win = sf.get("last_was_win")

    if last_was_win is None:
        # 规则1：首个信号，无条件接受
        return True

    if last_was_win:
        # 规则3：上次盈利出场 → 接受
        return True

    # 上次亏损出场 → 递增连续拒绝计数
    sf["rejections"] = sf.get("rejections", 0) + 1

    if sf["rejections"] >= 3:
        # 规则4：连续拒绝 ≥ 3 → 强制放行（不归零计数器）
        # 归零在 record_trade_result 中做（真正成交时）。
        # 若 calc_shares 因现金不足返回 0，入场失败但计数器保持 ≥3，
        # 下次信号继续放行，直到真正入场。
        return True

    # 规则3：拒绝本次入场
    return False


def check_exit(symbol: str, low: float, close: float, signals: dict, state: dict, pos: dict) -> str:
    """退出检查：返回 'full'（清仓）/ 'half'（减半）/ 'none'（不退出）。"""
    if state.get("buy_today", {}).get(symbol, False):
        return "none"

    half_closed = pos.get("half_closed", False)
    # 退出线始终用 10 日低点
    stop_period = "stop_low_10"
    stop_low = signals.get(stop_period)
    n_at_entry = pos.get("n_at_entry")
    entry_price = pos.get("entry_price", 0)
    high_since_entry = pos.get("high_since_entry", entry_price or 0)

    # ── 最终清仓：low < 10日低点 ──
    if stop_low is not None and low < stop_low:
        return "full"

    # ── 利润保护减半仓 ──
    if not half_closed and n_at_entry and n_at_entry > 0:

        # ★★★ 修复 Bug 2：增加永久标记，用 high 判断，改门槛为 19N ★★★
        if not pos.get("protection_activated", False):
            peak_profit_n = (high_since_entry - entry_price) / n_at_entry
            if peak_profit_n >= 19.0:  # 这里的 19.0 对应你的神级回测参数.不用20N，因为20N保护利润门槛，会有有好几笔交易都是，激活和减半发生在同一根K线。 会造成比较大的滑点。
                pos["protection_activated"] = True
                print(f"  [信号] {symbol} 利润保护激活 (最高={high_since_entry:.3f}, 浮盈={peak_profit_n:.1f}N)")

        # 一旦激活，永久检查 2N 回撤
        if pos.get("protection_activated", False):
            trigger = max(entry_price, high_since_entry - 2 * n_at_entry)
            if low <= trigger:
                return "half"

    return "none"



def should_add(symbol: str, close: float, n: float, pos: dict) -> bool:
    """加仓条件：价格 >= entry_price + units * PYRAMID_STEP * n_at_entry（对齐策略核心）。"""
    if pos["units"] >= MAX_UNITS:
        return False
    threshold = pos["entry_price"] + pos["units"] * PYRAMID_STEP * pos["n_at_entry"]
    return close >= threshold


# ════════════════════════════════════════════════════════════
#  仓位计算
# ════════════════════════════════════════════════════════════

def market_equity(state: dict, data_cache: dict | None = None) -> float:
    """账户总权益 = 现金 + 持仓市值。

    data_cache 为 None 时退化为"按持仓 entry_price 估值"（settle 早期阶段，
    数据尚未加载）。有 data_cache 时按最新收盘价估值（run() 主流程）。
    """
    pos_value = 0.0
    for p in state["positions"]:
        if data_cache and p["code"] in data_cache:
            px = float(data_cache[p["code"]]["close"].iloc[-1])
        else:
            px = float(p["entry_price"])
        pos_value += p["shares"] * px
    return state["cash"] + pos_value


def calc_fade() -> float:
    """集中度衰减系数，与 turtle_trading.py 的 fade_table 对齐。"""
    n_pos = len([p for p in load_state().get("positions", [])])
    n_total = len(ETFS)
    if n_total >= 7:
        tbl = {0: 1.0, 1: 1.0, 2: 1.0, 3: 1.0, 4: 0.8, 5: 0.6, 6: 0.5}
    elif n_total >= 6:
        tbl = {0: 1.0, 1: 1.0, 2: 1.0, 3: 0.85, 4: 0.7, 5: 0.6, 6: 0.5}
    else:
        tbl = {0: 1.0, 1: 1.0, 2: 1.0, 3: 0.8, 4: 0.6}
    return tbl.get(n_pos, 0.5)


def calc_shares(equity: float, n: float, price: float, max_cash: float | None = None,
                fade: float = 1.0, weight_mult: float = 1.0) -> int:
    """按 equity 计算 1% 风险预算股数。

    equity : 账户总权益（sizing 基数，多笔入场共享同一基数）。
    n      : 当前 ATR(N)。
    price  : 入场价（仅用于现金上限判断，不影响 sizing 公式）。
    max_cash : 可选，本笔最多占用的现金。用于 cash < equity 时按
               先到先得（配置顺序）兜底，避免现金不足时超买。None 表示不限制。
    fade   : 集中度衰减系数，默认 1.0（无衰减）。
    weight_mult : 品种权重倍率，默认 1.0（无偏置）。从 config.weighting.weight_multipliers 读取。
    """
    if n is None or n <= 0 or price is None or price <= 0:
        return 0
    raw = calc_position_size(
        equity=equity,
        n_value=n,
        price=price,
        risk_pct=RISK_PER_UNIT * fade * weight_mult,  # V6.2: 集中度衰减; S14: 权重倍率
        stop_mult=STOP_MULT,
    )
    shares = max(MIN_UNIT, int(raw // MIN_UNIT) * MIN_UNIT)
    # 现金上限兜底：cash 不足时按比例缩到可用现金买得起的整手数
    if max_cash is not None and max_cash < shares * price:
        lots = int(max_cash // (price * MIN_UNIT))
        shares = max(0, lots * MIN_UNIT)
    return shares


def _check_risk_limits(
    shares: int, n: float, price: float, equity: float,
    state: dict, sym: str,
) -> int:
    """校验单品种 + 全账户风险上限，超限时缩仓。

    对齐 turtle_trading.py 的 P0 校验（single_max_risk / max_portfolio_risk）。
    返回缩仓后的股数，0 表示完全拒绝。
    """
    if shares <= 0 or n is None or n <= 0:
        return 0
    per_share_risk = STOP_MULT * n
    requested_risk = shares * per_share_risk

    # 已有该品种的风险敞口
    existing_sym_risk = 0.0
    for p in state.get("positions", []):
        if p["code"] == sym:
            existing_sym_risk += p["shares"] * STOP_MULT * p["n_at_entry"]

    # 全账户已有风险敞口
    total_existing_risk = sum(
        p["shares"] * STOP_MULT * p["n_at_entry"]
        for p in state.get("positions", [])
    )

    # ① 单品种风险 ≤ SINGLE_MAX_RISK
    new_sym_risk_pct = (existing_sym_risk + requested_risk) / equity if equity > 0 else 0
    if new_sym_risk_pct > SINGLE_MAX_RISK:
        max_new = equity * SINGLE_MAX_RISK - existing_sym_risk
        adjusted = int(max_new / per_share_risk / MIN_UNIT) * MIN_UNIT
        if adjusted <= 0:
            return 0
        shares = adjusted
        requested_risk = shares * per_share_risk

    # ② 全账户风险 ≤ MAX_PORTFOLIO_RISK
    new_total_risk_pct = (total_existing_risk + requested_risk) / equity if equity > 0 else 0
    if new_total_risk_pct > MAX_PORTFOLIO_RISK:
        max_new = equity * MAX_PORTFOLIO_RISK - total_existing_risk
        adjusted = int(max_new / per_share_risk / MIN_UNIT) * MIN_UNIT
        if adjusted <= 0:
            return 0
        shares = adjusted

    return shares


# ════════════════════════════════════════════════════════════
#  信号过滤
# ════════════════════════════════════════════════════════════

def _get_sf(state: dict, sym: str) -> dict:
    if sym not in state["signal_filter"]:
        state["signal_filter"][sym] = {"rejections": 0, "last_was_win": None}
    return state["signal_filter"][sym]


def record_trade_result(state: dict, sym: str, was_win: bool):
    """记录一笔交易结果，更新 SignalFilter 状态（对齐 turtle_core.SignalFilter.record_result）。

    重置连续拒绝计数；last_was_win 下次入场时用于规则3判断。
    """
    sf = _get_sf(state, sym)
    sf["last_was_win"] = was_win
    sf["rejections"] = 0


# ════════════════════════════════════════════════════════════
#  主流程
# ════════════════════════════════════════════════════════════

def run():
    today = date.today()
    today_str = today.isoformat()
    state = load_state()

    # ── V5.16: 跨日清空 buy_today（对齐策略核心 _is_new_day + _buy_today.clear） ──
    if state.get("last_signal_date", "") != today_str:
        state["buy_today"] = {}
        state["last_signal_date"] = today_str

    # 拉取最新数据
    print(f"\n{'=' * 58}")
    print(f"  海龟 V6.2 每日信号   {today_str} ({today.strftime('%A')})")
    print(f"{'=' * 58}")

    # 数据新鲜度检查
    print()
    fresh_date = check_data_freshness()
    if fresh_date is None:
        print("  [ERROR] 无可用数据，请先运行 py scripts/pull_data.py")
        return

    # 加载数据并计算信号（先全部加载，再对齐到公共日期，避免索引错位）
    data_cache = {}
    sig_cache = {}
    raw_dfs = {}
    for sym in ETFS:
        df = get_data_for(sym)
        if df is None or len(df) < 100:
            continue
        df = df[df["date"] <= pd.Timestamp(today)].reset_index(drop=True)
        if len(df) < 100:
            continue
        raw_dfs[sym] = df

    if raw_dfs:
        # 对齐到公共日期
        all_dates = sorted(set.union(*(set(df["date"].dropna()) for df in raw_dfs.values())))
        all_dates = pd.DatetimeIndex(all_dates)
        for sym in ETFS:
            if sym not in raw_dfs:
                continue
            df = raw_dfs[sym].set_index("date").reindex(all_dates)
            df = df.ffill().bfill()
            df = df.reset_index().rename(columns={"index": "date"})
            data_cache[sym] = df
            sig_cache[sym] = compute_signals(df)

    if not data_cache:
        print("\n  [ERROR] 无可用数据，请先运行 py scripts/pull_data.py 拉取数据")
        return

    # ── V5.17: 入场前先确定 sizing 基数 = equity（账户总权益）──
    # 多笔入场共享同一 equity 基数（对齐 turtle_core：1% 风险基于净值，
    # 不是基于"买入后剩余的现金"）。同时用 sim_cash 模拟多笔入场的现金
    # 扣减，作为先到先得（配置顺序）的现金上限兜底。
    sizing_equity = market_equity(state, data_cache)
    sim_cash = state["cash"]

    # ══════════════════════════════════════════════════════
    # 退出检查（仅记录信号，不自动执行）
    # ══════════════════════════════════════════════════════
    exit_signals = []
    for pos in state["positions"]:
        sym = pos["code"]
        if sym not in data_cache:
            continue
        df = data_cache[sym]
        sig = sig_cache[sym]
        low = df["low"].iloc[-1]
        close = df["close"].iloc[-1]

    # ★★★ 修复 Bug 1：更新持仓期最高价 ★★★
        high_today = df["high"].iloc[-1]
        if high_today > pos.get("high_since_entry", 0):
            pos["high_since_entry"] = high_today

        exit_type = check_exit(sym, low, close, sig, state, pos)
    # ... 后续代码不变

        if exit_type == "full":
            exit_signals.append({
                "code": sym, "type": "full",
                "shares": pos["shares"],
                "entry_price": pos["entry_price"],
                "exit_price": float(close),
            })
        elif exit_type == "half":
            half_shares = pos["shares"] // 2
            exit_signals.append({
                "code": sym, "type": "half",
                "shares": half_shares,
                "entry_price": pos["entry_price"],
                "exit_price": float(close),
            })
            # ★★★ 减半闭环：更新持仓状态，标记 half_closed，记录事件 ★★★
            pos["half_closed"] = True
            pos["shares"] -= half_shares
            pnl_half = (float(close) - pos["entry_price"]) * half_shares
            state.setdefault("half_exit_events", []).append({
                "symbol": sym, "direction": "long",
                "entry_date": pos["entry_date"], "exit_date": today_str,
                "entry_price": pos["entry_price"], "exit_price": float(close),
                "shares_exited": half_shares, "shares_remaining": pos["shares"],
                "pnl": round(pnl_half, 2), "was_win": pnl_half > 0,
                "holding_days": (today - datetime.strptime(pos["entry_date"], "%Y-%m-%d").date()).days,
            })

    # ══════════════════════════════════════════════════════
    # 加仓检查（仅对剩余持仓，跳过已触发退出信号的）
    # ══════════════════════════════════════════════════════
    add_actions = []
    exiting_symbols = {e["code"] for e in exit_signals}
    for pos in state["positions"]:
        sym = pos["code"]
        if sym in exiting_symbols or sym not in data_cache:
            continue
        df = data_cache[sym]
        sig = sig_cache[sym]
        n = sig.get("n")
        if n is None or n <= 0:
            continue
        close = df["close"].iloc[-1]
        if should_add(sym, close, n, pos):
            # V5.16: 加仓股数复用初始 shares_per_unit（对齐策略核心 pos.shares_per_unit）
            shares = pos.get("shares_per_unit", pos["shares"])
            if shares <= 0:
                continue
            # 更新持仓（n_at_entry 保持不变，对齐策略核心）
            pos["shares"] += shares
            pos["units"] += 1
            # T+1 标记
            state["buy_today"][sym] = True
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
            # V5.17: sizing 基于 equity（多笔入场共享同一基数），
            #        sim_cash 作为现金上限兜底（配置顺序先到先得）。
            # V6.2: 集中度衰减
            _fade = calc_fade()
            _weight_mult = WEIGHT_MULTIPLIERS.get(sym, 1.0)
            shares = calc_shares(sizing_equity, n, close, max_cash=sim_cash, fade=_fade, weight_mult=_weight_mult)
            if shares <= 0:
                continue
            # V6.2: 单品种 + 全账户风控校验
            shares = _check_risk_limits(shares, n, close, sizing_equity, state, sym)
            if shares <= 0:
                continue
            sim_cash -= shares * close          # 模拟本笔占用，供后续入场兜底
            entry_high = sig.get("entry_high_20", close)
            stop_low = sig.get("stop_low_10", close * 0.9)
            pos = {
                "code": sym,
                "direction": "long",
                "entry_date": today_str,
                "entry_price": float(close),
                "shares": shares,
                "shares_per_unit": shares,      # V5.16: 初始单位股数（加仓复用）
                "units": 1,
                "n_at_entry": float(n),
                "trail_high": float(close),
                "high_since_entry": float(close),  # 利润保护用
                "half_closed": False,               # 利润保护用
                "protection_activated": False,      # ★ 新增初始化
            }
            entry_actions.append(pos)

    # ── V5.17: equity 由 market_equity 统一计算（持仓按最新收盘价估值）──
    state["equity"] = market_equity(state, data_cache)

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

        if pos:
            # 检查是否触发了退出信号
            exit_info = next((e for e in exit_signals if e["code"] == sym), None)
            if exit_info:
                showing_any = True
                pnl_est = (exit_info["exit_price"] - exit_info["entry_price"]) * exit_info["shares"]
                if exit_info["type"] == "half":
                    note = f"利润保护回撤2N PnL≈{pnl_est:+.0f}"
                else:
                    note = f"10日低点突破 PnL≈{pnl_est:+.0f}"
                print(line_fmt.format(sym, pos_str, "平仓", "卖出",
                       f"{exit_info['shares']}股",
                       f"{exit_info['entry_price']:.3f}",
                       f"{exit_info['exit_price']:.3f}",
                       note))
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
            if pos.get("half_closed"):
                notes = "已减半仓"
            elif holding_days > 12:
                notes = "别动"
            else:
                notes = ""
            stop_val = sig.get("stop_low_10")
            showing_any = True
            print(line_fmt.format(sym, pos_str, "持有", "—", "—",
                   f"{pos['entry_price']:.3f}",
                   f"{stop_val:.2f}" if stop_val else "—",
                   notes))

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
                    if sf.get("rejections", 0) > 0 and sf.get("last_was_win") is False:
                        reason += f"  SignalFilter拒绝({sf['rejections']}/3)"
                if not any(True for _ in state["positions"]) and not entry_actions:
                    showing_any = True
                    print(line_fmt.format(sym, "空仓", "—", "—", "—", "—", "—", reason))

    if not showing_any:
        print(f"  {'(无信号)':^66}")
    print("  " + "-" * 66)

    # ── 利润保护减半事件摘要 ──
    today_halves = [ev for ev in state.get("half_exit_events", []) if ev.get("exit_date") == today_str]
    if today_halves:
        print()
        print("  利润保护减半事件:")
        print("  {:<12} {:>8} {:>8} {:>10} {:>8}".format(
            "品种", "入场价", "减半价", "PnL", "剩余股数"))
        print("  " + "-" * 54)
        for ev in today_halves:
            print("  {:<12} {:>8.3f} {:>8.3f} {:>+10.0f} {:>8}".format(
                ev["symbol"], ev["entry_price"], ev["exit_price"],
                ev["pnl"], ev["shares_remaining"]))
        print("  " + "-" * 54)

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

    # V5.16: 保存 state（SignalFilter 连续拒绝计数需跨 run() 持久化）
    save_state(state)


def cmd_settle(state: dict, settle_str: str):
    """记录实际成交价，确认入场。未持仓的符号会自动创建持仓。"""
    today_str = date.today().isoformat()
    # V5.17: sizing 基数 = equity（账户总权益），多笔入场共享同一基数，
    #        与 run() 预览一致。settle 早期无 data_cache，按 entry_price 估值。
    sizing_equity = market_equity(state)
    for part in settle_str.split(","):
        part = part.strip()
        if "=" not in part:
            print(f"  ⚠ 格式错误: {part}，应为 symbol=price")
            continue
        sym, price_str = part.split("=", 1)
        sym = sym.strip().upper()
        if not sym.endswith((".SH", ".SZ")):
            matched = [c for c in ETFS if c.startswith(sym + ".")]
            sym = matched[0] if matched else sym + ".SH"
        try:
            price = float(price_str)
        except ValueError:
            print(f"  ⚠ 价格格式错误: {price_str}")
            continue

        pos = next((p for p in state["positions"] if p["code"] == sym), None)
        if pos:
            # 已有持仓 → 平仓
            exit_price = price
            pnl = (exit_price - pos["entry_price"]) * pos["shares"]
            fee = pos["shares"] * exit_price * (COMMISSION * 2 + SLIPPAGE)
            net_pnl = pnl - fee
            was_win = net_pnl > 0
            record_trade_result(state, sym, was_win)
            cl = state["consecutive_losses"].get(sym, 0)
            if was_win:
                state["consecutive_losses"][sym] = 0
            else:
                state["consecutive_losses"][sym] = cl + 1
            state["cash"] += pos["shares"] * exit_price * (1 - COMMISSION - SLIPPAGE)
            state["trade_history"].append({
                "symbol": sym, "direction": "long",
                "entry_date": pos["entry_date"], "exit_date": today_str,
                "entry_price": pos["entry_price"], "exit_price": exit_price,
                "shares": pos["shares"], "pnl": round(net_pnl, 2), "was_win": was_win,
            })
            state["positions"].remove(pos)
            # V5.17: equity 统一由 market_equity 计算（此处平仓后无持仓，等于 cash）
            state["equity"] = market_equity(state)
            print(f"  [OK] {sym} 平仓: {pos['shares']}股 @ {exit_price:.3f} PnL={net_pnl:+,.0f}")
        else:
            df = get_data_for(sym)
            if df is None or len(df) < 100:
                print(f"  [WARN] {sym} 无足够数据，无法入场")
                continue
            df = df[df["date"] <= pd.Timestamp(date.today())].reset_index(drop=True)
            if len(df) < 100:
                print(f"  [WARN] {sym} 截断后数据不足，无法入场")
                continue
            sig = compute_signals(df)
            n = sig.get("n")
            if n is None or n <= 0:
                print(f"  [WARN] {sym} ATR 计算异常，无法入场")
                continue
            _weight_mult = WEIGHT_MULTIPLIERS.get(sym, 1.0)
            shares = calc_shares(sizing_equity, n, price, max_cash=state["cash"], fade=calc_fade(), weight_mult=_weight_mult)
            if shares <= 0:
                print(f"  [WARN] {sym} 可买股数为 0，无法入场")
                continue
            # V6.2: 单品种 + 全账户风控校验
            shares = _check_risk_limits(shares, n, price, sizing_equity, state, sym)
            if shares <= 0:
                print(f"  [WARN] {sym} 风控校验未通过，无法入场")
                continue
            pos = {
                "code": sym,
                "direction": "long",
                "entry_date": today_str,
                "entry_price": price,
                "shares": shares,
                "shares_per_unit": shares,      # V5.16: 初始单位股数
                "units": 1,
                "n_at_entry": float(n),
                "trail_high": price,
                "high_since_entry": price,           # 利润保护用
                "half_closed": False,                # 利润保护用
                "protection_activated": False,       # ★ 新增初始化
            }
            state["positions"].append(pos)
            state["cash"] -= shares * price
            # V5.17: equity 统一由 market_equity 计算（刚入场按 entry_price 估值）
            state["equity"] = market_equity(state)
            state["buy_today"][sym] = True
            _get_sf(state, sym)["rejections"] = 0
            print(f"  [OK] {sym} 确认入场: {shares}股 @ {price:.3f} (N={n:.4f})")

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


def cmd_settle_half(state: dict, settle_str: str):
    """记录利润保护减半成交价（平掉一半仓位，不关闭持仓）。"""
    today_str = date.today().isoformat()
    for part in settle_str.split(","):
        part = part.strip()
        if "=" not in part:
            print(f"  ⚠ 格式错误: {part}，应为 symbol=price")
            continue
        sym, price_str = part.split("=", 1)
        sym = sym.strip().upper()
        if not sym.endswith((".SH", ".SZ")):
            matched = [c for c in ETFS if c.startswith(sym + ".")]
            sym = matched[0] if matched else sym + ".SH"
        try:
            price = float(price_str)
        except ValueError:
            print(f"  ⚠ 价格格式错误: {price_str}")
            continue

        pos = next((p for p in state["positions"] if p["code"] == sym), None)
        if pos is None:
            print(f"  [WARN] {sym} 无持仓，无法减半")
            continue
        if pos.get("half_closed", False):
            print(f"  [WARN] {sym} 已减半过，不能再次减半")
            continue

        half_shares = pos["shares"] // 2
        if half_shares <= 0:
            print(f"  [WARN] {sym} 持仓不足，无法减半")
            continue

        exit_price = price
        pnl = (exit_price - pos["entry_price"]) * half_shares
        fee = half_shares * exit_price * (COMMISSION * 2 + SLIPPAGE)
        net_pnl = pnl - fee

        state["cash"] += half_shares * exit_price * (1 - COMMISSION - SLIPPAGE)
        pos["shares"] -= half_shares
        pos["half_closed"] = True

        state.setdefault("half_exit_events", []).append({
            "symbol": sym, "direction": "long",
            "entry_date": pos["entry_date"], "exit_date": today_str,
            "entry_price": pos["entry_price"], "exit_price": exit_price,
            "shares_exited": half_shares, "shares_remaining": pos["shares"],
            "pnl": round(net_pnl, 2), "was_win": net_pnl > 0,
            "holding_days": (date.today() - datetime.strptime(pos["entry_date"], "%Y-%m-%d").date()).days,
        })

        state["equity"] = market_equity(state)
        print(f"  [OK] {sym} 减半: {half_shares}股 @ {exit_price:.3f} PnL={net_pnl:+,.0f} 剩余{pos['shares']}股")

    save_state(state)


def cmd_settle_add(state: dict, settle_str: str):
    """记录加仓成交价（追加一个单位，不改变入场价）。"""
    today_str = date.today().isoformat()
    for part in settle_str.split(","):
        part = part.strip()
        if "=" not in part:
            print(f"  ⚠ 格式错误: {part}，应为 symbol=price")
            continue
        sym, price_str = part.split("=", 1)
        sym = sym.strip().upper()
        if not sym.endswith((".SH", ".SZ")):
            matched = [c for c in ETFS if c.startswith(sym + ".")]
            sym = matched[0] if matched else sym + ".SH"
        try:
            price = float(price_str)
        except ValueError:
            print(f"  ⚠ 价格格式错误: {price_str}")
            continue

        pos = next((p for p in state["positions"] if p["code"] == sym), None)
        if pos is None:
            print(f"  [WARN] {sym} 无持仓，无法加仓")
            continue
        if pos["units"] >= MAX_UNITS:
            print(f"  [WARN] {sym} 已达最大单位数 {MAX_UNITS}，无法加仓")
            continue

        add_shares = pos.get("shares_per_unit", pos["shares"])
        if add_shares <= 0:
            print(f"  [WARN] {sym} 单位股数异常，无法加仓")
            continue

        pos["shares"] += add_shares
        pos["units"] += 1
        state["cash"] -= add_shares * price
        state["buy_today"][sym] = True

        state["equity"] = market_equity(state)
        print(f"  [OK] {sym} 加仓: +{add_shares}股 @ {price:.3f} (单位{pos['units']}/{MAX_UNITS})")

    save_state(state)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="海龟 V6.2 每日信号")
    parser.add_argument("--settle", type=str, default=None,
                        help="记录成交价: symbol=price[,symbol=price...]")
    parser.add_argument("--settle-half", type=str, default=None,
                        help="记录利润保护减半成交价: symbol=price[,symbol=price...]")
    parser.add_argument("--settle-add", type=str, default=None,
                        help="记录加仓成交价: symbol=price[,symbol=price...]")
    parser.add_argument("--status", action="store_true", help="查看持仓状态")
    args = parser.parse_args()

    if args.settle:
        state = load_state()
        cmd_settle(state, args.settle)
    elif args.settle_half:
        state = load_state()
        cmd_settle_half(state, args.settle_half)
    elif args.settle_add:
        state = load_state()
        cmd_settle_add(state, args.settle_add)
    elif args.status:
        state = load_state()
        cmd_status(state)
    else:
        run()
