---
name: experiment
description: >
  实验生命周期管理。管理 turtle_v2 项目的实验流程（分支/回测/检查/合并）。
  当用户提到"开实验"、"跑实验"、"实验验证"、"做实验"、"实验通过/失败"、
  或任何与"实验"、"S??_*"、"exp/"相关的话题时触发。
  底层调用 scripts/experiment.py 处理 git/回测等机械操作。
---

# 实验管理 Skill

## 架构

```
用户对话 ↔ Skill（你） ↔ scripts/experiment.py（机械操作）
```

- **你**（AI agent）负责对话引导、展示结果、做决策判断
- **`scripts/experiment.py`** 负责 git 操作、跑回测、解析实验文档
- **不要手动执行 git 操作**（branch/merge/commit）— 全部交给 CLI

## 实验状态标记

| 标记 | 含义 |
|:--:|:--|
| 📦 待验证 | 已立项未启动 |
| 🔄 运行中 | 正在改代码跑回测 |
| ✅ 通过 | 验证通过已合并 |
| ❌ 失败 | 验证失败已关闭 |

---

## 流程

### 1. 开实验 `/experiment start`

用户说"开个实验"或"做个实验验证XXX"时触发。

**步骤**：
1. 向用户确认三要素：
   - **实验名称**（简短英文，如 `short_factor`）
   - **假设**（一句话）
   - **成功标准**（可量化，如 `CAGR >= 14`、`Sharpe >= 0.8`）
2. 执行：
   ```bash
   py scripts/experiment.py start <name> -H "<假设>" -C "<标准1>" -C "<标准2>"
   ```
3. 展示结果：
   - 实验编号（如 S15）
   - 分支名（如 `exp/S15_short_factor`）
   - 文档路径
4. 提示用户下一步：修改代码 → `py scripts/experiment.py run`

### 2. 跑回测 `/experiment run`

用户说"跑一下"、"跑回测"、"看效果"时触发。

**步骤**：
1. 执行：
   ```bash
   py scripts/experiment.py run
   ```
   （如果用户说"快速看一下"，加 `--quick`）
2. 展示结果摘要（CAGR/Sharpe/MDD/Trades）
3. 提示下一步：`/experiment check` 检查是否达标

### 3. 检查结果 `/experiment check`

用户说"检查"、"达标了吗"、"看看结果"时触发。

**步骤**：
1. 执行：
   ```bash
   py scripts/experiment.py check
   ```
2. 逐条展示成功标准通过/不通过：
   ```
   ✅ CAGR ≥ 14 (实际: 15.2)
   ❌ Sharpe ≥ 0.8 (实际: 0.72)
   ✅ MDD ≤ 20 (实际: 16.3)
   ```
3. 询问用户决定：
   - **通过** → `/experiment pass`
   - **不通过，继续改** → 修改代码后重新 `/experiment run`
   - **放弃** → `/experiment fail`

### 4. 通过 `/experiment pass`

用户说"通过了"、"合并吧"、"定型"时触发。

**步骤**：
1. 执行：
   ```bash
   py scripts/experiment.py pass
   ```
   如果用户想打标签，加 `--tag <name>`
2. 展示合并结果
3. 提示：实验分支可删除

### 5. 失败 `/experiment fail`

用户说"失败了"、"放弃"、"不做了"时触发。

**步骤**：
1. 问用户失败原因
2. 执行：
   ```bash
   py scripts/experiment.py fail --reason "<原因>"
   ```
3. 展示关闭结果

### 6. 状态 `/experiment status`

用户说"当前状态"、"我在哪个分支"、"实验进度"时触发。

**步骤**：
1. 执行：
   ```bash
   py scripts/experiment.py status
   ```

### 7. 清单 `/experiment list`

用户说"都有哪些实验"、"实验列表"时触发。

**步骤**：
1. 执行：
   ```bash
   py scripts/experiment.py list
   ```

---

## 规则

1. **不要手动 git branch/merge/commit** — 所有 git 操作通过 `scripts/experiment.py` 完成
2. **不要手动编辑实验文档** — 交给 `scripts/experiment.py` 处理
3. **实验必须在 exp/ 分支上进行** — main 分支上不要直接改实验代码
4. **开实验前确认成功标准** — 没有量化标准不要开始
5. **每次跑完回测都 check** — 不要跳过检查直接合并
6. **失败的实验也要留文档** — ❌ 也是有用的记录

## 输出格式

展示结果时用表格和 emoji，让用户一目了然：

```
📊 实验结果
━━━━━━━━━━━━━━━━━━━━━
CAGR:     15.2%   ✅
Sharpe:   0.72    ❌
MDD:      16.3%   ✅
Trades:   142     ✅
━━━━━━━━━━━━━━━━━━━━━
通过率: 3/4
建议: 不满足 Sharpe 标准，继续优化或放弃
```
