# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

跨市场ETF海龟组合策略 V3.0 — 6 只低相关性 ETF（中证500/创业板/纳指ETF/黄金ETF/豆粕ETF/日经ETF）+ 海龟交易法则 + 三层权重（ATR仓位 × 集中度衰减 × 风险平价偏移）。

**双策略体系**：
- **海龟策略**（`strategies/turtle_trading.py`）：基于 Backtrader 的海龟交易法则，含仓位管理/加仓/移动止损/品种退化检测
- **N字结构策略**（`strategies/n_structure.py`）：纯 pandas/numpy 实现的 N 字形态识别策略，独立资金池运行

**当前绩效**：海龟 CAGR 17.23%, MDD 13.84%, 夏普 1.10 | N字结构 S42 OOS CAGR 6.1%, MDD 18.7%, 289 tests passed

## 命令速查

```bash
# 数据
py scripts/pull_data.py                    # 拉取数据（支持增量/全量/单品种）

# 回测
py scripts/run_backtest.py                 # 单次回测
py scripts/run_n_structure.py              # N字结构回测（--portfolio 组合模式）
py scripts/run_comparison.py --save        # 基准对比 B1-B4
py scripts/run_stress_test.py              # 压力测试

# 参数优化
py scripts/run_grid_search.py --workers 4  # 网格搜索（7个核心参数）
py scripts/scan_n_structure.py             # N字参数扫描（--robustness 稳健性）

# 报告
py scripts/gen_report.py                   # 综合报告

# 日线信号
py scripts/daily_signal.py                 # 海龟日线信号
py scripts/daily_signal_n_structure.py     # N字结构日线信号

# 测试
py -m pytest tests/ -q                     # 快速测试
py -m pytest tests/ -v --tb=short -x      # 全量测试（CI模式，含超时）
py -m pytest tests/test_n_structure.py -v  # 单文件测试
py -m pytest tests/ -q -k "test_name"     # 单测试用例
py -m pytest tests/ --cov=src             # 覆盖率测试

# 其它
py scripts/verify_adjustment.py            # 前复权验证
py scripts/check_consistency.py            # 文档一致性校验
```

**Windows 规范**：使用 `py` 命令启动 Python

## 项目结构

```
├── config/
│   └── turtle_config.yaml        # 统一配置文件（品种/参数/风控/回测区间）
├── src/                          # 核心模块（Layer 1-2，不依赖上层）
│   ├── config_loader.py          #   YAML 配置加载（消除硬编码品种列表）
│   ├── data_pipeline.py          #   Tushare Pro 数据管道（Parquet缓存/前复权/增量更新）
│   ├── data_utils.py             #   DataFrame加载/对齐/Backtrader feed 转换
│   ├── turtle_core.py            #   海龟核心：ATR/唐奇安通道/头寸计算/加仓止损/信号过滤器/Hurst
│   ├── risk_parity.py            #   风险平价：Ledoit-Wolf收缩估计/Newton-Raphson权重求解/α融合
│   ├── market_regime.py          #   市场状态判断器（N值分位/方向效率/N值趋势融合）
│   └── benchmarks.py             #   基准策略 B1(买入持有)/B2(等权再平衡)/B3(ATR等风险)
├── strategies/                   # 策略层（Layer 3）
│   ├── turtle_trading.py         #   Backtrader TurtleStrategy（入场/退出/加仓/品种退化/风控）
│   └── n_structure.py            #   N字结构策略（纯pandas：形态识别/趋势出场/组合回测）
├── scripts/                      # 脚本层（Layer 4，从根目录调用）
│   ├── run_backtest.py           #   核心回测引擎（可被其他脚本import复用）
│   ├── run_n_structure.py        #   N字结构回测入口
│   ├── run_grid_search.py        #   网格搜索（多进程/两阶段/权重搜索）
│   ├── run_comparison.py         #   基准对比
│   ├── gen_report.py             #   综合报告生成
│   └── ...
├── tests/                        #  pytest 测试（289 个函数）
│   ├── test_turtle_core.py       #   海龟核心计算测试
│   ├── test_turtle_strategy.py   #   TurtleStrategy 集成测试
│   ├── test_n_structure.py       #   N字结构策略测试（44个）
│   ├── test_risk_parity.py       #   风险平价测试
│   ├── test_data_pipeline.py     #   数据管道测试
│   └── ...
├── data/etf_daily/               # Parquet 缓存（每个品种独立文件）
├── results/                      # 回测输出（report/comparison/grid_search/diagnostics/etc.）
└── docs/                         # 技术文档与实验记录
    ├── architecture.md           #   模块依赖全景与架构描述
    └── experiments/              #   实验记录（S10-S45）
        ├── S43_combo_roadmap.md  #   双策略组合路线图（S43-S45 三阶段）
        └── BASELINE.md (项目根)  #   N 字结构策略基线记录（S42 为当前版本）
```

## 架构层级

```
Layer 0: 外部库       pandas, numpy, backtrader, yaml, tushare
Layer 1: 工具模块     config_loader, data_utils, data_pipeline
Layer 2: 计算模块     turtle_core, risk_parity, market_regime, benchmarks
Layer 3: 策略层       turtle_trading.py (TurtleStrategy) + n_structure.py (NStructureStrategy)
Layer 4: 脚本层       scripts/run_*.py
Layer 5: 数据输出     results/ 下的报告与 CSV
```

**核心规则**：下层不依赖上层。`turtle_core.py` 不依赖 `turtle_trading.py`。

## 关键设计决策

### 配置驱动
- 所有品种列表、参数、风控阈值统一在 `config/turtle_config.yaml`，代码中无硬编码品种

### 三层权重系统
1. **ATR 仓位管理**：基于波动率的头寸规模，风险比例 × 净值 / (ATR × 乘数)
2. **集中度衰减**：持仓品种越多，新入场 risk_pct 递减（progressive fade table）
3. **风险平价偏移**（α融合）：ATR等权与Ledoit-Wolf风险平价权重的线性融合

### 双策略入场确认
- 海龟策略支持 `breakout`（20日突破）和 `dual`（突破 + MA10金叉双模式）
- N字结构通过 `entry_confirm_bars` 控制连续确认 K 线数防假突破

### 防止未来数据
- 唐奇安通道采用 `shift(1)` 确保不含当日价格
- N字结构用 `confirm_k` 延迟确认极值，`local_half_window ≤ confirm_k` 做运行时校验
- N字结构信号在 bar i-1 判断，在 bar i 开盘价执行
- 前复权方向公式：`adj[t] / adj[latest]`（非 `latest/adj[t]`）

### 数据管道要点
- ETF 数据通过 Tushare `fund_daily` 拉取，增量更新 + 全量重做前复权
- `_apply_factor_adjustment`（官方因子）+ `_detect_and_adjust_splits`（价格检测）组合复权
- 自愈校验 `_validate_adjustment`：close 单日涨跌幅 >50% 判定失败返回空，不落盘
- 缓存格式：Parquet + Snappy 压缩

## 开发规范

- **Git 提交格式**：`S{N}: 阶段名 — 简要说明`（如 `S41: N字入场质量过滤`）
- **提交前**：`py -m pytest tests/ -q` 确认全部通过
- **命名约定**：阶段 `S{N}` 前缀（如 S39, S40, S41），无 `v` 前缀
- **数据拉取**：必须设置 `TUSHARE_TOKEN` 环境变量
- **郑商所后缀**：使用 `.ZCE`（非 `.CZC`）
- **期货模式**：`py scripts/run_backtest.py --futures`（12 品种双向）

## 核心算法

- **ATR**：指数平滑，初始值=前 N 个 TR 的简单平均，后续 `EMA = (1-1/N) × prev + (1/N) × TR`
- **Hurst指数**：重标极差法 (R/S)，H > 0.55 趋势持续，< 0.45 均值回归
- **风险平价**：Spinu (2013) Newton-Raphson 迭代求解
- **N字结构**：从右向左扫描 B→D→A 三级局部极值 + 确认延迟
- **品种退化**：三规则自动检测（沉默型/拦截型/磨损型）
