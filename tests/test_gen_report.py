"""
S8 综合报告生成 — 单元测试

覆盖：
    - load_best_params() 回退逻辑
    - 生成摘要表（通过/条件通过/不通过判定）
    - 报告包含所有 5 个章节
    - 缺失数据时优雅降级
    - 冒烟测试（需要数据缓存）
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.gen_report import (
    load_best_params,
    generate_summary_table,
    generate_performance_table,
    generate_params_section,
    generate_report,
    PASS_TARGETS,
)


# ════════════════════════════════════════════════════════════
#  load_best_params
# ════════════════════════════════════════════════════════════

class TestLoadBestParams:
    def test_returns_first_record_from_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "best_params.json"
            data = [{"mode": "A", "atr_period": 20, "alpha": 0.05}]
            path.write_text(json.dumps(data), encoding="utf-8")
            result = load_best_params(path)
            assert result["atr_period"] == 20
            assert result["alpha"] == 0.05

    def test_fallback_to_config_defaults(self):
        """JSON 不存在时使用 config 默认值。"""
        result = load_best_params(Path("/nonexistent/path.json"))
        assert "atr_period" in result
        assert "alpha" in result
        assert result["mode"] == "A"


# ════════════════════════════════════════════════════════════
#  generate_summary_table
# ════════════════════════════════════════════════════════════

class TestGenerateSummaryTable:
    def test_all_pass(self):
        metrics = {"cagr": 18.0, "max_drawdown": 15.0, "sharpe": 1.2,
                   "profit_factor": 2.0, "total_trades": 80}
        table = generate_summary_table(metrics)
        assert "✅" in table
        assert "**总体判定**" in table
        assert "**✅ 通过**" in table or "**5/5**" in table

    def test_all_fail(self):
        metrics = {"cagr": 5.0, "max_drawdown": 35.0, "sharpe": 0.3,
                   "profit_factor": 0.8, "total_trades": 10}
        table = generate_summary_table(metrics)
        assert "❌" in table

    def test_partial_pass(self):
        """2 fail + 3 pass = 条件通过。"""
        metrics = {"cagr": 18.0, "max_drawdown": 35.0, "sharpe": 0.9,
                   "profit_factor": 1.0, "total_trades": 60}
        table = generate_summary_table(metrics)
        assert "⚠️ 条件通过" in table

    def test_handles_none_values(self):
        metrics = {"cagr": None, "max_drawdown": None, "sharpe": None,
                   "profit_factor": None, "total_trades": None}
        table = generate_summary_table(metrics)
        assert "⚪ 无数据" in table


# ════════════════════════════════════════════════════════════
#  generate_performance_table
# ════════════════════════════════════════════════════════════

class TestGeneratePerformanceTable:
    def test_contains_key_metrics(self):
        metrics = {"initial_cash": 200000, "final_value": 350000, "cagr": 12.5,
                   "sharpe": 0.85, "max_drawdown": 18.0, "win_rate": 45.0,
                   "profit_factor": 1.6, "total_trades": 120,
                   "annual_vol": 10.0, "calmar": 0.7, "total_return": 75.0,
                   "concentration_cut": 3, "dd_warning": 2, "loss_pause": 1, "t1_stop_delay": 5}
        table = generate_performance_table(metrics)
        assert "年化收益率" in table
        assert "夏普比率" in table
        assert "最大回撤" in table
        assert "风控统计" in table
        assert "仓位集中度熔断" in table


# ════════════════════════════════════════════════════════════
#  generate_params_section
# ════════════════════════════════════════════════════════════

class TestGenerateParamsSection:
    def test_fallback_when_no_data(self):
        section = generate_params_section(None, None)
        assert "⚠️" in section or "尚未运行" in section

    def test_with_mock_data(self):
        df_full = pd.DataFrame({"sharpe": [0.9, 0.8], "cagr": [12.0, 10.0]})
        section = generate_params_section(df_full, df_full)
        assert "样本外衰减" in section


# ════════════════════════════════════════════════════════════
#  generate_report
# ════════════════════════════════════════════════════════════

class TestGenerateReport:
    def test_contains_all_5_sections(self):
        metrics = {"cagr": 12.0, "max_drawdown": 20.0, "sharpe": 0.7,
                   "profit_factor": 1.4, "total_trades": 60,
                   "final_value": 300000, "initial_cash": 200000,
                   "total_return": 50.0, "annual_vol": 12.0, "calmar": 0.6,
                   "win_rate": 42.0, "concentration_cut": 2, "dd_warning": 1,
                   "loss_pause": 0, "t1_stop_delay": 3}
        report = generate_report(metrics)
        for section_title in ["核心目标达成度", "核心绩效", "基准对比", "最优参数组合", "压力测试"]:
            assert section_title in report, f"缺少章节: {section_title}"
        assert report.startswith("#")  # Markdown h1
        assert report.endswith("\n")


# ════════════════════════════════════════════════════════════
#  smoke test
# ════════════════════════════════════════════════════════════

class TestSmoke:
    @pytest.fixture(scope="class")
    def data_exists(self):
        data_dir = ROOT / "data" / "etf_daily"
        parquet_files = list(data_dir.glob("*.parquet"))
        return len(parquet_files) >= 6

    def test_gen_report_does_not_crash(self, data_exists):
        """默认参数运行 gen_report，至少返回一段有效 Markdown。"""
        if not data_exists:
            pytest.skip("数据缓存不足，跳过冒烟测试")
        from scripts.gen_report import run_backtest_with_best, generate_report

        params = {"atr_period": 20, "breakout_period": 20,
                  "stop_period": 10, "stop_atr_multiple": 2.0, "alpha": 0.05}
        metrics = run_backtest_with_best(params, "2022-01-01", "2022-12-31", "A")
        assert metrics, "回测应成功"
        assert metrics["final_value"] > 0

        report = generate_report(metrics, mode="A")
        assert len(report) > 100
        assert "核心目标达成度" in report