#!/usr/bin/env python
"""
复权修复验证脚本（V5.19）

离线检查本地 ETF 缓存是否已正确前复权。无需网络，只读 parquet。
修复前：缓存为未复权原始价，拆分日出现 80%+ 假跌。
修复后（须先 `py scripts/pull_data.py --force` 重拉）：前复权 close 应连续。

用法：
    py scripts/verify_adjustment.py
"""
from __future__ import annotations

import sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA_DIR = ROOT / "data" / "etf_daily"

# 品种列表从配置动态读取，避免硬编码漏品种
from src.data_pipeline import get_symbols  # noqa: E402

# 前复权后单日涨跌幅合理上限
MAX_DAILY_CHANGE = 0.50
# 价格列 NaN 占比超过此阈值即判定数据损坏（百分比 → 小数）
NAN_PRICE_FAIL_RATIO = 0.10


def check_symbol(code: str) -> dict:
    path = DATA_DIR / f"{code}.parquet"
    if not path.exists():
        return {"code": code, "status": "missing", "detail": "缓存文件不存在"}

    df = pd.read_parquet(path)
    if df.empty:
        return {"code": code, "status": "empty", "detail": "缓存为空"}

    has_adj = "adj_factor" in df.columns

    # ── 数据有效性校验：NaN 价格是最严重的损坏，必须先行检查 ──
    price_cols = [c for c in ["open", "high", "low", "close"] if c in df.columns]
    nan_parts = []
    for col in price_cols:
        nan_ct = int(df[col].isna().sum())
        if nan_ct > 0:
            nan_parts.append(f"{col}={nan_ct}({nan_ct/len(df)*100:.0f}%)")
    if nan_parts:
        ratio = df["close"].isna().mean() if "close" in df.columns else 1.0
        status = "FAIL(NaN价格)" if ratio > NAN_PRICE_FAIL_RATIO else "WARN(NaN价格)"
        return {
            "code": code, "status": status,
            "detail": f"rows={len(df)} adj_factor={'有' if has_adj else '无'} "
                      f"NaN: {' '.join(nan_parts)}",
        }

    close = df["close"].astype(float)
    prev = close.shift(1)
    valid = prev.notna() & (prev > 0) & close.notna()
    changes = (close[valid] / prev[valid] - 1.0).abs()
    worst = float(changes.max()) if len(changes) else 0.0
    worst_date = df.loc[changes.idxmax(), "date"] if len(changes) else None

    # ── 空 changes 语义校验：非空数据却算不出收益率 → 数据损坏 ──
    if len(changes) == 0 and len(df) > 1:
        return {
            "code": code, "status": "FAIL(无有效收盘序列)",
            "detail": f"rows={len(df)} adj_factor={'有' if has_adj else '无'} "
                      f"有效收益率对=0（数据可能全 NaN/非正）",
        }

    # 前复权比率：最新日应为 1.0
    latest_ratio = float(df["adj_factor"].iloc[-1]) if has_adj else None

    # ── adj_factor 末值校验：NaN 说明复权流程异常 ──
    if has_adj and pd.isna(latest_ratio):
        return {
            "code": code, "status": "FAIL(adj_factor异常)",
            "detail": f"rows={len(df)} adj_factor=有 latest_ratio=NaN "
                      f"max单日变动={worst*100:.2f}%",
        }

    status = "ok" if worst < MAX_DAILY_CHANGE else "FAIL(残留跳空)"
    ratio_str = f"{latest_ratio:.4f}" if latest_ratio is not None else "N/A"
    date_str = worst_date.date() if worst_date is not None else "N/A"
    detail = (
        f"rows={len(df)} adj_factor={'有' if has_adj else '无'} "
        f"latest_ratio={ratio_str} "
        f"max单日变动={worst*100:.2f}% @ {date_str}"
    )
    return {"code": code, "status": status, "detail": detail}


def main():
    symbols = get_symbols(include_bond=True)
    codes = [s["code"] for s in symbols]

    print("=" * 70)
    print("前复权修复验证（V5.19）—— 检查本地缓存 close 序列连续性")
    print(f"品种列表（{len(codes)} 个，从配置动态读取）：{codes}")
    print("修复前预期：513100/510500 等拆分日 max单日变动 > 50%（FAIL）")
    print("重拉后预期：全部 < 50%（ok），且 latest_ratio ≈ 1.0")
    print("=" * 70)
    all_ok = True
    for code in codes:
        r = check_symbol(code)
        print(f"\n{r['code']}: {r['status']}")
        print(f"  {r['detail']}")
        if r["status"] != "ok":
            all_ok = False

    print("\n" + "=" * 70)
    if all_ok:
        print("✅ 全部品种前复权校验通过")
    else:
        print("❌ 存在校验失败 —— 见上方 FAIL/WARN 行")
        print("   若未重拉，请运行: py scripts/pull_data.py --force")
        print("   若重拉后仍 FAIL，说明 fund_adj 漏记折算事件，需人工核查该品种")
    print("=" * 70)

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
