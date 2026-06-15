# Changelog

## [S4] - 2026-06-15
### 风险平价权重（三层融合）
- `src/risk_parity.py`: Ledoit-Wolf 收缩协方差 + 风险平价权重 CCD 求解 + α 融合
  - `ledoit_wolf_cov()`: 收缩估计，提高样本外稳定性
  - `risk_parity_weights()`: Newton 迭代求解等边际风险贡献权重
  - `compute_alpha_weights()`: 一步式 (1-α)×w_ATR + α×w_RP
- `strategies/turtle_trading.py`: 集成 α 融合到 _check_entry 入场风险权重
  - `_should_rebalance_weights()`: 触发条件（首次/季度末/ATR变动>30%）
  - `_build_returns_matrix()`: 从信号序列构建 252日收益率矩阵
  - `_recalc_alpha_weights()`: 重新计算并缓存
- `tests/test_risk_parity.py`: 22 项单元测试（Ledoit-Wolf + RP + α融合）
- `tests/test_turtle_strategy.py`: 新增 TestAlphaWeighting 5 项集成测试
- `scripts/run_backtest.py`: 传入 weighting 参数（alpha/cov_lookback/rebalance/atr_threshold）
- 全量测试 116/116 passed ✅
- [S4] `已完成`

## [S3] - 2026-06-15
### Backtrader 策略层
- `strategies/turtle_trading.py`: TurtleStrategy (bt.Strategy) 回测策略
  - 入场: 20日突破 + 可选55日过滤 + SignalFilter + 仓位集中度熔断
  - 退出: 2N固定止损 / 移动止损 / 10日反向突破（取更早触发者）
  - 加仓: 每0.5N加仓，最多4单位 (S2 pyramid_add)
  - T+1约束: 4只A股ETF当日买入不可止损，3只T+0品种可自由交易
  - 空仓→国债ETF切换，连续亏损暂停机制
- `scripts/run_backtest.py`: 回测CLI入口，5分析器，支持--mode A/B
- `tests/test_turtle_strategy.py`: 17 项单元测试
- [S3] `已完成`

## [S2] - 2026-06-15
### 海龟核心模块移植
- `src/turtle_core.py`: 从 automated_trading/src/strategy_engine.py 提取并重构
  - 无状态计算函数: compute_tr, compute_atr, donchian_high/low, trail_high_close, calc_position_size, calc_fixed_stop, calc_trailing_stop, calc_pyramid_trigger, pyramid_add
  - TurtleSignals: 一次性预计算 20日/55日通道 + ATR + 移动止损序列
  - Position dataclass: 保留 system 字段，预留做空/期货扩展
  - TurtlePositions: 多品种持仓管理器
  - SignalFilter: 盈利过滤器（4规则 + 上限保护）
- `tests/test_turtle_core.py`: 52 项单元测试全覆盖
- [S2] `已完成`

## [S1] - 2026-06-15
### 数据管道
- `src/data_pipeline.py`: Tushare 拉取、清洗、Parquet 缓存、增量更新
- `scripts/pull_data.py`: CLI 入口，支持全量/单品种/--status 缓存检查
- `tests/test_data_pipeline.py`: 20 项单元测试
- [S1] `已完成`

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
- 文档体系：`docs/governance_model.md`（管控模型）+ `docs/strategy_design_v3.0.md`
- 配置文件：`config/turtle_config.yaml`
- 管理文件：`CHANGELOG.md`、`README.md`
- 一致性机制：`scripts/check_consistency.py`、`.pre-commit-config.yaml`
- 依赖声明：`requirements.txt`
- [S0] `已完成`