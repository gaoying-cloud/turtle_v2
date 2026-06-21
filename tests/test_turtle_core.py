"""
单元测试：src/turtle_core.py (S2)

覆盖范围：
    - 无状态计算函数：compute_tr, compute_atr, donchian_high/low,
      trail_high_close, calc_position_size, calc_fixed_stop,
      calc_trailing_stop, calc_pyramid_trigger, pyramid_add
    - TurtleSignals.precompute_all
    - Position dataclass
    - TurtlePositions 增删改查
    - SignalFilter 四种规则
"""

from __future__ import annotations

import sys
from datetime import date
from math import isclose
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pytest

# ── 将 src/ 加入 sys.path ──
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.turtle_core import (
    compute_tr,
    compute_atr,
    donchian_high,
    donchian_low,
    trail_high_close,
    calc_position_size,
    calc_fixed_stop,
    calc_trailing_stop,
    calc_pyramid_trigger,
    pyramid_add,
    Position,
    TurtleSignals,
    TurtlePositions,
    SignalFilter,
)


# ════════════════════════════════════════════════════════════
#  Fixtures
# ════════════════════════════════════════════════════════════

@pytest.fixture
def sample_ohlc() -> dict:
    """5 个交易日的模拟 OHLC 数据。"""
    dates = pd.date_range("2024-01-02", periods=5, freq="D")
    return {
        "high": pd.Series([10.5, 10.8, 10.6, 10.9, 11.0], index=dates),
        "low": pd.Series([10.2, 10.3, 10.1, 10.4, 10.5], index=dates),
        "close": pd.Series([10.4, 10.7, 10.3, 10.8, 10.9], index=dates),
    }


@pytest.fixture
def long_ohlc() -> dict:
    """20+ 个交易日的模拟数据（确保 ATR 和唐奇安通道有完整值）。"""
    np.random.seed(42)
    dates = pd.date_range("2024-01-02", periods=60, freq="D")
    close = 100.0 + np.cumsum(np.random.randn(60) * 0.5)
    high = close + np.abs(np.random.randn(60) * 0.3)
    low = close - np.abs(np.random.randn(60) * 0.3)
    return {
        "high": pd.Series(high, index=dates),
        "low": pd.Series(low, index=dates),
        "close": pd.Series(close, index=dates),
    }


# ════════════════════════════════════════════════════════════
#  无状态计算函数
# ════════════════════════════════════════════════════════════

class TestComputeTR:
    def test_tr_hand_calculation(self):
        """手工验算 TR。"""
        high = pd.Series([10.5, 11.0])
        low = pd.Series([10.2, 10.5])
        close = pd.Series([10.4, 10.8])

        tr = compute_tr(high, low, close)
        # 第0天：prev_close 为 NaN → TR = high - low = 0.3
        assert isclose(tr.iloc[0], 0.3)
        # 第1天：
        #   tr1 = 11.0 - 10.5 = 0.5
        #   tr2 = |11.0 - 10.4| = 0.6
        #   tr3 = |10.5 - 10.4| = 0.1
        #   TR = max(0.5, 0.6, 0.1) = 0.6
        assert isclose(tr.iloc[1], 0.6)

    def test_tr_empty_input(self):
        tr = compute_tr(pd.Series([], dtype=float), pd.Series([], dtype=float), pd.Series([], dtype=float))
        assert tr.empty


class TestComputeATR:
    def test_atr_initial_value(self, long_ohlc):
        """ATR 初始值 = 前 period 个 TR 的简单平均。"""
        tr = compute_tr(long_ohlc["high"], long_ohlc["low"], long_ohlc["close"])
        atr = compute_atr(tr, period=20)
        # 第 19 个位置（0-indexed）应该是前 20 个 TR 的平均
        expected_initial = tr.iloc[:20].mean()
        # atr 是四舍五入到 4 位小数的，所以 expected 也要相同精度
        assert abs(atr.iloc[19] - round(expected_initial, 4)) < 0.0001

    def test_atr_first_n_values_are_nan(self, long_ohlc):
        """前 period-1 个值为 NaN。"""
        tr = compute_tr(long_ohlc["high"], long_ohlc["low"], long_ohlc["close"])
        atr = compute_atr(tr, period=20)
        assert pd.isna(atr.iloc[:19]).all()

    def test_atr_empty(self):
        atr = compute_atr(pd.Series([], dtype=float))
        assert atr.empty


class TestDonchian:
    def test_donchian_high(self):
        """20 日最高价通道验证。"""
        high = pd.Series(range(1, 25))
        dh = donchian_high(high, 20)
        # 前 19 个值为 NaN（shift(1) 后不够 20 期）
        assert pd.isna(dh.iloc[:19]).all()
        # 第 20 个值 = shift(1) 后前 20 日的最大值 = max(1..20) = 20
        assert dh.iloc[20] == 20.0
        # 第 21 个值 = max(2..21) = 21
        assert dh.iloc[21] == 21.0

    def test_donchian_low(self):
        low = pd.Series(range(24, 0, -1))
        dl = donchian_low(low, 20)
        assert pd.isna(dl.iloc[:19]).all()
        # 第 20 个值 = min(24..5) = 5
        assert dl.iloc[20] == 5.0
        # 第 21 个值 = min(23..4) = 4
        assert dl.iloc[21] == 4.0


class TestTrailHighClose:
    def test_trail_high(self):
        close = pd.Series([10.0, 10.5, 10.3, 10.8, 10.6])
        th = trail_high_close(close, period=3)
        # 前 2 个为 NaN
        assert pd.isna(th.iloc[:2]).all()
        # 第 2 个 = max(10.0, 10.5, 10.3) = 10.5
        assert th.iloc[2] == 10.5
        # 第 3 个 = max(10.5, 10.3, 10.8) = 10.8
        assert th.iloc[3] == 10.8
        # 第 4 个 = max(10.3, 10.8, 10.6) = 10.8
        assert th.iloc[4] == 10.8


class TestCalcPositionSize:
    def test_basic(self):
        """标准计算。"""
        shares = calc_position_size(equity=100000, n_value=0.5, price=10.0, risk_pct=0.01)
        # theory = 100000 * 0.01 / (2 * 0.5) = 1000
        # lots = floor(1000 / 100) * 100 = 1000
        assert shares == 1000

    def test_large_equity(self):
        shares = calc_position_size(equity=1000000, n_value=0.2, price=50.0, risk_pct=0.01)
        # theory = 1000000 * 0.01 / (2 * 0.2) = 25000
        # lots = floor(25000 / 100) * 100 = 25000
        assert shares == 25000

    def test_n_value_zero_returns_zero(self):
        assert calc_position_size(100000, 0, 10.0) == 0

    def test_price_zero_returns_zero(self):
        # price 参数在新公式中不再使用，n_value为正时返回非零值
        shares = calc_position_size(100000, 0.5, 0)
        assert shares == 1000

    def test_no_lot_return_zero(self):
        """理论值不足 1 手时返回 0。"""
        shares = calc_position_size(equity=1000, n_value=5.0, price=100.0)
        # theory = 1000 * 0.01 / 5.0 = 2
        # lots = floor(2 / 100 / 100) = 0
        assert shares == 0


class TestCalcFixedStop:
    def test_basic(self):
        stop = calc_fixed_stop(entry_price=10.0, n_value=0.5, stop_mult=2.0)
        assert stop == 9.0  # 10 - 2*0.5

    def test_custom_multiple(self):
        stop = calc_fixed_stop(entry_price=50.0, n_value=1.0, stop_mult=3.0)
        assert stop == 47.0


class TestCalcTrailingStop:
    def test_trailing_stop_basic(self):
        """基础移动止损计算。"""
        stop = calc_trailing_stop(trail_price=11.0, n_value=0.5, stop_mult=2.0)
        assert stop == 10.0  # 11 - 2*0.5

    def test_trailing_only_goes_up(self):
        """只上移不下移。"""
        stop = calc_trailing_stop(trail_price=11.0, n_value=0.5, prev_stop=10.2, stop_mult=2.0)
        assert stop == 10.2

    def test_trailing_goes_up(self):
        """新计算值高于旧值时上移。"""
        stop = calc_trailing_stop(trail_price=12.0, n_value=0.5, prev_stop=10.2, stop_mult=2.0)
        assert stop == 11.0

    def test_nan_n_value_fallback(self):
        stop = calc_trailing_stop(trail_price=10.0, n_value=float("nan"), prev_stop=9.5)
        assert stop == 9.5

    def test_negative_n_value_fallback(self):
        stop = calc_trailing_stop(trail_price=10.0, n_value=-1.0, prev_stop=9.0)
        assert stop == 9.0

    def test_all_nan_returns_zero(self):
        stop = calc_trailing_stop(trail_price=float("nan"), n_value=float("nan"))
        assert stop == 0.0


class TestCalcPyramidTrigger:
    def test_first_add(self):
        """第一个加仓触发价。"""
        trigger = calc_pyramid_trigger(base_price=10.0, current_units=1, n_at_entry=0.5, step=0.5)
        assert trigger == 10.25  # 10.0 + 1 * 0.5 * 0.5

    def test_second_add(self):
        trigger = calc_pyramid_trigger(base_price=10.0, current_units=2, n_at_entry=0.5, step=0.5)
        assert trigger == 10.5  # 10.0 + 2 * 0.5 * 0.5

    def test_current_units_zero(self):
        trigger = calc_pyramid_trigger(base_price=10.0, current_units=0, n_at_entry=0.5)
        assert trigger == 10.0  # < 1 时返回 base_price


class TestPyramidAdd:
    def test_can_add(self):
        can, price = pyramid_add(current_units=2, max_units=4, base_price=10.0, n_at_entry=0.5)
        assert can
        assert price == 10.5

    def test_max_units_reached(self):
        can, price = pyramid_add(current_units=4, max_units=4, base_price=10.0, n_at_entry=0.5)
        assert not can
        assert price == 0.0

    def test_exceeds_max(self):
        can, price = pyramid_add(current_units=5, max_units=4, base_price=10.0, n_at_entry=0.5)
        assert not can
        assert price == 0.0


# ════════════════════════════════════════════════════════════
#  TurtleSignals
# ════════════════════════════════════════════════════════════

class TestTurtleSignals:
    def test_precompute_all_keys(self, long_ohlc):
        """precompute_all 返回的字典包含所有预期键。"""
        params = {"breakout_period": 20, "atr_period": 20, "stop_period": 10}
        ts = TurtleSignals(params)
        result = ts.precompute_all(long_ohlc["high"], long_ohlc["low"], long_ohlc["close"])
        expected_keys = {
            "n", "entry_high_20", "entry_low_20",
            "entry_high_55", "entry_low_55",
            "stop_high_10", "stop_low_10", "trail_high_10",
            "trail_low_10", "sma_50", "sma_20", "ma5", "ma10",
            "hurst_252", "trend_duration_median", "sma_60",
            "rsi_14", "bb_upper_20", "bb_lower_20",
        }
        assert set(result.keys()) == expected_keys

    def test_precompute_all_same_length(self, long_ohlc):
        """所有输出序列与输入长度相同。"""
        params = {"breakout_period": 20, "atr_period": 20, "stop_period": 10}
        ts = TurtleSignals(params)
        result = ts.precompute_all(long_ohlc["high"], long_ohlc["low"], long_ohlc["close"])
        expected_len = len(long_ohlc["high"])
        for key, series in result.items():
            assert len(series) == expected_len, f"{key} length mismatch"

    def test_55_channel_computed(self, long_ohlc):
        """55 日通道也被计算（即使数据不足 55 期）。"""
        params = {"breakout_period": 20, "atr_period": 20, "stop_period": 10}
        ts = TurtleSignals(params)
        result = ts.precompute_all(long_ohlc["high"], long_ohlc["low"], long_ohlc["close"])
        assert "entry_high_55" in result
        assert "entry_low_55" in result
        # 数据只有 60 期，55 日通道前 55 个值应为 NaN
        assert pd.isna(result["entry_high_55"].iloc[:55]).all()
        # 第 56 个值应为非 NaN
        assert pd.notna(result["entry_high_55"].iloc[55])


# ════════════════════════════════════════════════════════════
#  Position dataclass
# ════════════════════════════════════════════════════════════

class TestPosition:
    def test_create_default(self):
        pos = Position(symbol="510500.SH")
        assert pos.symbol == "510500.SH"
        assert pos.system == "primary"
        assert pos.direction == "long"
        assert pos.units == 1
        assert pos.total_shares == 0  # shares_per_unit=0

    def test_create_full(self):
        pos = Position(
            symbol="510500.SH",
            system="filtered",
            direction="long",
            entry_date=date(2024, 1, 15),
            entry_price=5.5,
            units=2,
            shares_per_unit=800,
            stop_loss=5.0,
        )
        assert pos.total_shares == 1600
        assert pos.market_value(5.8) == 1600 * 5.8

    def test_market_value(self):
        pos = Position(symbol="518880.SH", shares_per_unit=500)
        # units=1, shares_per_unit=500 → total_shares=500, 市值=500*100=50000
        assert pos.market_value(100.0) == 50000.0


# ════════════════════════════════════════════════════════════
#  TurtlePositions
# ════════════════════════════════════════════════════════════

class TestTurtlePositions:
    def test_open_and_get(self):
        pm = TurtlePositions()
        pos = pm.open("510500.SH", entry_price=5.5, shares=800, n_at_entry=0.12)
        assert pm.has_position("510500.SH")
        assert pm.get("510500.SH") is pos
        assert pm.count == 1
        assert pm.symbols == ["510500.SH"]

    def test_duplicate_open_raises(self):
        pm = TurtlePositions()
        pm.open("510500.SH", entry_price=5.5, shares=800, n_at_entry=0.12)
        with pytest.raises(ValueError, match="已有持仓"):
            pm.open("510500.SH", entry_price=5.6, shares=800, n_at_entry=0.13)

    def test_close(self):
        pm = TurtlePositions()
        pm.open("510500.SH", entry_price=5.5, shares=800, n_at_entry=0.12)
        closed = pm.close("510500.SH")
        assert closed is not None
        assert closed.entry_price == 5.5
        assert not pm.has_position("510500.SH")
        assert pm.count == 0

    def test_close_nonexistent(self):
        pm = TurtlePositions()
        assert pm.close("NONEXISTENT") is None

    def test_add_unit(self):
        pm = TurtlePositions(max_units=4)
        pm.open("510500.SH", entry_price=5.5, shares=800, n_at_entry=0.12)
        assert pm.add_unit("510500.SH", new_stop_loss=5.2)
        pos = pm.get("510500.SH")
        assert pos.units == 2
        assert pos.stop_loss == 5.2

    def test_add_unit_full(self):
        pm = TurtlePositions(max_units=2)
        pm.open("510500.SH", entry_price=5.5, shares=800, n_at_entry=0.12)
        pm.add_unit("510500.SH", new_stop_loss=5.2)
        assert not pm.add_unit("510500.SH", new_stop_loss=5.0)  # 已达 max_units=2
        assert pm.get("510500.SH").units == 2

    def test_add_unit_no_position(self):
        pm = TurtlePositions()
        assert not pm.add_unit("NONEXISTENT", new_stop_loss=5.0)

    def test_update_stop_loss(self):
        pm = TurtlePositions()
        pm.open("510500.SH", entry_price=5.5, shares=800, n_at_entry=0.12)
        pm.update_stop_loss("510500.SH", 5.0, stop_type="trailing")
        pos = pm.get("510500.SH")
        assert pos.stop_loss == 5.0
        assert pos.stop_type == "trailing"

    def test_update_trail_high(self):
        pm = TurtlePositions()
        pm.open("510500.SH", entry_price=5.5, shares=800, n_at_entry=0.12)
        pm.update_trail_high("510500.SH", 10.5)
        assert pm.get("510500.SH").trail_high == 10.5

    def test_is_full(self):
        pm = TurtlePositions(max_units=2)
        pm.open("510500.SH", entry_price=5.5, shares=800, n_at_entry=0.12)
        pm.add_unit("510500.SH", new_stop_loss=5.2)
        assert pm.is_full("510500.SH")
        assert not pm.is_full("OTHER")

    def test_all_positions(self):
        pm = TurtlePositions()
        pm.open("510500.SH", entry_price=5.5, shares=800, n_at_entry=0.12)
        pm.open("518880.SH", entry_price=100.0, shares=200, n_at_entry=2.0)
        all_pos = pm.all_positions()
        assert len(all_pos) == 2


# ════════════════════════════════════════════════════════════
#  SignalFilter
# ════════════════════════════════════════════════════════════

class TestSignalFilter:
    def test_first_signal_accepted(self):
        """首个信号无条件接受。"""
        sf = SignalFilter()
        ok, reason = sf.check_entry("510500.SH")
        assert ok
        assert "首个信号" in reason

    def test_reject_when_has_position(self):
        """已持仓时拒绝。"""
        sf = SignalFilter()
        sf.check_entry("510500.SH")  # 首个，通过
        sf.record_result("510500.SH", was_win=True)
        ok, reason = sf.check_entry("510500.SH", has_position=True)
        assert not ok
        assert "已持仓" in reason

    def test_accept_when_last_win(self):
        """上次盈利时接受。"""
        sf = SignalFilter()
        sf.check_entry("510500.SH")
        sf.record_result("510500.SH", was_win=True)
        ok, reason = sf.check_entry("510500.SH")
        assert ok
        assert "盈利" in reason

    def test_reject_when_last_loss(self):
        """上次亏损时跳过。"""
        sf = SignalFilter()
        sf.check_entry("510500.SH")
        sf.record_result("510500.SH", was_win=False)
        ok, reason = sf.check_entry("510500.SH")
        assert not ok
        assert "亏损" in reason

    def test_force_release_after_max_rejections(self):
        """连续拒绝 ≥ max_rejections 时强制放行。

        流程：
          1. 首个信号 → 接受
          2. record_result(was_win=False) → 设置亏损状态
          3. check_entry × 3 次（不插入 record_result）→ 前 3 次拒绝，consecutive 递增
          4. check_entry 第 4 次 → consecutive=3 ≥ max_rejections=3 → 强制放行
        """
        sf = SignalFilter(max_rejections=3)
        # 1. 首个信号
        sf.check_entry("510500.SH")
        # 2. 记录为亏损（后续信号将被拒绝）
        sf.record_result("510500.SH", was_win=False)

        # 3. 连续 3 次信号 → 拒绝，consecutive_rejections 累计
        for i in range(3):
            ok, _ = sf.check_entry("510500.SH")
            assert not ok, f"第 {i+1} 次应被拒绝"

        # 4. 第 4 次 → 强制放行
        ok, reason = sf.check_entry("510500.SH")
        assert ok
        assert "强制放行" in reason

    def test_update_position_clears_filter(self):
        """平仓后 set_position=False，盈利过滤器正常运作。"""
        sf = SignalFilter()
        sf.check_entry("510500.SH")
        sf.record_result("510500.SH", was_win=True)
        ok, _ = sf.check_entry("510500.SH", has_position=False)
        assert ok

    def test_multiple_symbols_independent(self):
        """不同品种的过滤器状态独立。"""
        sf = SignalFilter()
        sf.check_entry("510500.SH")
        sf.record_result("510500.SH", was_win=True)

        sf.check_entry("518880.SH")
        sf.record_result("518880.SH", was_win=False)

        # 510500 上次盈利 → 接受
        ok, _ = sf.check_entry("510500.SH")
        assert ok

        # 518880 上次亏损 → 拒绝
        ok, _ = sf.check_entry("518880.SH")
        assert not ok

    def test_get_stats(self):
        """get_stats 返回统计数据。"""
        sf = SignalFilter()
        sf.check_entry("510500.SH")
        sf.record_result("510500.SH", was_win=True)
        stats = sf.get_stats()
        assert "510500.SH" in stats
        assert stats["510500.SH"]["total_signals"] == 1
        assert stats["510500.SH"]["total_accepted"] == 1
