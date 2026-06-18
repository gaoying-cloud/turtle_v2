---
description: turtle_v2 开发规范（Git / 测试 / 数据）
globs: *
alwaysApply: true
---

# turtle_v2 开发规范

## Git 提交规范（强制）

- 完成一个阶段后必须执行：`git add -A && git commit -m "消息"`
- 提交消息格式：`S{N}: 阶段名 — 简要说明`
  - 示例：`S5: 基准对比 — 添加 Buy&Hold 和 60/40 对比`
- **禁止**提交消息包含：
  - 换行符（`\n`）
  - `task_progress:` 关键词
  - checklist（`[ ]` / `[x]` 等）
  - **违反以上任一条会触发 PowerShell 解析错误**

## 测试要求

- 每个阶段必须包含对应的 `tests/test_*.py`
- 提交前必须跑 `py -m pytest tests/ -q` 确认全部通过
- 新增功能必须同步添加测试用例

## 数据规则

- **环境变量**：未设置 `TUSHARE_TOKEN` 时，数据管道会抛出 `ValueError`
- **ETF 数据**：`data/etf_daily/{code}.parquet`（通过 `py scripts/pull_data.py` 拉取）
- **期货数据**：`data/futures_daily/{code}.parquet`（通过 `py scripts/pull_futures.py` 拉取）
- **郑商所后缀**：使用 `.ZCE`（非 `.CZC`），示例：`CF.ZCE`

## 回测入口参数

| 命令 | 品种范围 |
|------|----------|
| `py scripts/run_backtest.py` | ETF 全品种 |
| `py scripts/run_backtest.py --t0-only` | 仅 T+0（纳指+黄金）做空验证 |
| `py scripts/run_backtest.py --futures` | 期货 12 品种双向 |