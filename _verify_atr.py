"""Verify compute_atr rewrite matches original for-loop logic."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from src.turtle_core import compute_atr, compute_tr

# ---- 构造测试数据 ----
np.random.seed(42)
n = 1500
high = pd.Series(np.random.uniform(10, 20, n))
low = pd.Series(np.random.uniform(5, 15, n))
close = pd.Series(np.random.uniform(7, 18, n))

tr = compute_tr(high, low, close)
atr = compute_atr(tr, period=20)

# ---- 验证 1: 前 19 个为 NaN ----
assert pd.isna(atr.iloc[:19]).all(), "FAIL: first 19 not NaN"
print("PASS: first 19 are NaN")

# ---- 验证 2: 初始值 = 前 20 个 TR 均值 ----
expected_seed = tr.iloc[:20].mean()
assert abs(atr.iloc[19] - round(expected_seed, 4)) < 0.0001, \
    f"FAIL: seed {atr.iloc[19]} != {round(expected_seed, 4)}"
print(f"PASS: seed = {atr.iloc[19]:.4f} (expected {round(expected_seed, 4):.4f})")

# ---- 验证 3: 逐元素对比原 for-loop 实现 ----
alpha = 1.0 / 20
expected = np.full(n, np.nan)
expected[19] = expected_seed
prev = expected_seed
for i in range(20, n):
    prev = (1 - alpha) * prev + alpha * tr.values[i]
    expected[i] = prev

expected_s = pd.Series(expected)
diff = (atr.values[19:] - expected[19:]).round(4)
max_diff = np.abs(diff).max()
assert max_diff < 1e-8, f"FAIL: max diff = {max_diff}"
print(f"PASS: max diff from naive loop = {max_diff:.2e}")

# ---- 验证 4: round(4) 正确 ----
for i in range(19, min(n, 30)):
    assert atr.iloc[i] == round(expected[i], 4), \
        f"FAIL: rounding at {i}: {atr.iloc[i]} != {round(expected[i], 4)}"
print("PASS: round(4) correct for first 11 non-NaN values")

# ---- 验证 5: 空/短序列 ----
assert compute_atr(pd.Series([], dtype=float)).empty, "FAIL: empty"
assert compute_atr(pd.Series([1.0, 2.0]), 20).isna().all(), "FAIL: short series"
print("PASS: empty and short series handled")

print("\nAll verifications passed!")
