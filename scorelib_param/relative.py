# Copyright (c) 2026
"""相対値(分子/分母)計算。

split_axis(読み込み系なら Read_Override、書き込み系なら Program_Override
想定 — docs/score_gui_design.md 11節参照)の値で、各行を「評価側=分子」と
「基準側=分母」に振り分ける。分母は比を取る前に一部の軸(例: WL, STR)で
先に集計しておくこともできる(denominator_pre_aggregation)。

denominator_offset は比の**両辺**に加算する((分子+o)/(分母+o))。
分母側はゼロ割防止のため、分子側は「相対値がちょうど0」だと後段の
dVtBudget の log10 が発散するため(評価FBCが0になるのは日常的に起きる)。
確認事項として docs/score_gui_design.md 11節に記録あり。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import polars as pl

from .aggregate import apply_axis_op

if TYPE_CHECKING:
    from .models import RelativeConfig


def apply_relative(
    lf: pl.LazyFrame,
    value_col: str,
    relative: RelativeConfig,
) -> pl.LazyFrame:
    """split_axis の値で各行を分子/分母に振り分け、比(mode="diff" なら差)を取る。

    Returns:
        `value_col` が相対値(mode="diff" なら差)になった LazyFrame。
        split_axis の列と分母側の作業列は落としてある。

    """
    axis = relative.split_axis

    numerator = lf.filter(pl.col(axis) == relative.numerator_when).drop(axis)
    denominator = lf.filter(pl.col(axis) == relative.denominator_when).drop(axis)

    for step in relative.denominator_pre_aggregation:
        schema_cols = denominator.collect_schema().names()
        group_keys = [c for c in schema_cols if c not in {value_col, step.axis}]
        denominator = apply_axis_op(denominator, value_col, step.axis, step, group_keys)

    denominator = denominator.rename({value_col: "__denom__"})
    join_keys = [c for c in denominator.collect_schema().names() if c != "__denom__"]

    if join_keys:
        # その時点で残っている全軸の値が一致する行同士をペアにする
        out = numerator.join(denominator, on=join_keys, how="left")
    else:
        # 分母が1スカラーまで潰れている場合: 全行にブロードキャスト
        out = numerator.join(denominator, how="cross")
    if relative.mode == "diff":
        # delta値: 分子 - 分母。offset は差で相殺される((a+o)-(b+o)==a-b)
        # ため、単に適用しない
        combined = pl.col(value_col) - pl.col("__denom__")
    else:
        offset = relative.denominator_offset
        combined = (pl.col(value_col) + offset) / (pl.col("__denom__") + offset)
    # 分母の相手が見つからなかった行(left join 不成立)は null になる。後段の
    # mean/sum は null を黙って除外して「エラーなしで値がズレる」ため、NaN に
    # 変えて最終 collapse まで伝播させる(原因は compute_score_part が診断する)
    return out.with_columns(combined.fill_null(float("nan")).alias(value_col)).drop("__denom__")
