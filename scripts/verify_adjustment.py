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

from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "etf_daily"
SYMBOLS = ["510500.SH", "159915.SZ", "513100.SH", "518880.SH", "511010.SH"]

# 前复权后单日涨跌幅合理上限（与 data_pipeline.ADJUSTMENT_MAX_DAILY_CHANGE 一致）
MAX_DAILY_CHANGE = 0.50


def check_symbol(code: str) -> dict:
    path = DATA_DIR / f"{code}.parquet"
    if not path.exists():
        return {"code": code, "status": "missing", "detail": "缓存文件不存在"}

    df = pd.read_parquet(path)
    if df.empty:
        return {"code": code, "status": "empty", "detail": "缓存为空"}

    has_adj = "adj_factor" in df.columns
    close = df["close"].astype(float)
    prev = close.shift(1)
    valid = prev.notna() & (prev > 0) & close.notna()
    changes = (close[valid] / prev[valid] - 1.0).abs()
    worst = float(changes.max()) if len(changes) else 0.0
    worst_date = df.loc[changes.idxmax(), "date"] if len(changes) else None

    # 前复权比率：最新日应为 1.0
    latest_ratio = float(df["adj_factor"].iloc[-1]) if has_adj else None

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
    print("=" * 70)
    print("前复权修复验证（V5.19）—— 检查本地缓存 close 序列连续性")
    print("修复前预期：513100/510500 等拆分日 max单日变动 > 50%（FAIL）")
    print("重拉后预期：全部 < 50%（ok），且 latest_ratio ≈ 1.0")
    print("=" * 70)
    all_ok = True
    for sym in SYMBOLS:
        r = check_symbol(sym)
        print(f"\n{r['code']}: {r['status']}")
        print(f"  {r['detail']}")
        if r["status"] != "ok":
            all_ok = False

    print("\n" + "=" * 70)
    if all_ok:
        print("✅ 全部品种前复权校验通过")
    else:
        print("❌ 存在残留跳空 —— 若未重拉，请运行: py scripts/pull_data.py --force")
        print("   若重拉后仍 FAIL，说明 fund_adj 漏记折算事件，需人工核查该品种")
    print("=" * 70)


if __name__ == "__main__":
    main()
