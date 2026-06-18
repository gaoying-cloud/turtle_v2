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
    """T+0 品种（shortable=True, 即纳指+黄金）。"""
    return [s["code"] for s in config.get("symbols", []) if s.get("shortable", False)]


def get_futures_symbols(config: Dict[str, Any]) -> List[str]:
    """期货品种列表（从 futures.futures_list 读取，兼容旧 SIX_SYMBOLS 模式）。"""
    # 为兼容 run_backtest 中 FUTURES_SYMBOLS，直接从配置或默认返回
    # 当前期货品种尚未加入 config，保留硬编码默认值供期货模式用
    return [
        "CU.SHF", "RB.SHF", "RU.SHF", "M.DCE", "Y.DCE",
        "P.DCE", "JM.DCE", "I.DCE", "CF.ZCE", "TA.ZCE",
        "MA.ZCE", "FG.ZCE",
    ]