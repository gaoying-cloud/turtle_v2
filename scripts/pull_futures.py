#!/usr/bin/env python
"""
海龟策略期货版 · 数据拉取

从 Tushare Pro 拉取期货主力连续日线数据，缓存为 Parquet 文件。

用法：
    py scripts/pull_futures.py                          # 全量拉取
    py scripts/pull_futures.py --symbol I.DCE           # 单个品种
    py scripts/pull_futures.py --start 2014-01-01       # 指定起始日期
    py scripts/pull_futures.py --end 2024-12-31         # 指定截止日期
    py scripts/pull_futures.py --force                  # 强制重新拉取
    py scripts/pull_futures.py --status                 # 查看本地缓存状态
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from time import sleep
from typing import Optional

import pandas as pd
import yaml

# 确保 src/ 在 sys.path 中
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)

# ── 路径常量 ──
CONFIG_PATH = ROOT / "config" / "turtle_config.yaml"
DATA_DIR = ROOT / "data" / "futures_daily"

# 标准化后的 Parquet 列名（与 ETF 版本一致）
STD_COLUMNS = [
    "date", "open", "high", "low", "close", "volume", "amount",
]

# ════════════════════════════════════════════════════════════
#  期货品种列表（从 config/turtle_config.yaml 读取，单一来源）
# ════════════════════════════════════════════════════════════
from src.config_loader import get_futures_list

with open(CONFIG_PATH, "r", encoding="utf-8") as _f:
    _CONFIG = yaml.safe_load(_f)
FUTURES_SYMBOLS = get_futures_list(_CONFIG)


# ════════════════════════════════════════════════════════════
#  数据拉取（通过 Tushare MCP）
# ════════════════════════════════════════════════════════════

def _fetch_from_tushare(
    code: str,
    start_date: str,
    end_date: str,
    max_retries: int = 3,
) -> pd.DataFrame:
    """从 Tushare fut_daily 接口拉取期货主力连续数据。

    使用 tushare Python SDK 的 pro_api().fut_daily()。

    Parameters
    ----------
    code : str
        TS 代码 (如 "CU.SHF")。
    start_date : str
        起始日期 "YYYYMMDD"。
    end_date : str
        截止日期 "YYYYMMDD"。
    max_retries : int
        每次请求的最大重试次数。

    Returns
    -------
    pd.DataFrame
        列: ts_code, trade_date, open, high, low, close, vol, amount
    """
    import tushare as ts
    from os import environ

    token = environ.get("TUSHARE_TOKEN")
    if not token:
        raise ValueError("TUSHARE_TOKEN 环境变量未设置")
    ts.set_token(token)
    pro = ts.pro_api()

    for attempt in range(1, max_retries + 1):
        try:
            df = pro.fut_daily(
                ts_code=code,
                start_date=start_date,
                end_date=end_date,
                fields="ts_code,trade_date,open,high,low,close,vol,amount",
            )
            if df is None or df.empty:
                logger.warning("[%s] %s~%s 无数据返回", code, start_date, end_date)
                return pd.DataFrame()

            # 处理分页
            all_dfs = [df]
            while len(df) >= 5000:
                df = pro.fut_daily(
                    ts_code=code,
                    start_date=start_date,
                    end_date=end_date,
                    fields="ts_code,trade_date,open,high,low,close,vol,amount",
                    offset=len(pd.concat(all_dfs, ignore_index=True)),
                )
                if df is not None and not df.empty:
                    all_dfs.append(df)
                else:
                    break

            result = pd.concat(all_dfs, ignore_index=True)
            logger.info("[%s] 拉取 %s~%s 共 %d 条", code, start_date, end_date, len(result))
            return result

        except Exception as e:
            logger.warning("[%s] 第 %d/%d 次请求失败: %s", code, attempt, max_retries, e)
            if attempt < max_retries:
                sleep(attempt * 2)
            else:
                logger.error("[%s] 已耗尽重试次数", code)
                return pd.DataFrame()

    return pd.DataFrame()


# ════════════════════════════════════════════════════════════
#  数据清洗与标准化
# ════════════════════════════════════════════════════════════

def _clean_and_standardize(raw: pd.DataFrame) -> pd.DataFrame:
    """将 Tushare fut_daily 原始数据标准化为统一 Schema。

    标准化列：
        date (datetime64), open, high, low, close, volume, amount

    Parameters
    ----------
    raw : pd.DataFrame
        Tushare fut_daily 返回的原始数据。

    Returns
    -------
    pd.DataFrame
        清洗后的 DataFrame，按 date 升序排序，重复日期已去重。
    """
    if raw.empty:
        return raw

    df = raw.copy()

    # 列名标准化
    df.rename(columns={"trade_date": "date", "vol": "volume"}, inplace=True)

    # 日期列: str -> datetime
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")

    # 数值列类型安全转换
    numeric_cols = ["open", "high", "low", "close", "volume", "amount"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # amount 单位：万元 -> 元
    if "amount" in df.columns:
        df["amount"] = df["amount"] * 10000

    # volume 单位：手（回测中期货用"手"为单位，与 ETF 不同）
    # 保留原始手数，不上乘

    # 选择最终列
    keep_cols = [c for c in STD_COLUMNS if c in df.columns]
    df = df[keep_cols]

    # 排序
    df.sort_values("date", inplace=True)

    # 去重
    df.drop_duplicates(subset="date", keep="last", inplace=True)

    # 重置索引
    df.reset_index(drop=True, inplace=True)

    return df


# ════════════════════════════════════════════════════════════
#  本地 Parquet 缓存
# ════════════════════════════════════════════════════════════

def _parquet_path(code: str) -> Path:
    """获取指定品种的 Parquet 缓存路径。"""
    return DATA_DIR / f"{code}.parquet"


def _read_local_cache(code: str) -> pd.DataFrame:
    """读取本地 Parquet 缓存，不存在则返回空 DataFrame。"""
    path = _parquet_path(code)
    if path.exists():
        return pd.read_parquet(path)
    return pd.DataFrame()


def _save_to_parquet(df: pd.DataFrame, code: str):
    """将 DataFrame 写入 Parquet 文件（增量更新）。"""
    existing = _read_local_cache(code)
    if not existing.empty:
        df = pd.concat([existing, df], ignore_index=True)
        df.sort_values("date", inplace=True)
        df.drop_duplicates(subset="date", keep="last", inplace=True)
        df.reset_index(drop=True, inplace=True)

    path = _parquet_path(code)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False, compression="snappy")
    logger.info("[%s] 缓存已写入: %s (%d 行)", code, path, len(df))


# ════════════════════════════════════════════════════════════
#  核心拉取逻辑
# ════════════════════════════════════════════════════════════

def fetch_single(
    code: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    force: bool = False,
) -> pd.DataFrame:
    """拉取并缓存单个期货品种的日线数据。

    Parameters
    ----------
    code : str
        TS 代码 (如 "CU.SHF")。
    start_date : str, optional
        起始日期 "YYYY-MM-DD"。默认 2015-01-01。
    end_date : str, optional
        截止日期 "YYYY-MM-DD"。默认 today。
    force : bool
        强制重新拉取全部数据。

    Returns
    -------
    pd.DataFrame
        清洗后的完整数据。
    """
    if start_date is None:
        start_date = "2015-01-01"
    if end_date is None:
        end_date = date.today().isoformat()

    start_norm = start_date.replace("-", "")
    end_norm = end_date.replace("-", "")

    if not force:
        local_df = _read_local_cache(code)
        if not local_df.empty:
            local_min = local_df["date"].min().strftime("%Y%m%d")
            local_max = local_df["date"].max().strftime("%Y%m%d")

            if local_min <= start_norm and local_max >= end_norm:
                mask = (local_df["date"] >= start_date) & (local_df["date"] <= end_date)
                logger.info("[%s] 缓存已覆盖 %s~%s，直接返回", code, start_date, end_date)
                return local_df[mask].reset_index(drop=True)

            pull_start = local_max[:8]
            logger.info("[%s] 缓存最新 %s，增量拉取 %s~%s", code, local_max, pull_start, end_norm)
            raw = _fetch_from_tushare(code, pull_start, end_norm)
        else:
            raw = _fetch_from_tushare(code, start_norm, end_norm)
    else:
        raw = _fetch_from_tushare(code, start_norm, end_norm)

    if raw.empty:
        cached = _read_local_cache(code)
        if not cached.empty:
            mask = (cached["date"] >= start_date) & (cached["date"] <= end_date)
            return cached[mask].reset_index(drop=True)
        return pd.DataFrame()

    cleaned = _clean_and_standardize(raw)
    _save_to_parquet(cleaned, code)

    result = _read_local_cache(code)
    if not result.empty:
        mask = (result["date"] >= start_date) & (result["date"] <= end_date)
        return result[mask].reset_index(drop=True)
    return result


def pull_all(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    force: bool = False,
) -> dict[str, pd.DataFrame]:
    """拉取全部期货品种的日线数据。

    Returns
    -------
    dict[str, pd.DataFrame]
        {code: df} 映射。
    """
    results = {}
    for sym in FUTURES_SYMBOLS:
        code = sym["ts_code"]
        logger.info("===== 开始拉取: %s (%s) =====", sym["name"], code)
        try:
            df = fetch_single(code, start_date, end_date, force)
            results[code] = df
            logger.info(
                "[%s] 完成: %d 行, %s ~ %s",
                code, len(df),
                df["date"].min().date() if not df.empty else "N/A",
                df["date"].max().date() if not df.empty else "N/A",
            )
        except Exception as e:
            logger.error("[%s] 拉取失败: %s", code, e)
            results[code] = pd.DataFrame()
    return results


def check_status() -> pd.DataFrame:
    """检查所有期货品种的本地缓存状态。"""
    records = []
    for sym in FUTURES_SYMBOLS:
        code = sym["ts_code"]
        df = _read_local_cache(code)
        if df.empty:
            records.append({
                "code": code,
                "name": sym["name"],
                "category": sym["category"],
                "exchange": sym["exchange"],
                "earliest": "—",
                "latest": "—",
                "rows": 0,
            })
        else:
            records.append({
                "code": code,
                "name": sym["name"],
                "category": sym["category"],
                "exchange": sym["exchange"],
                "earliest": df["date"].min().strftime("%Y-%m-%d"),
                "latest": df["date"].max().strftime("%Y-%m-%d"),
                "rows": len(df),
            })
    return pd.DataFrame(records)


# ════════════════════════════════════════════════════════════
#  CLI 入口
# ════════════════════════════════════════════════════════════

def setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(message)s" if verbose else "%(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")


def cmd_status():
    df = check_status()
    if df.empty:
        print("本地缓存为空。请运行 py scripts/pull_futures.py 拉取数据。")
        return
    print("\n期货数据缓存状态")
    print("=" * 90)
    for _, row in df.iterrows():
        print(f"{row['code']:<12} {row['name']:<6} {row['category']:<8} "
              f"{row['earliest']:<12} ~ {row['latest']:<12} {int(row['rows']):>6} 行")
    print("=" * 90)


def main():
    parser = argparse.ArgumentParser(description="期货数据拉取")
    parser.add_argument("--symbol", type=str, default=None, help="单个品种代码 (如 CU.SHF)")
    parser.add_argument("--start", type=str, default=None, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", type=str, default=None, help="截止日期 YYYY-MM-DD")
    parser.add_argument("--force", action="store_true", default=False, help="强制重新拉取")
    parser.add_argument("--status", action="store_true", default=False, help="查看本地缓存状态")
    parser.add_argument("--verbose", action="store_true", default=False, help="详细日志")
    args = parser.parse_args()

    setup_logging(args.verbose)

    if args.status:
        cmd_status()
        return

    if args.symbol:
        df = fetch_single(args.symbol, args.start, args.end, args.force)
        print(f"\n{args.symbol}: {len(df)} 行")
    else:
        pull_all(args.start, args.end, args.force)


if __name__ == "__main__":
    main()
