#!/usr/bin/env python
"""
159915 逐日决策追踪：2024-04-16 后每笔入场的拒绝理由分析。

用法：py scripts/_ad_hoc/diagnose_159915_blocked.py
"""
from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import backtrader as bt  # noqa: E402
from strategies.turtle_trading import TurtleStrategy  # noqa: E402
from scripts.run_backtest import load_data, df_to_feed  # noqa: E402
from src.config_loader import load_config, get_trading_symbols, get_shortable_symbols, get_t_plus_one_symbols  # noqa: E402
from src.turtle_core import TurtleSignals  # noqa: E402

config = load_config()
symbols = get_trading_symbols(config)

feeds = {}
for sym in symbols:
    sdf = load_data(sym, "2014-01-01", "2026-06-10")
    feeds[sym] = df_to_feed(sdf, sym)

# 预计算 159915 的参考信号
sdf_159915 = load_data("159915.SZ", "2014-01-01", "2026-06-10")
ts = TurtleSignals(config["turtle"])
sig_ref = ts.precompute_all(sdf_159915["high"], sdf_159915["low"], sdf_159915["close"])
b4_signals = sdf_159915["close"].values > sig_ref["entry_high_20"].values
date_to_b4 = {d.date(): bool(b4_signals[i]) for i, d in enumerate(sdf_159915["date"])}

decision_log: list[dict] = []


class TracingTurtle(TurtleStrategy):
    """在 next() 之前和 _check_entry 内部双重记录。"""

    def log_state(self, code, data, tag: str):
        if code != "159915.SZ":
            return
        dt = data.datetime.date(0)
        if dt < pd.Timestamp("2024-01-01").date():
            return
        cash = self.broker.get_cash()
        equity = self._equity()
        idx = self._next_idx(code) if len(self) > 0 else 0
        si = self._signals.get(code, {})
        close = float(data.close[0])
        n_series = si.get("n", pd.Series([np.nan]))
        entry_h_series = si.get("entry_high_20", pd.Series([np.nan]))
        n_val = float(n_series.iloc[idx]) if idx < len(n_series) else np.nan
        entry_h = float(entry_h_series.iloc[idx]) if idx < len(entry_h_series) else np.nan
        has_breakout = bool(pd.notna(entry_h) and close > entry_h)
        sf = self._filter._states.get(code)
        decision_log.append({
            "date": dt, "close": close, "entry_high": entry_h,
            "breakout": has_breakout,
            "cash": cash, "equity": equity,
            "cash_pct": cash / equity * 100 if equity > 0 else 0,
            "in_pos": self._positions.has_position(code),
            "other_pos": sum(1 for s in symbols if s != code and self._positions.has_position(s)),
            "paused": self._paused_until.get(code),
            "n": n_val,
            "sf_last_win": sf.last_trade_was_win if sf else "N/A",
            "sf_consec": sf.consecutive_rejections if sf else 0,
            "sig_count": self._signal_count.get(code, 0),
            "tag": tag,
        })

    def next(self):
        # 记录每日状态（在 super().next() 之前，此时本品种可能持仓也可能空仓）
        for i, code in enumerate(self.params.symbols):
            if code == "159915.SZ":
                self.log_state(code, self.datas[i], "next_前")
        super().next()
        # 之后也记录一次
        for i, code in enumerate(self.params.symbols):
            if code == "159915.SZ":
                self.log_state(code, self.datas[i], "next_后")


# patch _check_entry
_orig_ce = TurtleStrategy._check_entry


def _patched_ce(self, code, data):
    if code == "159915.SZ":
        dt = data.datetime.date(0)
        if dt >= pd.Timestamp("2024-04-16").date():
            idx = self._next_idx(code)
            si = self._signals.get(code, {})
            close = float(data.close[0])
            n_series = si.get("n", pd.Series([np.nan]))
            entry_h_series = si.get("entry_high_20", pd.Series([np.nan]))
            n_val = float(n_series.iloc[idx]) if idx < len(n_series) else np.nan
            entry_h = float(entry_h_series.iloc[idx]) if idx < len(entry_h_series) else np.nan

            # 逐个检查出口点
            # 1) 暂停检查
            paused = self._paused_until.get(code)
            if paused:
                self.log_state(code, data, f"拒绝①暂停至{paused}")
                _orig_ce(self, code, data)
                return
            # 2) ATR 无效
            if pd.isna(n_val) or n_val <= 0:
                self.log_state(code, data, f"拒绝②ATR={n_val}")
                _orig_ce(self, code, data)
                return
            # 3) 方向检查（突破）
            has_breakout = bool(pd.notna(entry_h) and close > entry_h)
            if not has_breakout:
                self.log_state(code, data, f"拒绝③close{close:.4f}<=entry_h{entry_h:.4f}")
                _orig_ce(self, code, data)
                return
            # 通过了！记录通过前的状态
            self.log_state(code, data, f"✅突破通过，检查后续过滤")

    _orig_ce(self, code, data)


TurtleStrategy._check_entry = _patched_ce

# 补丁 2：拦截 _filter.check_entry 和风险平价、集中度检查
# 这些在 _check_entry 内部，我们没法直接 patch 局部变量
# 改用 post-_check_entry 日志：在 next() 后对比突破日与信号计数变化
# 但直接的 patch 更清晰

# 补丁 3：patch execute_entry (第 730 行附近) 以捕获实际入场
_orig_exec = TurtleStrategy._execute_exit if hasattr(TurtleStrategy, '_execute_exit') else None

cerebro = bt.Cerebro()
for sym, feed in feeds.items():
    cerebro.adddata(feed, name=sym)
cerebro.broker.setcash(config["initial_cash"])

cerebro.addstrategy(
    TracingTurtle,
    turtle_params=config["turtle"], symbols=symbols,
    use_55_filter=False,
    risk_per_unit=config["turtle"]["risk_per_unit"],
    concentration_trigger=config["risk"]["concentration_trigger"],
    max_consecutive_losses=config["risk"]["max_consecutive_losses"],
    max_cumulative_loss_pct=config["risk"]["max_cumulative_loss_pct"],
    pause_days=config["risk"]["pause_days"],
    max_portfolio_risk=config["risk"]["max_portfolio_risk"],
    alpha=config["weighting"]["alpha"],
    cov_lookback_days=config["weighting"]["cov_lookback_days"],
    rebalance_quarterly=config["weighting"]["rebalance_quarterly"],
    atr_change_threshold=config["weighting"]["atr_change_threshold"],
    shortable_symbols=get_shortable_symbols(config),
    t_plus_one_symbols=get_t_plus_one_symbols(config),
)

results = cerebro.run(runonce=False)
strat = results[0]

# 分析
df_log = pd.DataFrame(decision_log)
df_log["date"] = pd.to_datetime(df_log["date"])
df_log = df_log.sort_values("date").drop_duplicates(subset=["date", "tag"])

print("=" * 80)
print("159915 在 2024-04-16 后的逐日决策追踪")
print("=" * 80)

# 只看 2024-04-16 之后
mask = df_log["date"] >= "2024-04-16"
df_after = df_log[mask].copy()

# 突破日
bdays = df_after[df_after["breakout"] == True].copy()
print(f"\n突破信号日（B4 严格条件 close > entry_high_20）: {len(bdays)} 次")
print("-" * 80)
for _, r in bdays.iterrows():
    paused_str = f"暂停至{r['paused']}" if pd.notna(r['paused']) and r['paused'] else "无"
    print(f"{r['date'].date()}  close={r['close']:.4f}  entry_h={r['entry_high']:.4f}  "
          f"现金={r['cash']:>8.0f}({r['cash_pct']:>5.1f}%)  "
          f"权益={r['equity']:>8.0f}  持仓={r['in_pos']}  其他持仓={r['other_pos']}  "
          f"暂停={paused_str}  {r['tag']}")

# 每个突破日之后的下一个 tag（看拒绝理由）
print(f"\n突破日 → 对应的决策结果:")
print("-" * 80)
for _, br in bdays.iterrows():
    dt = br["date"]
    # 查找该日期之后/当天的其他 tag
    day_logs = df_after[df_after["date"] == dt]
    ce_logs = day_logs[day_logs["tag"].str.contains("拒绝|✅", na=False)]
    if not ce_logs.empty:
        for _, cl in ce_logs.iterrows():
            print(f"{cl['date'].date()}  {cl['tag']}  现金={cl['cash']:>8.0f}  "
                  f"持仓={cl['in_pos']}  其他={cl['other_pos']}  sf_上次赢={cl['sf_last_win']}  sf_连续拒={cl['sf_consec']}")
    else:
        print(f"{dt.date()}  [无决策记录]")

print()
print("=" * 80)
print("最终统计")
print("=" * 80)
print(f"  159915 总信号计数: {strat._signal_count.get('159915.SZ', 0)}")
print(f"  159915 入场计数:   {strat._enter_count.get('159915.SZ', 0)}")
print(f"  Filter拒绝:        {strat._filter_reject_count.get('159915.SZ', 0)}")
print(f"  暂停拒绝:           {strat._pause_reject_count.get('159915.SZ', 0)}")
print(f"  2024-04-16 后入场:  {sum(1 for t in strat._my_trades if t['symbol'] == '159915.SZ' and t['entry_date'] >= '2024-04-16')}")

# 各品种最终持仓
for sym in symbols:
    trades = [t for t in strat._my_trades if t["symbol"] == sym]
    print(f"  {sym}: {len(trades)} 笔, 最后退出={trades[-1]['exit_date'] if trades else '无'}")

# 现金变化
print(f"\n最终现金: {strat.broker.get_cash():.0f}")
print(f"最终权益: {strat.broker.getvalue():.0f}")
