"""
跨市场ETF海龟组合策略 · Backtrader 策略层 (S3)

依赖：
    - backtrader>=1.9.78.123
    - s2 turtle_core (纯 pandas，无 Backtrader 依赖)

架构：
    TurtleStrategy (bt.Strategy)
    ├── __init__(): 通过 S2 TurtleSignals 预计算所有信号序列
    ├── next(): 逐日迭代
    │   ├── _check_entry()    — 突破入场 + 55日过滤 + SignalFilter
    │   ├── _should_exit()    — 10 日反向突破退出
    │   ├── _check_pyramid()  — 加仓
    │   └── [[国债切换已移除 v5.3]]
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
    pyramid_add,
    Position,
    volume_confirmation,
    breakout_quality,
    recent_batting_avg,
)
from src.risk_parity import compute_alpha_weights

logger = logging.getLogger(__name__)


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
        ETF 品种代码列表（与 self.datas 顺序一致）。
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
        ("max_cumulative_loss_pct", 0.15),  # 已废弃：旧P2已删除，保留以兼容外部传参
        ("pause_days", 5),
        ("max_5day_drawdown_pct", 0.10),   # 5日最大回撤阈值，超阈值暂停交易
        ("max_portfolio_risk", 0.20),      # 全账户风险敞口上限
        ("single_max_risk", 0.04),         # 单品种风险敞口上限
        ("t_plus_one_symbols", set()),     # T+1 品种集合（从配置传入）
        ("shortable_symbols", set()),      # 可做空品种集合（从配置传入）
        ("alpha", 0.05),                  # α 风险平价偏移系数
        ("cov_lookback_days", 252),       # 协方差矩阵估计窗口
        ("rebalance_quarterly", True),    # 每季度再平衡
        ("atr_change_threshold", 0.30),   # ATR 变动 30% 强制再平衡
        ("futures_mode", False),          # 期货模式特殊处理
        ("multipliers", {}),              # 品种→合约乘数（期货用）
        ("min_unit", 100),                # 最小交易单位（ETF=100，期货=1）
        ("min_confirmations", 0),          # 确认规则投票：至少 N 个通过 (0=关闭)
        ("vol_threshold", 1.5),            # 成交量放量倍数阈值
        ("kline_min_body", 0.4),           # K线实体占比下限
        ("p2_mode", "none"),               # "none" | "batting_avg"
        ("p2_loss_ratio", 0.75),           # batting 模式：允许最大亏损占比
        ("p2_batting_window", 4),          # batting 模式：观察窗口
        ("use_signal_filter", True),       # 是否启用 SignalFilter 盈利过滤器
        ("use_sma_entry", False),           # 使用20日均线替代20日高点作为入场信号
        ("entry_mode", "breakout"),         # "breakout" | "dual"（突破+MA5金叉双模式）
        ("stop_buffer_n", 1.0),             # MA20入场模式下止损缓冲 N 值倍数
        ("degradation_config", None),       # 品种退化自动检测参数配置
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
        self._consecutive_losses: Dict[str, int] = {}  # 多品种连续亏损计数
        self._paused_until: Dict[str, Optional[date]] = {}  # 按品种暂停截止日期
        self._equity_history: List[Tuple[date, float]] = []  # 5日回撤监控用
        self._last_equity: float = self.broker.getvalue()
        self._trade_count: int = 0
        self._my_trades: List[dict] = []

        # ── 调试计数器（排查信号不足用） ──
        self._signal_count: Dict[str, int] = {}          # 突破信号次数
        self._filter_reject_count: Dict[str, int] = {}   # SignalFilter 拒绝次数
        self._pause_reject_count: Dict[str, int] = {}    # 风控暂停拒绝次数
        self._loss_lockout_count: Dict[str, int] = {}    # 累计亏损封禁次数
        self._risk_reject_count: Dict[str, int] = {}     # 敞口校验拒绝次数
        self._degraded_symbols: Dict[str, str] = {}  # 已退化品种及原因
        self._enter_count: Dict[str, int] = {}           # 实际入场次数

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
    #  品种退化自动检测（三规则）
    # ════════════════════════════════════════════════════════

    def _check_degradation(self):
        """检测每个品种是否满足退化条件（仅报警，不自动暂停交易）。

        触发时序：规则②拦截型（最早）→ 规则③磨损型 → 规则①沉默型（最晚）
        规则②—拦截型: 信号 ≥ min_signals 且 入场/信号 ≤ conv_min
        规则③—磨损型: 交易 ≥ 短窗口 且 (近short笔全亏 或 近long笔胜率<win_max) 且亏损>阈值
        规则①—沉默型: 年均信号 < annual_min（新品种上市不满2年跳过）
        """
        dc = self.params.degradation_config
        if not dc:
            return

        annual_min = dc.get("silent_annual_signals", 2)
        conv_min = dc.get("entry_conv_min", 0.30)
        min_signals = dc.get("entry_conv_min_signals", 10)
        short_win = dc.get("wear_window_short", 3)
        long_win = dc.get("wear_window_long", 6)
        win_max = dc.get("wear_win_rate_max", 0.25)
        loss_pct_min = dc.get("wear_min_loss_pct", 0.05)

        for code in self.params.symbols:
            reasons = []

            # ── 规则②：拦截型（最先触发）──
            sig = self._signal_count.get(code, 0)
            ent = self._enter_count.get(code, 0)
            conv = ent / sig if sig > 0 else 0
            if sig >= min_signals and conv <= conv_min:
                reasons.append(f"拦截②({sig}信{ent}入转化率{conv:.0%}≤{conv_min:.0%})")

            # ── 规则③：磨损型（第二触发）──
            code_trades = [t for t in self._my_trades if t["symbol"] == code]
            n_trades = len(code_trades)
            if n_trades >= short_win:
                # 近 short_win 笔全亏
                recent_short = code_trades[-short_win:]
                recent_short_all_loss = all(not t["was_win"] for t in recent_short)
                # 近 long_win 笔胜率 < win_max
                recent_long = code_trades[-long_win:] if n_trades >= long_win else code_trades
                recent_long_win_rate = sum(1 for t in recent_long if t["was_win"]) / len(recent_long) if recent_long else 1.0
                # 亏损总额超过阈值
                if recent_short_all_loss:
                    loss_total = abs(sum(t["pnl"] for t in recent_short if not t["was_win"]))
                elif recent_long_win_rate < win_max:
                    loss_total = abs(sum(t["pnl"] for t in recent_long if not t["was_win"]))
                else:
                    loss_total = 0
                equity = self._equity() if hasattr(self, '_equity') else self.broker.getvalue()
                loss_pct = loss_total / equity if equity > 0 else 0

                if (recent_short_all_loss or recent_long_win_rate < win_max) and loss_pct > loss_pct_min:
                    detail = "全亏" if recent_short_all_loss else f"近{long_win}笔胜率{recent_long_win_rate:.0%}"
                    reasons.append(f"磨损③({n_trades}笔{detail}亏{loss_pct:.1%}本金)")

            # ── 规则①：沉默型（最后触发）──
            n_bars = len(self._signals.get(code, {}).get("n", []))
            years = n_bars / 252
            annual_sig = sig / years if years > 0 else 0
            if years >= 2 and annual_sig < annual_min:
                reasons.append(f"沉默①(年均{annual_sig:.1f}<{annual_min})")

            # ── 更新退化状态 ──
            old_reason = self._degraded_symbols.get(code, "")
            new_status = "; ".join(reasons) if reasons else ""
            if new_status != old_reason:
                self._degraded_symbols[code] = new_status
                if new_status:
                    logger.warning("[退化] %s → %s", code, new_status)
                elif old_reason:
                    logger.info("[退化] %s → 已恢复（无退化信号）", code)

    # ════════════════════════════════════════════════════════
    #  next() — 逐日迭代
    # ════════════════════════════════════════════════════════

    def next(self):
        # ── 确保有足够的数据（runonce 模式下第 0 个 bar 可能数据不全）──
        if len(self) < 2:
            return

        # ── 检查暂停期（按品种） ──
        today = self.datas[0].datetime.date(0)
        for code in list(self._paused_until.keys()):
            until = self._paused_until[code]
            if until is not None and today >= until:
                self._paused_until[code] = None
                logger.info("[风控] %s 暂停期结束，恢复交易", code)

        # ── 回撤预警（5日滚动） ──
        self._check_5day_drawdown()

        # ── 重置 T+1 标记（新交易日） ──
        if self._is_new_day():
            self._buy_today.clear()

        # ── Step 0: 检查是否需要重新计算风险平价权重 ──
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
                if self._should_exit(code, data, pos):
                    self._execute_exit(code, data, pos)
                else:
                    # 检查加仓
                    self._check_pyramid(code, data, pos)

        # ── Step 3: [[空仓→国债切换已移除（V5.3）]] ──

        # ── Step 4: 品种退化自动检测 ──
        if len(self) % 5 == 0:
            self._check_degradation()

        # ── 每周健康日志 ──
        # 退化判定三规则（配置项位于 config.yaml risk.degradation，触发时序②→③→①）：
        #   规则②—拦截型: 信号 ≥ entry_conv_min_signals 且 入场/信号 ≤ entry_conv_min
        #   规则③—磨损型: 交易 ≥ wear_window_short 且 (近short笔全亏 或 近long笔胜率<wear_win_rate_max) 且亏损> wear_min_loss_pct
        #   规则①—沉默型: 年均信号 < silent_annual_signals（新品种上市不满2年跳过）
        if len(self) % 5 == 0 and len(self) > 2:  # 每周打印一次，避免刷屏
            has_data = any(
                self._signal_count.get(code, 0) > 0 or
                len([t for t in self._my_trades if t["symbol"] == code]) > 0
                for code in self.params.symbols
            )
            if has_data:
                logger.info("[健康] %12s %5s %5s %5s %8s %5s  %s",
                            "品种", "信号", "入场", "转化率", "近4笔胜率", "连亏", "退化")
                for code in sorted(self.params.symbols):
                    sig = self._signal_count.get(code, 0)
                    ent = self._enter_count.get(code, 0)
                    conv = f"{ent/sig*100:.0f}%" if sig > 0 else "-"
                    code_trades = [t for t in self._my_trades if t["symbol"] == code]
                    recent = code_trades[-4:]
                    wins = sum(1 for t in recent if t["was_win"])
                    recent_wr = f"{wins}/{len(recent)}={wins/len(recent)*100:.0f}%" if recent else "-"
                    cl = self._consecutive_losses.get(code, 0)
                    degraded = self._degraded_symbols.get(code, "")
                    logger.info("[健康] %12s %5d %5d %5s %8s %5d  %s",
                                code, sig, ent, conv, recent_wr, cl, degraded)

    # ════════════════════════════════════════════════════════
    #  入场
    # ════════════════════════════════════════════════════════

    def _should_enter_short(self, code: str, si: dict, idx: int, close: float, n: float) -> bool:
        """检查是否触发空头入场信号。仅对 shortable_symbols 返回 True。"""
        if code not in self.params.shortable_symbols:
            return False
        dc_low = si.get("entry_low_20")
        if dc_low is None:
            return False
        entry_low = dc_low.iloc[idx]
        return pd.notna(entry_low) and close < entry_low

    def _check_entry(self, code: str, data: bt.feeds.PandasData):
        """检查并执行入场信号。"""
        # ── 按品种暂停检查 ──
        paused = self._paused_until.get(code)
        if paused is not None:
            self._pause_reject_count[code] = self._pause_reject_count.get(code, 0) + 1
            return

        idx = self._next_idx(code)
        si = self._signals[code]
        close = data.close[0]
        n = si["n"].iloc[idx]

        if pd.isna(n) or n <= 0:
            return

        # ── 判断方向（多头） ──
        entry_source = None  # "breakout" | "ma5_golden"

        if self.params.entry_mode == "dual":
            # 双模式：A) 20日高点突破  OR  B) MA10>MA20 且 close>MA10
            entry_high = si["entry_high_20"].iloc[idx]
            is_long_a = pd.notna(entry_high) and close > entry_high

            ma10 = si.get("ma10")
            ma20 = si.get("sma_20")
            is_long_b = False
            if ma10 is not None and ma20 is not None and idx >= 20:
                m10 = ma10.iloc[idx]
                m20 = ma20.iloc[idx]
                if not pd.isna(m10) and not pd.isna(m20):
                    is_long_b = m10 > m20 and close > m10

            if is_long_a:
                entry_source = "breakout"
                is_long = True
            elif is_long_b:
                entry_source = "ma10_golden"
                is_long = True
            else:
                is_long = False
        else:
            # 标准模式：20日高点突破
            entry_high = si["entry_high_20"].iloc[idx]
            is_long = pd.notna(entry_high) and close > entry_high
            if is_long:
                entry_source = "breakout"
        entry_low_20 = si.get("entry_low_20")
        is_short = False
        if entry_low_20 is not None and code in self.params.shortable_symbols:
            el = entry_low_20.iloc[idx]
            if pd.notna(el) and close < el:
                is_short = True
        if not is_long and not is_short:
            return

        # ── 突破信号计数 ──
        self._signal_count[code] = self._signal_count.get(code, 0) + 1

        # ── 55日过滤（模式 B，多头+空头对称） ──
        if self.params.use_55_filter:
            if is_long:
                filter_high = si["entry_high_55"].iloc[idx]
                if pd.isna(filter_high) or close <= filter_high:
                    return
            elif is_short:
                filter_low = si["entry_low_55"].iloc[idx]
                if pd.isna(filter_low) or close >= filter_low:
                    return

        # ── 盈利过滤器（期货模式禁用） ──
        if self.params.use_signal_filter and not self.params.futures_mode:
            ok, reason = self._filter.check_entry(code, self._positions.has_position(code))
            if not ok:
                self._filter_reject_count[code] = self._filter_reject_count.get(code, 0) + 1
                logger.debug("[入场] %s 被过滤器拒绝: %s", code, reason)
                return

        # ── 投票式信号确认（成交量 / K线 / 近期胜率） ──
        if self.params.min_confirmations > 0 and not self.params.futures_mode:
            confirmations = []

            # ⑤ 成交量确认
            if self.params.vol_threshold > 0:
                # 取近 21 个 bar 的 volume 序列
                vol_count = min(21, len(data.volume.array))
                vol_series = pd.Series(data.volume.array[-vol_count:])
                vol_pass = volume_confirmation(
                    data.volume[0], vol_series,
                    threshold=self.params.vol_threshold,
                )
                if vol_pass:
                    confirmations.append("vol")
                else:
                    logger.debug("[确认] %s 成交量未达标 (%.1f/%.1f)",
                                 code,
                                 data.volume[0] / vol_series.iloc[:-1].mean()
                                 if len(vol_series) > 1 else 0,
                                 self.params.vol_threshold)

            # ④ K 线形态确认
            if self.params.kline_min_body > 0:
                kline_pass = breakout_quality(
                    data.open[0], data.high[0], data.low[0], data.close[0],
                    is_long=is_long,
                    min_body_ratio=self.params.kline_min_body,
                )
                if kline_pass:
                    confirmations.append("kline")
                else:
                    body = abs(data.close[0] - data.open[0])
                    cr = data.high[0] - data.low[0]
                    body_pct = body / cr * 100 if cr > 0 else 0
                    logger.debug("[确认] %s K线未达标 (实体%.0f%% %s)",
                                 code, body_pct,
                                 "十字星" if body_pct < self.params.kline_min_body * 100 else "位置不对")

            # ③ 近期胜率（替代原 P2 累计亏损金额）
            if self.params.p2_mode == "batting_avg":
                code_trades = [t for t in self._my_trades if t["symbol"] == code]
                win_size = self.params.p2_batting_window
                batting_pass = recent_batting_avg(
                    code_trades,
                    window=win_size,
                    max_loss_ratio=self.params.p2_loss_ratio,
                )
                if batting_pass:
                    confirmations.append("batting")
                else:
                    self._loss_lockout_count[code] = self._loss_lockout_count.get(code, 0) + 1
                    logger.warning("[确认] %s 近%d笔亏损占比 ≥ %.0f%%，暂停新开仓",
                                   code, win_size, self.params.p2_loss_ratio * 100)

            # 汇总投票
            if len(confirmations) < self.params.min_confirmations:
                logger.debug("[入场] %s 信号确认不足 (%d/%d): %s",
                             code, len(confirmations),
                             self.params.min_confirmations, confirmations)
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

        equity = self._equity()
        price = data.close[0]
        if not np.isfinite(risk) or risk <= 0:
            risk = self.params.risk_per_unit
        mu = self.params.min_unit if hasattr(self.params, 'min_unit') else 100
        ml = self.params.multipliers.get(code, 1)
        shares = calc_position_size(equity, n, price, risk, min_unit=mu, multiplier=ml)
        if shares == 0:
            return
        # P0: 校验单品种风险敞口 ≤ single_max_risk
        per_share_risk = 2.0 * n
        requested_risk = shares * per_share_risk
        # 已有该品种的敞口
        existing_risk = 0.0
        pos = self._positions.get(code)
        if pos is not None:
            existing_risk = pos.total_shares * 2.0 * pos.n_at_entry
        total_symbol_risk_pct = (existing_risk + requested_risk) / equity if equity > 0 else 0
        if total_symbol_risk_pct > self.params.single_max_risk:
            max_new = equity * self.params.single_max_risk - existing_risk
            adjusted = int(max_new / per_share_risk / 100) * 100
            if adjusted <= 0:
                logger.debug("[入场] %s 单品种风险敞口已达 %.0f%% (%.2f%%)",
                             code, self.params.single_max_risk * 100, total_symbol_risk_pct * 100)
                return
            shares = adjusted
        # P0: 校验全账户风险敞口 ≤ 15% (max_total_risk=0.15)
        total_existing_risk = 0.0
        for existing_pos in self._positions.all_positions():
            total_existing_risk += existing_pos.total_shares * 2.0 * existing_pos.n_at_entry
        total_new_risk_pct = (total_existing_risk + shares * per_share_risk) / equity if equity > 0 else 0
        if total_new_risk_pct > self.params.max_portfolio_risk:
            max_new = equity * self.params.max_portfolio_risk - total_existing_risk
            adjusted = int(max_new / per_share_risk / 100) * 100
            if adjusted <= 0:
                logger.debug("[入场] %s 全账户风险敞口已达 %.0f%% (%.2f%%)",
                             code, self.params.max_portfolio_risk * 100, total_new_risk_pct * 100)
                return
            shares = adjusted

        # ── T+1 约束：当日已买入的同品种不可再买 ──
        if code in self.params.t_plus_one_symbols and self._buy_today.get(code, False):
            return

        # ── 执行入场（多头或空头） ──
        dt = data.datetime.date(0)
        direction = "long" if is_long else "short"
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
            stop_loss=0.0,
            entry_mode=entry_source or "breakout",
        )
        if code in self.params.t_plus_one_symbols:
            self._buy_today[code] = True

        self._enter_count[code] = self._enter_count.get(code, 0) + 1
        logger.info("[入场] %s → %s %d 股 @ %.4f (N=%.4f %s)",
                    code, action, shares, price, n, direction)

    # ════════════════════════════════════════════════════════════
    #  退出（10 日反向突破，唯一退出规则）
    # ════════════════════════════════════════════════════════════

    def _should_exit(self, code: str, data: bt.feeds.PandasData, pos: Position) -> bool:
        """判断是否触发退出条件：仅 10 日反向突破（经典海龟退出规则）。"""
        idx = self._next_idx(code)
        si = self._signals[code]
        high = data.high[0]
        low = data.low[0]
        n = si["n"].iloc[idx]

        if pd.isna(n) or n <= 0:
            return False

        # ── T+1 约束 ──
        if code in self.params.t_plus_one_symbols and self._buy_today.get(code, False):
            return False

        if pos.direction == "short":
            # 10日向上突破退出（唯一退出规则）
            stop_high = si["stop_high_10"].iloc[idx]
            if not pd.isna(stop_high) and high >= stop_high:
                return True
        else:
            ep = getattr(pos, "entry_mode", "breakout")
            if ep == "ma10_golden":
                # B 入场：仅 MA20 止损，不走 10 日低点
                ma20 = si.get("sma_20")
                if ma20 is not None and not pd.isna(ma20.iloc[idx]):
                    c = data.close[0]
                    if pos.direction == "long":
                        if c < ma20.iloc[idx]:
                            return True
                    else:
                        if c > ma20.iloc[idx]:
                            return True
            else:
                # A 入场：10 日反向突破退出
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

        # 更新风控状态（按品种）
        self._trade_count += 1
        cl = self._consecutive_losses.get(code, 0)
        if not was_win:
            cl += 1
            self._consecutive_losses[code] = cl
            # 连续亏损暂停（仅该品种）
            if cl >= self.params.max_consecutive_losses:
                self._enter_pause(code, f"{code} 连续亏损 {cl} 次")
        else:
            self._consecutive_losses[code] = 0

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
        if code in self.params.t_plus_one_symbols:
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
    #  风控暂停
    # ════════════════════════════════════════════════════════

    def _check_5day_drawdown(self):
        """5日滚动最大回撤监控，超阈值暂停所有品种交易。"""
        today = self.datas[0].datetime.date(0)
        equity = self._equity()
        self._equity_history.append((today, equity))
        if len(self._equity_history) > 6:
            self._equity_history.pop(0)
        if len(self._equity_history) < 5:
            return
        peak = max(e for _, e in self._equity_history)
        if peak <= 0:
            return
        drawdown = (peak - equity) / peak
        if drawdown >= self.params.max_5day_drawdown_pct:
            pause_days = 5  # 全局熔断暂停 5 天
            for code in self.params.symbols:
                self._paused_until[code] = today + timedelta(days=pause_days)
            logger.warning("[风控] 5日回撤 %.1f%% ≥ %.0f%%，全部品种暂停 %d 天",
                           drawdown * 100, self.params.max_5day_drawdown_pct * 100, pause_days)

    def _enter_pause(self, code: str, reason: str):
        """按品种进入交易暂停状态。"""
        pause_days = self.params.pause_days
        current = self.datas[0].datetime.date(0)
        self._paused_until[code] = current + timedelta(days=pause_days)
        self._consecutive_losses[code] = 0
        logger.warning("[风控] %s 暂停交易 %d 天（至 %s）: %s",
                       code, pause_days, self._paused_until[code], reason)

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

        # ── 调试计数器输出 ──
        logger.info("")
        logger.info("=" * 50)
        logger.info("入场拦截统计（按品种）")
        logger.info("%16s %7s %7s %7s %7s %7s",
                     "品种", "突破信号", "Filter拒", "暂停拒",
                     "爆仓封禁", "实际入场")
        logger.info("-" * 60)
        for code in sorted(self.params.symbols):
            sig = self._signal_count.get(code, 0)
            fil = self._filter_reject_count.get(code, 0)
            pau = self._pause_reject_count.get(code, 0)
            loc = self._loss_lockout_count.get(code, 0)
            ent = self._enter_count.get(code, 0)
            logger.info("%16s %7d %7d %7d %7d %7d",
                        code, sig, fil, pau, loc, ent)
        logger.info("-" * 60)
        sig_t = sum(self._signal_count.values())
        fil_t = sum(self._filter_reject_count.values())
        pau_t = sum(self._pause_reject_count.values())
        loc_t = sum(self._loss_lockout_count.values())
        ent_t = sum(self._enter_count.values())
        logger.info("%16s %7d %7d %7d %7d %7d",
                    "合计", sig_t, fil_t, pau_t, loc_t, ent_t)
        logger.info("=" * 50)

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
