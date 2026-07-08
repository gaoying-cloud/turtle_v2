# 实验框架使用手册

## 概述

单仓库 + 实验分支的工作流，自动化 CLI `scripts/experiment.py` 处理机械操作，
AI agent（或用户手动）负责决策环节。

```
                   ┌─────────────┐
                   │  start      │  ← 用户输入假设 + 成功标准
                   └──────┬──────┘
                          ▼
                   ┌─────────────┐
                   │  改代码     │  ← 用户/AI 修改
                   └──────┬──────┘
                          ▼
                   ┌─────────────┐
                   │  run        │  ← 自动跑回测/对比/压力/报告
                   └──────┬──────┘
                          ▼
                   ┌─────────────┐
                   │  check      │  ← 自动对比成功标准
                   └──────┬──────┘
                          ▼
              ┌───────────┴───────────┐
              │                       │
         ┌────┴────┐           ┌──────┴──────┐
         │  pass   │           │   fail      │  ← 用户决定
         └────┬────┘           └──────┬──────┘
              │                       │
         ┌────┴────┐           ┌──────┴──────┐
         │ merge   │           │  关闭分支   │
         │ to main │           │  记录原因   │
         └─────────┘           └─────────────┘
```

---

## 一、自动化 CLI

`scripts/experiment.py` 处理全部机械操作：

```bash
py scripts/experiment.py start <name>    # 开实验：创建文档 + 创建分支
py scripts/experiment.py run              # 跑全套回测（回测+对比+压力+报告）
py scripts/experiment.py run --quick     # 仅跑回测（快速迭代时用）
py scripts/experiment.py check            # 对比结果 vs 成功标准
py scripts/experiment.py pass             # 通过→合并到 main
py scripts/experiment.py fail             # 失败→关闭分支
py scripts/experiment.py status           # 显示当前分支 + 最新结果
py scripts/experiment.py list             # 所有实验清单
```

### 典型流程（5 分钟）

```bash
# 1. 开实验 — 自动创建文档 + 分支
py scripts/experiment.py start S15_short_factor

# 2. 改代码（手动或 AI）
# ...

# 3. 跑全套回测
py scripts/experiment.py run

# 4. 检查结果是否达标
py scripts/experiment.py check

# 5a. 通过 → 自动合并
py scripts/experiment.py pass

# 5b. 不满意 → 继续改代码 → 再跑 → 再检查
# 5c. 放弃 → 关闭
py scripts/experiment.py fail --reason "做多Sharpe下降0.05，不通过"
```

## 二、完整流程（5 步）

### 第 1 步：提出实验

用 CLI 自动创建实验文档和分支：

```bash
py scripts/experiment.py start S15_short_factor
```

工具会交互式询问：
```
假设: 做空低 n_entry 加仓效应导致亏损放大 36%
成功标准（每行一条，空行结束）:
  > CAGR >= 14
  > Sharpe >= 0.8
  > MDD <= 20
  >
```

也可以一步到位：
```bash
py scripts/experiment.py start S15_short_factor \
    -H "做空低 n_entry 加仓效应导致亏损放大 36%" \
    -C "CAGR >= 14" -C "Sharpe >= 0.8" -C "MDD <= 20"
```

执行后自动：
1. 创建 `docs/experiments/S15_short_factor.md`
2. `git checkout -b exp/S15_short_factor`
3. `git commit` 实验文档

### 第 2 步：创建实验分支

`start` 命令已自动完成。手动等同时：

```bash
git checkout main && git checkout -b exp/S15_my_experiment
```

**分支命名规则**：

| 前缀 | 含义 | 示例 |
|:--|:--|:--|
| `exp/S??_` | 进行中的实验 | `exp/S15_short_factor` |
| `done/S??_` | 已合并的成果 | `done/S10_params` |
| `abandoned/S??_` | 放弃的实验 | `abandoned/S13_adaptive_exit` |

### 第 3 步：在实验分支上工作

```bash
# 正常开发，多次 commit
git add src/turtle_core.py
git commit -m "S15: 修改 XXX 逻辑"
py scripts/run_backtest.py          # 回测验证
py scripts/gen_report.py            # 看效果

# 不满意接着改
git add .
git commit -m "S15: 调整 YYY 参数"

# 随时可以切回 main 看正式版本
git checkout main
# 做完实验再切回来
git checkout exp/S15_my_experiment
```

**注意**：
- 每次实验迭代都 commit，方便回溯
- 实验分支上可以随便改，不影响 `main`
- 如果实验周期长，定期 `git rebase main` 同步主线最新代码

### 第 4 步：终判

实验完成后，更新实验文档的结果部分，然后根据结论选择：

#### ✅ 通过 → 合并到 main

```bash
git checkout main
git merge --squash exp/S15_my_experiment
git commit -m "S15: XXX 实验定型 — 关键结论摘要"
git branch -m exp/S15_my_experiment done/S15_my_experiment
```

`--squash` 会把实验分支上的所有 commit 压成一个，保持 `main` 历史干净。

更新 `docs/experiments/S15_my_experiment.md`：
- 状态改为 `✅ 通过`
- 填写结果数据

#### ❌ 失败 → 关闭分支

```bash
# 不合并代码，只留文档记录
git checkout main
git branch -m exp/S15_my_experiment abandoned/S15_my_experiment
```

更新 `docs/experiments/S15_my_experiment.md`：
- 状态改为 `❌ 失败`
- 填写失败原因和数据

#### 📦 搁置 → 保留分支

不合并也不删，等以后有条件再继续。

### 第 5 步：清理

```bash
# 删除远端已合并的分支
git push origin --delete exp/S15_my_experiment

# 本地删除（已 rename 为 done/ 或 abandoned/ 后）
git branch -D exp/S15_my_experiment    # -D 强制删除
```

---

## 二、实验文档模板说明

每个实验文档包含三个区块，用 `TEMPLATE.md` 创建：

```markdown
## 元数据         ← 自动维护，不依赖 git
提出日期、分支名、当前状态

## 假设           ← 必须写清楚，否则实验没有方向
一句话的说清要验证什么

## 成功标准       ← 必须量化，否则不知道是否通过
- CAGR 提升 ≥ 0.5pp
- Sharpe 不下降
- ...

## 结果           ← 实验完成后填写
### 数据          ← 表格对比基准 vs 实验
### 结论          ← 通过/失败 + 原因
```

**状态标记**：

| 标记 | 含义 |
|:--:|:--|
| 📦 待验证 | 已立项，未启动 |
| 🔄 运行中 | 实验分支上正在改代码 |
| ✅ 通过 | 验证通过，已合并到 main |
| ❌ 失败 | 验证失败，有记录可查 |
| ⏸️ 搁置 | 暂时不做，以后可能有条件 |

---

## 三、几个典型场景

### 场景 A：参数微调实验（简单）

```bash
# 1. 写文档
cp TEMPLATE.md S16_atr_period.md

# 2. 开分支
git checkout -b exp/S16_atr_period

# 3. 改参数，跑回测
# 修改 config/turtle_config.yaml
py scripts/run_backtest.py
py scripts/gen_report.py

# 4. 比较结果
# 看 report.md 中的指标

# 5. 通过则合并
git checkout main
git merge --squash exp/S16_atr_period
git commit -m "S16: ATR 周期微调 — 25→22"
```

### 场景 B：新增策略逻辑（复杂）

```bash
# 1. 写文档
cp TEMPLATE.md S17_my_new_feature.md

# 2. 开分支
git checkout -b exp/S17_my_new_feature

# 3. 改代码，多次 commit
git commit -m "S17: 新增 XXX 信号逻辑"
git commit -m "S17: 回测框架适配"
git commit -m "S17: 测试用例"

# 4. 回测验证
py scripts/run_backtest.py
py scripts/run_comparison.py --save
py scripts/run_stress_test.py

# 5. 失败了
git checkout main
git branch -m exp/S17_my_new_feature abandoned/S17_my_new_feature
# 更新文档 -> ❌ 失败
```

### 场景 C：多个实验并行

```bash
# main 同时开两个分支
git checkout -b exp/S16_atr_period    # 分支 A
git checkout main
git checkout -b exp/S17_my_feature    # 分支 B

# 在 A 上改完、合并
git checkout main
git merge --squash exp/S16_atr_period
git commit -m "S16: ATR 微调"

# B 需要 rebase 到最新的 main（包含 A 的改动）
git checkout exp/S17_my_feature
git rebase main
# 继续在 B 上工作
```

---

## 四、与版本管理的关系

```
CHANGELOG.md     ← 记录已合并到 main 的实验（S10/S11/...）
docs/experiments/ ← 所有实验的状态（含进行中和失败的）
git tags         ← 关键基线打 tag（S10_params_final）
```

每次实验合并后：

```bash
# 1. 更新 docs/experiments/ 文档状态
# 2. 更新 CHANGELOG.md
# 3. 打 tag（可选）
git tag S16_atr_final
```

---

## 五、检查清单

**开实验前**：
- [ ] 实验文档已创建（假设 + 成功标准）
- [ ] 从 `main` 最新版分支
- [ ] 分支名符合 `exp/S??_<name>` 规范

**实验进行中**：
- [ ] 每次有意义的改动都 commit
- [ ] 定期回测验证进展
- [ ] 如果周期长，rebase main

**实验收尾**：
- [ ] 实验文档已更新结果
- [ ] 通过 → `merge --squash` 到 main
- [ ] 失败 → rename 为 `abandoned/`
- [ ] 更新 CHANGELOG
- [ ] 本地和远端删除实验分支
