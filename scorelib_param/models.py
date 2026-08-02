# Copyright (c) 2026
"""スコア/スコアパーツ定義のデータモデル(pydantic)。

設計の経緯は docs/score_gui_design.md 3〜6節を参照。
検証ルールはすべてここに集約し、UIも同じモデルで検証する(二重実装しない)。
エラーメッセージは実行環境を選ばないよう英語のまま(UIがそのまま表示する)。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal, TypeIs

from pydantic import BaseModel, Field, RootModel, model_validator

# order エントリの複合軸の区切り(例: "State&Read_Label")
COMBINED_SEP = "&"

# 通常のパイプラインの代わりにユーザ定義のPython関数
# (custom_parts.py — scorelib_param/custom.py 参照)を呼ぶ ScorePart.type の値
CUSTOM_TYPE = "custom"

AggOp = Literal[
    # filter は行の選択: スカラーで等値、リストで is_in(複数値。該当行を残して
    # 軸列を落とし、残った行は後段集計に複製として流れ込む)
    "filter",
    # mean/sum/min/max は軸を潰す集計。任意の `value` リストを付けると、
    # 先にその選択集合へ限定してから集計する
    # (例: {"op": "sum", "value": [0, 1]} は軸が 0 か 1 の行だけの和)
    "mean",
    "sum",
    "min",
    "max",
    "expr",
    # ちょうど2つの選択の組(順序あり)で軸を潰す:
    # {"op": "diff", "value": [a, b]} -> value(a) - value(b)
    "diff",
    # 変換op(下の TRANSFORM_OPS): 軸を潰さず値列に行単位で適用する。
    # order の仮想ステップ "__xxx__"(例: __offset__)が使う
    "add",
    "sub",
    "mul",
    "div",
    # 単項変換op(下の UNARY_OPS): 定数を取らない行単位の関数。
    # abs = |x|、log = ln(max(|x|, floor))(floor 必須 — 0 や負値で発散しない
    # 安全な対数。KLD の標準計算 log(max(|x|, 1e-6)) がこの形)
    "abs",
    "log",
]

# 任意の選択リストを `value` に取れる集計op。UI(ui/widgets.py)と共有して
# 両者が食い違わないようにする
MULTI_OPS = ("mean", "sum", "min", "max")

# 変換op: 軸を潰さず値列へ行単位で定数演算を適用する(aggregate.apply_transform)。
# order には "__xxx__" 仮想ステップとして複数置ける。`value` は
# - 数値: 全行に同じ定数(例: {"op": "mul", "value": -1} で正負反転)
# - `by` + 辞書: 軸の値ごとの定数(例: WLgroup 別の重み。
#   {"op": "mul", "by": "WLgroup", "value": {"WLgroup00": 10.0, ...}})
TRANSFORM_OPS = ("add", "sub", "mul", "div")

# 単項変換op: 定数を取らない行単位の関数(0.6.0 で追加)。value/by/ref は
# 取らない。log は `floor` が必須: log(max(|x|, floor))。
# 変換ステップ全体 = TRANSFORM_OPS + UNARY_OPS(STEP_OPS)
UNARY_OPS = ("abs", "log")
STEP_OPS = TRANSFORM_OPS + UNARY_OPS

# op "diff" の value が取る選択の個数(result = a - b の2項)
_DIFF_SELECTIONS = 2


def _is_number(x: object) -> TypeIs[int | float]:
    """数値(bool を除く int / float)かどうかを判定する。

    bool は int のサブクラスなので明示的に弾く(重み・定数の検査で
    True/False を数値と誤認しないため)。

    Returns:
        bool でない int / float なら True。

    """
    return isinstance(x, (int, float)) and not isinstance(x, bool)


class AggregationSpec(BaseModel):
    """1つの軸(または仮想ステップ)の集計指示。

    選択は op によらず常に `value` に書く:
    - スカラー = 軸の値1つの選択({"op": "filter", "value": "A2B"})
    - 辞書 = 複合軸上の1つの組み合わせ
      ({"op": "filter", "value": {"State": "A2B", "Read_Label": "..."}})
    - リスト = 常に選択の並び({"op": "diff", "value": ["R2A", "B2A"]})
    op ごとに違うのは必要な選択の個数だけ(filter: 1個以上(複数は is_in)、
    diff: 2、mean/sum/min/max: 任意個または無し)。
    """

    op: AggOp
    value: Any | None = None
    # インラインの `value` の代わりに使う、名前付きセットへの参照。
    # 通常opでは選択セット(optimization.selectionSets)、変換op(TRANSFORM_OPS)
    # では重みセット(optimization.weightSets / WLgroupWeight)を指す。
    # 計算前に解決され、解決後の内容はインラインで書いた場合と
    # 全く同じ形状検査を通る
    ref: str | None = None
    expr: str | None = None
    # 変換op専用: 定数を「この軸の値ごと」に引く(例: by="WLgroup" +
    # value={グループ名: 重み})。その時点で軸列が残っている必要がある
    by: str | None = None
    # 集計時重み(mean/sum/min/max 専用・任意): この軸を潰す**直前**に、
    # 軸の値ごとの重みを値列に乗じてから集計する。正規化された加重平均では
    # ない(mean なら mean(weight * value))。
    # - 辞書 {軸の値: 数値}(例: {"WLgroup00": 10.0, ...})または数値1つ
    # - weight_ref は重みセット(optimization.WLgroupWeight / weightSets)参照
    # タイミングを明示的に制御したい場合(dVtBudget 変換の前後など)は従来
    # どおり "__xxx__" 変換ステップ(by + mul)を使う。両方が適用可能な
    # 場面では結果は同一
    weight: Any | None = None
    weight_ref: str | None = None
    # 表示・検証用の注記(実行には不使用): 選択値 → 表示名(例: Measure 番号 →
    # dataName)。Measure 番号で指定した設定に人が読める名前を残すための欄で、
    # UI と将来の validate が使う(docs/spec_change_dataname_measure.md 6.1節)。
    # キーは JSON の制約上文字列({"1": "evaluation_..."})
    labels: dict[str, str] | None = None
    # op="log" 専用(必須): log(max(|x|, floor)) の床。0 や負値で発散させない
    floor: float | None = None

    @model_validator(mode="after")
    def _check_value_shape(self) -> AggregationSpec:
        """Op ごとの value 形状検査(間違えやすい箇所なのでエラーは具体的に)。

        検査の実体は op の種類ごとの `_check_*` / `_normalize_*` ヘルパーに
        分かれている(このメソッドは適用順と早期 return だけを持つ)。
        value / ref / by / weight / floor / expr の組み合わせや形状が op の
        要求に合わないときの ValueError は各ヘルパーが送出する。

        Returns:
            検証を通った自身(filter の単一要素リストはスカラーへ、
            mean/sum/min/max のスカラー選択はリストへ正規化済み)。

        """
        self._check_modifier_fields()
        if self.op in UNARY_OPS:
            self._check_unary_shape()
            return self
        self._check_weight_shape()
        if self.ref is not None:
            self._check_ref_combination()
            # 形状検査は ref 解決後にもう一度走る
            return self
        if self.op == "filter":
            self._check_filter_shape()
        elif self.op in TRANSFORM_OPS:
            self._check_transform_shape()
        elif self.op == "diff":
            self._check_diff_shape()
        elif self.op in MULTI_OPS:
            self._normalize_multi_shape()
        elif self.op == "expr":
            self._check_expr_shape()
        return self

    def _check_modifier_fields(self) -> None:
        """修飾フィールド by / floor が対応する op 以外に付いていないか検査する。

        Raises:
            ValueError: 'by' が変換op以外に、または 'floor' が op 'log' 以外に
                付いているとき。

        """
        if self.by is not None and self.op not in TRANSFORM_OPS:
            msg = f"'by' applies only to transform ops {list(TRANSFORM_OPS)}, not op '{self.op}'"
            raise ValueError(msg)
        if self.floor is not None and self.op != "log":
            msg = f"'floor' applies only to op 'log', not op '{self.op}'"
            raise ValueError(msg)

    def _check_unary_shape(self) -> None:
        """単項変換op(abs/log)の検査: value/ref は取らず、log は floor 必須。

        Raises:
            ValueError: value / ref が指定されているとき、または op 'log' の
                floor が正の数でないとき。

        """
        if self.value is not None or self.ref is not None:
            msg = f"op '{self.op}' takes no 'value'/'ref' (row-wise function)"
            raise ValueError(msg)
        if self.op == "log" and (not _is_number(self.floor) or self.floor <= 0):
            msg = (
                "op 'log' requires a positive 'floor' — computes log(max(|x|, floor)) "
                '(e.g. {"op": "log", "floor": 1e-6})'
            )
            raise ValueError(msg)

    def _check_weight_shape(self) -> None:
        """集計時重み(weight / weight_ref)の適用可否と形状を検査する。

        Raises:
            ValueError: mean/sum/min/max 以外の op に weight / weight_ref が
                付いているとき、両方が同時に指定されたとき、または weight が
                数値でも空でない数値の辞書でもないとき。

        """
        if (self.weight is not None or self.weight_ref is not None) and self.op not in MULTI_OPS:
            msg = (
                f"'weight'/'weight_ref' apply only to aggregation ops {list(MULTI_OPS)}, "
                f"not op '{self.op}' (for transform steps use 'by' + a weight dict in 'value')"
            )
            raise ValueError(msg)
        if self.weight is not None and self.weight_ref is not None:
            msg = "give either 'weight' or 'weight_ref' (a named weight set), not both"
            raise ValueError(msg)
        if self.weight is None:
            return
        if isinstance(self.weight, dict):
            if not self.weight or not all(_is_number(x) for x in self.weight.values()):
                msg = (
                    "'weight' requires a non-empty dict of numbers keyed by axis values "
                    '(e.g. {"WLgroup00": 10.0, "WLgroup01": 1.0}) or a single number'
                )
                raise ValueError(msg)
        elif not _is_number(self.weight):
            msg = "'weight' requires a dict of numbers or a single number"
            raise ValueError(msg)

    def _check_ref_combination(self) -> None:
        """`ref`(名前付きセット参照)と他フィールドの組み合わせを検査する。

        参照先の中身の形状検査は、ref 解決後の再検証
        (ScorePart.resolve_selection_refs)に任せる。

        Raises:
            ValueError: value と ref が同時に指定されたとき、op 'expr' に
                ref が付いているとき、または変換opの ref に 'by' が無いとき。

        """
        if self.value is not None:
            msg = "give either 'value' or 'ref' (a named set), not both"
            raise ValueError(msg)
        if self.op == "expr":
            msg = "op 'expr' takes no selections, so 'ref' is not applicable"
            raise ValueError(msg)
        if self.op in TRANSFORM_OPS and self.by is None:
            msg = (
                f"op '{self.op}' with 'ref' (a weight set) also needs 'by': the axis whose "
                'values the weights are keyed by (e.g. "by": "WLgroup")'
            )
            raise ValueError(msg)

    def _check_filter_shape(self) -> None:
        """Op 'filter' の選択の形状検査(単一要素リストはスカラーへ正規化)。

        Raises:
            ValueError: value が無いとき、空リストのとき、または選択に
                ネストしたリストが混ざっているとき。

        """
        v = self.value
        if isinstance(v, list):
            if not v:
                msg = "op 'filter' requires 'value' (at least one selection)"
                raise ValueError(msg)
            if any(isinstance(x, list) for x in v):
                msg = (
                    "op 'filter': each selection must be a scalar or, for combined axes, "
                    "a dict {axis: value} — not a nested list"
                )
                raise ValueError(msg)
            if len(v) == 1:
                self.value = v[0]
        elif v is None:
            msg = "op 'filter' requires 'value'"
            raise ValueError(msg)

    def _check_transform_shape(self) -> None:
        """変換op(add/sub/mul/div)の value 形状検査(0除算もここで拒否)。

        Raises:
            ValueError: value が op の要求する形(by 無しは数値、by 付きは
                空でない数値の辞書または数値1つ)でないとき、または op 'div'
                の定数・重みに 0 が含まれるとき。

        """
        v = self.value
        if self.by is None:
            if not _is_number(v):
                msg = f"op '{self.op}' requires a numeric 'value'"
                raise ValueError(msg)
            if self.op == "div" and v == 0:
                msg = "op 'div' cannot divide by zero"
                raise ValueError(msg)
        elif isinstance(v, dict):
            if not v or not all(_is_number(x) for x in v.values()):
                msg = (
                    f"op '{self.op}' with 'by' requires 'value' as a non-empty dict of "
                    f"numbers keyed by values of '{self.by}' "
                    '(e.g. {"WLgroup00": 10.0, "WLgroup01": 1.0})'
                )
                raise ValueError(msg)
            if self.op == "div" and any(x == 0 for x in v.values()):
                msg = "op 'div' cannot divide by zero (a weight is 0)"
                raise ValueError(msg)
        elif _is_number(v):
            # スカラー重みセット(全行同一の定数)を by つきで参照した場合
            if self.op == "div" and v == 0:
                msg = "op 'div' cannot divide by zero"
                raise ValueError(msg)
        else:
            msg = (
                f"op '{self.op}' with 'by' requires 'value' as a dict of numbers "
                "(per-value weights) or a single number"
            )
            raise ValueError(msg)

    def _check_diff_shape(self) -> None:
        """Op 'diff' の value 形状検査: ちょうど2つの選択の組(順序あり)であること。

        Raises:
            ValueError: value が要素2つのリストでないとき、または選択に
                ネストしたリストが混ざっているとき。

        """
        v = self.value
        if not isinstance(v, list) or len(v) != _DIFF_SELECTIONS:
            msg = "op 'diff' requires 'value': [a, b] — exactly two selections (result = a - b)"
            raise ValueError(msg)
        if any(isinstance(x, list) for x in v):
            msg = (
                "op 'diff': each selection must be a scalar or, for combined axes, a "
                'dict like {"State": ..., "Read_Label": ...} — not a nested list'
            )
            raise ValueError(msg)

    def _normalize_multi_shape(self) -> None:
        """Mean/sum/min/max の選択の形状検査(スカラー選択はリストへ正規化)。

        Raises:
            ValueError: 選択にネストしたリストが混ざっているとき。

        """
        v = self.value
        if v is None:
            return
        if not isinstance(v, list):
            self.value = v = [v]
        if any(isinstance(x, list) for x in v):
            msg = (
                f"op '{self.op}': each selection must be a scalar or, for combined axes, "
                "a dict {axis: value} — not a nested list"
            )
            raise ValueError(msg)

    def _check_expr_shape(self) -> None:
        """Op 'expr' の検査: expr が必須で、value は取らない。

        Raises:
            ValueError: expr が無いとき、または value が指定されているとき。

        """
        if not self.expr:
            msg = "op 'expr' requires 'expr'"
            raise ValueError(msg)
        if self.value is not None:
            msg = "op 'expr' takes no 'value'; select inside the expression via by[...]"
            raise ValueError(msg)


class AxisAggregation(AggregationSpec):
    """AggregationSpec と同じ形+自分の軸名。

    denominator_pre_aggregation で使う: あちらは軸名をキーにした辞書ではなく
    リスト(順序が意味を持ち、理論上は order に無い軸も使えるため)なので、
    各ステップが軸名を自分で持つ必要がある。
    """

    axis: str


class RelativeConfig(BaseModel):
    """相対化の設定。ScorePart に `relative` ブロックが**あれば相対化する**。

    絶対値のまま計算したければブロックごと省略(またはコメントアウト)する。
    """

    split_axis: str
    numerator_when: Any
    denominator_when: Any
    # "ratio": (分子 + offset) / (分母 + offset)(デフォルト)
    # "diff":  分子 - 分母(offset は相殺されるため無関係・無視)
    mode: Literal["ratio", "diff"] = "ratio"
    denominator_offset: float = 0.0
    denominator_pre_aggregation: list[AxisAggregation] = Field(default_factory=list)
    # 表示・検証用の注記(実行には不使用)。AggregationSpec.labels と同じ形:
    # 分子/分母の選択値 → 表示名(例: {"1": "evaluation_...", "0": "reference_..."})
    labels: dict[str, str] | None = None

    @model_validator(mode="after")
    def _require_both_sides(self) -> RelativeConfig:
        """分子/分母の未指定(None)を検証時に検出する。

        None の側は「値 None の行」への等値 filter になり必ず0行マッチする —
        設定忘れをエンジンの手前で明確に検出する(UIは分子/分母未選択のまま
        保存された設定をこのエラーで表示する)。

        Returns:
            検証を通った自身(値の変更は行わない)。

        Raises:
            ValueError: numerator_when / denominator_when のどちらかが
                None のとき。

        """
        if self.numerator_when is None or self.denominator_when is None:
            msg = (
                "relative requires both numerator_when and denominator_when — choose the "
                f"values of '{self.split_axis}' that select each side"
            )
            raise ValueError(msg)
        return self


class ScorePart(BaseModel):
    """スコアパーツ1つの定義(名前・type・集計パイプライン)。"""

    name: str
    type: str
    relative: RelativeConfig | None = None
    order: list[str] = Field(default_factory=list)
    aggregations: dict[str, AggregationSpec] = Field(default_factory=dict)
    # type="custom" 専用: custom_parts.py 内の呼ぶ関数名(省略時はパーツ名と
    # 同名)と、関数に渡す params 辞書
    function: str | None = None
    params: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check_custom_fields(self) -> ScorePart:
        """集計パイプラインのフィールドと Custom 指定の混在を拒否する。

        Returns:
            検証を通った自身(値の変更は行わない)。

        Raises:
            ValueError: type="custom" なのに order / aggregations / relative
                があるとき、または custom でないのに function / params が
                あるとき。

        """
        if self.type == CUSTOM_TYPE:
            if self.order or self.aggregations or self.relative:
                msg = (
                    f"custom part '{self.name}' takes no order/aggregations/relative — "
                    "its function computes the value directly"
                )
                raise ValueError(msg)
        elif self.function is not None or self.params:
            msg = f"'function'/'params' are only valid on type='{CUSTOM_TYPE}' (part '{self.name}')"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_combined_axis_selections(self) -> ScorePart:
        """辞書選択のキーは複合軸エントリの構成軸と厳密一致すること。

        複合軸は辞書選択のみ(位置指定リストは「1組か複数選択か」が曖昧に
        なるため不可)、単一軸は辞書選択不可。

        Returns:
            検証を通った自身(値の変更は行わない)。

        Raises:
            ValueError: 単一軸エントリに辞書選択があるとき、複合軸の辞書の
                キーが構成軸と一致しないとき、または複合軸に辞書でない
                選択があるとき。

        """
        for entry in self.order:
            if entry.startswith("__"):
                continue
            axes = entry.split(COMBINED_SEP)
            spec = self.aggregations.get(entry)
            if spec is None or spec.value is None:
                continue
            selections = spec.value if isinstance(spec.value, list) else [spec.value]
            for sel in selections:
                if isinstance(sel, dict):
                    if len(axes) == 1:
                        msg = (
                            f"axis '{entry}' in '{self.name}': dict selections are only valid "
                            f"on combined axes (e.g. 'State{COMBINED_SEP}Read_Label')"
                        )
                        raise ValueError(msg)
                    if set(sel) != set(axes):
                        msg = f"combined axis '{entry}' in '{self.name}' expects keys {axes}, got {sorted(sel)}"
                        raise ValueError(msg)
                elif len(axes) > 1:
                    msg = (
                        f"combined axis '{entry}' in '{self.name}': each selection must be a "
                        f"dict naming its axes, e.g. {{{', '.join(repr(a) + ': ...' for a in axes)}}}; "
                        f"got {sel!r}"
                    )
                    raise ValueError(msg)
        return self

    def resolve_selection_refs(
        self,
        selection_sets: dict[str, list[Any]],
        weight_sets: dict[str, Any] | None = None,
    ) -> ScorePart:
        """全 `ref` を参照先のセットの中身で置き換えたコピーを返す。

        通常opの ref は選択セット、変換op(TRANSFORM_OPS)の ref は重みセット
        (optimization.WLgroupWeight / weightSets)から解決する。
        パーツ全体を再検証するので、解決後の選択はインラインで書いた場合と
        全く同じ検査を通る。

        Returns:
            解決後の内容で再検証した新しい ScorePart(ref / weight_ref が
            1つも無ければ自身をそのまま返す)。

        """
        weight_sets = weight_sets or {}
        specs = list(self.aggregations.values())
        if self.relative:
            specs += list(self.relative.denominator_pre_aggregation)
        if not any(s.ref is not None or s.weight_ref is not None for s in specs):
            return self

        data = self.model_dump()
        for spec_dict in data["aggregations"].values():
            self._resolve_refs_in_spec(spec_dict, selection_sets, weight_sets)
        if data.get("relative"):
            for step_dict in data["relative"]["denominator_pre_aggregation"]:
                self._resolve_refs_in_spec(step_dict, selection_sets, weight_sets)
        return ScorePart.model_validate(data)

    def _resolve_refs_in_spec(
        self,
        spec_dict: dict[str, Any],
        selection_sets: dict[str, list[Any]],
        weight_sets: dict[str, Any],
    ) -> None:
        """1つの集計指示 dict の weight_ref / ref を参照先の中身へ展開する。

        spec_dict はその場で書き換え、展開後の weight_ref / ref は None に
        戻す(インラインで書いた場合と同じ形にする)。resolve_selection_refs
        専用の下請けで、model_dump() した dict に対して使う。

        Raises:
            ValueError: 参照先の重みセット・選択セットが定義されていない
                とき。

        """
        wref = spec_dict.get("weight_ref")
        if wref is not None:
            if wref not in weight_sets:
                msg = (
                    f"score part '{self.name}': unknown weight set '{wref}' "
                    f"(defined weight sets: {sorted(weight_sets)})"
                )
                raise ValueError(msg)
            spec_dict["weight"] = deepcopy(weight_sets[wref])
            spec_dict["weight_ref"] = None
        ref = spec_dict.get("ref")
        if ref is None:
            return
        if spec_dict.get("op") in TRANSFORM_OPS:
            if ref not in weight_sets:
                msg = (
                    f"score part '{self.name}': unknown weight set '{ref}' "
                    f"(defined weight sets: {sorted(weight_sets)})"
                )
                raise ValueError(msg)
            spec_dict["value"] = deepcopy(weight_sets[ref])
        else:
            if ref not in selection_sets:
                msg = (
                    f"score part '{self.name}': unknown selection set '{ref}' (defined sets: {sorted(selection_sets)})"
                )
                raise ValueError(msg)
            spec_dict["value"] = deepcopy(selection_sets[ref])
        spec_dict["ref"] = None


class GroupDef(BaseModel):
    """グループ派生軸の定義: 元軸の整数範囲でグループ名を割り当てる。

    例: WLgroup: WL 0-3 → "WLgroup01"。グループ列はデータ読み込み時に
    作られるため、定義名をパーツの `order` に置いて任意の位置で普通の軸と
    同様に集計できる(例: WL mean → Board max → WLgroup max)。
    定義名は元軸名と同名にできない。

    definedInLogical=False のときは、範囲が Physical 番号で書かれている
    ことを表す(データの csv は Logical 番号。軸の総数 N に対して
    Physical p ↔ Logical N-1-p)。計算前に resolved_groups() で Logical 範囲へ
    読み替えてから使う。N は {Generation}.json(numWLs / numStrings)から
    取る(cli.resolve_group_defs)。
    """

    axis: str
    groups: dict[str, tuple[int, int]] = Field(default_factory=dict)
    # mixedCase は設定 jsonc のキー名そのもの(pydantic がこの名前で読み書きし、改名すると既存の設定が読めなくなる)
    definedInLogical: bool = True  # ruff: ignore[N815]

    def resolved_groups(self, axis_count: int | None = None) -> dict[str, tuple[int, int]]:
        """Logical 番号での範囲。Physical 定義は [lo, hi] → [N-1-hi, N-1-lo]。

        Returns:
            {グループ名: (下限, 上限)}(Logical 定義は groups のコピー、
            Physical 定義は axis_count で読み替えた範囲)。

        Raises:
            ValueError: Physical 記法の定義なのに axis_count が与えられ
                なかったとき。

        """
        if self.definedInLogical:
            return dict(self.groups)
        if axis_count is None:
            msg = (
                f"group def on axis '{self.axis}' is defined in physical numbering "
                "(definedInLogical=false) — the axis count (e.g. numWLs from "
                "{Generation}.json) is required to convert it"
            )
            raise ValueError(msg)
        return {name: (axis_count - 1 - hi, axis_count - 1 - lo) for name, (lo, hi) in self.groups.items()}


class ConstraintThresholdEntry(BaseModel):
    """constraintThreshold の1エントリ(しきい値と付帯情報)。"""

    value: float
    active: str | None = None
    type: str | None = None
    coef: float | None = None


class ScoreFile(BaseModel):
    """ユーザが作る内容一式: スコアパーツ + 合成式 + 制約。

    独立ファイルとして持ち続けるのではなく、実行時 config の
    `optimization{}` ブロックにマージされる(下の RunConfig 参照)。
    """

    score_parts: list[ScorePart] = Field(default_factory=list)
    expression: str = ""
    # mixedCase は設定 jsonc のキー名そのもの(pydantic がこの名前で読み書きし、改名すると既存の設定が読めなくなる)
    constraintThreshold: dict[str, ConstraintThresholdEntry] = Field(default_factory=dict)  # ruff: ignore[N815]
    # パーツが `ref` やグループ派生軸を使っていてもエクスポートが自己完結する
    # よう、選択セット・グループ定義・重みセットを同梱する
    selectionSets: dict[str, list[Any]] = Field(default_factory=dict)  # ruff: ignore[N815]
    groupDefs: dict[str, GroupDef] = Field(default_factory=dict)  # ruff: ignore[N815]
    # 変換op(TRANSFORM_OPS)の ref が参照する名前付き重み: 値は数値
    # (全行同一の定数)か {軸の値: 数値}(by 軸の値ごとの定数)
    weightSets: dict[str, Any] = Field(default_factory=dict)  # ruff: ignore[N815]

    @model_validator(mode="before")
    @classmethod
    def _absorb_wlgroup_keys(cls, data: object) -> object:
        """score.jsonc 単体形式でも WLgroup 系キーを受ける(0.7.0)。

        対象キーは WLgroup / WLgroupDefinLogical / WLgroupWeight —
        実験スクリプトが読む config の正式な形式。groupDefs / weightSets は
        任意の軸(STR 等)のグループ分割へ一般化した内部の入れ物で、WL の
        定義もそこへ移し替えて同じ仕組みで処理する。

        UI のエクスポートは WL 軸の "WLgroup" 定義を **WLgroup 系キーだけ**に
        書く(groupDefs と二重にしない): 合成後 config では実験スクリプトが
        読む optimization.WLgroup がそのまま編集後の内容になり、手編集でも
        「どちらが使われるか」の迷いが生じない(定義の在り処は常に1つ)。
        groupDefs / weightSets に同名があればそちらが勝つ(RunConfig の
        group_defs() / weight_sets() と同じ優先順位)。

        Returns:
            WLgroup 系キーを groupDefs / weightSets へ移し替えた入力データ
            (対象キーが無ければそのまま)。

        Raises:
            ValueError: WLgroupDefinLogical が "true"/"false" 以外の文字列の
                とき。

        """
        if not isinstance(data, dict) or not any(
            k in data for k in ("WLgroup", "WLgroupDefinLogical", "WLgroupWeight")
        ):
            return data
        data = dict(data)
        wl = data.pop("WLgroup", None)
        din = data.pop("WLgroupDefinLogical", True)
        ww = data.pop("WLgroupWeight", None)
        if isinstance(din, str):
            low = din.strip().lower()
            if low not in {"true", "false"}:
                msg = f"WLgroupDefinLogical must be true or false, got {din!r}"
                raise ValueError(msg)
            din = low == "true"
        if wl:
            defs = data.setdefault("groupDefs", {})
            if "WLgroup" not in defs:
                defs["WLgroup"] = {"axis": "WL", "groups": wl, "definedInLogical": bool(din)}
        if ww is not None:
            data.setdefault("weightSets", {}).setdefault("WLgroupWeight", ww)
        return data


def _is_weight(w: object) -> bool:
    return _is_number(w) or (isinstance(w, dict) and bool(w) and all(_is_number(x) for x in w.values()))


class VthSkipConfig(BaseModel):
    """実験 config の `optimization.vthSkip`(測定フロー側の既存項目)。

    フローは指定 epoch 数まで KLD / dVthSGWLD を測定しない(ファイルが出力
    されない)。エンジンは epochs は使わず「パーツの type ファイルが無い」を
    トリガーに、ここのダミー値で計算する(docs/score_gui_design.md 参照)。
    ダミー値は「変換後の値」: 変換ステップ(__log__ 等)はスキップされ、
    集計(選択リスト・集計時重み・sum/mean)だけが通常どおり適用される。
    """

    epochs: int | None = None
    # mixedCase は実験 config のキー名そのもの(pydantic がこの名前で読み書きし、改名すると既存の設定が読めなくなる)
    dummyKLDValue: float | None = None  # ruff: ignore[N815]
    dummyDVthValue: float | None = None  # ruff: ignore[N815]

    def dummy_values(self) -> dict[str, float]:
        """Type 名 → ダミー値(設定されているものだけ)。

        Returns:
            {測定 type 名: float に揃えたダミー値}。dummyKLDValue 等が
            None の type は含まれない。

        """
        out: dict[str, float] = {}
        for type_, key in VTHSKIP_TYPE_KEYS.items():
            v = getattr(self, key)
            if v is not None:
                out[type_] = float(v)
        return out


# vthSkip のフロー側キー名 → 対応する測定 type(フローの命名慣習に固定で対応。
# 他の type にもダミーが必要になったら、パーツ単位の汎用フィールドを検討する)
VTHSKIP_TYPE_KEYS = {"KLD": "dummyKLDValue", "dVthSGWLD": "dummyDVthValue"}


class OptimizationConfig(BaseModel):
    """実行時 config の `optimization{}` ブロック。"""

    score_function: str | None = None
    # mixedCase は実験 config のキー名そのもの(pydantic がこの名前で読み書きし、改名すると既存の設定が読めなくなる)
    # 測定フロー側の vthSkip 設定(あれば): ファイル不在時のダミー値の出所
    vthSkip: VthSkipConfig | None = None  # ruff: ignore[N815]
    constraintThreshold: dict[str, ConstraintThresholdEntry] = Field(default_factory=dict)  # ruff: ignore[N815]
    WLgroup: dict[str, tuple[int, int]] = Field(default_factory=dict)
    # WLgroup の範囲の記法: True(既定)= Logical 番号、False = Physical 番号
    # (データの Logical 番号を N-1-p で読み替える。N は {Generation}.json の
    # numWLs)。現行スクリプト互換で "True"/"False" の文字列も受ける
    WLgroupDefinLogical: bool = True
    # WLgroup 各グループの重み({グループ名: 数値})または全グループ共通の
    # 数値1つ。名前 "WLgroupWeight" の重みセットとして ref から参照できる
    WLgroupWeight: Any | None = None
    weightSets: dict[str, Any] = Field(default_factory=dict)  # ruff: ignore[N815]
    selectionSets: dict[str, list[Any]] = Field(default_factory=dict)  # ruff: ignore[N815]
    groupDefs: dict[str, GroupDef] = Field(default_factory=dict)  # ruff: ignore[N815]
    score_parts: list[ScorePart] = Field(default_factory=list)
    expression: str = ""

    @model_validator(mode="before")
    @classmethod
    def _parse_bool_strings(cls, data: object) -> object:
        """現行 config は真偽値を "True"/"False" 文字列で書く流儀があるので吸収する。

        Returns:
            WLgroupDefinLogical を bool へ変換した入力データ(文字列で
            なければそのまま)。

        Raises:
            ValueError: WLgroupDefinLogical が "true"/"false" 以外の文字列の
                とき。

        """
        if isinstance(data, dict) and isinstance(data.get("WLgroupDefinLogical"), str):
            low = data["WLgroupDefinLogical"].strip().lower()
            if low not in {"true", "false"}:
                msg = f"WLgroupDefinLogical must be true or false, got {data['WLgroupDefinLogical']!r}"
                raise ValueError(msg)
            data = {**data, "WLgroupDefinLogical": low == "true"}
        return data

    @model_validator(mode="after")
    def _check_weights(self) -> OptimizationConfig:
        for name, w in {
            **({"WLgroupWeight": self.WLgroupWeight} if self.WLgroupWeight is not None else {}),
            **self.weightSets,
        }.items():
            if not _is_weight(w):
                msg = f"weight set '{name}' must be a number or a non-empty dict {{value: number}}, got {w!r}"
                raise ValueError(msg)
        return self


class RunConfig(BaseModel):
    """エンジンが計算時に読む config.jsonc(sample.jsonc の形)。"""

    Generation: str
    optimization: OptimizationConfig

    def to_score_file(self) -> ScoreFile:
        """設定の optimization ブロックの内容を ScoreFile として取り出す。

        Returns:
            WLgroup 系キーも統合済みの groupDefs / weightSets を同梱した
            ScoreFile。

        """
        return ScoreFile(
            score_parts=self.optimization.score_parts,
            expression=self.optimization.expression,
            constraintThreshold=self.optimization.constraintThreshold,
            selectionSets=self.optimization.selectionSets,
            # WLgroup 系キーも統合した全定義(weightSets と対称 — 0.7.0 で修正。
            # エンジンの計算は resolve_group_defs → group_defs() を使うため
            # 挙動は不変で、UI が RunConfig を取り込む経路の取りこぼしを塞ぐ)
            groupDefs=self.group_defs(),
            weightSets=self.weight_sets(),
        )

    def group_defs(self) -> dict[str, GroupDef]:
        """全グループ定義(optimization.WLgroup + groupDefs)。

        optimization.WLgroup(実験スクリプト形式)は暗黙に「WLに対する定義」
        として読む(記法は WLgroupDefinLogical に従う)。名前が衝突したら
        groupDefs が勝つ。

        Returns:
            {定義名: GroupDef}(optimization.WLgroup は "WLgroup" という
            名前で含まれる)。

        """
        defs: dict[str, GroupDef] = {}
        if self.optimization.WLgroup:
            defs["WLgroup"] = GroupDef(
                axis="WL",
                groups=self.optimization.WLgroup,
                definedInLogical=self.optimization.WLgroupDefinLogical,
            )
        defs.update(self.optimization.groupDefs)
        return defs

    def weight_sets(self) -> dict[str, Any]:
        """全重みセット(optimization.WLgroupWeight + weightSets)。

        optimization.WLgroupWeight(実験スクリプト形式)は "WLgroupWeight"
        という名前のセットとして読む。衝突は weightSets が勝つ。

        Returns:
            {セット名: 重み(数値または {軸の値: 数値})}(WLgroupWeight は
            "WLgroupWeight" という名前で含まれる)。

        """
        sets: dict[str, Any] = {}
        if self.optimization.WLgroupWeight is not None:
            sets["WLgroupWeight"] = self.optimization.WLgroupWeight
        sets.update(self.optimization.weightSets)
        return sets


class DvtBudgetCoefEntry(BaseModel):
    """dVtBudget 係数表のリーフ {a, b}。"""

    a: float
    b: float


# 世代 → 温度(文字列キー。例: "-30", "85") → State → {a, b}
class DvtBudgetCoefFile(RootModel[dict[str, dict[str, dict[str, DvtBudgetCoefEntry]]]]):
    """dVtBudget 係数表: 世代 → 温度(文字列キー) → State → {a, b}。"""
