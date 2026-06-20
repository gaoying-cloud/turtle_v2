#!/usr/bin/env python
"""
跨市场ETF海龟组合策略 · 数据拉取 CLI (S1)

用法：
    py scripts/pull_data.py                          # 全量拉取全部品种
    py scripts/pull_data.py --symbol 510500.SH       # 单个品种
    py scripts/pull_data.py --start 2024-01-01        # 指定起始日期
    py scripts/pull_data.py --end 2024-12-31          # 指定截止日期
    py scripts/pull_data.py --force                   # 强制重新拉取
    py scripts/pull_data.py --status                  # 查看本地缓存状态
    py scripts/pull_data.py --verbose                 # 详细日志
"""

from __future__ import annotations

import argparse
import logging
import sys

from pathlib import Path

# 确保 src/ 在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_pipeline import (
    fetch_single,
    pull_all,
    check_status,
    get_symbols,
)


def setup_logging(verbose: bool):
    """配置日志输出。"""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(message)s" if verbose else "%(message)s"
    logging.basicConfig(
        level=level,
        format=fmt,
        datefmt="%H:%M:%S",
    )


def cmd_status():
    """--status: 展示本地缓存状态表格。"""
    df = check_status()
    if df.empty:
        print("本地缓存为空。请运行 py scripts/pull_data.py 拉取数据。")
        return

    # 终端友好输出
    print()
    print(f"{'品种代码':<14} {'名称':<10} {'市场':<6} {'最早日期':<12} {'最晚日期':<12} {'行数':<6}")
    print("-" * 60)
    for _, row in df.iterrows():
        print(
            f"{row['code']:<14} "
            f"{row['name']:<10} "
            f"{row['market']:<6} "
            f"{row['earliest']:<12} "
            f"{row['latest']:<12} "
            f"{row['rows']:<6}"
        )
    print()


def cmd_fetch(args):
    """拉取数据。"""
    if args.symbol:
        # 验证品种代码
        valid_codes = {s["code"] for s in get_symbols(include_bond=True)}
        if args.symbol not in valid_codes:
            print(
                f"错误: 未知品种代码 '{args.symbol}'。"
                f"支持: {', '.join(sorted(valid_codes))}"
            )
            sys.exit(1)

        print(f"拉取 {args.symbol} ...")
        df = fetch_single(
            code=args.symbol,
            start_date=args.start,
            end_date=args.end,
            force=args.force,
        )
        if df.empty:
            print(f"  {args.symbol}: 无数据")
        else:
            print(
                f"  {args.symbol}: {len(df)} 行, "
                f"{df['date'].min().date()} ~ {df['date'].max().date()}"
            )
    else:
        print("===== 全量拉取全部品种 =====")
        results = pull_all(
            start_date=args.start,
            end_date=args.end,
            force=args.force,
        )
        success = sum(1 for df in results.values() if not df.empty)
        total = len(results)
        print(f"===== 完成: {success}/{total} 个品种成功 =====")


def main():
    parser = argparse.ArgumentParser(
        description="跨市场ETF海龟组合策略 — 数据拉取工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  py scripts/pull_data.py                         全量拉取
  py scripts/pull_data.py --symbol 510500.SH      拉取中证500
  py scripts/pull_data.py --force                 强制重拉全部
  py scripts/pull_data.py --status                查看缓存状态
  py scripts/pull_data.py --verbose               调试模式
        """,
    )

    parser.add_argument(
        "--symbol", "-s",
        type=str,
        default=None,
        help="品种代码 (如 510500.SH)。不指定则拉取全部 7 只 ETF。",
    )
    parser.add_argument(
        "--start",
        type=str,
        default=None,
        help="起始日期 YYYY-MM-DD (默认: config 中的 backtest.start_date)",
    )
    parser.add_argument(
        "--end",
        type=str,
        default=None,
        help="截止日期 YYYY-MM-DD (默认: 今天)",
    )
    parser.add_argument(
        "--force", "-f",
        action="store_true",
        default=False,
        help="强制重新拉取，覆盖本地缓存",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        default=False,
        help="查看本地缓存状态（不拉取数据）",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="详细日志输出 (DEBUG 级别)",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    if args.status:
        cmd_status()
    else:
        cmd_fetch(args)


if __name__ == "__main__":
    main()
