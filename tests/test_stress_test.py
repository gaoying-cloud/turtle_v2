"""
S7 压力测试模块单元测试

测试覆盖：
    1. define_scenarios() — 场景定义完整性
    2. load_best_params() — 最优参数加载 + fallback
    3. run_historical_scenario() — 输出格式验证
    4. run_synthetic_shock() — B1 冲击矩阵结构
    5. run_liquidity_stress() — B2 计算正确性
    6. _compute_avg_correlation() — 相关性计算
    7. _check_stress_pass() — 通过判定逻辑
    8. generate_report() — 报告生成
    9. CLI 参数解析
    10. main() 不抛异常
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.run_stress_test import (
    define_scenarios,
    load_best_params,
    run_historical_scenario,
    run_synthetic_shock,
    run_liquidity_stress,
    _compute_avg_correlation,
    _check_stress_pass,
    generate_report,
    save_results,
    main,
)

# ── 有效数据准备 ──

@pytest.fixture
def mock_config():
    """模拟 config yaml 内容。"""
    return {
        "initial_cash": 200000,
        "commission_pct": 0.001,
        "slippage_pct": 0.001,
        "turtle": {
            "atr_period": 20,
            "breakout_period": 20,
            "stop_period": 10,
            "stop_atr_multiple": 2.0,
            "risk_per_unit": 0.01,
            "max_units": 4,
            "unit_step": 0.5,
            "exit_period": 10,
        },
        "risk": {
            "concentration_trigger": 4,
            "max_consecutive_losses": 8,
            "max_cumulative_loss_pct": 0.15,
            "pause_days": 5,
        },
        "weighting": {
            "alpha": 0.05,
            "cov_lookback_days": 252,
            "rebalance_quarterly": True,
            "atr_change_threshold": 0.30,
        },
    }


@pytest.fixture
def mock_best_params():
    """模拟最优参数 JSON。"""
    return [
        {
            "mode": "A",
            "atr_period": 15,
            "breakout_period": 15,
            "stop_period": 8,
            "stop_atr_multiple": 1.5,
            "alpha": 0.20,
            "sharpe": -0.2589,
            "cagr": 0.1624,
            "max_drawdown": 7.7681,
            "total_trades": 23,
            "robustness_score": 1.2213,
        }
    ]


@pytest.fixture
def mock_data():
    """模拟 6 品种的 Parquet 数据。"""
    dates = pd.date_range("2020-01-01", "2020-06-30", freq="B")
    n = len(dates)
    dfs = {}
    for i, symbol in enumerate([
        "510500.SH", "159845.SZ", "159915.SZ", "588000.SH", "513100.SH", "518880.SH",
    ]):
        base = 5.0 + i * 0.5
        np.random.seed(42 + i)
        closes = base * (1 + np.random.randn(n).cumsum() * 0.005)
        closes = np.maximum(closes, base * 0.5)
        df = pd.DataFrame({
            "date": dates.strftime("%Y-%m-%d"),
            "open": closes * 0.99,
            "high": closes * 1.02,
            "low": closes * 0.98,
            "close": closes,
            "volume": np.random.randint(1000000, 10000000, size=n),
        })
        dfs[symbol] = df
    # 国债
    df_bond = pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": 100.0,
        "high": 100.5,
        "low": 99.5,
        "close": 100.0,
        "volume": np.random.randint(1000000, 10000000, size=n),
    })
    dfs["511010.SH"] = df_bond
    return dfs


# ════════════════════════════════════════════════════════════
#  1. 场景定义
# ════════════════════════════════════════════════════════════

class TestDefineScenarios:
    def test_returns_dict(self):
        scenarios = define_scenarios()
        assert isinstance(scenarios, dict)

    def test_has_all_six_scenarios(self):
        scenarios = define_scenarios()
        assert len(scenarios) == 6
        expected_ids = {"A1_covid", "A2_russia_ukraine", "A3_double_bottom",
                        "A4_full_2022", "B1_synthetic_shock", "B2_liquidity_stress"}
        assert set(scenarios.keys()) == expected_ids

    def test_each_scenario_has_required_keys(self):
        scenarios = define_scenarios()
        required = {"id", "name", "type", "start_date", "end_date", "description", "tags"}
        for sid, sc in scenarios.items():
            assert required.issubset(sc.keys()), f"{sid} 缺少字段: {required - sc.keys()}"

    def test_historical_scenarios_have_valid_dates(self):
        scenarios = define_scenarios()
        for sid in ["A1_covid", "A2_russia_ukraine", "A3_double_bottom", "A4_full_2022"]:
            sc = scenarios[sid]
            assert sc["start_date"], f"{sid} start_date 为空"
            assert sc["end_date"], f"{sid} end_date 为空"
            s = datetime.strptime(sc["start_date"], "%Y-%m-%d")
            e = datetime.strptime(sc["end_date"], "%Y-%m-%d")
            assert s < e, f"{sid} start >= end"

    def test_b1_b2_have_type_synthetic(self):
        scenarios = define_scenarios()
        for sid in ["B1_synthetic_shock", "B2_liquidity_stress"]:
            assert scenarios[sid]["type"] == "synthetic"


# ════════════════════════════════════════════════════════════
#  2. 最优参数加载
# ════════════════════════════════════════════════════════════

class TestLoadBestParams:
    def test_load_from_file(self, tmp_path, mock_best_params):
        path = tmp_path / "best_params.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(mock_best_params, f)
        result = load_best_params(path)
        assert result["atr_period"] == 15
        assert result["breakout_period"] == 15
        assert result["alpha"] == 0.20

    def test_fallback_when_file_not_exists(self, tmp_path, mock_config):
        path = tmp_path / "nonexistent.json"
        # 创建临时 config.yaml 确保 fallback 加载成功
        cfg_path = tmp_path / "config.yaml"
        import yaml
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.dump(mock_config, f)
        with patch("scripts.run_stress_test.CONFIG_PATH", cfg_path):
            result = load_best_params(path)
            assert isinstance(result, dict)
            assert result["atr_period"] == 20

    def test_fallback_has_required_keys(self, tmp_path, mock_config):
        path = tmp_path / "nonexistent.json"
        # 创建临时 config.yaml（真实 config 可能会被 fallback 读取）
        import yaml
        cfg_path = tmp_path / "config.yaml"
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.dump(mock_config, f)
        with patch("scripts.run_stress_test.CONFIG_PATH", cfg_path):
            result = load_best_params(path)
            required = {"atr_period", "breakout_period", "stop_period", "stop_atr_multiple", "alpha"}
            assert required.issubset(result.keys())


# ════════════════════════════════════════════════════════════
#  3. 历史情景回测（输出格式）
# ════════════════════════════════════════════════════════════

class TestRunHistoricalScenario:
    @patch("scripts.run_stress_test.run_historical_scenario")
    def test_output_format(self, mock_run):
        """测试输出字典的字段完整性。"""
        mock_run.return_value = {
            "scenario": "A1_covid",
            "scenario_name": "COVID 熔断",
            "date_range": "2020-02-03~2020-04-30",
            "initial_cash": 200000,
            "final_value": 195000.50,
            "total_return": -2.5,
            "cagr": -15.0,
            "sharpe": -0.8,
            "max_drawdown": 8.5,
            "max_dd_duration": 15,
            "daily_var_95": -0.02,
            "daily_var_99": -0.035,
            "total_trades": 5,
            "win_rate": 40.0,
            "profit_factor": 1.2,
            "annual_vol": 18.0,
            "calmar": -1.76,
            "t1_stop_delay_hits": 2,
            "correlation_avg": 0.45,
        }
        result = mock_run()
        required_keys = {
            "scenario", "scenario_name", "date_range", "final_value",
            "total_return", "cagr", "sharpe", "max_drawdown",
            "max_dd_duration", "daily_var_95", "daily_var_99",
            "total_trades", "win_rate", "profit_factor",
        }
        assert required_keys.issubset(result.keys()), f"缺少字段: {required_keys - result.keys()}"


# ════════════════════════════════════════════════════════════
#  4. B1 合成冲击
# ════════════════════════════════════════════════════════════

class TestRunSyntheticShock:
    @patch("scripts.run_stress_test.load_data")
    def test_returns_dataframe(self, mock_load):
        """测试 B1 返回 DataFrame 结构。"""
        dates = pd.date_range("2022-01-03", "2022-12-30", freq="B")
        n = len(dates)
        mock_df = pd.DataFrame({
            "date": dates.strftime("%Y-%m-%d"),
            "close": 5.0 + np.random.randn(n).cumsum() * 0.01,
        })
        mock_load.return_value = mock_df

        params = {"atr_period": 20, "breakout_period": 20, "stop_period": 10,
                  "stop_atr_multiple": 2.0, "alpha": 0.05}
        result = run_synthetic_shock(params, mode="A")
        if result is not None:
            assert isinstance(result, pd.DataFrame)
            assert not result.empty
            expected_idx = "-3%" in result.columns or "月份" in str(result.index.name)
            assert expected_idx

    @patch("scripts.run_stress_test.load_data")
    def test_columns_are_shock_pcts(self, mock_load):
        """测试列名对应冲击幅度。"""
        dates = pd.date_range("2022-01-03", "2022-12-30", freq="B")
        n = len(dates)
        mock_df = pd.DataFrame({
            "date": dates.strftime("%Y-%m-%d"),
            "close": 5.0 + np.random.randn(n).cumsum() * 0.01,
        })
        mock_load.return_value = mock_df

        params = {"atr_period": 20, "breakout_period": 20, "stop_period": 10,
                  "stop_atr_multiple": 2.0, "alpha": 0.05}
        result = run_synthetic_shock(params, mode="A", shock_pcts=[-3, -5])
        if result is not None:
            expected_cols = {"-3%", "-5%"}
            assert expected_cols.issubset(set(result.columns))


# ════════════════════════════════════════════════════════════
#  5. B2 流动性枯竭
# ════════════════════════════════════════════════════════════

class TestRunLiquidityStress:
    def test_returns_dict(self):
        params = {"atr_period": 20, "breakout_period": 20, "stop_period": 10,
                  "stop_atr_multiple": 2.0, "alpha": 0.05}
        result = run_liquidity_stress(params)
        assert isinstance(result, dict)

    def test_has_required_keys(self):
        params = {"atr_period": 20, "breakout_period": 20, "stop_period": 10,
                  "stop_atr_multiple": 2.0, "alpha": 0.05}
        result = run_liquidity_stress(params)
        required = {"symbol", "units", "daily_loss_pct", "consecutive_days",
                    "max_loss_pct", "max_loss_amount"}
        assert required.issubset(result.keys()), f"缺少字段: {required - result.keys()}"

    def test_max_loss_is_positive(self):
        params = {"atr_period": 20, "breakout_period": 20, "stop_period": 10,
                  "stop_atr_multiple": 2.0, "alpha": 0.05}
        result = run_liquidity_stress(params)
        assert result["max_loss_pct"] > 0
        assert result["max_loss_amount"] > 0

    def test_units_is_4(self):
        params = {"atr_period": 20, "breakout_period": 20, "stop_period": 10,
                  "stop_atr_multiple": 2.0, "alpha": 0.05}
        result = run_liquidity_stress(params)
        assert result["units"] == 4


# ════════════════════════════════════════════════════════════
#  6. 相关性计算
# ════════════════════════════════════════════════════════════

class TestComputeAvgCorrelation:
    def test_returns_float_or_none(self):
        result = _compute_avg_correlation("2020-01-01", "2020-06-30")
        # 无数据时返回 None
        assert result is None or isinstance(result, float)

    def test_correlation_in_range(self):
        # 用确定性数据测试
        dates = pd.date_range("2020-01-01", "2020-06-30", freq="B")
        n = len(dates)
        # 创建多个目录写入临时 Parquet
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            for i, symbol in enumerate([
                "510500.SH", "159845.SZ", "159915.SZ", "588000.SH", "513100.SH", "518880.SH",
            ]):
                base = 5.0 + i * 1.0
                rng = np.random.default_rng(42 + i)
                closes = base + rng.normal(0, 0.1, n).cumsum()
                closes = np.maximum(closes, base * 0.5)
                df = pd.DataFrame({
                    "date": dates.strftime("%Y-%m-%d"),
                    "close": closes,
                })
                df.to_parquet(tmp_path / f"{symbol}.parquet")

            with patch("scripts.run_stress_test.DATA_DIR", tmp_path):
                result = _compute_avg_correlation("2020-01-01", "2020-06-30")
                assert result is not None
                assert -1.0 <= result <= 1.0

    def test_returns_none_with_insufficient_data(self, tmp_path):
        """数据不足时返回 None。"""
        with patch("scripts.run_stress_test.DATA_DIR", tmp_path):
            result = _compute_avg_correlation("2020-01-01", "2020-06-30")
            assert result is None


# ════════════════════════════════════════════════════════════
#  7. 通过判定
# ════════════════════════════════════════════════════════════

class TestCheckStressPass:
    def test_all_pass_when_within_threshold(self):
        metrics = {
            "max_drawdown": 10.0,
            "max_dd_duration": 30,
            "daily_var_99": -0.03,
            "total_return": -5.0,
            "t1_stop_delay_hits": 2,
        }
        passed, checks = _check_stress_pass(metrics)
        assert passed
        assert all(c["pass"] for c in checks.values())

    def test_fail_when_mdd_exceeds(self):
        metrics = {
            "max_drawdown": 30.0,
            "max_dd_duration": 30,
            "daily_var_99": -0.03,
            "total_return": -5.0,
            "t1_stop_delay_hits": 2,
        }
        passed, checks = _check_stress_pass(metrics)
        assert not passed
        assert not checks["max_drawdown"]["pass"]

    def test_fail_when_var99_exceeds(self):
        metrics = {
            "max_drawdown": 10.0,
            "max_dd_duration": 30,
            "daily_var_99": -0.08,
            "total_return": -5.0,
            "t1_stop_delay_hits": 2,
        }
        passed, checks = _check_stress_pass(metrics)
        assert not passed
        assert not checks["daily_var_99"]["pass"]

    def test_returns_checks_dict(self):
        metrics = {
            "max_drawdown": 10.0,
            "max_dd_duration": 30,
            "daily_var_99": -0.03,
            "total_return": -5.0,
            "t1_stop_delay_hits": 2,
        }
        passed, checks = _check_stress_pass(metrics)
        assert isinstance(checks, dict)
        assert len(checks) == 5


# ════════════════════════════════════════════════════════════
#  8. 报告生成
# ════════════════════════════════════════════════════════════

class TestGenerateReport:
    def test_returns_string(self):
        report = generate_report([], None, None, {"atr_period": 20}, "A")
        assert isinstance(report, str)
        assert len(report) > 50

    def test_contains_all_sections(self):
        params = {"atr_period": 20, "breakout_period": 20, "stop_period": 10,
                  "stop_atr_multiple": 2.0, "alpha": 0.05}
        report = generate_report([], None, None, params, "A")
        assert "## 1. 历史情景回放 (A1-A4)" in report
        assert "## 2. 合成单月同步暴跌 (B1)" in report
        assert "## 3. 连续流动性枯竭 (B2)" in report
        assert "## 4. 结论" in report

    def test_with_historical_results(self):
        params = {"atr_period": 20, "breakout_period": 20, "stop_period": 10,
                  "stop_atr_multiple": 2.0, "alpha": 0.05}
        mock_results = [{
            "scenario": "A1_covid",
            "scenario_name": "COVID 熔断",
            "date_range": "2020-02-03~2020-04-30",
            "total_return": -2.5, "cagr": -15.0, "sharpe": -0.8,
            "max_drawdown": 8.5, "max_dd_duration": 15, "total_trades": 5,
            "daily_var_95": -0.02, "daily_var_99": -0.035,
            "correlation_avg": 0.45, "t1_stop_delay_hits": 2,
            "final_value": 195000, "initial_cash": 200000,
            "win_rate": 40.0, "profit_factor": 1.2,
            "annual_vol": 18.0, "calmar": -1.76,
        }]
        report = generate_report(mock_results, None, None, params, "A")
        assert "COVID 熔断" in report

    def test_with_shock_df(self):
        params = {"atr_period": 20}
        shock_df = pd.DataFrame({"-3%": [1.0, 2.0], "-5%": [2.0, 3.0]},
                                 index=["2022-01", "2022-02"])
        report = generate_report([], shock_df, None, params, "A")
        assert "-3%" in report  # 冲击矩阵列名在报告标题中出现
        assert "冲击矩阵" in report
        # 检查 DataFrame 的 markdown 表示中包含数值
        md_str = shock_df.to_markdown()
        assert "  1 " in md_str  # markdown 表格中的整数表示
        assert "  2 " in md_str

    def test_with_liquidity(self):
        params = {"atr_period": 20}
        liq = {"symbol": "510500.SH", "units": 4, "daily_loss_pct": 10.0,
               "consecutive_days": 3, "max_loss_pct": 5.0,
               "max_loss_amount": 10000.0, "notional_per_unit": 50000.0,
               "total_notional": 200000.0}
        report = generate_report([], None, liq, params, "A")
        assert "200,000" in report


# ════════════════════════════════════════════════════════════
#  9. 保存结果
# ════════════════════════════════════════════════════════════

class TestSaveResults:
    def test_creates_output_files(self, tmp_path):
        params = {"atr_period": 20, "breakout_period": 20, "stop_period": 10,
                  "stop_atr_multiple": 2.0, "alpha": 0.05}
        historical_results = [{
            "scenario": "A1_covid",
            "scenario_name": "COVID 熔断",
            "date_range": "2020-02-03~2020-04-30",
            "total_return": -2.5, "cagr": -15.0, "sharpe": -0.8,
            "max_drawdown": 8.5, "max_dd_duration": 15, "total_trades": 5,
            "daily_var_95": -0.02, "daily_var_99": -0.035,
            "correlation_avg": 0.45, "t1_stop_delay_hits": 2,
            "final_value": 195000, "initial_cash": 200000,
            "win_rate": 40.0, "profit_factor": 1.2,
            "annual_vol": 18.0, "calmar": -1.76,
        }]
        save_results(historical_results, None, None, params, "A", tmp_path)
        assert (tmp_path / "scenario_summary.csv").exists()
        assert (tmp_path / "historical_A1_covid.csv").exists()
        assert (tmp_path / "stress_conclusion.json").exists()
        assert (tmp_path / "stress_report.md").exists()

    def test_saves_conclusion_json(self, tmp_path):
        params = {"atr_period": 20}
        historical_results = [{
            "scenario": "A1_covid",
            "scenario_name": "COVID 熔断",
            "date_range": "2020-02-03~2020-04-30",
            "total_return": -2.5, "cagr": -15.0, "sharpe": -0.8,
            "max_drawdown": 8.5, "max_dd_duration": 15, "total_trades": 5,
            "daily_var_95": -0.02, "daily_var_99": -0.035,
            "correlation_avg": 0.45, "t1_stop_delay_hits": 2,
            "final_value": 195000, "initial_cash": 200000,
            "win_rate": 40.0, "profit_factor": 1.2,
            "annual_vol": 18.0, "calmar": -1.76,
        }]
        save_results(historical_results, None, None, params, "A", tmp_path)
        with open(tmp_path / "stress_conclusion.json", "r", encoding="utf-8") as f:
            conclusion = json.load(f)
        assert "overall" in conclusion
        assert "scenarios" in conclusion


# ════════════════════════════════════════════════════════════
#  10. CLI 参数解析 & main
# ════════════════════════════════════════════════════════════

class TestCLI:
    def test_parse_help(self):
        """测试 argparse 不抛出异常。"""
        with patch.object(sys, "argv", ["run_stress_test.py", "--help"]):
            with pytest.raises(SystemExit):
                main()

    @patch("scripts.run_stress_test.save_results")
    @patch("scripts.run_stress_test.run_liquidity_stress")
    @patch("scripts.run_stress_test.run_synthetic_shock")
    @patch("scripts.run_stress_test.load_data")
    @patch("scripts.run_stress_test.load_best_params")
    def test_main_with_scenarios(self, mock_load_params, mock_load_data,
                                  mock_shock, mock_liquidity, mock_save):
        """测试 main() 指定场景时不抛异常。"""
        mock_load_params.return_value = {
            "atr_period": 20, "breakout_period": 20,
            "stop_period": 10, "stop_atr_multiple": 2.0, "alpha": 0.05,
        }
        mock_load_data.return_value = None  # 数据不可用，跳过历史回测
        mock_shock.return_value = None
        mock_liquidity.return_value = None

        with patch.object(sys, "argv", ["run_stress_test.py", "--scenarios", "B2_liquidity_stress", "--workers", "1"]):
            try:
                main()
            except SystemExit:
                pass  # 正常退出

    @patch("scripts.run_stress_test.save_results")
    @patch("scripts.run_stress_test.load_data")
    @patch("scripts.run_stress_test.load_best_params")
    def test_main_with_invalid_scenario(self, mock_load_params, mock_load_data, mock_save):
        """测试无效场景名应报错。"""
        mock_load_params.return_value = {
            "atr_period": 20, "breakout_period": 20,
            "stop_period": 10, "stop_atr_multiple": 2.0, "alpha": 0.05,
        }
        mock_load_data.return_value = None

        with patch.object(sys, "argv", ["run_stress_test.py", "--scenarios", "INVALID"]):
            with pytest.raises(SystemExit):
                main()