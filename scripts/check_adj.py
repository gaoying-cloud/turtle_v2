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
        df["adj_pct"] = df["adj_factor"].pct_change()
        big_jumps = df[df["adj_pct"].abs() > 0.01][["date", "close", "adj_factor", "adj_pct"]]
        if len(big_jumps) > 0:
            print(f"  发现 {len(big_jumps)} 次复权因子跳变 (除权/分红):")
            for _, row in big_jumps.iterrows():
                hfq_close = row["close"] * row["adj_factor"]
                print(f"    {row['date'].date()} | close(前复权)={row['close']:.4f} | "
                      f"adj_factor={row['adj_factor']:.6f} | 跳变={row['adj_pct']*100:+.2f}% | "
                      f"close×adj={hfq_close:.4f}")

        df["close_hfq"] = df["close"] * df["adj_factor"]
        print(f"  首日: {df.iloc[0]['date'].date()} close(前复权)={df.iloc[0]['close']:.4f}  hfq(复权×adj)={df.iloc[0]['close_hfq']:.4f}")
        print(f"  末日: {df.iloc[-1]['date'].date()} close(前复权)={df.iloc[-1]['close']:.4f}  hfq(复权×adj)={df.iloc[-1]['close_hfq']:.4f}")
        print(f"  前复权涨幅: {(df.iloc[-1]['close']/df.iloc[0]['close']-1)*100:.2f}%  ← 含除权假象")
        print(f"  真实累计收益: {(df.iloc[-1]['close_hfq']/df.iloc[0]['close_hfq']-1)*100:.2f}%  ← 扣除除权影响")
    else:
        print(f"  ⚠️ 没有复权因子！需要重新拉取数据")

print(f"\n{'='*70}")
