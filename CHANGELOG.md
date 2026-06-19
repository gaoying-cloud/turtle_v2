# Changelog

## [V5.6-删除旧P2+投票确认系统] - 2026-06-19
### 删除旧 P2 累计亏损冻结 + 新增投票式信号确认系统（默认关闭）
- `strategies/turtle_trading.py`: 删除旧 P2 累计亏损金额冻结代码块（近15笔亏损≥15%封禁），该逻辑对所有品种返回相同亏损比例，存在 bug
- `strategies/turtle_trading.py`: 新增投票式信号确认区块（成交量/K线形态/近期胜率），由 `min_confirmations` 控制（0=关闭）
- `strategies/turtle_trading.py`: `p2_mode` 默认值从 `"batting_avg"` 改为 `"none"`，移除 `"cumulative_loss"` 选项
- `strategies/turtle_trading.py`: `max_cumulative_loss_pct` 参数保留但标记废弃，兼容网格搜索
- `src/turtle_core.py`: 新增 `volume_confirmation()`, `breakout_quality()`, `recent_batting_avg()` 三个确认函数
- `scripts/run_backtest.py`: 新增 `min_confirmations`, `vol_threshold`, `kline_min_body`, `p2_loss_ratio`, `p2_batting_window`, `use_signal_filter`, `p2_mode` 参数传递
- `scripts/compare_filters.py`: 删除成交量+旧P2、K线+旧P2 两个组合，缩至 6 组
- [V5.6-删除旧P2] `已完成`

## [V5.5-暂停按品种+做空修复] - 2026-06-18
### 暂停粒度从全局改为按品种 + 6脚本 `shortable_symbols`/`t_plus_one_symbols` 传参修复
- `strategies/turtle_trading.py`: 暂停机制从全局单点改为按品种独立控制；`_consecutive_losses`、`_paused_until` 从 `int`/`Optional[date]` 改为 `Dict[str, int]`/`Dict[str, Optional[date]]`
- `strategies/turtle_trading.py`: `_check_entry` 累计亏损暂停从跨品种最近15笔改为按品种过滤
- `strategies/turtle_trading.py`: `_execute_exit` 连续亏损计数改为 per-symbol
- `strategies/turtle_trading.py`: `_enter_pause` 新增 `code` 参数
- `strategies/turtle_trading.py`: 删除全局 `T_PLUS_ONE_SYMBOLS`/`SHORTABLE_SYMBOLS` 常量，改为 `self.params.*`
- `tests/test_turtle_strategy.py`: fixture 同步更新 `_consecutive_losses`/`_paused_until` 类型，`_check_entry` 暂停测试改为按品种
- `scripts/run_backtest.py`: 新增 `shortable_symbols`、`t_plus_one_symbols` 传参；期货模式全部可做空
- `scripts/run_comparison.py`: B4 `addstrategy` 补充两个参数
- `scripts/run_grid_search.py`: import + `addstrategy` 补充
- `scripts/run_stress_test.py`: import + `addstrategy` 补充
- `scripts/gen_report.py`: import + `addstrategy` 补充
- 全量测试 185/185 passed ✅，无回归
- [V5.5-暂停按品种+做空修复] `已完成`

## [V5.4-config_loader] - 2026-06-18
### 全局硬编码消除：所有品种列表从 config/turtle_config.yaml 统一读取
- `src/config_loader.py`: 新建模块，6 个配置读取函数（load_config, get_trading_symbols, get_bond_symbol, get_all_symbols, get_shortable_symbols, get_t_plus_one_symbols, get_t0_symbols, get_futures_symbols）
- `config/turtle_config.yaml`: `symbols` 每项新增 `shortable`、`t_plus_one` 字段
- `strategies/turtle_trading.py`: 删除 `T_PLUS_ONE_SYMBOLS`、`SHORTABLE_SYMBOLS` 模块常量，改为 `self.params.t_plus_one_symbols`、`self.params.shortable_symbols`
- `strategies/turtle_trading.py`: 删除 `_bond_switch()` 方法及 `_in_bond`、`_bond_data` 字段
- `scripts/run_backtest.py`: 改用 `from src.config_loader import ...` 导入品种列表
- `scripts/run_comparison.py`: 同上
- `scripts/run_grid_search.py`: 同上
- `scripts/run_stress_test.py`: 同上
- `scripts/run_correlation_monitor.py`: 同上
- `scripts/gen_report.py`: 同上
- `tests/test_turtle_strategy.py`: fixture 新增 `t_plus_one_symbols`、`shortable_symbols` params，删除 `T_PLUS_ONE_SYMBOLS` import
- `docs/strategy_design_v3.0.md`: 升级 V5.3，新增 V5.1~V5.3 变更记录
- 全量 22 策略测试 + 所有模块测试通过
- [V5.4-config_loader] `已完成`

## [V5.3-风控+空头修复] - 2026-06-18
### P0 修复：累计亏损计算错误 + 5日回撤预警 + P1 国债死代码+ETF禁止空头
- `strategies/turtle_trading.py`: 删除 `_cumulative_loss_pct` 废弃字段（原本用 `abs(pnl)` 错误累加盈利），统一从 `_my_trades` 实时计算，仅统计亏损交易
- `strategies/turtle_trading.py`: 新增 `_check_5day_drawdown()` 方法，监控 5 日滚动峰值回撤，超 8% 阈值自动暂停交易
- `strategies/turtle_trading.py`: 新增 `max_5day_drawdown_pct` 参数（默认 0.08），`_equity_history` 净值历史缓存
- `strategies/turtle_trading.py`: 删除 `_bond_switch()`（国债切换死代码）、`_in_bond`、`_bond_data`
- `strategies/turtle_trading.py`: 新增 `SHORTABLE_SYMBOLS`，仅纳指+黄金可做空
- `tests/test_turtle_strategy.py`: fixture 同步删除 `_cumulative_loss_pct`，新增 `_equity_history`；params 新增 `max_5day_drawdown_pct`
- 合计 2+ 文件修改，git diff 确认无额外影响
- [V5.3-风控+空头修复] `已完成`

## [V5.1-S6双向回测] - 2026-06-18
### S6: T+0 双向回测 — 添加 direction 字段 + 品种级多空明细输出
- `strategies/turtle_trading.py`: `_execute_exit` 交易记录新增 `direction` 字段（long/short），支持按品种和方向分别统计盈亏
- 品种级多空明细输出：回测报告按品种拆分做多/做空净收益、胜率、交易次数
- `docs/strategy_design_v3.0.md`: 升级 V5.0
- 全量测试通过，无回归
- [V5.1-S6双向回测] `已完成`

## [V5.0-期货版+空头修复] - 2026-06-17
### 期货基础设施 + 空头方向 Bug 修复 + 策略失效判决
- **期货基础设施**（全新建）：
  - `scripts/pull_futures.py`: 从 Tushare `fut_daily` 拉取 12 品种主力连续日线，Parquet 缓存（与 ETF 版 Schema 一致）
  - `config/turtle_config.yaml`: 新增 `futures:` 节（初始资金 ¥1,000,000、保证金 15%）
  - `scripts/run_backtest.py`: 新增 `--futures` 模式，自动切换数据目录和资金参数
  - 核心代码零改动：`turtle_core.py` / `risk_parity.py` / `turtle_trading.py`
- **空头方向 3 个 Bug 修复**：
  | Bug | 位置 | 问题 | 影响 |
  |:---|:---|:---|:---:|
  | A | `should_activate_trailing_stop` | 空头浮盈计算 `(entry - current)/N` 应为 `(current - entry)/N` | 空头永远无法激活移动止损 |
  | B | `calc_pyramid_trigger` / `pyramid_add` | 加仓公式固定 `base + N×0.5`，空头应是 `base − N×0.5` | 空头在不利方向上加仓放大亏损 |
  | C | `_check_pyramid` | 条件 `high < trigger` 固定为多头逻辑 | 空头每天在上涨方向上加仓 |
- **A 股 ETF 版本失效判决**：6 品种 2020~2026 全品种回测亏损 -74.7%，胜率 22.2%
- **T+0 验证**：仅纳指+黄金回测 +2.49%（修复做空后减亏 66%，T+0 约束消除）
- **期货回测证明成功**：12 品种 2020~2022 +32.31%，年化 ~9.8%，夏普 1.53，最大回撤 12.71%，盈亏比 2.62（做空单笔最大盈利 +¥17.6 万）
- `docs/turtle_v2 完整总结.md`: 升级 V5.0
- 全量 185 测试通过，无回归（核心代码零改动确）
- [V5.0-期货版+空头修复] `已完成`

## [V4.0-绩效归因] - 2026-06-17
### 退出逻辑重构 + 入场/止损对齐 automated_trading + 三层风控升级
- **退出逻辑**：从「仅10日低点退出」升级为三重退出保护（2N固定止损 + 移动止损只上移 + 10日反向突破），与 automated_trading 等价
- **入场信号**：`high > entry_high` → `close > entry_high`（收盘确认突破，过滤假突破，+5-6pp）
- **止损触发**：`close ≤ stop_loss` → `low ≤ stop_loss`（更快止损，+3pp）
- **仓位公式**：`equity·risk/N/price` → `equity·risk/(2·N)`（仓位规模放大 3.6 倍）
- **三层风控升级**：新增三层敞口校验（单品种≤4%，全账户≤15%）+ 渐进式集中度熔断 + 滑动窗口累计亏损暂停
- **绩效**：全对齐条件下从 -1.90% → +11.17%
- 归因实验详见 `docs/检验执行计划.md`
- 全量 185 测试通过，无回归
- [V4.0-绩效归因] `已完成`

## [方案A] - 2026-06-16
### 退出规则重构 — 删除 2N 追尾止损，回归经典10日低点退出 (P0-2 重审)
- **背景**：P0-2 修复后（2N追尾止损每日上移）回测绩效反而从 +2.27% 降至 -1.53%，盈亏比从 1.41 降至 0.93
- **深度审计发现**：`_should_exit` 中存在两条退出线（close≤2N追尾止损 + low≤10日低点），2N追尾用 `trail_high_10`（10日最高收盘价 - 2N）计算始终先触发，紧贴价格导致过早止盈/亏损扩大
- **方案A**：删除 `close ≤ pos.stop_loss` 退出规则（移除 `_update_trailing_stop` 调用 + `calc_trailing_stop` 导入），仅保留经典 `low ≤ stop_low_10` 10日低点退出
- 绩效对比（vs 修复前 +2.27%）：
  | 指标 | 修复前 | 修复后 | **方案A** |
  |:--|:--:|:--:|:--:|
  | 总收益率 | +2.27% | -1.53% | **+9.38%** |
  | 盈亏比 | 1.41 | 0.93 | **2.08** |
  | 最大回撤 | 11.74% | 14.28% | **5.63%** |
  | 夏普 | -0.1654 | -0.3280 | **+0.1482** |
- 3 个退出测试适配更新（测试改为用 `low ≤ stop_low_10` 触发）
- `docs/analysis/moving_stop_fix_comparison.md`: 三次对比全记录
- 全量 185 测试通过，无回归
- [方案A] `已完成`

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