"""tests/test_benchmarks.py — S5 基准对比策略测试"""

import sys
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.benchmarks import BuyAndHold, EqualWeightRebalance, ATREqualRisk
from src.turtle_core import calc_position_size


# ── Mock 辅助 ──


class Line:
    """可变 Backtrader style line。"""
    def __init__(self, values):
        self._v = list(values)
    def __getitem__(self, idx):
        return self._v[idx]
    def __setitem__(self, idx, val):
        self._v[idx] = val


class _DT:
    def __init__(self, dt=None):
        self._dt = dt or date(2024, 1, 15)
    def date(self, idx):
        return self._dt


class MockData:
    def __init__(self, n=300):
        rng = np.random.default_rng(42)
        c = 100.0 + np.cumsum(rng.normal(0, 1.0, n))
        h = c + np.abs(rng.normal(0, 0.5, n))
        l = c - np.abs(rng.normal(0, 0.5, n))
        self.close = Line(c)
        self.high = Line(h)
        self.low = Line(l)
        self.volume = Line(np.ones(n) * 1e6)
        self._dt = date(2024, 1, 15)
    @property
    def datetime(self):
        return _DT(self._dt)


# ════════════════════════════════════════════════════════════
#  Test: BuyAndHold (B1)
# ════════════════════════════════════════════════════════════


class TestBuyAndHold:
    @pytest.fixture
    def strat(self):
        s = BuyAndHold.__new__(BuyAndHold)
        s.params = type("P", (), {"symbols": ["A", "B"]})()
        s.datas = [MockData(60), MockData(60)]
        s.broker = MagicMock()
        s.broker.getcash.return_value = 200000.0
        s.broker.getvalue.return_value = 200000.0
        s.buy = MagicMock()
        s.close = MagicMock()
        s.__dict__["_initialized"] = False
        s.__dict__["_trade_summary"] = None
        return s

    def test_initial_buy_on_first_call(self, strat):
        """第一次 next() 触发建仓。"""
        strat.broker.getcash.return_value = 200000.0
        strat.next()
        assert strat.buy.called

    def test_no_trades_after_init(self, strat):
        """初始化后不再买入。"""
        strat.next()  # first call → buy
        strat.buy.reset_mock()
        strat.next()  # second call → no buy
        assert not strat.buy.called


# ════════════════════════════════════════════════════════════
#  Test: EqualWeightRebalance (B2)
# ════════════════════════════════════════════════════════════


class TestEqualWeightRebalance:
    @pytest.fixture
    def strat(self):
        s = EqualWeightRebalance.__new__(EqualWeightRebalance)
        s.params = type("P", (), {
            "symbols": ["A", "B"],
            "rebalance_months": (3, 6, 9, 12),
        })()
        s.datas = [MockData(60), MockData(60)]
        s.broker = MagicMock()
        s.broker.getvalue.return_value = 200000.0
        s.buy = MagicMock()
        s.close = MagicMock()
        s.getposition = MagicMock(return_value=type("Pos", (), {"size": 100})())
        s.__dict__["_last_rebalance"] = None
        s.__dict__["_trade_summary"] = None
        return s

    def test_no_rebalance_outside_months(self, strat):
        """非3/6/9/12月 → 不触发。"""
        # 设置 1 月
        strat.datas[0]._dt = date(2024, 1, 15)
        strat.next()
        assert not strat.buy.called

    def test_rebalance_triggers_in_quarter_end(self, strat):
        """季末月 (3/6/9/12) 触发再平衡。"""
        strat.datas[0]._dt = date(2024, 3, 20)
        strat.next()
        assert strat.close.called
        assert strat.buy.called

    def test_same_quarter_only_triggers_once(self, strat):
        """同季度只触发一次。"""
        # 第一次触发
        strat.datas[0]._dt = date(2024, 3, 20)
        strat.next()
        strat.buy.reset_mock()

        # 同季度另一天
        strat.datas[0]._dt = date(2024, 3, 25)
        strat.next()
        assert not strat.buy.called


# ════════════════════════════════════════════════════════════
#  Test: ATREqualRisk (B3)
# ════════════════════════════════════════════════════════════


class TestATREqualRisk:
    @pytest.fixture
    def strat(self):
        s = ATREqualRisk.__new__(ATREqualRisk)
        s.params = type("P", (), {
            "symbols": ["A", "B"],
            "risk_per_unit": 0.01,
            "atr_period": 20,
            "atr_change_threshold": 0.30,
        })()
        # Mock data with low prices so calc_position_size returns >0
        data_a = MockData(60)
        data_b = MockData(60)
        # Force low close prices
        for d in [data_a, data_b]:
            d.close._v[0] = 5.0  # price=5 → lots = 2000/5/100 = 4 → shares=400
        s.datas = [data_a, data_b]
        s.broker = MagicMock()
        s.broker.getvalue.return_value = 200000.0
        s.buy = MagicMock()
        s.close = MagicMock()
        s.getposition = MagicMock(return_value=type("Pos", (), {"size": 100})())
        s.__dict__["_n_values"] = {}
        s.__dict__["_last_n"] = {}
        s.__dict__["_initialized"] = False
        s.__dict__["_trade_summary"] = None

        # Fill _n_values with mock data: all 1.0 for simplicity
        for code in ["A", "B"]:
            s._n_values[code] = pd.Series([1.0] * 60)
        # Track iteration count to simulate Backtrader's len()
        s.__dict__["_bt_len"] = 60
        return s

    def test_skips_before_warmup(self, strat):
        """前 20 个 bar 跳过。"""
        strat._bt_len = 15
        strat.next()
        assert not strat.buy.called

    def test_initial_buy_after_warmup(self, strat):
        """预热结束后建仓。"""
        strat._bt_len = 25
        strat.next()
        assert strat.buy.called

    def test_no_trades_if_n_value_too_low(self, strat):
        """N 值太低时跳过。"""
        for code in ["A", "B"]:
            strat._n_values[code] = pd.Series([0.0] * 60)
        strat._bt_len = 25
        strat.next()
        assert not strat.buy.called


# ════════════════════════════════════════════════════════════
#  Test: SIX_SYMBOLS 常量
# ════════════════════════════════════════════════════════════


class TestSIX_SYMBOLS:
    def test_six_symbols_count(self):
        """SIX_SYMBOLS 长度为 6。"""
        from src.benchmarks import SIX_SYMBOLS
        assert len(SIX_SYMBOLS) == 6

    def test_contains_core_etfs(self):
        """包含所有核心品种。"""
        from src.benchmarks import SIX_SYMBOLS
        assert "510500.SH" in SIX_SYMBOLS
        assert "518880.SH" in SIX_SYMBOLS
        assert "513100.SH" in SIX_SYMBOLS
