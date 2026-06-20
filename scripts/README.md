# Scripts 目录说明

本目录包含海龟策略回测框架（turtle_v2）的各类脚本。按职能分组如下：

---

## 📦 数据获取

| 脚本 | 作用 | 调用示例 |
|------|------|----------|
| `pull_data.py` | 从 Tushare 拉取 ETF 日线数据（支持增量/全量/单品种） | `py scripts/pull_data.py` `py scripts/pull_data.py --symbol 510500.SH` |
| `pull_futures.py` | 从 Tushare 拉取期货主力连续日线数据 | `py scripts/pull_futures.py` `py scripts/pull_futures.py --symbol I.DCE` |
| `adjust_splits.py` | 检测并修正 ETF 拆分/合并事件（复权因子校正） | `py scripts/adjust_splits.py` |

## 🔬 回测与参数优化

| 脚本 | 作用 | 调用示例 |
|------|------|----------|
| `run_backtest.py` | **核心回测引擎**（也被其他脚本作为模块 import） | `py scripts/run_backtest.py` `py scripts/run_backtest.py --mode B --plot` |
| `run_grid_search.py` | 参数网格搜索（支持多进程并行、样本内外分割） | `py scripts/run_grid_search.py` `py scripts/run_grid_search.py --quick` |
| `run_stress_test.py` | 极端情景回测与压力测试（历史情景回放 + 合成冲击 + 流动性枯竭） | `py scripts/run_stress_test.py` `py scripts/run_stress_test.py --scenarios A1,A2` |

## 📊 对比分析

| 脚本 | 作用 | 调用示例 |
|------|------|----------|
| `run_comparison.py` | 四种基准策略对比（等权持有、再平衡、ATR 风险平价、海龟） | `py scripts/run_comparison.py` `py scripts/run_comparison.py --mode B --save` |
| `run_correlation_monitor.py` | ETF 间滚动相关性监控（60 日窗口，>0.6 预警） | `py scripts/run_correlation_monitor.py` `py scripts/run_correlation_monitor.py --plot` |
| `yearly_benchmark.py` | 逐年收益对比（策略 vs 大盘指数 vs ETF 持有） | `py scripts/yearly_benchmark.py` `py scripts/yearly_benchmark.py --mode B` |

## ✅ 质量验证

| 脚本 | 作用 | 调用示例 |
|------|------|----------|
| `cross_validate.py` | TickFlow ↔ Tushare 数据交叉校验 | `py scripts/cross_validate.py` `py scripts/cross_validate.py -s 510500` |
| `check_consistency.py` | 文档与代码状态一致性校验（pre-commit 用） | `py scripts/check_consistency.py` |

## 📝 报告生成

| 脚本 | 作用 | 调用示例 |
|------|------|----------|
| `gen_report.py` | 综合报告生成（汇总各阶段结果，可选重新回测） | `py scripts/gen_report.py` `py scripts/gen_report.py --mode B --no-backtest` |

##  品种筛选量化框架自动化脚本

| 脚本 | 作用 | 调用示例 |
|------|------|----------|
| `screen_candidates.py` | 品种筛选工具 | `py scripts/screen_candidates.py` |



scripts/screen_candidates.py

---

## 🗂 子目录

| 目录 | 说明 |
|------|------|
| `_ad_hoc/` | 一次性分析/实验脚本（硬编码参数，仅供参考，不再维护）。包括：`_analyze_trend.py`（唐奇安突破收益分布分析）、`analyze_n_percentile.py`（市场状态判断器历史验证）、`compare_filters.py`（多信号确认组合对比） |

---

## ℹ️ 备注

- 所有脚本建议从项目根目录用 `py scripts/<script_name>.py` 调用（部分脚本依赖 `src/` 和 `strategies/` 的模块路径）。
- `run_backtest.py` 可被其他脚本 `from scripts.run_backtest import run_backtest` 导入复用。
- `_ad_hoc/` 下的脚本不再维护，仅保留作为分析历史参考。
