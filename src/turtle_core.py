"""
跨市场ETF海龟组合策略 · 海龟核心模块 (S2)

从 automated_trading/src/strategy_engine.py 提取并重构。

设计原则：
    - 纯 Python/numpy/pandas，无 Backtrader 依赖
    - 无状态计算函数 + 有状态管理类 分离
    - 保留 20日/55日 双通道支持（55日过滤可选）
    - 接口级别预留做空和商品期货扩展

模块结构：
    无状态计算函数  ← 纯函数，无副作用
    TurtleSignals   ← 一次性预计算所有信号序列
    Position        ← 单品种持仓 dataclass
    TurtlePositions ← 多品种持仓管理器
    SignalFilter    ← 盈利过滤器
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════
#  信号确认工具函数（成交量 / K线形态 / 近期胜率）
# ════════════════════════════════════════════════════════════

def volume_confirmation(
    vol: float,
    vol_series: pd.Series,
    lookback: int = 20,
    threshold: float = 1.5,
) -> bool:
    """成交量确认：突破日成交量是否显著大于过去 N 日均量。

    Parameters
    ----------
    vol : float
        当日成交量。
    vol_series : pd.Series
        过去一段时间的成交量序列（含当日）。
    lookback : int
        均量计算窗口（不含当日），默认 20。
    threshold : float
        放量倍数阈值，默认 1.5（即 >1.5 倍均量）。

    Returns
    -------
    bool
        True 表示成交量确认通过。
    """
    if len(vol_series) < lookback + 1 or vol <= 0:
        return False
    avg_vol = vol_series.iloc[-(lookback + 1):-1].mean()
    if avg_vol <= 0:
        return False
    return (vol / avg_vol) >= threshold


def breakout_quality(
    open_: float, high: float, low: float, close: float,
    is_long: bool = True,
    min_body_ratio: float = 0.4,
) -> bool:
    """K 线形态确认：突破日的 K 线实体占比和收盘位置。

    多头突破条件：
        - 实体占比 > min_body_ratio（默认 40%，不是十字星）
        - 收盘价在当日区间上半部（>60% 位置）

    空头突破条件：
        - 实体占比 > min_body_ratio
        - 收盘价在当日区间下半部（<40% 位置）

    Returns
    -------
    bool
        True 表示 K 线形态确认通过。
    """
    body = abs(close - open_)
    candle_range = high - low
    if candle_range < 1e-10:
        return False

    body_ratio = body / candle_range
    close_position = (close - low) / candle_range  # 0=最低, 1=最高

    if body_ratio <= min_body_ratio:
        return False

    if is_long:
        return close_position > 0.6
    else:
        return close_position < 0.4


def recent_batting_avg(
    recent_trades: list,
    window: int = 4,
    max_loss_ratio: float = 0.75,
) -> bool:
    """近期胜率监控：近 N 笔中亏损占比未超标 → 放行。

    替代原 P2 累计亏损金额冻结的逻辑——不看亏多少，
    而看输的次数比例。趋势跟踪亏损正常，
    但连续亏损意味着该品种当下的趋势判断可能失效。

    Parameters
    ----------
    recent_trades : list[dict]
        最近 N 笔该品种的交易记录（含 pnl 字段）。
    window : int
        观察窗口，默认 8 笔。
    max_loss_ratio : float
        允许的最大亏损占比，默认 0.75（8 笔中最多 6 笔亏）。

    Returns
    -------
    bool
        True 表示通过（可以继续交易），False 表示暂停。
    """
    if len(recent_trades) < window:
        return True  # 样本不够，不下判断
    recent = recent_trades[-window:]
    losses = sum(1 for t in recent if t["pnl"] < 0)
    return (losses / len(recent)) < max_loss_ratio


# ════════════════════════════════════════════════════════════
#  无状态计算函数
# ════════════════════════════════════════════════════════════

def compute_tr(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """计算真实波幅 TR。

    TR = max(high - low, |high - prev_close|, |low - prev_close|)
    """
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def compute_atr(tr: pd.Series, period: int = 20) -> pd.Series:
    """计算 ATR(N) —— 指数平滑平均真实波幅。

    初始值 = 前 period 个 TR 的简单平均
    后续 = (1 - 1/period) × ATR(t-1) + (1/period) × TR(t)

    使用 numpy 数组 + 纯 Python 循环，避免 pandas iloc 逐元素开销
    （约 50-100x 提速，对于 1500 bars × 4 symbols 的回测规模已足够）。
    若需进一步加速可引入 numba，但当前无外部依赖约束。

    Parameters
    ----------
    tr : pd.Series
        真实波幅序列。
    period : int
        平滑周期，默认 20。

    Returns
    -------
    pd.Series
        ATR 序列（前 period-1 个值为 NaN）。
    """
    n = len(tr)
    if n < period:
        return pd.Series(np.nan, index=tr.index, dtype=float)

    alpha = 1.0 / period
    vals = tr.values.astype(float)

    out = np.full(n, np.nan)

    # ── 扫描跳过前导 NaN，定位首个可用窗口 ──
    first_valid = 0
    while first_valid < n and np.isnan(vals[first_valid]):
        first_valid += 1
    if first_valid == n:
        # 全部是 NaN
        return pd.Series(out, index=tr.index).round(4)

    seed_end = first_valid + period
    if seed_end > n:
        # 有效 TR 不足以支撑一个完整 period
        return pd.Series(out, index=tr.index).round(4)

    seed = np.nanmean(vals[first_valid:seed_end])
    out[seed_end - 1] = seed

    # EMA 递推（纯 numpy 数组索引，无 pandas iloc 开销）
    prev = seed
    for i in range(seed_end, n):
        cur = vals[i]
        if np.isnan(cur):
            out[i] = prev
        else:
            prev = (1 - alpha) * prev + alpha * cur
            out[i] = prev

    return pd.Series(out, index=tr.index).round(4)


def donchian_high(high: pd.Series, period: int) -> pd.Series:
    """N 日最高价通道（不含当日）。

    唐奇安通道上轨 = max(high[-period:-1], high[-period+1:], ..., high[-1])
    shift(1) 确保不含当日最高价。
    """
    return high.shift(1).rolling(window=period, min_periods=period).max()


def donchian_low(low: pd.Series, period: int) -> pd.Series:
    """N 日最低价通道（不含当日）。

    唐奇安通道下轨 = min(low[-period:-1], low[-period+1:], ..., low[-1])
    shift(1) 确保不含当日最低价。
    """
    return low.shift(1).rolling(window=period, min_periods=period).min()


def trail_high_close(close: pd.Series, period: int = 10) -> pd.Series:
    """移动止损参考线：最近 M 日最高收盘价。"""
    return close.rolling(window=period, min_periods=period).max()


def trail_low_close(close: pd.Series, period: int = 10) -> pd.Series:
    """移动止损参考线（空头）：最近 M 日最低收盘价。"""
    return close.rolling(window=period, min_periods=period).min()


def calc_position_size(
    equity: float,
    n_value: float,
    price: float,
    risk_pct: float = 0.01,
    stop_mult: float = 2.0,
    min_unit: int = 100,
    multiplier: int = 1,
) -> int:
    """计算头寸规模（股数/手数）。

    Parameters
    ----------
    equity : float
        当前账户净值。
    n_value : float
        当前 ATR(N) 值。
    price : float
        当前价格（入场价，保留用于兼容，实际不使用）。
    risk_pct : float
        单位风险比例，默认 0.01（1%）。
    stop_mult : float
        N 的倍数定义"一股的风险"，默认 2.0。
    min_unit : int
        最小交易单位（ETF=100，期货=1），默认 100。
    multiplier : int
        合约乘数（ETF=1，期货每手吨数/桶数），默认 1。

    Returns
    -------
    int
        股数/手数，min_unit 的整数倍。最小为 0。
    """
    if not np.isfinite(n_value) or n_value <= 0:
        return 0

    risk_amount = equity * risk_pct
    per_unit_risk = stop_mult * n_value * multiplier
    if not np.isfinite(per_unit_risk) or per_unit_risk <= 0:
        return 0
    theoretical = risk_amount / per_unit_risk
    lots = int(theoretical / min_unit)
    return max(0, lots * min_unit)


def calc_fixed_stop(
    entry_price: float,
    n_value: float,
    stop_mult: float = 2.0,
    direction: str = "long",
) -> float:
    """固定止损线。

    Parameters
    ----------
    entry_price : float
        入场价格。
    n_value : float
        入场时的 N 值。
    stop_mult : float
        N 的倍数，默认 2.0（2N 止损）。
    direction : str
        交易方向，"long" 或 "short"。

    Returns
    -------
    float
        止损价格。多头=入场价-2N，空头=入场价+2N。
    """
    if direction == "short":
        return round(entry_price + stop_mult * n_value, 4)
    return round(entry_price - stop_mult * n_value, 4)


def calc_trailing_stop(
    trail_price: float,
    n_value: float,
    prev_stop: Optional[float] = None,
    stop_mult: float = 2.0,
    direction: str = "long",
) -> float:
    """移动止损线。

    Parameters
    ----------
    trail_price : float
        最近 M 日最高收盘价（多头）或最低收盘价（空头）。
    n_value : float
        当前 N 值。
    prev_stop : float, optional
        前一日移动止损线。
    stop_mult : float
        N 的倍数，默认 2.0。
    direction : str
        交易方向，"long" 或 "short"。

    Returns
    -------
    float
        当前移动止损价格。多头只上移，空头只下移。
    """
    if not np.isfinite(n_value) or n_value <= 0:
        logger.warning(
            "calc_trailing_stop: n_value=%.4f 非法，回退到 prev_stop=%.4f",
            n_value, prev_stop or 0,
        )
        return prev_stop if (prev_stop is not None and np.isfinite(prev_stop)) else 0.0

    if not np.isfinite(trail_price):
        logger.warning(
            "calc_trailing_stop: trail_price=%.4f 非法，回退到 prev_stop=%.4f",
            trail_price, prev_stop or 0,
        )
        return prev_stop if (prev_stop is not None and np.isfinite(prev_stop)) else 0.0

    if direction == "short":
        raw_stop = round(float(trail_price) + stop_mult * float(n_value), 4)
        if prev_stop is not None and np.isfinite(prev_stop):
            return min(raw_stop, prev_stop)  # 空头：只下移
        return raw_stop

    raw_stop = round(float(trail_price) - stop_mult * float(n_value), 4)

    if not np.isfinite(raw_stop):
        safe = prev_stop if (prev_stop is not None and np.isfinite(prev_stop)) else 0.0
        logger.warning(
            "calc_trailing_stop: raw_stop=%.4f 非法，回退到 %.4f",
            raw_stop, safe,
        )
        return safe

    if prev_stop is not None and np.isfinite(prev_stop):
        return max(raw_stop, prev_stop)
    return raw_stop


def calc_pyramid_trigger(
    base_price: float,
    current_units: int,
    n_at_entry: float,
    step: float = 0.5,
    direction: str = "long",
) -> float:
    """计算下一次加仓触发价。

    公式：
        多头：base_price + current_units × step × n_at_entry
        空头：base_price - current_units × step × n_at_entry

    其中 step = 0.5 表示每上涨（多头）/ 下跌（空头）0.5N 加仓一次。

    Parameters
    ----------
    base_price : float
        初始入场价格。
    current_units : int
        当前持有单位数（1 ~ max_units-1）。
    n_at_entry : float
        入场时的 N 值。
    step : float
        加仓步长（N 的倍数），默认 0.5。
    direction : str
        交易方向，"long" 或 "short"。

    Returns
    -------
    float
        下一次加仓触发价。
    """
    if current_units < 1:
        return base_price
    offset = float(current_units) * float(step) * float(n_at_entry)
    if direction == "short":
        return round(float(base_price) - offset, 4)
    return round(float(base_price) + offset, 4)


def pyramid_add(
    current_units: int,
    max_units: int = 4,
    base_price: float = 0.0,
    n_at_entry: float = 0.0,
    step: float = 0.5,
    direction: str = "long",
) -> Tuple[bool, float]:
    """检查是否满足加仓条件并返回新的触发价。

    用法在 S3 策略层：
        can_add, trigger_price = pyramid_add(pos.units, 4, pos.base_price, pos.n_at_entry, direction=pos.direction)
        if can_add and ((direction == "short" and low <= trigger_price) or high >= trigger_price):
            # 执行加仓

    Parameters
    ----------
    current_units : int
        当前单位数。
    max_units : int
        最大单位数，默认 4。
    base_price : float
        初始入场价。
    n_at_entry : float
        入场时的 N 值。
    step : float
        加仓步长，默认 0.5。
    direction : str
        交易方向，"long" 或 "short"。

    Returns
    -------
    (can_add, trigger_price)
        can_add: 是否可以加仓
        trigger_price: 加仓触发价（can_add=False 时为 0）
    """
    if current_units >= max_units:
        return False, 0.0

    trigger = calc_pyramid_trigger(base_price, current_units, n_at_entry, step, direction)
    return True, trigger


def should_activate_trailing_stop(
    current_price: float,
    entry_price: float,
    n_value: float,
    holding_days: int = 0,
    profit_threshold_n: float = 2.0,
    days_threshold: int = 20,
    direction: str = "long",
) -> bool:
    """判断是否应从固定止损切换为移动止损。

    切换条件（任一满足即切换）：
        1. 浮盈 ≥ profit_threshold_N × N（默认 2N）
        2. 持仓天数 ≥ days_threshold（默认 20 日）

    多头：浮盈 = (当前价 - 入场价) / N
    空头：浮盈 = (入场价 - 当前价) / N

    Parameters
    ----------
    current_price : float
        当前价格。
    entry_price : float
        入场价格。
    n_value : float
        当前的 N 值。
    holding_days : int
        持仓天数。
    profit_threshold_n : float
        浮盈 N 值倍数阈值，默认 2.0。
    days_threshold : int
        持仓天数阈值，默认 20。
    direction : str
        交易方向，"long" 或 "short"。

    Returns
    -------
    bool
        True 应切换为移动止损。
    """
    if n_value <= 0:
        return False

    if direction == "short":
        floating_profit_n = (entry_price - current_price) / n_value
    else:
        floating_profit_n = (current_price - entry_price) / n_value

    if floating_profit_n >= profit_threshold_n:
        return True
    if holding_days >= days_threshold:
        return True

    return False


# ════════════════════════════════════════════════════════════
#  Position — 单品种持仓
# ════════════════════════════════════════════════════════════

@dataclass
class Position:
    """单个品种的持仓信息。

    Attributes
    ----------
    symbol : str
        品种代码（如 "510500.SH"）。
    system : str
        信号系统标识。
        "primary"  — 20日突破入场（默认）
        "filtered" — 20日突破 + 55日过滤入场
        （扩展：商品期货时可使用 "S1"/"S2"）
    direction : str
        交易方向。当前仅支持 "long"（预留做空扩展）。
    """

    symbol: str
    system: str = "primary"
    direction: str = "long"
    entry_date: Optional[date] = None
    entry_price: float = 0.0
    units: int = 1
    shares_per_unit: int = 0
    stop_loss: float = 0.0
    stop_type: str = "fixed"          # "fixed" | "trailing"
    trail_high: float = 0.0           # 10日收盘价高点（移动止损用）
    n_at_entry: float = 0.0           # 入场时的 N 值（整笔交易固定使用）
    base_price: float = 0.0           # 初始入场价（加仓基准点）
    holding_days: int = 0
    entry_mode: str = "breakout"      # "breakout" | "ma20_cross"（决定止损方式）
    high_since_entry: float = 0.0     # 持仓以来最高价（用于利润保护跟踪）
    trailing_stop: float = 0.0        # ATR 移动止损当前价位（棘轮用，只上移不下移）
    half_closed: bool = False         # 是否已执行过半仓锁定利润
    protection_activated: bool = False  # 利润保护是否已激活（状态机，一旦激活永久保持）

    @property
    def total_shares(self) -> int:
        """持仓总股数。"""
        return self.units * self.shares_per_unit

    def market_value(self, current_price: float) -> float:
        """持仓市值。"""
        return self.total_shares * current_price


# ════════════════════════════════════════════════════════════
#  TurtleSignals — 信号预计算
# ════════════════════════════════════════════════════════════

class TurtleSignals:
    """一次性为所有品种预计算海龟信号所需的中间序列。

    在 Backtrader 的 __init__() 中调用 precompute_all()，
    将结果挂载到自定义 lines 上供 next() 直接访问。

    对单个品种输出（precompute_all 返回值字典的键）：
        n              — ATR(period)
        entry_high_20  — 20日最高价通道（S1 入场参考）
        entry_low_20   — 20日最低价通道（S1 入场参考）
        entry_high_55  — 55日最高价通道（S2/55过滤参考）
        entry_low_55   — 55日最低价通道（S2/55过滤参考）
        stop_high_10   — 10日最高价（空单止损参考）
        stop_low_10    — 10日最低价（多单止损参考）
        trail_high_10  — 10日最高收盘价（移动止损参考）
    """

    def __init__(self, params: dict):
        """初始化信号计算器。

        Parameters
        ----------
        params : dict
            海龟参数，从 turtle_config.yaml['turtle'] 读取。
            必须包含: breakout_period, atr_period, stop_period
        """
        self.breakout_period = int(params.get("breakout_period", 20))
        self.atr_period = int(params.get("atr_period", 20))
        self.stop_period = int(params.get("stop_period", 10))
        self.stop_mult = float(params.get("stop_atr_multiple", 2.0))

        # 55日通道参数（硬编码为 55）
        self.filter_period = 55

    def precompute_all(self, high: pd.Series, low: pd.Series, close: pd.Series) -> dict:
        """为单个品种预计算所有信号序列。

        Parameters
        ----------
        high, low, close : pd.Series
            该品种的 OHLC 数据（相同长度和索引）。

        Returns
        -------
        dict[str, pd.Series]
            键见类的 docstring。
        """
        tr = compute_tr(high, low, close)
        atr = compute_atr(tr, self.atr_period)

        return {
            "n": atr,
            "n_series": atr,     # 【S13】完整 ATR 序列，用于入场前百分位计算
            "entry_high_20": donchian_high(high, self.breakout_period),
            "entry_low_20": donchian_low(low, self.breakout_period),
            "entry_high_55": donchian_high(high, self.filter_period),
            "entry_low_55": donchian_low(low, self.filter_period),
            "stop_high_10": donchian_high(high, self.stop_period),
            "stop_low_10": donchian_low(low, self.stop_period),
            "stop_high_5": donchian_high(high, 5),   # 【新增】5日高点，减半仓后收紧退出用
            "stop_low_5": donchian_low(low, 5),      # 【新增】5日低点，减半仓后收紧退出用
            "stop_high_7": donchian_high(high, 7),   # 【实验】7日高点
            "stop_low_7": donchian_low(low, 7),      # 【实验】7日低点
            "stop_high_8": donchian_high(high, 8),   # 【实验】8日高点
            "stop_low_8": donchian_low(low, 8),      # 【实验】8日低点
            "stop_high_6": donchian_high(high, 6),   # 【S13】6日高点——持仓≤10天收紧用
            "stop_low_6": donchian_low(low, 6),      # 【S13】6日低点——持仓≤10天收紧用
            "stop_high_12": donchian_high(high, 12), # 【S13】12日高点——持仓≥21天放宽用
            "stop_low_12": donchian_low(low, 12),    # 【S13】12日低点——持仓≥21天放宽用
            "trail_high_10": trail_high_close(close, self.stop_period),
            "trail_low_10": trail_low_close(close, self.stop_period),
            "sma_50": close.rolling(50).mean(),  # 50日均线（趋势判断用）
            "sma_60": close.rolling(60).mean(),  # 60日均线（趋势方向过滤用）
            "sma_20": close.rolling(20).mean(),  # 20日均线
            "ma5": close.rolling(5).mean(),      # MA5（双模式入场用）
            "ma10": close.rolling(10).mean(),    # MA10（金叉判断用）
            "hurst_252": self._rolling_hurst(close),
            "trend_duration_median": pd.Series(trend_duration_median(close, 20), index=close.index),
            "rsi_14": self._rsi(close, 14),
            "bb_upper_20": close.rolling(20).mean() + 2 * close.rolling(20).std(),
            "bb_lower_20": close.rolling(20).mean() - 2 * close.rolling(20).std(),
        }

    def _rsi(self, close: pd.Series, period: int = 14) -> pd.Series:
        """Wilder 平滑 RSI。"""
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, 1e-10)
        return 100 - (100 / (1 + rs))

    def _rolling_hurst(self, close: pd.Series, window: int = 252) -> pd.Series:
        """滚动窗口 Hurst 指数（每日计算，末尾 pad 向前填充）。"""
        values = close.values.astype(np.float64)
        n = len(values)
        result = np.full(n, np.nan)
        for i in range(window, n):
            result[i] = hurst_exponent(values[i-window:i], max_lag=40)
        series = pd.Series(result, index=close.index)
        series.ffill(inplace=True)
        return series


# ════════════════════════════════════════════════════════════
#  TurtlePositions — 多品种持仓管理
# ════════════════════════════════════════════════════════════

class TurtlePositions:
    """管理所有品种的持仓，提供增删改查快捷方法。

    用法：
        positions = TurtlePositions(max_units=4)
        positions.open("510500.SH", entry_price=5.5, n_at_entry=0.12, shares=800)
        if positions.has_position("510500.SH"):
            pos = positions.get("510500.SH")
            pos.holding_days += 1
        positions.close("510500.SH")
    """

    def __init__(self, max_units: int = 4):
        self._positions: Dict[str, Position] = {}
        self._max_units = max_units

    # ── 查询 ──

    def has_position(self, symbol: str) -> bool:
        """检查指定品种是否有持仓。"""
        return symbol in self._positions

    def get(self, symbol: str) -> Optional[Position]:
        """获取指定品种的持仓，不存在返回 None。"""
        return self._positions.get(symbol)

    def all_positions(self) -> List[Position]:
        """返回所有持仓的列表副本。"""
        return list(self._positions.values())

    @property
    def count(self) -> int:
        """当前有持仓的品种数量。"""
        return len(self._positions)

    @property
    def symbols(self) -> List[str]:
        """当前有持仓的品种代码列表。"""
        return list(self._positions.keys())

    def is_full(self, symbol: str) -> bool:
        """指定品种是否已达最大单位数。"""
        pos = self.get(symbol)
        if pos is None:
            return False
        return pos.units >= self._max_units

    # ── 操作 ──

    def open(
        self,
        symbol: str,
        system: str = "primary",
        direction: str = "long",
        entry_date: Optional[date] = None,
        entry_price: float = 0.0,
        shares: int = 0,
        n_at_entry: float = 0.0,
        stop_loss: float = 0.0,
        entry_mode: str = "breakout",
    ) -> Position:
        """开新仓。

        Returns
        -------
        Position
            新创建的持仓对象。
        """
        if self.has_position(symbol):
            raise ValueError(f"{symbol} 已有持仓，不能重复开仓")

        position = Position(
            symbol=symbol,
            system=system,
            direction=direction,
            entry_date=entry_date,
            entry_price=entry_price,
            shares_per_unit=shares,
            n_at_entry=n_at_entry,
            stop_loss=stop_loss,
            base_price=entry_price,
            entry_mode=entry_mode,
        )
        self._positions[symbol] = position
        logger.info("[开仓] %s system=%s price=%.4f shares=%d units=1",
                    symbol, system, entry_price, shares)
        return position

    def add_unit(self, symbol: str, new_stop_loss: float) -> bool:
        """对一个已有持仓品种加仓一个单位。

        Parameters
        ----------
        symbol : str
            品种代码。
        new_stop_loss : float
            加仓后新的止损线。

        Returns
        -------
        bool
            是否成功加仓。
        """
        pos = self.get(symbol)
        if pos is None:
            logger.warning("[加仓] %s 无持仓，无法加仓", symbol)
            return False
        if pos.units >= self._max_units:
            logger.warning("[加仓] %s 已达最大单位 %d", symbol, self._max_units)
            return False

        pos.units += 1
        pos.stop_loss = new_stop_loss
        logger.info("[加仓] %s units=%d stop=%.4f", symbol, pos.units, new_stop_loss)
        return True

    def close(self, symbol: str) -> Optional[Position]:
        """平仓并返回平仓前的持仓信息。

        Returns
        -------
        Position or None
            平仓前的持仓对象（用于计算盈亏）；无持仓时返回 None。
        """
        pos = self._positions.pop(symbol, None)
        if pos is not None:
            logger.info("[平仓] %s units=%d entry=%.4f", symbol, pos.units, pos.entry_price)
        return pos

    def update_stop_loss(self, symbol: str, new_stop: float, stop_type: str = "trailing"):
        """更新指定品种的止损线。"""
        pos = self.get(symbol)
        if pos is not None:
            pos.stop_loss = new_stop
            pos.stop_type = stop_type

    def update_trail_high(self, symbol: str, new_high: float):
        """更新移动止损的参考高点。"""
        pos = self.get(symbol)
        if pos is not None:
            if new_high > pos.trail_high:
                pos.trail_high = new_high

    def reduce_shares(self, symbol: str, reduce_by: int) -> bool:
        """减仓指定股数（不关闭持仓），标记 half_closed。

        Parameters
        ----------
        symbol : str
            品种代码。
        reduce_by : int
            要减掉的股数。

        Returns
        -------
        bool
            是否成功减仓。
        """
        pos = self.get(symbol)
        if pos is None:
            return False
        actual = min(reduce_by, pos.total_shares)
        if actual <= 0:
            return False
        ratio = 1 - actual / pos.total_shares
        pos.shares_per_unit = max(1, int(pos.shares_per_unit * ratio))
        pos.half_closed = True
        logger.info("[减仓] %s 减 %d 股，剩余 %d 股 (half_closed=True)",
                     symbol, actual, pos.total_shares)
        return True


# ════════════════════════════════════════════════════════════
#  SignalFilter — 盈利过滤器（单系统简化版）
# ════════════════════════════════════════════════════════════

@dataclass
class _FilterState:
    """单个 (symbol) 的过滤器内部状态。"""
    symbol: str
    total_signals: int = 0
    total_accepted: int = 0
    total_rejected: int = 0
    consecutive_rejections: int = 0
    last_trade_was_win: Optional[bool] = None  # None = 无历史


class SignalFilter:
    """盈利过滤器（单系统版）。

    规则：
        1. 首个信号 → 无条件接受
        2. 同品种已持仓 → 拒绝
        3. 上次同品种交易盈利 → 接受；亏损 → 跳过
        4. 连续拒绝 ≥ max_rejections → 强制放行（上限保护）

    用法：
        filter = SignalFilter()
        accepted, reason = filter.check_entry("510500.SH", has_position=False)
        filter.record_result("510500.SH", was_win=True)
    """

    def __init__(self, max_rejections: int = 3):
        self._states: Dict[str, _FilterState] = {}
        self.max_rejections = max_rejections

    def _get_state(self, symbol: str) -> _FilterState:
        if symbol not in self._states:
            self._states[symbol] = _FilterState(symbol=symbol)
        return self._states[symbol]

    def check_entry(self, symbol: str, has_position: bool = False) -> Tuple[bool, str]:
        """检查入场信号是否通过过滤器。

        Parameters
        ----------
        symbol : str
            品种代码。
        has_position : bool
            该品种当前是否有持仓。

        Returns
        -------
        (accepted, reason)
            accepted: True 为接受，False 为拒绝
            reason: 原因说明
        """
        state = self._get_state(symbol)
        state.total_signals += 1

        # 规则 1：首个信号
        if state.last_trade_was_win is None:
            state.total_accepted += 1
            state.consecutive_rejections = 0
            return True, "首个信号，无条件接受"

        # 规则 2：持仓互斥
        if has_position:
            state.total_rejected += 1
            state.consecutive_rejections += 1
            return False, "同品种已持仓，拒绝"

        # 规则 3：盈利过滤器
        if state.last_trade_was_win:
            state.total_accepted += 1
            state.consecutive_rejections = 0
            return True, "上次交易盈利出场，接受"

        # 规则 4：上限保护 — 不放行计数器，只放行信号
        if state.consecutive_rejections >= self.max_rejections:
            actual = state.consecutive_rejections + 1
            state.total_accepted += 1
            # 不归零 consecutive_rejections！归零在 record_result 中做。
            # 若 entry 因外部原因（现金不足/风险约束）未成交，record_result 不会被调用，
            # 计数器保持 ≥3，后续每个信号都强制放行，直到真正成交。
            logger.info(
                "[滤波器] %s 连续拒绝 %d 次 → 强制放行",
                symbol, actual,
            )
            return True, f"连续拒绝≥{self.max_rejections}→强制放行"

        state.total_rejected += 1
        state.consecutive_rejections += 1
        return False, f"上次交易亏损出场（连续拒绝{state.consecutive_rejections}次），跳过"

    def record_result(self, symbol: str, was_win: bool):
        """记录一笔交易的结果，更新过滤器状态。

        Parameters
        ----------
        symbol : str
            品种代码。
        was_win : bool
            是否盈利出场。
        """
        state = self._get_state(symbol)
        state.last_trade_was_win = was_win
        state.consecutive_rejections = 0

    def reset(self, symbol: Optional[str] = None):
        """重置过滤器状态。

        Parameters
        ----------
        symbol : str, optional
            指定品种。不指定则重置所有。
        """
        if symbol:
            self._states.pop(symbol, None)
        else:
            self._states.clear()

    def get_stats(self) -> Dict[str, dict]:
        """导出所有过滤器的统计信息（用于日志/调试）。"""
        from dataclasses import asdict
        return {k: asdict(v) for k, v in self._states.items()}


# ════════════════════════════════════════════════════════════
#  Hurst 指数（用于品种筛选 §5.12）
# ════════════════════════════════════════════════════════════

def hurst_exponent(price: np.ndarray, max_lag: int = 100) -> float:
    """重标极差法 (R/S) 计算 Hurst 指数。

    衡量价格序列的趋势持续性。

    Parameters
    ----------
    price : np.ndarray, shape (n_days,)
        价格序列（收盘价），至少需要 252 个数据点。
    max_lag : int
        最大滞后阶数，默认 100。

    Returns
    -------
    float
        H ∈ [0, 1]。
        H > 0.55  → 趋势持续（适合海龟策略）
        H ≈ 0.50  → 随机游走（海龟仍可捕捉肥尾）
        H < 0.45  → 均值回归（不适合趋势跟踪）
    """
    returns = np.diff(np.log(price))
    n = len(returns)
    if n < 50:
        return 0.5

    if n < max_lag:
        max_lag = max(10, n // 4)

    lags = range(2, min(max_lag, n // 2))
    rs_values = []

    for lag in lags:
        n_chunks = n // lag
        if n_chunks < 2:
            break
        chunks = returns[:n_chunks * lag].reshape(n_chunks, lag)
        mean = chunks.mean(axis=1, keepdims=True)
        deviations = chunks - mean
        Z = deviations.cumsum(axis=1)
        R = Z.max(axis=1) - Z.min(axis=1)
        S = chunks.std(axis=1, ddof=1)
        S[S == 0] = 1e-10
        rs_values.append((R / S).mean())

    if len(rs_values) < 4:
        return 0.5

    log_lags = np.log(list(range(2, 2 + len(rs_values))))
    log_rs = np.log(np.array(rs_values))
    H = np.polyfit(log_lags, log_rs, 1)[0]
    return max(0.0, min(1.0, float(H)))


def trend_duration_median(close: pd.Series, ma_period: int = 20) -> float:
    """连续高于/低于均线天数的中位数。
    值 < 5 → 趋势太短，不适合20日突破系统。
    """
    ma = close.rolling(ma_period, min_periods=ma_period).mean().dropna()
    above = (close.reindex(ma.index) > ma).astype(int)
    streaks = []; cur = 0
    for v in above:
        cur = cur + 1 if v else (streaks.append(cur) or 0 if cur else 0)
    if cur: streaks.append(cur)
    cur = 0
    for v in above:
        cur = cur + 1 if not v else (streaks.append(cur) or 0 if cur else 0)
    if cur: streaks.append(cur)
    return float(np.median(streaks)) if streaks else 0.0
