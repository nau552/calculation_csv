# Copyright (c) 2026
"""CLI エントリポイント: 実epochデータから Score + 全スコアパーツの値を計算する。

現行最適化スクリプト(python3.7)の `get_score()` からサブプロセスとして
起動される想定(docs/score_gui_design.md 2節・7節)。

    python -m scorelib_param.cli --config config.jsonc --data-dir <epoch_dir> \
        [--dvtbudget-coef coef.jsonc] [--initial-temperature initial_temperature.csv]

stdout に JSON オブジェクトを1つだけ出力する: {"Score": ..., "<パーツ名>": ..., ...}
(InBatchEpoch 列は出さない — 出力契約は docs/score_gui_design.md 5節・7節)。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, overload

import polars as pl

from . import axis_resolve, custom, io_jsonc
from .aggregate import (
    CollapseNullError,
    apply_aggregations,
    apply_transform,
    collapse,
    collapse_to_scalar,
    group_column_expr,
)
from .dvtbudget import apply_dvtbudget, load_board_temperatures
from .expression import evaluate_expression
from .models import (
    COMBINED_SEP,
    CUSTOM_TYPE,
    AggregationSpec,
    DvtBudgetCoefFile,
    GroupDef,
    RunConfig,
    ScoreFile,
    ScorePart,
)
from .relative import apply_relative

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import ModuleType

# order に軸名と並べて置ける仮想エントリ(docs/score_gui_design.md 4.1節)。
# "__" 始まりのエントリは軸ではなくパイプラインステップ:
# - RELATIVE_STEP: 相対化を実行する位置(省略時は先頭)
# - DVTBUDGET_STEP: dVtBudget 変換を実行する位置(省略時は相対化の直後)。
#   その時点で Board/State がまだ潰されていない必要がある
# - それ以外の "__xxx__": 値列への行単位変換。指示は同名キーで aggregations に
#   置く(例: "__offset__": {"op": "add", "value": 1})
RELATIVE_STEP = "__relative__"
DVTBUDGET_STEP = "__dvtbudget__"

# order エントリは複数の軸を1つの複合軸に束ねられる(例: "State&Read_Label")。
# その集計指示は辞書選択を取る:
#   {"op": "sum", "value": [{"State": "R2A", "Read_Label": "read_level_upper1"},
#                           {"State": "A2R", "Read_Label": "read_level_lower1"}]}
# 束ねた軸は1つの軸として一緒に潰れるので、filter/sum/diff/expr がすべて
# (State, Read_Label) の組に対して働く。軸の値に "&" を含んではならない。


def _is_virtual(step: str) -> bool:
    return step.startswith("__")


def _step_axes(step: str) -> list[str]:
    return step.split(COMBINED_SEP)


def _named_axes(score_part: ScorePart) -> set[str]:
    """パーツ自身が言及する軸的な名前の集合。

    order エントリ(複合軸の構成軸込み)、相対化の split 軸、分母事前集計の
    軸を集める。グループ派生軸名を含みうる。

    ui/state.py の _part_axis_names は「編集途中の(不完全かもしれない)dict」
    を対象にした対になる実装 — 意図的な並行であり、統合を試みないこと。

    Returns:
        軸名の集合(order の構成軸・変換ステップの by 軸・相対化の split 軸・
        分母事前集計の軸と by 軸を合わせたもの)。

    """
    axes: set[str] = set()
    for entry in score_part.order:
        if not _is_virtual(entry):
            axes.update(_step_axes(entry))
    for spec in score_part.aggregations.values():
        if spec.by:
            axes.add(spec.by)  # 変換ステップの重みが参照する軸(グループ派生軸名も可)
    if score_part.relative:
        axes.add(score_part.relative.split_axis)
        for step in score_part.relative.denominator_pre_aggregation:
            axes.add(step.axis)
            if step.by:
                axes.add(step.by)
    return axes


def _referenced_group_defs(score_part: ScorePart, group_defs: dict[str, GroupDef] | None) -> dict[str, GroupDef]:
    """このパーツが派生軸として実際に使うグループ定義。

    Returns:
        パーツが軸名として言及している定義だけに絞った {定義名: GroupDef}。
        group_defs が空・None なら空辞書。

    Raises:
        ValueError: 使われている定義の名前がその元軸名と同名のとき。

    """
    if not group_defs:
        return {}
    used = {n: group_defs[n] for n in _named_axes(score_part) if n in group_defs}
    for name, gd in used.items():
        if gd.axis == name:
            msg = f"group def '{name}' must not have the same name as its source axis"
            raise ValueError(msg)
    return used


def _required_axes(score_part: ScorePart, group_defs: dict[str, GroupDef] | None = None) -> set[str]:
    """csv/map から実際に読み込むべき軸。

    グループ派生軸名はその元軸に読み替える(グループ列は読み込み後に
    元軸から作られる)。

    Returns:
        読み込むべき軸名の集合(dVtBudget パーツでは Board / State を必ず
        含む)。

    """
    named = _named_axes(score_part)
    derived = _referenced_group_defs(score_part, group_defs)
    axes = {a for a in named if a not in derived} | {gd.axis for gd in derived.values()}
    if score_part.type == "dVtBudget":
        axes.update({"Board", "State"})
    return axes


def _with_group_columns(
    lf: pl.LazyFrame, score_part: ScorePart, group_defs: dict[str, GroupDef] | None
) -> pl.LazyFrame:
    """このパーツが参照するグループ派生列を生成する。

    以降は普通の軸として集計される。派生のためだけに読み込んだ元軸は再び
    落とす: パーツ自身のエントリに無い軸は暗黙集約(混ぜる)が仕様であり、
    列が残ると最終の collapse がエラーになってしまうため。

    Returns:
        派生列を追加し、派生のためだけに読んだ元軸列を落とした LazyFrame
        (参照する定義が無ければ入力をそのまま返す)。

    Raises:
        ValueError: 参照する定義が Physical 記法のまま(definedInLogical=
            false)のとき、またはどのグループにも入らない軸の値が
            あるとき。

    """
    derived = _referenced_group_defs(score_part, group_defs)
    if not derived:
        return lf
    for name, gd in derived.items():
        if not gd.definedInLogical:
            msg = (
                f"group def '{name}' is still in physical numbering — resolve it to "
                "logical ranges first (cli.resolve_group_defs reads numWLs etc. from "
                "{Generation}.json and converts)"
            )
            raise ValueError(msg)
    lf = lf.with_columns([group_column_expr(gd.axis, gd.groups).alias(name) for name, gd in derived.items()])
    # どの範囲にも入らない行は「名無し(null)グループ」として静かに混ざって
    # しまう — ほぼ確実に定義の古さが原因なので、該当値の一覧つきで失敗させる
    for name, gd in derived.items():
        uncovered = lf.filter(pl.col(name).is_null()).select(pl.col(gd.axis).unique()).collect()
        if uncovered.height:
            vals = sorted(uncovered[gd.axis].to_list())
            msg = (
                f"values of axis '{gd.axis}' not covered by any group of '{name}': {vals} "
                f"(extend the group ranges or filter those values out first)"
            )
            raise ValueError(msg)
    keep = {a for a in _named_axes(score_part) if a not in derived}
    if score_part.type == "dVtBudget":
        keep.update({"Board", "State"})
    drop = {gd.axis for gd in derived.values() if gd.axis not in keep}
    return lf.drop(drop) if drop else lf


def _combined_key(v: object) -> str:
    return ("true" if v else "false") if isinstance(v, bool) else str(v)


def _combine_selection(sel: dict, axes: list[str]) -> str:
    """辞書選択1つ(ScorePart 検証済み)を、融合列に一致する内部の連結キー文字列へ変換する。

    Returns:
        構成軸の値を order エントリの軸順で "&" 連結した文字列
        (bool は "true"/"false" に揃える)。

    """
    return COMBINED_SEP.join(_combined_key(sel[a]) for a in axes)


def _effective_order(score_part: ScorePart) -> list[str]:
    """ユーザが明示配置しなかった暗黙のパイプラインステップを補完する。

    補完位置: 相対化は先頭、dVtBudget 変換は相対化の直後。

    Returns:
        暗黙ステップを補完した後の order のコピー(元のリストは変更しない)。

    Raises:
        ValueError: order に __relative__ があるのに relative 設定が無いとき、
            または __dvtbudget__ があるのに type が dVtBudget でないとき。

    """
    order = list(score_part.order)
    relative_enabled = score_part.relative is not None

    if RELATIVE_STEP in order and not relative_enabled:
        msg = f"'{RELATIVE_STEP}' in order but '{score_part.name}' has no relative config"
        raise ValueError(msg)
    if relative_enabled and RELATIVE_STEP not in order:
        order.insert(0, RELATIVE_STEP)

    if score_part.type == "dVtBudget" and DVTBUDGET_STEP not in order:
        pos = order.index(RELATIVE_STEP) + 1 if RELATIVE_STEP in order else 0
        order.insert(pos, DVTBUDGET_STEP)
    if DVTBUDGET_STEP in order and score_part.type != "dVtBudget":
        msg = f"'{DVTBUDGET_STEP}' in order but type of '{score_part.name}' is not dVtBudget"
        raise ValueError(msg)
    return order


def _expand_group_axis(name: str, group_defs: dict[str, GroupDef] | None) -> set[str]:
    """軸名を、グループ派生軸ならその元軸も加えた集合に広げる。

    filter 前出しの可否判定(_hoistable_prefilters)は展開後の集合同士の交わりで
    行うため、元軸と派生軸のどちらで書かれていても双方向に紐づく。

    Returns:
        {name}(name がグループ定義名なら、その元軸名も加えた集合)。

    """
    names = {name}
    if group_defs and name in group_defs:
        names.add(group_defs[name].axis)
    return names


def _prefilter_forbidden_axes(score_part: ScorePart, group_defs: dict[str, GroupDef] | None) -> set[str]:
    """前出しすると可換にならない filter の軸の集合(_hoistable_prefilters の下請け)。

    Returns:
        相対化の split 軸・分母事前集計で潰す軸とその重み参照軸(`by`)・
        複合軸エントリの構成軸を、グループ派生軸は元軸と紐づけて広げた集合。

    """
    forbidden: set[str] = set()
    rel = score_part.relative
    if rel is not None:
        forbidden |= _expand_group_axis(rel.split_axis, group_defs)
        for step in rel.denominator_pre_aggregation:
            forbidden |= _expand_group_axis(step.axis, group_defs)
            if step.by:
                forbidden |= _expand_group_axis(step.by, group_defs)
    for entry in score_part.order:
        if not _is_virtual(entry) and COMBINED_SEP in entry:
            for axis in _step_axes(entry):
                forbidden |= _expand_group_axis(axis, group_defs)
    return forbidden


def _hoistable_prefilters(
    score_part: ScorePart, group_defs: dict[str, GroupDef] | None = None
) -> list[tuple[str, object]]:
    """パイプラインの先頭に安全に前出しできる filter の列 [(軸, 値), ...]。

    軸 X の filter は X を潰さない演算すべてと可換: グループキーは常に
    「残っている全列」なので、他軸の集計・行単位変換(__offset__ 等・
    dVtBudget 変換)・相対化のペアリングは X の値ごとに独立した世界で
    計算され、X==v の世界を先に切り出しても結果は変わらない。そこで
    order 内の位置や __relative__ の明示/暗黙によらず、可換な filter は
    全行の相対化・変換が走る前に行だけ先に絞る。列は落とさない(列は
    本来の filter ステップが本来の位置で落とすので、__dvtbudget__ 等が
    途中で参照する列も欠けず、列を潰す順序の検証も従来どおり働く)。

    可換にならない軸だけを除外する:
    - relative.split_axis(分子/分母の振り分けに使う)
    - denominator_pre_aggregation で潰す軸・その重み参照軸(`by`)。
      分母は「全値の集計 vs 絞った値の集計」で結果が変わるため。
      グループ派生軸は元軸と紐づけて双方向に判定する
      (例: WL を事前集計するなら WLgroup の filter も前に出さない)
    - 複合軸エントリ("A&B")の構成軸(filter は組に対して働くため)

    対象は単一軸の純粋な filter(op="filter")のみ。selection ref は
    解決済みの ScorePart を渡すこと。

    診断への影響: 行が先に減るため、後段ステップの検証が「filter で残る
    行」だけを対象にするようになる(例: dVtBudget 係数は filter 後に残る
    State の分だけあればよい。従来は全 State 分を要求していた)。

    Returns:
        前出しできる filter の (軸名, 選択値) の並び(order での出現順)。

    """
    forbidden = _prefilter_forbidden_axes(score_part, group_defs)
    out: list[tuple[str, object]] = []
    for entry in score_part.order:
        if _is_virtual(entry) or COMBINED_SEP in entry:
            continue
        spec = score_part.aggregations.get(entry)
        if spec is None or spec.op != "filter":
            continue
        if _expand_group_axis(entry, group_defs) & forbidden:
            continue
        out.append((entry, spec.value))
    return out


def _source_type(score_part: ScorePart) -> str:
    """実際に読む csv の type(dVtBudget パーツは FBC.csv を読む)。

    Returns:
        読み込み対象の type 名(type="dVtBudget" なら "FBC"、それ以外は
        score_part.type そのまま)。

    """
    return "FBC" if score_part.type == "dVtBudget" else score_part.type


def _dummy_axis_values(data_dir: str | Path, axis: str, spec: AggregationSpec | None) -> list:
    """ダミー合成フレーム(compute_dummy_part)での軸の要素一覧。

    map ファイル → 同じ epoch の他の測定csvの実在値 → 集計指示の選択リスト →
    [0] の順で決める。sum の結果は要素数に依存するため、map のある軸
    (SGWLD 等)は実物と同じ要素数になる。

    Returns:
        軸の取りうる値のリスト(どの情報源からも決まらなければ [0])。

    """
    data_dir = Path(data_dir)
    # 同一パッケージ内部での意図的な利用(map ファイル探索の実装は axis_resolve に1つだけ持つ)
    map_path = axis_resolve._map_file_for_axis(data_dir, axis)  # ruff: ignore[SLF001]
    if map_path is not None:
        m = pl.read_csv(map_path, has_header=False, new_columns=["code", "text"])
        return m["text"].to_list()
    # introspect は UI 向けメタデータ導出モジュール。vthSkip のダミー計算経路でのみ使うため、使うときだけ読み込む
    from .introspect import detect_types  # ruff: ignore[PLC0415]

    for type_ in detect_types(data_dir):
        f = axis_resolve.data_file(data_dir, f"{type_}.csv")
        if not f.exists():
            continue
        try:
            lf = pl.scan_csv(f)
            if axis in lf.collect_schema().names():
                return lf.select(pl.col(axis).unique().sort()).collect()[axis].to_list()
        # 読めない・軸列の無い csv はダミー軸値の情報源から静かに外し、次の type の走査を続ける
        except Exception:  # ruff: ignore[S112, BLE001]
            continue
    if spec is not None and isinstance(spec.value, list) and spec.value:
        return list(spec.value)
    return [0]


def compute_dummy_part(  # ruff: ignore[PLR0913] — 公開 API: 多数の省略可能キーワード引数は設計(束ねない方針 — docs/dev_workflow.md)
    data_dir: str | Path,
    score_part: ScorePart,
    dummy_value: float,
    *,
    group_defs: dict[str, GroupDef] | None = None,
    selection_sets: dict[str, list] | None = None,
    weight_sets: dict[str, object] | None = None,
) -> float:
    """Type ファイルが無い epoch のダミー計算(vthSkip — models.VthSkipConfig)。

    設定に書かれたダミー値を「**変換後の値**」として軸の全組み合わせに敷き詰め、
    変換ステップ(__xxx__: log/abs/重み乗算など)は**スキップ**し、集計
    (選択リスト・集計時重み・sum/mean 等)だけを通常どおり適用する。
    フローの vthSkip 慣習(例: KLD のダミー 0 は log 適用後の量に対する値)を
    そのまま受け入れるための意味論(docs/score_gui_design.md 参照)。

    Returns:
        ダミー値を敷き詰めた合成フレームに集計だけを適用して畳んだ
        パーツ値。

    Raises:
        ValueError: パーツが相対化つき、または type=dVtBudget のとき
            (ダミー計算は素の集計パーツのみ対応)。

    """
    score_part = score_part.resolve_selection_refs(selection_sets or {}, weight_sets or {})
    if score_part.relative is not None or score_part.type == "dVtBudget":
        msg = (
            f"part '{score_part.name}': dummy computation (vthSkip) supports plain "
            "aggregation parts only — not relative or dVtBudget"
        )
        raise ValueError(msg)
    source_type = _source_type(score_part)
    axes = sorted(_required_axes(score_part, group_defs))
    axis_values = {a: _dummy_axis_values(data_dir, a, score_part.aggregations.get(a)) for a in axes}

    # ダミー計算(vthSkip)経路でのみ使うため、使うときだけ読み込む
    import itertools  # ruff: ignore[PLC0415]

    rows = list(itertools.product(*axis_values.values())) if axes else [()]
    data: dict[str, list] = {a: [r[i] for r in rows] for i, a in enumerate(axis_values)}
    data[source_type] = [float(dummy_value)] * len(rows)
    lf = pl.LazyFrame(data)

    lf = _with_group_columns(lf, score_part, group_defs)
    for step in _effective_order(score_part):
        if _is_virtual(step):
            continue  # ダミー値は「変換後の値」— 変換ステップは適用しない
        lf = _apply_axis_step(lf, source_type, step, score_part)
    return collapse_to_scalar(lf, source_type)


# {Generation}.json(世代ごとのチップ情報)のキー → 軸名。Physical 記法の
# グループ定義を Logical へ読み替えるときの軸総数 N の出所
_GENERATION_AXIS_KEYS = {"WL": "numWLs", "STR": "numStrings"}


def derive_axis_counts(data_dir: str | Path, axes: set[str]) -> dict[str, int]:
    """測定csvから軸の本数を導出する(max+1)。

    WL/STR 等の本数は世代で固定であり、測定フローが一部だけ測る設定は存在しない
    (2026-07-28 担当者確認 — docs/spec_change_dataname_measure.md 9節)。
    したがってデータ(ダミー一式含む)の最大値+1 が軸の総数として正確で、
    {Generation}.json が無くても Physical 記法の読み替えができる。
    同じ軸を持つ type が複数あれば最大を取る。

    Returns:
        {軸名: 本数}。どの csv からも読めなかった軸は含まれない。

    """
    # introspect は UI 向けメタデータ導出モジュール。Physical 記法の読み替え経路でのみ使うため、使うときだけ読み込む
    from .introspect import detect_types  # ruff: ignore[PLC0415]

    data_dir = Path(data_dir)
    counts: dict[str, int] = {}
    for type_ in detect_types(data_dir):
        f = axis_resolve.data_file(data_dir, f"{type_}.csv")
        try:
            lf = pl.scan_csv(f)
            cols = lf.collect_schema().names()
        # 読めない csv は軸本数の情報源から静かに外し、次の type の走査を続ける
        except Exception:  # ruff: ignore[S112, BLE001]
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


def load_axis_counts(generation_info_path: str | Path) -> dict[str, int]:
    """世代情報 json から軸ごとの本数({"WL": 120, "STR": 4} など)を読む。

    Returns:
        {軸名: 本数}(json に numWLs / numStrings 等の対応キーが無い軸は
        含まれない)。

    """
    # 世代情報 json を読むこの経路でのみ使うため、使うときだけ読み込む
    from . import jsonc  # ruff: ignore[PLC0415]

    info = jsonc.load(generation_info_path)
    counts: dict[str, int] = {}
    if isinstance(info, dict):
        for axis, key in _GENERATION_AXIS_KEYS.items():
            if isinstance(info.get(key), int):
                counts[axis] = info[key]
    return counts


def resolve_group_defs(
    run_config: RunConfig,
    data_dir: str | Path,
    generation_info_path: str | Path | None = None,
) -> dict[str, GroupDef]:
    """Config の全グループ定義を、Physical 記法の定義は Logical 範囲へ読み替えたうえで返す。

    Physical 記法 = definedInLogical=false の定義。読み替えに必要な軸総数 N は
    世代情報 json(既定: data_dir/{Generation}.json、`generation_info_path` で
    上書き可)の numWLs / numStrings から取り、**ファイルが無ければ測定csvから
    導出**する(derive_axis_counts。本数は世代で固定・フローは全数を測定する
    ため、データの最大値+1 が総数として正確)。全定義が Logical なら何も読まない。

    Returns:
        {定義名: GroupDef}(すべて Logical 範囲へ読み替え済み。
        definedInLogical=True に揃う)。

    Raises:
        ValueError: Physical 記法の定義があるのに、その軸の総数を世代情報
            json からも測定csvからも決められなかったとき。

    """
    defs = run_config.group_defs()
    if all(gd.definedInLogical for gd in defs.values()):
        return defs

    path = Path(generation_info_path) if generation_info_path else Path(data_dir) / f"{run_config.Generation}.json"
    physical_axes = {gd.axis for gd in defs.values() if not gd.definedInLogical}
    if path.is_file():
        counts = load_axis_counts(path)
        source = str(path)
    else:
        counts = derive_axis_counts(data_dir, physical_axes)
        source = f"measurement csvs in {data_dir}"
    resolved: dict[str, GroupDef] = {}
    for name, gd in defs.items():
        if gd.definedInLogical:
            resolved[name] = gd
            continue
        n = counts.get(gd.axis)
        if n is None:
            msg = (
                f"group def '{name}' uses physical numbering but the axis count for "
                f"'{gd.axis}' could not be determined from {source} "
                f"(generation info keys: {_GENERATION_AXIS_KEYS})"
            )
            raise ValueError(msg)
        resolved[name] = GroupDef(axis=gd.axis, groups=gd.resolved_groups(n), definedInLogical=True)
    return resolved


class SharedComputeContext:
    """1回の呼び出し内でスコアパーツ間で共有するキャッシュ。

    純粋な内部最適化であり、有無で結果は変わらない。

    - resolved(): source type ごとに、全パーツの軸の和集合で csv を1回だけ
      読み込み・結合する。各パーツは単独 resolve と全く同じ列に射影し直して
      使うので、ペアリングやグループキーの意味は変わらない。
    - prefix_cache: __relative__ / __dvtbudget__ ステップ直後の中間結果。
      キーは(source type・必要軸・そこまでに適用した全ステップの署名)で、
      そこまでの設定が完全一致するパーツだけがエントリを共有する。

    寿命は compute_score_file() 1回分。epoch をまたいで何も残らないので、
    キャッシュの陳腐化を管理する必要はない。
    """

    def __init__(
        self,
        data_dir: str | Path,
        score_parts: list[ScorePart],
        group_defs: dict[str, GroupDef] | None = None,
    ) -> None:
        """パーツ一覧から source type ごとの必要軸の和集合を前計算する。"""
        self.data_dir = data_dir
        self._union_axes: dict[str, set[str]] = {}
        for part in score_parts:
            if part.type == CUSTOM_TYPE:
                continue  # custom パーツはデータを自分で読む
            st = _source_type(part)
            self._union_axes.setdefault(st, set()).update(_required_axes(part, group_defs))
        self._resolved: dict[str, pl.DataFrame] = {}
        self.prefix_cache: dict[tuple, pl.DataFrame] = {}

    def resolved(self, source_type: str) -> pl.DataFrame:
        """Source type の csv を全パーツの軸の和集合で1回だけ解決した DataFrame。

        Returns:
            解決済み DataFrame(初回に読み込み・結合してキャッシュし、
            2回目以降は同じオブジェクトを返す)。

        """
        if source_type not in self._resolved:
            self._resolved[source_type] = axis_resolve.resolve_axes(
                self.data_dir, source_type, self._union_axes[source_type]
            ).collect()
        return self._resolved[source_type]


def _apply_axis_step(lf: pl.LazyFrame, value_col: str, step: str, score_part: ScorePart) -> pl.LazyFrame:
    """仮想でない order エントリ1つを適用する。

    単一軸ならそのまま、複合軸("A&B")なら構成列を一時的な1本のキー列に
    融合し、既存の軸単位opが値の組に対して働くようにする。

    Returns:
        当該エントリの集計指示を適用した後の LazyFrame。

    Raises:
        ValueError: 複合軸エントリに対応する集計指示が aggregations に
            無いとき。

    """
    axes = _step_axes(step)
    if len(axes) == 1:
        return apply_aggregations(lf, value_col, [step], score_part.aggregations)

    spec = score_part.aggregations.get(step)
    if spec is None:
        msg = f"axis '{step}' listed in order but has no aggregation instruction"
        raise ValueError(msg)
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
    """prefix_cache のキーに使う、1ステップの設定内容の署名。

    Returns:
        ステップ種別と設定内容(JSON 化した spec など)を並べたタプル。
        そこまでの設定が完全一致するパーツ同士でだけ等しくなる。

    Raises:
        ValueError: order に __relative__ があるのに relative 設定が無いとき
            (_effective_order が先に検出するため、到達しないパスの防御)。

    """
    if step == RELATIVE_STEP:
        rel = score_part.relative
        if rel is None:
            msg = f"'{RELATIVE_STEP}' in order but '{score_part.name}' has no relative config"
            raise ValueError(msg)
        return ("relative", rel.model_dump_json())
    if step == DVTBUDGET_STEP:
        return ("dvtbudget",)
    spec = score_part.aggregations.get(step)
    kind = "transform" if _is_virtual(step) else "axis"
    return (kind, step, spec.model_dump_json() if spec else "")


def _compute_custom_part(
    data_dir: str | Path,
    score_part: ScorePart,
    generation: str | None,
    group_defs: dict[str, GroupDef] | None,
    custom_module: ModuleType | None,
) -> float:
    """type="custom" のパーツを custom_parts.py の関数呼び出しで計算する(compute_score_part の下請け)。

    custom_parts.py に該当関数が無い・呼び出し可能でないときは
    custom.compute_custom_part が TypeError を送出する(ここからそのまま伝播)。

    Returns:
        カスタム関数が返したパーツ値。

    Raises:
        ValueError: custom_module が無い(custom_parts.py が読み込まれて
            いない)とき。

    """
    if custom_module is None:
        msg = (
            f"score part '{score_part.name}' has type='{CUSTOM_TYPE}' but no custom "
            f"parts file was loaded (expected {custom.default_custom_parts_path()})"
        )
        raise ValueError(msg)
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


def _base_frame(
    data_dir: str | Path,
    score_part: ScorePart,
    group_defs: dict[str, GroupDef] | None,
    shared_ctx: SharedComputeContext | None,
    identity_axes: tuple[str, ...],
) -> pl.LazyFrame:
    """パーツ計算の入力フレーム(resolve + グループ派生列)を用意する(compute_score_part の下請け)。

    Returns:
        値列 + 必要軸(identity_axes 指定時は識別列も)を持ち、グループ派生列を
        生成し終えた LazyFrame。

    """
    source_type = _source_type(score_part)
    required_axes = _required_axes(score_part, group_defs)
    if shared_ctx is not None:
        base = shared_ctx.resolved(source_type)
        # 単独 resolve が返すのと厳密に同じ列へ射影し直す: 和集合の余分な列が
        # 残ると相対化のペアリングキーや集計のグループキーが変わってしまうため、
        # この射影は結果の正しさを支えている(消してはいけない)
        cols = [source_type, *sorted(required_axes), *list(identity_axes)]
        lf = base.lazy().select(cols)
    else:
        lf = axis_resolve.resolve_axes(data_dir, source_type, required_axes)
    return _with_group_columns(lf, score_part, group_defs)


def _apply_prefilters(lf: pl.LazyFrame, prefilters: list[tuple[str, object]]) -> pl.LazyFrame:
    """前出しできる filter(_hoistable_prefilters)の行絞りだけを適用する。

    Returns:
        各 filter の該当行だけに絞った LazyFrame(列は落とさない — 列は本来の
        filter ステップが本来の位置で落とす)。

    """
    for axis, value in prefilters:
        # リスト値は is_in(複数値 filter)の前絞り。行の部分集合化である点は
        # 等値と同じなので可換性の議論は変わらない
        lf = lf.filter(pl.col(axis).is_in(value) if isinstance(value, list) else pl.col(axis) == value)
    return lf


def _prefix_cache_keys(
    score_part: ScorePart,
    group_defs: dict[str, GroupDef] | None,
    identity_axes: tuple[str, ...],
    prefilters: list[tuple[str, object]],
    steps: list[str],
) -> dict[int, tuple]:
    """shared_ctx.prefix_cache のキーを、キャッシュ点となるステップ位置ごとに構築する。

    キャッシュ点は各 __relative__ / __dvtbudget__ ステップの直後。キーは
    その時点までの frame に影響した全て(グループ派生軸の中身も含む)を覆い、
    そこまでの設定が完全一致するパーツだけがエントリを共有する。

    Returns:
        {steps 内の位置: キャッシュキー}(キャッシュ点が無ければ空辞書)。

    """
    sigs = [_step_signature(score_part, s) for s in steps]
    defs_sig = tuple(
        sorted(
            (name, gd.axis, gd.definedInLogical, tuple(sorted(gd.groups.items())))
            for name, gd in _referenced_group_defs(score_part, group_defs).items()
        )
    )
    # prefilters をキーに含める: 前絞りが違えばキャッシュ点のフレームの
    # 中身が違うため、ステップ署名列が同じでも共有してはならない。
    # リスト値(is_in)は辞書キーにできないので tuple 化する
    prefilters_sig = tuple((a, tuple(v) if isinstance(v, list) else v) for a, v in prefilters)
    base_sig = (
        _source_type(score_part),
        tuple(sorted(_required_axes(score_part, group_defs))),
        defs_sig,
        tuple(identity_axes),
        prefilters_sig,
    )
    return {i: (base_sig, tuple(sigs[: i + 1])) for i, s in enumerate(steps) if s in {RELATIVE_STEP, DVTBUDGET_STEP}}


def _resume_from_cache(
    lf: pl.LazyFrame,
    shared_ctx: SharedComputeContext | None,
    cache_keys: dict[int, tuple],
) -> tuple[pl.LazyFrame, int]:
    """いちばん後ろのキャッシュ点から再開できるところを探す(compute_score_part の下請け)。

    Returns:
        (再開に使う LazyFrame, 適用を再開するステップ位置)。ヒットが無ければ
        入力の LazyFrame と位置 0 をそのまま返す。

    """
    # shared_ctx が None なら cache_keys は空 = ループは元々0回。ガードは型の narrowing 用
    if shared_ctx is not None:
        for i in sorted(cache_keys, reverse=True):
            cached = shared_ctx.prefix_cache.get(cache_keys[i])
            if cached is not None:
                return cached.lazy(), i + 1
    return lf, 0


@dataclass(frozen=True)
class _StepContext:
    """order の1ステップの適用に必要な文脈一式(compute_score_part 内部用)。

    dVtBudget 変換の入力(generation / dvtbudget_coef / board_temperatures)は
    __dvtbudget__ ステップでだけ使われる(それ以外のステップでは None のままで
    よい)。
    """

    score_part: ScorePart
    source_type: str
    generation: str | None
    dvtbudget_coef: DvtBudgetCoefFile | None
    board_temperatures: Mapping[int, float] | Mapping[str, dict[int, float]] | None
    identity_axes: tuple[str, ...]


def _apply_steps(
    lf: pl.LazyFrame,
    indexed_steps: list[tuple[int, str]],
    ctx: _StepContext,
    shared_ctx: SharedComputeContext | None,
    cache_keys: dict[int, tuple],
) -> pl.LazyFrame:
    """ステップ列(order)を順に適用する(compute_score_part の下請け)。

    Returns:
        全ステップ適用後の LazyFrame。キャッシュ点(cache_keys にある位置)では
        collect して shared_ctx.prefix_cache に保存し、以降はその実体から続ける。

    """
    for j, step in indexed_steps:
        lf = _apply_pipeline_step(lf, step, ctx)
        if shared_ctx is not None and j in cache_keys:
            df = lf.collect()
            shared_ctx.prefix_cache[cache_keys[j]] = df
            lf = df.lazy()
    return lf


def _probe_frame(lf: pl.LazyFrame, value_col: str) -> tuple[int, int]:
    """診断用: フレームの行数と、値列が null / NaN の行数を数える。

    Returns:
        (行数, 値列が null または NaN の行数)のタプル。

    """
    v = pl.col(value_col).cast(pl.Float64)
    df = lf.select(pl.len().alias("__rows__"), (v.is_null() | v.is_nan()).sum().alias("__bad__")).collect()
    return int(df["__rows__"][0]), int(df["__bad__"][0])


def _diagnose_empty_step(step: str, ctx: _StepContext, before: pl.LazyFrame) -> str:
    """診断用: 行数が 0 になったステップの原因を言葉にする。

    Returns:
        原因の説明(filter の空振り・相対化の片側不在など)。

    """
    rel = ctx.score_part.relative
    if step == RELATIVE_STEP and rel is not None:
        axis = rel.split_axis
        n_num = int(before.filter(pl.col(axis) == rel.numerator_when).select(pl.len()).collect().item())
        n_den = int(before.filter(pl.col(axis) == rel.denominator_when).select(pl.len()).collect().item())
        if n_num == 0:
            return (
                f"relative split '{axis}': no rows where {axis} == {rel.numerator_when!r} "
                "(evaluation side) — the data does not contain that side"
            )
        if n_den == 0:
            return (
                f"relative split '{axis}': no rows where {axis} == {rel.denominator_when!r} "
                "(reference side) — the data does not contain that side"
            )
        return f"relative split '{axis}': rows exist on both sides but no pairs matched on the remaining axes"
    spec = ctx.score_part.aggregations.get(step)
    if spec is not None and spec.op == "filter":
        return f"filter {step} == {spec.value!r} matched no rows"
    if spec is not None and spec.op == "diff" and isinstance(spec.value, list):
        return f"diff selection {step} == {spec.value[0]!r} matched no rows"
    return f"step '{step}' left no rows"


def _diagnose_bad_step(step: str, ctx: _StepContext, after: pl.LazyFrame, bad: int, rows: int) -> str:
    """診断用: 値列に null / NaN を持ち込んだステップの原因を言葉にする。

    Returns:
        原因の説明(dVtBudget 係数/温度の照会失敗・相対化の相手不在など)。

    """
    v = pl.col(ctx.source_type).cast(pl.Float64)
    if step == DVTBUDGET_STEP:
        pairs = after.filter(v.is_null() | v.is_nan()).select("Board", "State").unique().limit(8).collect().rows()
        return (
            f"dVtBudget coefficient/temperature lookup failed for {bad} of {rows} rows "
            f"(no entry for (Board, State) = {pairs}; check dvtbudget_coef for "
            f"generation '{ctx.generation}' and the board temperatures)"
        )
    rel = ctx.score_part.relative
    if step == RELATIVE_STEP and rel is not None:
        return (
            f"relative split '{rel.split_axis}': {bad} of {rows} evaluation-side rows "
            "had no reference-side partner on the remaining axes"
        )
    spec = ctx.score_part.aggregations.get(step)
    if spec is not None and spec.op == "diff" and isinstance(spec.value, list):
        return f"diff: {bad} of {rows} rows for {spec.value[0]!r} had no {spec.value[1]!r} partner"
    return f"step '{step}' produced {bad} missing value(s) out of {rows} rows"


def _diagnose_pipeline(lf: pl.LazyFrame, steps: list[str], ctx: _StepContext) -> str | None:
    """最終結果の null / NaN の原因を、パイプラインを歩き直して特定する(エラー経路専用)。

    成功時には呼ばれない(コストはゼロ)。prefilter(前出し最適化)は適用せず
    素の順で歩く — 結果不変の最適化なので原因の位置は同じで、ステップ名で
    報告できる方が分かりやすい。

    Returns:
        原因の説明。診断中に二次エラーが起きた・特定できなかった場合は None
        (呼び出し側は元のエラーをそのまま出す)。

    """
    try:
        return _diagnose_walk(lf, steps, ctx)
    # 診断はおまけ: 二次エラーで元のエラーを隠さない
    except Exception:  # ruff: ignore[BLE001]
        return None


def _diagnose_walk(lf: pl.LazyFrame, steps: list[str], ctx: _StepContext) -> str | None:
    """診断の本体: ステップを順に適用しながら行数と null / NaN を監視する。

    Returns:
        最初に行または値を失ったステップの説明。見つからなければ None。

    """
    value_col = ctx.source_type
    rows, bad = _probe_frame(lf, value_col)
    if rows == 0:
        return "the source data has no rows"
    for step in steps:
        before, prev_rows, prev_bad = lf, rows, bad
        lf = _apply_pipeline_step(lf, step, ctx)
        rows, bad = _probe_frame(lf, value_col)
        if rows == 0 and prev_rows > 0:
            return _diagnose_empty_step(step, ctx, before)
        if bad > 0 and prev_bad == 0:
            return _diagnose_bad_step(step, ctx, lf, bad, rows)
    return None


def _apply_pipeline_step(lf: pl.LazyFrame, step: str, ctx: _StepContext) -> pl.LazyFrame:
    """仮想ステップ含む order のエントリ1つを適用する(compute_score_part の下請け)。

    Returns:
        当該ステップを適用した後の LazyFrame。

    Raises:
        ValueError: dVtBudget パーツに generation / dvtbudget_coef /
            board_temperatures が揃っていないとき、識別軸が2軸以上のとき、
            または order の仮想ステップに対応する集計指示が無いとき。

    """
    score_part = ctx.score_part
    if step == RELATIVE_STEP:
        rel = score_part.relative
        if rel is None:
            # 到達しないパスの防御: _effective_order が relative 無しの __relative__ を先に検出する
            msg = f"'{RELATIVE_STEP}' in order but '{score_part.name}' has no relative config"
            raise ValueError(msg)
        return apply_relative(lf, ctx.source_type, rel)
    if step == DVTBUDGET_STEP:
        if ctx.generation is None or ctx.dvtbudget_coef is None or ctx.board_temperatures is None:
            msg = "dVtBudget score parts require generation, dvtbudget_coef, and board_temperatures"
            raise ValueError(msg)
        # バッチ計算では温度(→係数b)が epoch ごとに違いうるため、
        # 識別軸を係数対応表のキーに含める(dvtbudget.apply_dvtbudget 参照)
        epoch_col = ctx.identity_axes[0] if ctx.identity_axes else None
        if len(ctx.identity_axes) > 1:
            msg = "dVtBudget parts support at most one identity axis"
            raise ValueError(msg)
        return apply_dvtbudget(
            lf,
            ctx.source_type,
            ctx.generation,
            ctx.dvtbudget_coef,
            ctx.board_temperatures,
            epoch_col=epoch_col,
        )
    if _is_virtual(step):
        spec = score_part.aggregations.get(step)
        if spec is None:
            msg = f"virtual step '{step}' has no entry in aggregations for '{score_part.name}'"
            raise ValueError(msg)
        return apply_transform(lf, ctx.source_type, spec)
    return _apply_axis_step(lf, ctx.source_type, step, score_part)


# overload: identity_axes 省略(空タプル)なら float、識別軸を指定したら
# DataFrame(呼び出し側の isinstance 分岐を不要にする)。実装は1つで挙動不変
@overload
def compute_score_part(
    data_dir: str | Path,
    score_part: ScorePart,
    *,
    group_defs: dict[str, GroupDef] | None = None,
    generation: str | None = None,
    dvtbudget_coef: DvtBudgetCoefFile | None = None,
    board_temperatures: Mapping[int, float] | Mapping[str, dict[int, float]] | None = None,
    shared_ctx: SharedComputeContext | None = None,
    selection_sets: dict[str, list] | None = None,
    weight_sets: dict[str, object] | None = None,
    custom_module: ModuleType | None = None,
    identity_axes: tuple[()] = (),
) -> float: ...


@overload
def compute_score_part(
    data_dir: str | Path,
    score_part: ScorePart,
    *,
    group_defs: dict[str, GroupDef] | None = None,
    generation: str | None = None,
    dvtbudget_coef: DvtBudgetCoefFile | None = None,
    board_temperatures: Mapping[int, float] | Mapping[str, dict[int, float]] | None = None,
    shared_ctx: SharedComputeContext | None = None,
    selection_sets: dict[str, list] | None = None,
    weight_sets: dict[str, object] | None = None,
    custom_module: ModuleType | None = None,
    identity_axes: tuple[str, ...],
) -> pl.DataFrame: ...


def compute_score_part(  # ruff: ignore[PLR0913] — 公開 API: 多数の省略可能キーワード引数は設計(束ねない方針 — docs/dev_workflow.md)
    data_dir: str | Path,
    score_part: ScorePart,
    *,
    group_defs: dict[str, GroupDef] | None = None,
    generation: str | None = None,
    dvtbudget_coef: DvtBudgetCoefFile | None = None,
    board_temperatures: Mapping[int, float] | Mapping[str, dict[int, float]] | None = None,
    shared_ctx: SharedComputeContext | None = None,
    selection_sets: dict[str, list] | None = None,
    weight_sets: dict[str, object] | None = None,
    custom_module: ModuleType | None = None,
    identity_axes: tuple[str, ...] = (),
) -> float | pl.DataFrame:
    """スコアパーツ1つの値を計算する。

    type="custom" は関数呼び出しへ分岐し、それ以外は resolve → グループ
    派生列 → order の逐次適用、で1スカラーに畳む。

    `identity_axes` はバッチ計算(scorelib_param.batch)用: shared_ctx が供給する
    フレームに識別列(例: "Epoch")が含まれる前提で、その列を潰さずに残し、
    識別値ごとに1行の DataFrame を返す(空タプル=従来どおり float を返す)。
    識別列は order に置かないため「残っている全列がグループキー」の仕組みに
    より、全集計・相対化ペア照合が自動的に識別値ごとに分かれて実行される。

    Returns:
        パーツ値のスカラー(identity_axes 指定時は識別値ごとに1行の
        DataFrame)。

    Raises:
        ValueError: identity_axes を shared_ctx 無し・custom パーツ・2軸
            以上で使ったとき、custom パーツなのに custom_module が無い
            とき、dVtBudget パーツに generation / dvtbudget_coef /
            board_temperatures が揃っていないとき、order の仮想
            ステップに対応する集計指示が無いとき、または計算結果が
            null / NaN で原因ステップを特定できたとき(名指しの説明つき)。
        CollapseNullError: 計算結果が null / NaN で、原因ステップを
            特定できなかったとき。

    """
    if identity_axes:
        if shared_ctx is None:
            msg = "identity_axes requires a shared context that provides the identity columns"
            raise ValueError(msg)
        if score_part.type == CUSTOM_TYPE:
            msg = (
                f"custom part '{score_part.name}' cannot be batched with identity_axes; "
                "compute it once per epoch instead (scorelib_param.batch does this automatically)"
            )
            raise ValueError(msg)
    if score_part.type == CUSTOM_TYPE:
        return _compute_custom_part(data_dir, score_part, generation, group_defs, custom_module)

    score_part = score_part.resolve_selection_refs(selection_sets or {}, weight_sets or {})
    source_type = _source_type(score_part)
    lf = _base_frame(data_dir, score_part, group_defs, shared_ctx, identity_axes)

    # 暗黙の __relative__ より前に安全な filter の行絞りだけ先に適用する
    # (列は残し、本来の filter ステップがそのまま再適用+列削除する)。
    # 相対化・dVtBudget 変換の入力行数を減らす純粋な最適化で、結果は不変
    prefilters = _hoistable_prefilters(score_part, group_defs)
    lf = _apply_prefilters(lf, prefilters)

    steps = _effective_order(score_part)
    cache_keys: dict[int, tuple] = {}
    if shared_ctx is not None:
        cache_keys = _prefix_cache_keys(score_part, group_defs, identity_axes, prefilters, steps)
    lf, start = _resume_from_cache(lf, shared_ctx, cache_keys)

    ctx = _StepContext(
        score_part=score_part,
        source_type=source_type,
        generation=generation,
        dvtbudget_coef=dvtbudget_coef,
        board_temperatures=board_temperatures,
        identity_axes=identity_axes,
    )
    try:
        lf = _apply_steps(lf, list(enumerate(steps))[start:], ctx, shared_ctx, cache_keys)
        if identity_axes:
            return collapse(lf, source_type, identity_axes)
        return collapse_to_scalar(lf, source_type)
    except CollapseNullError as err:
        # 最終結果の null / NaN は「どこかのステップが行または値を失った」
        # 事実しか伝えない。エラー経路に限りパイプラインを素の順で歩き直し、
        # 原因ステップ(filter 空振り / 相対化の片側不在 / 係数照会失敗など)を
        # 名指しした ValueError に変換する
        detail = _diagnose_pipeline(
            _base_frame(data_dir, score_part, group_defs, shared_ctx, identity_axes), steps, ctx
        )
        if detail is None:
            raise
        msg = f"{err} — {detail}"
        raise ValueError(msg) from err


def _load_custom_module_if_needed(score_file: ScoreFile, custom_parts_path: str | Path | None) -> ModuleType | None:
    """type="custom" のパーツがあれば custom_parts.py を読み込む(compute_score_file の下請け)。

    リポジトリ直下の SVN 管理された custom_parts.py の関数を呼ぶ。config に
    パスは持たせない(configから任意コードを実行できてしまうため)。
    `custom_parts_path` はテスト・設計UI用の上書き。

    Returns:
        読み込んだモジュール(custom パーツが無ければ None)。

    Raises:
        ValueError: custom パーツがあるのにファイルが見つからないとき。

    """
    if not any(p.type == CUSTOM_TYPE for p in score_file.score_parts):
        return None
    path = Path(custom_parts_path) if custom_parts_path else custom.default_custom_parts_path()
    if not path.is_file():
        msg = f"score parts with type='{CUSTOM_TYPE}' need the custom parts file: {path}"
        raise ValueError(msg)
    return custom.load_custom_module(path)


def _warn_unmatched_constraints(score_file: ScoreFile) -> None:
    """どのパーツにも一致しない constraintThreshold キーを stderr に警告する(compute_score_file の下請け)。"""
    part_names = {p.name for p in score_file.score_parts}
    for key in score_file.constraintThreshold:
        if key not in part_names:
            print(
                f"warning: constraintThreshold key '{key}' does not match any score part "
                f"(defined parts: {sorted(part_names)})",
                file=sys.stderr,
            )


def compute_score_file(  # ruff: ignore[PLR0913] — 公開 API: 多数の省略可能キーワード引数は設計(束ねない方針 — docs/dev_workflow.md)
    data_dir: str | Path,
    run_config: RunConfig,
    *,
    dvtbudget_coef: DvtBudgetCoefFile | None = None,
    board_temperatures: dict[int, float] | None = None,
    custom_parts_path: str | Path | None = None,
    generation_info_path: str | Path | None = None,
) -> dict[str, float | None]:
    """Config の全スコアパーツを計算し、expression を評価して {"Score": ..., パーツ名: ...} を返す。

    Returns:
        {"Score": 合成式の値(式が無ければ None), パーツ名: パーツ値, ...}。

    Raises:
        ValueError: type="custom" のパーツがあるのに custom_parts.py が
            見つからないとき。また、パーツ計算中の ValueError / TypeError は
            「score part '名前': 元メッセージ」の形で失敗パーツを名指しする。
            **エラーがあっても全パーツを計算し終えてから**落とす(「1つ直すと
            次のエラー」の往復を避ける): 1件なら従来と同じ形・同じ型、複数なら
            「N score parts failed:」+ 1行1パーツでまとめて送出。失敗が1件でも
            あれば値の辞書は**返さない**(部分結果で実験が続くことはない)。

    """
    score_file = run_config.to_score_file()
    group_defs = resolve_group_defs(run_config, data_dir, generation_info_path)

    custom_module = _load_custom_module_if_needed(score_file, custom_parts_path)
    _warn_unmatched_constraints(score_file)

    # vthSkip(測定フロー側の設定): 指定 type のファイルが無い epoch は
    # ダミー値で計算する(models.VthSkipConfig / compute_dummy_part)
    vth = run_config.optimization.vthSkip
    dummy_values = vth.dummy_values() if vth else {}

    shared_ctx = SharedComputeContext(data_dir, score_file.score_parts, group_defs)
    values: dict[str, float] = {}
    # パーツのエラーは集めて最後まで計算を続ける: 「1つ直して動かしたら次の
    # エラー」の往復を避けるため。ただし1件でもあれば**必ず例外で終わる** —
    # 部分的な values を返して実験が続いてしまう経路は作らない
    part_errors: list[Exception] = []
    for score_part in score_file.score_parts:
        st = _source_type(score_part)
        use_dummy = (
            score_part.type != CUSTOM_TYPE
            and st in dummy_values
            and not axis_resolve.data_file(Path(data_dir), f"{st}.csv").exists()
        )
        try:
            if use_dummy:
                values[score_part.name] = compute_dummy_part(
                    data_dir,
                    score_part,
                    dummy_values[st],
                    group_defs=group_defs,
                    selection_sets=score_file.selectionSets,
                    weight_sets=score_file.weightSets,
                )
            else:
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
        except (ValueError, TypeError) as err:
            # どのパーツで失敗したかを常に名指しする(深部のエラー — null 集計
            # など — はパーツ名を知らないため。既に名指し済みなら包み直さない)
            if f"'{score_part.name}'" not in str(err):
                named = type(err)(f"score part '{score_part.name}': {err}")
                named.__cause__ = err
                err = named
            part_errors.append(err)
            continue
        if use_dummy:
            print(
                f"note: part '{score_part.name}' computed with vthSkip dummy value "
                f"{dummy_values[st]} ({st}.csv not found in {data_dir})",
                file=sys.stderr,
            )

    if len(part_errors) == 1:
        raise part_errors[0]  # 従来と同じ形・同じ型で落とす
    if part_errors:
        msg = f"{len(part_errors)} score parts failed:\n" + "\n".join(f"  {e}" for e in part_errors)
        raise ValueError(msg)

    score = evaluate_expression(score_file.expression, values) if score_file.expression else None
    return {"Score": score, **values}


def main(argv: list[str] | None = None) -> None:
    """コマンドライン実行の入り口(引数解析 → 計算 → stdout へ JSON 出力)。"""
    # 版数表示(--version / stderr)でのみ使うため、使うときだけ読み込む
    from . import __version__  # ruff: ignore[PLC0415]

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"scorelib_param {__version__}")
    parser.add_argument("--config", required=True, help="run config jsonc (Generation + optimization{...})")
    parser.add_argument("--data-dir", required=True, help="directory containing {type}.csv etc. for this epoch")
    parser.add_argument(
        "--dvtbudget-coef", help="dVtBudget coefficient jsonc (required if any score part uses type=dVtBudget)"
    )
    parser.add_argument(
        "--initial-temperature", help="initial_temperature.csv (Board,Temperature; required for dVtBudget)"
    )
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
        args.data_dir,
        run_config,
        dvtbudget_coef=dvtbudget_coef,
        board_temperatures=board_temperatures,
        custom_parts_path=args.custom_parts,
        generation_info_path=args.generation_info,
    )
    # stdout には結果 JSON **だけ**を出す(最適化側がパースする)。
    # 版数の目印は実行ログ用に stderr へ
    print(f"scorelib_param {__version__}", file=sys.stderr)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
