"""风险平价权重计算模块 (S4)

实现设计文档 §3.1.3「第三层：α 风险平价偏移」的全部计算：

1. Ledoit-Wolf 收缩协方差估计
2. 风险平价权重数值求解 (CCD)
3. α 融合权重：最终 weight = (1-α) × w_ATR + α × w_RP

纯 numpy 实现，无外部依赖 (不引入 scikit-learn/Scipy)。
"""

from typing import Optional, Tuple
import numpy as np


# ────────────────────────────────────────────────────────────
#  一、Ledoit-Wolf 收缩协方差估计
# ────────────────────────────────────────────────────────────


def ledoit_wolf_cov(returns: np.ndarray) -> np.ndarray:
    """Ledoit-Wolf 收缩协方差估计。

    将样本协方差矩阵向常数相关模型收缩，提高样本外稳定性。
    算法来自 Ledoit & Wolf (2004), "A well-conditioned estimator for
    large-dimensional covariance matrices".

    Parameters
    ----------
    returns : np.ndarray, shape (T, N)
        日收益率矩阵，T = 交易日数，N = 品种数。

    Returns
    -------
    np.ndarray, shape (N, N)
        收缩后的协方差矩阵（始终对称正定）。

    Raises
    ------
    ValueError
        当输入维度不足 (T < 2) 或包含全 NaN 时。
    """
    if returns.ndim != 2:
        raise ValueError(f"returns 须为 2D 数组，当前 shape={returns.shape}")
    T, N = returns.shape
    if T < 2 or N < 2:
        raise ValueError(f"需要至少 2 个样本和 2 个品种，当前 T={T}, N={N}")

    # 剔除全 NaN 列，再剔除含 NaN 行
    finite_mask = np.isfinite(returns)
    col_valid = finite_mask.all(axis=0)
    if col_valid.sum() < 2:
        raise ValueError(f"有效品种不足 2，当前仅 {col_valid.sum()}")
    returns = returns[:, col_valid]

    # 再次检查有限行
    row_valid = np.isfinite(returns).all(axis=1)
    returns = returns[row_valid]
    T, N = returns.shape
    if T < 2:
        raise ValueError("剔除 NaN 后有效样本不足 2")

    # 样本协方差
    sample_cov = np.cov(returns, rowvar=False)  # (N, N)

    # 目标矩阵：常数相关模型
    # 用样本方差作为对角线，所有相关系数等于平均 pairwise 相关系数
    variances = np.diag(sample_cov)
    stds = np.sqrt(variances)
    corr = sample_cov / np.outer(stds, stds)
    # 平均相关系数（不包括对角线）
    avg_corr = (np.sum(corr) - N) / (N * (N - 1))
    # 目标协方差矩阵
    target = avg_corr * np.outer(stds, stds)
    np.fill_diagonal(target, variances)

    # 最优收缩强度 δ 的解析估计
    # Pi: Σ_sample 与 Σ_sample 的"误估计方差"
    pi_sum = 0.0
    for i in range(N):
        for j in range(i, N):
            pi_ij = _pi_ij(returns[:, i], returns[:, j], sample_cov[i, j], T)
            pi_sum += pi_ij

    # rho: Σ_target 与 Σ_sample 的协方差
    rho_sum = 0.0
    for i in range(N):
        for j in range(i, N):
            rho_ij = _rho_ij(returns[:, i], returns[:, j],
                             sample_cov[i, j], target[i, j], T)
            rho_sum += rho_ij

    # gamma: ||Σ_sample - Σ_target||^2_F
    gamma = np.sum((sample_cov - target) ** 2)

    delta = max(0.0, min(1.0, (pi_sum - rho_sum) / gamma)) if gamma > 1e-12 else 0.0

    # 收缩协方差
    shrunk = (1 - delta) * sample_cov + delta * target

    # 强制对称（数值误差修正）
    shrunk = (shrunk + shrunk.T) / 2

    return shrunk


def _pi_ij(x: np.ndarray, y: np.ndarray, s_ij: float, T: int) -> float:
    """计算 Pi_ij 的估计值。"""
    # 去中心化
    xc = x - x.mean()
    yc = y - y.mean()
    # 逐元素乘积的方差
    z = xc * yc
    var_z = np.var(z, ddof=1)
    return (T / (T - 1) ** 2) * var_z * T * T  # Ledoit-Wolf 公式


def _rho_ij(x: np.ndarray, y: np.ndarray, s_ij: float, t_ij: float, T: int) -> float:
    """计算 Rho_ij 的估计值。"""
    xc = x - x.mean()
    yc = y - y.mean()
    # (x_i - x_bar)(y_i - y_bar) - s_ij
    z = xc * yc - s_ij
    # (x_i - x_bar)^2 和 (y_i - y_bar)^2
    xx = xc ** 2
    yy = yc ** 2
    # rho_ij_hat
    sum_term = np.sum(z * (xx * y.mean() / 2 + yy * x.mean() / 2))
    return (T / (T - 1) ** 2) * sum_term


# ────────────────────────────────────────────────────────────
#  二、风险平价权重数值求解
# ────────────────────────────────────────────────────────────


def risk_parity_weights(
    cov: np.ndarray,
    max_iter: int = 200,
    tol: float = 1e-10,
) -> Tuple[np.ndarray, bool]:
    """求解风险平价权重（Newton-Raphson 迭代）。

    使各资产对组合总风险的边际风险贡献 (MRC) 相等：
        MRC_i = w_i · (Σw)_i = σ² / N   (其中 σ² = w^T Σ w)

    用 Newton-Raphson 法直接求解 f(w) = Σw - diag(w)^{-1} · (σ² / N) 的根。
    参考：Spinu (2013) - An iterative algorithm for risk parity.

    Parameters
    ----------
    cov : np.ndarray, shape (N, N)
        协方差矩阵。
    max_iter : int
        最大迭代次数，默认 200。
    tol : float
        收敛容差（权重变化 L2 范数），默认 1e-10。

    Returns
    -------
    tuple
        (weights, converged)
        - weights : np.ndarray, shape (N,)
            风险平价权重，Σw_i = 1，所有元素 > 0。
        - converged : bool
            是否收敛。
    """
    if cov.ndim != 2 or cov.shape[0] != cov.shape[1]:
        raise ValueError(f"协方差矩阵须为方阵，当前 shape={cov.shape}")

    N = cov.shape[0]

    if not np.allclose(cov, cov.T, atol=1e-10):
        raise ValueError("协方差矩阵不对称")

    eigvals = np.linalg.eigvalsh(cov)
    if eigvals.min() <= 0:
        raise ValueError(f"协方差矩阵非正定（最小特征值={eigvals.min():.2e}）")

    # 初始值：等权
    w = np.ones(N) / N

    for iteration in range(max_iter):
        w_old = w.copy()

        # 当前组合协方差: Σw
        sw = cov @ w                  # (N,)
        sigma_sq = w @ sw             # w^T Σ w (标量)

        # 目标 MRC = σ² / N
        target = sigma_sq / N

        # Spinu (2013) 迭代公式:
        # w_i_new = w_i * sqrt( target / (w_i · (Σw)_i) )
        # 即 w_i_new = sqrt( target / Σ_ii ) ? 不对，是：
        # w_i_new = w_i * sqrt( sigma_sq / (N * w_i · (Σw)_i) )
        # 简化：w_i_new = w_i * sqrt( target / mrc_i )
        # 其中 mrc_i = w_i * (Σw)_i
        mrc = w * sw                  # shape (N,) 当前每个品种的边际风险贡献
        ratio = target / (mrc + 1e-30)  # 避免除零
        w = w * np.sqrt(ratio)

        # 归一化
        w = w / w.sum()

        # 收敛检查
        change = np.max(np.abs(w - w_old))
        if change < tol:
            return w, True

    return w, False


# ────────────────────────────────────────────────────────────
#  三、α 融合权重
# ────────────────────────────────────────────────────────────


def compute_alpha_weights(
    returns: np.ndarray,
    alpha: float = 0.05,
    base_risk_pct: float = 0.01,
) -> dict:
    """一步式：输入日收益率 → 输出 α 融合后的每品种 risk_pct。

    Parameters
    ----------
    returns : np.ndarray, shape (T, N)
        252 个交易日的日收益率矩阵（对数收益率或简单收益率均可）。
    alpha : float
        风险平价偏移系数，0=纯 ATR（等权），1=纯风险平价，默认 0.05。
    base_risk_pct : float
        基础单位风险比例，默认 0.01（1%）。

    Returns
    -------
    dict
        {
            "risk_pcts": np.ndarray,    # shape (N,) 每品种调整后的 risk_pct
            "rp_weights": np.ndarray,   # shape (N,) 纯风险平价权重
            "cov": np.ndarray,          # shape (N,N) 收缩协方差矩阵
            "converged": bool,          # 优化是否收敛
            "n_assets": int,            # 有效品种数
        }
    """
    if returns.ndim != 2:
        raise ValueError(f"returns 须为 2D 数组，当前 shape={returns.shape}")
    if not 0.0 <= alpha <= 1.0:
        raise ValueError(f"alpha 须在 [0,1] 范围内，当前 alpha={alpha}")

    T, N = returns.shape
    if T < 2 or N < 2:
        raise ValueError(f"数据维度不足，当前 T={T}, N={N}")

    # Step 1: Ledoit-Wolf 收缩协方差
    cov = ledoit_wolf_cov(returns)
    effective_n = cov.shape[0]  # 可能少于 N（全 NaN 列被剔除）

    # Step 2: 风险平价权重
    rp_weights, converged = risk_parity_weights(cov)

    # Step 3: ATR 等权基准 (每个品种对 ATR 的 risk_pct 贡献相等)
    # 注意：ATR 层本身是等 risk_pct（所有品种都用 base_risk_pct）
    # 所以 ATR 层的"权重"暗示为等权 1/N
    atr_weight = 1.0 / effective_n

    # Step 4: α 融合
    # 融合后的品种相对权重
    fused_weight = (1 - alpha) * atr_weight + alpha * rp_weights

    # 转回 risk_pct：base_risk_pct × (融合权重 / 等权)
    # 等权 = 1/N
    risk_pcts = base_risk_pct * fused_weight / atr_weight

    # 归一化：保持总风险敞口不变
    # 在 _check_entry 中，risk_pct 会逐个品种使用
    # base_risk_pct 对应 1%，经 α 融合后，高波动品种 risk_pct 降低，低波动品种提高

    return {
        "risk_pcts": risk_pcts,
        "rp_weights": rp_weights,
        "cov": cov,
        "converged": converged,
        "n_assets": effective_n,
    }