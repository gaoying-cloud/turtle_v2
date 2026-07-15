"""策略组合引擎单元测试 (S43 Phase 2c)"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import pytest

from strategies.combo_engine import ComboEngine, compute_metrics, N_TRADING_DAYS


# ════════════════════════════════════════════════════════════
#  测试数据构造
# ════════════════════════════════════════════════════════════

def _make_equity_series(start_val=1.0, daily_ret=0.0005, n_days=500, start_date="2020-01-02"):
    """构造每日净值曲线（固定日收益率）。"""
    dates = pd.date_range(start=start_date, periods=n_days, freq="B")
    cum = (1 + daily_ret) ** np.arange(n_days)
    return pd.Series(start_val * cum, index=dates, name="test")


def _make_equity_df(series, date_col="date", value_col="equity"):
    """将 Series 转为 export_turtle_equity 风格的 DataFrame。"""
    return pd.DataFrame({
        date_col: series.index,
        value_col: series.values,
    })


# ════════════════════════════════════════════════════════════
#  模块级 compute_metrics 测试
# ════════════════════════════════════════════════════════════

class TestComputeMetrics:
    """模块级 compute_metrics() 函数测试。"""

    def test_basic_positive_cagr(self):
        """随机游走正收益 → 正确的 CAGR。"""
        np.random.seed(42)
        dates = pd.date_range(start="2020-01-02", periods=504, freq="B")
        daily_rets = np.random.normal(0.0004, 0.01, 503)  # 均值 ~10%/年
        cum = np.cumprod(1 + daily_rets)
        eq = pd.Series(np.concatenate([[1.0], cum]), index=dates)
        m = compute_metrics(eq, total_years=2.0)
        assert abs(m["cagr"] - 0.10) < 0.05  # 随机游走容差放大
        assert m["vol"] > 0
        assert m["sharpe"] > 0

    def test_flat_equity(self):
        """净值不变 → 零收益率/波动率。"""
        dates = pd.date_range(start="2020-01-02", periods=100, freq="B")
        eq = pd.Series(1.0, index=dates)
        m = compute_metrics(eq, total_years=0.4)
        assert m["cagr"] == 0.0
        assert m["vol"] == 0.0
        assert m["sharpe"] == 0.0
        assert m["mdd"] == 0.0
        assert m["calmar"] == 0.0

    def test_mdd_calculation(self):
        """净值先涨后跌再涨 → MDD 正确。"""
        dates = pd.date_range(start="2020-01-02", periods=6, freq="B")
        # 峰值在 index 2，谷底在 index 3
        eq = pd.Series([1.0, 1.2, 1.5, 1.0, 1.1, 1.3], index=dates)
        m = compute_metrics(eq, total_years=0.02)
        # MDD = (1.0 - 1.5) / 1.5 = -0.333...
        assert abs(m["mdd"] - (-0.3333)) < 0.01

    def test_total_years_inference(self):
        """不传 total_years 时从序列长度推断。"""
        eq = _make_equity_series(n_days=504)  # ~2 年
        m = compute_metrics(eq)  # 不传 total_years
        assert 1.9 < m["total_years"] < 2.1

    def test_custom_total_years(self):
        """手动指定 total_years 优先。"""
        eq = _make_equity_series(n_days=252)
        m = compute_metrics(eq, total_years=3.5)
        assert m["total_years"] == 3.5

    def test_too_short_series(self):
        """只有 1 个点 → 返回零值不崩溃。"""
        dates = pd.date_range(start="2020-01-02", periods=1, freq="B")
        eq = pd.Series([1.0], index=dates)
        m = compute_metrics(eq)
        assert m["n_days"] == 1

    def test_total_return(self):
        """总收益率正确。"""
        eq = _make_equity_series(start_val=1.0, daily_ret=0.001, n_days=252)
        m = compute_metrics(eq, total_years=1.0)
        # 252 天，日收益 0.001 → (1.001)^251-1 ≈ 0.284
        assert 0.25 < m["total_return"] < 0.35


# ════════════════════════════════════════════════════════════
#  ComboEngine 测试
# ════════════════════════════════════════════════════════════

class TestComboEngineInit:
    """初始化参数校验。"""

    def test_default_init(self):
        engine = ComboEngine()
        assert engine.weight_mode == "equal"
        assert engine.rebalance_schedule == "daily"
        assert engine.enable_n is True
        assert engine.enable_turtle is True

    def test_invalid_weight_mode(self):
        with pytest.raises(ValueError, match="不支持的权重模式"):
            ComboEngine(weight_mode="invalid")

    def test_invalid_rebalance_schedule(self):
        with pytest.raises(ValueError, match="不支持的再平衡模式"):
            ComboEngine(rebalance_schedule="monthly")

    def test_custom_weights(self):
        engine = ComboEngine(weights={"n": 0.3, "turtle": 0.7})
        assert engine._custom_weights == {"n": 0.3, "turtle": 0.7}

    def test_disable_n(self):
        engine = ComboEngine(enable_n=False)
        assert engine.enable_n is False
        assert engine.enable_turtle is True

    def test_disable_turtle(self):
        engine = ComboEngine(enable_turtle=False)
        assert engine.enable_n is True
        assert engine.enable_turtle is False


class TestFeedEquityCurves:
    """净值曲线摄入测试。"""

    def test_series_input(self):
        engine = ComboEngine()
        n_eq = _make_equity_series(n_days=100)
        t_eq = _make_equity_series(n_days=100)
        engine.feed_equity_curves(n_equity=n_eq, turtle_equity=t_eq)
        assert engine._is_fed is True
        assert len(engine._common_dates) == 100

    def test_dataframe_input(self):
        engine = ComboEngine()
        n_eq = _make_equity_series(n_days=100)
        t_eq = _make_equity_series(n_days=100)
        engine.feed_equity_curves(
            n_equity=_make_equity_df(n_eq),
            turtle_equity=_make_equity_df(t_eq),
        )
        assert engine._is_fed is True
        assert len(engine._common_dates) == 100

    def test_mixed_input(self):
        """Series + DataFrame 混合输入。"""
        engine = ComboEngine()
        n_eq = _make_equity_series(n_days=100)
        t_eq = _make_equity_series(n_days=100)
        engine.feed_equity_curves(
            n_equity=n_eq,
            turtle_equity=_make_equity_df(t_eq),
        )
        assert engine._is_fed is True

    def test_different_start_dates_align(self):
        """不同起始日期 → 仅保留公共日期。"""
        engine = ComboEngine()
        n_eq = _make_equity_series(n_days=200, start_date="2020-01-02")
        t_eq = _make_equity_series(n_days=200, start_date="2020-03-02")
        engine.feed_equity_curves(n_equity=n_eq, turtle_equity=t_eq)
        assert len(engine._common_dates) < 200

    def test_no_overlap_raises(self):
        """完全不重叠的日期区间 → 抛出异常。"""
        engine = ComboEngine()
        n_eq = _make_equity_series(n_days=100, start_date="2018-01-02")
        t_eq = _make_equity_series(n_days=100, start_date="2025-01-02")
        with pytest.raises(ValueError, match="无公共交易日"):
            engine.feed_equity_curves(n_equity=n_eq, turtle_equity=t_eq)

    def test_empty_series_raises(self):
        engine = ComboEngine()
        with pytest.raises(ValueError, match="为空"):
            engine.feed_equity_curves(
                n_equity=pd.Series(dtype=float),
                turtle_equity=_make_equity_series(n_days=100),
            )

    def test_combine_before_feed_raises(self):
        engine = ComboEngine()
        with pytest.raises(RuntimeError, match="feed_equity_curves"):
            engine.combine()


class TestNormalization:
    """归一化测试。"""

    def test_normalize_from_one(self):
        eq = pd.Series([1.0, 1.1, 1.21], index=pd.date_range("2020-01-02", periods=3, freq="B"))
        result = ComboEngine._normalize(eq)
        assert abs(result.iloc[0] - 1.0) < 1e-10
        assert abs(result.iloc[1] - 1.1) < 1e-10
        assert abs(result.iloc[2] - 1.21) < 1e-10

    def test_normalize_from_large_value(self):
        """从大额资金归一化到 1.0。"""
        dates = pd.date_range("2020-01-02", periods=5, freq="B")
        eq = pd.Series([600000, 612000, 618000, 606000, 630000], index=dates)
        result = ComboEngine._normalize(eq)
        assert abs(result.iloc[0] - 1.0) < 1e-10
        assert abs(result.iloc[1] - 612000/600000) < 1e-10

    def test_normalize_zero_base_raises(self):
        dates = pd.date_range("2020-01-02", periods=3, freq="B")
        eq = pd.Series([0.0, 0.5, 1.0], index=dates)
        with pytest.raises(ValueError, match="起始值无效"):
            ComboEngine._normalize(eq)

    def test_normalize_with_nan_raises(self):
        """含 NaN 的净值序列 → 抛出明确异常。"""
        dates = pd.date_range("2020-01-02", periods=5, freq="B")
        eq = pd.Series([1.0, 1.1, np.nan, 1.2, 1.3], index=dates)
        with pytest.raises(ValueError, match="NaN"):
            ComboEngine._normalize(eq)

    def test_normalize_with_inf_raises(self):
        """含 Inf 的净值序列 → 抛出明确异常。"""
        dates = pd.date_range("2020-01-02", periods=5, freq="B")
        eq = pd.Series([1.0, 1.1, np.inf, 1.2, 1.3], index=dates)
        with pytest.raises(ValueError, match="Inf"):
            ComboEngine._normalize(eq)


class TestCombineEqualWeight:
    """等权组合测试。"""

    def test_equal_weight_nominal(self):
        """N字 +10%/年, 海龟 +20%/年 → 等权组合约 +15%/年。"""
        engine = ComboEngine(weight_mode="equal")
        np.random.seed(42)
        dates = pd.date_range("2020-01-02", periods=504, freq="B")
        # 使用随机游走模拟不同增长率
        n_rets = np.random.normal(0.0004, 0.01, 503)   # ~10%/年
        t_rets = np.random.normal(0.0007, 0.012, 503)  # ~18%/年
        n_cum = np.cumprod(1 + n_rets)
        t_cum = np.cumprod(1 + t_rets)
        n_eq = pd.Series(np.concatenate([[600000.0], 600000 * n_cum]), index=dates)
        t_eq = pd.Series(np.concatenate([[600000.0], 600000 * t_cum]), index=dates)

        engine.feed_equity_curves(n_equity=n_eq, turtle_equity=t_eq)
        df = engine.combine()

        # 组合指标应在两者之间
        m = df.attrs["combo_metrics"]
        n_cagr = df.attrs["n_metrics"]["cagr"]
        t_cagr = df.attrs["t_metrics"]["cagr"]
        assert m["cagr"] > min(n_cagr, t_cagr)
        assert m["cagr"] < max(n_cagr, t_cagr)

    def test_equal_weight_values(self):
        """逐日验证组合净值 = 0.5*n + 0.5*t。"""
        engine = ComboEngine(weight_mode="equal")
        dates = pd.date_range("2020-01-02", periods=5, freq="B")
        n_eq = pd.Series([600000, 606000, 618000, 612000, 624000], index=dates)
        t_eq = pd.Series([600000, 603000, 609000, 615000, 612000], index=dates)
        engine.feed_equity_curves(n_equity=n_eq, turtle_equity=t_eq)
        df = engine.combine()

        norm_n = n_eq / n_eq.iloc[0]
        norm_t = t_eq / t_eq.iloc[0]
        expected = 0.5 * norm_n + 0.5 * norm_t

        for i in range(len(dates)):
            assert abs(df["combo_equity"].iloc[i] - expected.iloc[i]) < 1e-10

    def test_weights_in_attrs(self):
        engine = ComboEngine(weight_mode="equal")
        engine.feed_equity_curves(
            n_equity=_make_equity_series(n_days=100),
            turtle_equity=_make_equity_series(n_days=100),
        )
        df = engine.combine()
        assert df.attrs["weight_mode"] == "equal"
        assert df.attrs["weights"] == {"n": 0.5, "turtle": 0.5}

    def test_result_columns(self):
        engine = ComboEngine()
        engine.feed_equity_curves(
            n_equity=_make_equity_series(n_days=100),
            turtle_equity=_make_equity_series(n_days=100),
        )
        df = engine.combine()
        expected_cols = {
            "date", "n_equity_norm", "t_equity_norm", "combo_equity",
            "n_return", "t_return", "combo_return",
        }
        assert set(df.columns) == expected_cols


class TestEnableDisable:
    """策略开关测试。"""

    def test_disable_n_mirrors_turtle(self):
        """禁用 N 字 → 组合净值 = 海龟净值。"""
        engine = ComboEngine(enable_n=False)
        n_eq = _make_equity_series(start_val=600000, n_days=100)
        t_eq = _make_equity_series(start_val=600000, n_days=100)
        engine.feed_equity_curves(n_equity=n_eq, turtle_equity=t_eq)
        df = engine.combine()

        norm_t = t_eq / t_eq.iloc[0]
        for i in range(len(df)):
            assert abs(df["combo_equity"].iloc[i] - norm_t.iloc[i]) < 1e-10

        assert df.attrs["weights"] == {"n": 0.0, "turtle": 1.0}

    def test_disable_turtle_mirrors_n(self):
        """禁用海龟 → 组合净值 = N 字净值。"""
        engine = ComboEngine(enable_turtle=False)
        n_eq = _make_equity_series(start_val=600000, n_days=100)
        t_eq = _make_equity_series(start_val=600000, n_days=100)
        engine.feed_equity_curves(n_equity=n_eq, turtle_equity=t_eq)
        df = engine.combine()

        norm_n = n_eq / n_eq.iloc[0]
        for i in range(len(df)):
            assert abs(df["combo_equity"].iloc[i] - norm_n.iloc[i]) < 1e-10

        assert df.attrs["weights"] == {"n": 1.0, "turtle": 0.0}

    def test_disable_both_raises(self):
        """同时禁用两个策略 → 抛出异常。"""
        engine = ComboEngine(enable_n=False, enable_turtle=False)
        engine.feed_equity_curves(
            n_equity=_make_equity_series(n_days=100),
            turtle_equity=_make_equity_series(n_days=100),
        )
        with pytest.raises(ValueError, match="至少需要启用"):
            engine.combine()


class TestCustomWeights:
    """自定义权重测试。"""

    def test_custom_70_30(self):
        """自定义 70/30 权重 → 组合 = 0.7*n + 0.3*t。"""
        engine = ComboEngine(weights={"n": 0.7, "turtle": 0.3})
        dates = pd.date_range("2020-01-02", periods=5, freq="B")
        n_eq = pd.Series([1.0, 1.1, 1.05, 1.15, 1.2], index=dates)
        t_eq = pd.Series([1.0, 1.02, 1.08, 1.06, 1.1], index=dates)
        engine.feed_equity_curves(n_equity=n_eq, turtle_equity=t_eq)
        df = engine.combine()

        expected = 0.7 * n_eq + 0.3 * t_eq
        for i in range(len(dates)):
            assert abs(df["combo_equity"].iloc[i] - expected.iloc[i]) < 1e-10

    def test_custom_weights_normalized(self):
        """自定义权重自动归一化到 sum=1。"""
        engine = ComboEngine(weights={"n": 1.0, "turtle": 1.0})  # 1:1 → 50/50
        engine.feed_equity_curves(
            n_equity=_make_equity_series(n_days=100),
            turtle_equity=_make_equity_series(n_days=100),
        )
        df = engine.combine()
        w = df.attrs["weights"]
        assert abs(w["n"] - 0.5) < 1e-10
        assert abs(w["turtle"] - 0.5) < 1e-10


class TestReservedModes:
    """预留权重模式测试。"""

    def test_risk_parity_not_implemented(self):
        engine = ComboEngine(weight_mode="risk_parity")
        engine.feed_equity_curves(
            n_equity=_make_equity_series(n_days=100),
            turtle_equity=_make_equity_series(n_days=100),
        )
        with pytest.raises(NotImplementedError, match="risk_parity"):
            engine.combine()

    def test_dynamic_not_implemented(self):
        engine = ComboEngine(weight_mode="dynamic")
        engine.feed_equity_curves(
            n_equity=_make_equity_series(n_days=100),
            turtle_equity=_make_equity_series(n_days=100),
        )
        with pytest.raises(NotImplementedError, match="dynamic"):
            engine.combine()


class TestYearlyReturns:
    """逐年收益拆分测试。"""

    def test_yearly_returns_structure(self):
        engine = ComboEngine()
        # 跨 3 年的数据
        n_eq = _make_equity_series(start_val=1.0, daily_ret=0.0005,
                                    n_days=756, start_date="2020-01-02")
        t_eq = _make_equity_series(start_val=1.0, daily_ret=0.0007,
                                    n_days=756, start_date="2020-01-02")
        engine.feed_equity_curves(n_equity=n_eq, turtle_equity=t_eq)
        engine.combine()

        yearly = engine.yearly_returns
        assert "year" in yearly.columns
        assert "n_return" in yearly.columns
        assert "t_return" in yearly.columns
        assert "combo_return" in yearly.columns
        assert "n_active" in yearly.columns
        assert "t_active" in yearly.columns
        assert len(yearly) >= 2  # 至少覆盖 2 个年份

    def test_yearly_before_combine_raises(self):
        engine = ComboEngine()
        with pytest.raises(RuntimeError, match="请先调用 combine"):
            _ = engine.yearly_returns


class TestRollingCorrelation:
    """滚动相关性测试。"""

    def test_perfect_positive_correlation(self):
        """完全正相关 → 相关系数 ≈ 1.0。"""
        np.random.seed(42)
        dates = pd.date_range("2020-01-02", periods=300, freq="B")
        # 使用共享的随机收益率 → 完美正相关
        shared_rets = np.random.normal(0.0005, 0.01, 299)
        n_cum = np.cumprod(1 + shared_rets)
        t_cum = np.cumprod(1 + shared_rets)  # 完全相同的收益率
        n_eq = pd.Series(np.concatenate([[1.0], n_cum]), index=dates)
        t_eq = pd.Series(np.concatenate([[1.0], t_cum]), index=dates)

        engine = ComboEngine()
        engine.feed_equity_curves(n_equity=n_eq, turtle_equity=t_eq)
        engine.combine()

        corr = engine.rolling_correlation(window=60)
        last_val = corr.dropna().iloc[-1]
        assert abs(last_val - 1.0) < 0.01

    def test_perfect_negative_correlation(self):
        """完全负相关 → 相关系数 ≈ -1.0。"""
        np.random.seed(42)
        dates = pd.date_range("2020-01-02", periods=300, freq="B")
        base_rets = np.random.normal(0.0005, 0.01, 299)
        n_cum = np.cumprod(1 + base_rets)
        t_cum = np.cumprod(1 - base_rets)  # 完全相反的收益率
        n_eq = pd.Series(np.concatenate([[1.0], n_cum]), index=dates)
        t_eq = pd.Series(np.concatenate([[1.0], t_cum]), index=dates)

        engine = ComboEngine()
        engine.feed_equity_curves(n_equity=n_eq, turtle_equity=t_eq)
        engine.combine()

        corr = engine.rolling_correlation(window=60)
        last_val = corr.dropna().iloc[-1]
        assert last_val < -0.9  # 接近 -1

    def test_rolling_before_combine_raises(self):
        engine = ComboEngine()
        with pytest.raises(RuntimeError, match="请先调用 combine"):
            _ = engine.rolling_correlation()


class TestMetricsProperty:
    """metrics 便捷属性测试。"""

    def test_metrics_returns_attrs_copy(self):
        engine = ComboEngine()
        engine.feed_equity_curves(
            n_equity=_make_equity_series(n_days=100),
            turtle_equity=_make_equity_series(n_days=100),
        )
        engine.combine()
        m = engine.metrics
        assert "combo_metrics" in m
        assert "n_metrics" in m
        assert "t_metrics" in m

    def test_metrics_before_combine(self):
        engine = ComboEngine()
        assert engine.metrics == {}


class TestRebalanceNone:
    """无再平衡模式 (rebalance_schedule="none") 测试。"""

    def test_none_mode_formula(self):
        """组合日收益率 = w_n × n_return + w_t × t_return，净值由复利累积。"""
        engine = ComboEngine(rebalance_schedule="none")
        dates = pd.date_range("2020-01-02", periods=6, freq="B")
        n_eq = pd.Series([1.0, 1.02, 1.01, 1.04, 1.03, 1.06], index=dates)
        t_eq = pd.Series([1.0, 1.01, 1.03, 1.02, 1.05, 1.04], index=dates)
        engine.feed_equity_curves(n_equity=n_eq, turtle_equity=t_eq)
        df = engine.combine()

        # 手工计算"none"模式期望值
        w_n, w_t = 0.5, 0.5
        ret_n = n_eq.pct_change()
        ret_t = t_eq.pct_change()
        expected_ret = w_n * ret_n + w_t * ret_t
        expected_combo = (1.0 + expected_ret).cumprod()
        expected_combo.iloc[0] = 1.0

        for i in range(len(dates)):
            assert abs(df["combo_return"].iloc[i] - expected_ret.iloc[i]) < 1e-10 or (
                pd.isna(df["combo_return"].iloc[i]) and pd.isna(expected_ret.iloc[i])
            )
            assert abs(df["combo_equity"].iloc[i] - expected_combo.iloc[i]) < 1e-10

    def test_none_vs_daily_different(self):
        """none 和 daily 两种模式产生不同的组合净值曲线。"""
        np.random.seed(42)
        dates = pd.date_range("2020-01-02", periods=500, freq="B")
        n_rets = np.random.normal(0.0004, 0.01, 499)
        t_rets = np.random.normal(0.0007, 0.012, 499)
        n_eq = pd.Series(np.concatenate([[1.0], np.cumprod(1 + n_rets)]), index=dates)
        t_eq = pd.Series(np.concatenate([[1.0], np.cumprod(1 + t_rets)]), index=dates)

        engine_daily = ComboEngine(rebalance_schedule="daily")
        engine_daily.feed_equity_curves(n_equity=n_eq.copy(), turtle_equity=t_eq.copy())
        df_daily = engine_daily.combine()

        engine_none = ComboEngine(rebalance_schedule="none")
        engine_none.feed_equity_curves(n_equity=n_eq.copy(), turtle_equity=t_eq.copy())
        df_none = engine_none.combine()

        # 两种模式终值不同（因再平衡效应累积）
        assert abs(df_daily["combo_equity"].iloc[-1] - df_none["combo_equity"].iloc[-1]) > 0.001

    def test_none_mode_attrs(self):
        """none 模式 attrs 中包含正确的 rebalance_schedule。"""
        engine = ComboEngine(rebalance_schedule="none")
        engine.feed_equity_curves(
            n_equity=_make_equity_series(n_days=100),
            turtle_equity=_make_equity_series(n_days=100),
        )
        df = engine.combine()
        assert df.attrs["rebalance_schedule"] == "none"

    def test_none_mode_single_strategy(self):
        """none 模式 + 禁用 N 字 → 组合净值 = 海龟净值（无漂移对象）。"""
        engine = ComboEngine(rebalance_schedule="none", enable_n=False)
        t_eq = _make_equity_series(start_val=1.0, n_days=100)
        engine.feed_equity_curves(
            n_equity=_make_equity_series(n_days=100),
            turtle_equity=t_eq,
        )
        df = engine.combine()
        for i in range(len(df)):
            assert abs(df["combo_equity"].iloc[i] - t_eq.iloc[i]) < 1e-10


class TestRandomSeedData:
    """随机种子数据的一致性测试。"""

    def test_deterministic_result(self):
        """相同输入 → 相同输出（可用作回归基准）。"""
        np.random.seed(42)
        n_eq = _make_equity_series(start_val=600000, n_days=500)
        t_eq = _make_equity_series(start_val=600000, daily_ret=0.001, n_days=500)

        engine1 = ComboEngine()
        engine1.feed_equity_curves(n_equity=n_eq.copy(), turtle_equity=t_eq.copy())
        df1 = engine1.combine()

        engine2 = ComboEngine()
        engine2.feed_equity_curves(n_equity=n_eq.copy(), turtle_equity=t_eq.copy())
        df2 = engine2.combine()

        pd.testing.assert_series_equal(
            df1["combo_equity"], df2["combo_equity"],
            check_names=False,
        )
        assert df1.attrs["combo_metrics"] == df2.attrs["combo_metrics"]
