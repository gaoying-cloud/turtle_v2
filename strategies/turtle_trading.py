"""
跨市场ETF海龟组合策略 · Backtrader 策略层 (S3)

依赖：
    - backtrader>=1.9.78.123
    - s2 turtle_core (纯 pandas，无 Backtrader 依赖)

架构：
    TurtleStrategy (bt.Strategy)
    ├── __init__(): 通过 S2 TurtleSignals 预计算所有信号序列
    ├── next(): 逐日迭代
    │   ├── _check_exits()     — 止损/退出
    │   ├── _check_entries()   — 突破入场 + 55日过滤 + SignalFilter
    │   ├── _check_pyramid()   — 加仓
    │   └── _bond_switch()     — 空仓→国债ETF 切换
    └── stop(): 输出统计
"""

from __future__ import annotations

import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import backtrader as bt
import numpy as np
import pandas as pd

# ── S2 海龟核心 ──
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.turtle_core import (
    TurtleSignals,
    TurtlePositions,
    SignalFilter,
    calc_position_size,
    calc_fixed_stop,
    calc_trailing_stop,
    calc_pyramid_trigger,
    pyramid_add,
    should_activate_trailing_stop,
    Position,
)
from src.risk_parity import compute_alpha_weights

logger = logging.getLogger(__name__)

# ── T+1 品种（A 股 ETF） ──
T_PLUS_ONE_SYMBOLS = {
    "510500.SH",
    "159845.SZ",
    "159915.SZ",
    "588000.SH",
}


# ════════════════════════════════════════════════════════════
#  TurtleStrategy
# ════════════════════════════════════════════════════════════

class TurtleStrategy(bt.Strategy):
    """海龟交易策略 — Backtrader 实现。

    Parameters
    ----------
    turtle_params : dict
        海龟参数（从 config['turtle'] 读取）。
    symbols : list[str]
        6 只海龟品种代码列表（与 self.datas 顺序一致）。
    use_55_filter : bool
        是否启用 55 日过滤（模式 B）。
    risk_per_unit : float
        每单位风险比例，默认 0.01。
    concentration_trigger : int
        仓位集中度熔断阈值，默认 4。
    max_consecutive_losses : int
        连续亏损暂停阈值，默认 8。
    max_cumulative_loss_pct : float
        累计亏损暂停阈值，默认 0.15。
    pause_days : int
        暂停交易天数，默认 5。
    """

    params = (
        ("turtle_params", None),
        ("symbols", None),
        ("use_55_filter", False),
        ("risk_per_unit", 0.01),
        ("concentration_trigger", 4),
        ("max_consecutive_losses", 8),
        ("max_cumulative_loss_pct", 0.15),
        ("pause_days", 5),
        ("alpha", 0.05),                  # α 风险平价偏移系数
        ("cov_lookback_days", 252),       # 协方差矩阵估计窗口
        ("rebalance_quarterly", True),    # 每季度再平衡
        ("atr_change_threshold", 0.30),   # ATR 变动 30% 强制再平衡
    )

    def __init__(self):
        # ── 初始化 S2 组件 ──
        self._signals: Dict[str, dict] = {}
        self._positions = TurtlePositions(max_units=4)
        self._filter = SignalFilter(max_rejections=3)

        # ── 预计算所有品种的信号序列 ──
        self._close_series: Dict[str, pd.Series] = {}
        signal_calc = TurtleSignals(self.params.turtle_params)
        for i, code in enumerate(self.params.symbols):
            data = self.datas[i]
            # 将 Backtrader lines 转为 pandas Series
            n_bars = len(data.close.array)
            idx = pd.RangeIndex(n_bars)
            high = pd.Series(data.high.array, index=idx)
            low = pd.Series(data.low.array, index=idx)
            close = pd.Series(data.close.array, index=idx)
            self._signals[code] = signal_calc.precompute_all(high, low, close)
            self._close_series[code] = close

        # ── S4 风险平价权重状态 ──
        self._alpha_risk_pcts: Optional[np.ndarray] = None
        self._last_rebalance_day: Optional[date] = None
        self._last_n_values: Dict[str, float] = {}

        # ── 状态字段 ──
        self._risk_events: dict = {}
        self._current_day = None          # 当前交易日（用于 T+1 标记重置）
        self._buy_today: Dict[str, bool] = {}  # T+1 品种当日是否已买入
        self._consecutive_losses: int = 0
        self._cumulative_loss_pct: float = 0.0
        self._paused_until: Optional[date] = None
        self._in_bond: bool = False
        self._bond_data = None
        self._last_equity: float = self.broker.getvalue()
        self._trade_count: int = 0
        self._my_trades: List[dict] = []


    def _next_idx(self, code: str) -> int:
        """获取安全索引，防止 runonce 模式下 len(self) 与信号数组长度不匹配。"""
        idx = len(self) - 1
        if idx < 0:
            return 0
        if code in self._signals and "n" in self._signals[code]:
            max_idx = len(self._signals[code]["n"]) - 1
            if idx > max_idx:
                return max_idx
        return idx

    def _is_new_day(self) -> bool:
        """检测是否进入新的交易日（用于重置 T+1 标记）。"""
        dt = self.datas[0].datetime.date(0)
        if dt != self._current_day:
            self._current_day = dt
            return True
        return False

    def _equity(self) -> float:
        """当前账户总净值（现金 + 持仓市值）。"""
        return self.broker.getvalue()

    # ════════════════════════════════════════════════════════
    #  S4 风险平价权重
    # ════════════════════════════════════════════════════════

    def _should_rebalance_weights(self, today: date) -> bool:
        """判断是否需要重新计算风险平价权重。

        触发条件（任一满足）：
        1. 首次运行（_alpha_risk_pcts 为空）
        2. 每季度末（rebalance_quarterly=True）
        3. 任一品种 ATR 变动超过阈值
        """
        if self._alpha_risk_pcts is None:
            return True

        # 检查 ATR 变动
        if self.params.atr_change_threshold > 0:
            for code in self.params.symbols:
                old_n = self._last_n_values.get(code)
                if old_n is None or old_n <= 0:
                    continue
                idx = self._next_idx(code)
                current_n = self._signals[code]["n"].iloc[idx]
                if pd.isna(current_n) or current_n <= 0:
                    continue
                change_pct = abs(current_n - old_n) / old_n
                if change_pct >= self.params.atr_change_threshold:
                    logger.info("[风险平价] %s ATR 变动 %.1f%% ≥ %.0f%%，触发再平衡",
                                code, change_pct * 100,
                                self.params.atr_change_threshold * 100)
                    return True

        # 检查季度末
        if self.params.rebalance_quarterly and self._last_rebalance_day is not None:
            # 季度：月份为 3, 6, 9, 12
            month = today.month
            if month in (3, 6, 9, 12):
                # 确保同季度只触发一次
                last_q = (self._last_rebalance_day.month - 1) // 3
                this_q = (month - 1) // 3
                if this_q != last_q:
                    logger.info("[风险平价] 季度切换 (%d→%d)，触发再平衡", last_q + 1, this_q + 1)
                    return True

        return False

    def _build_returns_matrix(self) -> np.ndarray:
        """从协方差窗口内构建日收益率矩阵。

        将所有品种在 [idx - cov_lookback, idx] 窗口内的 close 价格
        对齐到公共日期后计算对数收益率。

        Returns
        -------
        np.ndarray, shape (T, N)
            日收益率矩阵。T = 有效交易日数，N = 品种数（6）。
        """
        # 最近 cov_lookback_days + 1 个交易日
        idx = self._next_idx(self.params.symbols[0])
        lookback = self.params.cov_lookback_days
        start = max(0, idx - lookback)

        prices = {}
        for code in self.params.symbols:
            # 从 _close_series 缓存取 close 价格
            series = self._close_series[code].iloc[start:idx + 1].copy()
            series.name = code
            prices[code] = series

        # 对齐到 DataFrame
        df = pd.DataFrame(prices)
        # 计算对数收益率
        returns = np.log(df).diff().dropna()
        return returns.values

    def _recalc_alpha_weights(self):
        """重新计算 α 融合风险平价权重并缓存。"""
        returns = self._build_returns_matrix()
        T, N = returns.shape
        if T < 10 or N < 2:
            # 数据不足时回退到 base_risk_per_unit
            logger.warning("[风险平价] 数据不足 (T=%d, N=%d)，回退到纯 ATR", T, N)
            self._alpha_risk_pcts = None
            return

        result = compute_alpha_weights(
            returns=returns,
            alpha=self.params.alpha,
            base_risk_pct=self.params.risk_per_unit,
        )
        self._alpha_risk_pcts = result["risk_pcts"]

        # 缓存当前 N 值
        for code in self.params.symbols:
            idx = self._next_idx(code)
            n_val = self._signals[code]["n"].iloc[idx]
            if not pd.isna(n_val) and n_val > 0:
                self._last_n_values[code] = n_val

        if result["converged"]:
            rp_str = ", ".join(
                f"{self.params.symbols[i]}: {w:.4f}"
                for i, w in enumerate(result["rp_weights"])
            )
            logger.info("[风险平价] 重新计算完成 (α=%.2f) rp_weights=[%s]",
                        self.params.alpha, rp_str)

    # ════════════════════════════════════════════════════════
    #  next() — 逐日迭代
    # ════════════════════════════════════════════════════════

    def next(self):
        # ── 确保有足够的数据（runonce 模式下第 0 个 bar 可能数据不全）──
        if len(self) < 2:
            return

        # ── 检查是否在暂停期 ──
        if self._paused_until is not None:
            if self.datas[0].datetime.date(0) < self._paused_until:
                return  # 暂停中，跳过所有操作
            self._paused_until = None
            logger.info("[风控] 暂停期结束，恢复交易")

        # ── 重置 T+1 标记（新交易日） ──
        if self._is_new_day():
            self._buy_today.clear()

        # ── Step 0: 检查是否需要重新计算风险平价权重 ──
        today = self.datas[0].datetime.date(0)
        if self._should_rebalance_weights(today):
            self._recalc_alpha_weights()
            self._last_rebalance_day = today

        # ── Step 1: 更新持仓天数 ──
        for pos in self._positions.all_positions():
            pos.holding_days += 1

        # ── Step 2: 逐个品种处理 ──
        n_symbols = len(self.params.symbols)
        for i in range(n_symbols):
            code = self.params.symbols[i]
            data = self.datas[i]
            if not self._positions.has_position(code):
                # 检查入场
                self._check_entry(code, data)
            else:
                pos = self._positions.get(code)

                # 先更新移动止损线
                self._update_trailing_stop(code, pos)

                # 再检查退出
                if self._should_exit(code, data, pos):
                    self._execute_exit(code, data, pos)
                else:
                    # 检查加仓
                    self._check_pyramid(code, data, pos)

        # ── Step 3: 空仓→国债切换（当日所有品种处理完后） ──
        self._bond_switch()

    # ════════════════════════════════════════════════════════
    #  入场
    # ════════════════════════════════════════════════════════

    def _should_enter_short(self, code: str, si: dict, idx: int, close: float, n: float) -> bool:
        """检查是否触发空头入场信号。"""
        dc_low = si.get("entry_low_20")
        if dc_low is None:
            return False
        entry_low = dc_low.iloc[idx]
        return pd.notna(entry_low) and close < entry_low

    def _check_entry(self, code: str, data: bt.feeds.PandasData):
        """检查并执行入场信号。"""
        if self._paused_until is not None:
            return

        idx = self._next_idx(code)
        si = self._signals[code]
        close = data.close[0]
        n = si["n"].iloc[idx]

        if pd.isna(n) or n <= 0:
            return

        # ── 判断方向（多头或空头） ──
        entry_high = si["entry_high_20"].iloc[idx]
        is_long = pd.notna(entry_high) and close > entry_high
        entry_low_20 = si.get("entry_low_20")
        is_short = False
        if entry_low_20 is not None:
            el = entry_low_20.iloc[idx]
            if pd.notna(el) and close < el:
                is_short = True
        if not is_long and not is_short:
            return

        # ── 55日过滤（模式 B，仅多头） ──
        if is_long and self.params.use_55_filter:
            filter_high = si["entry_high_55"].iloc[idx]
            if pd.isna(filter_high) or close <= filter_high:
                return

        # ── 盈利过滤器 ──
        ok, reason = self._filter.check_entry(code, self._positions.has_position(code))
        if not ok:
            logger.debug("[入场] %s 被过滤器拒绝: %s", code, reason)
            return

        # ── S4: α 融合风险权重 ──
        if self._alpha_risk_pcts is not None:
            i = self.params.symbols.index(code)
            base_risk = float(self._alpha_risk_pcts[i])
        else:
            base_risk = self.params.risk_per_unit

        # ── P1: 渐进式集中度熔断 ──
        pos_count = self._positions.count
        fade_table = {0: 1.0, 1: 1.0, 2: 1.0, 3: 0.8, 4: 0.6}
        fade = fade_table.get(pos_count, 0.5)
        risk = base_risk * fade
        if fade < 1.0:
            logger.debug("[入场] %s 仓位集中%d，风险降为 %.2f%% (原 %.2f%%)",
                         code, pos_count, risk * 100, base_risk * 100)

        # ── P2: 滑动窗口累计亏损暂停（检查最近 15 笔） ──
        recent_trades = self._my_trades[-15:] if len(self._my_trades) >= 15 else self._my_trades
        recent_loss_pct = sum(
            abs(t["pnl"]) for t in recent_trades if t["pnl"] < 0
        ) / self.broker.startingcash if self._my_trades else 0.0
        if recent_loss_pct >= self.params.max_cumulative_loss_pct:
            logger.warning("[入场] %s 近%d笔累计亏损 %.2f%% ≥ %.2f%%，禁止开新仓",
                           code, len(recent_trades) if self._my_trades else 15,
                           recent_loss_pct * 100,
                           self.params.max_cumulative_loss_pct * 100)
            return

        # ── P0: 三层敞口校验 ──
        equity = self._equity()
        price = data.close[0]
        if not np.isfinite(risk) or risk <= 0:
            risk = self.params.risk_per_unit
        shares = calc_position_size(equity, n, price, risk)
        if shares == 0:
            return
        # P0: 校验单品种风险敞口 ≤ 4% (max_single_risk=0.04)
        per_share_risk = 2.0 * n
        requested_risk = shares * per_share_risk
        # 已有该品种的敞口
        existing_risk = 0.0
        pos = self._positions.get(code)
        if pos is not None:
            existing_risk = pos.total_shares * 2.0 * pos.n_at_entry
        total_symbol_risk_pct = (existing_risk + requested_risk) / equity if equity > 0 else 0
        if total_symbol_risk_pct > 0.04:
            max_new = equity * 0.04 - existing_risk
            adjusted = int(max_new / per_share_risk / 100) * 100
            if adjusted <= 0:
                logger.debug("[入场] %s 单品种风险敞口已达 4%% (%.2f%%)", code, total_symbol_risk_pct * 100)
                return
            shares = adjusted
        # P0: 校验全账户风险敞口 ≤ 15% (max_total_risk=0.15)
        total_existing_risk = 0.0
        for existing_pos in self._positions.all_positions():
            total_existing_risk += existing_pos.total_shares * 2.0 * existing_pos.n_at_entry
        total_new_risk_pct = (total_existing_risk + shares * per_share_risk) / equity if equity > 0 else 0
        if total_new_risk_pct > 0.15:
            max_new = equity * 0.15 - total_existing_risk
            adjusted = int(max_new / per_share_risk / 100) * 100
            if adjusted <= 0:
                logger.debug("[入场] %s 全账户风险敞口已达 15%% (%.2f%%)", code, total_new_risk_pct * 100)
                return
            shares = adjusted

        # ── T+1 约束：当日已买入的同品种不可再买 ──
        if code in T_PLUS_ONE_SYMBOLS and self._buy_today.get(code, False):
            return

        # ── 执行入场（多头或空头） ──
        dt = data.datetime.date(0)
        direction = "long" if is_long else "short"
        stop_atr_multiple = float(self.params.turtle_params.get("stop_atr_multiple", 2.0))
        stop_loss = calc_fixed_stop(price, n, stop_atr_multiple, direction)

        if is_long:
            self.buy(data=data, size=shares)
            action = "买入"
        else:
            self.sell(data=data, size=shares)
            action = "卖出"

        self._positions.open(
            code,
            system="filtered" if self.params.use_55_filter else "primary",
            direction=direction,
            entry_date=dt,
            entry_price=price,
            shares=shares,
            n_at_entry=n,
            stop_loss=stop_loss,
        )
        if code in T_PLUS_ONE_SYMBOLS:
            self._buy_today[code] = True

        logger.info("[入场] %s → %s %d 股 @ %.4f (N=%.4f SL=%.4f %s)",
                    code, action, shares, price, n, stop_loss, direction)

    # ════════════════════════════════════════════════════════
    #  移动止损（每日更新）
    # ════════════════════════════════════════════════════════

    def _update_trailing_stop(self, code: str, pos: Position):
        """每日更新移动止损线，只上移不下移（多头）或只下移不下移（空头）。

        切换条件（满足任一即可）：
            1. 浮盈 ≥ 2N
            2. 持仓天数 ≥ 20 日
        """
        direction = pos.direction
        idx = self._next_idx(code)
        n = self._signals[code]["n"].iloc[idx]
        if pd.isna(n) or n <= 0:
            return

        if pos.stop_type == "trailing":
            # 已激活移动止损 → 检查是否需要更新止损线
            if direction == "short":
                trail_low = self._signals[code].get("trail_low_10")
                if trail_low is not None:
                    tl = trail_low.iloc[idx]
                    if pd.notna(tl) and tl < pos.trail_high:
                        self._positions.update_trail_high(code, float(tl))
                        new_stop = calc_trailing_stop(
                            float(tl), float(n), pos.stop_loss, direction="short",
                        )
                        if new_stop < pos.stop_loss:
                            self._positions.update_stop_loss(code, new_stop, "trailing")
                            logger.debug("[移动止损] %s 下移至 %.4f (trail_low=%.4f)",
                                         code, new_stop, tl)
            else:
                trail_high = self._signals[code]["trail_high_10"].iloc[idx]
                if pd.notna(trail_high) and trail_high > pos.trail_high:
                    self._positions.update_trail_high(code, float(trail_high))
                    new_stop = calc_trailing_stop(
                        float(trail_high), float(n), pos.stop_loss,
                    )
                    if new_stop > pos.stop_loss:
                        self._positions.update_stop_loss(code, new_stop, "trailing")
                        logger.debug("[移动止损] %s 上移至 %.4f (trail_high=%.4f)",
                                     code, new_stop, trail_high)
        else:
            # 固定止损 → 检查是否应切换为移动止损
            close = self._close_series[code].iloc[
                min(idx, len(self._close_series[code]) - 1)
            ]
            if should_activate_trailing_stop(
                float(close), pos.entry_price, float(n), pos.holding_days,
                direction=pos.direction,
            ):
                if direction == "short":
                    trail_low = self._signals[code].get("trail_low_10")
                    if trail_low is not None:
                        tl = trail_low.iloc[idx]
                        if pd.notna(tl):
                            self._positions.update_trail_high(code, float(tl))
                            new_stop = calc_trailing_stop(
                                float(tl), float(n), pos.stop_loss, direction="short",
                            )
                            self._positions.update_stop_loss(code, new_stop, "trailing")
                            logger.info("[移动止损] %s 空头激活 SL=%.4f", code, new_stop)
                else:
                    trail_high = self._signals[code]["trail_high_10"].iloc[idx]
                    if pd.notna(trail_high):
                        self._positions.update_trail_high(code, float(trail_high))
                        new_stop = calc_trailing_stop(
                            float(trail_high), float(n), pos.stop_loss,
                        )
                        self._positions.update_stop_loss(code, new_stop, "trailing")
                        logger.info("[移动止损] %s 激活 (floatPn=%.2fN, hold=%d天) SL=%.4f",
                                    code,
                                    (float(close) - pos.entry_price) / float(n),
                                    pos.holding_days, new_stop)

    # ════════════════════════════════════════════════════════
    #  退出（止损 + 退出）
    # ════════════════════════════════════════════════════════

    def _should_exit(self, code: str, data: bt.feeds.PandasData, pos: Position) -> bool:
        """判断是否触发退出条件。

        规则（任一满足即退出，取更早触发者）：
            1. 固定止损/移动止损
            2. 10日反向突破
        """
        idx = self._next_idx(code)
        si = self._signals[code]
        high = data.high[0]
        low = data.low[0]
        n = si["n"].iloc[idx]

        if pd.isna(n) or n <= 0:
            return False

        # ── T+1 约束 ──
        if code in T_PLUS_ONE_SYMBOLS and self._buy_today.get(code, False):
            return False

        if pos.direction == "short":
            # 规则1 + 2：空头止损（high >= stop_loss）
            if pos.stop_loss > 0 and high >= pos.stop_loss:
                return True
            # 规则3：10日向上突破（high >= stop_high_10）
            stop_high = si["stop_high_10"].iloc[idx]
            if not pd.isna(stop_high) and high >= stop_high:
                return True
        else:
            # 规则1 + 2：多头止损（low <= stop_loss）
            if pos.stop_loss > 0 and low <= pos.stop_loss:
                return True
            # 规则3：10日反向突破（low <= stop_low_10）
            stop_low = si["stop_low_10"].iloc[idx]
            if not pd.isna(stop_low) and low <= stop_low:
                return True

        return False

    # ════════════════════════════════════════════════════════
    #  退出（执行）
    # ════════════════════════════════════════════════════════

    def _execute_exit(self, code: str, data: bt.feeds.PandasData, pos: Position):
        """执行平仓。"""
        dt = data.datetime.date(0)
        price = data.close[0]
        if pos.direction == "short":
            pnl = (pos.entry_price - price) * pos.total_shares
        else:
            pnl = (price - pos.entry_price) * pos.total_shares
        was_win = pnl > 0

        self.close(data=data)
        self._positions.close(code)

        # 更新过滤器
        self._filter.record_result(code, was_win)

        # 更新风控状态
        self._trade_count += 1
        if not was_win:
            self._consecutive_losses += 1
            self._cumulative_loss_pct += abs(pnl) / self.broker.startingcash
            # 连续亏损暂停
            if self._consecutive_losses >= self.params.max_consecutive_losses:
                self._enter_pause(f"连续亏损 {self._consecutive_losses} 次")
        else:
            self._consecutive_losses = 0

        # 记录交易
        self._my_trades.append({
            "symbol": code,
            "direction": pos.direction,
            "entry_date": pos.entry_date.isoformat() if pos.entry_date else "",
            "exit_date": dt.isoformat(),
            "entry_price": pos.entry_price,
            "exit_price": price,
            "units": pos.units,
            "pnl": round(pnl, 2),
            "was_win": was_win,
            "holding_days": pos.holding_days,
        })

        logger.info("[退出] %s → 卖出 %d 股 @ %.4f PnL=%.2f %s",
                    code, pos.total_shares, price, pnl,
                    "盈利" if was_win else "亏损")

    # ════════════════════════════════════════════════════════
    #  加仓
    # ════════════════════════════════════════════════════════

    def _check_pyramid(self, code: str, data: bt.feeds.PandasData, pos: Position):
        """检查并执行加仓。"""
        if pos.units >= 4:
            return

        idx = self._next_idx(code)
        si = self._signals[code]
        high = data.high[0]
        low = data.low[0]
        n = si["n"].iloc[idx]

        if pd.isna(n) or n <= 0:
            return

        can_add, trigger = pyramid_add(
            pos.units, 4, pos.base_price, pos.n_at_entry,
            direction=pos.direction,
        )
        if not can_add:
            return

        # 空头：价格下跌触发加仓；多头：价格上涨触发加仓
        if pos.direction == "short":
            if low > trigger:
                return
        else:
            if high < trigger:
                return

        # T+1 约束：标记当日已买（不影响 T+0 品种）
        if code in T_PLUS_ONE_SYMBOLS:
            self._buy_today[code] = True

        # 空头加仓用 sell，多头用 buy
        shares = pos.shares_per_unit
        if pos.direction == "short":
            self.sell(data=data, size=shares)
        else:
            self.buy(data=data, size=shares)

        # 加仓后止损线不变（退出由10日高低点决定）
        self._positions.add_unit(code, pos.stop_loss)

        direction_label = "做空加仓" if pos.direction == "short" else "加仓"
        logger.info("[%s] %s → +%d 股 @ %.4f now %d units SL=%.4f",
                    direction_label, code, shares, trigger, pos.units + 1, pos.stop_loss)

    # ════════════════════════════════════════════════════════
    #  空仓 → 国债ETF 切换
    # ════════════════════════════════════════════════════════

    def _bond_switch(self):
        """空仓期买入国债ETF，有海龟信号时优先卖出。"""
        # 找国债ETF data（有持仓控制器的品种列表中最后一个）
        bond_data = None
        for d in self.datas:
            if hasattr(d, "_name") and d._name == "511010.SH":
                bond_data = d
                break
        if bond_data is None:
            return

        if self._positions.count == 0 and not self._in_bond:
            # 无持仓 → 买入国债ETF（用 90% 现金）
            equity = self._equity()
            cash = self.broker.getcash()
            if cash > 0:
                target = cash * 0.9
                price = bond_data.close[0]
                if price > 0:
                    shares = int(target / price / 100) * 100
                    if shares > 0:
                        self.buy(data=bond_data, size=shares)
                        self._in_bond = True
                        logger.info("[国债] 空仓 → 买入 %d 股 @ %.4f", shares, price)

        elif self._positions.count > 0 and self._in_bond:
            # 有海龟持仓 → 卖出国债ETF 腾出资金
            self.close(data=bond_data)
            self._in_bond = False
            logger.info("[国债] 卖出全部国债ETF，腾出资金")

    # ════════════════════════════════════════════════════════
    #  风控暂停
    # ════════════════════════════════════════════════════════

    def _enter_pause(self, reason: str):
        """进入交易暂停状态。"""
        pause_days = self.params.pause_days
        current = self.datas[0].datetime.date(0)
        self._paused_until = current + timedelta(days=pause_days)
        self._consecutive_losses = 0
        self._cumulative_loss_pct = 0.0
        logger.warning("[风控] 暂停交易 %d 天（至 %s）: %s",
                       pause_days, self._paused_until, reason)

    # ════════════════════════════════════════════════════════
    #  stop() — 回测结束输出
    # ════════════════════════════════════════════════════════

    def stop(self):
        """回测结束输出交易统计及品种级明细。"""
        total_trades = len(self._my_trades)
        wins = sum(1 for t in self._my_trades if t["was_win"])
        losses = total_trades - wins
        win_rate = wins / total_trades if total_trades > 0 else 0
        total_pnl = sum(t["pnl"] for t in self._my_trades)

        logger.info("=" * 50)
        logger.info("回测结束 — 交易统计")
        logger.info("总交易次数: %d", total_trades)
        logger.info("盈利次数: %d / 亏损次数: %d", wins, losses)
        logger.info("胜率: %.2f%%", win_rate * 100)
        logger.info("总盈亏: %.2f", total_pnl)
        logger.info("最终净值: %.2f", self._equity())
        logger.info("=" * 50)

        # ── 品种级 × 多空分项明细 ──
        logger.info("")
        logger.info("品种级盈亏明细")
        logger.info("%16s %6s %5s %10s %10s %10s %6s",
                     "品种", "方向", "次数", "盈利", "亏损", "净盈亏", "胜率")
        logger.info("-" * 70)
        for code in sorted(set(t["symbol"] for t in self._my_trades)):
            for direction in ("long", "short"):
                trades = [t for t in self._my_trades if t["symbol"] == code and t["direction"] == direction]
                if not trades:
                    continue
                cnt = len(trades)
                total = sum(t["pnl"] for t in trades)
                profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
                loss = sum(t["pnl"] for t in trades if t["pnl"] < 0)
                dir_wins = sum(1 for t in trades if t["was_win"])
                dir_win_rate = dir_wins / cnt * 100 if cnt > 0 else 0.0
                dir_label = "多头" if direction == "long" else "空头"
                logger.info("%16s %6s %5d %10.0f %10.0f %10.0f %5.1f%%",
                            code, dir_label, cnt, profit, loss, total, dir_win_rate)
        logger.info("-" * 70)

        # 存入实例属性供外部分析器获取
        self._trade_summary = {
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "final_value": self._equity(),
            "trades": pd.DataFrame(self._my_trades) if self._my_trades else pd.DataFrame(),
        }
