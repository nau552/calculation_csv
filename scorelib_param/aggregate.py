"""軸ごとの逐次集計。

値列と軸列を持つ LazyFrame に対し、`order` の順に各軸の集計指示を適用して
軸を1つずつ潰していき、最終的に値列が1スカラーになるまで畳む。
op の一覧は docs/score_gui_design.md 4.2節。
"""
from __future__ import annotations

from typing import Dict, Mapping, Sequence, Tuple

import polars as pl

from .expression import evaluate_expression
from .models import TRANSFORM_OPS, AggregationSpec

_SIMPLE_OPS = {"mean", "sum", "min", "max"}


def group_column_expr(axis: str, ranges: Mapping[str, Tuple[int, int]]) -> pl.Expr:
    """グループ派生軸（models.GroupDef）のラベル式: 各行の元軸の値が入る範囲の
    グループ名を割り当てる。データ読み込み直後に使われ、以降グループ列は
    普通の軸として集計される。"""
    expr = pl.lit(None, dtype=pl.Utf8)
    for name, (lo, hi) in ranges.items():
        expr = pl.when((pl.col(axis) >= lo) & (pl.col(axis) <= hi)).then(pl.lit(name)).otherwise(expr)
    return expr


def _reduce(lf: pl.LazyFrame, value_col: str, group_keys: Sequence[str], op: str) -> pl.LazyFrame:
    agg_expr = getattr(pl.col(value_col), op)().alias(value_col)
    if group_keys:
        return lf.group_by(list(group_keys)).agg(agg_expr)
    return lf.select(agg_expr)


def _combine(op: str, value: pl.Expr, operand) -> pl.Expr:
    if op == "add":
        return value + operand
    if op == "sub":
        return value - operand
    if op == "mul":
        return value * operand
    if op == "div":
        return value / operand
    raise ValueError(f"unknown transform op '{op}' (expected one of {sorted(TRANSFORM_OPS)})")


def _per_value_operand(lf: pl.LazyFrame, axis: str, mapping: Mapping, what: str) -> pl.Expr:
    """{軸の値: 定数} の辞書を「各行の axis 列の値に対応する定数」の式にする。

    辞書に無い値の行が存在すると定数が null になり静かに null が伝播して
    しまう — ほぼ確実に定義の古さが原因なので、該当値の一覧つきで失敗させる
    （グループ派生列の未カバー検出 cli._with_group_columns と同じ方針）。
    """
    operand = pl.lit(None, dtype=pl.Float64)
    for key, const in mapping.items():
        operand = pl.when(pl.col(axis) == key).then(pl.lit(float(const))).otherwise(operand)
    uncovered = lf.filter(operand.is_null()).select(pl.col(axis).unique()).collect()
    if uncovered.height:
        vals = sorted(uncovered[axis].to_list())
        raise ValueError(
            f"values of '{axis}' have no entry in the {what}: {vals} "
            "(extend the weight dict or aggregate those values away first)"
        )
    return operand


def apply_transform(lf: pl.LazyFrame, value_col: str, spec: AggregationSpec) -> pl.LazyFrame:
    """値列への行単位の定数演算（add/sub/mul/div）。apply_axis_op と違って軸は
    潰さない。order 内の仮想ステップ "__xxx__"（例: 相対化の前にオフセットを
    足す __offset__、WLgroup 別の重みを掛ける __weight__）が使う。

    `spec.by` が指定され value が辞書のときは「by 軸の値ごとの定数」:
    各行の by 列の値に対応する定数で演算する（例: WLgroup 別の重み）。
    辞書に無い値の行が存在したらエラー（重み定義の古さの検出）。
    """
    if spec.value is None:
        raise ValueError(f"transform op '{spec.op}' requires 'value'")

    if spec.by is None or not isinstance(spec.value, dict):
        # 全行共通の定数（スカラー重みセットを by つきで参照した場合を含む）
        return lf.with_columns(_combine(spec.op, pl.col(value_col), spec.value).alias(value_col))

    schema_cols = lf.collect_schema().names()
    if spec.by not in schema_cols:
        raise ValueError(
            f"transform 'by' axis '{spec.by}' not present at this step (already "
            f"aggregated away? columns = {schema_cols}); place the step while the "
            "axis is still alive"
        )
    operand = _per_value_operand(lf, spec.by, spec.value, "transform weights")
    return lf.with_columns(_combine(spec.op, pl.col(value_col), operand).alias(value_col))


def apply_axis_op(
    lf: pl.LazyFrame,
    value_col: str,
    axis: str,
    spec: AggregationSpec,
    group_keys: Sequence[str],
) -> pl.LazyFrame:
    """1つの軸を1つの集計指示で潰す（結果の frame から `axis` 列は消え、
    `group_keys` + 値列だけが残る）。
    """
    if spec.op == "filter":
        return lf.filter(pl.col(axis) == spec.value).drop(axis)

    if spec.op in _SIMPLE_OPS:
        # `value` リストを付けると、その選択集合に限定してから集計する
        target = lf.filter(pl.col(axis).is_in(spec.value)) if spec.value is not None else lf
        if spec.weight is not None:
            # 集計時重み: この軸を潰す直前に軸の値ごとの重みを値へ乗じる
            # （正規化された加重平均ではない: mean なら mean(weight × value)）。
            # 未カバー検出は選択集合で絞った後の行が対象
            if isinstance(spec.weight, dict):
                operand = _per_value_operand(
                    target, axis, spec.weight, f"aggregation weights for axis '{axis}'"
                )
            else:
                operand = pl.lit(float(spec.weight))
            target = target.with_columns((pl.col(value_col) * operand).alias(value_col))
        return _reduce(target, value_col, group_keys, spec.op)

    if spec.op == "diff":
        # 2つの選択の差で潰す: value(a) - value(b)。自分自身との結合で対にする
        a_val, b_val = spec.value
        a = lf.filter(pl.col(axis) == a_val).drop(axis)
        b = lf.filter(pl.col(axis) == b_val).drop(axis).rename({value_col: "__b__"})
        keys = list(group_keys)
        joined = a.join(b, on=keys, how="left") if keys else a.join(b, how="cross")
        return joined.with_columns((pl.col(value_col) - pl.col("__b__")).alias(value_col)).drop("__b__")

    if spec.op == "expr":
        if not spec.expr:
            raise ValueError(f"expr op for axis '{axis}' requires 'expr'")

        def _eval(vals: list, axis_vals: list) -> float:
            # 式の中では values（この軸の全値のリスト）と by[軸の値] が使える
            by: dict = {}
            for k, v in zip(axis_vals, vals):
                if k in by:
                    raise ValueError(
                        f"axis value '{k}' appears more than once within a group for axis "
                        f"'{axis}'; 'by' lookups require unique axis values"
                    )
                by[k] = v
            return evaluate_expression(spec.expr, {"values": vals, "by": by})

        if group_keys:
            df = lf.group_by(list(group_keys)).agg([pl.col(value_col), pl.col(axis)]).collect()
            result = [
                _eval(vals, axis_vals)
                for vals, axis_vals in zip(df[value_col].to_list(), df[axis].to_list())
            ]
            return df.drop(value_col, axis).with_columns(pl.Series(value_col, result)).lazy()
        df = lf.select([pl.col(value_col), pl.col(axis)]).collect()
        result_value = _eval(df[value_col].to_list(), df[axis].to_list())
        return pl.LazyFrame({value_col: [float(result_value)]})

    raise ValueError(f"unknown aggregation op '{spec.op}'")


def apply_aggregations(
    lf: pl.LazyFrame,
    value_col: str,
    order: Sequence[str],
    aggregations: Dict[str, AggregationSpec],
) -> pl.LazyFrame:
    """order の各軸の集計指示を順に適用して軸を1つずつ潰す。ここでは結果が
    スカラーであることは要求しない（`__relative__` ステップの前後など、
    order の一部分だけを処理する呼び出し元があるため）。

    重要: グループキーは「その時点で残っている全列」。order に置いた
    グループ派生列などが自然にキーとして生き残る仕組みの要。
    """
    for axis in order:
        if axis not in aggregations:
            raise ValueError(f"axis '{axis}' listed in order but has no aggregation instruction")
        spec = aggregations[axis]
        schema_cols = lf.collect_schema().names()
        if axis not in schema_cols:
            raise ValueError(f"axis '{axis}' not present (already aggregated away?): columns = {schema_cols}")
        group_keys = [c for c in schema_cols if c not in (value_col, axis)]
        lf = apply_axis_op(lf, value_col, axis, spec, group_keys)
    return lf


def collapse(
    lf: pl.LazyFrame, value_col: str, identity_axes: Sequence[str] = ()
) -> pl.DataFrame:
    """`identity_axes` 以外がすべて潰れていることを検証して DataFrame を返す
    （identity 軸の組み合わせごとに1行）。

    identity_axes は現状常に空（単一epoch運用 → 1スカラー）。将来、過去epoch
    一括処理で Epoch 列をパイプライン全体に通す場合に使うための引数。
    """
    df = lf.collect()
    expected = set(identity_axes) | {value_col}
    if set(df.columns) != expected:
        raise ValueError(
            f"expected aggregation to collapse to columns {sorted(expected)}, got {df.columns} "
            "(order did not cover all axes?)"
        )
    if not identity_axes and df.height != 1:
        raise ValueError(
            f"expected aggregation to collapse to a single value, got {df.height} rows"
        )
    if df[value_col].null_count() > 0:
        raise ValueError(
            f"aggregation produced null for '{value_col}' — a filter value probably "
            "matched no rows (check filter values against the data)"
        )
    return df


def collapse_to_scalar(lf: pl.LazyFrame, value_col: str) -> float:
    return float(collapse(lf, value_col)[value_col][0])


def aggregate_score_part(
    lf: pl.LazyFrame,
    value_col: str,
    order: Sequence[str],
    aggregations: Dict[str, AggregationSpec],
) -> float:
    """1スコアパーツぶんの逐次集計パイプラインを最後まで実行してスカラーを返す。"""
    lf = apply_aggregations(lf, value_col, order, aggregations)
    return collapse_to_scalar(lf, value_col)
