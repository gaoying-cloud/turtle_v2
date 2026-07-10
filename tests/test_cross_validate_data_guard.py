"""
单元测试：scripts/cross_validate.py 数据有效性防护

核心回归目标：本地 Tushare 缓存 close 全 NaN 时，交叉校验必须报 CRITICAL
而非静默通过。这是 V5.19 bug 的防护 —— 之前 NaN 收益率行被 continue 跳过，
导致坏数据 0 error 通过校验。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

# ── 将项目根加入 sys.path ──
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.cross_validate as cv  # noqa: E402


@pytest.fixture(autouse=True)
def patch_data_dir(tmp_path):
    """将 cross_validate 的 DATA_DIR 重定向到临时目录。"""
    original = cv.DATA_DIR
    cv.DATA_DIR = tmp_path / "etf_daily"
    cv.DATA_DIR.mkdir(parents=True, exist_ok=True)
    yield
    cv.DATA_DIR = original


def _write_tushare_cache(symbol: str, df: pd.DataFrame):
    """写一个带后缀的 Tushare parquet 缓存。"""
    suffix = ".SH" if symbol.startswith(("5", "6")) else ".SZ"
    df.to_parquet(cv.DATA_DIR / f"{symbol}{suffix}.parquet", index=False)


# ════════════════════════════════════════════════════════════
#  load_tushare_parquet 数据防护
# ════════════════════════════════════════════════════════════
class TestLoadTushareParquetGuard:
    def test_all_nan_close_raises_data_error(self):
        """close 100% NaN → DataError（而非静默返回坏数据）。"""
        df = pd.DataFrame({
            "date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
            "open": [float("nan")] * 3, "high": [float("nan")] * 3,
            "low": [float("nan")] * 3, "close": [float("nan")] * 3,
            "volume": [1e6, 1.1e6, 1.2e6],
        })
        _write_tushare_cache("510500", df)

        with pytest.raises(cv.DataError, match="NaN"):
            cv.load_tushare_parquet("510500")

    def test_partial_nan_above_threshold_raises_data_error(self):
        """close NaN 占比 > 10% 阈值 → DataError。"""
        dates = pd.date_range("2024-01-02", periods=10, freq="B")
        df = pd.DataFrame({
            "date": dates, "open": 5.0, "high": 5.1, "low": 4.9,
            "close": 5.0, "volume": 1e6,
        })
        df.loc[1:3, "close"] = float("nan")  # 3/10 = 30% > 10%
        _write_tushare_cache("510500", df)

        with pytest.raises(cv.DataError, match="30"):
            cv.load_tushare_parquet("510500")

    def test_empty_parquet_raises_data_error(self):
        """空 parquet → DataError。"""
        _write_tushare_cache("510500", pd.DataFrame())

        with pytest.raises(cv.DataError, match="为空"):
            cv.load_tushare_parquet("510500")

    def test_clean_data_loads_successfully(self):
        """正常数据 → 不抛异常，返回 DataFrame。"""
        dates = pd.date_range("2024-01-02", periods=5, freq="B")
        df = pd.DataFrame({
            "date": dates, "open": 5.0, "high": 5.1, "low": 4.9,
            "close": [5.0, 5.1, 5.2, 5.1, 5.3], "volume": 1e6,
        })
        _write_tushare_cache("510500", df)

        result = cv.load_tushare_parquet("510500")
        assert len(result) == 5
        assert not result["close"].isna().any()


# ════════════════════════════════════════════════════════════
#  compare_return_based 不再静默跳过 NaN 行
# ════════════════════════════════════════════════════════════
class TestCompareReturnBasedNaN:
    def test_nan_ret_diff_flagged_as_error(self):
        """非首行 ret_diff 为 NaN → 标记 ERROR(价格缺失)，而非静默跳过。"""
        tf_df = pd.DataFrame({
            "date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
            "close": [5.0, 5.1, 5.2], "volume": [1e6, 1.1e6, 1.2e6],
        })
        ts_df = pd.DataFrame({
            "date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
            "close": [5.0, float("nan"), float("nan")],
            "volume": [1e6, 1.1e6, 1.2e6],
        })

        diffs = cv.compare_return_based(tf_df, ts_df, "510500")
        # 首行(01-02)跳过；01-03、01-04 应为 ERROR
        error_diffs = [d for d in diffs if d.level == "error"]
        assert len(error_diffs) == 2, "NaN 行应标记为 ERROR 而非静默跳过"
        for d in error_diffs:
            assert "价格缺失" in d.reason

    def test_first_row_skipped(self):
        """首行无前日收益率，正常跳过（不应产生 diff）。"""
        tf_df = pd.DataFrame({
            "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "close": [5.0, 5.1], "volume": [1e6, 1.1e6],
        })
        ts_df = pd.DataFrame({
            "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "close": [5.0, 5.1], "volume": [1e6, 1.1e6],
        })

        diffs = cv.compare_return_based(tf_df, ts_df, "510500")
        # 只有 01-03 一条 diff（01-02 首行跳过）
        assert len(diffs) == 1
        assert diffs[0].level == "ok"

    def test_normal_match_produces_ok(self):
        """两边收益率一致 → 全部 ok。"""
        dates = pd.date_range("2024-01-02", periods=5, freq="B")
        closes = [5.0, 5.1, 5.2, 5.1, 5.3]
        tf_df = pd.DataFrame({"date": dates, "close": closes, "volume": 1e6})
        ts_df = pd.DataFrame({"date": dates, "close": closes, "volume": 1e6})

        diffs = cv.compare_return_based(tf_df, ts_df, "510500")
        assert len(diffs) == 4  # 首行跳过
        assert all(d.level == "ok" for d in diffs)


# ════════════════════════════════════════════════════════════
#  validate_symbol 对 DataError 生成 CRITICAL 报告
# ════════════════════════════════════════════════════════════
class TestValidateSymbolDataError:
    def test_data_error_produces_critical_block_all(self):
        """缓存数据损坏 → validate_symbol 返回 CRITICAL + block_all_trading=True。"""
        # 写入全 NaN close 缓存
        df = pd.DataFrame({
            "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "open": [float("nan")] * 2, "high": [float("nan")] * 2,
            "low": [float("nan")] * 2, "close": [float("nan")] * 2,
            "volume": [1e6, 1.1e6],
        })
        _write_tushare_cache("510500", df)

        # TickFlow 会被调用——mock 掉，避免网络依赖
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(cv, "fetch_tickflow_daily", lambda *a, **k: pd.DataFrame({
                "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
                "open": [5.0, 5.1], "high": [5.1, 5.2],
                "low": [4.9, 5.0], "close": [5.0, 5.1], "volume": [1e6, 1.1e6],
            }))
            report = cv.validate_symbol("510500")

        assert report.critical_days == 1
        assert report.block_all_trading is True
        assert "损坏" in report.worst_detail or "NaN" in report.worst_detail
