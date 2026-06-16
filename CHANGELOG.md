# Changelog

## [Audit-fix] - 2026-06-16
### 策略核心逻辑审查修复 — 2 项严重缺陷 + 2 项编码规范
- **P0-1 累计亏损百分比永不复位**（`_enter_pause` 增加 `self._cumulative_loss_pct = 0.0`）：暂停期结束后可恢复新开仓，消除永久冻结 bug
- **P0-2 移动止损首次激活后冻结**（`_update_trailing_stop` 删除提前返回逻辑）：移动止损线每日随价格上涨上移，恢复趋势跟踪利润保护能力
- **#3 盈亏百分比使用固定基准**：`self._equity()` → `self.broker.startingcash`，与设计文档 §6.2「累计亏损 > 总资金 15%」的固定基准语义一致
- **#4 运行时动态导入**：顶部增加 `timedelta`，`__import__("datetime").timedelta()` → `timedelta()`，消除编码规范问题
- `docs/strategy_design_v3.0.md`: 新增 §1.3 架构局限标注 + §5.7.6 累计亏损基准说明
- 全部 22 项策略测试通过，无回归
- [Audit-fix] `已完成`

## [Bugfix-6failed] - 2026-06-16
### 修复 6 个已知失败测试（185 passed, 0 failed）
- `tests/test_gen_report.py` — mock `best_params.json` 不存在，使占位符降级分支可到达
- `tests/test_grid_search.py` — 烟雾测试日期从 `2020-2021` 调整为 `2023-2024`，避免 159845.SZ 早期数据不足
- `tests/test_turtle_strategy.py` — fixture 中 `_trades` → `_my_trades`（属性名与策略代码不匹配）
- `tests/test_turtle_strategy.py` — 2 个 S4 测试补充 `_close_series` 填充
- `tests/test_turtle_strategy.py` — `test_pause_after_losses` 中 `_trades` → `_my_trades`
- [Bugfix-6failed] `已完成`

## [S5-bugfix] - 2026-06-16
### B4 兼容性修复（Python 3.14 + Backtrader）
- 修复 3 个 Backtrader 内部兼容性 bug：
  - `self._trades: List[dict] = []` 覆盖 Backtrader 内部 `self._trades` dict，导致 `PandasData` 对象被用作 list 索引
    → 重命名为 `self._my_trades`
  - `_next_idx()` 使用 `len(self)-1` 在 runonce 模式第 0 个 bar 返回 -1，超出预计算数组长度
    → 增加负值和越界保护
  - `next()` 首次调用时 len(self)=0 数据不全
    → 增加 `len(self) < 2: return` 防护
- B4 在 `run_comparison.py` 中改用独立 Cerebro 实例，避免多轮回测状态残留
- S5 四个基准全部成功运行：

  | 基准 | 最终净值 | 总收益率 |
  |:--|:--:|:--:|
  | B1 买入等权持有 | ¥360,666 | +80.33% |
  | B2 等权再平衡 | ¥409,609 | +104.80% |
  | B3 ATR等风险 | ¥201,956 | +0.98% |
  | B4 海龟+国债 | ¥196,951 | -1.52% |

- [S5-bugfix] `已完成`

## [S8] - 2026-06-16
### 综合报告 + 测试
- `scripts/gen_report.py`: 综合报告生成脚本
  - `load_best_params()`: 读取 S6 最优参数，文件不存在时回退 config 默认值
  - `run_backtest_with_best()`: 用最优参数运行全区间回测，输出 18 项指标
  - `generate_summary_table()`: §1.2 核心目标通过/条件通过/不通过判定（5 项指标）
  - `generate_report()`: 5 章节 Markdown 报告（核心目标/绩效/基准对比/最优参数/压力测试）
  - CLI: `--mode/--start/--end/--params/--no-backtest/--output`
- `tests/test_gen_report.py`: 10 项单元测试（通过判定、章节完整性、优雅降级）
- `docs/strategy_design_v3.0.md`: 升级 V3.4，新增 §5.11 S8 施工图设计
- 全量测试 150/150 passed ✅（+10 新增，无回归）
- [S8] `已完成`

## [S7] - 2026-06-16
### 极端情景回测 + 压力测试 — 完整实现
- `scripts/run_stress_test.py`: 完整实现（§5.9 施工图落地）
  - `define_scenarios()`: 4 个历史情景（A1 COVID 熔断 / A2 俄乌冲突 / A3 A股二次探底 / A4 完整2022年）+ 2 个合成情景（B1 每月同步暴跌 / B2 连续3日跌停）
  - `load_best_params()`: 从 S6 best_params.json 加载，文件不存在时回退 config 默认值
  - `run_historical_scenario()`: Backtrader 回测 + 含 VaR/相关性/T+1 止损延迟等 18 项指标
  - `run_synthetic_shock()`: B1 — 每月首日注入 -3%/-5%/-7% 冲击矩阵
  - `run_liquidity_stress()`: B2 — 满仓 4 单位 × 3 日跌停不可抗损失计算
  - `_check_stress_pass()`: 5 项通过线判定（MDD≤25% / 回撤持续≤60日 / VaR99≤5% / 月亏≤15% / 止损保护触发）
  - `generate_report()`: Markdown 完整报告 + 综合判定
  - `save_results()`: 输出 5 个产物文件 + conclusion JSON
  - CLI: `--params/--scenarios/--mode/--workers/--output`
  - 并行支持: `ProcessPoolExecutor`（A1-A4 可同时回测）
- `scripts/run_correlation_monitor.py`: 完整实现（§5.10 要求）
  - `load_price_matrix()`: 从 Parquet 加载 6 品种价格，内连接对齐公共交易日
  - `compute_rolling_correlation()`: 对数收益率 → rolling().corr() → 上三角聚合
  - `detect_correlation_events()`: 连续阈值突破合并为预警事件
  - `plot_correlation_timeseries()`: 折线图 + 阈值线 + 预警区域填充 + 峰值标注
  - `generate_report()`: Markdown 报告（总体统计 + 事件列表 + 结论）
  - CLI: `--start/--end/--window/--threshold/--plot`
- `tests/test_stress_test.py`: 32 项测试全部通过
  - 场景定义完整性 / 最优参数加载+fallback / 历史回测输出格式 / B1 矩阵结构 / B2 计算正确性 / 相关性计算 / 通过判定逻辑 / 报告生成 / 文件保存 / CLI 主函数
- `scripts/gen_report.py`: 对接 S7 — 消除占位符
  - `load_stress_conclusion()`: 读取 `stress_conclusion.json`
  - `load_stress_report()`: 读取 `stress_report.md` 摘要
  - `generate_stress_section()`: 替代硬编码占位符，存在 S7 结果时内联判定 + 场景表格
- `results/stress_test/` 输出产物（运行时生成）:
  - `stress_report.md` / `scenario_summary.csv` / `historical_{s}.csv` / `synthetic_shock.csv` / `stress_conclusion.json`
  - `correlation_series.csv` / `correlation_events.csv` / `correlation_report.md` / `correlation_plot.png`（--plot）
- `docs/strategy_design_v3.0.md`: 升级 V3.7
- 全量测试: 179 passed, 6 failed（6 个已有问题，S7 无回归）
- [S7] `已完成`

## [S6] - 2026-06-15
### 参数网格搜索
- `scripts/run_grid_search.py`: 完整网格搜索模块
  - `build_param_grid()`: 5 参数笛卡尔积 → 405 组（含 α=0.15）
  - `run_single_backtest()`: 单次回测包装器，18 项标准化指标
  - `run_grid_search()`: 样本内（2020-2023）+ 样本外（2024-2026）分割验证
  - `evaluate_results()`: 稳健性评分（Sharpe/Calmar/CAGR/MDD/trades 加权）
  - `_worker()` + `ProcessPoolExecutor`: 多进程并行（默认 4 workers）
  - `plot_results()`: 散点图 + 热力图（可选 --plot）
  - CLI: `--mode/--start/--split/--end/--workers/--quick/--plot`
- `tests/test_grid_search.py`: 14 项单元测试（笛卡尔积、schema、稳健性评分、JSON 读写、冒烟测试）
- `docs/strategy_design_v3.0.md`: 升级 V3.2，新增 §5.8 施工图设计
- 全量测试 140/140 passed ✅（+14 新增，无回归）
- [S6] `已完成`

## [S5] - 2026-06-15
### 四种基准对比
- `src/benchmarks.py`: 三种基准策略
  - `BuyAndHold`: 买入等权持有（B1）
  - `EqualWeightRebalance`: 等权定期再平衡（B2）
  - `ATREqualRisk`: ATR 等风险贡献，无海龟信号（B3）
- `scripts/run_comparison.py`: 一键运行 B1-B4 对比，输出表格 + CSV
- `tests/test_benchmarks.py`: 10 项单元测试（B1/B2/B3 各逻辑 + SIX_SYMBOLS 常量）
- 全量测试 126/126 passed ✅
- [S5] `已完成`

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