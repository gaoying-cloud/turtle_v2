#!/usr/bin/env python
"""
Trade Diagnostics: yearly breakdown + feature annotation + Mann-Whitney U test.
Outputs to results/diagnostics/ directory.

Usage:  py scripts/run_trade_diagnostics.py
"""
from __future__ import annotations
import sys, json, logging, warnings, gc
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml
import numpy as np
import pandas as pd
import backtrader as bt
from scipy.stats import mannwhitneyu
from datetime import datetime, timedelta

logging.disable(logging.CRITICAL)
warnings.filterwarnings("ignore")

from src.turtle_core import TurtleSignals, volume_confirmation, breakout_quality
from src.risk_parity import compute_alpha_weights
from src.data_pipeline import fetch_single
from src.config_loader import get_shortable_symbols, get_t_plus_one_symbols
from strategies.turtle_trading import TurtleStrategy

# ── Config ──
CONFIG_PATH = ROOT / "config" / "turtle_config.yaml"
DATA_DIR = ROOT / "data" / "etf_daily"
OUT_DIR = ROOT / "results" / "diagnostics"
OUT_DIR.mkdir(parents=True, exist_ok=True)

BEST_PARAMS = {"atr_period": 15, "breakout_period": 20, "stop_period": 12,
               "stop_atr_multiple": 1.5, "alpha": 0.0,
               "max_cumulative_loss_pct": 0.1, "max_consecutive_losses": 5}

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)

SYMBOLS = [s["code"] for s in CONFIG["symbols"] if s.get("shortable") is not None]
BOND_SYMBOL = [s["code"] for s in CONFIG["symbols"] if "annual_return" in s]
if BOND_SYMBOL:
    SYMBOLS.append(BOND_SYMBOL[0])

SIX_SYMBOLS = [s for s in SYMBOLS if s != (BOND_SYMBOL[0] if BOND_SYMBOL else None)]
ACTIVE_SYMBOLS = ["510500.SH", "159915.SZ", "513100.SH", "518880.SH"]


def load_data(symbol: str, start: str, end: str) -> pd.DataFrame | None:
    path = DATA_DIR / f"{symbol}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    return df[(df["date"] >= start) & (df["date"] <= end)].sort_values("date").reset_index(drop=True)


def run_full_backtest(start: str, end: str):
    """Run the full backtest with BEST_PARAMS, return (strat, cerebro)."""
    config = CONFIG
    feeds = {}
    for sym in SYMBOLS:
        df = load_data(sym, start, end)
        if df is None or df.empty:
            continue
        feed = bt.feeds.PandasData(
            dataname=df[["date", "open", "high", "low", "close", "volume"]].set_index("date"),
            open="open", high="high", low="low", close="close", volume="volume")
        feed._name = sym
        feeds[sym] = feed

    cerebro = bt.Cerebro()
    for sym in (SIX_SYMBOLS + [BOND_SYMBOL[0]] if BOND_SYMBOL else SIX_SYMBOLS):
        if sym in feeds:
            cerebro.adddata(feeds[sym], name=sym)

    cerebro.broker.setcash(config["initial_cash"])
    cm = config["commission_pct"] + config["slippage_pct"]
    cerebro.broker.setcommission(commission=cm)

    turtle_params = {
        "atr_period": BEST_PARAMS["atr_period"],
        "breakout_period": BEST_PARAMS["breakout_period"],
        "stop_period": BEST_PARAMS["stop_period"],
        "stop_atr_multiple": BEST_PARAMS["stop_atr_multiple"],
        "risk_per_unit": config["turtle"]["risk_per_unit"],
        "max_units": config["turtle"]["max_units"],
        "unit_step": config["turtle"]["unit_step"],
        "use_55_filter": False,
        "exit_period": config["turtle"]["exit_period"],
    }

    cerebro.addstrategy(TurtleStrategy,
        turtle_params=turtle_params,
        symbols=SIX_SYMBOLS,
        use_55_filter=False,
        risk_per_unit=config["turtle"]["risk_per_unit"],
        concentration_trigger=config["risk"]["concentration_trigger"],
        max_consecutive_losses=BEST_PARAMS["max_consecutive_losses"],
        max_cumulative_loss_pct=BEST_PARAMS["max_cumulative_loss_pct"],
        pause_days=config["risk"]["pause_days"],
        max_portfolio_risk=config["risk"]["max_portfolio_risk"],
        alpha=BEST_PARAMS["alpha"],
        cov_lookback_days=config["weighting"]["cov_lookback_days"],
        rebalance_quarterly=config["weighting"]["rebalance_quarterly"],
        atr_change_threshold=config["weighting"]["atr_change_threshold"],
        shortable_symbols=get_shortable_symbols(config),
        t_plus_one_symbols=get_t_plus_one_symbols(config),
    )

    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", timeframe=bt.TimeFrame.Years)
    cerebro.addanalyzer(bt.analyzers.TimeReturn, _name="timereturn", timeframe=bt.TimeFrame.Days)

    results = cerebro.run()
    if not results:
        return None, None
    return results[0], cerebro


def compute_entry_features(trades_df: pd.DataFrame) -> pd.DataFrame:
    """For each trade, annotate entry features by looking up the entry date in pre-computed signal data."""
    # Pre-compute signals for each symbol
    signal_cache = {}
    for sym in ACTIVE_SYMBOLS:
        df = load_data(sym, "2014-01-01", "2026-06-22")
        if df is None or df.empty:
            continue
        close = df["close"]
        high = df["high"]
        low = df["low"]
        open_ = df["open"]
        volume = df["volume"]
        dates_idx = pd.DatetimeIndex(df["date"])

        calcs = TurtleSignals({"atr_period": 15, "breakout_period": 20, "stop_period": 12,
                                "exit_period": 10, "risk_per_unit": 0.01, "max_units": 5,
                                "unit_step": 1})
        si = calcs.precompute_all(high, low, close)

        n_series = si["n"]
        hurst_series = si["hurst_252"]
        sma60 = si["sma_60"]
        rsi14 = si["rsi_14"]
        entry_high = si["entry_high_20"]
        sma20 = si["sma_20"]

        signal_cache[sym] = {
            "dates": dates_idx,
            "close": close, "high": high, "low": low, "open": open_, "volume": volume,
            "n": n_series, "hurst": hurst_series, "sma60": sma60,
            "rsi14": rsi14, "entry_high": entry_high, "sma20": sma20,
        }

    features_list = []
    for _, tr in trades_df.iterrows():
        sym = tr["symbol"]
        entry_dt = pd.Timestamp(tr["entry_date"])
        exit_dt = pd.Timestamp(tr["exit_date"])

        if sym not in signal_cache:
            continue
        cache = signal_cache[sym]
        dates = cache["dates"]

        # Find nearest index on or before entry date
        idx = dates.get_indexer([entry_dt], method="ffill")[0]
        if idx < 0:
            continue

        n_val = cache["n"].iloc[idx] if idx < len(cache["n"]) else np.nan
        hurst_val = cache["hurst"].iloc[idx] if idx < len(cache["hurst"]) else np.nan
        close_val = cache["close"].iloc[idx]
        high_val = cache["high"].iloc[idx]
        low_val = cache["low"].iloc[idx]
        open_val = cache["open"].iloc[idx]
        vol_val = cache["volume"].iloc[idx]
        entry_high_val = cache["entry_high"].iloc[idx] if idx < len(cache["entry_high"]) else np.nan
        sma60_val = cache["sma60"].iloc[idx] if idx < len(cache["sma60"]) else np.nan
        rsi_val = cache["rsi14"].iloc[idx] if idx < len(cache["rsi14"]) else np.nan
        sma20_val = cache["sma20"].iloc[idx] if idx < len(cache["sma20"]) else np.nan

        # Volume ratio
        lookback = 20
        vol_arr = cache["volume"].values
        if idx >= lookback:
            avg_vol = np.mean(vol_arr[idx-lookback:idx])
        else:
            avg_vol = np.nan
        vol_ratio = vol_val / avg_vol if avg_vol and avg_vol > 0 else np.nan

        # Breakout amplitude (for long: (close - entry_high_20) / N)
        amp = (close_val - entry_high_val) / n_val if (n_val and n_val > 0 and not np.isnan(entry_high_val)) else np.nan

        # K线 body ratio
        body = abs(close_val - open_val) / (high_val - low_val) if (high_val - low_val) > 0 else np.nan
        close_pos = (close_val - low_val) / (high_val - low_val) if (high_val - low_val) > 0 else np.nan

        # ATR percentile (252d)
        n_arr = cache["n"].values
        if idx >= 252:
            n_window = n_arr[max(0, idx-252+1):idx+1]
            n_pct = (n_val - n_window.min()) / (n_window.max() - n_window.min() + 1e-10) if len(n_window) > 1 else np.nan
        else:
            n_pct = np.nan

        # SMA slope (20-day)
        sma60_arr = cache["sma60"].values
        sma_slope = (sma60_val - sma60_arr[max(0, idx-20)]) / sma60_arr[max(0, idx-20)] * 100 \
            if idx >= 20 and not np.isnan(sma60_val) and sma60_arr[max(0, idx-20)] > 0 else np.nan

        # Season (quarter)
        season = f"{entry_dt.year}Q{(entry_dt.month-1)//3+1}"

        features_list.append({
            "symbol": sym,
            "entry_date": entry_dt.isoformat(),
            "exit_date": exit_dt.isoformat(),
            "holding_days": tr["holding_days"],
            "direction": tr["direction"],
            "entry_price": tr["entry_price"],
            "exit_price": tr["exit_price"],
            "pnl": tr["pnl"],
            "was_win": tr["was_win"],
            "units": tr["units"],
            "n_atr": round(n_val, 4) if not np.isnan(n_val) else None,
            "hurst_252": round(hurst_val, 4) if not np.isnan(hurst_val) else None,
            "rsi_14": round(rsi_val, 2) if not np.isnan(rsi_val) else None,
            "vol_ratio": round(vol_ratio, 2) if not np.isnan(vol_ratio) else None,
            "breakout_amplitude_n": round(amp, 4) if not np.isnan(amp) else None,
            "body_ratio": round(body, 4) if not np.isnan(body) else None,
            "close_position": round(close_pos, 4) if not np.isnan(close_pos) else None,
            "atr_percentile_252": round(n_pct, 4) if not np.isnan(n_pct) else None,
            "sma60_slope_20d": round(sma_slope, 4) if not np.isnan(sma_slope) else None,
            "sma20": round(sma20_val, 4) if not np.isnan(sma20_val) else None,
            "season": season,
        })
    return pd.DataFrame(features_list)


def run_yearly_breakdown(strat, features_df: pd.DataFrame,
                         initial_cash: float = 120000) -> pd.DataFrame:
    """从全周期回测的 TimeReturn analyzer 切年度指标。

    旧实现每年独立 run_full_backtest，导致:
      (a) 复利断裂 — 跨年持仓被切断，年度收益不连续;
      (b) pnl 重复计入 — 每年独立从 initial_cash 起算，Σpnl ≠ 真实账户增长。
    新实现只跑一次全周期回测，用每日收益率序列聚合年度收益、MDD、Calmar，
    年度交易数/胜率/pnl 则从全周期 features_df 按入场年聚合。

    ⚠️ 口径提示：return_pct / mdd_pct / calmar 基于**净值序列**
    （含跨年持仓的浮动盈亏与复利效应），而 trades / win_rate / total_pnl
    按**入场年**归属（一笔跨年交易的 pnl 全部计入开仓所在年）。
    二者口径不同，不可用 total_pnl / initial_cash 反推 return_pct
    （会因跨年持仓与复利而失配，可能出现符号相反或量级不匹配）。
    """
    if strat is None or features_df.empty:
        return pd.DataFrame()

    # ── 每日净值序列（从 TimeReturn analyzer 反推 cumulative 净值）──
    tr = strat.analyzers.timereturn.get_analysis()
    daily_ret = pd.Series(dict(tr))  # index=日期, value=当日收益率
    if daily_ret.empty:
        return pd.DataFrame()
    daily_ret.index = pd.to_datetime(daily_ret.index)
    # 净值 = initial_cash × (1+r).cumprod()
    nav = initial_cash * (1 + daily_ret).cumprod()

    years = sorted(set(nav.index.year))
    df = features_df.copy()
    df["entry_year"] = pd.to_datetime(df["entry_date"]).dt.year

    rows = []
    for y in years:
        # ── 当年净值切片（含上年末最后一个交易日作起点，保证连续复利）──
        prev_idx = nav.index[nav.index.year < y]
        start_nav = nav.loc[prev_idx[-1]] if len(prev_idx) else initial_cash
        year_nav = nav[nav.index.year == y]
        if year_nav.empty:
            continue
        end_nav = year_nav.iloc[-1]
        ret = (end_nav / start_nav - 1) * 100

        # ── 当年 MDD（基于当年净值序列的回撤）──
        running_max = year_nav.cummax()
        dd = (year_nav - running_max) / running_max * 100
        mdd = abs(dd.min()) if not dd.empty else 0.0
        calmar_val = round(ret / mdd, 4) if mdd > 1e-9 else None

        # ── 当年交易（按入场年归属）──
        yr_trades = df[df["entry_year"] == y]
        n_trades = len(yr_trades)
        wins = int(yr_trades["was_win"].sum()) if n_trades else 0
        wr = wins / n_trades * 100 if n_trades > 0 else 0
        total_pnl = float(yr_trades["pnl"].sum()) if n_trades else 0.0
        win_pnls = yr_trades.loc[yr_trades["pnl"] > 0, "pnl"]
        loss_pnls = yr_trades.loc[yr_trades["pnl"] < 0, "pnl"]
        avg_win = float(win_pnls.mean()) if len(win_pnls) else 0
        avg_loss = abs(float(loss_pnls.mean())) if len(loss_pnls) else 0
        pf = avg_win / avg_loss if avg_loss > 0 else None
        max_hold = int(yr_trades["holding_days"].max()) if n_trades else 0

        rows.append({
            "year": y, "return_pct": round(ret, 2),
            "mdd_pct": round(mdd, 2) if mdd > 0 else None,
            "calmar": calmar_val,
            "trades": n_trades, "win_rate": round(wr, 1),
            "profit_factor": round(pf, 2) if pf is not None else None,
            "total_pnl": round(total_pnl, 2),
            "max_holding_days": max_hold,
        })

    return pd.DataFrame(rows)


def run_mann_whitney(features_df: pd.DataFrame) -> pd.DataFrame:
    """Run Mann-Whitney U test for each feature comparing win vs loss groups."""
    if features_df.empty:
        return pd.DataFrame()

    numeric_cols = ["n_atr", "hurst_252", "rsi_14", "vol_ratio", "breakout_amplitude_n",
                    "body_ratio", "close_position", "atr_percentile_252", "sma60_slope_20d",
                    "holding_days"]

    results = []
    for col in numeric_cols:
        if col not in features_df.columns:
            continue
        df = features_df.dropna(subset=[col, "was_win"])
        win = df[df["was_win"]][col]
        lose = df[~df["was_win"]][col]
        if len(win) < 3 or len(lose) < 3:
            continue

        stat, p = mannwhitneyu(win, lose, alternative="two-sided")
        # Effect size: rank-biserial correlation
        n1, n2 = len(win), len(lose)
        effect = 1 - (2 * stat) / (n1 * n2)

        results.append({
            "feature": col,
            "win_median": round(win.median(), 4),
            "loss_median": round(lose.median(), 4),
            "win_count": len(win),
            "loss_count": len(lose),
            "U_statistic": round(stat, 2),
            "p_value": round(p, 4),
            "effect_size": round(effect, 4),
            "significant": "Y" if p < 0.05 and abs(effect) > 0.3 else "N",
        })

    return pd.DataFrame(results).sort_values("effect_size", key=abs, ascending=False)


# ════════════════════════════════════════════════════════════
#  5. Market Timing Diagnosis (Alpha / Beta)
# ════════════════════════════════════════════════════════════

def timing_diagnosis(features_df: pd.DataFrame) -> dict:
    """Compare strategy holding-period returns vs benchmark (HS300)."""
    # Load benchmark
    idx_path = ROOT / "data" / "index_daily" / "000300.SH.parquet"
    if not idx_path.exists():
        return {}
    idx_df = pd.read_parquet(idx_path)
    idx_df["date"] = pd.to_datetime(idx_df["date"])
    idx_close = idx_df.set_index("date")["close"]

    results = []
    for _, tr in features_df.iterrows():
        ed = pd.Timestamp(tr["entry_date"])
        xd = pd.Timestamp(tr["exit_date"])
        try:
            bench_entry = idx_close.loc[ed]
            bench_exit = idx_close.loc[xd]
        except:
            continue
        bench_ret = (bench_exit / bench_entry - 1) * 100
        # Strategy per-trade return
        strat_ret = (tr["exit_price"] / tr["entry_price"] - 1) * 100
        if tr["direction"] == "short":
            strat_ret = -strat_ret

        alpha_i = strat_ret - bench_ret
        results.append({
            "entry_date": ed, "symbol": tr["symbol"],
            "strat_ret": strat_ret, "bench_ret": bench_ret, "alpha": alpha_i,
            "was_win": tr["was_win"], "pnl": tr["pnl"],
        })

    if not results:
        return {}
    df = pd.DataFrame(results)

    # Beta via regression
    beta = np.cov(df["strat_ret"], df["bench_ret"])[0, 1] / np.var(df["bench_ret"]) if np.var(df["bench_ret"]) > 0 else 0
    mean_alpha = df["alpha"].mean()

    # Yearly breakdown
    df["year"] = df["entry_date"].dt.year
    yearly = df.groupby("year").agg(
        strat_ret=("strat_ret", "mean"),
        bench_ret=("bench_ret", "mean"),
        alpha=("alpha", "mean"),
        trades=("strat_ret", "count"),
        total_pnl=("pnl", "sum"),
    ).reset_index()
    yearly = yearly.round(2)

    return {
        "beta": round(beta, 4),
        "mean_alpha": round(mean_alpha, 4),
        "per_trade_alpha": df,
        "yearly_alpha": yearly,
    }


# ════════════════════════════════════════════════════════════
#  6. Cross-Dimensional PnL Attribution
# ════════════════════════════════════════════════════════════

def cross_attribution(features_df: pd.DataFrame):
    """Multi-dimensional PnL breakdown: Hurst buckets, monthly heatmap, position-count."""
    df = features_df.copy()

    # Hurst bucket
    df["hurst_bucket"] = pd.cut(df["hurst_252"],
        bins=[0, 0.55, 0.65, 1.5],
        labels=["H<0.55", "0.55-0.65", "H>0.65"])
    hurst_attr = df.groupby("hurst_bucket", observed=False).agg(
        trades=("pnl", "count"),
        total_pnl=("pnl", "sum"),
        win_rate=("was_win", "mean"),
        avg_pnl=("pnl", "mean"),
    ).round(2)
    hurst_attr["win_rate"] = (hurst_attr["win_rate"] * 100).round(1)

    # Monthly heatmap
    df["year"] = pd.to_datetime(df["entry_date"]).dt.year
    df["month"] = pd.to_datetime(df["entry_date"]).dt.month
    monthly = df.pivot_table(values="pnl", index="year", columns="month", aggfunc="sum").round(0)
    # Fill NaN with 0
    monthly = monthly.fillna(0).astype(int)

    # Position-count bucket (simplified: using data we have)
    per_symbol = df.groupby("symbol").agg(
        trades=("pnl", "count"),
        total_pnl=("pnl", "sum"),
        win_rate=("was_win", "mean"),
        avg_holding=("holding_days", "mean"),
    ).round(2)
    per_symbol["win_rate"] = (per_symbol["win_rate"] * 100).round(1)
    per_symbol["avg_holding"] = per_symbol["avg_holding"].round(0)

    return {
        "hurst_buckets": hurst_attr,
        "monthly_pnl": monthly,
        "per_symbol": per_symbol,
    }


# ════════════════════════════════════════════════════════════
#  7. Holding Period & Turnover
# ════════════════════════════════════════════════════════════

def holding_analysis(features_df: pd.DataFrame) -> dict:
    """Holding period distribution and turnover analysis."""
    df = features_df.copy()

    wins = df[df["was_win"]]
    losses = df[~df["was_win"]]

    # Holding distribution
    hist = df.groupby(pd.cut(df["holding_days"], bins=[0, 5, 10, 20, 30, 50, 100, 999],
                             labels=["1-5d", "6-10d", "11-20d", "21-30d", "31-50d", "51-100d", ">100d"]),
                      observed=False).agg(
        trades=("pnl", "count"),
        total_pnl=("pnl", "sum"),
        win_rate=("was_win", "mean"),
    ).round(2)
    hist["win_rate"] = (hist["win_rate"] * 100).round(1)

    # Turnover: approximate cost per trade
    # shares = |pnl| / |exit_price - entry_price|
    df["price_diff"] = abs(df["exit_price"] - df["entry_price"])
    df["approx_shares"] = np.where(df["price_diff"] > 0,
                                    abs(df["pnl"]) / df["price_diff"],
                                    0)
    df["turnover"] = df["approx_shares"] * (df["entry_price"] + df["exit_price"])

    total_turnover = df["turnover"].sum()
    total_pnl = df["pnl"].sum()

    # Cost scenarios: 0.015% commission + 0.01% slippage (current) vs 0.05% vs 0.10%
    scenarios = []
    for bp, label in [(0.025, "当前(0.025%)"), (0.05, "5bp"), (0.10, "10bp"), (0.15, "15bp")]:
        cost = total_turnover * bp / 100
        net_pnl = total_pnl - cost
        erosion = cost / total_pnl * 100 if total_pnl > 0 else 0
        scenarios.append({"label": label, "cost": cost, "net_pnl": net_pnl, "erosion_pct": round(erosion, 2)})

    return {
        "holding_histogram": hist,
        "avg_holding_win": round(wins["holding_days"].mean(), 1),
        "avg_holding_loss": round(losses["holding_days"].mean(), 1),
        "total_turnover": round(total_turnover, 2),
        "annual_turnover_rate": round(total_turnover / 12.5, 0),  # 12.5 years
        "cost_scenarios": scenarios,
    }


# ════════════════════════════════════════════════════════════
#  8. Slippage Sensitivity Stress Test
# ════════════════════════════════════════════════════════════

def slippage_stress(features_df: pd.DataFrame, baseline_final_value: float,
                    initial_cash: float = 120000, years: int = 12.5) -> dict:
    """Stress test: add fixed slippage to each trade, recompute CAGR from TRUE final value.

    baseline_final_value 必须来自 Backtrader broker.getvalue() 的真实终值（含复利），
    不能用 initial_cash + Σpnl。原实现把每笔 pnl 当成从同一初始资金独立起算、忽略复利，
    导致 CAGR 被高估（27.18% vs 扩展报告真实 14.56%）。
    """
    df = features_df.copy()

    # Approximate shares and turnover per trade
    df["price_diff"] = abs(df["exit_price"] - df["entry_price"])
    df["approx_shares"] = np.where(df["price_diff"] > 0,
                                    abs(df["pnl"]) / df["price_diff"],
                                    0)
    df["turnover"] = df["approx_shares"] * (df["entry_price"] + df["exit_price"])

    # Tick size (approximate for ETFs: 0.001 for most)
    tick = 0.001  # 1 tick = 1厘
    df["tick_cost"] = df["approx_shares"] * tick * 2  # entry + exit

    baseline_pnl = df["pnl"].sum()
    baseline_cagr = ((baseline_final_value / initial_cash) ** (1 / years) - 1) * 100

    results = []
    scenarios = [
        ("基线(无滑点)", 0, None),
        ("1跳(0.001元)", None, "tick"),
        ("5bp(0.05%)", 0.05, None),
        ("10bp(0.10%)", 0.10, None),
        ("15bp(0.15%)", 0.15, None),
    ]

    for label, bp_rate, cost_mode in scenarios:
        if cost_mode == "tick":
            total_cost = df["tick_cost"].sum()
        elif bp_rate:
            total_cost = df["turnover"].sum() * bp_rate / 100
        else:
            total_cost = 0.0

        # 从真实终值扣除执行成本
        fv = baseline_final_value - total_cost
        cagr = ((fv / initial_cash) ** (1 / years) - 1) * 100

        results.append({
            "scenario": label,
            "total_cost": round(total_cost, 2),
            "net_pnl": round(baseline_pnl - total_cost, 2),
            "final_value": round(fv, 2),
            "cagr": round(cagr, 2),
            "cagr_delta_pct": round(cagr - baseline_cagr, 2),
        })

    return {"slippage_scenarios": results, "baseline_cagr": round(baseline_cagr, 2)}


def generate_report(yearly_df: pd.DataFrame, features_df: pd.DataFrame, mw_df: pd.DataFrame,
                    timing: dict = None, cross_attr: dict = None,
                    holding: dict = None, slippage: dict = None):
    """Generate Markdown diagnostic report."""
    lines = []
    lines.append("# Trade Diagnostics Report\n")
    lines.append(f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    lines.append(f"**Period**: 2014-01-02 ~ 2026-06-22\n")
    lines.append(f"**Params**: ATR=15, Breakout=20, Stop=12, 1.5xATR, alpha=0, Mode A\n")
    lines.append(f"**Total trades**: {len(features_df)}\n")
    if not features_df.empty:
        lines.append(f"**Wins**: {features_df['was_win'].sum()} / **Losses**: {(~features_df['was_win']).sum()}\n")
    lines.append("\n---\n")

    # Yearly breakdown
    if not yearly_df.empty:
        lines.append("## 1. Yearly Breakdown\n")
        lines.append("| Year | Return% | MDD% | Calmar | Trades | WinRate% | ProfitFactor | TotalPnL | MaxHold(d) |")
        lines.append("|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|")
        for _, r in yearly_df.iterrows():
            pf = f"{r['profit_factor']:.2f}" if r['profit_factor'] else "inf"
            mdd_s = f"{r['mdd_pct']:.1f}" if r['mdd_pct'] is not None else "N/A"
            cal_s = f"{r['calmar']:.3f}" if r['calmar'] is not None else "N/A"
            lines.append(f"| {r['year']} | {r['return_pct']:+.2f} | {mdd_s} | {cal_s} | "
                         f"{r['trades']} | {r['win_rate']:.1f} | {pf} | "
                         f"{r['total_pnl']:+,.0f} | {r['max_holding_days']} |")
        lines.append("")

    # Trade features table
    if not features_df.empty:
        lines.append("\n---\n## 2. Trade Export (first 20 rows)\n")
        display_cols = ["symbol", "entry_date", "holding_days", "pnl", "was_win",
                        "hurst_252", "vol_ratio", "body_ratio"]
        lines.append(features_df[display_cols].head(20).to_string(index=False))
        # Save full CSV
        csv_path = OUT_DIR / "trades_features.csv"
        features_df.to_csv(csv_path, index=False, encoding="utf-8")
        lines.append(f"\n\nFull export saved to `{csv_path}` ({len(features_df)} rows)\n")

    # Mann-Whitney U results
    if not mw_df.empty:
        lines.append("\n---\n## 3. Mann-Whitney U Test Results\n")
        lines.append("| Feature | Win Median | Loss Median | Win:N | Loss:N | U | p-value | Effect| Sig? |")
        lines.append("|:--|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|")
        for _, r in mw_df.iterrows():
            sig = "**Y**" if r["significant"] == "Y" else "N"
            lines.append(f"| {r['feature']} | {r['win_median']} | {r['loss_median']} | "
                         f"{r['win_count']} | {r['loss_count']} | {r['U_statistic']} | "
                         f"{r['p_value']} | {r['effect_size']} | {sig} |")
        lines.append("")
        sig_count = (mw_df["significant"] == "Y").sum()
        lines.append(f"\n**Conclusion**: {sig_count}/{len(mw_df)} features show significant discrimination "
                     f"(p<0.05 & |effect|>0.3). "
                     f"{'Trades may have identifiable structural features.' if sig_count > 0 else 'No evidence that trade outcomes are predictable from entry features.'}\n")

    # Win/Loss analysis
    if not features_df.empty:
        wins = features_df[features_df["was_win"]]
        losses = features_df[~features_df["was_win"]]
        lines.append("\n---\n## 4. PnL Distribution\n")
        lines.append(f"- Top 20% trades PnL share: ")
        top20 = features_df.nlargest(max(1, len(features_df)//5), "pnl")["pnl"].sum()
        total = features_df["pnl"].sum()
        lines[-1] += f"{top20/total*100:.1f}% (top {max(1, len(features_df)//5)}/{len(features_df)} trades)\n"
        lines.append(f"- Avg win: {wins['pnl'].mean():+,.0f} / Avg loss: {losses['pnl'].mean():+,.0f}\n")
        lines.append(f"- Win/Loss ratio: {wins['pnl'].mean()/abs(losses['pnl'].mean()):.2f}\n")

    # Timing diagnosis
    if timing:
        lines.append("\n---\n## 5. Market Timing Diagnosis\n")
        lines.append(f"- Strategy Beta (to HS300): **{timing['beta']:.3f}**\n")
        lines.append(f"- Mean Alpha per trade: **{timing['mean_alpha']:+.2f}%**\n")
        ya = timing["yearly_alpha"]
        if not ya.empty:
            lines.append("\n| Year | Trades | StratRet% | BenchRet% | Alpha% | TotalPnL |")
            lines.append("|:--:|:--:|:--:|:--:|:--:|:--:|")
            for _, r in ya.iterrows():
                lines.append(f"| {int(r['year'])} | {int(r['trades'])} | "
                             f"{r['strat_ret']:+.2f} | {r['bench_ret']:+.2f} | "
                             f"{r['alpha']:+.2f} | {r['total_pnl']:+,.0f} |")
            lines.append("")
        if timing["beta"] < 0.2 and timing["mean_alpha"] > 0:
            lines.append("> **结论**: 策略收益与指数弱相关，Alpha 为正，存在独立的择时能力。\n")
        elif timing["beta"] > 0.5:
            lines.append("> **结论**: 策略收益与指数强相关，利润主要由 Beta 驱动。\n")
        else:
            lines.append("> **结论**: 策略与指数弱相关，但 Alpha 有限。\n")

    # Cross attribution
    if cross_attr:
        lines.append("\n---\n## 6. Cross-Dimensional PnL Attribution\n")
        # Hurst buckets
        hb = cross_attr["hurst_buckets"]
        if not hb.empty:
            lines.append("\n### 6.1 Hurst Bucket Attribution\n")
            lines.append("| Hurst Range | Trades | Total PnL | WinRate% | Avg PnL |")
            lines.append("|:--|:--:|:--:|:--:|:--:|")
            for idx, r in hb.iterrows():
                lines.append(f"| {idx} | {int(r['trades'])} | {r['total_pnl']:+,.0f} | "
                             f"{r['win_rate']:.1f} | {r['avg_pnl']:+,.0f} |")
            lines.append("")
        # Per-symbol
        ps = cross_attr["per_symbol"]
        if not ps.empty:
            lines.append("\n### 6.2 Per-Symbol Attribution\n")
            lines.append("| Symbol | Trades | Total PnL | WinRate% | AvgHold(d) |")
            lines.append("|:--|:--:|:--:|:--:|:--:|")
            for sym, r in ps.iterrows():
                lines.append(f"| {sym} | {int(r['trades'])} | {r['total_pnl']:+,.0f} | "
                             f"{r['win_rate']:.1f} | {int(r['avg_holding'])} |")
            lines.append("")
        # Monthly heatmap
        mp = cross_attr["monthly_pnl"]
        if not mp.empty:
            lines.append("\n### 6.3 Monthly PnL Heatmap (in thousands)\n")
            mp_k = (mp / 1000).astype(int)
            months = [f"{m}月" for m in mp_k.columns]
            lines.append(f"| Year | {' | '.join(months)} |")
            lines.append(f"|:--:|{'|'.join([':--:']*len(months))}|")
            for year, row in mp_k.iterrows():
                vals = " | ".join([f"{v:+,d}" if v != 0 else "·" for v in row])
                lines.append(f"| {int(year)} | {vals} |")
            lines.append("")

    # Holding analysis
    if holding:
        lines.append("\n---\n## 7. Holding Period & Turnover\n")
        lines.append(f"- 盈利交易平均持仓: **{holding['avg_holding_win']}天** / 亏损交易: **{holding['avg_holding_loss']}天**\n")
        lines.append(f"- 总成交额: {holding['total_turnover']:,.0f}\n")
        lines.append(f"- 年均换手率: ~{holding['annual_turnover_rate']:,.0f} CNY/year\n")
        # Holding histogram
        hist = holding["holding_histogram"]
        if not hist.empty:
            lines.append("\n| Holding Days | Trades | Total PnL | WinRate% |")
            lines.append("|:--|:--:|:--:|:--:|")
            for idx, r in hist.iterrows():
                lines.append(f"| {idx} | {int(r['trades'])} | {r['total_pnl']:+,.0f} | {r['win_rate']:.1f} |")
        # Cost scenarios
        sc = holding["cost_scenarios"]
        if sc:
            lines.append("\n### Cost Erosion\n")
            lines.append("| Scenario | Cost | Net PnL | Erosion% |")
            lines.append("|:--|:--:|:--:|:--:|")
            for s in sc:
                lines.append(f"| {s['label']} | {s['cost']:,.0f} | {s['net_pnl']:+,.0f} | {s['erosion_pct']:.2f}% |")

    # Slippage stress
    if slippage:
        lines.append("\n---\n## 8. Slippage Sensitivity Stress Test\n")
        lines.append(f"Baseline CAGR: **{slippage['baseline_cagr']:.2f}%**\n\n")
        lines.append("| Scenario | Total Cost | Net PnL | CAGR% | DeltaCAGR% |")
        lines.append("|:--|:--:|:--:|:--:|:--:|")
        for s in slippage["slippage_scenarios"]:
            delta_flag = " 🔴" if abs(s['cagr_delta_pct']) > 2 else ""
            lines.append(f"| {s['scenario']} | {s['total_cost']:,.0f} | {s['net_pnl']:+,.0f} | "
                         f"{s['cagr']:.2f} | {s['cagr_delta_pct']:+.2f}{delta_flag} |")
        lines.append("")
        worst = slippage["slippage_scenarios"][-1]
        if abs(worst["cagr_delta_pct"]) < 2:
            lines.append("**结论**: 15bp 滑点下 CAGR 下降 < 2%，策略对执行成本不敏感。实盘可使用市价单。\n")
        elif abs(slippage["slippage_scenarios"][2]["cagr_delta_pct"]) > 2:
            lines.append("**结论**: ⚠️ 10bp 滑点即导致 CAGR 下降超 2%。策略对执行成本敏感，实盘必须使用限价单。\n")
        else:
            lines.append("**结论**: 中等敏感度。建议实盘使用限价单控制滑点 <= 5bp。\n")

    report = "\n".join(lines)
    report_path = OUT_DIR / "diagnostic_report.md"
    report_path.write_text(report, encoding="utf-8")
    logger = logging.getLogger("diag")
    print(f"\nDiagnostic report saved: {report_path}")
    return report


# ════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("Trade Diagnostics")
    print("=" * 60)

    # ── 一次全周期回测，复用 strat 派生年度/交易/终值 ──
    print("\n[1/8] Running full-period backtest (2014-2026)...")
    strat, _ = run_full_backtest("2014-01-02", "2026-06-22")
    if strat is None:
        print("  ERROR: Backtest failed!")
        sys.exit(1)

    baseline_final_value = strat.broker.getvalue()
    years_span = 12.5
    print(f"  True final value: {baseline_final_value:,.0f} "
          f"(CAGR={((baseline_final_value/CONFIG['initial_cash'])**(1/years_span)-1)*100:.2f}%)")

    raw_trades = getattr(strat, "_my_trades", [])
    print(f"  Total trades: {len(raw_trades)}")
    raw_df = pd.DataFrame(raw_trades) if raw_trades else pd.DataFrame()

    # ── 年度 breakdown：从全周期净值序列切（不再每年独立回测）──
    # 先做特征标注（年度统计需要 features_df），再算 yearly
    print("\n[2/8] Computing entry features...")
    features_df = compute_entry_features(raw_df)
    print(f"  {len(features_df)} trades annotated")
    if features_df.empty:
        print("  ERROR: No annotated trades!")
        sys.exit(1)

    wins = features_df["was_win"].sum()
    losses = len(features_df) - wins
    print(f"  Wins: {wins} / Losses: {losses}")

    print("\n[3/8] Yearly breakdown (from full-period NAV series)...")
    yearly = run_yearly_breakdown(strat, features_df, initial_cash=CONFIG["initial_cash"])
    if not yearly.empty:
        yearly.to_csv(OUT_DIR / "yearly_breakdown.csv", index=False, encoding="utf-8")
        print(f"  {len(yearly)} years, saved to results/diagnostics/yearly_breakdown.csv")

    # 释放 strat 后续不再需要（保留 features_df）
    del strat; gc.collect()

    # Phase 4: Mann-Whitney U
    print("\n[4/8] Running Mann-Whitney U tests...")
    mw_df = run_mann_whitney(features_df)
    if not mw_df.empty:
        print(mw_df.to_string(index=False))

    # Phase 5: Market timing diagnosis
    print("\n[5/8] Market timing diagnosis (vs HS300)...")
    timing = timing_diagnosis(features_df)
    if timing:
        print(f"  Beta: {timing['beta']:.3f}  |  Mean Alpha: {timing['mean_alpha']:+.2f}%")
    else:
        timing = None

    # Phase 6: Cross attribution
    print("\n[6/8] Cross-dimensional PnL attribution...")
    cross_attr = cross_attribution(features_df)

    # Phase 7: Holding period & turnover
    print("\n[7/8] Holding period & turnover analysis...")
    holding = holding_analysis(features_df)
    if holding:
        print(f"  Avg holding: win={holding['avg_holding_win']}d / loss={holding['avg_holding_loss']}d")

    # Phase 8: Slippage stress test (CAGR 从真实终值算)
    print("\n[8/8] Slippage sensitivity stress test...")
    slippage = slippage_stress(features_df, baseline_final_value=baseline_final_value,
                               initial_cash=CONFIG["initial_cash"], years=years_span)
    if slippage:
        for s in slippage["slippage_scenarios"]:
            d_flag = " [DOWN]" if s["cagr_delta_pct"] < -2 else ""
            print(f"  {s['scenario']}: CAGR={s['cagr']:.2f}%{d_flag}")

    # Generate report
    generate_report(yearly, features_df, mw_df, timing=timing,
                    cross_attr=cross_attr, holding=holding, slippage=slippage)

    print("\n" + "=" * 60)
    print("Done. Output in results/diagnostics/")
    print("=" * 60)
