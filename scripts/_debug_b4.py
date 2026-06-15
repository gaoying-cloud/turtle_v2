"""调试 B4 基准对比的 'close' 错误"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backtrader as bt
import yaml
from strategies.turtle_trading import TurtleStrategy
from scripts.run_backtest import load_data, df_to_feed, SIX_SYMBOLS

cfg = yaml.safe_load(open(ROOT / "config" / "turtle_config.yaml", encoding="utf-8"))

feeds = {}
for s in SIX_SYMBOLS:
    df = load_data(s, "2020-01-01", "2026-06-10")
    if df is not None:
        feed = df_to_feed(df, s)
        feed._name = s
        feeds[s] = feed

cere = bt.Cerebro()
for s in SIX_SYMBOLS:
    cere.adddata(feeds[s], name=s)
cere.broker.setcash(cfg["initial_cash"])
cere.addstrategy(TurtleStrategy, turtle_params=cfg["turtle"], symbols=SIX_SYMBOLS,
                 use_55_filter=False, alpha=cfg["weighting"]["alpha"],
                 cov_lookback_days=cfg["weighting"]["cov_lookback_days"],
                 rebalance_quarterly=cfg["weighting"]["rebalance_quarterly"],
                 atr_change_threshold=cfg["weighting"]["atr_change_threshold"])
results = cere.run()
print("B4 OK:", cere.broker.getvalue())