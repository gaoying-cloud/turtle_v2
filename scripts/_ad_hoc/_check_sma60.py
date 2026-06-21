"""探查 SMA20 < SMA60 在可做空品种上的占比。"""
import pandas as pd, numpy as np
from pathlib import Path

for code, name in [('513100.SH','纳指ETF'), ('518880.SH','黄金ETF')]:
    df = pd.read_parquet(Path(f'data/etf_daily/{code}.parquet')).sort_values('date')
    df = df[(df['date'] >= '2020-01-01') & (df['date'] <= '2026-06-10')].copy()

    close = df['close']
    sma20 = close.rolling(20).mean()
    sma60 = close.rolling(60).mean()
    low_20 = close.rolling(20).min().shift(1)

    total = len(df)
    sma20_below = (sma20 < sma60).sum()

    has_signal = close < low_20
    signal_days = has_signal.sum()
    signal_allowed = (has_signal & (sma20 < sma60)).sum()

    print(f'{name}({code}):')
    print(f'  SMA20<SMA60: {sma20_below}d / {sma20_below/total*100:.1f}%')
    print(f'  做空信号: {signal_days}d, 允许做空: {signal_allowed}d ({signal_allowed/signal_days*100:.0f}%)')

    # streaks
    below = (sma20 < sma60).astype(int)
    changes = below.diff().fillna(0)
    entries = (changes == 1).sum()
    streaks = []
    cur = 0
    for v in below:
        if v: cur += 1
        else:
            if cur: streaks.append(cur); cur = 0
    if cur: streaks.append(cur)
    if streaks:
        print(f'  最长连续: {max(streaks)}d, 平均: {np.mean(streaks):.0f}d, 事件: {entries}次')
    print()
