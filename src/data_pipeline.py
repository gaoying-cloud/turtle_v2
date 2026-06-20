"""
跨市场ETF海龟组合策略 · 数据管道 (S1)

从 Tushare Pro 拉取 ETF 日线数据，清洗后缓存为 Parquet 文件。

文件结构：
    data/etf_daily/{code}.parquet  (每个品种独立文件)

依赖：
    - tushare>=1.4.0
    - pandas>=2.0.0
    - pyarrow>=12.0.0
    - pyyaml>=6.0

环境变量：
    TUSHARE_TOKEN  — Tushare Pro API token
"""

from __future__ import annotations

import os
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from time import sleep

import pandas as pd
import yaml

logger = logging.getLogger(__name__)

# ── 路径常量 ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "turtle_config.yaml"
DATA_DIR = PROJECT_ROOT / "data" / "etf_daily"

# Tushare fund_daily 原始字段
TUSHARE_FIELDS = [
    "ts_code", "trade_date",
    "open", "high", "low", "close", "pre_close",
    "change", "pct_chg", "vol", "amount",
]

# 标准化后的 Parquet 列名（用于回测）
STD_COLUMNS = [
    "date", "open", "high", "low", "close", "volume", "amount", "pre_close",
]


# ════════════════════════════════════════════════════════════
#  配置加载
# ════════════════════════════════════════════════════════════

def _load_config() -> dict:
    """加载 turtle_config.yaml 配置。"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_symbols(include_bond: bool = True) -> list[dict]:
    """从配置读取交易品种列表。

    Parameters
    ----------
    include_bond : bool
        是否包含国债ETF (511010.SH)。

    Returns
    -------
    list[dict]
        每个元素含 code、name、market 字段。
    """
    config = _load_config()
    symbols = list(config["symbols"])
    if include_bond:
        symbols.append(config["bond"])
    return symbols


# ════════════════════════════════════════════════════════════
#  Tushare 接口层
# ════════════════════════════════════════════════════════════

def _get_tushare_token() -> str:
    """从环境变量获取 Tushare token，不存在则抛出异常。"""
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        raise ValueError(
            "Tushare token 未设置。请通过环境变量 TUSHARE_TOKEN 配置。"
        )
    return token


def _create_tushare_pro():
    """创建 Tushare Pro API 实例。"""
    import tushare as ts
    ts.set_token(_get_tushare_token())
    return ts.pro_api()


def _fetch_from_tushare(
    code: str,
    start_date: str,
    end_date: str,
    max_retries: int = 3,
) -> pd.DataFrame:
    """从 Tushare fund_daily 接口拉取数据，含自动分页与重试。

    Parameters
    ----------
    code : str
        TS 代码 (如 "510500.SH")。
    start_date : str
        起始日期 "YYYYMMDD"。
    end_date : str
        截止日期 "YYYYMMDD"。
    max_retries : int
        每次请求的最大重试次数。

    Returns
    -------
    pd.DataFrame
        Tushare 原始返回的 DataFrame（字段见 TUSHARE_FIELDS）。
        失败时返回空 DataFrame。
    """
    pro = _create_tushare_pro()

    for attempt in range(1, max_retries + 1):
        try:
            df = pro.fund_daily(
                ts_code=code,
                start_date=start_date,
                end_date=end_date,
                fields=",".join(TUSHARE_FIELDS),
            )
            if df is None or df.empty:
                logger.warning(
                    "[%s] %s~%s 无数据返回", code, start_date, end_date
                )
                return pd.DataFrame()

            # fund_daily 可能分页，自动处理 offset
            all_dfs = [df]
            while len(df) >= 5000:
                df = pro.fund_daily(
                    ts_code=code,
                    start_date=start_date,
                    end_date=end_date,
                    fields=",".join(TUSHARE_FIELDS),
                    offset=len(pd.concat(all_dfs, ignore_index=True)),
                )
                if df is not None and not df.empty:
                    all_dfs.append(df)
                else:
                    break

            result = pd.concat(all_dfs, ignore_index=True)
            logger.info(
                "[%s] 拉取 %s~%s 共 %d 条",
                code, start_date, end_date, len(result),
            )
            return result

        except Exception as e:
            logger.warning(
                "[%s] 第 %d/%d 次请求失败: %s",
                code, attempt, max_retries, e,
            )
            if attempt < max_retries:
                sleep(attempt * 2)  # 指数退避: 2s, 4s, 6s
            else:
                logger.error("[%s] 已耗尽重试次数，跳过", code)
                return pd.DataFrame()

    return pd.DataFrame()


# ════════════════════════════════════════════════════════════
#  数据清洗与标准化
# ════════════════════════════════════════════════════════════

def _clean_and_standardize(raw: pd.DataFrame) -> pd.DataFrame:
    """将 Tushare fund_daily 原始数据标准化为统一 Schema。

    标准化列：
        date (datetime64), open, high, low, close, volume, amount, pre_close

    Parameters
    ----------
    raw : pd.DataFrame
        Tushare fund_daily 返回的原始数据。

    Returns
    -------
    pd.DataFrame
        清洗后的 DataFrame，按 date 升序排序，重复日期已去重。
    """
    if raw.empty:
        return raw

    df = raw.copy()

    # 列名标准化
    df.rename(columns={"trade_date": "date"}, inplace=True)

    # 日期列: str->datetime
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")

    # 数值列类型安全转换
    numeric_cols = ["open", "high", "low", "close", "pre_close", "vol", "amount"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 列名统一：vol -> volume，并转换单位（手→股）
    df.rename(columns={"vol": "volume"}, inplace=True)
    if "volume" in df.columns:
        df["volume"] = df["volume"] * 100  # 手 → 股

    # amount 单位转换（千元→元）
    if "amount" in df.columns:
        df["amount"] = df["amount"] * 1000  # 千元 → 元

    # 选择最终列
    keep_cols = [c for c in STD_COLUMNS if c in df.columns]
    df = df[keep_cols]

    # 排序
    df.sort_values("date", inplace=True)

    # 去重（同一天只保留最后一条）
    df.drop_duplicates(subset="date", keep="last", inplace=True)

    # 重置索引
    df.reset_index(drop=True, inplace=True)

    return df


def _adjust_backward(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """后复权处理：优先使用 Tushare fund_adj 官方因子，不可用时用价格检测。

    后复权以最新日期为基准，等比缩放历史 OHLC，使价格连续可比的。
    """
    adj_df = _fetch_adj_factors(code)
    if not adj_df.empty:
        return _apply_factor_adjustment(df, adj_df)
    return _detect_and_adjust_splits(df)


def _fetch_adj_factors(code: str) -> pd.DataFrame:
    """从 Tushare fund_adj 拉取复权因子。"""
    try:
        import tushare as ts
        import os
        token = os.environ.get("TUSHARE_TOKEN")
        if not token:
            return pd.DataFrame()
        ts.set_token(token)
        pro = ts.pro_api()
        adj = pro.fund_adj(ts_code=code)
        if adj is None or adj.empty:
            return pd.DataFrame()
        adj = adj.rename(columns={"trade_date": "date"})
        adj["date"] = pd.to_datetime(adj["date"], format="%Y%m%d")
        adj = adj.sort_values("date").reset_index(drop=True)
        logger.info("[%s] 拉取复权因子 %d 条", code, len(adj))
        return adj
    except Exception as e:
        logger.warning("[%s] 拉取复权因子失败: %s，使用价格检测法", code, e)
        return pd.DataFrame()


def _apply_factor_adjustment(df: pd.DataFrame, adj_df: pd.DataFrame) -> pd.DataFrame:
    """用官方复权因子做后复权：最新日因子为基准，等比缩放历史价格。"""
    if adj_df.empty or df.empty:
        return df

    df = df.sort_values("date").reset_index(drop=True)
    latest_factor = adj_df["adj_factor"].iloc[-1]
    adj_map = dict(zip(adj_df["date"], adj_df["adj_factor"]))

    price_cols = ["open", "high", "low", "close", "pre_close"]
    for idx, row in df.iterrows():
        d = row["date"]
        factor = adj_map.get(d)
        if factor is None or factor <= 0:
            continue
        ratio = latest_factor / factor
        if abs(ratio - 1) < 0.001:
            continue
        for col in price_cols:
            if col in df.columns and pd.notna(row[col]):
                df.at[idx, col] = round(float(row[col]) * ratio, 4)

    return df


def _detect_and_adjust_splits(df: pd.DataFrame) -> pd.DataFrame:
    """检测并修正基金拆分/合并事件（后复权）。

    通过对比 pre_close[t] 与 close[t-1] 的比值来检测。
    正常日比值接近 1.0，拆分日会大幅偏离。
    将所有事件前的价格乘以累积调整因子，使历史与当前在同一基准。
    """
    df = df.sort_values("date").reset_index(drop=True)

    events = []  # [(date_index, factor)]
    for i in range(1, len(df)):
        prev_close = df.loc[i - 1, "close"]
        curr_pre = df.loc[i, "pre_close"]
        if prev_close <= 0:
            continue
        ratio = curr_pre / prev_close
        if abs(ratio - 1) > 0.15:
            events.append((i, ratio))

    if not events:
        return df

    cum_factor = 1.0
    for _, factor in events:
        cum_factor *= factor

    first_idx = events[0][0] - 1  # 第一个事件的前一天
    if first_idx <= 0:
        return df

    price_cols = ["open", "high", "low", "close", "pre_close"]
    for col in price_cols:
        if col in df.columns:
            df.loc[:first_idx, col] = df.loc[:first_idx, col].astype(float) * cum_factor

    logger.info("[复权] %s: %d 个事件, 累积因子 %.4f, 调整 %d 行",
                df.get("ts_code", df.iloc[0].get("date")), len(events),
                cum_factor, first_idx + 1)
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
    """将 DataFrame 写入 Parquet 文件。

    若本地已有缓存，则合并后再写入（增量更新）。
    """
    # 读取现有缓存
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
    """拉取并缓存单个品种的日线数据。

    流程：
        1. 检查本地缓存，确定已有数据的时间范围
        2. 计算需要补拉的时间窗口
        3. 从 Tushare 拉取缺失区间
        4. 清洗后合并写入 Parquet

    Parameters
    ----------
    code : str
        TS 代码 (如 "510500.SH")。
    start_date : str, optional
        起始日期 "YYYY-MM-DD"。默认使用回测配置的 start_date。
    end_date : str, optional
        截止日期 "YYYY-MM-DD"。默认使用 today。
    force : bool
        强制重新拉取全部数据（覆盖缓存）。

    Returns
    -------
    pd.DataFrame
        清洗后的完整数据。
    """
    # ── 确定拉取区间 ──
    config = _load_config()
    bt = config["backtest"]
    if start_date is None:
        start_date = bt["start_date"]
    if end_date is None:
        end_date = date.today().isoformat()

    start_norm = start_date.replace("-", "")
    end_norm = end_date.replace("-", "")

    # ── 检查本地缓存 ──
    if not force:
        local_df = _read_local_cache(code)
        if not local_df.empty:
            local_min = local_df["date"].min().strftime("%Y%m%d")
            local_max = local_df["date"].max().strftime("%Y%m%d")

            # 如果缓存已覆盖请求区间，且数据完整，直接返回
            if local_min <= start_norm and local_max >= end_norm:
                # 裁剪到请求区间
                mask = (local_df["date"] >= start_date) & (
                    local_df["date"] <= end_date
                )
                logger.info(
                    "[%s] 缓存数据已覆盖请求区间 %s~%s，直接返回",
                    code, start_date, end_date,
                )
                return local_df[mask].reset_index(drop=True)

            # 增量: 只拉取缓存未覆盖的部分
            pull_start = local_max[:8]
            logger.info(
                "[%s] 缓存最新日期 %s，增量拉取 %s~%s",
                code, local_max, pull_start, end_norm,
            )
            raw = _fetch_from_tushare(code, pull_start, end_norm)
        else:
            raw = _fetch_from_tushare(code, start_norm, end_norm)
    else:
        raw = _fetch_from_tushare(code, start_norm, end_norm)

    # ── 清理 + 缓存 ──
    if raw.empty:
        # 返回已有缓存（如果有）
        cached = _read_local_cache(code)
        if not cached.empty:
            mask = (cached["date"] >= start_date) & (cached["date"] <= end_date)
            return cached[mask].reset_index(drop=True)
        return pd.DataFrame()

    cleaned = _clean_and_standardize(raw)
    cleaned = _adjust_backward(cleaned, code)
    _save_to_parquet(cleaned, code)

    # ── 返回请求区间数据 ──
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
    """拉取全部品种（6 海龟品种 + 国债ETF）的日线数据。

    Parameters
    ----------
    start_date, end_date, force :
        同 fetch_single。

    Returns
    -------
    dict[str, pd.DataFrame]
        {code: df} 映射。
    """
    symbols = get_symbols(include_bond=True)
    results = {}
    for sym in symbols:
        code = sym["code"]
        logger.info("===== 开始拉取: %s (%s) =====", sym["name"], code)
        try:
            df = fetch_single(code, start_date, end_date, force)
            results[code] = df
            logger.info(
                "[%s] 完成: %d 行, %s ~ %s",
                code,
                len(df),
                df["date"].min().date() if not df.empty else "N/A",
                df["date"].max().date() if not df.empty else "N/A",
            )
        except Exception as e:
            logger.error("[%s] 拉取失败: %s", code, e)
            results[code] = pd.DataFrame()
    return results


# ════════════════════════════════════════════════════════════
#  数据可用性检查
# ════════════════════════════════════════════════════════════

def check_status() -> pd.DataFrame:
    """检查所有品种的本地缓存状态。

    Returns
    -------
    pd.DataFrame
        列: code, name, market, earliest, latest, rows
    """
    symbols = get_symbols(include_bond=True)
    records = []
    for sym in symbols:
        code = sym["code"]
        df = _read_local_cache(code)
        if df.empty:
            records.append({
                "code": code,
                "name": sym["name"],
                "market": sym.get("market", "债券"),
                "earliest": "—",
                "latest": "—",
                "rows": 0,
            })
        else:
            records.append({
                "code": code,
                "name": sym["name"],
                "market": sym.get("market", "债券"),
                "earliest": df["date"].min().strftime("%Y-%m-%d"),
                "latest": df["date"].max().strftime("%Y-%m-%d"),
                "rows": len(df),
            })
    return pd.DataFrame(records)


# ════════════════════════════════════════════════════════════
#  指数日线数据（用于大盘基准对比）
# ════════════════════════════════════════════════════════════

INDEX_DIR = PROJECT_ROOT / "data" / "index_daily"

INDEX_FIELDS = [
    "ts_code", "trade_date",
    "open", "high", "low", "close", "pre_close",
    "change", "pct_chg", "vol", "amount",
]


def _index_cache_path(code: str) -> Path:
    """指数 parquet 缓存路径。"""
    return INDEX_DIR / f"{code}.parquet"


def fetch_index_daily(
    code: str,
    start_date: str,
    end_date: str,
    max_retries: int = 3,
) -> pd.DataFrame:
    """从 Tushare index_daily 接口拉取指数日线，存入本地缓存。

    Parameters
    ----------
    code : str
        TS 指数代码，如 '000300.SH'。
    start_date : str
        起始日期 "YYYYMMDD"。
    end_date : str
        截止日期 "YYYYMMDD"。
    max_retries : int
        每次请求最大重试次数。

    Returns
    -------
    pd.DataFrame
        标准化后的日线数据，列为 date, open, high, low, close, volume。
        失败或无 token 时返回空 DataFrame。
    """
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _index_cache_path(code)

    # 若缓存已覆盖区间则直接返回
    if cache_path.exists():
        cached = pd.read_parquet(cache_path)
        if not cached.empty:
            cached_min = cached["date"].min().strftime("%Y%m%d")
            cached_max = cached["date"].max().strftime("%Y%m%d")
            if cached_min <= start_date and cached_max >= end_date:
                mask = (cached["date"] >= start_date) & (cached["date"] <= end_date)
                return cached[mask].reset_index(drop=True)

    # 尝试从 Tushare 拉取
    try:
        import tushare as ts
        token = os.environ.get("TUSHARE_TOKEN")
        if not token:
            logger.warning("TUSHARE_TOKEN 未设置，跳过指数数据拉取")
            return _read_existing_index(code, start_date, end_date)

        ts.set_token(token)
        pro = ts.pro_api()

        for attempt in range(1, max_retries + 1):
            try:
                df = pro.index_daily(
                    ts_code=code,
                    start_date=start_date,
                    end_date=end_date,
                    fields=",".join(INDEX_FIELDS),
                )
                if df is None or df.empty:
                    logger.warning("[指数 %s] %s~%s 无数据返回", code, start_date, end_date)
                    return _read_existing_index(code, start_date, end_date)

                # 清洗
                df.rename(columns={"trade_date": "date"}, inplace=True)
                df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
                for col in ["open", "high", "low", "close", "pre_close", "vol", "amount"]:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")

                df.sort_values("date", inplace=True)
                df.drop_duplicates(subset="date", keep="last", inplace=True)
                df.reset_index(drop=True, inplace=True)

                # 合并缓存
                if cache_path.exists():
                    existing = pd.read_parquet(cache_path)
                    df = pd.concat([existing, df], ignore_index=True)
                    df.sort_values("date", inplace=True)
                    df.drop_duplicates(subset="date", keep="last", inplace=True)
                    df.reset_index(drop=True, inplace=True)

                df.to_parquet(cache_path, index=False, compression="snappy")
                logger.info("[指数 %s] 已缓存 %d 行", code, len(df))

                mask = (df["date"] >= start_date) & (df["date"] <= end_date)
                return df[mask].reset_index(drop=True)

            except Exception as e:
                logger.warning("[指数 %s] 第 %d/%d 次请求失败: %s", code, attempt, max_retries, e)
                if attempt < max_retries:
                    from time import sleep
                    sleep(attempt * 2)

        return _read_existing_index(code, start_date, end_date)

    except ImportError:
        logger.warning("tushare 未安装，无法拉取指数数据")
        return _read_existing_index(code, start_date, end_date)


def _read_existing_index(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """从本地缓存读取指数数据（降级路径）。"""
    cache_path = _index_cache_path(code)
    if cache_path.exists():
        df = pd.read_parquet(cache_path)
        mask = (df["date"] >= start_date) & (df["date"] <= end_date)
        return df[mask].reset_index(drop=True)
    return pd.DataFrame()