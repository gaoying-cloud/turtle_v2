#!/usr/bin/env python
"""
品种趋势质量分析：Hurst 指数 + 趋势持续时间中位数

Hurst < 0.45  → 均值回归（不适合趋势跟踪）
Hurst > 0.55  → 趋势持续（适合趋势跟踪）

趋势持续时间：连续站上/跌破 20 日均线的天数中位数
中位数 < 5 天 → 趋势太短，不适合海龟（20日突破）

用法：
    py scripts/_ad_hoc/trend_quality.py
"""
from __future__ import annotations
import sys, logging, warnings, yaml, numpy as np, pandas as pd
from pathlib import Path
warnings.filterwarnings('ignore')
logging.disable(logging.CRITICAL)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data" / "etf_daily"

# ── 分析品种（4 当前 + 2 已剔除）──
SYMBOLS = [
    ("510500.SH", "中证500", "当前"),
    ("159915.SZ", "创业板",  "当前"),
    ("513100.SH", "纳指ETF", "当前"),
    ("518880.SH", "黄金ETF", "当前"),
    ("588000.SH", "科创50",  "已剔除"),
    ("159845.SZ", "中证1000","已剔除"),
]

def hurst_exponent(ts: np.ndarray, max_lag: int = 40) -> float:
    """计算 Hurst 指数（标准 R/S 分析法）。"""
    ts = np.asarray(ts, dtype=np.float64)
    ts = ts[~np.isnan(ts)]
    if len(ts) < max_lag * 2 + 1:
        return np.nan

    # 对数收益率
    returns = np.diff(np.log(ts))
    if len(returns) < max_lag * 2:
        return np.nan

    # 减去均值
    returns = returns - np.mean(returns)

    lags = range(2, min(max_lag, len(returns) // 2 - 1))
    rs_values = []

    for lag in lags:
        # 分割为长度为 lag 的子区间
        n_segments = len(returns) // lag
        if n_segments < 1:
            continue
        segments = returns[:n_segments * lag].reshape(n_segments, lag)

        # 每个子区间的累计离差
        y = np.cumsum(segments, axis=1)

        # R = max - min
        r = np.max(y, axis=1) - np.min(y, axis=1)

        # S = 标准差
        s = np.std(segments, axis=1, ddof=1)

        # R/S
        rs = r / s
        rs_mean = np.mean(rs[~np.isnan(rs) & (s > 0)])
        if not np.isnan(rs_mean) and rs_mean > 0:
            rs_values.append(rs_mean)

    if len(rs_values) < 5:
        return np.nan

    usable_lags = lags[:len(rs_values)]
    poly = np.polyfit(np.log(usable_lags), np.log(rs_values), 1)
    return poly[0]  # H = slope


def trend_duration_median(close: pd.Series, ma_period: int = 20) -> float:
    """连续站上/跌破均线天数的中位数（仅统计完整的趋势段）。"""
    ma = close.rolling(ma_period, min_periods=ma_period).mean()
    above = (close > ma).astype(int)
    above = above.dropna()
    if len(above) < 10:
        return 0.0

    # 计算连续段的长度
    change_points = above.diff().fillna(0) != 0
    change_points.iloc[0] = True

    streaks = []
    start = 0
    for i in range(1, len(above)):
        if change_points.iloc[i]:
            streaks.append(i - start)
            start = i
    streaks.append(len(above) - start)

    return float(np.median(streaks)) if streaks else 0.0


def main():
    results = []
    print("=" * 70)
    print(f"{'品种':>12s} {'名称':<8s} {'状态':<6s} {'Hurst':>7s} {'趋势中位数':>8s} {'样本':>6s} {'判定':>6s}")
    print("-" * 70)

    for code, name, status in SYMBOLS:
        path = DATA_DIR / f"{code}.parquet"
        if not path.exists():
            print(f"{'—':>12s} {name:<8s} {status:<6s}  无数据")
            continue
        df = pd.read_parquet(path)
        df = df.sort_values("date")
        close = df["close"].values
        close_series = df["close"]

        hurst = hurst_exponent(close)
        med = trend_duration_median(close_series)

        n_years = len(df) / 252
        hurst_ok = "✅" if (hurst > 0.45 if not np.isnan(hurst) else False) else "❌"
        med_ok = "✅" if med >= 5 else "❌"

        if hurst > 0.55 and med >= 7:
            verdict = "适合趋势"
        elif hurst > 0.45 and med >= 5:
            verdict = "边界"
        else:
            verdict = "不适合"

        hurst_str = f"{hurst:.4f}" if not np.isnan(hurst) else "N/A"
        print(f"{code:>12s} {name:<8s} {status:<6s} {hurst_str:>7s} {med:>7.1f}d {len(df)//252:>5d}yr {verdict:>6s}")
        results.append({"code": code, "name": name, "status": status,
                        "hurst": hurst, "trend_median_days": med,
                        "n_bars": len(df), "verdict": verdict})

    print("=" * 70)
    print()

    # ── 总结 ──
    print("=== 结论 ===")
    for r in results:
        h_flag = "均值回归" if r["hurst"] < 0.45 else ("趋势持续" if r["hurst"] > 0.55 else "随机游走")
        m_flag = "趋势过短" if r["trend_median_days"] < 5 else ("趋势充足" if r["trend_median_days"] >= 7 else "边界")
        print(f"  {r['name']:>6s}({r['code']}): H={r['hurst']:.3f} [{h_flag}]  |  T-med={r['trend_median_days']:.0f}d [{m_flag}]  -> {r['verdict']}")


if __name__ == "__main__":
    main()
