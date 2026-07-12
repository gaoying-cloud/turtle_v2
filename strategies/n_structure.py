"""
N字结构策略 — 纯 pandas/numpy 实现

核心逻辑：
  1. 滑动窗口扫描 N 字结构（A 低点 → D 中间高点 → B 更高低点）
  2. 价格突破 B 点 + MA250 过滤 → C 点进场
  3. D 点突破确认/止损/加仓管理

策略参数（可在实例化时覆盖）：
    window_size   : int   = 100   — 滑动窗口大小（S22 调优定型）
    atr_period    : int   = 25    — ATR 周期
    stop_mult     : float = 1.5   — 初始止损 ATR 倍数（S22 调优后）
    trail_mult    : float = 5.0   — 跟踪止损 ATR 倍数
    add_step      : float = 2.0   — 加仓间隔（ATR 倍数）
    max_units     : int   = 6     — 最大单位数（S22 调优后）
    ma_trend      : int   = 0     — 趋势过滤均线周期，0=关闭（S22 调优后）
    ma_confirm    : int   = 5     — 形态确认均线周期（S22 调优后关闭 use_ma5_confirm）

形态识别参数（S24 新增，控制局部极值确认）：
    confirm_k     : int   = 2     — 极值确认延迟（K 线数）
    min_advance   : float = 0.05  — D > A 的最小幅度
    min_gap_ad    : int   = 5     — A→D 最小 K 线间距
    min_gap_db    : int   = 3     — D→B 最小 K 线间距
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════
#  辅助计算
# ════════════════════════════════════════════════════════════

def compute_atr(high: pd.Series, low: pd.Series, close: pd.Series,
                period: int = 25) -> pd.Series:
    """ATR — 指数平滑，与 turtle_core 一致。"""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    n = len(tr)
    if n < period:
        return pd.Series(np.nan, index=tr.index, dtype=float)

    alpha = 1.0 / period
    vals = tr.values.astype(float)
    out = np.full(n, np.nan)

    first = 0
    while first < n and np.isnan(vals[first]):
        first += 1
    if first == n:
        return pd.Series(out, index=tr.index)

    seed_end = first + period
    if seed_end > n:
        return pd.Series(out, index=tr.index)

    seed = np.nanmean(vals[first:seed_end])
    out[seed_end - 1] = seed

    prev = seed
    for i in range(seed_end, n):
        cur = vals[i]
        if np.isnan(cur):
            out[i] = prev
        else:
            prev = (1 - alpha) * prev + alpha * cur
            out[i] = prev

    return pd.Series(out, index=tr.index)


def compute_ma(close: pd.Series, period: int) -> pd.Series:
    """简单移动平均。"""
    return close.rolling(window=period, min_periods=period).mean()


# ════════════════════════════════════════════════════════════
#  N 字结构识别
# ════════════════════════════════════════════════════════════

@dataclass
class NStructure:
    """一个完整的 N 字结构。"""
    a_idx: int         # A 点（第一个低点）在 df 中的索引
    d_idx: int         # D 点（中间高点）索引
    b_idx: int         # B 点（第二个低点）索引
    a_price: float
    d_price: float
    b_price: float

    def is_valid(self) -> bool:
        """结构有效性：B > A（更高低点）。"""
        return self.b_price > self.a_price


def _is_local_min(series: pd.Series, idx: int, half_window: int = 2) -> bool:
    """检查 idx 是否为局部最低点（±half_window 范围内）。

    仅用 idx 两侧各 half_window 根已闭合 K 线做比较。
    边界附近自动缩小比较范围。
    """
    n = len(series)
    lo = max(0, idx - half_window)
    hi = min(n - 1, idx + half_window)
    if lo == hi:
        return True
    window_min = series.iloc[lo:hi + 1].min()
    return series.iloc[idx] <= window_min


def _is_local_max(series: pd.Series, idx: int, half_window: int = 2) -> bool:
    """检查 idx 是否为局部最高点（±half_window 范围内）。"""
    n = len(series)
    lo = max(0, idx - half_window)
    hi = min(n - 1, idx + half_window)
    if lo == hi:
        return True
    window_max = series.iloc[lo:hi + 1].max()
    return series.iloc[idx] >= window_max


def _is_confirmed_low(df: pd.DataFrame, idx: int, confirm_k: int = 2) -> bool:
    """idx 处的低点是否已确认：后续 confirm_k 根 K 线未再创新低。"""
    n = len(df)
    end_check = min(n, idx + confirm_k + 1)
    after_low = df['low'].iloc[idx + 1:end_check].min()
    return after_low >= df.loc[idx, 'low']


def _is_confirmed_high(df: pd.DataFrame, idx: int, confirm_k: int = 2) -> bool:
    """idx 处的高点是否已确认：后续 confirm_k 根 K 线未再创新高。"""
    n = len(df)
    end_check = min(n, idx + confirm_k + 1)
    after_high = df['high'].iloc[idx + 1:end_check].max()
    return after_high <= df.loc[idx, 'high']


# ── 向后兼容别名（旧函数签名，内部转发到新实现） ──
def find_n_structure_in_window(
    df: pd.DataFrame,
    end_idx: int,
    window_size: int = 60,   # S39: 100→60
    *,
    confirm_k: int = 2,
    min_advance: float = 0.05,
    min_gap_ad: int = 5,
    min_gap_db: int = 3,
    local_half_window: int = 2,
) -> Optional[NStructure]:
    """在窗口中寻找实时可确认的 N 字结构（无未来信息泄露）。

    **与旧版关键区别**：
      旧版用 window.low.idxmin() / high.idxmax() — 全局极值依赖完整窗口数据。
      新版用局部极值 + 确认延迟 — 每个关键点在当时即可确认。

    算法概要：
      1. 从窗口右侧向左扫描，找已确认的局部低点作为 B 候选
      2. 对每个 B，向左找已确认的局部高点作为 D
      3. 对每个 D，向左找 A（窗口内低点，B > A 即有效）
      4. 返回第一个满足条件的完整 N 字结构（最接近当前的）

    Parameters
    ----------
    df : pd.DataFrame
        含 date, open, high, low, close 列，按日期升序。
    end_idx : int
        当前 bar 索引（不含，仅用 [end_idx - window_size, end_idx)）。
    window_size : int
        滑动窗口大小。
    confirm_k : int
        极值确认延迟（K 线数），默认 2。一个低点/高点出现后，
        需要 confirm_k 根 K 线不创新低/新高才算确认。
    min_advance : float
        D 必须高于 A 的最小比例，默认 0.05（5%）。
    min_gap_ad : int
        A→D 最小 K 线间距。
    min_gap_db : int
        D→B 最小 K 线间距。
    local_half_window : int
        局部极值判断的半窗口大小，默认 2（±2 根 K 线内最低/最高）。

    Returns
    -------
    NStructure or None
    """
    start = max(0, end_idx - window_size)
    last_bar = end_idx - 1  # 最后可用 K 线索引

    if last_bar - start < 20:  # S39: 30→20，允许更小窗口
        return None

    # 数据切片：start 到 end_idx（不含），全部是已闭合 K 线
    # 为了索引方便，直接在原 df 上用整数位置操作

    # 可确认的最晚位置：必须有 confirm_k 根 K 线在它之后
    latest_usable = last_bar - confirm_k
    if latest_usable < start:
        return None

    # ── 从右向左扫描 B 候选（最近的结构优先） ──
    b_search_end = latest_usable
    b_search_start = start + min_gap_ad + min_gap_db + 2  # B 前面至少要有 A 和 D

    for b_idx in range(b_search_end, b_search_start, -1):
        if not _is_local_min(df['low'], b_idx, local_half_window):
            continue
        if not _is_confirmed_low(df, b_idx, confirm_k):
            continue

        b_price = df.loc[b_idx, 'low']

        # ── 向左扫描 D 候选 ──
        d_search_end = b_idx - min_gap_db
        d_search_start = start + min_gap_ad + 1

        for d_idx in range(d_search_end, d_search_start, -1):
            if not _is_local_max(df['high'], d_idx, local_half_window):
                continue
            if not _is_confirmed_high(df, d_idx, confirm_k):
                continue

            d_price = df.loc[d_idx, 'high']

            # ── 找 A：start 到 d_idx 之间的最低价 ──
            a_slice = df['low'].iloc[start:d_idx + 1]
            if len(a_slice) == 0:
                continue
            a_pos = int(a_slice.values.argmin())
            a_idx = start + a_pos
            a_price = df.loc[a_idx, 'low']

            # ── 结构有效性检查 ──
            if d_idx - a_idx < min_gap_ad:
                continue
            if d_price <= a_price * (1 + min_advance):
                continue
            if not (a_price < b_price < d_price):
                continue

            return NStructure(
                a_idx=a_idx, d_idx=d_idx, b_idx=b_idx,
                a_price=a_price, d_price=d_price, b_price=b_price,
            )

    return None


def ma5_confirm(df: pd.DataFrame, ns: NStructure,
                current_idx: int) -> bool:
    """MA5 辅助确认：B 点附近 MA5 已拐头向上（仅用已闭合 K 线）。

    判断标准：
      - B 点到当前 K 线之前，MA5 出现过拐头向上（MA5[t] > MA5[t-1]）

    Parameters
    ----------
    current_idx : int
        当前 K 线索引，用于限制不访问未来数据。
    """
    if 'ma5' not in df.columns:
        return True  # 无 MA5 时不拦截

    # 只检查 B 点到当前 K 线之前（最多 5 根），不访问未来
    end = min(ns.b_idx + 5, current_idx - 1)  # 不含当前 K 线
    for i in range(ns.b_idx, end + 1):
        if i < 1:
            continue
        if df.loc[i, 'ma5'] > df.loc[i - 1, 'ma5']:
            return True  # MA5 拐头向上

    return False


# ════════════════════════════════════════════════════════════
#  交易记录
# ════════════════════════════════════════════════════════════

@dataclass
class Trade:
    """一笔完整交易的记录。"""
    symbol: str
    entry_idx: int
    entry_price: float
    exit_idx: int = -1
    exit_price: float = 0.0
    exit_reason: str = ""
    units: int = 1
    pnl: float = 0.0
    a_price: float = 0.0
    b_price: float = 0.0
    d_price: float = 0.0


# ════════════════════════════════════════════════════════════
#  主策略
# ════════════════════════════════════════════════════════════

@dataclass
class PositionState:
    """单品种当前持仓状态。"""
    active: bool = False
    entry_idx: int = -1
    entry_price: float = 0.0
    stop_loss: float = 0.0
    d_price: float = 0.0       # 本结构的 D 点
    b_price: float = 0.0
    a_price: float = 0.0
    units: int = 1              # 当前加仓单位数
    shares_per_unit: int = 0    # 每个单位的股数
    total_cost: float = 0.0     # 累计持仓成本 = Σ(每次fill价格 × 股数)
    highest_since_entry: float = 0.0
    next_add_level: float = 0.0
    d_broken: bool = False      # 是否已突破 D 点

    # 再进场
    reentry_eligible: bool = False   # 是否允许再进场
    reentry_b_price: float = 0.0     # 再进场的 B 点参考价
    reentry_d_price: float = 0.0     # 再进场的 D 点参考价
    reentry_a_price: float = 0.0     # 再进场的 A 点参考价
    reentry_count: int = 0           # 已再进场次数
    trade: Optional[Trade] = None


class NStructureStrategy:
    """N 字结构交易策略。

    对单个品种的 DataFrame 执行回测，返回交易记录和净值曲线。
    """

    @staticmethod
    def _avg_entry_price(pos: PositionState) -> float:
        """计算加权平均入场价（考虑加仓）。"""
        total_shares = pos.units * pos.shares_per_unit
        if total_shares > 0 and pos.total_cost > 0:
            return pos.total_cost / total_shares
        return pos.entry_price

    def __init__(
        self,
    window_size: int = 60,   # S39: 100→60
    atr_period: int = 25,
    stop_mult: float = 1.5,
    trail_mult: float = 5.0,    # 跟踪止损 ATR 倍数（中期正常）
    trail_mult_wide: float = 8.0,  # 跟踪止损 ATR 倍数（D突破初期，宽止损让趋势发育）
    trail_mult_tight: float = 3.0, # 跟踪止损 ATR 倍数（大浮盈锁利）
    d_timeout_days: int = 40,      # D点超时：持仓N天未突破D则退出
    add_step: float = 2.0,      # 加仓间隔（ATR 倍数），每 2N 加仓一次
    max_units: int = 6,         # 最大单位数：1 初始 + 5 次加仓（S22 调优）
    ma_trend: int = 50,         # 趋势过滤均线周期，50=MA50（S37 启用）
    ma_confirm: int = 5,
    use_ma5_confirm: bool = False,  # 关闭 MA5 确认（S22 调优）
    initial_capital: float = 100000.0,
    risk_per_trade: float = 0.01,
    max_reentries: int = 0,     # 0=关闭, N=最多再进场N次
    num_symbols: int = 6,       # 品种数，用于资金分配
    # ── S24 形态识别参数 ──
    confirm_k: int = 3,              # 极值确认延迟（K线数，S37: 2→3 减少假结构）
    min_advance: float = 0.05,       # D > A 的最小幅度
    min_gap_ad: int = 5,             # A→D 最小K线间距
    min_gap_db: int = 3,             # D→B 最小K线间距
    local_half_window: int = 2,      # 局部极值判断半窗口
    # ── S24 摩擦成本参数 ──
    slippage_pct: float = 0.001,     # 成交滑点 (0.1%)
    commission_pct: float = 0.00015, # 手续费率 (0.015%, ETF 万1.5)
    # ── S25 风控参数 ──
    use_dynamic_equity: bool = True,       # 动态权益仓位（复利）
    max_consecutive_losses: int = 5,        # 连续亏损熔断阈值（42%胜率下概率≈6.6%）
    pause_bars: int = 20,                   # 熔断冷却 K 线数
    # ── S27/S39 止损地板参数 ──
    stop_floor_pre_break: float = 0.95,    # D点未突破时止损地板（S39: 0.93→0.95 收紧）
    stop_floor_post_break: float = 0.95,   # D点突破后/进场时止损地板
    # ── S30 信号过滤 + 仓位管理 ──
    use_ma_cross: bool = True,             # MA5>MA20 金叉过滤（过滤短期下行假突破）
    max_position_pct: float = 0.25,        # 单品种最大仓位上限（价格比例，防 ETF 杠杆过度）
    # ── S37 进场确认 ──
    entry_confirm_bars: int = 2,           # 突破 B 点后需连续站稳 K 线数（1=当日确认, 2=需前日也站上）
    # ── S38 结构质量过滤 ──
    max_ad_advance: float = 1.0,           # A→D 最大涨幅上限，1.0=关闭（S38 实验后回退）
    max_ab_advance: float = 1.0,           # A→B 最大抬升上限，1.0=关闭（候选）
    # ── S39 出场逻辑重构 ──
    trail_pre_d: float = 2.5,              # D突破前 ATR 跟踪倍数（S39: 2.0→2.5 减少误杀）
    use_ma_exit: bool = True,              # D突破后启用 MA 趋势出场
    ma_exit_period: int = 20,              # MA 出场均线周期
    ma_exit_margin: float = 0.97,          # MA 有效跌破阈值（备用，ma_exit_confirm>0 时以确认天数为主）
    ma_exit_confirm: int = 0,              # MA 有效跌破：0=margin百分比（S39最优）, -1=实体1/3法, >0=连续N日
    ma_exit_bearish: bool = True,          # MA 出场要求阴线确认（close < open）
    d_exit_floor: float = 0.95,            # D突破后硬止损地板：D × floor（防极端回撤）
    ):
        self.window_size = window_size
        self.atr_period = atr_period
        self.stop_mult = stop_mult
        self.trail_mult = trail_mult
        self.trail_mult_wide = trail_mult_wide
        self.trail_mult_tight = trail_mult_tight
        self.d_timeout_days = d_timeout_days
        self.add_step = add_step
        self.max_units = max_units
        self.ma_trend = ma_trend
        self.ma_confirm = ma_confirm
        self.use_ma5_confirm = use_ma5_confirm
        self.initial_capital = initial_capital
        self.risk_per_trade = risk_per_trade
        self.max_reentries = max_reentries
        self.capital_per_symbol = initial_capital / max(1, num_symbols)
        # S24 形态识别参数
        self.confirm_k = confirm_k
        self.min_advance = min_advance
        self.min_gap_ad = min_gap_ad
        self.min_gap_db = min_gap_db
        self.local_half_window = local_half_window
        # S24 摩擦成本
        self.slippage_pct = slippage_pct
        self.commission_pct = commission_pct
        # S25 风控
        self.use_dynamic_equity = use_dynamic_equity
        self.max_consecutive_losses = max_consecutive_losses
        self.pause_bars = pause_bars
        # S27 止损地板
        self.stop_floor_pre_break = stop_floor_pre_break
        self.stop_floor_post_break = stop_floor_post_break
        # S30 信号过滤 + 仓位
        self.use_ma_cross = use_ma_cross
        self.max_position_pct = max_position_pct
        # S37 进场确认
        self.entry_confirm_bars = entry_confirm_bars
        # S38 结构质量过滤
        self.max_ad_advance = max_ad_advance
        self.max_ab_advance = max_ab_advance
        # S39 出场逻辑重构
        self.trail_pre_d = trail_pre_d
        self.use_ma_exit = use_ma_exit
        self.ma_exit_period = ma_exit_period
        self.ma_exit_margin = ma_exit_margin
        self.ma_exit_confirm = ma_exit_confirm
        self.ma_exit_bearish = ma_exit_bearish
        self.d_exit_floor = d_exit_floor

    def _buy_price(self, price: float) -> float:
        """买入实际成交价 = 理想价 × (1 + 滑点)。"""
        return price * (1 + self.slippage_pct)

    def _sell_price(self, price: float) -> float:
        """卖出实际成交价 = 理想价 × (1 - 滑点)。"""
        return price * (1 - self.slippage_pct)

    def _commission_cost(self, price: float, shares: int) -> float:
        """单边手续费 = 成交价 × 股数 × 费率。"""
        return price * shares * self.commission_pct

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """预计算策略所需的全部指标。"""
        result = df.copy()
        result['atr'] = compute_atr(result['high'], result['low'],
                                     result['close'], self.atr_period)
        if self.ma_trend > 0:
            result['ma_trend'] = compute_ma(result['close'], self.ma_trend)
        result['ma5'] = compute_ma(result['close'], self.ma_confirm)
        if self.use_ma_cross or self.use_ma_exit:
            result['ma20'] = compute_ma(result['close'], self.ma_exit_period)
        return result

    def _calc_shares(self, equity: float, price: float, atr: float) -> int:
        """仓位计算：ATR 风险预算 + 价格比例上限，取较小值。

        1. ATR-based: risk_amount / per_share_risk（海龟原版）
        2. Price-cap:   equity × max_position_pct / price（防 ETF 杠杆过度）
        3. 取 min，舍入到 100 的倍数，下限 100 股
        """
        # ATR 风险预算
        risk_amount = equity * self.risk_per_trade
        per_share_risk = self.stop_mult * atr
        atr_shares = risk_amount / per_share_risk if per_share_risk > 0 else 0

        # 价格比例上限
        max_cost = equity * self.max_position_pct
        price_cap_shares = max_cost / price if price > 0 else 0

        shares = int(min(atr_shares, price_cap_shares) / 100) * 100
        return max(100, shares)

    def run(self, df: pd.DataFrame, symbol: str = "",
            verbose: bool = True) -> Tuple[pd.DataFrame, list[Trade], pd.Series]:
        """对单品种执行 N 字结构策略回测。

        进场规则（防偷价）：
          - 用 bar i-1 的收盘价判断信号是否触发
          - 在 bar i 的开盘价执行进场（次日开盘进场）

        Parameters
        ----------
        df : pd.DataFrame
            含 date, open, high, low, close, volume 列，按日期升序。
        symbol : str
            品种代码（仅用于日志）。
        verbose : bool
            是否打印交易明细。

        Returns
        -------
        (df_result, trades, equity_curve)
            df_result 带了信号列；trades 为 Trade 列表；
            equity_curve 为日频权益 Series（index=date）。
        """
        df = self.compute_indicators(df)
        trades: list[Trade] = []
        pos = PositionState()

        n = len(df)

        # ── 日频权益追踪 (S24/S25) ──
        equity_arr = np.full(n, np.nan)
        current_equity = float(self.capital_per_symbol)  # 已实现权益（现金）

        # ── 连续亏损熔断 (S25) ──
        consecutive_losses = 0
        paused_until_bar = -1

        # 窗口初期：无交易，权益 = 初始资金
        equity_arr[:self.window_size] = self.capital_per_symbol

        for i in range(self.window_size, n):
            # ── 熔断恢复检查 (S25) ──
            if i >= paused_until_bar and paused_until_bar >= 0:
                if verbose:
                    print(f"  🟢 熔断恢复 [{i}]  继续交易")
                paused_until_bar = -1

            # ── 持仓管理 ──
            if pos.active:
                self._manage_position(df, i, pos, trades, verbose)
                if pos.active:
                    avg_cost = self._avg_entry_price(pos)
                    unrealized = ((df.loc[i, 'close'] - avg_cost)
                                  * pos.units * pos.shares_per_unit)
                    equity_arr[i] = current_equity + unrealized
                else:
                    # 刚平仓：更新已实现权益 + 熔断计数
                    current_equity += trades[-1].pnl
                    equity_arr[i] = current_equity
                    if trades[-1].pnl < 0:
                        consecutive_losses += 1
                        if consecutive_losses >= self.max_consecutive_losses:
                            paused_until_bar = i + self.pause_bars
                            if verbose:
                                print(f"  🔴 连续亏损 {consecutive_losses} 次 → 熔断 "
                                      f"{self.pause_bars} 根 K 线 (至 bar {paused_until_bar})")
                    else:
                        consecutive_losses = 0
                continue

            # ── 熔断中：跳过进场 (S25) ──
            if i < paused_until_bar:
                equity_arr[i] = current_equity
                continue

            # ── 用于仓位计算的权益（取上一日权益值） ──
            sizing_equity = (equity_arr[i - 1] if i > 0 and not np.isnan(equity_arr[i - 1])
                             else current_equity)

            # ── 再进场检查 ──
            if pos.reentry_eligible:
                self._execute_reentry(df, i, pos, trades, symbol, verbose,
                                      current_equity=sizing_equity)
                if pos.active:
                    pos.reentry_eligible = False
                    avg_cost = self._avg_entry_price(pos)
                    unrealized = ((df.loc[i, 'close'] - avg_cost)
                                  * pos.units * pos.shares_per_unit)
                    equity_arr[i] = current_equity + unrealized
                    continue
                pos.reentry_eligible = False

            # ── 正常进场扫描 ──
            self._check_entry_from_prev(df, i, pos, trades, symbol, verbose,
                                        current_equity=sizing_equity)
            if pos.active:
                avg_cost = self._avg_entry_price(pos)
                unrealized = ((df.loc[i, 'close'] - avg_cost)
                              * pos.units * pos.shares_per_unit)
                equity_arr[i] = current_equity + unrealized
            else:
                equity_arr[i] = current_equity

        # 填充前导 NaN + 构建 Series
        equity_filled = pd.Series(equity_arr, index=df.index).ffill()
        equity_curve = pd.Series(equity_filled.values, index=df['date'])

        return df, trades, equity_curve

    def _check_entry_from_prev(self, df: pd.DataFrame, i: int,
                                pos: PositionState, trades: list[Trade],
                                symbol: str, verbose: bool,
                                current_equity: float | None = None):
        """用 bar i-1 的数据检查信号，在 bar i 的开盘进场。

        设计原因：实盘中无法在收盘价成交，信号触发后最早在下个开盘进场。
        """
        if i < 1:
            return

        # S25: 动态权益（默认使用固定 capital_per_symbol）
        if current_equity is None:
            current_equity = self.capital_per_symbol

        # 用昨日收盘数据检测信号
        prev = i - 1

        # 1. 扫描 N 字结构（数据截止到 prev，不含当前 bar）
        ns = find_n_structure_in_window(
            df, prev, self.window_size,
            confirm_k=self.confirm_k,
            min_advance=self.min_advance,
            min_gap_ad=self.min_gap_ad,
            min_gap_db=self.min_gap_db,
            local_half_window=self.local_half_window,
        )
        if ns is None:
            return

        # 2. S38 结构质量过滤：A→D 涨幅过大 → 趋势已耗竭
        if self.max_ad_advance < 1.0:
            ad_advance = (ns.d_price - ns.a_price) / ns.a_price
            if ad_advance > self.max_ad_advance:
                return
        if self.max_ab_advance < 1.0:
            ab_advance = (ns.b_price - ns.a_price) / ns.a_price
            if ab_advance > self.max_ab_advance:
                return

        # 3. MA5 辅助确认（只用已闭合 K 线）
        if self.use_ma5_confirm:
            if not ma5_confirm(df, ns, prev):
                return

        # 3. 进场条件：连续 N 日收盘 > B，且（可选）收盘 > 趋势 MA
        prev_close = df.loc[prev, 'close']

        if prev_close <= ns.b_price:
            return

        # S37 进场确认延迟：要求连续 entry_confirm_bars 日收盘 > B
        if self.entry_confirm_bars > 1:
            for offset in range(1, self.entry_confirm_bars):
                check_idx = prev - offset
                if check_idx < 0:
                    return
                if df.loc[check_idx, 'close'] <= ns.b_price:
                    return

        # 趋势过滤（ma_trend <= 0 表示关闭, S37 默认启用 MA50）
        if self.ma_trend > 0:
            prev_ma = df.loc[prev, 'ma_trend']
            if pd.isna(prev_ma) or prev_close <= prev_ma:
                return

        # MA5×MA20 金叉过滤（S30：过滤短期下行假突破）
        if self.use_ma_cross:
            ma5_val = df.loc[prev, 'ma5']
            ma20_val = df.loc[prev, 'ma20']
            if pd.isna(ma5_val) or pd.isna(ma20_val) or ma5_val <= ma20_val:
                return

        # ── 信号触发 → 今日开盘进场 ──
        entry_price = self._buy_price(df.loc[i, 'open'])
        atr = df.loc[prev, 'atr']
        if pd.isna(atr) or atr <= 0:
            return

        # 初始止损
        stop = min(ns.b_price - self.stop_mult * atr, ns.b_price * self.stop_floor_post_break)

        # 仓位计算 (S25: 使用动态权益)
        equity_for_sizing = current_equity if self.use_dynamic_equity else self.capital_per_symbol
        shares_per_unit = self._calc_shares(equity_for_sizing, entry_price, atr)
        if shares_per_unit <= 0:
            return

        pos.active = True
        pos.entry_idx = i
        pos.entry_price = entry_price
        pos.stop_loss = stop
        pos.d_price = ns.d_price
        pos.b_price = ns.b_price
        pos.a_price = ns.a_price
        pos.units = 1
        pos.shares_per_unit = shares_per_unit
        pos.total_cost = entry_price * shares_per_unit
        pos.highest_since_entry = entry_price
        pos.next_add_level = entry_price + self.add_step * atr
        pos.d_broken = entry_price > ns.d_price
        pos.trade = Trade(
            symbol=symbol,
            entry_idx=i,
            entry_price=entry_price,
            units=1,
            a_price=ns.a_price,
            b_price=ns.b_price,
            d_price=ns.d_price,
        )

        if verbose:
            ma_label = f"MA{self.ma_trend}={prev_ma:.3f}" if self.ma_trend > 0 else "无趋势过滤"
            print(f"  🟢 进场 [{symbol}]  idx={i}  "
                  f"价格={entry_price:.3f}  B={ns.b_price:.3f}  "
                  f"D={ns.d_price:.3f}  A={ns.a_price:.3f}  "
                  f"{ma_label}  止损={stop:.3f}  "
                  f"股数={shares_per_unit}")

    def _manage_position(self, df: pd.DataFrame, i: int,
                         pos: PositionState, trades: list[Trade],
                         verbose: bool,
                         max_total_exposure: float | None = None,
                         current_equity: float | None = None,
                         other_position_value: float = 0.0):
        """管理已有持仓：止损 / D 点突破 / 加仓 / 跟踪止损。

        Parameters
        ----------
        max_total_exposure : float | None
            组合最大敞口比例，仅在 run_portfolio 中传入。
        current_equity : float | None
            组合当前总权益，用于敞口计算。
        other_position_value : float
            其他品种的持仓市值。
        """
        low = df.loc[i, 'low']
        high = df.loc[i, 'high']
        close = df.loc[i, 'close']

        def _check_exposure(add_cost: float) -> bool:
            """检查加仓后是否超出组合敞口限制。"""
            if max_total_exposure is None or current_equity is None:
                return True
            if current_equity <= 0:
                return False
            total_pos_value = (pos.units * pos.shares_per_unit * close
                               + other_position_value)
            new_exposure = (total_pos_value + add_cost) / current_equity
            return new_exposure <= max_total_exposure

        # 更新最高价
        if close > pos.highest_since_entry:
            pos.highest_since_entry = close

        total_shares = pos.units * pos.shares_per_unit

        # ── 1. 止损检查 ──
        # S39: D 突破后不再通过 stop_loss 退出（MA20 出场接管）
        #      D 突破前由初始止损 + trail_pre_d 跟踪管理
        if not pos.d_broken and low <= pos.stop_loss:
            exit_price = self._sell_price(min(close, pos.stop_loss))
            pos.trade.exit_idx = i
            pos.trade.exit_price = exit_price
            pos.trade.exit_reason = "初始止损"
            avg_cost = pos.total_cost / total_shares if total_shares > 0 else pos.entry_price
            gross_pnl = (exit_price - avg_cost) * total_shares
            commission = (self._commission_cost(avg_cost, total_shares)
                          + self._commission_cost(exit_price, total_shares))
            pos.trade.pnl = gross_pnl - commission
            pos.trade.units = pos.units
            trades.append(pos.trade)
            if verbose:
                print(f"  🔴 {pos.trade.exit_reason} [{i}]  价格={exit_price:.3f}  止损={pos.stop_loss:.3f}  "
                      f"盈亏={pos.trade.pnl:.0f}")
            pos.active = False
            self._setup_reentry(pos)
            return

        # ── 2. D 点突破前 ──
        held_days = i - pos.entry_idx
        if not pos.d_broken:
            # D 点超时退出（S30：持仓 > d_timeout_days 天仍未突破 D）
            if held_days > self.d_timeout_days:
                exit_price = self._sell_price(close)
                pos.trade.exit_idx = i
                pos.trade.exit_price = exit_price
                pos.trade.exit_reason = "D点超时"
                avg_cost = pos.total_cost / total_shares if total_shares > 0 else pos.entry_price
                gross_pnl = (exit_price - avg_cost) * total_shares
                commission = (self._commission_cost(avg_cost, total_shares)
                              + self._commission_cost(exit_price, total_shares))
                pos.trade.pnl = gross_pnl - commission
                pos.trade.units = pos.units
                trades.append(pos.trade)
                if verbose:
                    print(f"  ⏰ D点超时 [{i}]  持仓{held_days}天  价格={exit_price:.3f}  "
                          f"盈亏={pos.trade.pnl:.0f}")
                pos.active = False
                self._setup_reentry(pos)
                return

            if close > pos.d_price:
                pos.d_broken = True
                atr_val = df.loc[i, 'atr']
                if not pd.isna(atr_val) and atr_val > 0:
                    # D点突破后止损上移：不低于当前止损
                    new_stop = min(
                        pos.d_price - self.stop_mult * atr_val,
                        pos.b_price * self.stop_floor_post_break
                    )
                    pos.stop_loss = max(pos.stop_loss, new_stop)
                    if pos.units < self.max_units:
                        add_cost = close * pos.shares_per_unit
                        if _check_exposure(add_cost):
                            pos.units += 1
                            pos.total_cost += close * pos.shares_per_unit
                            pos.next_add_level = (pos.entry_price
                                                  + pos.units * self.add_step * atr_val)
                            pos.stop_loss = max(pos.stop_loss, close * self.stop_floor_post_break)
                            if verbose:
                                print(f"  ➕ D点突破加仓 [{i}]  价格={close:.3f}  "
                                      f"单位={pos.units}/{self.max_units}")
                if verbose:
                    print(f"  🟡 突破 D [{i}]  价格={close:.3f}  D={pos.d_price:.3f}  "
                          f"止损调整至 {pos.stop_loss:.3f}")
            else:
                # S39: D突破前主动跟踪止损（趋势未确认，亏不起）
                atr_val = df.loc[i, 'atr']
                if not pd.isna(atr_val) and atr_val > 0:
                    trail_stop = close - self.trail_pre_d * atr_val
                    pos.stop_loss = max(pos.stop_loss, trail_stop)
                # 地板收紧 0.93→0.95
                b_floor = pos.b_price * self.stop_floor_pre_break
                if pos.stop_loss < b_floor:
                    pos.stop_loss = b_floor
            return

        # ── 3. D 突破后：MA20 趋势出场 + 加仓 ──
        if self.use_ma_exit:
            # S39: MA20 趋势出场 + D 点地板（趋势已确认，让利润跑）
            ma20 = df.loc[i, 'ma20']
            d_floor = pos.d_price * self.d_exit_floor
            if self.ma_exit_confirm > 0:
                # 连续 N 日收盘 < MA20 = 有效跌破
                ma_trigger = not pd.isna(ma20) and close < ma20
                if ma_trigger and self.ma_exit_confirm > 1:
                    for offset in range(1, self.ma_exit_confirm):
                        prev_i = i - offset
                        if prev_i < 0:
                            ma_trigger = False
                            break
                        prev_ma20 = df.loc[prev_i, 'ma20']
                        if pd.isna(prev_ma20) or df.loc[prev_i, 'close'] >= prev_ma20:
                            ma_trigger = False
                            break
            elif self.ma_exit_confirm < 0:
                # K线实体 1/3 法：MA20 穿过实体且 ≥1/3 在 MA20 之下
                open_i = df.loc[i, 'open']
                body_low = min(open_i, close)
                body_high = max(open_i, close)
                body_range = body_high - body_low
                if not pd.isna(ma20) and body_range > 0 and body_low < ma20 < body_high:
                    below_ratio = (ma20 - body_low) / body_range
                    ma_trigger = below_ratio >= 1.0 / 3.0
                elif not pd.isna(ma20) and body_range == 0:
                    ma_trigger = close < ma20  # 十字星，简单判断
                else:
                    ma_trigger = False
            else:
                ma_trigger = not pd.isna(ma20) and close < ma20 * self.ma_exit_margin
            # 阴线过滤：收盘 < 开盘（空方主导）才确认有效跌破
            if ma_trigger and self.ma_exit_bearish:
                if close >= df.loc[i, 'open']:  # 阳线 → 买方还在，不触发
                    ma_trigger = False

            d_trigger = close < d_floor

            if ma_trigger or d_trigger:
                exit_price = self._sell_price(close)
                pos.trade.exit_idx = i
                pos.trade.exit_price = exit_price
                pos.trade.exit_reason = "MA20出场" if ma_trigger else "D点地板"
                avg_cost = pos.total_cost / total_shares if total_shares > 0 else pos.entry_price
                gross_pnl = (exit_price - avg_cost) * total_shares
                commission = (self._commission_cost(avg_cost, total_shares)
                              + self._commission_cost(exit_price, total_shares))
                pos.trade.pnl = gross_pnl - commission
                pos.trade.units = pos.units
                trades.append(pos.trade)
                if verbose:
                    reason = pos.trade.exit_reason
                    print(f"  🔵 {reason} [{i}]  价格={exit_price:.3f}  "
                          f"MA20={ma20:.3f}  D地板={d_floor:.3f}  盈亏={pos.trade.pnl:.0f}")
                pos.active = False
                self._setup_reentry(pos)
                return
        else:
            # 旧版三阶段 ATR 跟踪止损（use_ma_exit=False 时启用）
            atr = df.loc[i, 'atr']
            if pd.isna(atr) or atr <= 0:
                return

            avg_cost = pos.total_cost / total_shares if total_shares > 0 else pos.entry_price
            profit_pct = (close - avg_cost) / avg_cost if avg_cost > 0 else 0

            if profit_pct > 0.25:
                trail_mult = self.trail_mult_tight
            elif held_days > 30 or profit_pct > 0.15:
                trail_mult = self.trail_mult
            else:
                trail_mult = self.trail_mult_wide

            new_stop = high - trail_mult * atr
            pos.stop_loss = max(pos.stop_loss, new_stop)

        # 加仓：每 2N 加一个单位（两套出场模式共用）
        if pos.units < self.max_units and close >= pos.next_add_level:
            atr = df.loc[i, 'atr']
            if pd.isna(atr) or atr <= 0:
                return
            add_cost = close * pos.shares_per_unit
            if _check_exposure(add_cost):
                pos.units += 1
                pos.total_cost += close * pos.shares_per_unit
                pos.next_add_level = pos.entry_price + pos.units * self.add_step * atr
                # 加仓后上移止损保护新增仓位
                pos.stop_loss = max(pos.stop_loss, close * self.stop_floor_pre_break)
                if verbose:
                    print(f"  ➕ 加仓 [{i}]  价格={close:.3f}  "
                          f"单位={pos.units}/{self.max_units}  "
                          f"股数={pos.units * pos.shares_per_unit}")

    def _setup_reentry(self, pos: PositionState):
        """在平仓时保存 N 字结构信息，允许再进场。"""
        if self.max_reentries <= 0:
            return
        if pos.reentry_count >= self.max_reentries:
            return
        pos.reentry_eligible = True
        pos.reentry_b_price = pos.b_price
        pos.reentry_d_price = pos.d_price
        pos.reentry_a_price = pos.a_price
        pos.reentry_count += 1

    def _execute_reentry(self, df: pd.DataFrame, i: int,
                          pos: PositionState, trades: list[Trade],
                          symbol: str, verbose: bool,
                          current_equity: float | None = None):
        """在平仓后的下一 bar 执行再进场（如果条件满足）。"""
        if i < 1:
            pos.reentry_eligible = False
            return

        if current_equity is None:
            current_equity = self.capital_per_symbol

        prev = i - 1
        prev_close = df.loc[prev, 'close']

        # 条件：价格仍在 B 点之上
        if prev_close <= pos.reentry_b_price:
            pos.reentry_eligible = False
            return

        # S37 进场确认延迟（与正常进场一致）
        if self.entry_confirm_bars > 1:
            for offset in range(1, self.entry_confirm_bars):
                check_idx = prev - offset
                if check_idx < 0:
                    pos.reentry_eligible = False
                    return
                if df.loc[check_idx, 'close'] <= pos.reentry_b_price:
                    pos.reentry_eligible = False
                    return

        # 趋势过滤（与进场一致）
        if self.ma_trend > 0:
            prev_ma = df.loc[prev, 'ma_trend']
            if pd.isna(prev_ma) or prev_close <= prev_ma:
                pos.reentry_eligible = False
                return

        # MA5×MA20 金叉过滤（S30）
        if self.use_ma_cross:
            ma5_val = df.loc[prev, 'ma5']
            ma20_val = df.loc[prev, 'ma20']
            if pd.isna(ma5_val) or pd.isna(ma20_val) or ma5_val <= ma20_val:
                pos.reentry_eligible = False
                return

        # 进场
        entry_price = self._buy_price(df.loc[i, 'open'])
        atr = df.loc[prev, 'atr']
        if pd.isna(atr) or atr <= 0:
            pos.reentry_eligible = False
            return

        stop = min(pos.reentry_b_price - self.stop_mult * atr,
                   pos.reentry_b_price * self.stop_floor_post_break)

        equity_for_sizing = current_equity if self.use_dynamic_equity else self.capital_per_symbol
        shares_per_unit = self._calc_shares(equity_for_sizing, entry_price, atr)
        if shares_per_unit <= 0:
            pos.reentry_eligible = False
            return

        # 重置持仓状态（保留再进场计数器）
        pos.active = True
        pos.entry_idx = i
        pos.entry_price = entry_price
        pos.stop_loss = stop
        pos.d_price = pos.reentry_d_price
        pos.b_price = pos.reentry_b_price
        pos.a_price = pos.reentry_a_price
        pos.units = 1
        pos.shares_per_unit = shares_per_unit
        pos.total_cost = entry_price * shares_per_unit
        pos.highest_since_entry = entry_price
        pos.next_add_level = entry_price + self.add_step * atr
        pos.d_broken = entry_price > pos.reentry_d_price
        pos.trade = Trade(
            symbol=symbol,
            entry_idx=i,
            entry_price=entry_price,
            units=1,
            a_price=pos.reentry_a_price,
            b_price=pos.reentry_b_price,
            d_price=pos.reentry_d_price,
        )

        if verbose:
            ma_label = f"MA{self.ma_trend}={prev_ma:.3f}" if self.ma_trend > 0 else "无趋势过滤"
            print(f"  🔄 再进场 [{symbol}]  idx={i}  "
                  f"价格={entry_price:.3f}  B={pos.reentry_b_price:.3f}  "
                  f"D={pos.reentry_d_price:.3f}  "
                  f"{ma_label}  止损={stop:.3f}  "
                  f"第{pos.reentry_count}次")

    def run_on_multi(self, dfs: dict[str, pd.DataFrame],
                     verbose: bool = True) -> Tuple[dict[str, list[Trade]], dict[str, pd.Series]]:
        """对多个品种执行回测（独立资金池，各自为战）。"""
        results = {}
        equity_curves = {}
        for symbol, df in dfs.items():
            if verbose:
                print(f"\n{'='*60}")
                print(f"📊 {symbol}")
                print(f"{'='*60}")
            _, trades, equity = self.run(df, symbol=symbol, verbose=verbose)
            results[symbol] = trades
            equity_curves[symbol] = equity
        return results, equity_curves

    # ════════════════════════════════════════════════════════════
    #  S26 组合回测 — 共享资金池
    # ════════════════════════════════════════════════════════════

    def run_portfolio(self, dfs: dict[str, pd.DataFrame],
                      max_total_exposure: float = 1.5,
                      verbose: bool = True) -> dict:
        """多品种共享资金池组合回测 (S26)。

        设计要点：
          - 所有品种共享 100,000 初始资金池
          - 同一 bar 多个信号按质量排序分配资金（质量高者优先）
          - 总敞口 ≤ max_total_exposure × 当前权益
          - 熔断机制在组合层面生效（任一品种连续亏损都可能触发）

        Parameters
        ----------
        dfs : dict[str, pd.DataFrame]
            品种代码 → DataFrame（日期应已对齐）。
        max_total_exposure : float
            最大总敞口比例，默认 1.5（150%）。

        Returns
        -------
        dict
            portfolio_equity : pd.Series    组合日频净值
            all_trades : list[Trade]        全部交易记录
            symbol_trades : dict[str, list] 按品种分组的交易
            symbol_equity : dict[str, pd.Series] 按品种的独立净值
            metrics : dict                  组合级业绩指标
        """
        symbols = list(dfs.keys())
        # ── 对齐日期 ──
        common_dates = None
        for sym in symbols:
            dates = set(dfs[sym]['date'])
            if common_dates is None:
                common_dates = dates
            else:
                common_dates = common_dates & dates
        common_dates = sorted(common_dates)

        # ── 预计算指标 ──
        indicators: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            df_aligned = dfs[sym].set_index('date').reindex(common_dates)
            df_aligned.index.name = 'date'
            df_aligned = df_aligned.reset_index()
            # ffill OHLC for gaps
            for col in ['open', 'high', 'low', 'close', 'volume']:
                if col in df_aligned.columns:
                    df_aligned[col] = df_aligned[col].ffill()
            indicators[sym] = self.compute_indicators(df_aligned)

        n = len(common_dates)

        # ── 组合状态 ──
        total_capital = float(self.initial_capital)
        closed_pnl = 0.0  # 已平仓累计盈亏
        all_trades: list[Trade] = []
        symbol_trades: dict[str, list[Trade]] = {s: [] for s in symbols}
        positions: dict[str, PositionState] = {}
        portfolio_equity = np.full(n, np.nan)
        consecutive_losses = 0
        paused_until_bar = -1

        # 日频敞口追踪
        daily_exposure = np.zeros(n)

        # 窗口初期
        portfolio_equity[:self.window_size] = total_capital

        for i in range(self.window_size, n):
            today = common_dates[i]

            # ── 熔断恢复 ──
            if i >= paused_until_bar and paused_until_bar >= 0:
                paused_until_bar = -1

            # ── Step 1: 计算当前权益（持仓管理前） ──
            position_value = sum(
                pos.units * pos.shares_per_unit * indicators[sym].loc[i, 'close']
                for sym, pos in positions.items()
            )
            current_equity = total_capital + closed_pnl + position_value
            portfolio_equity[i] = current_equity

            # ── Step 2: 管理现有持仓 ──
            closed_symbols = []
            for sym, pos in list(positions.items()):
                df_sym = indicators[sym]
                # 实时计算其他品种持仓市值（反映本轮已处理品种的加仓）
                other_value = sum(
                    p.units * p.shares_per_unit * indicators[s].loc[i, 'close']
                    for s, p in positions.items() if s != sym
                )
                self._manage_position(
                    df_sym, i, pos, symbol_trades[sym], False,
                    max_total_exposure=max_total_exposure,
                    current_equity=current_equity,
                    other_position_value=other_value,
                )
                if not pos.active:
                    closed_symbols.append(sym)

            # 处理平仓: 更新累计盈亏和熔断
            # 先汇总同日所有平仓盈亏，再统一更新熔断（避免遍历顺序影响结果）
            daily_closed_pnl = sum(
                symbol_trades[sym][-1].pnl for sym in closed_symbols
            )
            for sym in closed_symbols:
                trade = symbol_trades[sym][-1]
                all_trades.append(trade)
                closed_pnl += trade.pnl
                del positions[sym]

            if daily_closed_pnl < 0:
                consecutive_losses += 1
                if consecutive_losses >= self.max_consecutive_losses:
                    paused_until_bar = i + self.pause_bars
                    if verbose:
                        print(f"  🔴 [{today.date()}] 连续亏损{consecutive_losses}次→熔断{self.pause_bars}根K线")
            elif daily_closed_pnl > 0:
                consecutive_losses = 0

            # ── 刷新持仓市值和权益（Step 2 中可能加仓，position_value 已过期） ──
            position_value = sum(
                pos.units * pos.shares_per_unit * indicators[sym].loc[i, 'close']
                for sym, pos in positions.items()
            )
            current_equity = total_capital + closed_pnl + position_value
            portfolio_equity[i] = current_equity

            # ── 可用资金 = 当前权益 - 已占用市值 ──
            available_cash = current_equity - position_value

            # ── Step 3: 扫描入场信号（熔断中跳过） ──
            if i >= paused_until_bar:
                entry_candidates = []
                for sym in symbols:
                    if sym in positions:
                        continue  # 已有持仓，跳过
                    df_sym = indicators[sym]
                    ns = find_n_structure_in_window(
                        df_sym, i - 1, self.window_size,
                        confirm_k=self.confirm_k, min_advance=self.min_advance,
                        min_gap_ad=self.min_gap_ad, min_gap_db=self.min_gap_db,
                        local_half_window=self.local_half_window,
                    )
                    if ns is None:
                        continue
                    prev = i - 1
                    prev_close = df_sym.loc[prev, 'close']
                    if prev_close <= ns.b_price:
                        continue
                    if self.ma_trend > 0:
                        prev_ma = df_sym.loc[prev, 'ma_trend']
                        if pd.isna(prev_ma) or prev_close <= prev_ma:
                            continue
                    if self.use_ma_cross:
                        ma5_val = df_sym.loc[prev, 'ma5']
                        ma20_val = df_sym.loc[prev, 'ma20']
                        if pd.isna(ma5_val) or pd.isna(ma20_val) or ma5_val <= ma20_val:
                            continue
                    atr = df_sym.loc[prev, 'atr']
                    if pd.isna(atr) or atr <= 0:
                        continue
                    # 信号质量 = (突破幅度) / ATR
                    quality = (prev_close - ns.b_price) / atr
                    entry_candidates.append((quality, sym, ns, atr))

                # 按质量降序排列
                entry_candidates.sort(key=lambda x: x[0], reverse=True)

                # ── Step 4: 分配资金 ──
                available_candidates = len(entry_candidates)
                for rank, (quality, sym, ns, atr_val) in enumerate(entry_candidates):
                    # 计算可分配资金
                    if available_candidates > 0:
                        capital_per_signal = available_cash / available_candidates
                    else:
                        capital_per_signal = available_cash

                    if capital_per_signal <= 0:
                        break

                    entry_price = self._buy_price(indicators[sym].loc[i, 'open'])
                    equity_for_size = (current_equity
                                       if self.use_dynamic_equity
                                       else self.capital_per_symbol * len(symbols))
                    shares = self._calc_shares(
                        min(capital_per_signal, equity_for_size),
                        entry_price, atr_val,
                    )
                    if shares <= 0:
                        available_candidates -= 1
                        continue

                    cost = shares * entry_price
                    # 总敞口检查
                    new_exposure = (position_value + cost) / current_equity if current_equity > 0 else 1.0
                    if new_exposure > max_total_exposure:
                        if rank == 0 and verbose:
                            pass  # 第一个信号就超限，静默跳过
                        available_candidates -= 1
                        continue

                    # ── 执行入场 ──
                    stop = min(ns.b_price - self.stop_mult * atr_val,
                              ns.b_price * self.stop_floor_post_break)
                    pos = PositionState()
                    pos.active = True
                    pos.entry_idx = i
                    pos.entry_price = entry_price
                    pos.stop_loss = stop
                    pos.d_price = ns.d_price
                    pos.b_price = ns.b_price
                    pos.a_price = ns.a_price
                    pos.units = 1
                    pos.shares_per_unit = shares
                    pos.total_cost = entry_price * shares
                    pos.highest_since_entry = entry_price
                    pos.next_add_level = entry_price + self.add_step * atr_val
                    pos.d_broken = entry_price > ns.d_price
                    pos.trade = Trade(
                        symbol=sym, entry_idx=i, entry_price=entry_price,
                        units=1, a_price=ns.a_price, b_price=ns.b_price,
                        d_price=ns.d_price,
                    )
                    positions[sym] = pos
                    available_cash -= cost
                    available_candidates -= 1
                    position_value += cost  # 同步更新，确保后续信号敞口检查正确

                    if verbose:
                        print(f"  🟢 [{today.date()}] {sym} 进场 "
                              f"价格={entry_price:.3f} B={ns.b_price:.3f} "
                              f"质量={quality:.3f} 股数={shares}")

            # ── 记录敞口 ──
            position_value = sum(
                pos.units * pos.shares_per_unit * indicators[sym].loc[i, 'close']
                for sym, pos in positions.items()
            )
            daily_exposure[i] = (position_value / portfolio_equity[i]
                                 if portfolio_equity[i] > 0 else 0)

        # ── 构建返回值 ──
        equity_s = pd.Series(portfolio_equity, index=common_dates).ffill()
        equity_s.iloc[:self.window_size] = total_capital

        # 按品种净值（从交易记录反推累计已实现盈亏）
        sym_equity = {}
        per_symbol_capital = total_capital / len(symbols)
        for sym in symbols:
            s_trades = symbol_trades[sym]
            eq = np.full(n, np.nan)
            eq[:self.window_size] = per_symbol_capital
            running = per_symbol_capital
            if s_trades:
                # 构建 bar → 累计已实现盈亏 的映射
                pnl_by_bar: dict[int, float] = {}
                for t in s_trades:
                    if t.exit_idx >= 0:
                        pnl_by_bar[t.exit_idx] = pnl_by_bar.get(t.exit_idx, 0.0) + t.pnl
                for ti in range(self.window_size, n):
                    if ti in pnl_by_bar:
                        running += pnl_by_bar[ti]
                    eq[ti] = running
            else:
                eq[self.window_size:] = per_symbol_capital
            sym_equity[sym] = pd.Series(eq, index=common_dates).ffill()

        return {
            'portfolio_equity': equity_s,
            'all_trades': all_trades,
            'symbol_trades': symbol_trades,
            'daily_exposure': pd.Series(daily_exposure, index=common_dates),
        }
