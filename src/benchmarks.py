"""四种基准对比策略 (S5)

实现设计文档 §4.4 定义的三种补充基准策略（B1/B2/B3），
B4（海龟+国债）即为现有 TurtleStrategy，不在此模块重复实现。

基准对比分析入口在 scripts/run_comparison.py。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

import backtrader as bt
import numpy as np
import pandas as pd

# ── S2 核心 ──
from src.turtle_core import (
    compute_tr,
    compute_atr,
    calc_position_size,
)

logger = logging.getLogger(__name__)

# ── 6 只海龟品种（与 run_backtest 中的 SIX_SYMBOLS 一致） ──
SIX_SYMBOLS = [
    "510500.SH",
    "159845.SZ",
    "159915.SZ",
    "588000.SH",
    "513100.SH",
    "518880.SH",
]


# ════════════════════════════════════════════════════════════
#  B1: 买入等权持有
# ════════════════════════════════════════════════════════════


class BuyAndHold(bt.Strategy):
    """B1: 买入等权持有基准。

    回测第一天等权买入 6 只 ETF，之后不交易。
    """

    params = (
        ("symbols", None),
    )

    def __init__(self):
        self._initialized = False
        self._trade_summary = None

    def next(self):
        if not self._initialized:
            # 第一天：等权买入
            cash = self.broker.getcash()
            n = len(self.params.symbols)
            per_symbol = cash / n

            for i, code in enumerate(self.params.symbols):
                data = self.datas[i]
                price = data.close[0]
                if price <= 0:
                    continue
                shares = int(per_symbol / price / 100) * 100
                if shares > 0:
                    self.buy(data=data, size=shares)
                    logger.info("[B1] %s 买入 %d 股 @ %.4f", code, shares, price)

            self._initialized = True

    def stop(self):
        final_value = self.broker.getvalue()
        logger.info("[B1] 等权持有最终净值: %.2f", final_value)


# ════════════════════════════════════════════════════════════
#  B2: 等权定期再平衡
# ════════════════════════════════════════════════════════════


class EqualWeightRebalance(bt.Strategy):
    """B2: 等权定期再平衡基准。

    每季度末（3/6/9/12 月）按等权重全仓再平衡。
    """

    params = (
        ("symbols", None),
        ("rebalance_months", (3, 6, 9, 12)),
    )

    def __init__(self):
        self._last_rebalance = None
        self._trade_summary = None

    def next(self):
        today = self.datas[0].datetime.date(0)
        month = today.month
        if month not in self.params.rebalance_months:
            return

        # 确保同季度只触发一次
        q = (month - 1) // 3
        if self._last_rebalance is not None:
            last_q = (self._last_rebalance.month - 1) // 3
            if q == last_q:
                return

        # 执行再平衡
        self._rebalance(today)

    def _rebalance(self, today: date):
        """等权重全仓再平衡。"""
        n = len(self.params.symbols)
        if n == 0:
            return

        # 先清仓
        for data in self.datas:
            pos = self.getposition(data)
            if pos.size > 0:
                self.close(data=data)

        # 再等权买入
        equity = self.broker.getvalue()
        per_symbol = equity * 0.98 / n  # 留 2% 现金避免小数误差

        for i, code in enumerate(self.params.symbols):
            data = self.datas[i]
            price = data.close[0]
            if price <= 0:
                continue
            shares = int(per_symbol / price / 100) * 100
            if shares > 0:
                self.buy(data=data, size=shares)

        self._last_rebalance = today
        logger.info("[B2] 再平衡完成 @ %s", today.isoformat())

    def stop(self):
        final_value = self.broker.getvalue()
        logger.info("[B2] 等权再平衡最终净值: %.2f", final_value)


# ════════════════════════════════════════════════════════════
#  B3: ATR 等风险贡献（无海龟信号）
# ════════════════════════════════════════════════════════════


class ATREqualRisk(bt.Strategy):
    """B3: ATR 等风险贡献基准。

    仅使用第二层（ATR 仓位管理），不使用海龟入场/止损/加仓信号。
    始终持有 6 只 ETF，仅当 ATR 变动 > 30% 时调整头寸规模。

    注意：由于 ATR 需要 20 个交易日的数据预热，前 20 个 bar 跳过。
    """

    params = (
        ("symbols", None),
        ("risk_per_unit", 0.01),
        ("atr_period", 20),
        ("atr_change_threshold", 0.30),
    )

    def __init__(self):
        # 预计算 N 值序列（从 close 计算 ATR）
        self._n_values: dict[str, pd.Series] = {}
        self._last_n: dict[str, float] = {}
        self._initialized = False
        self._trade_summary = None

        # 计算每只品种的 ATR
        for i, code in enumerate(self.params.symbols):
            data = self.datas[i]
            n_bars = len(data.close.array)
            idx = pd.RangeIndex(n_bars)
            high = pd.Series(data.high.array, index=idx)
            low = pd.Series(data.low.array, index=idx)
            close = pd.Series(data.close.array, index=idx)

            tr = compute_tr(high, low, close)
            n = compute_atr(tr, self.params.atr_period)
            self._n_values[code] = n

    def next(self):
        # 前 20 个 bar 跳过（ATR 预热）
        bt_len = getattr(self, "_bt_len", None)
        if bt_len is None:
            bt_len = len(self)
        if bt_len < self.params.atr_period + 1:
            return

        if not self._initialized:
            self._initial_buy()
            self._initialized = True
            return

        # 检查 ATR 变动是否超过阈值 → 强制再平衡
        for code in self.params.symbols:
            bt_len = getattr(self, "_bt_len", None) or len(self)
            idx = min(bt_len - 1, len(self._n_values[code]) - 1)
            current_n = self._n_values[code].iloc[idx]
            if pd.isna(current_n) or current_n <= 0:
                continue
            old_n = self._last_n.get(code)
            if old_n is not None and old_n > 0:
                change_pct = abs(current_n - old_n) / old_n
                if change_pct >= self.params.atr_change_threshold:
                    logger.info("[B3] %s ATR 变动 %.1f%%，触发再平衡", code, change_pct * 100)
                    self._rebalance_positions()
                    return  # 一季只触发一次

    def _initial_buy(self):
        """首次建仓：按 ATR 分配资金（等保证金贡献）。"""
        equity = self.broker.getvalue()
        n = len(self.params.symbols)
        bt_len = getattr(self, "_bt_len", None) or len(self)
        idx = min(bt_len - 1, min(len(v) - 1 for v in self._n_values.values()))
        idx = max(0, idx)  # 防止负数索引

        for i, code in enumerate(self.params.symbols):
            data = self.datas[i]
            price = data.close[0]
            current_n = self._n_values[code].iloc[idx]

            if pd.isna(current_n) or current_n <= 0 or price <= 0:
                continue

            # ATR 仓位公式
            from src.turtle_core import calc_position_size
            shares = calc_position_size(equity, current_n, price, self.params.risk_per_unit)
            if shares > 0:
                self.buy(data=data, size=shares)
                self._last_n[code] = current_n
                logger.info("[B3] %s 首次买入 %d 股 @ %.4f (N=%.4f)", code, shares, price, current_n)

        self._initialized = True

    def _rebalance_positions(self):
        """重新平衡仓位（先清仓再按当前 ATR 买入）。"""
        # 清仓
        for data in self.datas:
            pos = self.getposition(data)
            if pos.size > 0:
                self.close(data=data)

        self._last_n.clear()
        self._initial_buy()

    def stop(self):
        final_value = self.broker.getvalue()
        logger.info("[B3] ATR 等风险最终净值: %.2f", final_value)
