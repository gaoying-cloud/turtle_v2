"""
单元测试：scripts/verify_adjustment.py

核心回归目标：确保前复权损坏（close 全 NaN）时脚本报 FAIL 而非 ok。
这是 V5.19 bug 的直接防护 —— 之前全 NaN 缓存被误判为"完美连续"。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

# ── 将项目根加入 sys.path ──
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.verify_adjustment as va  # noqa: E402


@pytest.fixture(autouse=True)
def patch_data_dir(tmp_path):
    """将 verify_adjustment 的 DATA_DIR 重定向到临时目录，避免污染真实数据。"""
    original = va.DATA_DIR
    va.DATA_DIR = tmp_path / "etf_daily"
    va.DATA_DIR.mkdir(parents=True, exist_ok=True)
    yield
    va.DATA_DIR = original


def _write_cache(code: str, df: pd.DataFrame):
    df.to_parquet(va.DATA_DIR / f"{code}.parquet", index=False)


def _normal_df() -> pd.DataFrame:
    """正常前复权 close 序列，连续无跳空。"""
    return pd.DataFrame({
        "date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
        "open": [5.50, 5.55, 5.48], "high": [5.58, 5.60, 5.52],
        "low": [5.45, 5.50, 5.42], "close": [5.52, 5.53, 5.46],
        "volume": [1e6, 1.2e6, 9.5e5], "amount": [5e7, 6e7, 5e7],
        "pre_close": [5.48, 5.52, 5.53], "adj_factor": [1.0, 1.0, 1.0],
    })


# ════════════════════════════════════════════════════════════
#  核心回归：全 NaN close 必须报 FAIL（旧 bug 让它伪装成 ok）
# ════════════════════════════════════════════════════════════
class TestNaNPriceDetection:
    def test_all_nan_close_reports_fail(self):
        """close 100% NaN（前复权索引错位 bug 的典型后果）必须 FAIL。"""
        df = _normal_df()
        df[["open", "high", "low", "close"]] = float("nan")
        df["adj_factor"] = float("nan")
        _write_cache("510500.SH", df)

        r = va.check_symbol("510500.SH")
        assert r["status"] == "FAIL(NaN价格)"
        assert "close=3(100%)" in r["detail"]

    def test_partial_nan_close_reports_fail(self):
        """close 部分NaN（>10%阈值）也应 FAIL。"""
        df = _normal_df()
        df.loc[1:2, "close"] = float("nan")  # 2/3 = 67% NaN
        _write_cache("510500.SH", df)

        r = va.check_symbol("510500.SH")
        assert r["status"] == "FAIL(NaN价格)"

    def test_few_nan_close_reports_warn(self):
        """close 少量 NaN（≤10%阈值）报 WARN 而非静默 ok。"""
        df = _normal_df()
        df.loc[1, "close"] = float("nan")  # 1/3 ≈ 33%
        # 降到 1/10 以测试 WARN 路径
        dates = pd.date_range("2024-01-02", periods=10, freq="B")
        big = pd.DataFrame({
            "date": dates, "open": 5.0, "high": 5.1, "low": 4.9,
            "close": 5.0, "volume": 1e6, "amount": 5e7,
            "pre_close": 5.0, "adj_factor": 1.0,
        })
        big.loc[1, "close"] = float("nan")  # 1/10 = 10%，边界
        _write_cache("510500.SH", big)

        r = va.check_symbol("510500.SH")
        assert r["status"] in ("WARN(NaN价格)", "FAIL(NaN价格)")


# ════════════════════════════════════════════════════════════
#  正常 & 跳空场景
# ════════════════════════════════════════════════════════════
class TestNormalAndGap:
    def test_normal_continuous_close_reports_ok(self):
        """正常连续 close，无跳空，adj_factor 末值=1.0 → ok。"""
        _write_cache("510500.SH", _normal_df())

        r = va.check_symbol("510500.SH")
        assert r["status"] == "ok"
        assert "latest_ratio=1.0000" in r["detail"]

    def test_large_gap_reports_fail(self):
        """>50% 单日跳空 → FAIL(残留跳空)。"""
        df = _normal_df()
        df.loc[2, "close"] = 2.0  # 5.53 → 2.0 ≈ -64%
        _write_cache("510500.SH", df)

        r = va.check_symbol("510500.SH")
        assert r["status"] == "FAIL(残留跳空)"


# ════════════════════════════════════════════════════════════
#  adj_factor 异常 & 缺失场景
# ════════════════════════════════════════════════════════════
class TestAdjFactorAndMissing:
    def test_nan_adj_factor_latest_reports_fail(self):
        """adj_factor 末值为 NaN → FAIL(adj_factor异常)。"""
        df = _normal_df()
        df["adj_factor"] = [1.0, 1.0, float("nan")]
        _write_cache("510500.SH", df)

        r = va.check_symbol("510500.SH")
        assert r["status"] == "FAIL(adj_factor异常)"

    def test_missing_file_reports_missing(self):
        """缓存文件不存在 → status=missing。"""
        r = va.check_symbol("NONEXIST.SH")
        assert r["status"] == "missing"

    def test_empty_parquet_reports_empty(self):
        """空 parquet → status=empty。"""
        _write_cache("510500.SH", pd.DataFrame())
        r = va.check_symbol("510500.SH")
        assert r["status"] == "empty"


# ════════════════════════════════════════════════════════════
#  动态品种列表
# ════════════════════════════════════════════════════════════
class TestSymbolList:
    def test_main_uses_config_symbols(self, capsys):
        """main() 应从配置读取品种（7个），而非硬编码 5 个。"""
        # 写入一个全 NaN 缓存，确保 main 会 exit 1（FAIL）
        df = _normal_df()
        df[["open", "high", "low", "close"]] = float("nan")
        df["adj_factor"] = float("nan")
        _write_cache("510500.SH", df)

        with pytest.raises(SystemExit) as exc_info:
            va.main()
        captured = capsys.readouterr()

        assert exc_info.value.code == 1  # FAIL → exit 1
        # 必须出现配置里的全部 7 个品种代码（含 513520.SH、159985.SZ）
        for code in ["510500.SH", "159915.SZ", "513100.SH",
                     "518880.SH", "159985.SZ", "513520.SH", "511010.SH"]:
            assert code in captured.out, f"{code} 未出现在输出中"
