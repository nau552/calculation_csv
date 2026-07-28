"""CLI エントリポイント: 実epochデータから Score + 全スコアパーツの値を計算する。
現行最適化スクリプト（python3.7）の `get_score()` からサブプロセスとして
起動される想定（docs/score_gui_design.md 2節・7節）。

    python -m scorelib_param.cli --config config.jsonc --data-dir <epoch_dir> \
        [--dvtbudget-coef coef.jsonc] [--initial-temperature initial_temperature.csv]

stdout に JSON オブジェクトを1つだけ出力する: {"Score": ..., "<パーツ名>": ..., ...}
（InBatchEpoch 列は出さない — 出力契約は docs/score_gui_design.md 5節・7節）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Optional, Set

import polars as pl

from . import axis_resolve, custom, io_jsonc
from .aggregate import (
    apply_aggregations,
    apply_transform,
    collapse,
    collapse_to_scalar,
    group_column_expr,
)
from .dvtbudget import apply_dvtbudget, load_board_temperatures
from .expression import evaluate_expression
from .models import COMBINED_SEP, CUSTOM_TYPE, DvtBudgetCoefFile, GroupDef, RunConfig, ScorePart
from .relative import apply_relative

# order に軸名と並べて置ける仮想エントリ（docs/score_gui_design.md 4.1節）。
# "__" 始まりのエントリは軸ではなくパイプラインステップ:
# - RELATIVE_STEP: 相対化を実行する位置（省略時は先頭）
# - DVTBUDGET_STEP: dVtBudget 変換を実行する位置（省略時は相対化の直後）。
#   その時点で Board/State がまだ潰されていない必要がある
# - それ以外の "__xxx__": 値列への行単位変換。指示は同名キーで aggregations に
#   置く（例: "__offset__": {"op": "add", "value": 1}）
RELATIVE_STEP = "__relative__"
DVTBUDGET_STEP = "__dvtbudget__"

# order エントリは複数の軸を1つの複合軸に束ねられる（例: "State&Read_Label"）。
# その集計指示は辞書選択を取る:
#   {"op": "sum", "value": [{"State": "R2A", "Read_Label": "read_level_upper1"},
#                           {"State": "A2R", "Read_Label": "read_level_lower1"}]}
# 束ねた軸は1つの軸として一緒に潰れるので、filter/sum/diff/expr がすべて
# (State, Read_Label) の組に対して働く。軸の値に "&" を含んではならない。


def _is_virtual(step: str) -> bool:
    return step.startswith("__")


def _step_axes(step: str) -> list[str]:
    return step.split(COMBINED_SEP)


def _named_axes(score_part: ScorePart) -> Set[str]:
    """パーツ自身が言及する軸的な名前の集合（order エントリ=複合軸の構成軸
    込み、相対化の split 軸、分母事前集計の軸）。グループ派生軸名を含みうる。

    ui/state.py の _part_axis_names は「編集途中の（不完全かもしれない）dict」
    を対象にした対になる実装 — 意図的な並行であり、統合を試みないこと。"""
    axes: Set[str] = set()
    for entry in score_part.order:
        if not _is_virtual(entry):
            axes.update(_step_axes(entry))
    for spec in score_part.aggregations.values():
        if spec.by:
            axes.add(spec.by)  # 変換ステップの重みが参照する軸（グループ派生軸名も可）
    if score_part.relative:
        axes.add(score_part.relative.split_axis)
        for step in score_part.relative.denominator_pre_aggregation:
            axes.add(step.axis)
            if step.by:
                axes.add(step.by)
    return axes


def _referenced_group_defs(
    score_part: ScorePart, group_defs: Optional[Dict[str, GroupDef]]
) -> Dict[str, GroupDef]:
    """このパーツが派生軸として実際に使うグループ定義。"""
    if not group_defs:
        return {}
    used = {n: group_defs[n] for n in _named_axes(score_part) if n in group_defs}
    for name, gd in used.items():
        if gd.axis == name:
            raise ValueError(
                f"group def '{name}' must not have the same name as its source axis"
            )
    return used


def _required_axes(
    score_part: ScorePart, group_defs: Optional[Dict[str, GroupDef]] = None
) -> Set[str]:
    """csv/map から実際に読み込むべき軸: グループ派生軸名はその元軸に
    読み替える（グループ列は読み込み後に元軸から作られる）。"""
    named = _named_axes(score_part)
    derived = _referenced_group_defs(score_part, group_defs)
    axes = {a for a in named if a not in derived} | {gd.axis for gd in derived.values()}
    if score_part.type == "dVtBudget":
        axes.update({"Board", "State"})
    return axes


def _with_group_columns(
    lf, score_part: ScorePart, group_defs: Optional[Dict[str, GroupDef]]
):
    """このパーツが参照するグループ派生列を生成する。以降は普通の軸として
    集計される。派生のためだけに読み込んだ元軸は再び落とす: パーツ自身の
    エントリに無い軸は暗黙集約（混ぜる）が仕様であり、列が残ると最終の
    collapse がエラーになってしまうため。"""
    derived = _referenced_group_defs(score_part, group_defs)
    if not derived:
        return lf
    for name, gd in derived.items():
        if not gd.definedInLogical:
            raise ValueError(
                f"group def '{name}' is still in physical numbering — resolve it to "
                "logical ranges first (cli.resolve_group_defs reads numWLs etc. from "
                "{Generation}.json and converts)"
            )
    lf = lf.with_columns(
        [group_column_expr(gd.axis, gd.groups).alias(name) for name, gd in derived.items()]
    )
    # どの範囲にも入らない行は「名無し(null)グループ」として静かに混ざって
    # しまう — ほぼ確実に定義の古さが原因なので、該当値の一覧つきで失敗させる
    for name, gd in derived.items():
        uncovered = lf.filter(pl.col(name).is_null()).select(pl.col(gd.axis).unique()).collect()
        if uncovered.height:
            vals = sorted(uncovered[gd.axis].to_list())
            raise ValueError(
                f"values of axis '{gd.axis}' not covered by any group of '{name}': {vals} "
                f"(extend the group ranges or filter those values out first)"
            )
    keep = {a for a in _named_axes(score_part) if a not in derived}
    if score_part.type == "dVtBudget":
        keep.update({"Board", "State"})
    drop = {gd.axis for gd in derived.values() if gd.axis not in keep}
    return lf.drop(drop) if drop else lf


def _combined_key(v) -> str:
    return ("true" if v else "false") if isinstance(v, bool) else str(v)


def _combine_selection(sel: dict, axes: list[str]) -> str:
    """辞書選択1つ（ScorePart 検証済み）を、融合列に一致する内部の
    連結キー文字列へ変換する。"""
    return COMBINED_SEP.join(_combined_key(sel[a]) for a in axes)


def _effective_order(score_part: ScorePart) -> list[str]:
    """ユーザが明示配置しなかった暗黙のパイプラインステップを補完する:
    相対化は先頭、dVtBudget 変換は相対化の直後。"""
    order = list(score_part.order)
    relative_enabled = score_part.relative is not None

    if RELATIVE_STEP in order and not relative_enabled:
        raise ValueError(f"'{RELATIVE_STEP}' in order but '{score_part.name}' has no relative config")
    if relative_enabled and RELATIVE_STEP not in order:
        order.insert(0, RELATIVE_STEP)

    if score_part.type == "dVtBudget" and DVTBUDGET_STEP not in order:
        pos = order.index(RELATIVE_STEP) + 1 if RELATIVE_STEP in order else 0
        order.insert(pos, DVTBUDGET_STEP)
    if DVTBUDGET_STEP in order and score_part.type != "dVtBudget":
        raise ValueError(f"'{DVTBUDGET_STEP}' in order but type of '{score_part.name}' is not dVtBudget")
    return order


def _hoistable_prefilters(
    score_part: ScorePart, group_defs: Optional[Dict[str, GroupDef]] = None
) -> list[tuple[str, object]]:
    """パイプラインの先頭に安全に前出しできる filter の列 [(軸, 値), ...]。

    軸 X の filter は X を潰さない演算すべてと可換: グループキーは常に
    「残っている全列」なので、他軸の集計・行単位変換（__offset__ 等・
    dVtBudget 変換）・相対化のペアリングは X の値ごとに独立した世界で
    計算され、X==v の世界を先に切り出しても結果は変わらない。そこで
    order 内の位置や __relative__ の明示/暗黙によらず、可換な filter は
    全行の相対化・変換が走る前に行だけ先に絞る。列は落とさない（列は
    本来の filter ステップが本来の位置で落とすので、__dvtbudget__ 等が
    途中で参照する列も欠けず、列を潰す順序の検証も従来どおり働く）。

    可換にならない軸だけを除外する:
    - relative.split_axis（分子/分母の振り分けに使う）
    - denominator_pre_aggregation で潰す軸・その重み参照軸（`by`）。
      分母は「全値の集計 vs 絞った値の集計」で結果が変わるため。
      グループ派生軸は元軸と紐づけて双方向に判定する
      （例: WL を事前集計するなら WLgroup の filter も前に出さない）
    - 複合軸エントリ（"A&B"）の構成軸（filter は組に対して働くため）

    対象は単一軸の純粋な filter（op="filter"）のみ。selection ref は
    解決済みの ScorePart を渡すこと。

    診断への影響: 行が先に減るため、後段ステップの検証が「filter で残る
    行」だけを対象にするようになる（例: dVtBudget 係数は filter 後に残る
    State の分だけあればよい。従来は全 State 分を要求していた）。
    """
    def expand(name: str) -> set:
        names = {name}
        if group_defs and name in group_defs:
            names.add(group_defs[name].axis)
        return names

    forbidden: set = set()
    rel = score_part.relative
    if rel is not None:
        forbidden |= expand(rel.split_axis)
        for step in rel.denominator_pre_aggregation:
            forbidden |= expand(step.axis)
            if step.by:
                forbidden |= expand(step.by)
    for entry in score_part.order:
        if not _is_virtual(entry) and COMBINED_SEP in entry:
            for axis in _step_axes(entry):
                forbidden |= expand(axis)

    out: list[tuple[str, object]] = []
    for entry in score_part.order:
        if _is_virtual(entry) or COMBINED_SEP in entry:
            continue
        spec = score_part.aggregations.get(entry)
        if spec is None or spec.op != "filter":
            continue
        if expand(entry) & forbidden:
            continue
        out.append((entry, spec.value))
    return out


def _source_type(score_part: ScorePart) -> str:
    """実際に読む csv の type（dVtBudget パーツは FBC.csv を読む）。"""
    return "FBC" if score_part.type == "dVtBudget" else score_part.type


# {Generation}.json（世代ごとのチップ情報）のキー → 軸名。Physical 記法の
# グループ定義を Logical へ読み替えるときの軸総数 N の出所
_GENERATION_AXIS_KEYS = {"WL": "numWLs", "STR": "numStrings"}


def derive_axis_counts(data_dir: str | Path, axes: Set[str]) -> Dict[str, int]:
    """測定csvから軸の本数を導出する（max+1）。

    WL/STR 等の本数は世代で固定であり、測定フローが一部だけ測る設定は存在しない
    （2026-07-28 担当者確認 — docs/spec_change_dataname_measure.md 9節）。
    したがってデータ（ダミー一式含む）の最大値+1 が軸の総数として正確で、
    {Generation}.json が無くても Physical 記法の読み替えができる。
    同じ軸を持つ type が複数あれば最大を取る。"""
    from .introspect import detect_types

    data_dir = Path(data_dir)
    counts: Dict[str, int] = {}
    for type_ in detect_types(data_dir):
        f = axis_resolve.data_file(data_dir, f"{type_}.csv")
        try:
            lf = pl.scan_csv(f)
            cols = lf.collect_schema().names()
        except Exception:
            continue
        take = [a for a in axes if a in cols]
        if not take:
            continue
        row = lf.select([pl.col(a).max() for a in take]).collect()
        for a in take:
            m = row[a][0]
            if m is not None:
                counts[a] = max(counts.get(a, 0), int(m) + 1)
    return counts


def load_axis_counts(generation_info_path: str | Path) -> Dict[str, int]:
    """世代情報 json から軸ごとの本数（{"WL": 120, "STR": 4} など）を読む。"""
    from . import jsonc

    info = jsonc.load(generation_info_path)
    counts: Dict[str, int] = {}
    if isinstance(info, dict):
        for axis, key in _GENERATION_AXIS_KEYS.items():
            if isinstance(info.get(key), int):
                counts[axis] = info[key]
    return counts


def resolve_group_defs(
    run_config: RunConfig,
    data_dir: str | Path,
    generation_info_path: Optional[str | Path] = None,
) -> Dict[str, GroupDef]:
    """config の全グループ定義を、Physical 記法（definedInLogical=false）の
    定義は Logical 範囲へ読み替えたうえで返す。読み替えに必要な軸総数 N は
    世代情報 json（既定: data_dir/{Generation}.json、`generation_info_path` で
    上書き可）の numWLs / numStrings から取り、**ファイルが無ければ測定csvから
    導出**する（derive_axis_counts。本数は世代で固定・フローは全数を測定する
    ため、データの最大値+1 が総数として正確）。全定義が Logical なら何も読まない。"""
    defs = run_config.group_defs()
    if all(gd.definedInLogical for gd in defs.values()):
        return defs

    path = (
        Path(generation_info_path)
        if generation_info_path
        else Path(data_dir) / f"{run_config.Generation}.json"
    )
    physical_axes = {gd.axis for gd in defs.values() if not gd.definedInLogical}
    if path.is_file():
        counts = load_axis_counts(path)
        source = str(path)
    else:
        counts = derive_axis_counts(data_dir, physical_axes)
        source = f"measurement csvs in {data_dir}"
    resolved: Dict[str, GroupDef] = {}
    for name, gd in defs.items():
        if gd.definedInLogical:
            resolved[name] = gd
            continue
        n = counts.get(gd.axis)
        if n is None:
            raise ValueError(
                f"group def '{name}' uses physical numbering but the axis count for "
                f"'{gd.axis}' could not be determined from {source} "
                f"(generation info keys: {_GENERATION_AXIS_KEYS})"
            )
        resolved[name] = GroupDef(
            axis=gd.axis, groups=gd.resolved_groups(n), definedInLogical=True
        )
    return resolved


class SharedComputeContext:
    """1回の呼び出し内でスコアパーツ間で共有するキャッシュ。純粋な内部最適化
    であり、有無で結果は変わらない。

    - resolved(): source type ごとに、全パーツの軸の和集合で csv を1回だけ
      読み込み・結合する。各パーツは単独 resolve と全く同じ列に射影し直して
      使うので、ペアリングやグループキーの意味は変わらない。
    - prefix_cache: __relative__ / __dvtbudget__ ステップ直後の中間結果。
      キーは（source type・必要軸・そこまでに適用した全ステップの署名）で、
      そこまでの設定が完全一致するパーツだけがエントリを共有する。

    寿命は compute_score_file() 1回分。epoch をまたいで何も残らないので、
    キャッシュの陳腐化を管理する必要はない。
    """

    def __init__(
        self,
        data_dir: str | Path,
        score_parts: list[ScorePart],
        group_defs: Optional[Dict[str, GroupDef]] = None,
    ):
        self.data_dir = data_dir
        self._union_axes: Dict[str, Set[str]] = {}
        for part in score_parts:
            if part.type == CUSTOM_TYPE:
                continue  # custom パーツはデータを自分で読む
            st = _source_type(part)
            self._union_axes.setdefault(st, set()).update(_required_axes(part, group_defs))
        self._resolved: Dict[str, "object"] = {}
        self.prefix_cache: Dict[tuple, "object"] = {}

    def resolved(self, source_type: str):
        if source_type not in self._resolved:
            self._resolved[source_type] = axis_resolve.resolve_axes(
                self.data_dir, source_type, self._union_axes[source_type]
            ).collect()
        return self._resolved[source_type]


def _apply_axis_step(lf, value_col: str, step: str, score_part: ScorePart):
    """仮想でない order エントリ1つを適用する: 単一軸ならそのまま、複合軸
    ("A&B") なら構成列を一時的な1本のキー列に融合し、既存の軸単位opが
    値の組に対して働くようにする。"""
    axes = _step_axes(step)
    if len(axes) == 1:
        return apply_aggregations(lf, value_col, [step], score_part.aggregations)

    spec = score_part.aggregations.get(step)
    if spec is None:
        raise ValueError(f"axis '{step}' listed in order but has no aggregation instruction")
    lf = lf.with_columns(
        pl.concat_str([pl.col(a).cast(pl.Utf8) for a in axes], separator=COMBINED_SEP).alias(step)
    ).drop(axes)
    if isinstance(spec.value, list):
        combined = [_combine_selection(v, axes) for v in spec.value]
    elif spec.value is not None:
        combined = _combine_selection(spec.value, axes)
    else:
        combined = None
    spec = spec.model_copy(update={"value": combined})
    return apply_aggregations(lf, value_col, [step], {step: spec})


def _step_signature(score_part: ScorePart, step: str) -> tuple:
    """prefix_cache のキーに使う、1ステップの設定内容の署名。"""
    if step == RELATIVE_STEP:
        return ("relative", score_part.relative.model_dump_json())
    if step == DVTBUDGET_STEP:
        return ("dvtbudget",)
    spec = score_part.aggregations.get(step)
    kind = "transform" if _is_virtual(step) else "axis"
    return (kind, step, spec.model_dump_json() if spec else "")


def compute_score_part(
    data_dir: str | Path,
    score_part: ScorePart,
    group_defs: Optional[Dict[str, GroupDef]] = None,
    generation: Optional[str] = None,
    dvtbudget_coef: Optional[DvtBudgetCoefFile] = None,
    board_temperatures: Optional[Dict[int, float]] = None,
    shared_ctx: Optional[SharedComputeContext] = None,
    selection_sets: Optional[Dict[str, list]] = None,
    weight_sets: Optional[Dict[str, object]] = None,
    custom_module=None,
    identity_axes: tuple[str, ...] = (),
):
    """スコアパーツ1つの値を計算する。type="custom" は関数呼び出しへ分岐し、
    それ以外は resolve → グループ派生列 → order の逐次適用、で1スカラーに畳む。

    `identity_axes` はバッチ計算（scorelib_param.batch）用: shared_ctx が供給する
    フレームに識別列（例: "Epoch"）が含まれる前提で、その列を潰さずに残し、
    識別値ごとに1行の DataFrame を返す（空タプル=従来どおり float を返す）。
    識別列は order に置かないため「残っている全列がグループキー」の仕組みに
    より、全集計・相対化ペア照合が自動的に識別値ごとに分かれて実行される。
    """
    if identity_axes:
        if shared_ctx is None:
            raise ValueError(
                "identity_axes requires a shared context that provides the identity columns"
            )
        if score_part.type == CUSTOM_TYPE:
            raise ValueError(
                f"custom part '{score_part.name}' cannot be batched with identity_axes; "
                "compute it once per epoch instead (scorelib_param.batch does this automatically)"
            )
    if score_part.type == CUSTOM_TYPE:
        if custom_module is None:
            raise ValueError(
                f"score part '{score_part.name}' has type='{CUSTOM_TYPE}' but no custom "
                f"parts file was loaded (expected {custom.default_custom_parts_path()})"
            )
        return custom.compute_custom_part(
            score_part,
            custom_module,
            custom.CustomContext(
                data_dir=Path(data_dir),
                generation=generation,
                group_defs=group_defs or {},
                params=score_part.params or {},
            ),
        )

    score_part = score_part.resolve_selection_refs(selection_sets or {}, weight_sets or {})
    source_type = _source_type(score_part)
    required_axes = _required_axes(score_part, group_defs)

    if shared_ctx is not None:
        base = shared_ctx.resolved(source_type)
        # 単独 resolve が返すのと厳密に同じ列へ射影し直す: 和集合の余分な列が
        # 残ると相対化のペアリングキーや集計のグループキーが変わってしまうため、
        # この射影は結果の正しさを支えている（消してはいけない）
        cols = [source_type] + sorted(required_axes) + list(identity_axes)
        lf = base.lazy().select(cols)
    else:
        lf = axis_resolve.resolve_axes(data_dir, source_type, required_axes)

    lf = _with_group_columns(lf, score_part, group_defs)

    # 暗黙の __relative__ より前に安全な filter の行絞りだけ先に適用する
    # （列は残し、本来の filter ステップがそのまま再適用+列削除する）。
    # 相対化・dVtBudget 変換の入力行数を減らす純粋な最適化で、結果は不変
    prefilters = _hoistable_prefilters(score_part, group_defs)
    for axis, value in prefilters:
        # リスト値は is_in（複数値 filter）の前絞り。行の部分集合化である点は
        # 等値と同じなので可換性の議論は変わらない
        lf = lf.filter(pl.col(axis).is_in(value) if isinstance(value, list) else pl.col(axis) == value)

    steps = _effective_order(score_part)
    sigs = [_step_signature(score_part, s) for s in steps]

    # キャッシュ点は各 __relative__ / __dvtbudget__ ステップの直後。キーは
    # その時点までの frame に影響した全て（グループ派生軸の中身も含む）を覆う
    cache_keys: Dict[int, tuple] = {}
    if shared_ctx is not None:
        defs_sig = tuple(
            sorted(
                (name, gd.axis, gd.definedInLogical, tuple(sorted(gd.groups.items())))
                for name, gd in _referenced_group_defs(score_part, group_defs).items()
            )
        )
        # prefilters をキーに含める: 前絞りが違えばキャッシュ点のフレームの
        # 中身が違うため、ステップ署名列が同じでも共有してはならない。
        # リスト値（is_in）は辞書キーにできないので tuple 化する
        prefilters_sig = tuple(
            (a, tuple(v) if isinstance(v, list) else v) for a, v in prefilters
        )
        base_sig = (
            source_type, tuple(sorted(required_axes)), defs_sig,
            tuple(identity_axes), prefilters_sig,
        )
        cache_keys = {
            i: (base_sig, tuple(sigs[: i + 1]))
            for i, s in enumerate(steps)
            if s in (RELATIVE_STEP, DVTBUDGET_STEP)
        }

    # いちばん後ろのキャッシュ点から再開できるところを探す
    start = 0
    for i in sorted(cache_keys, reverse=True):
        cached = shared_ctx.prefix_cache.get(cache_keys[i])
        if cached is not None:
            lf = cached.lazy()
            start = i + 1
            break

    for j in range(start, len(steps)):
        step = steps[j]
        if step == RELATIVE_STEP:
            lf = apply_relative(lf, source_type, score_part.relative)
        elif step == DVTBUDGET_STEP:
            if generation is None or dvtbudget_coef is None or board_temperatures is None:
                raise ValueError(
                    "dVtBudget score parts require generation, dvtbudget_coef, and board_temperatures"
                )
            # バッチ計算では温度（→係数b）が epoch ごとに違いうるため、
            # 識別軸を係数対応表のキーに含める（dvtbudget.apply_dvtbudget 参照）
            epoch_col = identity_axes[0] if identity_axes else None
            if len(identity_axes) > 1:
                raise ValueError("dVtBudget parts support at most one identity axis")
            lf = apply_dvtbudget(
                lf, source_type, generation, dvtbudget_coef, board_temperatures,
                epoch_col=epoch_col,
            )
        elif _is_virtual(step):
            spec = score_part.aggregations.get(step)
            if spec is None:
                raise ValueError(f"virtual step '{step}' has no entry in aggregations for '{score_part.name}'")
            lf = apply_transform(lf, source_type, spec)
        else:
            lf = _apply_axis_step(lf, source_type, step, score_part)

        if j in cache_keys:
            df = lf.collect()
            shared_ctx.prefix_cache[cache_keys[j]] = df
            lf = df.lazy()

    if identity_axes:
        return collapse(lf, source_type, identity_axes)
    return collapse_to_scalar(lf, source_type)


def compute_score_file(
    data_dir: str | Path,
    run_config: RunConfig,
    dvtbudget_coef: Optional[DvtBudgetCoefFile] = None,
    board_temperatures: Optional[Dict[int, float]] = None,
    custom_parts_path: Optional[str | Path] = None,
    generation_info_path: Optional[str | Path] = None,
) -> Dict[str, float]:
    """config の全スコアパーツを計算し、expression を評価して
    {"Score": ..., パーツ名: ...} を返す。"""
    score_file = run_config.to_score_file()
    group_defs = resolve_group_defs(run_config, data_dir, generation_info_path)

    # type="custom" のパーツは、リポジトリ直下の SVN 管理された custom_parts.py
    # の関数を呼ぶ。config にパスは持たせない（configから任意コードを実行
    # できてしまうため）。`custom_parts_path` はテスト・設計UI用の上書き
    custom_module = None
    if any(p.type == CUSTOM_TYPE for p in score_file.score_parts):
        path = Path(custom_parts_path) if custom_parts_path else custom.default_custom_parts_path()
        if not path.is_file():
            raise ValueError(
                f"score parts with type='{CUSTOM_TYPE}' need the custom parts file: {path}"
            )
        custom_module = custom.load_custom_module(path)

    part_names = {p.name for p in score_file.score_parts}
    for key in score_file.constraintThreshold:
        if key not in part_names:
            print(
                f"warning: constraintThreshold key '{key}' does not match any score part "
                f"(defined parts: {sorted(part_names)})",
                file=sys.stderr,
            )

    shared_ctx = SharedComputeContext(data_dir, score_file.score_parts, group_defs)
    values: Dict[str, float] = {}
    for score_part in score_file.score_parts:
        values[score_part.name] = compute_score_part(
            data_dir,
            score_part,
            group_defs=group_defs,
            generation=run_config.Generation,
            dvtbudget_coef=dvtbudget_coef,
            board_temperatures=board_temperatures,
            shared_ctx=shared_ctx,
            selection_sets=score_file.selectionSets,
            weight_sets=score_file.weightSets,
            custom_module=custom_module,
        )

    score = evaluate_expression(score_file.expression, values) if score_file.expression else None
    return {"Score": score, **values}


def main(argv: Optional[list[str]] = None) -> None:
    from . import __version__

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"scorelib_param {__version__}")
    parser.add_argument("--config", required=True, help="run config jsonc (Generation + optimization{...})")
    parser.add_argument("--data-dir", required=True, help="directory containing {type}.csv etc. for this epoch")
    parser.add_argument("--dvtbudget-coef", help="dVtBudget coefficient jsonc (required if any score part uses type=dVtBudget)")
    parser.add_argument("--initial-temperature", help="initial_temperature.csv (Board,Temperature; required for dVtBudget)")
    parser.add_argument("--custom-parts", help="custom_parts.py override (default: repository root)")
    parser.add_argument(
        "--generation-info",
        help="{Generation}.json with numWLs etc. (default: found in --data-dir; optional "
             "even for physical-numbering group defs — axis counts are derived from the "
             "measurement csvs when the file is absent)",
    )
    args = parser.parse_args(argv)

    run_config = io_jsonc.load_run_config(args.config)
    dvtbudget_coef = io_jsonc.load_dvtbudget_coef(args.dvtbudget_coef) if args.dvtbudget_coef else None
    board_temperatures = load_board_temperatures(args.initial_temperature) if args.initial_temperature else None

    result = compute_score_file(
        args.data_dir, run_config, dvtbudget_coef, board_temperatures,
        custom_parts_path=args.custom_parts,
        generation_info_path=args.generation_info,
    )
    # stdout には結果 JSON **だけ**を出す（最適化側がパースする）。
    # 版数の目印は実行ログ用に stderr へ
    print(f"scorelib_param {__version__}", file=sys.stderr)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
