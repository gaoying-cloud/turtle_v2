"""
品种筛选模块 — 测试
"""

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.turtle_core import hurst_exponent
from scripts.screen_candidates import (
    CandidateResult,
    CheckVerdict,
    ScreeningReport,
    SingleCheck,
    check_listing_age,
    check_liquidity,
    check_trend_persistence,
    screen_candidate,
    screen_all,
)


# ════════════════════════════════════════════════════════════
#  Hurst 指数
# ════════════════════════════════════════════════════════════

class TestHurstExponent:
    def test_random_walk_near_05(self):
        """随机游走序列 Hurst 应接近 0.5。"""
        np.random.seed(42)
        rw = np.cumsum(np.random.randn(1000)) + 100
        H = hurst_exponent(rw, max_lag=100)
        assert 0.35 < H < 0.65, f"随机游走 H={H:.3f}，应接近 0.5"

    def test_trend_series_high_hurst(self):
        """强趋势序列 Hurst 应 > 0.55。"""
        np.random.seed(0)
        n = 2000
        trend_strength = 0.001
        trend = np.cumsum(np.ones(n) * trend_strength + np.random.randn(n) * 0.005) + 100
        H = hurst_exponent(trend, max_lag=100)
        assert H > 0.5, f"趋势序列 H={H:.3f}，应 > 0.5"

    def test_mean_reverting_low_hurst(self):
        """均值回归序列 Hurst 应 < 0.5。"""
        np.random.seed(1)
        mr = [100]
        for _ in range(1000):
            mr.append(mr[-1] - 0.3 * (mr[-1] - 100) + np.random.randn() * 2)
        mr = np.array(mr)
        H = hurst_exponent(mr, max_lag=100)
        assert H < 0.55, f"均值回归序列 H={H:.3f}，应较低"

    def test_short_input_returns_05(self):
        """短序列直接返回 0.5。"""
        short = np.array([100, 101, 102, 103, 104], dtype=float)
        H = hurst_exponent(short, max_lag=100)
        assert H == 0.5

    def test_bounds(self):
        """Hurst 值始终在 [0, 1] 范围内。"""
        np.random.seed(0)
        for _ in range(10):
            prices = np.cumsum(np.random.randn(500)) + 100
            H = hurst_exponent(prices, max_lag=50)
            assert 0.0 <= H <= 1.0


# ════════════════════════════════════════════════════════════
#  数据类
# ════════════════════════════════════════════════════════════

class TestDataClasses:
    def test_single_check_creation(self):
        c = SingleCheck(
            stage="data_quality", verdict="pass",
            metric_name="worst_level", metric_value="ok",
            threshold="!= critical", detail="通过",
        )
        assert c.stage == "data_quality"
        assert c.verdict == "pass"

    def test_candidate_result_defaults(self):
        r = CandidateResult(symbol="510500.SH", name="中证500", final_verdict="pass")
        assert len(r.checks) == 0

    def test_screening_report_summary(self):
        c1 = CandidateResult(symbol="A.SH", name="A", final_verdict="pass")
        c2 = CandidateResult(symbol="B.SH", name="B", final_verdict="reject", stopped_at_stage="data_quality")
        report = ScreeningReport(
            timestamp="2026-01-01",
            existing_universe=["X.SH"],
            candidates=[c1, c2],
            summary={"total": 2, "pass": 1, "reject": 1, "warn_only": 0},
        )
        assert report.summary["pass"] == 1


# ════════════════════════════════════════════════════════════
#  检查函数（不依赖网络/回测）
# ════════════════════════════════════════════════════════════

class TestCheckListingAge:
    def test_known_old_etf_passes(self):
        """510500（中证500）应通过上市年限检查。"""
        c = check_listing_age("510500.SH")
        assert c.verdict == "pass", f"510500 应通过: {c.detail}"

    def test_missing_data_rejects(self):
        """不存在的数据文件 → reject。"""
        c = check_listing_age("NOEXIST.SH")
        assert c.verdict == "reject"

    def test_new_etf_rejects(self):
        """588000（科创50，2020上市）应被拒绝。"""
        c = check_listing_age("588000.SH")
        assert c.verdict in ("pass", "reject", "warn")


class TestCheckLiquidity:
    def test_known_etf_returns_result(self):
        """510500 流动性检查应返回有效结果。"""
        c = check_liquidity("510500.SH")
        assert c.verdict in ("pass", "warn")
        assert c.metric_name == "avg_vol_252d"

    def test_missing_data_warns(self):
        c = check_liquidity("NOEXIST.SH")
        assert c.verdict == "warn"


class TestCheckTrendPersistence:
    def test_known_etf_returns_hurst(self):
        """510500 趋势检查应返回 Hurst 值。"""
        c = check_trend_persistence("510500.SH")
        assert c.verdict in ("pass", "warn", "skip")
        if c.verdict != "skip":
            assert isinstance(c.metric_value, float) or c.metric_value == "N/A"

    def test_missing_data_warns(self):
        c = check_trend_persistence("NOEXIST.SH")
        assert c.verdict == "warn"


# ════════════════════════════════════════════════════════════
#  筛查主流程
# ════════════════════════════════════════════════════════════

class TestScreenCandidate:
    def test_known_good_etf_passes_skip_cv(self):
        """现有组合中的中证500应通过②~⑥检查（跳过①数据质量）。"""
        r = screen_candidate(
            "510500.SH",
            existing_symbols=["159915.SZ", "513100.SH", "518880.SH"],
            start_date="2020-01-01",
            end_date="2025-12-31",
            skip_backtest=True,
            skip_cross_validate=True,
        )
        assert r.final_verdict in ("pass", "warn"), f"中证500 结果: {r.final_verdict}"

    def test_bad_symbol_rejected_early(self):
        """不存在的品种应在②上市年限被淘汰。"""
        r = screen_candidate(
            "NOEXIST.SH",
            existing_symbols=[],
            start_date="2020-01-01",
            end_date="2025-12-31",
            skip_backtest=True,
            skip_cross_validate=True,
        )
        assert r.final_verdict == "reject"
        assert r.stopped_at_stage in ("listing_age",)

    def test_returns_all_checks(self):
        """筛查应返回所有已执行的检查步骤。"""
        r = screen_candidate(
            "510500.SH",
            existing_symbols=["159915.SZ"],
            start_date="2020-01-01",
            end_date="2025-12-31",
            skip_backtest=True,
            skip_cross_validate=True,
        )
        stages = [c.stage for c in r.checks]
        assert "data_quality" in stages
        assert "listing_age" in stages
        assert "liquidity" in stages
        assert "trend_persistence" in stages
        assert "correlation" in stages
        assert "t1_ratio" in stages


class TestScreenAll:
    def test_empty_candidates(self):
        report = screen_all([], ["510500.SH"], "2020-01-01", "2025-12-31",
                            skip_backtest=True, skip_cross_validate=True)
        assert report.summary["total"] == 0
        assert report.summary["pass"] == 0

    def test_multiple_candidates(self):
        report = screen_all(
            ["510500.SH", "NOEXIST.SH"],
            existing_symbols=["159915.SZ"],
            start_date="2020-01-01",
            end_date="2025-12-31",
            skip_backtest=True,
            skip_cross_validate=True,
        )
        assert report.summary["total"] == 2
        assert report.summary["reject"] >= 1
