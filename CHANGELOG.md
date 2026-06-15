# Changelog
## S1 | - 2026-06-15
### 数据管道 `已完成`

## [V3.1] - 2026-06-15
### T+0/T+1 结算规则差异分析
- 新增 `docs/strategy_design_v3.0.md` §5.7 T+0/T+1 结算规则差异对回测的影响
- 新增 `docs/strategy_design_v3.0.md` §2.5 ETF 结算规则差异说明
- 新增 `docs/analysis/t+0_t+1_impact.md` 独立影响分析文档
- 已知风险汇总新增 "T+1 品种止损滞后" 风险项（#6）
- Dry-Run 验证项新增 "T+1 品种止损滑点测量"
- [V3.1] `已完成`

## [S0] - 2026-06-14
### 项目骨架搭建
- 创建独立项目 `turtle_v2/`，与 `automated_trading/` 物理隔离
- 文档体系：`docs/governance_model.md`（管控模型）+ `docs/strategy_design_v3.0.md`（已完成）
- 配置文件：`config/turtle_config.yaml`
- 管理文件：`CHANGELOG.md`、`README.md`（占位符版）
- 一致性机制：`scripts/check_consistency.py`、`.pre-commit-config.yaml`
- 依赖声明：`requirements.txt`
- [S0] `已完成`
