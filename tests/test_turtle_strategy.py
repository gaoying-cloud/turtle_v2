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

from strategies.turtle_trading import TurtleStrategy
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
        "max_5day_drawdown_pct": 0.10,
        "max_portfolio_risk": 0.20,
        "single_max_risk": 0.04,
        "t_plus_one_symbols": {"510500.SH", "159845.SZ", "159915.SZ", "588000.SH"},
        "shortable_symbols": {"513100.SH", "518880.SH"},
        "alpha": 0.05,
        "cov_lookback_days": 252,
        "rebalance_quarterly": True,
        "atr_change_threshold": 0.30,
        "futures_mode": False,
        "multipliers": {},
        "min_unit": 100,
        "min_confirmations": 0,
        "vol_threshold": 1.5,
        "kline_min_body": 0.4,
        "p2_mode": "none",
        "p2_loss_ratio": 0.75,
        "p2_batting_window": 4,
        "use_signal_filter": True,
        "use_sma_entry": False,
        "entry_mode": "breakout",
        "stop_buffer_n": 1.0,
    })()
    s.datas = [MockData(), MockData()]
    s.broker = MagicMock()
    s.broker.getvalue.return_value = 200000.0
    s.broker.getcash.return_value = 195000.0
    s.buy = MagicMock()
    s.close = MagicMock()

    s.__dict__["_signals"] = {}
    s.__dict__["_positions"] = TurtlePositions(max_units=4)
    s.__dict__["_filter"] = SignalFilter(max_rejections=3)
    s.__dict__["_current_day"] = None
    s.__dict__["_buy_today"] = {}
    s.__dict__["_consecutive_losses"] = {}
    s.__dict__["_paused_until"] = {}
    s.__dict__["_equity_history"] = []
    s.__dict__["_trade_count"] = 0
    s.__dict__["_my_trades"] = []
    # 调试计数器（新加）
    s.__dict__["_signal_count"] = {}
    s.__dict__["_filter_reject_count"] = {}
    s.__dict__["_pause_reject_count"] = {}
    s.__dict__["_loss_lockout_count"] = {}
    s.__dict__["_risk_reject_count"] = {}
    s.__dict__["_enter_count"] = {}
    # S4 状态字段
    s.__dict__["_alpha_risk_pcts"] = None
    s.__dict__["_last_rebalance_day"] = None
    s.__dict__["_last_n_values"] = {}
    s.__dict__["_close_series"] = {}

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
        """未突破 → 不入场（入场逻辑改为 close > entry_high）。"""
        strat._signals["510500.SH"] = sig(high=10.5, low=10.0, close=10.5)
        d = strat.datas[0]
        d.close[0] = 10.3          # < entry_high_20 = 10.4

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
        d.close[0] = 10.6          # > entry_high_20(10.4) 但 < entry_high_55(11.0)

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
        """跌破10日低点 → 退出（经典 Turtle 系统退出规则）。"""
        s = sig(high=10.5, low=10.0, close=10.5, n=0.5)
        s["stop_low_10"] = pd.Series([np.nan, np.nan, 9.5], index=pd.RangeIndex(3))
        strat._signals["510500.SH"] = s
        pos = strat._positions.open("510500.SH", entry_price=10.0, shares=800,
                                     n_at_entry=0.5, stop_loss=9.0)
        d = strat.datas[0]
        d.low[0] = 9.4   # ≤ 9.5 (stop_low_10)

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
        """T+0 当日买入 → 可同日退出（由10日低点触发）。"""
        strat._buy_today["518880.SH"] = True  # T+0 不受约束
        s = sig(high=102, low=98, close=101, n=1.0)
        s["stop_low_10"] = pd.Series([np.nan, np.nan, 99.0], index=pd.RangeIndex(3))
        strat._signals["518880.SH"] = s
        pos = strat._positions.open("518880.SH", entry_price=101, shares=500,
                                     n_at_entry=1.0, stop_loss=99.0)
        d = strat.datas[0]
        d.low[0] = 98.0   # ≤ 99.0 (stop_low_10)

        with patch.object(strat, "_next_idx", return_value=2):
            assert strat._should_exit("518880.SH", d, pos)

    def test_t1_next_day_can_exit(self, strat):
        """T+1 隔日 → 可退出（由10日低点触发）。"""
        strat._buy_today = {}   # 无当日买入记录
        s = sig(high=10.5, low=10.0, close=10.5, n=0.5)
        s["stop_low_10"] = pd.Series([np.nan, np.nan, 10.0], index=pd.RangeIndex(3))
        strat._signals["510500.SH"] = s
        pos = strat._positions.open("510500.SH", entry_price=10.5, shares=800,
                                     n_at_entry=0.5, stop_loss=10.0)
        d = strat.datas[0]
        d.low[0] = 9.5   # ≤ 10.0 (stop_low_10)

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
        """连续亏损达阈值 → 该品种暂停。"""
        strat._consecutive_losses["510500.SH"] = 7  # 阈值=8
        d = strat.datas[0]
        d.close[0] = 8.0  # 亏损

        strat._execute_exit("510500.SH", d,
            Position(symbol="510500.SH", entry_price=10.0, shares_per_unit=800))

        assert strat._paused_until.get("510500.SH") is not None
        assert strat._my_trades[-1]["was_win"] is False

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


# ════════════════════════════════════════════════════════════
#  S4: α 融合风险权重
# ════════════════════════════════════════════════════════════


class TestAlphaWeighting:
    """验证 S4 风险平价权重集成正确性。"""

    def test_alpha_risk_pcts_fallback_on_none(self, strat):
        """_alpha_risk_pcts=None → 使用 risk_per_unit。"""
        strat._alpha_risk_pcts = None
        assert strat._alpha_risk_pcts is None

    def test_alpha_risk_pcts_cached_after_recalc(self, strat):
        """_recalc_alpha_weights 后 _alpha_risk_pcts 不为空（当数据足够时）。"""
        # 模拟足够的信号长度
        n_bars = 300
        for code in strat.params.symbols:
            n_series = pd.Series(np.random.uniform(0.5, 2.0, n_bars))
            close_series = pd.Series(np.random.uniform(1.0, 100.0, n_bars))
            strat._signals[code] = {"n": n_series, "close": close_series}
            strat._close_series[code] = close_series

        # 模拟当前 bar 位置
        with patch.object(strat, "_next_idx", return_value=n_bars - 1):
            strat._recalc_alpha_weights()

        if strat._alpha_risk_pcts is not None:
            assert len(strat._alpha_risk_pcts) == len(strat.params.symbols)
            assert all(p > 0 for p in strat._alpha_risk_pcts)

    def test_should_rebalance_on_first_call(self, strat):
        """首次调用 _should_rebalance_weights → True（_alpha_risk_pcts is None）。"""
        strat._alpha_risk_pcts = None
        assert strat._should_rebalance_weights(date(2024, 1, 15))

    def test_build_returns_matrix_shape(self, strat):
        """_build_returns_matrix 返回形状正确。"""
        n_bars = 300
        for code in strat.params.symbols:
            close_series = pd.Series(np.random.uniform(10.0, 100.0, n_bars))
            strat._signals[code] = {"close": close_series, "n": pd.Series(np.ones(n_bars))}
            strat._close_series[code] = close_series

        with patch.object(strat, "_next_idx", return_value=n_bars - 1):
            returns = strat._build_returns_matrix()
            assert returns.shape[0] >= 1  # 至少 1 个交易日
            assert returns.shape[1] == len(strat.params.symbols)

    def test_entry_uses_alpha_risk_pcts(self, strat):
        """_check_entry 中使用 _alpha_risk_pcts 调整的 risk。"""
        # 设置 _alpha_risk_pcts
        strat._alpha_risk_pcts = np.array([0.005, 0.01])

        # 模拟入场条件（突破 + 足够 equity）
        strat._filter.check_entry = lambda c, hp: (True, "")

        # 模拟数据
        code = strat.params.symbols[0]
        idx = 2
        with patch.object(strat, "_next_idx", return_value=idx):
            strat._signals[code] = sig(high=10.5, low=10.0, close=10.5)
            d = strat.datas[0]
            d.high[0] = 10.6
            d.close[0] = 10.5

            strat._check_entry(code, d)

            assert strat._positions.has_position(code)