"""
单元测试：src/data_pipeline.py (S1)

覆盖范围：
    - 配置加载与品种列表
    - 数据清洗与标准化 (_clean_and_standardize)
    - Parquet 缓存读写
    - 增量更新逻辑
    - 数据可用性检查

注意：涉及 Tushare 网络请求的测试通过 mock 隔离。
"""

from __future__ import annotations

import os
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest
import yaml

# ── 将 src/ 加入 sys.path ──
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_pipeline import (
    _load_config,
    get_symbols,
    _clean_and_standardize,
    _parquet_path,
    _read_local_cache,
    _save_to_parquet,
    fetch_single,
    check_status,
    DATA_DIR,
    STD_COLUMNS,
)


# ════════════════════════════════════════════════════════════
#  Fixtures
# ════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def patch_data_dir(tmp_path):
    """将所有 Parquet 读写重定向到临时目录，避免污染真实数据。"""
    original = DATA_DIR
    import src.data_pipeline as dp
    dp.DATA_DIR = tmp_path / "etf_daily"
    dp.DATA_DIR.mkdir(parents=True, exist_ok=True)
    yield
    dp.DATA_DIR = original


@pytest.fixture
def sample_raw_data() -> pd.DataFrame:
    """模拟 Tushare fund_daily 返回的原始数据。"""
    return pd.DataFrame({
        "ts_code": ["510500.SH", "510500.SH", "510500.SH"],
        "trade_date": ["20240102", "20240103", "20240104"],
        "open": [5.50, 5.55, 5.48],
        "high": [5.58, 5.60, 5.52],
        "low": [5.45, 5.50, 5.42],
        "close": [5.52, 5.53, 5.46],
        "pre_close": [5.48, 5.52, 5.53],
        "change": [0.04, 0.01, -0.07],
        "pct_chg": [0.73, 0.18, -1.27],
        "vol": [10000.0, 12000.0, 9500.0],
        "amount": [55200.0, 66360.0, 51870.0],
    })


# ════════════════════════════════════════════════════════════
#  配置与品种列表
# ════════════════════════════════════════════════════════════

class TestConfig:
    def test_load_config_returns_dict(self):
        config = _load_config()
        assert isinstance(config, dict)
        assert "symbols" in config
        assert "bond" in config
        assert "turtle" in config

    def test_get_symbols_six_plus_bond(self):
        symbols = get_symbols(include_bond=True)
        assert len(symbols) == 5  # 4 交易标的 + 1 国债
        codes = [s["code"] for s in symbols]
        assert "511010.SH" in codes  # 国债ETF
        assert "510500.SH" in codes

    def test_get_symbols_without_bond(self):
        symbols = get_symbols(include_bond=False)
        assert len(symbols) == 4  # 4 交易标的
        codes = [s["code"] for s in symbols]
        assert "511010.SH" not in codes


# ════════════════════════════════════════════════════════════
#  数据清洗
# ════════════════════════════════════════════════════════════

class TestCleanAndStandardize:
    def test_empty_input_returns_empty(self):
        result = _clean_and_standardize(pd.DataFrame())
        assert result.empty

    def test_column_names_and_types(self, sample_raw_data):
        df = _clean_and_standardize(sample_raw_data)
        # 列名（adj_factor 由 _adjust_backward 后续添加）
        expected = [c for c in STD_COLUMNS if c != "adj_factor"]
        assert list(df.columns) == expected
        # 类型
        assert df["date"].dtype in ("datetime64[ns]", "datetime64[us]")
        assert df["open"].dtype == "float64"
        assert df["volume"].dtype == "float64"

    def test_volume_converted_to_shares(self, sample_raw_data):
        """vol 从 手 转为 股 (×100)。"""
        df = _clean_and_standardize(sample_raw_data)
        # 原始 vol = [10000, 12000, 9500] (手)
        # 转换后 volume = [1_000_000, 1_200_000, 950_000] (股)
        assert df["volume"].iloc[0] == 1_000_000.0
        assert df["volume"].iloc[1] == 1_200_000.0
        assert df["volume"].iloc[2] == 950_000.0

    def test_amount_converted_to_yuan(self, sample_raw_data):
        """amount 从 千元 转为 元 (×1000)。"""
        df = _clean_and_standardize(sample_raw_data)
        assert df["amount"].iloc[0] == 55_200_000.0  # 55200 * 1000

    def test_sorted_and_deduplicated(self, sample_raw_data):
        """按 date 升序，重复日期去重。"""
        # 添加一个重复日期
        dup = sample_raw_data.iloc[[0]].copy()
        dup["trade_date"] = "20240103"
        dup["close"] = 5.54
        raw = pd.concat([sample_raw_data, dup], ignore_index=True)

        df = _clean_and_standardize(raw)
        # 按 date 升序
        assert df["date"].is_monotonic_increasing
        # 2024-01-03 应该只有一条（keep="last" 保留 5.54）
        jan3 = df[df["date"] == "2024-01-03"]
        assert len(jan3) == 1
        assert jan3["close"].iloc[0] == 5.54

    def test_pre_close_preserved(self, sample_raw_data):
        df = _clean_and_standardize(sample_raw_data)
        assert "pre_close" in df.columns
        assert df["pre_close"].iloc[0] == 5.48


# ════════════════════════════════════════════════════════════
#  Parquet 缓存
# ════════════════════════════════════════════════════════════

class TestParquetCache:
    def test_parquet_path_format(self):
        path = _parquet_path("510500.SH")
        assert path.name == "510500.SH.parquet"
        assert path.suffix == ".parquet"

    def test_read_empty_cache_returns_empty(self):
        df = _read_local_cache("NONEXISTENT.SH")
        assert df.empty

    def test_save_and_read_back(self, sample_raw_data):
        """写入后能正确读取。"""
        cleaned = _clean_and_standardize(sample_raw_data)
        _save_to_parquet(cleaned, "510500.SH")

        loaded = _read_local_cache("510500.SH")
        assert len(loaded) == 3
        assert loaded["date"].iloc[0] == pd.Timestamp("2024-01-02")
        assert loaded["close"].iloc[-1] == 5.46

    def test_incremental_update(self, sample_raw_data):
        """增量写入：新数据与缓存合并去重。"""
        # 先写入前两行
        df1 = _clean_and_standardize(sample_raw_data.iloc[:2])
        _save_to_parquet(df1, "510500.SH")

        # 再写入全部三行（第三行为新增，前两行重复）
        df2 = _clean_and_standardize(sample_raw_data)
        _save_to_parquet(df2, "510500.SH")

        # 最终应该有 3 条（无重复）
        loaded = _read_local_cache("510500.SH")
        assert len(loaded) == 3


# ════════════════════════════════════════════════════════════
#  拉取逻辑（mock Tushare）
# ════════════════════════════════════════════════════════════

class TestFetchSingle:
    def test_cached_returns_without_network(self, sample_raw_data):
        """如果缓存已覆盖请求区间，不应调用 Tushare。"""
        cleaned = _clean_and_standardize(sample_raw_data)
        _save_to_parquet(cleaned, "510500.SH")

        # mock Tushare——如果 fetch_single 走了缓存路径，不会调用它
        with patch("src.data_pipeline._fetch_from_tushare") as mock_fetch:
            df = fetch_single("510500.SH", start_date="2024-01-02", end_date="2024-01-04")
            mock_fetch.assert_not_called()
            assert len(df) == 3

    def test_force_fetch_calls_tushare(self, sample_raw_data):
        """--force 即使有缓存也应调用 Tushare。"""
        cleaned = _clean_and_standardize(sample_raw_data)
        _save_to_parquet(cleaned, "510500.SH")

        with patch("src.data_pipeline._fetch_from_tushare") as mock_fetch:
            mock_fetch.return_value = sample_raw_data
            df = fetch_single("510500.SH", start_date="2024-01-02", end_date="2024-01-04", force=True)
            mock_fetch.assert_called_once()

    def test_partial_cache_triggers_incremental(self, sample_raw_data):
        """缓存只覆盖部分区间时，应增量拉取。"""
        # 只缓存前两行
        df_partial = _clean_and_standardize(sample_raw_data.iloc[:2])
        _save_to_parquet(df_partial, "510500.SH")

        with patch("src.data_pipeline._fetch_from_tushare") as mock_fetch:
            # 返回第三行数据
            new_row = sample_raw_data.iloc[[2]].copy()
            mock_fetch.return_value = new_row

            with patch("src.data_pipeline._adjust_forward") as mock_adj:
                mock_adj.side_effect = lambda df, code: df
                df = fetch_single("510500.SH", start_date="2024-01-02", end_date="2024-01-04")
                mock_fetch.assert_called_once()
                assert len(df) == 3  # 合并后应有 3 行

    def test_fetch_failure_returns_empty(self):
        """Tushare 请求失败时返回空 DataFrame。"""
        with patch("src.data_pipeline._fetch_from_tushare") as mock_fetch:
            mock_fetch.return_value = pd.DataFrame()
            df = fetch_single("UNKNOWN.XSX")
            assert df.empty

    def test_cache_not_in_region_returns_empty(self):
        """缓存数据不在请求区间内时，返回空。"""
        df_old = pd.DataFrame({
            "date": pd.to_datetime(["2020-01-02", "2020-01-03"]),
            "open": [5.0, 5.1],
            "high": [5.2, 5.3],
            "low": [4.9, 5.0],
            "close": [5.1, 5.2],
            "volume": [1e6, 1.2e6],
            "amount": [5.1e7, 6.24e7],
            "pre_close": [4.95, 5.1],
        })
        _save_to_parquet(df_old, "510500.SH")

        with patch("src.data_pipeline._fetch_from_tushare") as mock_fetch:
            mock_fetch.return_value = pd.DataFrame()
            df = fetch_single("510500.SH", start_date="2025-01-01", end_date="2025-01-10")
            assert df.empty


# ════════════════════════════════════════════════════════════
#  数据可用性检查
# ════════════════════════════════════════════════════════════

class TestCheckStatus:
    def test_status_returns_dataframe(self, sample_raw_data):
        """写入一条数据后，status 能正确展示。"""
        cleaned = _clean_and_standardize(sample_raw_data)
        _save_to_parquet(cleaned, "510500.SH")

        status = check_status()
        assert isinstance(status, pd.DataFrame)
        assert len(status) == 5  # 4 交易标的 + 1 国债

        # 只有 510500.SH 有数据
        row = status[status["code"] == "510500.SH"].iloc[0]
        assert row["rows"] == 3
        assert row["earliest"] == "2024-01-02"
        assert row["latest"] == "2024-01-04"

    def test_status_all_empty_returns_zero_rows(self):
        status = check_status()
        assert all(status["rows"] == 0)
