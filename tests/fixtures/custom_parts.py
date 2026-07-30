# Copyright (c) 2026
"""テストが使う自作スコアパーツ関数の例。

ユーザがリポジトリ直下の custom_parts.py として SVN に登録するのと同じ形。
"""

from typing import cast

import polars as pl

from scorelib_param.custom import CustomContext


def fixed_value(ctx: CustomContext) -> float:
    """常に 42.0 を返す最小のパーツ関数。

    Returns:
        データに依存しない定数 42.0。

    """
    return 42.0


def mean_fbc_plus_offset(ctx: CustomContext) -> float:
    """Csv を直接読む。

    データディレクトリ内の任意のファイルを自由に集計できるのが
    custom パーツの存在意義そのもの。

    Returns:
        FBC.csv の FBC 列の平均に params["offset"](既定 0)を足した値。

    """
    df = pl.read_csv(ctx.data_dir / "FBC.csv")
    return float(cast("float", df["FBC"].mean())) + float(ctx.params.get("offset", 0))


def broken_returns_nan(ctx: CustomContext) -> float:
    """NaN を返す壊れたパーツ関数(有限値検査のテスト用)。

    Returns:
        常に非有限値 float("nan")。

    """
    return float("nan")


def _private_helper(ctx: CustomContext) -> float:  # must NOT appear in the function list
    return 0.0
