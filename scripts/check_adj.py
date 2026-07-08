"""
诊断: adj_factor 元数据检查

作用：
  读取 etf_daily 缓存中的 adj_factor 列，检测是否存在除权/分红事件
  并验证前复权 close 序列的连续性。

  与 verify_adjustment.py 的区别：
  - check_adj.py: 看 adj_factor 元数据（"理论上发生了什么"）
  - verify_adjustment.py: 测 close 涨跌幅（"实际上数据是否连续"）

用法：
    py scripts/check_adj.py
"""
import pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
symbols = ["510500.SH", "159915.SZ", "513100.SH", "518880.SH"]

print("=" * 70)
print("诊断: 检查 adj_factor 是否存在，以及除权日的表现")
print("=" * 70)

for sym in symbols:
    df = pd.read_parquet(ROOT / f"data/etf_daily/{sym}.parquet")
    cols = df.columns.tolist()
    has_adj = "adj_factor" in cols

    print(f"\n{'─'*50}")
    print(f"{sym}")
    print(f"  列名: {cols}")
    print(f"  adj_factor 存在: {'✅ 是' if has_adj else '❌ 否'}")

    if has_adj:
        # adj_factor 现存的是前复权比率（最新日=1.0，历史日≤1）；其跳变即除权/分红事件。
        df["adj_pct"] = df["adj_factor"].pct_change()
        big_jumps = df[df["adj_pct"].abs() > 0.01][["date", "close", "adj_factor", "adj_pct"]]
        if len(big_jumps) > 0:
            print(f"  发现 {len(big_jumps)} 次复权因子跳变 (除权/分红):")
            for _, row in big_jumps.iterrows():
                print(f"    {row['date'].date()} | close(前复权)={row['close']:.4f} | "
                      f"adj_factor={row['adj_factor']:.6f} | 跳变={row['adj_pct']*100:+.2f}%")
        else:
            print(f"  无 >1% 的因子跳变（无除权/分红事件，或数据未复权需 --force 重拉）")

        # close 已是前复权连续序列，直接计算累计收益即为真实收益（已扣除除权影响）。
        print(f"  首日: {df.iloc[0]['date'].date()} close(前复权)={df.iloc[0]['close']:.4f}")
        print(f"  末日: {df.iloc[-1]['date'].date()} close(前复权)={df.iloc[-1]['close']:.4f}")
        print(f"  真实累计收益: {(df.iloc[-1]['close']/df.iloc[0]['close']-1)*100:.2f}%  ← 前复权 close 直接计算")
    else:
        print(f"  ⚠️ 没有复权因子！需要重新拉取数据")

print(f"\n{'='*70}")
