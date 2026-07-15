#!/usr/bin/env python
"""
策略组合调度引擎 — ComboEngine (S43 Phase 2c)

将 N 字结构策略与海龟趋势策略的每日净值曲线进行上层加权组合，
支持静态等权 / 风险平价(预留) / 动态(预留) 三种权重模式。

设计原则:
  - 解耦: 引擎不修改底层策略信号逻辑，仅消费预计算净值曲线
  - 归一化: 净值归一化到 1.0 消除初始资金差异
  - 可开关: 支持单独启用任一策略用于基准对比
  - 精简日志: 默认仅输出汇总绩效

用法:
    from strategies.combo_engine import ComboEngine

    engine = ComboEngine(weight_mode="equal")
    engine.feed_equity_curves(n_equity=n_series, turtle_equity=t_series)
    df = engine.combine()
    print(df.attrs["combo_metrics"]["cagr"])  # 0.1386
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

# ── 常量 ──
N_TRADING_DAYS = 252
WEIGHT_MODES = ("equal", "risk_parity", "dynamic")
REBALANCE_SCHEDULES = ("daily", "none")

# 模块级 logger（调用方可传入自定义 logger）
_logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
#  模块级工具函数
# ════════════════════════════════════════════════════════════════

def compute_metrics(
    equity_series: pd.Series,
    total_years: Optional[float] = None,
    trading_days_per_year: int = N_TRADING_DAYS,
) -> dict[str, float]:
    """从日频净值曲线计算标准绩效指标。

    Parameters
    ----------
    equity_series : pd.Series
        日频净值曲线（从 1.0 起始的归一化净值，或绝对金额均可）。
        指标对线性缩放不变（CAGR/Sharpe/MDD/Vol 均为比率）。
    total_years : float, optional
        回测年数。默认从序列长度推断 (len / trading_days_per_year)。
    trading_days_per_year : int
        年化交易日数，默认 252。

    Returns
    -------
    dict
        {"cagr": float, "vol": float, "sharpe": float,
         "mdd": float, "calmar": float, "total_return": float,
         "n_days": int, "total_years": float}
    """
    n = len(equity_series)
    if n < 2:
        return {
            "cagr": 0.0, "vol": 0.0, "sharpe": 0.0,
            "mdd": 0.0, "calmar": 0.0, "total_return": 0.0,
            "n_days": n, "total_years": 0.0,
        }

    daily_returns = equity_series.pct_change().dropna()
    if len(daily_returns) < 2:
        return {
            "cagr": 0.0, "vol": 0.0, "sharpe": 0.0,
            "mdd": 0.0, "calmar": 0.0, "total_return": 0.0,
            "n_days": n, "total_years": 0.0,
        }

    if total_years is None:
        total_years = max(0.5, len(daily_returns) / trading_days_per_year)

    # ── CAGR ──
    start_val = float(equity_series.iloc[0])
    end_val = float(equity_series.iloc[-1])
    if start_val > 0:
        total_return = end_val / start_val - 1.0
        cagr = (end_val / start_val) ** (1.0 / total_years) - 1.0
    else:
        total_return = 0.0
        cagr = 0.0

    # ── 年化波动率 ──
    vol = float(daily_returns.std() * np.sqrt(trading_days_per_year))

    # ── Sharpe ──
    mean_ret = float(daily_returns.mean())
    std_ret = float(daily_returns.std())
    if std_ret > 1e-10:
        sharpe = float((mean_ret / std_ret) * np.sqrt(trading_days_per_year))
    else:
        sharpe = 0.0

    # ── 最大回撤 (MDD) ──
    peak = equity_series.expanding().max()
    drawdowns = (equity_series - peak) / peak
    mdd = float(drawdowns.min()) if not drawdowns.empty else 0.0

    # ── Calmar ──
    calmar = cagr / abs(mdd) if abs(mdd) > 1e-10 else 0.0

    return {
        "cagr": cagr,
        "vol": vol,
        "sharpe": sharpe,
        "mdd": mdd,
        "calmar": calmar,
        "total_return": total_return,
        "n_days": n,
        "total_years": total_years,
    }


# ════════════════════════════════════════════════════════════════
#  ComboEngine
# ════════════════════════════════════════════════════════════════

class ComboEngine:
    """双策略组合调度引擎。

    接受 N 字结构和海龟策略的预计算每日净值曲线，按指定权重模式
    组合为统一组合净值，并输出绩效指标、逐年收益、滚动相关性。

    两种再平衡模式（rebalance_schedule 参数）：

    - **"daily"**（默认）：每日将权重强制复位至目标权重。
      组合净值 = w_n × N 净值 + w_t × T 净值。
      等价于每日收盘后调仓，使次日开盘权重精准回到目标。
      此模式会系统性地压低组合波动率（持续调仓稀释单策略极端走势）。

    - **"none"**：初始配置后不进行再平衡，允许权重随策略表现自然漂移。
      组合日收益率 = w_n × N 日收益率 + w_t × T 日收益率，
      组合净值由收益率累积复利得到。
      此模式适用于评估"买入持有"风格的多策略配置效果。

    Attributes
    ----------
    weight_mode : str
        权重模式 ("equal" / "risk_parity" / "dynamic")。
    rebalance_schedule : str
        再平衡频率 ("daily" / "none")。
    weights : dict
        策略权重 {"n": float, "turtle": float}。
    enable_n : bool
        是否启用 N 字结构策略。
    enable_turtle : bool
        是否启用海龟趋势策略。
    """

    def __init__(
        self,
        weight_mode: str = "equal",
        weights: Optional[dict[str, float]] = None,
        rebalance_schedule: str = "daily",
        enable_n: bool = True,
        enable_turtle: bool = True,
        name: str = "combo",
        logger: Optional[logging.Logger] = None,
    ):
        """初始化组合引擎。

        Parameters
        ----------
        weight_mode : str
            权重模式。支持 "equal"，预留 "risk_parity" / "dynamic"。
        weights : dict, optional
            自定义权重，如 {"n": 0.6, "turtle": 0.4}。
            传入后覆盖 weight_mode 自动计算。会自动归一化到 sum=1。
        rebalance_schedule : str
            再平衡频率: "daily" = 每日权重复位至目标（净值层面加权），
            "none" = 无再平衡，仅初始配置（收益率层面加权，权重自然漂移）。
        enable_n : bool
            是否启用 N 字结构策略。禁用时权重为 0。
        enable_turtle : bool
            是否启用海龟策略。禁用时权重为 0。
        name : str
            引擎标识名（用于日志前缀）。
        logger : logging.Logger, optional
            自定义 logger。默认使用模块级 logger。
        """
        if weight_mode not in WEIGHT_MODES:
            raise ValueError(
                f"不支持的权重模式: {weight_mode!r}，可选: {WEIGHT_MODES}"
            )
        if rebalance_schedule not in REBALANCE_SCHEDULES:
            raise ValueError(
                f"不支持的再平衡模式: {rebalance_schedule!r}，可选: {REBALANCE_SCHEDULES}"
            )

        self.weight_mode = weight_mode
        self._custom_weights = weights
        self.rebalance_schedule = rebalance_schedule
        self.enable_n = enable_n
        self.enable_turtle = enable_turtle
        self.name = name
        self.logger = logger or _logger

        # ── 内部状态（由 feed_equity_curves 填充） ──
        self._n_raw: Optional[pd.Series] = None
        self._t_raw: Optional[pd.Series] = None
        self._common_dates: Optional[pd.DatetimeIndex] = None
        self._is_fed: bool = False

        # ── 组合结果（由 combine 填充） ──
        self._result_df: Optional[pd.DataFrame] = None
        self._weights: dict[str, float] = {}

    # ── 公有方法 ──

    def feed_equity_curves(
        self,
        n_equity: pd.Series | pd.DataFrame,
        turtle_equity: pd.Series | pd.DataFrame,
    ) -> None:
        """摄入预计算的每日净值曲线。

        支持两种输入格式：
          - pd.Series: 索引为日期 (DatetimeIndex)，值为净值
          - pd.DataFrame: 至少含 "date" + "equity" 列

        Parameters
        ----------
        n_equity : pd.Series or pd.DataFrame
            N 字结构策略净值曲线。
        turtle_equity : pd.Series or pd.DataFrame
            海龟策略净值曲线。

        Raises
        ------
        ValueError
            净值曲线为空或格式无法识别。
        """
        self._n_raw = self._to_series(n_equity, label="N字结构")
        self._t_raw = self._to_series(turtle_equity, label="海龟")

        # ── 对齐到公共日期 ──
        common = self._n_raw.index.intersection(self._t_raw.index)
        if len(common) == 0:
            raise ValueError(
                f"N 字结构 ({len(self._n_raw)} 天) 与海龟 ({len(self._t_raw)} 天) "
                f"无公共交易日，无法组合"
            )
        self._common_dates = common.sort_values()
        self._is_fed = True

        self.logger.debug(
            "[%s] 净值摄入: N字=%d天, 海龟=%d天, 公共=%d天",
            self.name, len(self._n_raw), len(self._t_raw), len(self._common_dates),
        )

    def combine(self) -> pd.DataFrame:
        """执行组合并计算所有指标。

        必须先调用 feed_equity_curves()。

        两种再平衡模式的核心差异:

        - **daily 模式**: 组合净值 = w_n × N_净值 + w_t × T_净值。
          每日将权重强制拉回目标，等价于每日收盘后调仓。

        - **none 模式**: 组合日收益率 = w_n × N_日收益率 + w_t × T_日收益率，
          净值由收益率累积复利得到。初始配置后不再调仓，
          策略权重随各自表现自然漂移。最终两个策略对组合的
          实际贡献比例可能与初始权重有显著偏差。

        Returns
        -------
        pd.DataFrame
            列: date, n_equity_norm, t_equity_norm, combo_equity,
                 n_return, t_return, combo_return
            .attrs 包含:
                - n_metrics, t_metrics, combo_metrics: 三方绩效指标 dict
                - weight_mode, weights, rebalance_schedule: 权重与再平衡配置
                - n_trading_days, total_years: 回测规模

        Raises
        ------
        RuntimeError
            尚未摄入净值曲线。
        """
        if not self._is_fed:
            raise RuntimeError("请先调用 feed_equity_curves() 摄入净值曲线")

        # ── 1. 对齐 + 归一化 ──
        n_aligned = self._n_raw.loc[self._common_dates].copy()
        t_aligned = self._t_raw.loc[self._common_dates].copy()

        norm_n = self._normalize(n_aligned)
        norm_t = self._normalize(t_aligned)

        # ── 2. 计算权重 ──
        self._weights = self._compute_weights()
        w_n = self._weights["n"]
        w_t = self._weights["turtle"]

        # ── 3. 日收益率 ──
        ret_n = norm_n.pct_change()
        ret_t = norm_t.pct_change()

        # ── 4. 加权组合（两种再平衡模式） ──
        if self.rebalance_schedule == "daily":
            # 每日再平衡: 净值层面加权 → 每日收盘后权重复位至目标
            combo = w_n * norm_n + w_t * norm_t
            ret_combo = combo.pct_change()
        else:
            # 无再平衡 (rebalance_schedule="none"):
            #   收益率层面加权 → 仅初始配置，允许权重自然漂移
            ret_combo = w_n * ret_n + w_t * ret_t
            combo = (1.0 + ret_combo).cumprod()
            combo.iloc[0] = 1.0  # 首个 pct_change 为 NaN，初始净值为 1.0

        # ── 5. 构建输出 DataFrame ──
        self._result_df = pd.DataFrame({
            "date": self._common_dates,
            "n_equity_norm": norm_n.values,
            "t_equity_norm": norm_t.values,
            "combo_equity": combo.values,
            "n_return": ret_n.values,
            "t_return": ret_t.values,
            "combo_return": ret_combo.values,
        })

        # ── 6. 计算三方指标 ──
        total_years = max(0.5, (len(self._common_dates) - 1) / N_TRADING_DAYS)

        n_metrics = compute_metrics(norm_n, total_years=total_years)
        t_metrics = compute_metrics(norm_t, total_years=total_years)
        combo_metrics = compute_metrics(combo, total_years=total_years)

        # ── 7. 挂载元数据 ──
        self._result_df.attrs = {
            "n_metrics": {k: n_metrics[k] for k in ("cagr", "vol", "sharpe", "mdd", "calmar")},
            "t_metrics": {k: t_metrics[k] for k in ("cagr", "vol", "sharpe", "mdd", "calmar")},
            "combo_metrics": {k: combo_metrics[k] for k in ("cagr", "vol", "sharpe", "mdd", "calmar")},
            "weight_mode": self.weight_mode,
            "weights": dict(self._weights),
            "rebalance_schedule": self.rebalance_schedule,
            "n_trading_days": len(self._result_df),
            "total_years": round(total_years, 2),
        }

        # ── 8. 精简日志 ──
        self._log_summary(n_metrics, t_metrics, combo_metrics)

        return self._result_df

    # ── 分析属性 ──

    @property
    def yearly_returns(self) -> pd.DataFrame:
        """逐年收益拆分。

        Returns
        -------
        pd.DataFrame
            列: year, n_return, t_return, combo_return, n_active, t_active
        """
        if self._result_df is None:
            raise RuntimeError("请先调用 combine()")

        df = self._result_df.copy()
        df["year"] = pd.to_datetime(df["date"]).dt.year

        yearly = df.groupby("year").agg(
            n_return=("n_return", lambda x: (1 + x.dropna()).prod() - 1),
            t_return=("t_return", lambda x: (1 + x.dropna()).prod() - 1),
            combo_return=("combo_return", lambda x: (1 + x.dropna()).prod() - 1),
        ).reset_index()

        yearly["n_active"] = self.enable_n
        yearly["t_active"] = self.enable_turtle

        return yearly

    def rolling_correlation(self, window: int = N_TRADING_DAYS) -> pd.Series:
        """N 字与海龟日收益率的滚动 Pearson 相关系数。

        Parameters
        ----------
        window : int
            滚动窗宽（交易日数），默认 252（约 1 年）。

        Returns
        -------
        pd.Series
            滚动相关系数，索引为日期。
        """
        if self._result_df is None:
            raise RuntimeError("请先调用 combine()")

        df = self._result_df.copy()
        df["date"] = pd.to_datetime(df["date"])

        corr = (
            df["n_return"]
            .rolling(window=window, min_periods=max(20, window // 4))
            .corr(df["t_return"])
        )
        corr.index = df["date"]
        corr.name = "rolling_corr"
        return corr

    @property
    def metrics(self) -> dict:
        """组合结果指标（便捷访问）。

        Returns combine() 后 .attrs 的副本，或空 dict。
        """
        if self._result_df is None:
            return {}
        return dict(self._result_df.attrs)

    # ── 私有方法 ──

    @staticmethod
    def _to_series(data: pd.Series | pd.DataFrame, label: str) -> pd.Series:
        """将输入统一转为 date-indexed pd.Series。"""
        if isinstance(data, pd.Series):
            if data.empty:
                raise ValueError(f"{label} 净值曲线为空")
            s = data.copy()
            if not isinstance(s.index, pd.DatetimeIndex):
                # 尝试转换索引
                s.index = pd.to_datetime(s.index)
            s = s.sort_index()
            return s

        if isinstance(data, pd.DataFrame):
            if data.empty:
                raise ValueError(f"{label} 净值曲线为空")
            # 识别列名
            if "date" in data.columns and "equity" in data.columns:
                s = pd.Series(
                    data["equity"].values,
                    index=pd.to_datetime(data["date"]),
                    name=label,
                )
            elif "date" in data.columns:
                # 取第一个非 date 的数值列
                val_cols = [c for c in data.columns if c != "date"]
                if not val_cols:
                    raise ValueError(f"{label} DataFrame 缺少数值列")
                s = pd.Series(
                    data[val_cols[0]].values,
                    index=pd.to_datetime(data["date"]),
                    name=label,
                )
            else:
                raise ValueError(
                    f"{label} DataFrame 格式无法识别，需要 'date' + 'equity' 列"
                )
            s = s.sort_index()
            return s

        raise TypeError(f"{label} 净值曲线类型错误: {type(data)}")

    @staticmethod
    def _normalize(series: pd.Series) -> pd.Series:
        """归一化净值曲线到 1.0 起始。

        Parameters
        ----------
        series : pd.Series
            原始净值曲线。

        Returns
        -------
        pd.Series
            归一化净值 (eq / eq.iloc[0])。

        Raises
        ------
        ValueError
            净值起始值无效，或序列包含 NaN/Inf。
        """
        # ── NaN/Inf 防御（S43 修复） ──
        if series.isna().any():
            bad_count = int(series.isna().sum())
            raise ValueError(
                f"净值曲线包含 {bad_count} 个 NaN 值，"
                f"请检查上游数据源"
            )
        if np.isinf(series).any():
            bad_count = int(np.isinf(series).sum())
            raise ValueError(
                f"净值曲线包含 {bad_count} 个 Inf 值，"
                f"请检查上游数据源"
            )

        base = series.iloc[0]
        if base <= 0:
            raise ValueError(f"净值曲线起始值无效: {base}")
        return series / base

    def _compute_weights(self) -> dict[str, float]:
        """根据权重模式计算策略权重。

        Returns
        -------
        dict
            {"n": float, "turtle": float}，和为 1。
        """
        # ── 自定义权重优先 ──
        if self._custom_weights is not None:
            w_n = self._custom_weights.get("n", 0.5)
            w_t = self._custom_weights.get("turtle", 0.5)
            total = w_n + w_t
            if total <= 0:
                raise ValueError(f"自定义权重和必须 > 0，当前: n={w_n}, turtle={w_t}")
            return {"n": w_n / total, "turtle": w_t / total}

        # ── 禁用标志 ──
        n_on = self.enable_n
        t_on = self.enable_turtle
        if not n_on and not t_on:
            raise ValueError("至少需要启用一套策略")

        # ── 等权模式 ──
        if self.weight_mode == "equal":
            active_count = int(n_on) + int(t_on)
            return {
                "n": 1.0 / active_count if n_on else 0.0,
                "turtle": 1.0 / active_count if t_on else 0.0,
            }

        # ── 预留模式 ──
        if self.weight_mode == "risk_parity":
            raise NotImplementedError(
                "risk_parity 权重模式尚未实现，请先用 equal 模式"
            )
        if self.weight_mode == "dynamic":
            raise NotImplementedError(
                "dynamic 权重模式尚未实现，请先用 equal 模式"
            )

        # fallback
        return {"n": 0.5, "turtle": 0.5}

    def _log_summary(
        self,
        n_metrics: dict,
        t_metrics: dict,
        combo_metrics: dict,
    ) -> None:
        """精简输出组合绩效汇总。"""
        w = self._weights
        self.logger.info(
            "[%s] 权益组合: mode=%s rebalance=%s weights={n:%.2f, turtle:%.2f}",
            self.name, self.weight_mode, self.rebalance_schedule, w["n"], w["turtle"],
        )
        self.logger.info(
            "[%s] 公共交易日: %d 天 (%.2f 年)",
            self.name, len(self._common_dates), n_metrics["total_years"],
        )
        self.logger.info(
            "[%s] N 字    : CAGR=%5.1f%%, vol=%4.1f%%, Sharpe=%.2f, MDD=%5.1f%%, Calmar=%.2f",
            self.name,
            n_metrics["cagr"] * 100, n_metrics["vol"] * 100,
            n_metrics["sharpe"], n_metrics["mdd"] * 100, n_metrics["calmar"],
        )
        self.logger.info(
            "[%s] 海龟    : CAGR=%5.1f%%, vol=%4.1f%%, Sharpe=%.2f, MDD=%5.1f%%, Calmar=%.2f",
            self.name,
            t_metrics["cagr"] * 100, t_metrics["vol"] * 100,
            t_metrics["sharpe"], t_metrics["mdd"] * 100, t_metrics["calmar"],
        )
        self.logger.info(
            "[%s] 组合    : CAGR=%5.1f%%, vol=%4.1f%%, Sharpe=%.2f, MDD=%5.1f%%, Calmar=%.2f",
            self.name,
            combo_metrics["cagr"] * 100, combo_metrics["vol"] * 100,
            combo_metrics["sharpe"], combo_metrics["mdd"] * 100, combo_metrics["calmar"],
        )
