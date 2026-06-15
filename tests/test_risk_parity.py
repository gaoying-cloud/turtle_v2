"""tests/test_risk_parity.py — S4 风险平价权重模块测试"""

import numpy as np
import pytest

from src.risk_parity import (
    ledoit_wolf_cov,
    risk_parity_weights,
    compute_alpha_weights,
)


# ────────────────────────────────────────────────────────────
#  Fixtures
# ────────────────────────────────────────────────────────────


@pytest.fixture
def independent_returns() -> np.ndarray:
    """3 只完全独立（零相关）的品种，不同波动率。"""
    rng = np.random.default_rng(42)
    n = 500
    vols = [0.01, 0.02, 0.03]  # 波动率递增
    returns = np.column_stack([rng.normal(0, v, n) for v in vols])
    return returns


@pytest.fixture
def correlated_returns() -> np.ndarray:
    """3 品种，2 个高度相关 + 1 个独立。"""
    rng = np.random.default_rng(123)
    n = 500
    common = rng.normal(0, 0.015, n)
    a = common + rng.normal(0, 0.005, n)   # 高度相关
    b = common * 0.8 + rng.normal(0, 0.006, n)  # 高度相关（略低）
    c = rng.normal(0, 0.025, n)             # 独立（黄金ETF风格）
    return np.column_stack([a, b, c])


@pytest.fixture
def low_data_returns() -> np.ndarray:
    """极短时间序列（T=5, N=3）。"""
    rng = np.random.default_rng(7)
    return rng.normal(0, 0.01, (5, 3))


# ────────────────────────────────────────────────────────────
#  Test: ledoit_wolf_cov
# ────────────────────────────────────────────────────────────


class TestLedoitWolfCov:
    def test_shape(self, independent_returns):
        """输入 (T,N) → 输出 (N,N)。"""
        cov = ledoit_wolf_cov(independent_returns)
        assert cov.shape == (3, 3)

    def test_symmetric(self, independent_returns):
        """结果对称。"""
        cov = ledoit_wolf_cov(independent_returns)
        assert np.allclose(cov, cov.T, atol=1e-12)

    def test_positive_definite(self, independent_returns):
        """收缩后正定。"""
        cov = ledoit_wolf_cov(independent_returns)
        eigvals = np.linalg.eigvalsh(cov)
        assert eigvals.min() > 0

    def test_diagonal_equals_sample_variances(self, independent_returns):
        """对角线保留样本方差（无条件向目标收缩的是协方差，不是方差）。"""
        cov_lw = ledoit_wolf_cov(independent_returns)
        cov_sample = np.cov(independent_returns, rowvar=False)
        # Ledoit-Wolf 对对角线保留原值
        assert np.allclose(np.diag(cov_lw), np.diag(cov_sample), atol=1e-10)

    def test_shrinks_to_target(self, independent_returns):
        """收缩后比样本协方差更接近常数相关模型。"""
        cov_lw = ledoit_wolf_cov(independent_returns)
        cov_sample = np.cov(independent_returns, rowvar=False)

        # 计算目标矩阵
        N = 3
        variances = np.diag(cov_sample)
        stds = np.sqrt(variances)
        corr = cov_sample / np.outer(stds, stds)
        avg_corr = (np.sum(corr) - N) / (N * (N - 1))
        target = avg_corr * np.outer(stds, stds)
        np.fill_diagonal(target, variances)

        # 检查 Ledoit-Wolf 是否更接近 target
        diff_lw = np.sum((cov_lw - target) ** 2)
        diff_sample = np.sum((cov_sample - target) ** 2)
        # LW 理论上应更接近 target（或至少不更远）
        assert diff_lw <= diff_sample * 1.001  # 允许 0.1% 的容差

    def test_raises_on_1d_input(self):
        """1D 输入抛 ValueError。"""
        with pytest.raises(ValueError):
            ledoit_wolf_cov(np.array([1.0, 2.0, 3.0]))

    def test_raises_on_insufficient_data(self):
        """不足 2 个品种抛 ValueError。"""
        with pytest.raises(ValueError):
            ledoit_wolf_cov(np.random.randn(100, 1))


# ────────────────────────────────────────────────────────────
#  Test: risk_parity_weights
# ────────────────────────────────────────────────────────────


class TestRiskParityWeights:
    def test_sum_to_one(self, independent_returns):
        """权重和为 1。"""
        cov = ledoit_wolf_cov(independent_returns)
        w, _ = risk_parity_weights(cov)
        assert abs(w.sum() - 1.0) < 1e-10

    def test_all_positive(self, independent_returns):
        """所有权重 > 0。"""
        cov = ledoit_wolf_cov(independent_returns)
        w, _ = risk_parity_weights(cov)
        assert (w > 0).all()

    def test_converged(self, independent_returns):
        """在合理迭代次数内收敛。"""
        cov = ledoit_wolf_cov(independent_returns)
        w, converged = risk_parity_weights(cov)
        assert converged

    def test_equal_risk_equal_weight(self):
        """等方差、零相关 → 等权。"""
        cov = np.eye(4) * 0.01  # 单位矩阵 × 0.01
        w, _ = risk_parity_weights(cov)
        expected = np.ones(4) / 4
        assert np.allclose(w, expected, atol=1e-6)

    def test_lower_vol_gets_higher_weight(self):
        """低波动资产权重 > 高波动资产权重。"""
        # 构造：品种1 波动率 = 1, 品种2 波动率 = 4（方差 = 16）
        cov = np.array([[1.0, 0.0], [0.0, 16.0]])
        w, _ = risk_parity_weights(cov)
        assert w[0] > w[1]  # 波动低的权重更高

    def test_raises_on_non_symmetric(self):
        """非对称协方差矩阵抛 ValueError。"""
        with pytest.raises(ValueError):
            risk_parity_weights(np.array([[1.0, 0.5], [0.3, 2.0]]))

    def test_raises_on_non_positive_definite(self):
        """非正定协方差矩阵抛 ValueError。"""
        # 构造负特征值
        cov = np.array([[1.0, 2.0], [2.0, 1.0]])
        with pytest.raises(ValueError):
            risk_parity_weights(cov)

    def test_raises_on_non_square(self):
        """非方阵抛 ValueError。"""
        with pytest.raises(ValueError):
            risk_parity_weights(np.random.randn(3, 4))


# ────────────────────────────────────────────────────────────
#  Test: compute_alpha_weights
# ────────────────────────────────────────────────────────────


class TestComputeAlphaWeights:
    def test_alpha_zero_returns_equal_risk_pcts(self, independent_returns):
        """α=0 → 所有品种 risk_pct = base_risk_pct。"""
        result = compute_alpha_weights(independent_returns, alpha=0.0, base_risk_pct=0.01)
        assert np.allclose(result["risk_pcts"], 0.01, atol=1e-10)

    def test_alpha_one_gives_differentiation(self, independent_returns):
        """α=1 → 品种间存在分化。"""
        result = compute_alpha_weights(independent_returns, alpha=1.0, base_risk_pct=0.01)
        # 独立品种波动率不同（0.01, 0.02, 0.03），风险平价会分配不同权重
        risk_pcts = result["risk_pcts"]
        # 至少有两个品种的 risk_pct 不同
        assert not np.allclose(risk_pcts[0], risk_pcts[1], atol=1e-8)
        assert not np.allclose(risk_pcts[0], risk_pcts[2], atol=1e-8)

    def test_alpha_half_risk_pcts_in_between(self, independent_returns):
        """α=0.5 → risk_pcts 在 base_risk_pct 和纯风险平价之间。"""
        r0 = compute_alpha_weights(independent_returns, alpha=0.0, base_risk_pct=0.01)
        r1 = compute_alpha_weights(independent_returns, alpha=1.0, base_risk_pct=0.01)
        r05 = compute_alpha_weights(independent_returns, alpha=0.5, base_risk_pct=0.01)

        for i in range(3):
            rpct_half = r05["risk_pcts"][i]
            rpct_low = r0["risk_pcts"][i]
            rpct_high = r1["risk_pcts"][i]
            # α=0.5 居中（可能在 0 和 1 之间）
            min_rpct = min(rpct_low, rpct_high)
            max_rpct = max(rpct_low, rpct_high)
            assert min_rpct <= rpct_half <= max_rpct or abs(rpct_half - min_rpct) < 1e-10

    def test_low_data_still_works(self, low_data_returns):
        """短序列（T=5）仍然可以计算（虽然估计不稳定，但不能崩溃）。"""
        result = compute_alpha_weights(low_data_returns, alpha=0.05)
        assert result["risk_pcts"].shape[0] == 3
        assert result["converged"]

    def test_alpha_out_of_range_raises(self, independent_returns):
        """alpha<0 或 alpha>1 抛 ValueError。"""
        with pytest.raises(ValueError):
            compute_alpha_weights(independent_returns, alpha=-0.1)
        with pytest.raises(ValueError):
            compute_alpha_weights(independent_returns, alpha=1.1)

    def test_correlated_assets_stable(self, correlated_returns):
        """高相关品种仍然稳定计算。"""
        result = compute_alpha_weights(correlated_returns, alpha=0.05)
        assert result["converged"]
        assert abs(result["rp_weights"].sum() - 1.0) < 1e-8

    def test_returns_rp_weights_sum_to_one(self, independent_returns):
        """返回的 rp_weights 和为 1。"""
        result = compute_alpha_weights(independent_returns, alpha=0.05)
        assert abs(result["rp_weights"].sum() - 1.0) < 1e-8