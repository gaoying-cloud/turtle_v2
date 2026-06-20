# 跨市场ETF海龟组合策略 · 项目管控模型

**版本**：1.0  
**日期**：2026-06-14  
**用途**：定义本项目文档/代码一致性保证体系，供人类开发者与 AI 助手共同遵守。

---

## 1. 背景：为什么需要这份文档

前身项目 `automated_trading/` 存在 5 类同步失败问题：

| # | 问题类型 | 真实案例 |
|:--|:--|:--|
| 1 | 版本号不一致 | `strategy_engine.py` 文件头写 v1.1，代码实际是 v1.2 |
| 2 | 统计数据过期 | README 测试数 571→674 未更新；PROGRESS 637→674 未更新 |
| 3 | 状态标记错误 | README 架构图中 execution_engine 标记为 🟡（开发中），实际已完工 |
| 4 | 跨文件矛盾 | strategy_design 与操作手册对同一参数的描述不一致 |
| 5 | 文件路径过期 | PROGRESS 文件树描述与磁盘实际结构不同步 |

本管控模型的目标：**从 "靠人记住" 转换为 "靠机制拦截"**。

---

## 2. 文件最小化原则

项目只保留 **3 个手工维护的管理文件**，其余信息由脚本自动生成或由 Git 历史承载。

| 文件 | 定位 | 维护方式 |
|:--|:--|:--|
| `README.md` | **状态入口** — 别人打开项目第一眼看到的东西 | 含 `<!-- STATUS_START -->` 占位符，由 `gen_report.py` 自动填充 |
| `CHANGELOG.md` | **时间线** — 何时做了什么变更 | 追加式手工记录，每次阶段完成追加一条 |
| `docs/strategy_design_v3.0.md` | **空间** — 策略全量设计，**唯一权威来源** | 手工维护，禁止在 README 中重复策略参数 |

**禁止行为**：
- 不创建第二个设计文件
- 不在 README 中包含策略参数（ATR 周期、突破周期、止损倍数等）——这些属于唯一权威来源 `strategy_design_v3.0.md`
- 不维护 `PROGRESS.md`——进度由 Git commit history 承载

---

## 3. 单一真相来源原则

**`docs/strategy_design_v3.0.md`** 是策略细节的 **唯一权威来源**。

- `README.md`：不包含任何策略参数、公式、阈值。只包含"当前状态 + 如何运行"。
- `CHANGELOG.md`：只包含"何时改了什么东西"。不含策略细节。
- 代码 docstring：只包含从设计文档派生的版本号和简要描述，不重复具体参数含义。
- 测试代码：测试断言的值直接引用策略参数，不硬编码。

**参数定义链**：
```
strategy_design_v3.0.md (定义)
       ↓
config/turtle_config.yaml (参数值，与设计文档对应)
       ↓
src/turtle_core.py (读取配置，执行计算)
       ↓
tests/ (断言值从 config 读取，不做硬编码)
```

---

## 4. 机制矩阵

| 同步维度 | 机制 | 触发时机 | 阻断级别 |
|:--|:--|:--|:--|
| 版本号一致 | `check_consistency.py` 校验 | pre-commit | ⚠ 警告 |
| 统计数字 | `gen_report.py` 自动生成 | 每阶段完成时 | 自动修复 |
| 状态标记 | 文件存在性检测 | pre-commit | 🔴 阻断 |
| 跨文件矛盾 | 单设计文件 + README 纯净度检查 | pre-commit | ⚠ 警告 |
| 文件路径 | 引用路径存在性校验 | pre-commit | 🔴 阻断 |
| 设计实现一致 | test suite 覆盖设计文档中的每个约束 | CI / pytest | 🔴 阻断 |

### 阻断级别说明

- **🔴 阻断**：pre-commit hook 检测到问题后，拒绝 `git commit`。开发者必须修复后才能提交。
- **⚠ 警告**：`gen_report.py` 或 `check_consistency.py` 输出警告信息，不阻断提交，但建议尽快修复。
- **自动修复**：脚本直接修改 README 中的占位符区域，无需人工干预。

---

## 5. Pre-Commit 校验清单

`scripts/check_consistency.py` 在 pre-commit 阶段执行以下检查：

### 5.1 版本号一致性

读取 `docs/strategy_design_v3.0.md` 头部 YAML 元数据中的版本号，与所有 `src/*.py` 文件 docstring 中的版本引用比对。不一致则输出警告。

### 5.2 文件路径完整性

提取 `docs/strategy_design_v3.0.md` 中所有形如 `src/xxx.py`、`scripts/xxx.py`、`config/xxx.yaml` 的引用路径，检查磁盘上是否存在。不存在则阻断提交。

### 5.3 阶段状态与现实一致性

在 `docs/strategy_design_v3.0.md` 的 §9 实施路线图表中，检查：
- 标记 `✅` 的阶段，对应的交付文件必须全部存在
- 标记 `⏳` 的阶段，对应的交付文件不应存在
- 标记 `🔄` 的阶段，对应文件可存在可不存在（开发中）

如果不一致，说明状态标记与文件系统现实不符，阻断提交。

### 5.4 README 纯净度检查

扫描 `README.md`，检测是否包含策略术语（ATR、N值、突破周期等）。如果包含，输出警告——策略参数应仅在设计文档中定义。

### 5.5 检查项白名单

检出误报时，可在 `check_consistency.py` 中添加白名单条目，但必须在注释中说明理由。

---

## 6. Gen_Report 自动生成逻辑

每阶段完成时运行 `py scripts/gen_report.py`，该脚本执行：

### 6.1 测试计数更新
```python
pytest --collect-only -q → 解析测试统计数字
```
替换 README.md 中 `<!-- STATUS_START -->` 区域的测试数字。

### 6.2 阶段状态检测
遍历 `STAGE_FILES` 映射表，根据文件存在性推断当前阶段。将结果写入 `<!-- STATUS_START -->` 区域。

```python
STAGE_FILES = {
    "S0": ["requirements.txt", "config/turtle_config.yaml"],
    "S1": ["src/data_pipeline.py", "scripts/pull_data.py"],
    ...
}
```

### 6.3 版本号校验
读取设计文档版本号，与代码文件版本号比对，输出差异警告。

### 6.4 回测快照
如果 `results/backtest/` 中存在最新 summary.json，提取 Cagr、Sharpe、MaxDrawdown 等指标，写入 README。

---

## 7. 日常开发流程

```
修改代码或文档 → git add → git commit
                              ↓
                    pre-commit hook 触发
                              ↓
                    check_consistency.py 执行
                              ↓
                    阻断问题？ → 是 → 拒绝 commit，显示错误
                              ↓ 否
                    commit 成功
                              ↓
                    阶段结束时 → py scripts/gen_report.py
                              ↓
                    README 状态自动刷新
                              ↓
                    CHANGELOG.md 追加一条记录
```

---

## 8. 修改此模型的原则

1. 如果需要新增管理文件，必须在 `docs/governance_model.md` 的 §2 中登记，并说明与现有 3 个文件的信息定位差异。
2. `check_consistency.py` 是 **校验盾牌**，不是**阻拦盾牌**。阻断级别仅用于"文件路径不存在、阶段状态与文件现实矛盾"这类必然导致项目混乱的问题。不应用来阻断代码风格之类可修复但不破坏信息一致性的问题。
3. 如果某个检查项连续 3 次产生误报且无实际收益，将其降级为警告或删除。
4. 所有自动生成逻辑（`gen_report.py`）的输出，应保持可手工修正的格式——不改变非占位符区域。

---

## 9. 文件路径映射（从 automated_trading 到 turtle_v2）

| automated_trading 位置 | turtle_v2 位置 | 说明 |
|:--|:--|:--|
| `docs/跨市场ETF海龟组合策略_V2.md` | `docs/strategy_design_v3.0.md` | 改名+版本升级 |
| `docs/设计文件 vs 代码审计 — 完成总结.md` | — | 不迁移，经验教训已吸收至此文档 |
| `scripts/v2_config.py` | `config/turtle_config.yaml` | 从 Python 改为 YAML |
| `scripts/v2_pull_data.py` | `scripts/pull_data.py` | 改名 |
| `scripts/v2_run_simple.py` | `scripts/run_backtest.py` (Backtrader 版) | 从纯 Python 改为 Backtrader |
| `data/etf_v2/` | `data/etf_daily/` | 目录改名 |
| `results/etf_v2/` | `results/` | 归入新结构 |
| `src/strategy_engine.py` (相关部分) | `src/turtle_core.py` | 核心逻辑复制，保持完全隔离 |

---

*本文件不包含任何策略参数。策略参数的定义请参见 `docs/strategy_design_v3.0.md`。*
