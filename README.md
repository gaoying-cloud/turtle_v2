# 跨市场ETF海龟组合策略

6 只低相关性 ETF（中证500/创业板/纳指ETF/黄金ETF/豆粕ETF/日经ETF）+ 海龟交易法则 + 三层权重（ATR仓位 × 集中度衰减 × 风险平价偏移）。

---

## 状态

<!-- STATUS_START -->
当前阶段: S10 定型 | 测试: 245 passed | 6品种 | **5/5 目标达成** ✅ | CAGR=17.23% Sharpe=1.10 MDD=13.84%
<!-- STATUS_END -->

## 核心绩效 (2026-07-08 重跑)

| 指标 | 值 | 目标 | 状态 |
|:--|:--:|:--:|:--:|
| 年化收益率 (CAGR) | 17.23% | 15.0% | ✅ |
| 最大回撤 (MDD) | 13.84% | 25.0% | ✅ |
| 夏普比率 | 1.10 | 0.8 | ✅ |
| 盈亏比 | 4.78 | 1.5 | ✅ |
| 交易次数 | 156 | 50 | ✅ |
| **总体判定** | **5/5** | — | **✅ 通过** |

> 对比旧版 (06-30): CAGR 12.97% → 17.23%, 夏普 0.76 → 1.10, MDD 24.2% → 13.84%

## 快速开始

```bash
pip install -r requirements.txt
py scripts/pull_data.py                    # 拉取数据
py scripts/run_backtest.py                 # 运行回测
py scripts/run_comparison.py --save        # 基准对比
py scripts/run_stress_test.py              # 压力测试
py scripts/gen_report.py                   # 生成报告
py scripts/run_grid_search.py --workers 4  # 网格搜索（夜间跑）
```

## 网格搜索

搜索 7 个核心参数 + 可选 Stage-2 权重倍率：

```bash
# 标准搜索（7290 次回测，约 2-3 小时）
py scripts/run_grid_search.py --workers 4

# 快速验证
py scripts/run_grid_search.py --quick

# 两阶段搜索（粗筛+精搜，更快）
py scripts/run_grid_search.py --two-stage --workers 4

# 完整搜索 + 品种权重倍率优化（额外 32 次回测）
py scripts/run_grid_search.py --workers 4 --weight-search
```

Stage-2 权重倍率在核心参数定型后,搜索品种级超配/低配系数：

| 品种 | 倍率范围 | 说明 |
|:--|:--:|:--|
| 513100.SH 纳指ETF | 1.0 ~ 2.5 | 超配（趋势强、胜率高） |
| 159985.SZ 豆粕ETF | 0.2 ~ 1.0 | 低配（ATR 权重虚高、利润效率低） |

## 项目结构

```
├── config/                   # 配置文件 (turtle_config.yaml)
├── src/                      # 核心模块
│   ├── turtle_core.py        #   海龟核心（ATR/仓位/SignalFilter）
│   ├── risk_parity.py        #   风险平价 + 权重倍率
│   └── data_utils.py         #   数据加载
├── strategies/
│   └── turtle_trading.py     #   Backtrader 策略（含 weight_multipliers）
├── scripts/
│   ├── run_backtest.py       #   单次回测
│   ├── run_comparison.py     #   基准对比 B1-B4
│   ├── run_stress_test.py    #   压力测试
│   ├── run_grid_search.py    #   网格搜索 + Stage-2 权重搜索
│   ├── gen_report.py         #   综合报告
│   ├── daily_signal.py       #   日线信号
│   └── ...
├── tests/                    # 205 passed
├── data/etf_daily/           # 数据缓存 (Parquet)
├── results/                  # 回测输出
│   ├── report.md
│   ├── comparison/
│   ├── stress_test/
│   ├── diagnostics/
│   ├── grid_search/
│   └── archive/
└── docs/
    └── _archive/             # 归档旧文档
```

## 夜间计划

```bash
# 1. 网格搜索 + 权重优化
py scripts/run_grid_search.py --workers 4 --weight-search

# 2. 重跑报告（含网格搜索结果和最优权重）
py scripts/gen_report.py
```
