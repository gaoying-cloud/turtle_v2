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
    window_size: int = 100,
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

    if last_bar - start < 30:
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
            a_idx = int(a_slice.idxmin())  # type: ignore[arg-type]
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

    def __init__(
        self,
    window_size: int = 100,
    atr_period: int = 25,
    stop_mult: float = 1.5,
    trail_mult: float = 5.0,    # 跟踪止损 ATR 倍数
    add_step: float = 2.0,      # 加仓间隔（ATR 倍数），每 2N 加仓一次
    max_units: int = 6,         # 最大单位数：1 初始 + 5 次加仓（S22 调优）
    ma_trend: int = 0,          # 0=关闭趋势过滤（S22 调优）
    ma_confirm: int = 5,
    use_ma5_confirm: bool = False,  # 关闭 MA5 确认（S22 调优）
    initial_capital: float = 100000.0,
    risk_per_trade: float = 0.01,
    max_reentries: int = 0,     # 0=关闭, N=最多再进场N次
    num_symbols: int = 6,       # 品种数，用于资金分配
    # ── S24 形态识别参数 ──
    confirm_k: int = 2,              # 极值确认延迟（K线数）
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
    ):
        self.window_size = window_size
        self.atr_period = atr_period
        self.stop_mult = stop_mult
        self.trail_mult = trail_mult
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
        return result

    def _calc_shares(self, equity: float, price: float, atr: float) -> int:
        """海龟风格仓位计算。

        risk_amount = equity × risk_per_trade
        per_share_risk = stop_mult × ATR
        shares = risk_amount / per_share_risk，舍入到 100 的倍数
        """
        risk_amount = equity * self.risk_per_trade
        per_share_risk = self.stop_mult * atr
        if per_share_risk <= 0:
            return 0
        theoretical = risk_amount / per_share_risk
        shares = int(theoretical / 100) * 100
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
                    unrealized = ((df.loc[i, 'close'] - pos.entry_price)
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
                    unrealized = ((df.loc[i, 'close'] - pos.entry_price)
                                  * pos.units * pos.shares_per_unit)
                    equity_arr[i] = current_equity + unrealized
                    continue
                pos.reentry_eligible = False

            # ── 正常进场扫描 ──
            self._check_entry_from_prev(df, i, pos, trades, symbol, verbose,
                                        current_equity=sizing_equity)
            if pos.active:
                unrealized = ((df.loc[i, 'close'] - pos.entry_price)
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

        # 2. MA5 辅助确认（只用已闭合 K 线）
        if self.use_ma5_confirm:
            if not ma5_confirm(df, ns, prev):
                return

        # 3. 进场条件：昨日收盘 > B 且（可选）昨日收盘 > 趋势 MA
        prev_close = df.loc[prev, 'close']

        if prev_close <= ns.b_price:
            return

        # 趋势过滤（ma_trend <= 0 表示关闭）
        if self.ma_trend > 0:
            prev_ma = df.loc[prev, 'ma_trend']
            if pd.isna(prev_ma) or prev_close <= prev_ma:
                return

        # ── 信号触发 → 今日开盘进场 ──
        entry_price = self._buy_price(df.loc[i, 'open'])
        atr = df.loc[prev, 'atr']
        if pd.isna(atr) or atr <= 0:
            return

        # 初始止损
        stop = min(ns.b_price - self.stop_mult * atr, ns.b_price * 0.95)

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
                         verbose: bool):
        """管理已有持仓：止损 / D 点突破 / 加仓 / 跟踪止损。"""
        low = df.loc[i, 'low']
        high = df.loc[i, 'high']
        close = df.loc[i, 'close']

        # 更新最高价
        if close > pos.highest_since_entry:
            pos.highest_since_entry = close

        total_shares = pos.units * pos.shares_per_unit

        # ── 1. 止损检查（区分 初始止损 / 跟踪止损） ──
        if low <= pos.stop_loss:
            exit_price = self._sell_price(min(close, pos.stop_loss))
            pos.trade.exit_idx = i
            pos.trade.exit_price = exit_price
            pos.trade.exit_reason = "跟踪止损" if pos.d_broken else "初始止损"
            gross_pnl = (exit_price - pos.entry_price) * total_shares
            commission = (self._commission_cost(pos.entry_price, total_shares)
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

        # ── 2. D 点突破判断 ──
        if not pos.d_broken:
            if close > pos.d_price:
                pos.d_broken = True
                atr_val = df.loc[i, 'atr']
                # 止损移到 D 点附近
                if not pd.isna(atr_val) and atr_val > 0:
                    pos.stop_loss = min(
                        pos.d_price - self.stop_mult * atr_val,
                        pos.b_price * 0.95
                    )
                    # 突破 D 点 → 立即加仓 1 个单位
                    if pos.units < self.max_units:
                        pos.units += 1
                        pos.next_add_level = (pos.entry_price
                                              + pos.units * self.add_step * atr_val)
                        if verbose:
                            print(f"  ➕ D点突破加仓 [{i}]  价格={close:.3f}  "
                                  f"单位={pos.units}/{self.max_units}")
                if verbose:
                    print(f"  🟡 突破 D [{i}]  价格={close:.3f}  D={pos.d_price:.3f}  "
                          f"止损调整至 {pos.stop_loss:.3f}")
            else:
                # ① 收盘价跌破 B 点 → N字结构失效，立即平仓
                if close < pos.b_price:
                    exit_price = self._sell_price(close)
                    pos.trade.exit_idx = i
                    pos.trade.exit_price = exit_price
                    pos.trade.exit_reason = "B点结构失效"
                    gross_pnl = (exit_price - pos.entry_price) * total_shares
                    commission = (self._commission_cost(pos.entry_price, total_shares)
                                  + self._commission_cost(exit_price, total_shares))
                    pos.trade.pnl = gross_pnl - commission
                    pos.trade.units = pos.units
                    trades.append(pos.trade)
                    if verbose:
                        print(f"  🔴 B 点结构失效 [{i}]  价格={close:.3f}  "
                              f"B={pos.b_price:.3f}  盈亏={pos.trade.pnl:.0f}")
                    pos.active = False
                    self._setup_reentry(pos)
                    return
                # ② 超过 5 根 K 线未突破 D 点 → 超时平仓
                bars_since_entry = i - pos.entry_idx
                if bars_since_entry > 5:
                    exit_price = self._sell_price(close)
                    pos.trade.exit_idx = i
                    pos.trade.exit_price = exit_price
                    pos.trade.exit_reason = "D点突破失败"
                    gross_pnl = (exit_price - pos.entry_price) * total_shares
                    commission = (self._commission_cost(pos.entry_price, total_shares)
                                  + self._commission_cost(exit_price, total_shares))
                    pos.trade.pnl = gross_pnl - commission
                    pos.trade.units = pos.units
                    trades.append(pos.trade)
                    if verbose:
                        print(f"  🔴 D 突破失败 [{i}]  价格={close:.3f}  "
                              f"D={pos.d_price:.3f}  盈亏={pos.trade.pnl:.0f}")
                    pos.active = False
                    self._setup_reentry(pos)
                    return
            return

        # ── 3. 已突破 D：跟踪止损 + 加仓 ──
        atr = df.loc[i, 'atr']
        if pd.isna(atr) or atr <= 0:
            return

        new_stop = high - self.trail_mult * atr
        pos.stop_loss = max(pos.stop_loss, new_stop)

        # 加仓：每 2N 加一个单位
        if pos.units < self.max_units and close >= pos.next_add_level:
            pos.units += 1
            pos.next_add_level = pos.entry_price + pos.units * self.add_step * atr
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

        # 趋势过滤（与进场一致）
        if self.ma_trend > 0:
            prev_ma = df.loc[prev, 'ma_trend']
            if pd.isna(prev_ma) or prev_close <= prev_ma:
                pos.reentry_eligible = False
                return

        # 进场
        entry_price = self._buy_price(df.loc[i, 'open'])
        atr = df.loc[prev, 'atr']
        if pd.isna(atr) or atr <= 0:
            pos.reentry_eligible = False
            return

        stop = min(pos.reentry_b_price - self.stop_mult * atr,
                   pos.reentry_b_price * 0.95)

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
        """对多个品种执行回测。

        Parameters
        ----------
        dfs : dict[str, pd.DataFrame]
            品种代码 → DataFrame 的映射。

        Returns
        -------
        (all_trades, all_equity)
            all_trades: 品种代码 → 交易记录列表
            all_equity: 品种代码 → 日频权益曲线
        """
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
