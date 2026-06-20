"""
复权工具 — 检测并修正 ETF 拆分/合并事件
"""
from __future__ import annotations
from pathlib import Path
import pandas as pd
import numpy as np


def detect_splits(df: pd.DataFrame, threshold: float = 0.20) -> list[tuple]:
    """检测 parquet 数据中的拆分事件。

    通过对比 pre_close[t] 与 close[t-1] 的比值来判断。
    正常日比值应在 1±0.10 范围内。大幅偏离说明发生了拆分/合并。

    Returns
    -------
    list[(date, factor)]
        拆分事件列表，factor = pre_close[t] / close[t-1]（>1=拆分，<1=合并）
    """
    df = df.sort_values("date").reset_index(drop=True)
    events = []
    for i in range(1, len(df)):
        prev_close = df.loc[i - 1, "close"]
        curr_pre = df.loc[i, "pre_close"]
        if prev_close <= 0:
            continue
        ratio = curr_pre / prev_close
        if abs(ratio - 1) > threshold:
            events.append((df.loc[i, "date"], ratio))
    return events


def adjust_for_splits(df: pd.DataFrame) -> pd.DataFrame:
    """对 DataFrame 做后复权（backward adjustment）。

    检测拆分事件后，将事件之前的所有 OHLC 价格乘以累积调整因子，
    使得历史价格与当前价格在同一基准上，消除拆分造成的虚假跳跃。

    只调整 open/high/low/close/pre_close，不调整 volume 和 amount。
    """
    df = df.sort_values("date").reset_index(drop=True)
    events = detect_splits(df)
    if not events:
        return df

    # 计算累积调整因子（从最早事件到最晚）
    cum_factor = 1.0
    for evt_date, factor in events:
        cum_factor *= factor

    # 对事件之前的所有价格进行调整
    first_evt_idx = None
    for evt_date, _ in events:
        matches = df[df["date"] == evt_date].index
        if len(matches) > 0:
            idx = matches[0]
            if first_evt_idx is None or idx < first_evt_idx:
                first_evt_idx = idx

    if first_evt_idx is None or first_evt_idx == 0:
        return df

    price_cols = ["open", "high", "low", "close", "pre_close"]
    for col in price_cols:
        if col in df.columns:
            df.loc[: first_evt_idx - 1, col] = (
                df.loc[: first_evt_idx - 1, col] * cum_factor
            )

    print(f"  [复权] 检测到 {len(events)} 个拆分事件: "
          f"{', '.join(f'{d.date()} x{f:.2f}' for d, f in events)}")
    print(f"         累积因子: {cum_factor:.4f}, "
          f"调整 {first_evt_idx} 行历史数据")

    return df


def batch_adjust(data_dir: Path, codes: list[str]):
    """批量扫描并复权 parquet 文件。"""
    for code in codes:
        path = data_dir / f"{code}.parquet"
        if not path.exists():
            print(f"  [跳过] {code} 不存在")
            continue
        df = pd.read_parquet(path)
        df["date"] = pd.to_datetime(df["date"])
        before = len(df)
        df2 = adjust_for_splits(df)
        if len(df2) != before or df2 is not df:
            df2.to_parquet(path, index=False, compression="snappy")
            print(f"  [已修复] {code}")
        else:
            print(f"  [无事件] {code}")


if __name__ == "__main__":
    ROOT = Path(__file__).resolve().parent.parent
    DATA_DIR = ROOT / "data" / "etf_daily"
    SYMBOLS = ["510500.SH", "159845.SZ", "159915.SZ",
               "588000.SH", "513100.SH", "518880.SH", "511010.SH"]

    print("=" * 60)
    print("ETF 拆分事件检测与复权")
    print("=" * 60)

    for code in SYMBOLS:
        path = DATA_DIR / f"{code}.parquet"
        if not path.exists():
            continue
        df = pd.read_parquet(path)
        df["date"] = pd.to_datetime(df["date"])
        events = detect_splits(df)
        if events:
            print(f"\n{code}: 发现 {len(events)} 个事件")
            for d, f in events:
                print(f"  {d.date()}: 调整因子 x{f:.4f}")
        else:
            print(f"\n{code}: 无拆分事件")

    print("\n" + "-" * 60)
    print("执行复权? 运行: py scripts/adjust_splits.py --apply")
