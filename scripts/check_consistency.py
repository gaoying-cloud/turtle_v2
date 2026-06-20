#!/usr/bin/env python
"""
一致性校验脚本 (V1.0)
========================
在 pre-commit 阶段执行，检查文档与代码状态的一致性。

使用：py scripts/check_consistency.py

退出码：
  0 = 无问题
  1 = 警告（不阻断提交）
  2 = 阻断（必须修复后提交）
"""

import re
import sys
import yaml
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── 阶段-文件映射 ──
# 设计文档 §9 中的交付物必须在此登记
STAGE_FILES = {
    "S0": ["requirements.txt", "config/turtle_config.yaml"],
    "S1": ["src/data_pipeline.py", "scripts/pull_data.py"],
    "S2": ["src/turtle_core.py"],
    "S3": ["strategies/turtle_trading.py", "scripts/run_backtest.py"],
    "S4": ["src/risk_parity.py"],
    "S5": ["src/benchmarks.py", "scripts/run_comparison.py"],
    "S6": ["scripts/run_grid_search.py"],
    "S7": ["scripts/run_stress_test.py"],
    "S8": ["scripts/gen_report.py"],
}

# ── README 纯净度检查关键词 ──
README_FORBIDDEN_TERMS = [
    "atr_period", "breakout_period", "stop_period",
    "stop_atr_multiple", "risk_per_unit", "max_units",
    "N值", "risk_parity", "Ledoit-Wolf",
]


def load_design_metadata() -> dict:
    """读取 design doc 的 YAML 头"""
    path = ROOT / "docs" / "strategy_design_v3.0.md"
    if not path.exists():
        return {"version": "unknown", "error": "strategy_design_v3.0.md not found"}
    content = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not m:
        return {"version": "unknown", "error": "no YAML header"}
    try:
        return yaml.safe_load(m.group(1)) or {"version": "unknown"}
    except yaml.YAMLError:
        return {"version": "unknown", "error": "YAML parse error"}


def extract_file_refs(text: str) -> list:
    """从文本中提取代码文件引用路径（含目录前缀）"""
    refs = []
    dir_prefixes = {
        "src/": r"src/([\w/]+\.(?:py|yaml|yml))",
        "scripts/": r"scripts/([\w/]+\.(?:py|yaml|yml))",
        "config/": r"config/([\w/]+\.(?:ya?ml))",
        "strategies/": r"strategies/([\w/]+\.(?:py|yaml|yml))",
        "docs/": r"docs/([\w/]+\.(?:md|py|yaml|yml))",
        "tests/": r"tests/([\w/]+\.(?:py|yaml|yml))",
    }
    for prefix, pat in dir_prefixes.items():
        for match in re.finditer(pat, text):
            refs.append(prefix + match.group(1))
    return list(set(refs))


def extract_bare_file_refs(text: str) -> list:
    """提取缺少目录前缀的裸文件名引用，用于辅助警告"""
    bare = []
    for match in re.finditer(r'`([\w-]+\.(?:py|yaml|yml|md))`', text):
        fn = match.group(1)
        # 排除已有目录前缀的、CHANGELOG.md 等根目录文件、和明确指向根目录的
        if '/' not in match.group() and fn not in ('CHANGELOG.md', 'README.md'):
            bare.append(fn)
    return list(set(bare))


# ────────────────────────────────────────────
# 检查项
# ────────────────────────────────────────────

def check_version_consistency(warnings: list, errors: list):
    """检查设计文档版本 vs 代码文件版本"""
    meta = load_design_metadata()
    design_ver = meta.get("version", "unknown")
    if design_ver == "unknown":
        warnings.append(f"无法确定设计文档版本: {meta.get('error', 'unknown')}")
        return

    for py_file in (ROOT / "src").glob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        # 查找 docstring 中的版本引用, 如 "版本：3.0" 或 "V3.0"
        m = re.search(r"(?:版本|Version|version)[：:]?\s*(V?)([\d.]+)", content)
        if m:
            code_ver = m.group(2)
            if code_ver != design_ver:
                warnings.append(
                    f"版本不一致: {py_file.name} 声称 v{code_ver}, "
                    f"设计文档 v{design_ver}"
                )


def check_file_refs_in_design(errors: list, warnings: list):
    """检查设计文档中引用的所有文件路径是否存在"""
    path = ROOT / "docs" / "strategy_design_v3.0.md"
    if not path.exists():
        errors.append("strategy_design_v3.0.md 不存在")
        return
    content = path.read_text(encoding="utf-8")
    refs = extract_file_refs(content)

    # 已知外部引用（存在于 automated_trading 项目，不存在于 turtle_v2）
    EXTERNAL_REFS = {"src/strategy_engine.py", "docs/检验执行计划.md",
                     "scripts/analyze_n_percentile.py",
                     "scripts/screen_candidates.py", "tests/test_screening.py"}
    refs = [r for r in refs if r not in EXTERNAL_REFS]

    for ref in refs:
        if not (ROOT / ref).exists():
            errors.append(f"设计文档引用不存在的文件: {ref}")

    # 检查裸文件名（缺少目录前缀）
    bare = extract_bare_file_refs(content)
    for fn in sorted(bare):
        warnings.append(
            f"设计文档引用 `{fn}` 缺少目录前缀（如 scripts/{fn}），请补全"
        )


def check_stage_status(errors: list):
    """检查阶段状态与实际文件存在性是否一致"""
    path = ROOT / "docs" / "strategy_design_v3.0.md"
    if not path.exists():
        return  # 文件还未就绪时不检查
    content = path.read_text(encoding="utf-8")

    for stage_name, files in STAGE_FILES.items():
        # 找到状态标记: | stage_name | ... | ✅/🔄/⏳ |
        pattern = rf"\| *{stage_name} *\|.*\| *([✅🔄⏳]) *\|"
        m = re.search(pattern, content)
        if not m:
            continue
        status = m.group(1)

        all_exist = all((ROOT / f).exists() for f in files)
        any_exist = any((ROOT / f).exists() for f in files)

        if status == "✅" and not all_exist:
            errors.append(
                f"阶段 {stage_name} 标记为已完成(✅)，但交付文件缺失: "
                + ", ".join(f for f in files if not (ROOT / f).exists())
            )
        elif status == "⏳" and any_exist:
            errors.append(
                f"阶段 {stage_name} 标记为未开始(⏳)，但已有交付文件存在: "
                + ", ".join(f for f in files if (ROOT / f).exists())
            )


def check_readme_purity(warnings: list):
    """检查 README 是否包含策略参数"""
    path = ROOT / "README.md"
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    for term in README_FORBIDDEN_TERMS:
        if term.lower() in content.lower():
            warnings.append(
                f"README.md 包含策略参数术语 '{term}'。"
                f"策略参数应仅在 docs/strategy_design_v3.0.md 中定义。"
            )


# ────────────────────────────────────────────
# 主入口
# ────────────────────────────────────────────

def main():
    warnings = []
    errors = []

    check_version_consistency(warnings, errors)
    check_file_refs_in_design(errors, warnings)
    check_stage_status(errors)
    check_readme_purity(warnings)

    # 输出
    exit_code = 0
    if warnings:
        print("\n[WARN] 一致性警告:")
        for w in warnings:
            print(f"  [WARN] {w}")
        exit_code = 1

    if errors:
        print(f"\n[FAIL] 一致性检查失败 (共 {len(errors)} 项):")
        for e in errors:
            print(f"  [FAIL] {e}")
        exit_code = 2

    if not warnings and not errors:
        print("[OK] 一致性检查通过")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
