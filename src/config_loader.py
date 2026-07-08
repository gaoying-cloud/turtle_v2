"""
配置加载器 — 从 turtle_config.yaml 动态推导品种列表 (V5.3)

消除所有代码中的硬编码品种列表，统一从配置读取。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Set

import yaml


def load_config(path: str | Path = "config/turtle_config.yaml") -> Dict[str, Any]:
    """加载 YAML 配置文件。"""
    p = Path(__file__).resolve().parent.parent / path
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_trading_symbols(config: Dict[str, Any]) -> List[str]:
    """获取交易标的所有 code 列表。"""
    return [s["code"] for s in config.get("symbols", [])]


def get_symbol_names(config: Dict[str, Any]) -> Dict[str, str]:
    """获取品种 code → 中文名称映射（消除硬编码）。"""
    return {s["code"]: s["name"] for s in config.get("symbols", [])}


def get_bond_symbol(config: Dict[str, Any]) -> str:
    """获取国债 code。"""
    return config.get("bond", {}).get("code", "")


def get_all_symbols(config: Dict[str, Any]) -> List[str]:
    """交易标的 + 国债。"""
    return get_trading_symbols(config) + [get_bond_symbol(config)]


def get_shortable_symbols(config: Dict[str, Any]) -> Set[str]:
    """可做空品种（shortable=True）。"""
    return {s["code"] for s in config.get("symbols", []) if s.get("shortable", False)}


def get_t_plus_one_symbols(config: Dict[str, Any]) -> Set[str]:
    """T+1 品种（t_plus_one=True）。"""
    return {s["code"] for s in config.get("symbols", []) if s.get("t_plus_one", False)}


def get_t0_symbols(config: Dict[str, Any]) -> List[str]:
    """T+0 品种（t_plus_one=False, 即非 T+1）。"""
    return [s["code"] for s in config.get("symbols", []) if not s.get("t_plus_one", False)]


def get_futures_symbols(config: Dict[str, Any]) -> List[str]:
    """期货品种 code 列表（从 futures.futures_list 读取）。

    字段命名为 ts_code（Tushare 术语），本函数返回纯 code 字符串列表。
    """
    return [s["ts_code"] for s in config.get("futures", {}).get("futures_list", [])]


def get_futures_list(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """期货品种完整列表（含 name/exchange/category/multiplier 等元信息）。
    供 pull_futures.py 等需要品种详情的脚本使用。
    """
    return config.get("futures", {}).get("futures_list", [])


def get_futures_multipliers(config: Dict[str, Any]) -> Dict[str, int]:
    """期货合约乘数映射 {ts_code: multiplier}，供 run_backtest 计算手数价值使用。"""
    return {
        s["ts_code"]: s.get("multiplier", 1)
        for s in config.get("futures", {}).get("futures_list", [])
    }
