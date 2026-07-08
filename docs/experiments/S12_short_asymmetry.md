# 实验: 做空风险不对称修正

## 元数据
- 提出: 2026-07-08
- 分支: —
- 状态: 📦 待验证

## 假设
做空时低 n_entry 加仓效应导致亏损放大 36%（计算值），
引入 `short_risk_factor` 和加仓时用当前 n 重算可消除此不对称。

## 成功标准
- 做空 Sharpe 提升 ≥ 0.05
- 做空 MDD 不恶化 ≥ 2%
- 组合级（多+空）CAGR 不下降

## 结果
（待验证，当前主配置 `shortable: false`，不影响现有回测）

## 参考
详见 `docs/_archive/ideas/short_risk_asymmetry.md` 完整分析。
