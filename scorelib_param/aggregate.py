# Copyright (c) 2026
"""軸ごとの逐次集計。

値列と軸列を持つ LazyFrame に対し、`order` の順に各軸の集計指示を適用して
軸を1つずつ潰していき、最終的に値列が1スカラーになるまで畳む。
op の一覧は docs/score_gui_design.md 4.2節。
"""

from __future__ import annotations

from itertools import starmap
from typing import TYPE_CHECKING, Any, cast

import polars as pl

from .expression import evaluate_expression
from .models import MULTI_OPS, TRANSFORM_OPS, UNARY_OPS, AggregationSpec

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# op の集合は models.MULTI_OPS が唯一の正(op 追加時の二重更新を避ける)
_SIMPLE_OPS = frozenset(MULTI_OPS)


def group_column_expr(axis: str, ranges: Mapping[str, tuple[int, int]]) -> pl.Expr:
    """グループ派生軸(models.GroupDef)のラベル式。

    各行の元軸の値が入る範囲のグループ名を割り当てる。データ読み込み直後に
    使われ、以降グループ列は普通の軸として集計される。

    Returns:
        各行に「元軸の値が入る範囲のグループ名」を割り当てる Utf8 の式。
        どの範囲にも入らない行は null になる。

    """
    expr = pl.lit(None, dtype=pl.Utf8)
    for name, (lo, hi) in ranges.items():
        expr = pl.when((pl.col(axis) >= lo) & (pl.col(axis) <= hi)).then(pl.lit(name)).otherwise(expr)
    return expr


def _reduce(lf: pl.LazyFrame, value_col: str, group_keys: Sequence[str], op: str) -> pl.LazyFrame:
    agg_expr = getattr(pl.col(value_col), op)()
    if op in {"min", "max"}:
        # polars の min/max は NaN を黙って飛ばす(mean/sum は伝播する)。照会
        # 失敗の NaN 印(relative.py / dvtbudget.py / diff)が途中の min/max で
        # 消えると「エラーなしで値がズレる」ため、NaN があれば結果も NaN にする
        has_nan = pl.col(value_col).cast(pl.Float64).is_nan().any()
        agg_expr = pl.when(has_nan).then(float("nan")).otherwise(agg_expr)
    agg_expr = agg_expr.alias(value_col)
    if group_keys:
        return lf.group_by(list(group_keys)).agg(agg_expr)
    return lf.select(agg_expr)


def _combine(op: str, value: pl.Expr, operand: float | pl.Expr) -> pl.Expr:
    if op == "add":
        return value + operand
    if op == "sub":
        return value - operand
    if op == "mul":
        return value * operand
    if op == "div":
        return value / operand
    msg = f"unknown transform op '{op}' (expected one of {sorted(TRANSFORM_OPS)})"
    raise ValueError(msg)


def _per_value_operand(lf: pl.LazyFrame, axis: str, mapping: Mapping, what: str) -> pl.Expr:
    """{軸の値: 定数} の辞書を「各行の axis 列の値に対応する定数」の式にする。

    辞書に無い値の行が存在すると定数が null になり静かに null が伝播して
    しまう — ほぼ確実に定義の古さが原因なので、該当値の一覧つきで失敗させる
    (グループ派生列の未カバー検出 cli._with_group_columns と同じ方針)。

    Returns:
        各行の axis 列の値に対応する定数を与える Float64 の式。

    Raises:
        ValueError: axis 列に辞書でカバーされない値の行が存在するとき。

    """
    operand = pl.lit(None, dtype=pl.Float64)
    for key, const in mapping.items():
        operand = pl.when(pl.col(axis) == key).then(pl.lit(float(const))).otherwise(operand)
    uncovered = lf.filter(operand.is_null()).select(pl.col(axis).unique()).collect()
    if uncovered.height:
        vals = sorted(uncovered[axis].to_list())
        msg = (
            f"values of '{axis}' have no entry in the {what}: {vals} "
            "(extend the weight dict or aggregate those values away first)"
        )
        raise ValueError(msg)
    return operand


def apply_transform(lf: pl.LazyFrame, value_col: str, spec: AggregationSpec) -> pl.LazyFrame:
    """値列への行単位の定数演算(add/sub/mul/div)。

    apply_axis_op と違って軸は潰さない。order 内の仮想ステップ "__xxx__"
    (例: 相対化の前にオフセットを足す __offset__、WLgroup 別の重みを掛ける
    __weight__)が使う。

    `spec.by` が指定され value が辞書のときは「by 軸の値ごとの定数」:
    各行の by 列の値に対応する定数で演算する(例: WLgroup 別の重み)。
    辞書に無い値の行が存在したらエラー(重み定義の古さの検出)。

    単項op(UNARY_OPS)は定数を取らない行単位の関数:
    abs = |x|、log = ln(max(|x|, floor))(floor は必須・モデル検証済み)。

    Returns:
        値列を演算後の値で置き換えた LazyFrame(行数・軸列は変わらない)。

    Raises:
        ValueError: 定数演算 op に value が無いとき、by 軸がその時点で
            残っていないとき、by 辞書に無い値の行が存在するとき。

    """
    if spec.op in UNARY_OPS:
        col = pl.col(value_col)
        if spec.op == "abs":
            return lf.with_columns(col.abs().alias(value_col))
        # log: 0 や負値で発散しない安全な対数(KLD の標準計算の形)
        return lf.with_columns(col.abs().clip(lower_bound=spec.floor).log().alias(value_col))

    if spec.value is None:
        msg = f"transform op '{spec.op}' requires 'value'"
        raise ValueError(msg)

    if spec.by is None or not isinstance(spec.value, dict):
        # 全行共通の定数(スカラー重みセットを by つきで参照した場合を含む)
        return lf.with_columns(_combine(spec.op, pl.col(value_col), spec.value).alias(value_col))

    schema_cols = lf.collect_schema().names()
    if spec.by not in schema_cols:
        msg = (
            f"transform 'by' axis '{spec.by}' not present at this step (already "
            f"aggregated away? columns = {schema_cols}); place the step while the "
            "axis is still alive"
        )
        raise ValueError(msg)
    operand = _per_value_operand(lf, spec.by, spec.value, "transform weights")
    return lf.with_columns(_combine(spec.op, pl.col(value_col), operand).alias(value_col))


def _apply_simple_op(
    lf: pl.LazyFrame,
    value_col: str,
    axis: str,
    spec: AggregationSpec,
    group_keys: Sequence[str],
) -> pl.LazyFrame:
    """単純集計op(mean/sum/min/max)で軸を潰す(apply_axis_op の下請け)。

    集計時重みの辞書に無い値の行が存在するときのエラーは _per_value_operand が
    投げる。

    Returns:
        選択集合での限定・集計時重みの乗算を経て `axis` 列を潰した LazyFrame。

    """
    # `value` リストを付けると、その選択集合に限定してから集計する
    target = lf.filter(pl.col(axis).is_in(spec.value)) if spec.value is not None else lf
    if spec.weight is not None:
        # 集計時重み: この軸を潰す直前に軸の値ごとの重みを値へ乗じる
        # (正規化された加重平均ではない: mean なら mean(weight * value))。
        # 未カバー検出は選択集合で絞った後の行が対象
        if isinstance(spec.weight, dict):
            operand = _per_value_operand(target, axis, spec.weight, f"aggregation weights for axis '{axis}'")
        else:
            operand = pl.lit(float(spec.weight))
        target = target.with_columns((pl.col(value_col) * operand).alias(value_col))
    return _reduce(target, value_col, group_keys, spec.op)


def _apply_diff_op(
    lf: pl.LazyFrame,
    value_col: str,
    axis: str,
    spec: AggregationSpec,
    group_keys: Sequence[str],
) -> pl.LazyFrame:
    """2つの選択の差 value(a) - value(b) で軸を潰す(apply_axis_op の下請け)。

    自分自身との結合で対にする(op="diff" は pydantic の _check_value_shape が
    2要素リストを保証)。

    Returns:
        `axis` 列を潰し、値列を差で置き換えた LazyFrame。

    """
    a_val, b_val = cast("list[Any]", spec.value)
    a = lf.filter(pl.col(axis) == a_val).drop(axis)
    b = lf.filter(pl.col(axis) == b_val).drop(axis).rename({value_col: "__b__"})
    keys = list(group_keys)
    joined = a.join(b, on=keys, how="left") if keys else a.join(b, how="cross")
    # b 側の相手が見つからなかった行(left join 不成立)は null になる。後段の
    # 集計が null を黙って除外して「エラーなしで値がズレる」ため、NaN に変えて
    # 最終 collapse まで伝播させる(原因は compute_score_part が診断する)
    diffed = (pl.col(value_col) - pl.col("__b__")).fill_null(float("nan"))
    return joined.with_columns(diffed.alias(value_col)).drop("__b__")


def _apply_expr_op(
    lf: pl.LazyFrame,
    value_col: str,
    axis: str,
    spec: AggregationSpec,
    group_keys: Sequence[str],
) -> pl.LazyFrame:
    """式(expr)の評価で軸を潰す(apply_axis_op の下請け)。

    Returns:
        グループごとに式を評価した結果で `axis` 列を潰した LazyFrame。

    Raises:
        ValueError: expr op に式が無いとき、by 参照でグループ内に同じ軸の値が
            複数回現れたとき。

    """
    if not spec.expr:
        msg = f"expr op for axis '{axis}' requires 'expr'"
        raise ValueError(msg)
    # ネスト関数の閉包にはローカル変数の narrowing だけが届く(spec.expr のままだと届かない)
    expr = spec.expr

    def _eval(vals: list, axis_vals: list) -> float:
        # 式の中では values(この軸の全値のリスト)と by[軸の値] が使える
        by: dict = {}
        for k, v in zip(axis_vals, vals, strict=False):
            if k in by:
                msg = (
                    f"axis value '{k}' appears more than once within a group for axis "
                    f"'{axis}'; 'by' lookups require unique axis values"
                )
                raise ValueError(msg)
            by[k] = v
        return evaluate_expression(expr, {"values": vals, "by": by})

    if group_keys:
        df = lf.group_by(list(group_keys)).agg([pl.col(value_col), pl.col(axis)]).collect()
        result = list(starmap(_eval, zip(df[value_col].to_list(), df[axis].to_list(), strict=False)))
        return df.drop(value_col, axis).with_columns(pl.Series(value_col, result)).lazy()
    df = lf.select([pl.col(value_col), pl.col(axis)]).collect()
    result_value = _eval(df[value_col].to_list(), df[axis].to_list())
    return pl.LazyFrame({value_col: [float(result_value)]})


def apply_axis_op(
    lf: pl.LazyFrame,
    value_col: str,
    axis: str,
    spec: AggregationSpec,
    group_keys: Sequence[str],
) -> pl.LazyFrame:
    """1つの軸を1つの集計指示で潰す(結果の frame から `axis` 列は消え、`group_keys` + 値列だけが残る)。

    Returns:
        `axis` 列を潰した後の LazyFrame(残る列は `group_keys` + 値列)。

    Raises:
        ValueError: op が未知のとき、expr op に式が無いとき、by 参照で
            グループ内に同じ軸の値が複数回現れたとき、集計時重みの辞書に
            無い値の行が存在するとき。

    """
    if spec.op == "filter":
        # リストは is_in(複数値選択): 該当行を残して軸列を落とす。残った行は
        # 後段集計に複製として流れ込む(例: 同じ dataName を持つ複数 Measure を
        # まとめて対象にする — docs/spec_change_dataname_measure.md 6.4節)
        if isinstance(spec.value, list):
            return lf.filter(pl.col(axis).is_in(spec.value)).drop(axis)
        return lf.filter(pl.col(axis) == spec.value).drop(axis)
    if spec.op in _SIMPLE_OPS:
        return _apply_simple_op(lf, value_col, axis, spec, group_keys)
    if spec.op == "diff":
        return _apply_diff_op(lf, value_col, axis, spec, group_keys)
    if spec.op == "expr":
        return _apply_expr_op(lf, value_col, axis, spec, group_keys)
    msg = f"unknown aggregation op '{spec.op}'"
    raise ValueError(msg)


def apply_aggregations(
    lf: pl.LazyFrame,
    value_col: str,
    order: Sequence[str],
    aggregations: dict[str, AggregationSpec],
) -> pl.LazyFrame:
    """Order の各軸の集計指示を順に適用して軸を1つずつ潰す。

    ここでは結果がスカラーであることは要求しない(`__relative__` ステップの
    前後など、order の一部分だけを処理する呼び出し元があるため)。

    重要: グループキーは「その時点で残っている全列」。order に置いた
    グループ派生列などが自然にキーとして生き残る仕組みの要。

    Returns:
        `order` の全軸を潰し終えた LazyFrame。

    Raises:
        ValueError: order に載っている軸に集計指示が無いとき、または
            その軸がその時点の列に存在しないとき。

    """
    for axis in order:
        if axis not in aggregations:
            msg = f"axis '{axis}' listed in order but has no aggregation instruction"
            raise ValueError(msg)
        spec = aggregations[axis]
        schema_cols = lf.collect_schema().names()
        if axis not in schema_cols:
            msg = f"axis '{axis}' not present (already aggregated away?): columns = {schema_cols}"
            raise ValueError(msg)
        group_keys = [c for c in schema_cols if c not in {value_col, axis}]
        lf = apply_axis_op(lf, value_col, axis, spec, group_keys)
    return lf


class CollapseNullError(ValueError):
    """最終結果の値列に null / NaN が残っていたことを表す。

    原因(filter の空振り・相対化ペア不成立・dVtBudget 係数/温度の照会失敗
    など)はこの層では分からない。compute_score_part がこの例外を捕まえて
    パイプラインを歩き直し、原因ステップを名指しした ValueError に変換する。
    """


def collapse(lf: pl.LazyFrame, value_col: str, identity_axes: Sequence[str] = ()) -> pl.DataFrame:
    """`identity_axes` 以外がすべて潰れていることを検証して DataFrame を返す。

    結果は identity 軸の組み合わせごとに1行。

    identity_axes は現状常に空(単一epoch運用 → 1スカラー)。将来、過去epoch
    一括処理で Epoch 列をパイプライン全体に通す場合に使うための引数。

    Returns:
        identity 軸 + 値列だけを持つ collect 済みの DataFrame。

    Raises:
        ValueError: identity 軸以外の列が潰れずに残っているとき、または
            identity 軸なしで結果が1行に収束していないとき。
        CollapseNullError: 値列に null / NaN が残っているとき(入力行が
            0件のまま集計された・照会系ステップが値を失った、のどちらか)。

    """
    df = lf.collect()
    expected = set(identity_axes) | {value_col}
    if set(df.columns) != expected:
        msg = (
            f"expected aggregation to collapse to columns {sorted(expected)}, got {df.columns} "
            "(order did not cover all axes?)"
        )
        raise ValueError(msg)
    if not identity_axes and df.height != 1:
        msg = f"expected aggregation to collapse to a single value, got {df.height} rows"
        raise ValueError(msg)
    # NaN も検査対象: 照会系ステップ(相対化ペア・dVtBudget 係数)は照会に
    # 失敗した行を null でなく NaN として伝播させる(mean 等が null を黙って
    # 除外して「エラーなしで値がズレる」のを防ぐため — relative.py / dvtbudget.py)
    values = df[value_col].cast(pl.Float64)
    if values.is_null().any() or values.is_nan().any():
        msg = f"aggregation produced null for '{value_col}'"
        raise CollapseNullError(msg)
    return df


def collapse_to_scalar(lf: pl.LazyFrame, value_col: str) -> float:
    """全軸が潰れていることを検証して値列の1スカラーを返す。

    Returns:
        1行に収束した値列の中身を float にした値。

    """
    return float(collapse(lf, value_col)[value_col][0])


def aggregate_score_part(
    lf: pl.LazyFrame,
    value_col: str,
    order: Sequence[str],
    aggregations: dict[str, AggregationSpec],
) -> float:
    """1スコアパーツぶんの逐次集計パイプラインを最後まで実行してスカラーを返す。

    Returns:
        全軸を畳み終えたスコアパーツの値(float)。

    """
    lf = apply_aggregations(lf, value_col, order, aggregations)
    return collapse_to_scalar(lf, value_col)
