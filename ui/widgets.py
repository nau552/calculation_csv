"""スコア設計UIの再利用ウィジェット。

各エディタは渡された dict（session の score_file の一部）をその場で書き換える。
検証は呼び出し元の画面が後段で ui.state.validate_* を通して行うので、
エラーメッセージは常にエンジン自身のものになる。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import streamlit as st

from scorelib.models import COMBINED_SEP, MULTI_OPS

# ドラッグ&ドロップ並べ替えはソフト依存: コミュニティ製カスタムコンポーネント
# （streamlit-sortables）は Streamlit 本体のメジャー更新で壊れうる。
# 未インストール・故障時は呼び出し側が上下ボタンへフォールバックするので、
# アプリ本体はこれに依存しない（設計書 8-3節）。
try:
    from streamlit_sortables import sort_items as _sort_items

    HAS_SORTABLES = True
except Exception:  # ImportError またはコンポーネントの破損
    HAS_SORTABLES = False

AXIS_OPS = ["filter", "mean", "sum", "min", "max", "diff", "expr"]

# コンポーネントの既定スタイルはテーマの primary 色（既定テーマだと真っ赤。
# ユーザ評: 目が痛い）+ 中央揃え（⠿ ハンドルが縦に揃わない）。
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


def sortable_list(items: List[str], key: str) -> Optional[List[str]]:
    """`items` をドラッグ&ドロップのリストとして描画し、（並べ替え後かも
    しれない）リストを返す。コンポーネントが使えない・挙動が怪しいときは
    None を返し、呼び出し側が上下ボタンへフォールバックする。

    key は項目テキスト由来にしてある: テキストが変わったら（別の場所での編集や
    こちらが適用した並べ替え）、古い内部状態を見せ続けずに新しい並びで
    再マウントさせるため。"""
    if not HAS_SORTABLES or not items:
        return None
    try:
        result = _sort_items(list(items), direction="vertical",
                             custom_style=_SORTABLE_STYLE,
                             key=f"{key}_{abs(hash(tuple(items)))}")
    except Exception:
        return None
    if isinstance(result, list) and sorted(result) == sorted(items):
        return result
    return None


def parse_scalar(text: str) -> Any:
    """自由入力テキスト → 型付きの軸の値（bool / int / float / str）。"""
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


def value_widget(container, label: str, candidates: Optional[list], current: Any, key: str) -> Any:
    """軸の値1つの入力: 候補が分かればプルダウン、無ければ自由入力
    （型付きスカラーにパース）。"""
    if candidates:
        index = candidates.index(current) if current in candidates else 0
        return container.selectbox(label, candidates, index=index, key=key, format_func=str)
    text = container.text_input(label, value="" if current is None else str(current), key=key)
    return parse_scalar(text) if text.strip() else None


def dict_selection_row(axes: List[str], catalog: Dict[str, Optional[list]], current: Any, key: str) -> Dict[str, Any]:
    """複合軸の選択1つ: 軸ごとのプルダウンを1行に並べて辞書を返す。"""
    cols = st.columns(len(axes))
    current = current if isinstance(current, dict) else {}
    return {
        a: value_widget(col, a, catalog.get(a), current.get(a), f"{key}_{a}")
        for col, a in zip(cols, axes)
    }


def selection_widget(axes: List[str], catalog: Dict[str, Optional[list]], current: Any, key: str) -> Any:
    """単一軸・複合軸どちらにも対応した選択1つぶんの入力。"""
    if len(axes) > 1:
        return dict_selection_row(axes, catalog, current, key)
    return value_widget(st, axes[0], catalog.get(axes[0]), current, key)


def selection_list_widget(
    axes: List[str], catalog: Dict[str, Optional[list]], values: list, key: str
) -> list:
    """可変長の選択リスト（mean/sum/min/max の対象選択と選択セット編集で使用）。
    行の追加・削除ができる。"""
    if len(axes) == 1 and catalog.get(axes[0]):
        cands = catalog[axes[0]]
        default = [v for v in values if v in cands]
        return st.multiselect(f"{axes[0]} の値", cands, default=default, key=key)

    out = []
    for i, v in enumerate(values):
        row_cols = st.columns([10, 1])
        with row_cols[0]:
            out.append(selection_widget(axes, catalog, v, f"{key}_r{i}"))
        if row_cols[1].button("✕", key=f"{key}_del{i}", help="この行を削除"):
            values.pop(i)
            st.rerun()
    if st.button("＋ 行を追加", key=f"{key}_add"):
        values.append({a: None for a in axes} if len(axes) > 1 else None)
        st.rerun()
    return out


def _ref_widget(spec: Dict[str, Any], set_names: List[str], key: str) -> None:
    if not set_names:
        st.caption("選択セットが未定義です（画面3で作成）")
        spec["ref"] = None
        return
    current = spec.get("ref")
    index = set_names.index(current) if current in set_names else 0
    spec["ref"] = st.selectbox("選択セット", set_names, index=index, key=f"{key}_ref")


def agg_editor(
    entry: str,
    spec: Dict[str, Any],
    catalog: Dict[str, Optional[list]],
    set_names: List[str],
    key: str,
) -> None:
    """order エントリごとの集計指示エディタ（設計書 画面2）。
    選んだ op に関係する入力欄だけを出すので、value/values の混同は
    UI 上は構造的に起きない。`spec` をその場で書き換える。"""
    axes = entry.split(COMBINED_SEP)
    is_virtual = entry.startswith("__")

    ops = ["add"] if is_virtual else AXIS_OPS
    cur_op = spec.get("op") if spec.get("op") in ops else ops[0]
    op = st.selectbox("op", ops, index=ops.index(cur_op), key=f"{key}_op")
    if op != spec.get("op"):
        # op が変わった: op 固有のフィールドが残らないよう掃除する
        # （"axis" だけは残す: 事前集計ステップは軸名を spec 内に持つため）
        axis_field = spec.get("axis")
        spec.clear()
        if axis_field is not None:
            spec["axis"] = axis_field
        spec["op"] = op
    spec["op"] = op

    if op == "add":
        spec["value"] = st.number_input("加算する値", value=float(spec.get("value") or 0.0), key=f"{key}_addv")
        return

    if op == "filter":
        spec.pop("ref", None)
        spec["value"] = selection_widget(axes, catalog, spec.get("value"), f"{key}_fv")
        return

    if op == "diff":
        source = st.radio("選択方法", ["直接指定", "選択セット(ref)"], key=f"{key}_dsrc", horizontal=True,
                          index=1 if spec.get("ref") else 0)
        if source == "選択セット(ref)":
            spec.pop("value", None)
            _ref_widget(spec, set_names, key)
        else:
            spec.pop("ref", None)
            v = spec.get("value") if isinstance(spec.get("value"), list) and len(spec["value"]) == 2 else [None, None]
            st.caption("結果 = a − b")
            a = selection_widget(axes, catalog, v[0], f"{key}_da")
            b = selection_widget(axes, catalog, v[1], f"{key}_db")
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
            spec["value"] = selection_list_widget(axes, catalog, spec["value"], f"{key}_mv")
        return

    if op == "expr":
        spec["expr"] = st.text_input(
            "式", value=spec.get("expr") or "", key=f"{key}_expr",
            help="values = この軸の全値のリスト。by[値] で特定の値を参照。例: max(values) - min(values), by['R2A'] + by['A2R']",
        )
        return


def relative_editor(
    part: Dict[str, Any],
    catalog: Dict[str, Optional[list]],
    set_names: List[str],
    key: str,
) -> None:
    """相対化ブロックのエディタ。part['relative'] の存在=ON（エンジンに
    enabled フラグは無い）。OFF にしたら split 軸を `order` へ復帰させる
    （放置するとエンジンが暗黙に集約して分子と分母の行が混ざる）。
    ON にしたとき・split 軸を変えたときは対称に、新しい split 軸を `order`
    から外す。"""
    from ui import state as ui_state

    prev_enabled = part.get("relative") is not None
    enabled = st.checkbox("相対化する（基準測定との比を取る）", value=prev_enabled, key=f"{key}_on")
    if not enabled:
        if prev_enabled:
            restored = ui_state.disable_relative(part, catalog)
            if restored:
                st.toast(f"'{restored}' を order に戻しました（filter {part['aggregations'][restored].get('value')}）")
            st.rerun()
        return
    if not prev_enabled:
        ui_state.enable_relative(part, catalog)
        st.rerun()
    rel = part["relative"]

    override_axes = [a for a in catalog if a.endswith("_Override")] or [rel.get("split_axis", "Read_Override")]
    c1, c2, c3 = st.columns(3)
    new_split = c1.selectbox(
        "split_axis（分子/分母を分ける軸）", override_axes,
        index=override_axes.index(rel.get("split_axis")) if rel.get("split_axis") in override_axes else 0,
        key=f"{key}_sa",
    )
    if new_split != rel.get("split_axis"):
        ui_state.change_split_axis(part, new_split, catalog)
        st.rerun()
    tf = [True, False]
    rel["numerator_when"] = c2.selectbox("分子側の値", tf, index=tf.index(bool(rel.get("numerator_when", True))),
                                         key=f"{key}_num", format_func=str)
    rel["denominator_when"] = c3.selectbox("分母側の値", tf, index=tf.index(bool(rel.get("denominator_when", False))),
                                           key=f"{key}_den", format_func=str)
    c4, c5 = st.columns(2)
    modes = ["ratio", "diff"]
    rel["mode"] = c4.selectbox("mode", modes, index=modes.index(rel.get("mode", "ratio")), key=f"{key}_mode",
                               help="ratio: (分子+offset)/(分母+offset)　diff: 分子 − 分母")
    if rel["mode"] == "ratio":
        rel["denominator_offset"] = c5.number_input("offset（分子分母の両方に加算）",
                                                    value=float(rel.get("denominator_offset", 0)), key=f"{key}_off")
    else:
        rel.pop("denominator_offset", None)

    with st.expander("分母の事前集計 (denominator_pre_aggregation)", expanded=bool(rel.get("denominator_pre_aggregation"))):
        st.caption("分母（基準測定）側だけに、比を取る前の集計を適用します（例: 分母はWL平均、分子はWLごと）。opごとの対象選択も通常の集計指示と同じように使えます")
        steps = rel.setdefault("denominator_pre_aggregation", [])
        for i, step in enumerate(steps):
            c_axis, c_del = st.columns([8, 1])
            axis_opts = sorted(catalog)
            step["axis"] = c_axis.selectbox("軸", axis_opts,
                                            index=axis_opts.index(step.get("axis")) if step.get("axis") in axis_opts else 0,
                                            key=f"{key}_pre{i}_axis")
            if c_del.button("✕", key=f"{key}_pre{i}_del", help="この事前集計を削除"):
                steps.pop(i)
                st.rerun()
            agg_editor(step["axis"], step, catalog, set_names, key=f"{key}_pre{i}")
            st.divider()
        if st.button("＋ 事前集計を追加", key=f"{key}_pre_add"):
            steps.append({"axis": sorted(catalog)[0], "op": "mean"})
            st.rerun()
        if not steps:
            rel.pop("denominator_pre_aggregation", None)
