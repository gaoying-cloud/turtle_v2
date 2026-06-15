"""
单元测试：strategies/turtle_trading.py (S3)

覆盖范围：
    - 入场逻辑 (_check_entry)
    - 退出逻辑 (_should_exit)
    - 加仓逻辑 (_check_pyramid)
    - T+1 约束 (当日买入后不可止损)
    - 风控暂停 (_enter_pause)
    - 新交易日检测

注意：使用 mock 隔离 Backtrader，不启动完整 Cerebro。
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategies.turtle_trading import TurtleStrategy, T_PLUS_ONE_SYMBOLS
from src.turtle_core import Position, TurtlePositions, SignalFilter


# ════════════════════════════════════════════════════════════
#  辅助：可变 line
# ════════════════════════════════════════════════════════════

class Line:
    """可变的 Backtrader style line，支持 [0] 读写。"""
    def __init__(self, values):
        self._v = list(values)

    def __getitem__(self, idx):
        return self._v[idx]

    def __setitem__(self, idx, val):
        self._v[idx] = val


class MockData:
    """模拟 bt.feeds.PandasData。"""
    def __init__(self, n=60, seed=42):
        np.random.seed(seed)
        c = 100.0 + np.cumsum(np.random.randn(n) * 0.5)
        h = c + np.abs(np.random.randn(n) * 0.3)
        l = c - np.abs(np.random.randn(n) * 0.3)
        v = np.ones(n) * 1e6

        self.close = Line(c)
        self.high = Line(h)
        self.low = Line(l)
        self.volume = Line(v)
        self._dt = date(2024, 1, 15)

    @property
    def datetime(self):
        return _DT(self._dt)


class _DT:
    def __init__(self, dt):
        self._dt = dt
    def date(self, idx):
        return self._dt


# ════════════════════════════════════════════════════════════
#  Fixtures
# ════════════════════════════════════════════════════════════

@pytest.fixture
def strat():
    """创建干净的 TurtleStrategy，所有外部依赖 mock。"""
    s = TurtleStrategy.__new__(TurtleStrategy)
    s.params = type("P", (), {
        "turtle_params": {"breakout_period": 20, "atr_period": 20,
                          "stop_period": 10, "stop_atr_multiple": 2.0},
        "symbols": ["510500.SH", "518880.SH"],
        "use_55_filter": False,
        "risk_per_unit": 0.01,
        "concentration_trigger": 4,
        "max_consecutive_losses": 8,
        "max_cumulative_loss_pct": 0.15,
        "pause_days": 5,
    })()
    s.datas = [MockData(), MockData()]
    s.broker = MagicMock()
    s.broker.getvalue.return_value = 200000.0
    s.broker.getcash.return_value = 195000.0
    s.buy = MagicMock()
    s.close = MagicMock()

    s._signals = {}
    s._positions = TurtlePositions(max_units=4)
    s._filter = SignalFilter(max_rejections=3)
    s._current_day = None
    s._buy_today = {}
    s._consecutive_losses = 0
    s._cumulative_loss_pct = 0.0
    s._paused_until = None
    s._in_bond = False
    s._trade_count = 0
    s._trades = []

    return s


def sig(high, low, close, n=0.5):
    """创建 3-bar 的信号字典，idx=2 有值。"""
    idx = pd.RangeIndex(3)
    return {
        "n": pd.Series([np.nan, np.nan, n], index=idx),
        "entry_high_20": pd.Series([np.nan, np.nan, high - 0.1], index=idx),
        "entry_low_20": pd.Series([np.nan, np.nan, low + 0.1], index=idx),
        "entry_high_55": pd.Series([np.nan, np.nan, high + 0.5], index=idx),
        "entry_low_55": pd.Series([np.nan, np.nan, low - 0.5], index=idx),
        "stop_high_10": pd.Series([np.nan, np.nan, high + 0.2], index=idx),
        "stop_low_10": pd.Series([np.nan, np.nan, low - 0.2], index=idx),
        "trail_high_10": pd.Series([np.nan, np.nan, close], index=idx),
    }


# ════════════════════════════════════════════════════════════
#  入场
# ════════════════════════════════════════════════════════════

class TestEntry:
    def test_breakout_enters(self, strat):
        """突破20日高点 → 入场。"""
        strat._signals["510500.SH"] = sig(high=10.5, low=10.0, close=10.5)
        d = strat.datas[0]
        d.high[0] = 10.6          # > entry_high_20 = 10.4
        d.close[0] = 10.5

        with patch.object(strat, "_next_idx", return_value=2):
            strat._check_entry("510500.SH", d)
            assert strat._positions.has_position("510500.SH")

    def test_no_breakout_skips(self, strat):
        """未突破 → 不入场。"""
        strat._signals["510500.SH"] = sig(high=10.5, low=10.0, close=10.5)
        d = strat.datas[0]
        d.high[0] = 10.3          # < entry_high_20 = 10.4

        with patch.object(strat, "_next_idx", return_value=2):
            strat._check_entry("510500.SH", d)
            assert not strat._positions.has_position("510500.SH")

    def test_55_filter_blocks(self, strat):
        """模式B：20日突破但55日未突破 → 拒绝。"""
        strat.params.use_55_filter = True
        s = sig(high=10.5, low=10.0, close=10.5)
        s["entry_high_55"] = pd.Series([np.nan, np.nan, 11.0], index=pd.RangeIndex(3))
        strat._signals["510500.SH"] = s

        d = strat.datas[0]
        d.high[0] = 10.6          # > entry_high_20(10.4) 但 < entry_high_55(11.0)

        with patch.object(strat, "_next_idx", return_value=2):
            strat._check_entry("510500.SH", d)
            assert not strat._positions.has_position("510500.SH")

    def test_equity_too_small(self, strat):
        """净值不足 → 0 手 → 不入场。"""
        strat.broker.getvalue.return_value = 100.0
        strat._signals["510500.SH"] = sig(high=10.5, low=10.0, close=10.5)
        d = strat.datas[0]
        d.high[0] = 10.6
        d.close[0] = 10.5

        with patch.object(strat, "_next_idx", return_value=2):
            strat._check_entry("510500.SH", d)
            assert not strat._positions.has_position("510500.SH")


# ════════════════════════════════════════════════════════════
#  退出
# ════════════════════════════════════════════════════════════

class TestExit:
    def test_fixed_stop(self, strat):
        """跌破2N止损 → 退出。"""
        strat._signals["510500.SH"] = sig(high=10.5, low=10.0, close=10.5, n=0.5)
        pos = strat._positions.open("510500.SH", entry_price=10.0, shares=800,
                                     n_at_entry=0.5, stop_loss=9.0)  # 10-2*0.5
        d = strat.datas[0]
        d.close[0] = 8.9   # ≤ 9.0

        with patch.object(strat, "_next_idx", return_value=2):
            assert strat._should_exit("510500.SH", d, pos)

    def test_above_stop(self, strat):
        """在止损上方 → 不退出。"""
        strat._signals["518880.SH"] = sig(high=102, low=98, close=101, n=1.0)
        pos = strat._positions.open("518880.SH", entry_price=100, shares=500,
                                     n_at_entry=1.0, stop_loss=98.0)
        d = strat.datas[0]
        d.close[0] = 101.0  # > 98

        with patch.object(strat, "_next_idx", return_value=2):
            assert not strat._should_exit("518880.SH", d, pos)

    def test_10day_break(self, strat):
        """跌破10日低点 → 退出。"""
        s = sig(high=10.5, low=10.0, close=10.5, n=0.3)
        s["stop_low_10"] = pd.Series([np.nan, np.nan, 9.9], index=pd.RangeIndex(3))
        strat._signals["510500.SH"] = s
        pos = strat._positions.open("510500.SH", entry_price=10.5, shares=800,
                                     n_at_entry=0.3, stop_loss=9.0)
        d = strat.datas[0]
        d.low[0] = 9.8   # ≤ 9.9

        with patch.object(strat, "_next_idx", return_value=2):
            assert strat._should_exit("510500.SH", d, pos)


# ════════════════════════════════════════════════════════════
#  T+1
# ════════════════════════════════════════════════════════════

class TestTPlusOne:
    def test_t1_buy_today_no_exit(self, strat):
        """T+1 当日买入 → 同日不可止损。"""
        strat._buy_today["510500.SH"] = True
        strat._signals["510500.SH"] = sig(high=10.5, low=10.0, close=10.5, n=0.5)
        pos = strat._positions.open("510500.SH", entry_price=10.5, shares=800,
                                     n_at_entry=0.5, stop_loss=10.0)
        d = strat.datas[0]
        d.close[0] = 9.5

        with patch.object(strat, "_next_idx", return_value=2):
            assert not strat._should_exit("510500.SH", d, pos)

    def test_t0_can_exit_same_day(self, strat):
        """T+0 当日买入 → 可同日止损。"""
        strat._buy_today["518880.SH"] = True  # T+0 不受约束
        strat._signals["518880.SH"] = sig(high=102, low=98, close=101, n=1.0)
        pos = strat._positions.open("518880.SH", entry_price=101, shares=500,
                                     n_at_entry=1.0, stop_loss=99.0)
        d = strat.datas[0]
        d.close[0] = 98.0   # ≤ 99

        with patch.object(strat, "_next_idx", return_value=2):
            assert strat._should_exit("518880.SH", d, pos)

    def test_t1_next_day_can_exit(self, strat):
        """T+1 隔日 → 可止损。"""
        strat._buy_today = {}   # 无当日买入记录
        strat._signals["510500.SH"] = sig(high=10.5, low=10.0, close=10.5, n=0.5)
        pos = strat._positions.open("510500.SH", entry_price=10.5, shares=800,
                                     n_at_entry=0.5, stop_loss=10.0)
        d = strat.datas[0]
        d.close[0] = 9.5

        with patch.object(strat, "_next_idx", return_value=2):
            assert strat._should_exit("510500.SH", d, pos)


# ════════════════════════════════════════════════════════════
#  加仓
# ════════════════════════════════════════════════════════════

class TestPyramid:
    def test_triggers(self, strat):
        """达触发价 → 加仓。"""
        strat._signals["510500.SH"] = sig(high=11, low=10, close=10.8, n=0.5)
        pos = strat._positions.open("510500.SH", entry_price=10.0, shares=800,
                                     n_at_entry=0.5)
        d = strat.datas[0]
        d.high[0] = 10.5    # ≥ 10.0 + 1*0.5*0.5 = 10.25

        with patch.object(strat, "_next_idx", return_value=2):
            strat._check_pyramid("510500.SH", d, pos)
            assert pos.units == 2

    def test_not_triggered(self, strat):
        """未达触发价 → 不加仓。"""
        strat._signals["510500.SH"] = sig(high=11, low=10, close=10.8, n=0.5)
        pos = strat._positions.open("510500.SH", entry_price=10.0, shares=800,
                                     n_at_entry=0.5)
        d = strat.datas[0]
        d.high[0] = 10.2    # < 10.25

        with patch.object(strat, "_next_idx", return_value=2):
            strat._check_pyramid("510500.SH", d, pos)
            assert pos.units == 1

    def test_max_units(self, strat):
        """满单位 → 不加仓。"""
        strat._signals["510500.SH"] = sig(high=11, low=10, close=10.8, n=0.5)
        pos = strat._positions.open("510500.SH", entry_price=10.0, shares=800,
                                     n_at_entry=0.5)
        pos.units = 4

        with patch.object(strat, "_next_idx", return_value=2):
            strat._check_pyramid("510500.SH", strat.datas[0], pos)
            assert pos.units == 4


# ════════════════════════════════════════════════════════════
#  风控
# ════════════════════════════════════════════════════════════

class TestRisk:
    def test_pause_after_losses(self, strat):
        """连续亏损达阈值 → 暂停。"""
        strat._consecutive_losses = 7  # 阈值=8
        d = strat.datas[0]
        d.close[0] = 8.0  # 亏损

        strat._execute_exit("510500.SH", d,
            Position(symbol="510500.SH", entry_price=10.0, shares_per_unit=800))

        assert strat._paused_until is not None
        assert strat._trades[-1]["was_win"] is False

    def test_concentration_count(self, strat):
        """4品种持仓 → 满足集中度条件。"""
        for sym in ["A", "B", "C", "D"]:
            strat._positions.open(sym, entry_price=10, shares=800, n_at_entry=0.5)
        assert strat._positions.count >= strat.params.concentration_trigger


# ════════════════════════════════════════════════════════════
#  新交易日
# ════════════════════════════════════════════════════════════

class TestIsNewDay:
    def test_new_day(self, strat):
        """日期变化 → True。"""
        strat._current_day = date(2024, 1, 14)
        assert strat._is_new_day()
        assert strat._current_day == date(2024, 1, 15)

    def test_same_day(self, strat):
        """同一天 → False。"""
        strat._current_day = date(2024, 1, 15)
        assert not strat._is_new_day()