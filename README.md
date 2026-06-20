# 跨市场ETF海龟组合策略 V3.0

4 只低相关性 ETF（中证500/创业板/纳指ETF/黄金ETF） + 海龟交易法则 + 三层权重（ATR仓位×风险平价偏移）。

---

## 状态

<!-- STATUS_START -->
当前阶段: S9 (Dry-Run准备) | 测试: 205 passed | 最后回测: +210.53%(V5.12, 初始12万)
<!-- STATUS_END -->

## 快速开始

```bash
pip install -r requirements.txt
py scripts/pull_data.py         # 拉取数据
py scripts/run_backtest.py      # 运行回测
```

## 文档

| 文件 | 内容 |
|:--|:--|
| `docs/strategy_design_v3.0.md` | 策略全量设计（参数、规则、风控） |
| `docs/governance_model.md` | 项目管控模型（一致性保证体系） |
| `CHANGELOG.md` | 版本变更记录 |
| `config/turtle_config.yaml` | 回测参数配置 |

## 项目结构

```
├── config/           # 配置文件
├── src/              # 核心模块（ATR、仓位、风险平价）
├── strategies/       # Backtrader Strategy 子类
├── scripts/          # 运行脚本
├── tests/            # 测试
├── data/etf_daily/   # 数据缓存
└── results/          # 回测输出
