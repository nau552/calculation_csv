# Copyright (c) 2026
"""テストが使う自作スコアパーツ関数の例。

ユーザがリポジトリ直下の custom_parts.py として SVN に登録するのと同じ形。
"""

import polars as pl

from scorelib_param.custom import CustomContext


def fixed_value(ctx: CustomContext) -> float:
    """常に 42.0 を返す最小のパーツ関数。"""
    return 42.0


def mean_fbc_plus_offset(ctx: CustomContext) -> float:
    """Csv を直接読む。

    データディレクトリ内の任意のファイルを自由に集計できるのが
    custom パーツの存在意義そのもの。
    """
    df = pl.read_csv(ctx.data_dir / "FBC.csv")
    return float(df["FBC"].mean()) + float(ctx.params.get("offset", 0))


def broken_returns_nan(ctx: CustomContext) -> float:
    """NaN を返す壊れたパーツ関数(有限値検査のテスト用)。"""
    return float("nan")


def _private_helper(ctx: CustomContext) -> float:  # must NOT appear in the function list
    return 0.0
