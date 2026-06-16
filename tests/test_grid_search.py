"""
参数网格搜索 (S6) — 单元测试

覆盖：
    - build_param_grid() 笛卡尔积展开
    - run_single_backtest() 烟雾测试
    - 结果 schema 校验
    - CSV 读写一致性
    - 稳健性评分计算
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

from scripts.run_grid_search import (
    build_param_grid,
    PARAM_GRID,
    MODES,
    evaluate_results,
    _robust_scaler,
    _save_best_params_json,
)


# ════════════════════════════════════════════════════════════
#  build_param_grid
# ════════════════════════════════════════════════════════════

class TestBuildParamGrid:
    """验证笛卡尔积展开的正确性。"""

    def test_grid_size(self):
        grid = build_param_grid()
        expected = (
            len(PARAM_GRID["atr_period"])
            * len(PARAM_GRID["breakout_period"])
            * len(PARAM_GRID["stop_period"])
            * len(PARAM_GRID["stop_atr_multiple"])
            * len(PARAM_GRID["alpha"])
        )
        assert len(grid) == expected, f"期望 {expected} 组，实际 {len(grid)}"

    def test_each_entry_has_all_keys(self):
        grid = build_param_grid()
        required_keys = {"atr_period", "breakout_period", "stop_period",
                         "stop_atr_multiple", "alpha"}
        for entry in grid:
            assert set(entry.keys()) == required_keys, f"缺少键: {set(required_keys) - set(entry.keys())}"

    def test_each_entry_values_in_range(self):
        grid = build_param_grid()
        for entry in grid:
            assert entry["atr_period"] in PARAM_GRID["atr_period"]
            assert entry["breakout_period"] in PARAM_GRID["breakout_period"]
            assert entry["stop_period"] in PARAM_GRID["stop_period"]
            assert entry["stop_atr_multiple"] in PARAM_GRID["stop_atr_multiple"]
            assert entry["alpha"] in PARAM_GRID["alpha"]

    def test_all_combinations_unique(self):
        grid = build_param_grid()
        tuples = {tuple(sorted(e.items())) for e in grid}
        assert len(tuples) == len(grid), "存在重复组合"

    def test_grid_values_match_design_doc(self):
        """参数范围与设计文档 §5.5 保持一致。"""
        assert PARAM_GRID["atr_period"] == [15, 20, 25]
        assert PARAM_GRID["breakout_period"] == [15, 20, 25]
        assert PARAM_GRID["stop_period"] == [8, 10, 12]
        assert PARAM_GRID["stop_atr_multiple"] == [1.5, 2.0, 2.5]
        # α 包含用户指定的 5 个值
        assert PARAM_GRID["alpha"] == [0, 0.05, 0.10, 0.15, 0.20]
        assert len(PARAM_GRID["alpha"]) == 5


# ════════════════════════════════════════════════════════════
#  evaluate_results
# ════════════════════════════════════════════════════════════

class TestEvaluateResults:
    """验证稳健性评分和最优参数选择逻辑。"""

    def test_returns_top_n(self):
        # 构造测试数据
        rows = []
        for i in range(20):
            for m in ["A", "B"]:
                rows.append({
                    "run_id": i,
                    "mode": m,
                    "atr_period": 15 + (i % 3) * 5,
                    "breakout_period": 15 + (i % 3) * 5,
                    "stop_period": 8 + (i % 3) * 2,
                    "stop_atr_multiple": 1.5 + (i % 3) * 0.5,
                    "alpha": 0.05 * (i % 5),
                    "total_return": 30 + np.random.uniform(-5, 5),
                    "cagr": 8 + np.random.uniform(-2, 2),
                    "sharpe": 0.8 + np.random.uniform(-0.3, 0.3),
                    "max_drawdown": 15 + np.random.uniform(-3, 3),
                    "win_rate": 40 + np.random.uniform(-5, 5),
                    "profit_factor": 1.5 + np.random.uniform(-0.3, 0.3),
                    "total_trades": 100 + int(np.random.uniform(-20, 20)),
                    "annual_vol": 12 + np.random.uniform(-2, 2),
                    "calmar": 0.5 + np.random.uniform(-0.2, 0.2),
                    "final_value": 250000 + np.random.uniform(-30000, 30000),
                })
        df = pd.DataFrame(rows)

        df_best = evaluate_results(df, top_n=5)
        assert len(df_best) <= 10  # 2 modes × 5 = 10 max
        assert "robustness_score" in df_best.columns
        assert df_best["robustness_score"].is_monotonic_increasing is False  # 降序

    def test_handles_empty_df(self):
        df = pd.DataFrame()
        result = evaluate_results(df)
        assert isinstance(result, pd.DataFrame)
        assert result.empty

    def test_handles_all_nan_sharpe(self):
        df = pd.DataFrame({
            "run_id": [1, 2],
            "mode": ["A", "B"],
            "atr_period": [15, 20],
            "breakout_period": [15, 20],
            "stop_period": [8, 10],
            "stop_atr_multiple": [1.5, 2.0],
            "alpha": [0, 0.05],
            "total_return": [10.0, 20.0],
            "cagr": [3.0, 6.0],
            "sharpe": [None, None],
            "max_drawdown": [15.0, 20.0],
            "win_rate": [40.0, 50.0],
            "profit_factor": [1.2, 1.5],
            "total_trades": [50, 80],
            "annual_vol": [10.0, 12.0],
            "calmar": [0.2, 0.3],
            "final_value": [210000, 220000],
        })
        result = evaluate_results(df)
        assert not result.empty  # 回退到收益率排序


# ════════════════════════════════════════════════════════════
#  _robust_scaler
# ════════════════════════════════════════════════════════════

class TestRobustScaler:
    """验证稳健标准化函数。"""

    def test_standard_case(self):
        arr = pd.Series([1, 2, 3, 4, 5])
        scaled = _robust_scaler(arr)
        assert abs(scaled.median()) < 1e-10  # 中位数 ≈ 0
        assert scaled.std() > 0

    def test_constant_series_returns_zero(self):
        arr = pd.Series([3, 3, 3, 3])
        scaled = _robust_scaler(arr)
        assert (scaled == 0).all()

    def test_single_element(self):
        arr = pd.Series([42])
        scaled = _robust_scaler(arr)
        assert scaled.iloc[0] == 0


# ════════════════════════════════════════════════════════════
#  _save_best_params_json
# ════════════════════════════════════════════════════════════

class TestSaveBestParamsJson:
    """验证 JSON 保存和读写一致性。"""

    def test_roundtrip(self):
        df = pd.DataFrame({
            "mode": ["A", "B"],
            "atr_period": [20, 15],
            "breakout_period": [20, 15],
            "stop_period": [10, 8],
            "stop_atr_multiple": [2.0, 1.5],
            "alpha": [0.05, 0.10],
            "sharpe": [0.9, 0.7],
            "cagr": [12.0, 8.0],
            "max_drawdown": [18.0, 22.0],
            "total_trades": [120, 80],
            "robustness_score": [1.5, 0.8],
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "best_params.json"
            _save_best_params_json(df, path)

            assert path.exists()
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            assert len(data) == 2
            assert data[0]["mode"] == "A"
            assert data[0]["atr_period"] == 20
            assert data[0]["alpha"] == 0.05
            assert data[1]["mode"] == "B"


# ════════════════════════════════════════════════════════════
#  smoke tests（可选，仅在数据缓存存在时运行）
# ════════════════════════════════════════════════════════════

class TestRunSingleBacktestSmoke:
    """冒烟测试：使用默认参数跑一次回测。

    需要 Parquet 缓存存在，否则跳过。
    """

    @pytest.fixture(scope="class")
    def data_exists(self):
        data_dir = ROOT / "data" / "etf_daily"
        parquet_files = list(data_dir.glob("*.parquet"))
        return len(parquet_files) >= 6

    def test_smoke_with_default_params(self, data_exists):
        if not data_exists:
            pytest.skip("数据缓存不足，跳过冒烟测试")

        from scripts.run_grid_search import run_single_backtest

        params = {
            "atr_period": 20,
            "breakout_period": 20,
            "stop_period": 10,
            "stop_atr_multiple": 2.0,
            "alpha": 0.05,
        }
        result = run_single_backtest(params, "A", "2023-01-01", "2024-01-01", run_id=999)

        assert result is not None
        assert result["run_id"] == 999
        assert result["mode"] == "A"
        assert result["atr_period"] == 20
        assert result["total_trades"] >= 0
        assert result["final_value"] > 0
        assert result["cagr"] is not None

    def test_result_schema(self, data_exists):
        """验证返回 dict 包含所有期望的 key 且类型正确。"""
        if not data_exists:
            pytest.skip("数据缓存不足，跳过 schema 测试")

        from scripts.run_grid_search import run_single_backtest

        params = {
            "atr_period": 20,
            "breakout_period": 20,
            "stop_period": 10,
            "stop_atr_multiple": 2.0,
            "alpha": 0.05,
        }
        result = run_single_backtest(params, "B", "2023-01-01", "2024-01-01", run_id=100)

        expected_keys = {
            "run_id", "mode", "atr_period", "breakout_period",
            "stop_period", "stop_atr_multiple", "alpha",
            "total_return", "cagr", "sharpe", "max_drawdown",
            "win_rate", "profit_factor", "total_trades",
            "annual_vol", "calmar", "final_value", "date_range",
        }
        assert set(result.keys()) == expected_keys, f"缺失 key: {expected_keys - set(result.keys())}"

        # 类型检查
        assert isinstance(result["run_id"], int)
        assert isinstance(result["mode"], str)
        assert isinstance(result["atr_period"], int)
        assert isinstance(result["total_trades"], int)
        assert isinstance(result["final_value"], float)
        assert result["sharpe"] is None or isinstance(result["sharpe"], float)
        assert isinstance(result["cagr"], float)

    def test_bad_params_returns_none(self):
        """非法参数应返回 None（而不是崩溃）。"""
        from scripts.run_grid_search import run_single_backtest

        # 缺少必要参数
        result = run_single_backtest(
            {"atr_period": 0, "breakout_period": 20, "stop_period": 10, "stop_atr_multiple": 2.0},
            "A", "2020-01-01", "2021-01-01",
        )
        assert result is None


# ════════════════════════════════════════════════════════════
#  modes 常量
# ════════════════════════════════════════════════════════════

class TestModes:
    def test_modes_are_a_and_b(self):
        assert MODES == ["A", "B"]
        assert len(MODES) == 2