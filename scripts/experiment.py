#!/usr/bin/env python
"""
实验生命周期管理 CLI

自动化实验流程中的机械操作，配合 AI agent 使用。

用法：
    py scripts/experiment.py start <name>                      # 开实验
    py scripts/experiment.py run [--quick]                     # 跑全套回测
    py scripts/experiment.py check                             # 对比成功标准
    py scripts/experiment.py pass [--tag TAG]                  # 通过→合并
    py scripts/experiment.py fail                              # 失败→关闭
    py scripts/experiment.py status                            # 当前状态
    py scripts/experiment.py list                              # 所有实验清单

典型流程：
    py scripts/experiment.py start S15_short_factor
    # (改代码)
    py scripts/experiment.py run
    py scripts/experiment.py check
    # (满意/不满意)
    py scripts/experiment.py pass
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
EXPERIMENTS_DIR = ROOT / "docs" / "experiments"
TEMPLATE_PATH = EXPERIMENTS_DIR / "TEMPLATE.md"
RESULTS_METRICS = ROOT / "results" / "report_metrics.json"


# ════════════════════════════════════════════════════════════
#  工具函数
# ════════════════════════════════════════════════════════════

def _git(*args: str) -> str:
    """执行 git 命令，返回 stdout"""
    result = subprocess.run(["git"] + list(args), capture_output=True, text=True, cwd=ROOT)
    if result.returncode != 0:
        print(f"  ⚠️ git {' '.join(args)} 失败:\n{result.stderr.strip()}")
    return result.stdout.strip()

def _current_branch() -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD")

def _branch_exists(name: str) -> bool:
    return bool(_git("rev-parse", "--verify", "--quiet", name))

def _check_clean_working_tree() -> bool:
    status = _git("status", "--porcelain")
    return len(status) == 0

def _next_exp_number() -> int:
    """从现有实验文档推断下一个编号"""
    existing = list(EXPERIMENTS_DIR.glob("S??_*.md"))
    nums = []
    for p in existing:
        m = re.search(r"S(\d+)_", p.name)
        if m:
            nums.append(int(m.group(1)))
    # 也检查分支
    branches = _git("branch", "-a").split("\n")
    for b in branches:
        m = re.search(r"S(\d+)_", b)
        if m:
            nums.append(int(m.group(1)))
    return max(nums) + 1 if nums else 11  # S10 已定型


# ════════════════════════════════════════════════════════════
#  子命令
# ════════════════════════════════════════════════════════════

def cmd_start(args: argparse.Namespace):
    """开实验：创建文档 + 创建分支"""
    name = args.name
    # 确保名称不含空格
    name = name.replace(" ", "_")

    # 检查工作区是否干净（开分支前）
    if not _check_clean_working_tree() and not args.force:
        print("❌ 工作区有未提交的改动。请先 git commit 或使用 --force")
        sys.exit(1)

    # 推断编号
    num = _next_exp_number()
    doc_name = f"S{num:02d}_{name}.md"
    doc_path = EXPERIMENTS_DIR / doc_name
    branch_name = f"exp/S{num:02d}_{name}"

    # 检查是否已存在
    if doc_path.exists():
        print(f"❌ 实验文档已存在: {doc_path}")
        sys.exit(1)
    if _branch_exists(branch_name):
        print(f"❌ 分支已存在: {branch_name}")
        sys.exit(1)

    # 获取假设和成功标准（交互式或参数）
    hypothesis = args.hypothesis or input("假设: ").strip()
    criteria_lines = []
    if args.criteria:
        criteria_lines = [args.criteria]
    else:
        print("成功标准（每行一条，空行结束）:")
        while True:
            line = input("  > ").strip()
            if not line:
                break
            criteria_lines.append(line)

    if not hypothesis:
        print("❌ 假设不能为空")
        sys.exit(1)
    if not criteria_lines:
        print("❌ 至少需要一个成功标准")
        sys.exit(1)

    # 写入实验文档
    content = f"""# 实验: {name}

## 元数据
- 提出: {date.today()}
- 分支: {branch_name}
- 状态: 🔄 运行中

## 假设
{hypothesis}

## 成功标准
"""
    for c in criteria_lines:
        content += f"- {c}\n"
    content += """
## 结果
（实验完成后填写）

### 数据
| 指标 | 基准 | 实验 | 变化 |
|:--|:--:|:--:|:--:|
| CAGR | | | |
| Sharpe | | | |
| MDD | | | |

### 结论
（通过/失败/搁置）
"""
    doc_path.write_text(content, encoding="utf-8")

    # 创建分支
    _git("checkout", "-b", branch_name)
    _git("add", str(doc_path))
    _git("commit", "-m", f"exp: S{num:02d}_{name} 实验立项 — {hypothesis[:60]}")

    print(f"\n✅ 实验 S{num:02d}_{name} 已启动")
    print(f"   文档: {doc_path}")
    print(f"   分支: {branch_name}")
    print(f"\n   下一步: 修改代码 → py scripts/experiment.py run")


def cmd_run(args: argparse.Namespace):
    """跑全套回测"""
    branch = _current_branch()
    if not branch.startswith("exp/"):
        print("⚠️  当前不在 exp/ 分支上，继续运行将使用当前代码（非实验分支）")

    scripts = [
        ("回测", ["py", "scripts/run_backtest.py"]),
    ]
    if not args.quick:
        scripts.append(("对比", ["py", "scripts/run_comparison.py", "--save"]))
        scripts.append(("压力", ["py", "scripts/run_stress_test.py"]))
        scripts.append(("报告", ["py", "scripts/gen_report.py"]))

    for label, cmd in scripts:
        print(f"\n▶ {'='*50}")
        print(f"▶ {label}")
        print(f"{'='*50}")
        result = subprocess.run(cmd, cwd=ROOT)
        if result.returncode != 0:
            print(f"  ⚠️ {label} 失败 (exit={result.returncode})")

    print(f"\n✅ 回测完成 ({'快速' if args.quick else '全量'})")
    print(f"   查看报告: results/report.md")
    print(f"   查看指标: results/report_metrics.json")
    if not args.quick:
        print(f"   下一步: py scripts/experiment.py check")


def cmd_check(args: argparse.Namespace):
    """检查结果是否满足成功标准"""
    branch = _current_branch()

    # 从实验文档读取成功标准
    doc = _find_current_doc()
    if not doc:
        print("❌ 找不到对应的实验文档 (docs/experiments/S??_*.md)")
        print("   请确保文档存在，或手动指定: --doc S15_xxx.md")
        sys.exit(1)

    criteria = _parse_criteria(doc)
    if not criteria:
        print("⚠️  实验文档中未找到成功标准")
        return

    # 读取回测结果
    if not RESULTS_METRICS.exists():
        print(f"❌ 回测结果不存在: {RESULTS_METRICS}")
        print("   请先运行: py scripts/experiment.py run")
        sys.exit(1)

    metrics = json.loads(RESULTS_METRICS.read_text(encoding="utf-8"))

    print(f"\n{'='*60}")
    print(f"实验结果检查")
    print(f"{'='*60}")
    print(f"实验: {doc.name.replace('.md', '')}")
    print(f"分支: {branch}")
    print(f"报告: results/report.md")
    print()

    # 显示当前指标
    _print_metrics(metrics)

    print(f"\n成功标准:")
    passed = 0
    total = len(criteria)
    for c in criteria:
        result_str = _evaluate_criterion(c, metrics)
        if result_str.startswith("✅"):
            passed += 1
        print(f"  {result_str}")

    print(f"\n{'─'*60}")
    print(f"通过率: {passed}/{total}")
    if passed == total:
        print(f"🎉 全部通过！可以合并: py scripts/experiment.py pass")
    elif passed >= total / 2:
        print(f"⚠️  部分通过，确认是否接受: py scripts/experiment.py pass --force")
    else:
        print(f"❌ 多数未通过，建议: py scripts/experiment.py fail")


def cmd_pass(args: argparse.Namespace):
    """通过实验：合并到 main"""
    branch = _current_branch()
    if not branch.startswith("exp/"):
        print(f"❌ 当前不在 exp/ 分支上 (当前: {branch})")
        sys.exit(1)

    if not _check_clean_working_tree():
        print("❌ 工作区有未提交的改动，请先 git commit")
        sys.exit(1)

    # 确认
    if not args.force:
        confirm = input(f"确认将 {branch} 合并到 main? (y/N): ").strip().lower()
        if confirm != "y":
            print("已取消")
            return

    # 更新实验文档
    doc = _find_current_doc()
    if doc:
        content = doc.read_text(encoding="utf-8")
        # 填入通过状态
        content = content.replace("状态: 🔄 运行中", "状态: ✅ 通过")
        # 如果 results_metrics.json 存在，填入数据
        if RESULTS_METRICS.exists():
            metrics = json.loads(RESULTS_METRICS.read_text(encoding="utf-8"))
            data_block = f"""### 数据
| 指标 | 值 |
|:--|:--:|
| CAGR | {metrics.get('cagr', 'N/A')}% |
| Sharpe | {metrics.get('sharpe', 'N/A')} |
| MDD | {metrics.get('max_drawdown', 'N/A')}% |
| 交易次数 | {metrics.get('total_trades', 'N/A')} |
"""
            # 替换数据表格
            content = re.sub(r"### 数据\n\n.*?\n### 结论",
                           f"### 数据\n\n{data_block}\n### 结论",
                           content, flags=re.DOTALL)
            content = content.replace("（实验完成后填写）", "")
        doc.write_text(content, encoding="utf-8")
        _git("add", str(doc))

    # 合并到 main
    tag = args.tag or branch.replace("exp/", "done/")
    _git("checkout", "main")
    _git("merge", "--squash", branch)

    commit_msg = f"{branch.replace('exp/', 'S')}: 实验定型"
    if doc:
        hypo = _parse_hypothesis(doc)
        if hypo:
            commit_msg += f" — {hypo[:80]}"
    _git("commit", "-m", commit_msg)

    # 打 tag
    if args.tag:
        _git("tag", "-f", args.tag)
        print(f"   标签: {args.tag}")

    # 重命名分支
    _git("branch", "-m", branch, tag)

    print(f"\n✅ {branch} 已合并到 main")
    print(f"   分支已重命名: {tag}")
    print(f"   清理: git branch -D {tag}  # 确认无误后删除")


def cmd_fail(args: argparse.Namespace):
    """失败实验：关闭分支"""
    branch = _current_branch()
    if not branch.startswith("exp/"):
        print(f"❌ 当前不在 exp/ 分支上 (当前: {branch})")
        sys.exit(1)

    reason = args.reason or input("失败原因: ").strip()

    # 更新文档
    doc = _find_current_doc()
    if doc:
        content = doc.read_text(encoding="utf-8")
        content = content.replace("状态: 🔄 运行中", "状态: ❌ 失败")
        if reason:
            content = content.replace("### 结论\n（通过/失败/搁置）",
                                    f"### 结论\n❌ 失败\n\n**原因**: {reason}")
        doc.write_text(content, encoding="utf-8")
        _git("add", str(doc))
        _git("commit", "-m", f"{branch}: 实验失败 — {reason[:60]}")

    # 回到 main，重命名分支
    _git("checkout", "main")
    new_name = branch.replace("exp/", "abandoned/")
    _git("branch", "-m", branch, new_name)

    print(f"\n❌ {branch} 已关闭")
    print(f"   分支已重命名: {new_name}")
    print(f"   清理: git branch -D {new_name}  # 确认无误后删除")


def cmd_status(args: argparse.Namespace):
    """显示当前实验状态"""
    branch = _current_branch()
    print(f"当前分支: {branch}")

    if branch.startswith("exp/"):
        doc = _find_current_doc()
        if doc:
            print(f"实验文档: {doc}")
            print(doc.read_text(encoding="utf-8"))
        else:
            print("⚠️  未找到对应实验文档")

        if RESULTS_METRICS.exists():
            metrics = json.loads(RESULTS_METRICS.read_text(encoding="utf-8"))
            print(f"\n最新回测结果:")
            _print_metrics(metrics)
    elif branch == "main":
        print("📌 主分支（稳定版本）")
        latest_tag = _git("describe", "--tags", "--abbrev=0")
        if latest_tag:
            print(f"   最新标签: {latest_tag}")
    else:
        print(f"📌 非实验分支")


def cmd_list(args: argparse.Namespace):
    """列出所有实验"""
    docs = sorted(EXPERIMENTS_DIR.glob("S??_*.md"))
    branches = _git("branch", "-a").split("\n")

    print(f"{'编号':<8s} {'名称':<30s} {'状态':<10s} {'分支':<30s}")
    print("-" * 80)

    for doc_path in docs:
        content = doc_path.read_text(encoding="utf-8")
        name = doc_path.stem  # S15_short_factor
        parts = name.split("_", 1)
        num = parts[0] if len(parts) > 1 else name
        display_name = parts[1] if len(parts) > 1 else name

        # 解析状态
        status_match = re.search(r"状态:\s*([📦🔄✅❌⏸️]\S*)", content)
        status = status_match.group(1) if status_match else "📦"

        # 找对应分支
        related = [b.strip().replace("*", "").strip()
                   for b in branches if display_name in b or num in b]
        branch_str = related[0] if related else "—"

        print(f"{num:<8s} {display_name:<30s} {status:<10s} {branch_str:<30s}")

    # 统计
    total = len(docs)
    running = sum(1 for d in docs if "🔄" in d.read_text())
    passed = sum(1 for d in docs if "✅" in d.read_text())
    failed = sum(1 for d in docs if "❌" in d.read_text())
    pending = total - running - passed - failed
    print("-" * 80)
    print(f"总计: {total}  |  运行中: {running}  |  通过: {passed}  |  失败: {failed}  |  待验证: {pending}")


# ════════════════════════════════════════════════════════════
#  辅助函数
# ════════════════════════════════════════════════════════════

def _find_current_doc() -> Optional[Path]:
    """从当前分支名找到对应的实验文档"""
    branch = _current_branch()
    # exp/S15_xxx → S15_xxx
    key = branch.replace("exp/", "").replace("done/", "").replace("abandoned/", "")
    candidates = list(EXPERIMENTS_DIR.glob(f"{key}.md"))
    if candidates:
        return candidates[0]
    # 放宽匹配
    for p in EXPERIMENTS_DIR.glob("S??_*.md"):
        if key[3:] in p.stem or p.stem in key:
            return p
    return None

def _parse_criteria(doc: Path) -> list[str]:
    """从实验文档解析成功标准"""
    content = doc.read_text(encoding="utf-8")
    lines = content.split("\n")
    criteria = []
    in_criteria = False
    for line in lines:
        if line.strip() == "## 成功标准":
            in_criteria = True
            continue
        if in_criteria:
            if line.startswith("## "):
                break
            if line.strip().startswith("- "):
                criteria.append(line.strip()[2:])
    return criteria

def _parse_hypothesis(doc: Path) -> Optional[str]:
    content = doc.read_text(encoding="utf-8")
    m = re.search(r"## 假设\n\n(.+?)(?:\n|$)", content)
    return m.group(1).strip() if m else None

def _print_metrics(metrics: dict):
    """格式化输出回测指标"""
    for key in ["cagr", "sharpe", "max_drawdown", "total_trades", "win_rate", "profit_factor", "final_value"]:
        val = metrics.get(key)
        if val is not None:
            if key == "cagr":
                print(f"  CAGR:       {val}%")
            elif key == "sharpe":
                print(f"  Sharpe:     {val}")
            elif key == "max_drawdown":
                print(f"  MDD:        {val}%")
            elif key == "total_trades":
                print(f"  Trades:     {int(val)}")
            elif key == "win_rate":
                print(f"  胜率:       {val}%")
            elif key == "profit_factor":
                print(f"  盈亏比:     {val}")
            elif key == "final_value":
                print(f"  最终净值:   ¥{val:,.2f}")

def _evaluate_criterion(criterion: str, metrics: dict) -> str:
    """评估一条成功标准是否满足。支持格式：
    - CAGR >= 14
    - Sharpe > 0.8
    - MDD <= 20
    - Trades >= 50
    """
    m = re.match(r"(\w+)\s*([><=!]+)\s*([\d.]+)", criterion)
    if not m:
        return f"❓ 无法解析: {criterion}"

    key_map = {
        "CAGR": "cagr", "cagr": "cagr",
        "Sharpe": "sharpe", "sharpe": "sharpe",
        "MDD": "max_drawdown", "mdd": "max_drawdown",
        "Trades": "total_trades", "trades": "total_trades",
        "胜率": "win_rate", "WinRate": "win_rate",
        "盈亏比": "profit_factor", "ProfitFactor": "profit_factor",
    }
    metric_key = key_map.get(m.group(1))
    if not metric_key or metric_key not in metrics:
        return f"❓ 未知指标: {m.group(1)}"

    actual = metrics[metric_key]
    threshold = float(m.group(3))
    op = m.group(2)

    if op == ">=":
        ok = actual >= threshold
    elif op == ">":
        ok = actual > threshold
    elif op == "<=":
        ok = actual <= threshold
    elif op == "<":
        ok = actual < threshold
    elif op == "==":
        ok = abs(actual - threshold) < 0.001
    elif op == "!=":
        ok = abs(actual - threshold) >= 0.001
    else:
        return f"❓ 未知运算符: {op}"

    icon = "✅" if ok else "❌"
    arrow = "≥" if "=" in op else op
    return f"{icon} {m.group(1)} {arrow} {threshold} (实际: {actual})"


# ════════════════════════════════════════════════════════════
#  CLI 入口
# ════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="实验生命周期管理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # start
    p = sub.add_parser("start", help="开实验")
    p.add_argument("name", help="实验名称，如 S15_short_factor")
    p.add_argument("--hypothesis", "-H", help="假设")
    p.add_argument("--criteria", "-C", help="成功标准（单条）")
    p.add_argument("--force", "-f", action="store_true", help="强制创建（即使工作区不干净）")

    # run
    p = sub.add_parser("run", help="跑回测")
    p.add_argument("--quick", "-q", action="store_true", help="仅跑回测，跳过对比/压力/报告")

    # check
    sub.add_parser("check", help="检查结果")

    # pass
    p = sub.add_parser("pass", help="通过→合并")
    p.add_argument("--tag", "-t", help="标签名")
    p.add_argument("--force", "-f", action="store_true", help="跳过确认")

    # fail
    p = sub.add_parser("fail", help="失败→关闭")
    p.add_argument("--reason", "-r", help="失败原因")

    # status
    sub.add_parser("status", help="当前状态")

    # list
    sub.add_parser("list", help="实验清单")

    args = parser.parse_args()

    # 路由
    {
        "start": cmd_start,
        "run": cmd_run,
        "check": cmd_check,
        "pass": cmd_pass,
        "fail": cmd_fail,
        "status": cmd_status,
        "list": cmd_list,
    }[args.command](args)


if __name__ == "__main__":
    main()
