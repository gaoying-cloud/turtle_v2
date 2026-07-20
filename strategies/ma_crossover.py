"""
MA 交叉趋势跟踪策略 — 纯 pandas/numpy 实现（验证实验）

核心逻辑：
  1. 进场信号：close > MA120（慢线确认长期上升趋势）
  2. 离场信号：close < MA60（快线破位，趋势结束）
                OR close < 进场价格（保本止损）

设计理念：
  - 极简趋势跟踪：用双均线捕捉中长期趋势，保本止损防回撤侵蚀
  - 无 ATR 仓位管理、无加仓、无复杂出场逻辑
  - 纯 SMA，不做指数平滑

参数：
    ma_slow       : int   = 120   — 慢线周期（进场过滤器）
    ma_fast       : int   = 60    — 快线周期（出场信号）
    position_pct  : float = 0.20  — 单品种仓位比例
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

def compute_ma(close: pd.Series, period: int) -> pd.Series:
    """简单移动平均。"""
    return close.rolling(window=period, min_periods=period).mean()


# ════════════════════════════════════════════════════════════
#  数据结构
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
    pnl: float = 0.0


@dataclass
class PositionState:
    """持仓状态。"""
    active: bool = False
    entry_idx: int = -1
    entry_price: float = 0.0
    shares: int = 0


# ════════════════════════════════════════════════════════════
#  主策略
# ════════════════════════════════════════════════════════════

class MACrossoverStrategy:
    """MA 交叉趋势跟踪策略。

    对单个品种的 DataFrame 执行回测，返回交易记录和净值曲线。
    """

    def __init__(
        self,
        ma_slow: int = 120,           # 慢线周期（进场）
        ma_fast: int = 60,            # 快线周期（出场）
        position_pct: float = 0.20,   # 单品种仓位比例
        stop_floor: float = 0.0,      # 止损地板比例（0=保本进场价, 0.95=5%亏损止损, <0=关闭）
        initial_capital: float = 100000.0,
        num_symbols: int = 6,         # 品种数，用于资金分配
        slippage_pct: float = 0.001,     # 成交滑点 (0.1%)
        commission_pct: float = 0.00015, # 手续费率 (0.015%, ETF 万1.5)
    ):
        self.ma_slow = ma_slow
        self.ma_fast = ma_fast
        self.position_pct = position_pct
        self.stop_floor = stop_floor
        self.initial_capital = initial_capital
        self.capital_per_symbol = initial_capital / max(1, num_symbols)
        self.slippage_pct = slippage_pct
        self.commission_pct = commission_pct
        self.num_symbols = num_symbols

        # 最小预热期：慢线周期 + 1（确保 MA 有效）
        self.min_warmup = ma_slow + 1

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
        result['ma_slow'] = compute_ma(result['close'], self.ma_slow)
        result['ma_fast'] = compute_ma(result['close'], self.ma_fast)
        return result

    def _calc_shares(self, equity: float, price: float) -> int:
        """按仓位比例计算股数，舍入到 100 的倍数，下限 100 股。"""
        max_cost = equity * self.position_pct
        shares = int(max_cost / price / 100) * 100
        return max(100, shares)

    def run(self, df: pd.DataFrame, symbol: str = "",
            verbose: bool = True) -> Tuple[pd.DataFrame, list[Trade], pd.Series]:
        """对单品种执行 MA 趋势跟踪策略回测。

        进场规则（防偷价）：
          - 用 bar i-1 的收盘价判断信号是否触发
          - 在 bar i 的开盘价执行进场（次日开盘进场）

        离场规则（防偷价）：
          - 用 bar i-1 的收盘价判断离场条件
          - 在 bar i 的开盘价执行离场（次日开盘离场）

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

        # ── 日频权益追踪 ──
        equity_arr = np.full(n, np.nan)
        current_equity = float(self.capital_per_symbol)  # 已实现权益（现金）

        # 窗口初期：无交易，权益 = 初始资金
        equity_arr[:self.min_warmup] = self.capital_per_symbol

        for i in range(self.min_warmup, n):
            # ── 持仓管理 ──
            if pos.active:
                self._check_exit(df, i, pos, trades, verbose)
                if pos.active:
                    # 仍在持仓：计算未实现权益
                    unrealized = (df.loc[i, 'close'] - pos.entry_price) * pos.shares
                    equity_arr[i] = current_equity + unrealized
                else:
                    # 刚平仓：更新已实现权益
                    current_equity += trades[-1].pnl
                    equity_arr[i] = current_equity
                continue

            # ── 空仓：检查进场信号 ──
            equity_arr[i] = current_equity
            self._check_entry(df, i, pos, trades, symbol, verbose,
                              current_equity=current_equity)
            if pos.active:
                unrealized = (df.loc[i, 'close'] - pos.entry_price) * pos.shares
                equity_arr[i] = current_equity + unrealized

        # 填充前导 NaN + 构建 Series
        equity_filled = pd.Series(equity_arr, index=df.index).ffill()
        equity_curve = pd.Series(equity_filled.values, index=df['date'])

        return df, trades, equity_curve

    def _check_entry(self, df: pd.DataFrame, i: int,
                     pos: PositionState, trades: list[Trade],
                     symbol: str, verbose: bool,
                     current_equity: float):
        """用 bar i-1 的数据检查进场信号，在 bar i 的开盘进场。

        进场条件（在 bar i-1 判断）：
          close > MA120（慢线）
        """
        if i < 1:
            return

        prev = i - 1
        prev_close = df.loc[prev, 'close']
        prev_ma_slow = df.loc[prev, 'ma_slow']

        # 检查 MA 是否有效（预热期内为 NaN）
        if pd.isna(prev_ma_slow):
            return

        # 进场条件：close > MA120
        if prev_close <= prev_ma_slow:
            return

        # ── 信号触发 → 今日开盘进场 ──
        entry_price = self._buy_price(df.loc[i, 'open'])
        shares = self._calc_shares(current_equity, entry_price)
        if shares <= 0:
            return

        pos.active = True
        pos.entry_idx = i
        pos.entry_price = entry_price
        pos.shares = shares

        if verbose:
            print(f"  🟢 进场 [{symbol}]  idx={i}  "
                  f"价格={entry_price:.3f}  MA120={prev_ma_slow:.3f}  "
                  f"股数={shares}")

    def _check_exit(self, df: pd.DataFrame, i: int,
                    pos: PositionState, trades: list[Trade],
                    verbose: bool):
        """用 bar i-1 的数据检查离场信号，在 bar i 的开盘离场。

        离场条件（在 bar i-1 判断，OR 关系）：
          close < MA60（趋势破位）
          close < 止损地板（stop_floor × 进场价）
            - stop_floor=0: 保本止损（close < 进场价）
            - stop_floor=0.95: 5% 亏损止损
            - stop_floor<0: 关闭止损
        """
        if i < 1:
            return

        prev = i - 1
        prev_close = df.loc[prev, 'close']
        prev_ma_fast = df.loc[prev, 'ma_fast']

        exit_reason = None

        # 条件1: 止损地板（优先级最高）
        if self.stop_floor >= 0:
            stop_price = pos.entry_price * self.stop_floor if self.stop_floor > 0 else pos.entry_price
            if prev_close < stop_price:
                exit_reason = "保本止损" if self.stop_floor == 0 else f"止损地板({self.stop_floor:.0%})"
        # 条件2: close < MA60（趋势破位）
        if exit_reason is None and not pd.isna(prev_ma_fast) and prev_close < prev_ma_fast:
            exit_reason = "MA60破位"

        if exit_reason is None:
            return

        # ── 信号触发 → 今日开盘离场 ──
        exit_price = self._sell_price(df.loc[i, 'open'])

        # 计算盈亏
        gross_pnl = (exit_price - pos.entry_price) * pos.shares
        commission = (self._commission_cost(pos.entry_price, pos.shares)
                      + self._commission_cost(exit_price, pos.shares))
        pnl = gross_pnl - commission

        trade = Trade(
            symbol="",
            entry_idx=pos.entry_idx,
            entry_price=pos.entry_price,
            exit_idx=i,
            exit_price=exit_price,
            exit_reason=exit_reason,
            pnl=pnl,
        )
        trades.append(trade)
        pos.active = False

        if verbose:
            emoji = "🟢" if pnl > 0 else "🔴"
            print(f"  {emoji} {exit_reason} [{i}]  价格={exit_price:.3f}  "
                  f"进场价={pos.entry_price:.3f}  MA60={prev_ma_fast:.3f}  "
                  f"盈亏={pnl:+.0f}")

    def run_on_multi(self, dfs: dict[str, pd.DataFrame],
                     verbose: bool = True) -> Tuple[dict[str, list[Trade]], dict[str, pd.Series]]:
        """对多个品种执行回测（独立资金池）。"""
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
