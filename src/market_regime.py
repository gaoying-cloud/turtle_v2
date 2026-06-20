"""
实盘可用的市场状态判断器

用法：
    from src.market_regime import MarketRegime
    
    regime = MarketRegime()
    for date, close, high, low in data:
        state = regime.update(date, close, high, low)
        print(regime.score, regime.state)
"""

from __future__ import annotations
from datetime import date
from typing import Optional
import numpy as np
import pandas as pd


class MarketRegime:
    """市场状态判断器 — 每日调用 update() 更新。

    三个子指标（均无 look-ahead）：
        n_pct      — N 值(ATR20)在历史 252 天中的分位
        eff_20d    — 最近 20 日的价格方向效率 = |move|/|path|
        n_trend    — 最近 60 日 N 值是扩张还是收缩
    
    融合 score = w_n × n_pct + w_eff × eff_20d + w_trend × n_trend_sign
    输出 state: "trending" | "choppy" | "transitional"
    """

    def __init__(
        self,
        n_period: int = 20,
        n_percentile_window: int = 252,
        eff_window: int = 20,
        trend_window: int = 60,
        w_n: float = 0.40,
        w_eff: float = 0.40,
        w_trend: float = 0.20,
        trending_threshold: float = 0.60,
        choppy_threshold: float = 0.35,
        n_pct_low: float = 0.30,
        n_pct_high: float = 0.70,
    ):
        # 参数
        self.n_period = n_period
        self.n_percentile_window = n_percentile_window
        self.eff_window = eff_window
        self.trend_window = trend_window
        self.w_n = w_n
        self.w_eff = w_eff
        self.w_trend = w_trend
        self.trending_threshold = trending_threshold
        self.choppy_threshold = choppy_threshold
        self.n_pct_low = n_pct_low
        self.n_pct_high = n_pct_high

        # 状态缓存
        self._dates: list[date] = []
        self._close_prices: list[float] = []
        self._high_prices: list[float] = []
        self._low_prices: list[float] = []
        self._n_values: list[float] = []
        self._score: float = 0.5
        self._state: str = "transitional"
        self._sub: dict = {"n_pct": 0.5, "eff_20d": 0.5, "n_trend": 0.0}

    @property
    def score(self) -> float:
        return self._score

    @property
    def state(self) -> str:
        return self._state

    @property
    def sub_scores(self) -> dict:
        return dict(self._sub)

    def update(self, dt: date, close: float, high: float, low: float) -> str:
        """每日调用一次，更新市场状态。

        Returns
        -------
        str
            当前状态 ("trending" / "choppy" / "transitional")
        """
        self._dates.append(dt)
        self._close_prices.append(close)
        self._high_prices.append(high)
        self._low_prices.append(low)

        # 计算当日 TR / ATR
        if len(self._close_prices) >= 2:
            prev_close = self._close_prices[-2]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        else:
            tr = high - low

        if len(self._n_values) >= 1:
            prev_n = self._n_values[-1]
            alpha = 1.0 / self.n_period
            n = (1 - alpha) * prev_n + alpha * tr
        else:
            n = tr

        self._n_values.append(n)

        n_vals = np.array(self._n_values)
        n_pct = 0.5  # 默认
        if len(n_vals) >= self.n_percentile_window:
            recent_n = n_vals[-(self.n_percentile_window + 1):-1] if len(n_vals) > self.n_percentile_window else n_vals[:-1]
            n_pct = float(np.mean(recent_n < n)) if len(recent_n) > 0 else 0.5
        self._sub["n_pct"] = n_pct

        # eff_20d: 最近 20 日方向效率
        eff = 0.5
        if len(self._close_prices) >= self.eff_window:
            closes = self._close_prices[-(self.eff_window + 1):]
            net = abs(closes[-1] - closes[0])
            path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
            eff = net / path if path > 0 else 0
        self._sub["eff_20d"] = eff

        # n_trend: N 值在最近 window 天中的走势
        n_sign = 0.0
        if len(n_vals) >= self.trend_window:
            seg = n_vals[-self.trend_window:]
            x = np.arange(len(seg))
            slope = np.polyfit(x, seg, 1)[0]
            n_sign = float(np.tanh(slope / (np.mean(seg) + 1e-10) * 100))
        self._sub["n_trend"] = n_sign

        # 融合
        self._score = self.w_n * n_pct + self.w_eff * eff + self.w_trend * max(n_sign, 0)

        if self._score > self.trending_threshold:
            self._state = "trending"
        elif self._score < self.choppy_threshold:
            self._state = "choppy"
        else:
            self._state = "transitional"

        return self._state

    def calc_n_pct(
        self, n_series: pd.Series, n_window: int = 252
    ) -> pd.Series:
        """计算 N 值序列的滚动分位（独立于 update，用于批量计算）。"""
        def _rank(ser):
            if len(ser) < 5:
                return np.nan
            val = ser.iloc[-1]
            return (ser.iloc[:-1] < val).mean()
        return n_series.rolling(n_window + 1, min_periods=n_window // 2).apply(
            _rank, raw=False
        )

    def calc_eff_20d(self, close_series: pd.Series, window: int = 20) -> pd.Series:
        """计算收盘价序列的滚动方向效率。"""
        def _eff(ser):
            if len(ser) < window + 1:
                return np.nan
            net = abs(ser.iloc[-1] - ser.iloc[0])
            path = ser.diff().abs().sum()
            return net / path if path > 0 else 0
        return close_series.rolling(window + 1).apply(_eff, raw=False)

    def calc_score_from_series(
        self, n_pct: float, eff: float, n_trend_sign: float
    ) -> float:
        """给定三个子指标值，返回融合 score。"""
        return self.w_n * n_pct + self.w_eff * eff + self.w_trend * max(n_trend_sign, 0)
