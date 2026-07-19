"""Reusable Streamlit widgets for the score-design UI.

Each editor mutates the passed-in dict (part of the session's score_file)
in place; validation happens afterwards in the calling screen via
ui.state.validate_*, so error messages are always the engine's own.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import streamlit as st

from scorelib.models import COMBINED_SEP

# Drag & drop reordering is a soft dependency: a community custom component
# (streamlit-sortables) that could break on a Streamlit major update. When it
# is missing or fails, callers fall back to up/down buttons, so the app never
# depends on it (design doc section 8-3).
try:
    from streamlit_sortables import sort_items as _sort_items

    HAS_SORTABLES = True
except Exception:  # ImportError or a broken component install
    HAS_SORTABLES = False

AXIS_OPS = ["filter", "mean", "sum", "min", "max", "diff", "group_reduce", "expr"]
_MULTI_OPS = ("mean", "sum", "min", "max")


def sortable_list(items: List[str], key: str) -> Optional[List[str]]:
    """Render `items` as a drag&drop list and return the (possibly reordered)
    list. Returns None when the component is unavailable or misbehaves --
    the caller then falls back to up/down buttons.

    The key is derived from the item texts: when they change (edit elsewhere,
    or a reorder we applied), the component remounts with the fresh order
    instead of showing stale internal state."""
    if not HAS_SORTABLES or not items:
        return None
    try:
        result = _sort_items(list(items), direction="vertical",
                             key=f"{key}_{abs(hash(tuple(items)))}")
    except Exception:
        return None
    if isinstance(result, list) and sorted(result) == sorted(items):
        return result
    return None


def parse_scalar(text: str) -> Any:
    """Free-text input -> typed axis value (bool / int / float / str)."""
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
    """One axis-value input: dropdown when candidates are known, free text
    (parsed to a typed scalar) otherwise."""
    if candidates:
        index = candidates.index(current) if current in candidates else 0
        return container.selectbox(label, candidates, index=index, key=key, format_func=str)
    text = container.text_input(label, value="" if current is None else str(current), key=key)
    return parse_scalar(text) if text.strip() else None


def dict_selection_row(axes: List[str], catalog: Dict[str, Optional[list]], current: Any, key: str) -> Dict[str, Any]:
    """One combined-axis selection: a per-axis dropdown row -> dict."""
    cols = st.columns(len(axes))
    current = current if isinstance(current, dict) else {}
    return {
        a: value_widget(col, a, catalog.get(a), current.get(a), f"{key}_{a}")
        for col, a in zip(cols, axes)
    }


def selection_widget(axes: List[str], catalog: Dict[str, Optional[list]], current: Any, key: str) -> Any:
    """One selection on a plain or combined axis."""
    if len(axes) > 1:
        return dict_selection_row(axes, catalog, current, key)
    return value_widget(st, axes[0], catalog.get(axes[0]), current, key)


def selection_list_widget(
    axes: List[str], catalog: Dict[str, Optional[list]], values: list, key: str
) -> list:
    """A variable-length list of selections (used by mean/sum/min/max and
    selection-set editing). Rows can be added/removed."""
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
    group_def_names: List[str],
    key: str,
) -> None:
    """The per-order-entry aggregation instruction editor (design doc screen 2).
    Only the inputs relevant to the chosen op are shown, so the value/values
    confusion cannot occur in the UI. Mutates `spec` in place."""
    axes = entry.split(COMBINED_SEP)
    is_virtual = entry.startswith("__")

    ops = ["add"] if is_virtual else AXIS_OPS
    cur_op = spec.get("op") if spec.get("op") in ops else ops[0]
    op = st.selectbox("op", ops, index=ops.index(cur_op), key=f"{key}_op")
    if op != spec.get("op"):
        # op changed: drop op-specific fields so stale ones don't linger
        # ("axis" survives: pre-aggregation steps carry their axis in the spec)
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

    if op in _MULTI_OPS:
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
            # pass the dict's own list so add/remove-row mutations survive st.rerun()
            spec["value"] = selection_list_widget(axes, catalog, spec["value"], f"{key}_mv")
        return

    if op == "group_reduce":
        if not group_def_names:
            st.warning("WLgroup 定義が読み込まれていません（画面1で設定jsoncを含むディレクトリを読み込むと使えます）")
        gd = spec.get("group_def")
        options = group_def_names or ([gd] if gd else [])
        if options:
            spec["group_def"] = st.selectbox(
                "group_def", options, index=options.index(gd) if gd in options else 0, key=f"{key}_gd"
            )
        io_ops = ["mean", "sum", "min", "max"]
        c1, c2 = st.columns(2)
        spec["inner_op"] = c1.selectbox("グループ内 (inner_op)", io_ops,
                                        index=io_ops.index(spec.get("inner_op") or "mean"), key=f"{key}_io")
        spec["outer_op"] = c2.selectbox("グループ間 (outer_op)", io_ops,
                                        index=io_ops.index(spec.get("outer_op") or "mean"), key=f"{key}_oo")
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
    group_def_names: List[str],
    key: str,
) -> None:
    """The relative-ization block editor. Presence of part['relative'] means
    ON (the engine has no enabled flag). Turning it off restores the split
    axis into `order` (otherwise the engine would silently aggregate over
    it, mixing numerator and denominator rows); turning it on / changing the
    split axis removes the new split axis from `order` symmetrically."""
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
            agg_editor(step["axis"], step, catalog, set_names, group_def_names, key=f"{key}_pre{i}")
            st.divider()
        if st.button("＋ 事前集計を追加", key=f"{key}_pre_add"):
            steps.append({"axis": sorted(catalog)[0], "op": "mean"})
            st.rerun()
        if not steps:
            rel.pop("denominator_pre_aggregation", None)
