"""スコア／スコアパーツ定義のデータモデル（pydantic）。

設計の経緯は docs/score_gui_design.md 3〜6節を参照。
検証ルールはすべてここに集約し、UIも同じモデルで検証する（二重実装しない）。
エラーメッセージは実行環境を選ばないよう英語のまま（UIがそのまま表示する）。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, RootModel, model_validator

# order エントリの複合軸の区切り（例: "State&Read_Label"）
COMBINED_SEP = "&"

# 通常のパイプラインの代わりにユーザ定義のPython関数
# （custom_parts.py — scorelib_param/custom.py 参照）を呼ぶ ScorePart.type の値
CUSTOM_TYPE = "custom"

AggOp = Literal[
    "filter",
    # mean/sum/min/max は軸を潰す集計。任意の `value` リストを付けると、
    # 先にその選択集合へ限定してから集計する
    # （例: {"op": "sum", "value": [0, 1]} は軸が 0 か 1 の行だけの和）
    "mean",
    "sum",
    "min",
    "max",
    "expr",
    # ちょうど2つの選択の組（順序あり）で軸を潰す:
    # {"op": "diff", "value": [a, b]} -> value(a) - value(b)
    "diff",
    # 変換op: 軸を潰さず値列に行単位で適用する。order の仮想ステップ
    # "__xxx__"（例: __offset__）が使う
    "add",
]

# `value` が通常opの修飾子になる前の旧表記（読み込み時に自動変換）
_SUBSET_ALIASES = {
    "mean_subset": "mean",
    "sum_subset": "sum",
    "min_subset": "min",
    "max_subset": "max",
}

# 任意の選択リストを `value` に取れる集計op。UI（ui/widgets.py）と共有して
# 両者が食い違わないようにする
MULTI_OPS = ("mean", "sum", "min", "max")


class AggregationSpec(BaseModel):
    """1つの軸（または仮想ステップ）の集計指示。

    選択は op によらず常に `value` に書く:
    - スカラー = 軸の値1つの選択（{"op": "filter", "value": "A2B"}）
    - 辞書 = 複合軸上の1つの組み合わせ
      （{"op": "filter", "value": {"State": "A2B", "Read_Label": "..."}}）
    - リスト = 常に選択の並び（{"op": "diff", "value": ["R2A", "B2A"]}）
    op ごとに違うのは必要な選択の個数だけ（filter: 1、diff: 2、
    mean/sum/min/max: 任意個または無し）。互換のため `values` も
    `value` の別名として受ける。
    """

    op: AggOp
    value: Optional[Any] = None
    # インラインの `value` の代わりに使う、名前付き選択セット
    # （optimization.selectionSets）への参照。計算前に解決され、解決後の
    # 内容はインラインで書いた場合と全く同じ形状検査を通る
    ref: Optional[str] = None
    expr: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_spellings(cls, data):
        """旧表記の吸収: *_subset は自動変換、values は value の別名。
        廃止した group_reduce は移行案内つきでエラーにする。"""
        if isinstance(data, dict):
            if data.get("op") == "group_reduce":
                raise ValueError(
                    "op 'group_reduce' has been removed; define the group in groupDefs and "
                    "put its name (e.g. 'WLgroup') in `order` as a derived axis instead "
                    "(inner op on the source axis, outer op on the group axis)"
                )
            if data.get("op") in _SUBSET_ALIASES:
                data = {**data, "op": _SUBSET_ALIASES[data["op"]]}
            if data.get("values") is not None:
                if data.get("value") is not None:
                    raise ValueError("give selections in 'value' ('values' is an alias) — not both")
                values = data["values"]
                data = {k: v for k, v in data.items() if k != "values"}
                data["value"] = values
        return data

    @model_validator(mode="after")
    def _check_value_shape(self):
        """op ごとの value 形状検査（間違えやすい箇所なのでエラーは具体的に）。"""
        op, v = self.op, self.value
        if self.ref is not None:
            if v is not None:
                raise ValueError("give either 'value' or 'ref' (a named selection set), not both")
            if op in ("add", "expr"):
                raise ValueError(f"op '{op}' takes no selections, so 'ref' is not applicable")
            # 形状検査は ref 解決後にもう一度走る
            return self
        if op == "filter":
            if isinstance(v, list):
                if len(v) == 1 and not isinstance(v[0], list):
                    self.value = v[0]
                else:
                    raise ValueError(
                        "op 'filter' selects exactly one value (a scalar, or a dict for "
                        "combined axes); to reduce over several values use mean/sum/min/max"
                    )
            elif v is None:
                raise ValueError("op 'filter' requires 'value'")
        elif op == "add":
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise ValueError("op 'add' requires a numeric 'value'")
        elif op == "diff":
            if not isinstance(v, list) or len(v) != 2:
                raise ValueError(
                    "op 'diff' requires 'value': [a, b] — exactly two selections (result = a - b)"
                )
            if any(isinstance(x, list) for x in v):
                raise ValueError(
                    "op 'diff': each selection must be a scalar or, for combined axes, a "
                    "dict like {\"State\": ..., \"Read_Label\": ...} — not a nested list"
                )
        elif op in MULTI_OPS:
            if v is not None:
                if not isinstance(v, list):
                    self.value = v = [v]
                if any(isinstance(x, list) for x in v):
                    raise ValueError(
                        f"op '{op}': each selection must be a scalar or, for combined axes, "
                        "a dict {axis: value} — not a nested list"
                    )
        elif op == "expr":
            if not self.expr:
                raise ValueError("op 'expr' requires 'expr'")
            if v is not None:
                raise ValueError("op 'expr' takes no 'value'; select inside the expression via by[...]")
        return self


class AxisAggregation(AggregationSpec):
    """AggregationSpec と同じ形+自分の軸名。

    denominator_pre_aggregation で使う: あちらは軸名をキーにした辞書ではなく
    リスト（順序が意味を持ち、理論上は order に無い軸も使えるため）なので、
    各ステップが軸名を自分で持つ必要がある。
    """

    axis: str


class RelativeConfig(BaseModel):
    """相対化の設定。ScorePart に `relative` ブロックが**あれば相対化する**。
    絶対値のまま計算したければブロックごと省略（またはコメントアウト）する。
    `enabled` フラグは存在しない。"""

    split_axis: str
    numerator_when: Any
    denominator_when: Any
    # "ratio": (分子 + offset) / (分母 + offset)（デフォルト）
    # "diff":  分子 - 分母（offset は相殺されるため無関係・無視）
    mode: Literal["ratio", "diff"] = "ratio"
    denominator_offset: float = 0.0
    denominator_pre_aggregation: List[AxisAggregation] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_enabled(cls, data):
        if isinstance(data, dict) and "enabled" in data:
            data = dict(data)
            enabled = data.pop("enabled")
            # 残骸の `enabled: true` は無害なので黙って捨てる。
            # `enabled: false` を黙って「有効」にしてはならない: 大声で失敗する
            if not enabled or str(enabled).strip().lower() == "false":
                raise ValueError(
                    "relative.enabled has been removed; to compute without "
                    "relative-ization, delete (or comment out) the whole relative block"
                )
        return data


class ScorePart(BaseModel):
    name: str
    type: str
    relative: Optional[RelativeConfig] = None
    order: List[str] = Field(default_factory=list)
    aggregations: Dict[str, AggregationSpec] = Field(default_factory=dict)
    # type="custom" 専用: custom_parts.py 内の呼ぶ関数名（省略時はパーツ名と
    # 同名）と、関数に渡す params 辞書
    function: Optional[str] = None
    params: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def _check_custom_fields(self):
        """custom と集計パイプラインのフィールド混在を拒否する。"""
        if self.type == CUSTOM_TYPE:
            if self.order or self.aggregations or self.relative:
                raise ValueError(
                    f"custom part '{self.name}' takes no order/aggregations/relative — "
                    "its function computes the value directly"
                )
        elif self.function is not None or self.params:
            raise ValueError(
                f"'function'/'params' are only valid on type='{CUSTOM_TYPE}' (part '{self.name}')"
            )
        return self

    @model_validator(mode="after")
    def _check_combined_axis_selections(self):
        """辞書選択のキーは複合軸エントリの構成軸と厳密一致すること。
        複合軸は辞書選択のみ（位置指定リストは「1組か複数選択か」が曖昧に
        なるため不可）、単一軸は辞書選択不可。"""
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
                        raise ValueError(
                            f"axis '{entry}' in '{self.name}': dict selections are only valid "
                            f"on combined axes (e.g. 'State{COMBINED_SEP}Read_Label')"
                        )
                    if set(sel) != set(axes):
                        raise ValueError(
                            f"combined axis '{entry}' in '{self.name}' expects keys {axes}, "
                            f"got {sorted(sel)}"
                        )
                elif len(axes) > 1:
                    raise ValueError(
                        f"combined axis '{entry}' in '{self.name}': each selection must be a "
                        f"dict naming its axes, e.g. {{{', '.join(repr(a) + ': ...' for a in axes)}}}; "
                        f"got {sel!r}"
                    )
        return self

    def resolve_selection_refs(self, selection_sets: Dict[str, List[Any]]) -> "ScorePart":
        """全 `ref` を参照先の選択セットの中身で置き換えたコピーを返す。
        パーツ全体を再検証するので、解決後の選択はインラインで書いた場合と
        全く同じ検査を通る。"""
        specs = list(self.aggregations.values())
        if self.relative:
            specs += list(self.relative.denominator_pre_aggregation)
        if not any(s.ref is not None for s in specs):
            return self

        def _resolve(spec_dict: dict) -> None:
            ref = spec_dict.get("ref")
            if ref is None:
                return
            if ref not in selection_sets:
                raise ValueError(
                    f"score part '{self.name}': unknown selection set '{ref}' "
                    f"(defined sets: {sorted(selection_sets)})"
                )
            spec_dict["value"] = deepcopy(selection_sets[ref])
            spec_dict["ref"] = None

        data = self.model_dump()
        for spec_dict in data["aggregations"].values():
            _resolve(spec_dict)
        if data.get("relative"):
            for step_dict in data["relative"]["denominator_pre_aggregation"]:
                _resolve(step_dict)
        return ScorePart.model_validate(data)


class GroupDef(BaseModel):
    """グループ派生軸の定義: 元軸の整数範囲でグループ名を割り当てる
    （例: WLgroup: WL 0-3 → "WLgroup01"）。グループ列はデータ読み込み時に
    作られるため、定義名をパーツの `order` に置いて任意の位置で普通の軸と
    同様に集計できる（例: WL mean → Board max → WLgroup max）。
    定義名は元軸名と同名にできない。"""

    axis: str
    groups: Dict[str, Tuple[int, int]] = Field(default_factory=dict)


class ConstraintThresholdEntry(BaseModel):
    value: float
    active: Optional[str] = None
    type: Optional[str] = None
    coef: Optional[float] = None


class ScoreFile(BaseModel):
    """ユーザが作る内容一式: スコアパーツ + 合成式 + 制約。

    独立ファイルとして持ち続けるのではなく、実行時 config の
    `optimization{}` ブロックにマージされる（下の RunConfig 参照）。
    """

    score_parts: List[ScorePart] = Field(default_factory=list)
    expression: str = ""
    constraintThreshold: Dict[str, ConstraintThresholdEntry] = Field(default_factory=dict)
    # パーツが `ref` やグループ派生軸を使っていてもエクスポートが自己完結する
    # よう、選択セットとグループ定義を同梱する
    selectionSets: Dict[str, List[Any]] = Field(default_factory=dict)
    groupDefs: Dict[str, GroupDef] = Field(default_factory=dict)


class OptimizationConfig(BaseModel):
    score_function: Optional[str] = None
    constraintThreshold: Dict[str, ConstraintThresholdEntry] = Field(default_factory=dict)
    WLgroup: Dict[str, Tuple[int, int]] = Field(default_factory=dict)
    selectionSets: Dict[str, List[Any]] = Field(default_factory=dict)
    groupDefs: Dict[str, GroupDef] = Field(default_factory=dict)
    score_parts: List[ScorePart] = Field(default_factory=list)
    expression: str = ""


class RunConfig(BaseModel):
    """エンジンが計算時に読む config.jsonc（sample.jsonc の形）。"""

    Generation: str
    optimization: OptimizationConfig

    def to_score_file(self) -> ScoreFile:
        return ScoreFile(
            score_parts=self.optimization.score_parts,
            expression=self.optimization.expression,
            constraintThreshold=self.optimization.constraintThreshold,
            selectionSets=self.optimization.selectionSets,
            groupDefs=self.optimization.groupDefs,
        )

    def group_defs(self) -> Dict[str, GroupDef]:
        """全グループ定義: 旧来の optimization.WLgroup（暗黙に「WLに対する
        定義」として読む）+ groupDefs。名前が衝突したら groupDefs が勝つ。"""
        defs: Dict[str, GroupDef] = {}
        if self.optimization.WLgroup:
            defs["WLgroup"] = GroupDef(axis="WL", groups=self.optimization.WLgroup)
        defs.update(self.optimization.groupDefs)
        return defs


class DvtBudgetCoefEntry(BaseModel):
    a: float
    b: float


# 世代 → 温度（文字列キー。例: "-30", "85"） → State → {a, b}
class DvtBudgetCoefFile(RootModel[Dict[str, Dict[str, Dict[str, DvtBudgetCoefEntry]]]]):
    pass
