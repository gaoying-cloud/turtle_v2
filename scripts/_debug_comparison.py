"""调试 run_comparison.py 的 list.index 错误"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backtrader as bt
import pandas as pd
import yaml
from src.benchmarks import BuyAndHold
from scripts.run_backtest import load_data, SIX_SYMBOLS

cfg = yaml.safe_load(open(ROOT / "config" / "turtle_config.yaml", encoding="utf-8"))

feeds = {}
for s in SIX_SYMBOLS:
    df = load_data(s, "2020-01-01", "2026-06-10")
    if df is not None:
        feed_df = df[["date", "open", "high", "low", "close", "volume"]].copy()
        feed_df["date"] = pd.to_datetime(feed_df["date"])
        feed_df.set_index("date", inplace=True)
        # 不传 datetime 参数，让 PandasData 默认用索引
        feed = bt.feeds.PandasData(dataname=feed_df)
        feed._name = s
        feeds[s] = feed

print(f"加载了 {len(feeds)} 个品种")

cere = bt.Cerebro()
for s in SIX_SYMBOLS:
    if s in feeds:
        cere.adddata(feeds[s], name=s)
cere.broker.setcash(cfg["initial_cash"])
cere.addstrategy(BuyAndHold, symbols=SIX_SYMBOLS)
cere.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", timeframe=bt.TimeFrame.Years)
cere.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")

print("运行 BuyAndHold...")
results = cere.run()
val = cere.broker.getvalue()
print(f"✅ B1 净值: {val:.2f}")