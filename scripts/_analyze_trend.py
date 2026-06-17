import pandas as pd
import numpy as np
from pathlib import Path

DIR = Path("data/etf_daily")
codes = {
    "510500.SH": "中证500",
    "518880.SH": "黄金ETF",
    "588000.SH": "科创50",
}

def calc_forward_returns(df, period=20):
    """计算突破信号后 period 日的收益分布"""
    # 做多信号：close > 20日唐奇安高点
    df["dc20_h"] = df["high"].shift(1).rolling(20).max()
    df["signal_long"] = df["close"] > df["dc20_h"]
    df["enter_long"] = df["signal_long"] & (~df["signal_long"].shift(1).fillna(False))

    # 做空信号（向下突破）：close < 20日唐奇安低点
    df["dc20_l"] = df["low"].shift(1).rolling(20).min()
    df["signal_short"] = df["close"] < df["dc20_l"]
    df["enter_short"] = df["signal_short"] & (~df["signal_short"].shift(1).fillna(False))

    res = {}
    for direction, col in [("多头", "enter_long"), ("空头", "enter_short")]:
        idx = df[df[col]].index
        fwd_rets = []
        for i in idx:
            if i + period < len(df):
                r = df.iloc[i + period]["close"] / df.iloc[i]["close"] - 1
                fwd_rets.append(r)
        if fwd_rets:
            arr = np.array(fwd_rets) * 100
            res[direction] = {
                "信号数": len(arr),
                "胜率%": np.mean(arr > 0) * 100,
                "均值%": np.mean(arr),
                "中位数%": np.median(arr),
                "75分位%": np.percentile(arr, 75),
                "90分位%": np.percentile(arr, 90),
                "95分位%": np.percentile(arr, 95),
                "99分位%": np.percentile(arr, 99),
                "最大值%": np.max(arr),
                "最小值%": np.min(arr),
                "盈亏比": float(np.mean(arr[arr > 0]) / -np.mean(arr[arr < 0]))
                if (arr[arr > 0].any() and arr[arr < 0].any())
                else None,
            }
    return res

for code, name in codes.items():
    df = pd.read_parquet(DIR / f"{code}.parquet")
    df = df[(df["date"] >= "2020-01-01") & (df["date"] <= "2026-06-10")].copy()
    df.sort_values("date", inplace=True)
    R = calc_forward_returns(df, period=20)

    print(f"\n{'='*60}")
    print(f"  {name} — 突破信号后20日收益分布")
    print(f"{'='*60}")
    for direction, r in R.items():
        print(f"\n  [{direction}]  信号数={r['信号数']}")
        print(f"    胜率= {r['胜率%']:.1f}%  均值= {r['均值%']:+.2f}%  中位数= {r['中位数%']:+.2f}%")
        print(f"    75分位= {r['75分位%']:+.2f}%  90分位= {r['90分位%']:+.2f}%")
        print(f"    95分位= {r['95分位%']:+.2f}%  99分位= {r['99分位%']:+.2f}%")
        print(f"    最大值= {r['最大值%']:+.2f}%  最小值= {r['最小值%']:+.2f}%")
        print(f"    盈亏比= {r['盈亏比']:.2f}")