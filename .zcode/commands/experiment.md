---
name: experiment
description: 实验生命周期管理。开实验/跑回测/检查/通过/失败。用法: /experiment start <名称>, /experiment run, /experiment check, /experiment pass, /experiment fail, /experiment status, /experiment list
skills: experiment
argument-hint: start <name> | run | check | pass | fail | status | list
---

# 实验管理命令

自动加载 experiment skill 处理 `/experiment` 请求。
将 `$ARGUMENTS` 作为子命令参数传给 skill。

- `start <name>` — 开新实验，引导输入假设和成功标准
- `run` — 跑全套回测
- `check` — 检查结果是否达标
- `pass` — 通过并合并到 main
- `fail` — 失败并关闭分支
- `status` — 当前实验状态
- `list` — 所有实验清单
