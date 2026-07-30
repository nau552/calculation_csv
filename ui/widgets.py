# Copyright (c) 2026
"""スコア設計UIの再利用ウィジェット。

各エディタは渡された dict(session の score_file の一部)をその場で書き換える。
検証は呼び出し元の画面が後段で ui.state.validate_* を通して行うので、
エラーメッセージは常にエンジン自身のものになる。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import streamlit as st

from scorelib_param.models import COMBINED_SEP, MULTI_OPS, STEP_OPS, TRANSFORM_OPS, UNARY_OPS

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    from streamlit.delta_generator import DeltaGenerator

# ドラッグ&ドロップ並べ替えはソフト依存: コミュニティ製カスタムコンポーネント
# (streamlit-sortables)は Streamlit 本体のメジャー更新で壊れうる。
# 未インストール・故障時は呼び出し側が上下ボタンへフォールバックするので、
# アプリ本体はこれに依存しない(設計書 8-3節)。
try:
    from streamlit_sortables import sort_items as _sort_items

    HAS_SORTABLES = True
except Exception:  # ImportError またはコンポーネントの破損
    HAS_SORTABLES = False

AXIS_OPS = ["filter", "mean", "sum", "min", "max", "diff", "expr"]

# diff の被演算数: 結果 = a - b の2値ペア
_DIFF_OPERANDS = 2

# コンポーネントの既定スタイルはテーマの primary 色(既定テーマだと真っ赤。
# ユーザ評: 目が痛い)+ 中央揃え(⠿ ハンドルが縦に揃わない)。
# 左揃え+周囲の枠と一段違う半透明グレーに上書きし、ライト/ダーク両テーマで
# 「項目が独立した物」に見えるようにする。
_SORTABLE_STYLE = """
.sortable-item, .sortable-item:hover {
    background-color: rgba(128, 128, 128, 0.2);
    color: var(--text-color);
    border: 1px solid rgba(128, 128, 128, 0.45);
    border-radius: 4px;
    padding-left: 8px;
    text-align: left;
}
"""


def sortable_list(items: list[str], key: str) -> list[str] | None:
    """`items` をドラッグ&ドロップのリストとして描画する。

    (並べ替え後かもしれない)リストを返す。コンポーネントが使えない・
    挙動が怪しいときは None を返し、呼び出し側が上下ボタンへ
    フォールバックする。

    key は項目テキスト由来にしてある: テキストが変わったら(別の場所での編集や
    こちらが適用した並べ替え)、古い内部状態を見せ続けずに新しい並びで
    再マウントさせるため。
    """
    if not HAS_SORTABLES or not items:
        return None
    try:
        result = _sort_items(
            list(items), direction="vertical", custom_style=_SORTABLE_STYLE, key=f"{key}_{abs(hash(tuple(items)))}"
        )
    except Exception:
        return None
    if isinstance(result, list) and sorted(result) == sorted(items):
        return result
    return None


def measure_format(measure_labels: dict[int, str] | None) -> Callable[[object], str]:
    """Measure 軸の値の表示関数を返す。

    表示は「dataName (Measure N)」、名無しの番号は
    「Measure N」(docs/spec_change_dataname_measure.md 6.4節の複合表示。
    選択・保存されるのは常に番号そのもの)。
    """
    m = measure_labels or {}
    return lambda v: f"{m[v]} (Measure {v})" if v in m else f"Measure {v}"


def _axis_format(axis: str, measure_labels: dict[int, str] | None) -> Callable[[object], str]:
    return measure_format(measure_labels) if axis == "Measure" else str


def parse_scalar(text: str) -> bool | int | float | str:
    """自由入力テキスト → 型付きの軸の値(bool / int / float / str)。"""
    t = text.strip()
    if t.lower() == "true":
        return True
    if t.lower() == "false":
        return False
    for cast in (int, float):
        try:
            return cast(t)
        except ValueError:
            pass
    return t


def value_widget(
    container: DeltaGenerator | ModuleType,
    label: str,
    candidates: list | None,
    current: object,
    key: str,
    format_func: Callable[[object], str] = str,
) -> object:
    """軸の値1つの入力。

    候補が分かればプルダウン、無ければ自由入力
    (型付きスカラーにパース)。
    """
    if candidates:
        index = candidates.index(current) if current in candidates else 0
        return container.selectbox(label, candidates, index=index, key=key, format_func=format_func)
    text = container.text_input(label, value="" if current is None else str(current), key=key)
    return parse_scalar(text) if text.strip() else None


def dict_selection_row(
    axes: list[str],
    catalog: dict[str, list | None],
    current: object,
    key: str,
    measure_labels: dict[int, str] | None = None,
) -> dict[str, Any]:
    """複合軸の選択1つ: 軸ごとのプルダウンを1行に並べて辞書を返す。"""
    cols = st.columns(len(axes))
    current = current if isinstance(current, dict) else {}
    return {
        a: value_widget(col, a, catalog.get(a), current.get(a), f"{key}_{a}", _axis_format(a, measure_labels))
        for col, a in zip(cols, axes, strict=False)
    }


def selection_widget(
    axes: list[str],
    catalog: dict[str, list | None],
    current: object,
    key: str,
    measure_labels: dict[int, str] | None = None,
) -> object:
    """単一軸・複合軸どちらにも対応した選択1つぶんの入力。"""
    if len(axes) > 1:
        return dict_selection_row(axes, catalog, current, key, measure_labels)
    return value_widget(st, axes[0], catalog.get(axes[0]), current, key, _axis_format(axes[0], measure_labels))


def selection_list_widget(
    axes: list[str],
    catalog: dict[str, list | None],
    values: list,
    key: str,
    measure_labels: dict[int, str] | None = None,
) -> list:
    """可変長の選択リスト(mean/sum/min/max の対象選択と選択セット編集で使用)。

    行の追加・削除ができる。
    """
    if len(axes) == 1 and catalog.get(axes[0]):
        cands = catalog[axes[0]]
        default = [v for v in values if v in cands]
        return st.multiselect(
            f"{axes[0]} の値", cands, default=default, key=key, format_func=_axis_format(axes[0], measure_labels)
        )

    out = []
    for i, v in enumerate(values):
        row_cols = st.columns([10, 1])
        with row_cols[0]:
            out.append(selection_widget(axes, catalog, v, f"{key}_r{i}", measure_labels))
        if row_cols[1].button("✕", key=f"{key}_del{i}", help="この行を削除"):
            values.pop(i)
            st.rerun()
    if st.button("+ 行を追加", key=f"{key}_add"):
        values.append(dict.fromkeys(axes) if len(axes) > 1 else None)
        st.rerun()
    return out


def _ref_widget(spec: dict[str, Any], set_names: list[str], key: str) -> None:
    if not set_names:
        st.caption("選択セットが未定義です(画面3で作成)")
        spec["ref"] = None
        return
    current = spec.get("ref")
    index = set_names.index(current) if current in set_names else 0
    spec["ref"] = st.selectbox("選択セット", set_names, index=index, key=f"{key}_ref")


_TRANSFORM_LABELS = {
    "add": "add(足す)",
    "sub": "sub(引く)",
    "mul": "mul(掛ける)",
    "div": "div(割る)",
    "abs": "abs(絶対値)",
    "log": "log(対数)",
}


def _transform_editor(
    spec: dict[str, Any],
    op: str,
    by_candidates: list[str],
    by_value_labels: dict[str, list],
    weight_set_names: list[str],
    key: str,
) -> None:
    """変換ステップ(定数演算)の入力欄。

    全行共通の定数か、by 軸の値ごとの
    定数(グループ別重みなど)かを選ぶ。
    """
    modes = ["定数(全行共通)", "軸の値ごと(重み)"]
    mode = st.radio(
        "適用のしかた",
        modes,
        index=1 if spec.get("by") else 0,
        key=f"{key}_tmode",
        horizontal=True,
    )
    default = 1.0 if op in ("mul", "div") else 0.0

    if mode == modes[0]:
        spec.pop("by", None)
        spec.pop("ref", None)
        v = spec.get("value")
        spec["value"] = st.number_input(
            "値", value=float(v) if isinstance(v, (int, float)) else default, key=f"{key}_tv"
        )
        return

    if not by_candidates:
        st.caption("値ごとの定数を使うにはグループ定義(画面3)を作成してください")
        spec.pop("by", None)
        return
    cur = spec.get("by")
    spec["by"] = st.selectbox(
        "対象軸(この軸の値ごとに定数を変える)",
        by_candidates,
        index=by_candidates.index(cur) if cur in by_candidates else 0,
        key=f"{key}_tby",
        help="通常はグループ派生軸(例: WLgroup)。この軸がまだ列として残っている位置にステップを置いてください",
    )

    sources = ["重みセット(ref)", "直接入力"]
    src = st.radio(
        "重みの指定",
        sources,
        index=0 if spec.get("ref") else 1,
        key=f"{key}_tsrc",
        horizontal=True,
    )
    if src == sources[0]:
        if not weight_set_names:
            st.caption("重みセットが未定義です(画面3のグループ定義で作成、または設定jsoncの WLgroupWeight を読み込み)")
            spec["ref"] = None
            return
        spec.pop("value", None)
        cur_ref = spec.get("ref")
        spec["ref"] = st.selectbox(
            "重みセット",
            weight_set_names,
            index=weight_set_names.index(cur_ref) if cur_ref in weight_set_names else 0,
            key=f"{key}_tref",
        )
        return

    spec.pop("ref", None)
    labels = by_value_labels.get(spec["by"]) or []
    current = spec.get("value") if isinstance(spec.get("value"), dict) else {}
    if labels:
        weights = {}
        for i, label in enumerate(labels):
            v = current.get(label)
            weights[label] = st.number_input(
                str(label),
                value=float(v) if isinstance(v, (int, float)) else default,
                key=f"{key}_tw{i}",
            )
        spec["value"] = weights
    elif current:
        # 値の候補が分からない軸: 既存の辞書のキーだけ編集できる
        spec["value"] = {
            k: st.number_input(str(k), value=float(v), key=f"{key}_twk{i}") for i, (k, v) in enumerate(current.items())
        }
    else:
        st.caption("この軸の値の候補が分かりません。グループ派生軸を選ぶか、jsonc で直接記入してください")


def _agg_weight_editor(
    spec: dict[str, Any],
    labels: list[Any],
    weight_set_names: list[str],
    key: str,
) -> None:
    """集計時重み(任意)の入力欄。

    この軸を潰す直前に、軸の値ごとの重みを値へ乗じてから
    集計する(例: WLgroup の重み付き worst)。正規化された加重平均ではない
    (mean なら mean(重み * 値))。タイミングを制御したい場合は変換ステップ
    ("__xxx__" + by)を使う。
    """
    modes = ["なし", "重みセット(ref)", "値ごとに入力", "定数1つ"]
    if spec.get("weight_ref"):
        idx = 1
    elif isinstance(spec.get("weight"), dict):
        idx = 2
    elif spec.get("weight") is not None:
        idx = 3
    else:
        idx = 0
    mode = st.radio(
        "重み(集計の前に値へ掛ける)",
        modes,
        index=idx,
        key=f"{key}_wmode",
        horizontal=True,
        help="この軸の値ごとの重みを掛けてから集計します。"
        "掛けるタイミングを自分で制御したい場合は変換ステップ(__xxx__)を使ってください",
    )
    if mode == modes[0]:
        spec.pop("weight", None)
        spec.pop("weight_ref", None)
        return
    if mode == modes[1]:
        spec.pop("weight", None)
        if not weight_set_names:
            st.caption(
                "重みセットが未定義です(画面3のグループ定義で作成、または設定jsoncの WLgroupWeight / weightSets)"
            )
            spec["weight_ref"] = None
            return
        cur = spec.get("weight_ref")
        spec["weight_ref"] = st.selectbox(
            "重みセット",
            weight_set_names,
            index=weight_set_names.index(cur) if cur in weight_set_names else 0,
            key=f"{key}_wref",
        )
        return
    spec.pop("weight_ref", None)
    if mode == modes[3]:
        v = spec.get("weight")
        spec["weight"] = st.number_input(
            "重み", value=float(v) if isinstance(v, (int, float)) else 1.0, key=f"{key}_wc"
        )
        return
    current = spec.get("weight") if isinstance(spec.get("weight"), dict) else {}
    if labels:
        weights = {}
        for i, label in enumerate(labels):
            v = current.get(label)
            weights[label] = st.number_input(
                str(label),
                value=float(v) if isinstance(v, (int, float)) else 1.0,
                key=f"{key}_wv{i}",
            )
        spec["weight"] = weights
    elif current:
        # 値の候補が分からない軸: 既存の辞書のキーだけ編集できる
        spec["weight"] = {
            k: st.number_input(str(k), value=float(v), key=f"{key}_wvk{i}") for i, (k, v) in enumerate(current.items())
        }
    else:
        st.caption("この軸の値の候補が分かりません。重みセットを使うか、jsonc で直接記入してください")


def agg_editor(
    entry: str,
    spec: dict[str, Any],
    catalog: dict[str, list | None],
    set_names: list[str],
    key: str,
    by_candidates: list[str] | None = None,
    by_value_labels: dict[str, list] | None = None,
    weight_set_names: list[str] | None = None,
    measure_labels: dict[int, str] | None = None,
) -> None:
    """Order エントリごとの集計指示エディタ(設計書 画面2)。

    選んだ op に関係する入力欄だけを出すので、value/values の混同は
    UI 上は構造的に起きない。`spec` をその場で書き換える。

    `by_candidates` / `by_value_labels` / `weight_set_names` は変換ステップ
    ("__xxx__")の「軸の値ごとの定数(重み)」入力用: 対象軸の候補・軸ごとの
    値ラベル・参照できる重みセット名。`measure_labels` は Measure 軸の複合表示
    「dataName (Measure N)」用の 番号 → dataName 対応。
    """
    axes = entry.split(COMBINED_SEP)
    is_virtual = entry.startswith("__")

    ops = list(STEP_OPS) if is_virtual else AXIS_OPS
    cur_op = spec.get("op") if spec.get("op") in ops else ops[0]
    op = st.selectbox(
        "op",
        ops,
        index=ops.index(cur_op),
        key=f"{key}_op",
        format_func=(lambda o: _TRANSFORM_LABELS.get(o, o)) if is_virtual else str,
    )
    if op != spec.get("op"):
        # op が変わった: op 固有のフィールドが残らないよう掃除する
        # ("axis" だけは残す: 事前集計ステップは軸名を spec 内に持つため。
        # 変換op同士の切り替えでは by/value/ref を保つ — 演算だけ変える操作なので)
        if not (op in TRANSFORM_OPS and spec.get("op") in TRANSFORM_OPS):
            axis_field = spec.get("axis")
            spec.clear()
            if axis_field is not None:
                spec["axis"] = axis_field
        spec["op"] = op
    spec["op"] = op

    if op in UNARY_OPS:
        # 定数を取らない行単位の関数。log は床(floor)だけを入力する
        for k in ("value", "by", "ref"):
            spec.pop(k, None)
        if op == "log":
            v = spec.get("floor")
            spec["floor"] = st.number_input(
                "floor",
                value=float(v) if isinstance(v, (int, float)) else 1e-6,
                format="%.1e",
                key=f"{key}_floor",
                help="log(max(|x|, floor)) を計算します。0 や負の値でも発散しないための床です",
            )
        else:
            spec.pop("floor", None)
        return

    if op in TRANSFORM_OPS:
        _transform_editor(
            spec,
            op,
            by_candidates or [],
            by_value_labels or {},
            weight_set_names or [],
            key,
        )
        return

    if op == "filter":
        from ui import state as ui_state

        spec.pop("ref", None)
        if len(axes) == 1 and catalog.get(axes[0]):
            # 候補が分かる単一軸は複数選択可(複数 = is_in: 選んだ値の行を
            # すべて残し、後段の集計に複製として流す)
            cands = catalog[axes[0]]
            cur = spec.get("value")
            cur_list = cur if isinstance(cur, list) else ([cur] if cur is not None else [])
            picked = st.multiselect(
                f"{axes[0]} の値(複数選ぶと該当する値の行をすべて残します)",
                cands,
                default=[v for v in cur_list if v in cands],
                key=f"{key}_fv",
                format_func=_axis_format(axes[0], measure_labels),
            )
            spec["value"] = picked[0] if len(picked) == 1 else picked
        else:
            spec["value"] = selection_widget(axes, catalog, spec.get("value"), f"{key}_fv", measure_labels)
        if axes == ["Measure"]:
            ui_state.annotate_measure_labels(spec, measure_labels or {})
        return

    if op == "diff":
        source = st.radio(
            "選択方法",
            ["直接指定", "選択セット(ref)"],
            key=f"{key}_dsrc",
            horizontal=True,
            index=1 if spec.get("ref") else 0,
        )
        if source == "選択セット(ref)":
            spec.pop("value", None)
            _ref_widget(spec, set_names, key)
        else:
            spec.pop("ref", None)
            v = (
                spec.get("value")
                if isinstance(spec.get("value"), list) and len(spec["value"]) == _DIFF_OPERANDS
                else [None, None]
            )
            st.caption("結果 = a - b")
            a = selection_widget(axes, catalog, v[0], f"{key}_da", measure_labels)
            b = selection_widget(axes, catalog, v[1], f"{key}_db", measure_labels)
            spec["value"] = [a, b]
        return

    if op in MULTI_OPS:
        modes = ["全値", "値を選択", "選択セット(ref)"]
        if spec.get("ref"):
            mode_idx = 2
        elif spec.get("value") is not None:
            mode_idx = 1
        else:
            mode_idx = 0
        mode = st.radio("対象", modes, index=mode_idx, key=f"{key}_mode", horizontal=True)
        if mode == "全値":
            spec.pop("value", None)
            spec.pop("ref", None)
        elif mode == "選択セット(ref)":
            spec.pop("value", None)
            _ref_widget(spec, set_names, key)
        else:
            spec.pop("ref", None)
            if not isinstance(spec.get("value"), list):
                spec["value"] = []
            # dict が持つリストそのものを渡す: 行の追加・削除の変更が
            # st.rerun() をまたいで生き残るように
            spec["value"] = selection_list_widget(axes, catalog, spec["value"], f"{key}_mv", measure_labels)
        if len(axes) == 1:
            # 集計時重み(複合軸は jsonc 直接記入のみ対応)。値ラベルは
            # by_value_labels 優先: グループ派生軸(WLgroup等)の名前は
            # catalog(データ軸の値)には無く、そちらにしか入っていない
            labels = (by_value_labels or {}).get(axes[0]) or catalog.get(axes[0]) or []
            _agg_weight_editor(spec, labels, weight_set_names or [], key)
        return

    if op == "expr":
        spec["expr"] = st.text_input(
            "式",
            value=spec.get("expr") or "",
            key=f"{key}_expr",
            help="values = この軸の全値のリスト。by[値] で特定の値を参照。"
            "例: max(values) - min(values), by['R2A'] + by['A2R']",
        )
        return


def relative_editor(
    part: dict[str, Any],
    catalog: dict[str, list | None],
    set_names: list[str],
    key: str,
    measure_labels: dict[int, str] | None = None,
) -> None:
    """相対化ブロックのエディタ。

    part['relative'] の存在=ON(エンジンに
    enabled フラグは無い)。OFF にしたら split 軸を `order` へ復帰させる
    (放置するとエンジンが暗黙に集約して分子と分母の行が混ざる)。
    ON にしたとき・split 軸を変えたときは対称に、新しい split 軸を `order`
    から外す。

    split 軸は任意の軸から選べる(旧仕様の Override 限定は廃止 —
    docs/spec_change_dataname_measure.md)。基本形は Measure 番号での指定で、
    dataName が分かる番号は「dataName (Measure N)」と表示し、選んだ番号の
    dataName を labels 注記として設定に残す。
    """
    from ui import state as ui_state

    prev_enabled = part.get("relative") is not None
    enabled = st.checkbox("相対化する(基準測定との比を取る)", value=prev_enabled, key=f"{key}_on")
    if not enabled:
        if prev_enabled:
            restored = ui_state.disable_relative(part, catalog)
            if restored:
                st.toast(f"'{restored}' を order に戻しました(filter {part['aggregations'][restored].get('value')})")
            st.rerun()
        return
    if not prev_enabled:
        ui_state.enable_relative(part, catalog)
        st.rerun()
    rel = part["relative"]

    axis_opts = [a for a in catalog if a != "InBatchEpoch"]
    cur_split = rel.get("split_axis")
    if cur_split and cur_split not in axis_opts:
        axis_opts = [cur_split, *axis_opts]
    c1, c2, c3 = st.columns(3)
    new_split = c1.selectbox(
        "split_axis(分子/分母を分ける軸)",
        axis_opts,
        index=axis_opts.index(cur_split) if cur_split in axis_opts else 0,
        key=f"{key}_sa",
        help="通常は Measure(測定番号)。Measure 列の無い集計済み type では任意の軸(Chip 等)で割り算・引き算ができます",
    )
    if new_split != rel.get("split_axis"):
        ui_state.change_split_axis(part, new_split, catalog)
        st.rerun()
    fmt = _axis_format(new_split, measure_labels)
    cands = catalog.get(new_split)
    rel["numerator_when"] = value_widget(c2, "分子側の値(評価側)", cands, rel.get("numerator_when"), f"{key}_num", fmt)
    rel["denominator_when"] = value_widget(
        c3, "分母側の値(基準側)", cands, rel.get("denominator_when"), f"{key}_den", fmt
    )
    if new_split == "Measure":
        ui_state.annotate_measure_labels(rel, measure_labels or {})
    else:
        rel.pop("labels", None)
    c4, c5 = st.columns(2)
    modes = ["ratio", "diff"]
    rel["mode"] = c4.selectbox(
        "mode",
        modes,
        index=modes.index(rel.get("mode", "ratio")),
        key=f"{key}_mode",
        help="ratio: (分子+offset)/(分母+offset)　diff: 分子 - 分母",
    )
    if rel["mode"] == "ratio":
        rel["denominator_offset"] = c5.number_input(
            "offset(分子分母の両方に加算)", value=float(rel.get("denominator_offset", 0)), key=f"{key}_off"
        )
    else:
        rel.pop("denominator_offset", None)

    with st.expander(
        "分母の事前集計 (denominator_pre_aggregation)", expanded=bool(rel.get("denominator_pre_aggregation"))
    ):
        st.caption(
            "分母(基準測定)側だけに、比を取る前の集計を適用します(例: 分母はWL平均、分子はWLごと)。"
            "opごとの対象選択も通常の集計指示と同じように使えます"
        )
        steps = rel.setdefault("denominator_pre_aggregation", [])
        for i, step in enumerate(steps):
            c_axis, c_del = st.columns([8, 1])
            axis_opts = sorted(catalog)
            step["axis"] = c_axis.selectbox(
                "軸",
                axis_opts,
                index=axis_opts.index(step.get("axis")) if step.get("axis") in axis_opts else 0,
                key=f"{key}_pre{i}_axis",
            )
            if c_del.button("✕", key=f"{key}_pre{i}_del", help="この事前集計を削除"):
                steps.pop(i)
                st.rerun()
            agg_editor(step["axis"], step, catalog, set_names, key=f"{key}_pre{i}", measure_labels=measure_labels)
            st.divider()
        if st.button("+ 事前集計を追加", key=f"{key}_pre_add"):
            steps.append({"axis": min(catalog), "op": "mean"})
            st.rerun()
        if not steps:
            rel.pop("denominator_pre_aggregation", None)
