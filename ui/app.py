"""スコア設計UI (score_gui Phase1).

起動: .venv/Scripts/streamlit run ui/app.py

サイドバーで5画面を切り替える。判断ロジックはすべて ui/state.py と
scorelib 側にあり、本ファイルはウィジェット配置と session_state の
受け渡しのみを行う（score_gui_ui_design.md 参照）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scorelib.models import COMBINED_SEP  # noqa: E402
from ui import state, widgets  # noqa: E402

SCREENS = [
    "1. データ読み込み",
    "2. スコアパーツ編集",
    "3. 選択セット管理",
    "4. スコア合成・制約",
    "5. テスト実行・エクスポート",
]
VIRTUAL_FIXED = ("__relative__", "__dvtbudget__")


HISTORY_LIMIT = 20
# session_state keys that hold app data rather than widget state; everything
# else is wiped on undo so widgets re-read their values from score_file
_RESERVED_STATE = {
    "score_file", "context", "selected_part", "draft_prompt_done",
    "history", "last_snapshot", "screen",
}


def _init() -> None:
    ss = st.session_state
    ss.setdefault("score_file", state.empty_score_file())
    ss.setdefault("context", None)
    ss.setdefault("selected_part", 0)
    ss.setdefault("draft_prompt_done", False)
    ss.setdefault("history", [])
    ss.setdefault("last_snapshot", None)


def _snapshot(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _track_history() -> None:
    """One undo entry per user action: runs at the end of each settled rerun
    (reruns triggered mid-script skip this, so intermediate states are not
    recorded)."""
    ss = st.session_state
    snap = _snapshot(ss.score_file)
    if ss.last_snapshot is None:
        ss.last_snapshot = snap
    elif snap != ss.last_snapshot:
        ss.history.append(ss.last_snapshot)
        del ss.history[:-HISTORY_LIMIT]
        ss.last_snapshot = snap


def _undo() -> None:
    ss = st.session_state
    if not ss.history:
        return
    prev = ss.history.pop()
    ss.score_file = json.loads(prev)
    ss.last_snapshot = prev
    # widgets remember their own state, so without this the screen would
    # keep showing the pre-undo values
    for k in list(ss.keys()):
        if k not in _RESERVED_STATE:
            del ss[k]
    st.rerun()


def _offer_draft_restore() -> None:
    ss = st.session_state
    if ss.draft_prompt_done or ss.score_file["score_parts"]:
        ss.draft_prompt_done = True
        return
    draft = state.load_draft()
    if draft is None:
        ss.draft_prompt_done = True
        return
    st.info(f"前回の編集内容が {state.DRAFT_PATH} に残っています。復元しますか？")
    ci = draft.get("context_inputs") or {}
    if ci.get("data_dir"):
        st.caption(f"データ読み込みも復元されます: {ci['data_dir']}")
    c1, c2 = st.columns(2)
    if c1.button("復元する", key="restore_btn"):
        ss.score_file = draft["score_file"]
        state.ensure_uids(ss.score_file)
        if ci.get("data_dir"):
            try:
                ss.context = state.build_context(
                    ci["data_dir"], ci.get("config_path"), ci.get("coef_path")
                )
            except Exception as err:
                ss.restore_warning = (
                    f"編集内容は復元しましたが、データの再読み込みに失敗しました。"
                    f"画面1で読み込み直してください: {err}"
                )
        ss.draft_prompt_done = True
        st.rerun()
    if c2.button("破棄して新規に始める", key="discard_btn"):
        ss.draft_prompt_done = True
        st.rerun()
    st.stop()


def _autosave() -> None:
    ss = st.session_state
    sf = ss.score_file
    if sf["score_parts"] or sf["selectionSets"] or sf["expression"]:
        ctx = ss.context or {}
        context_inputs = {
            "data_dir": ctx.get("data_dir"),
            "config_path": ctx.get("config_path"),
            "coef_path": ctx.get("coef_path"),
        }
        try:
            state.save_draft(sf, context_inputs)
        except OSError:
            pass


def _merged_catalog(ctx) -> dict:
    merged: dict = {}
    for cat in ctx["catalogs"].values():
        for axis, cands in cat.items():
            merged.setdefault(axis, cands)
    return merged


def _catalog_for_part(ctx, part) -> dict:
    return ctx["catalogs"].get(part.get("type"), _merged_catalog(ctx))


# ------------------------------------------------------------ 画面1: 読み込み

def screen_data() -> None:
    ss = st.session_state
    st.header("データ読み込み")
    st.caption(
        "**同系統の過去実験の測定結果ディレクトリ**（result_tmp 相当）を指定してください。"
        "測定前の実験には定義ファイルが存在しないため、前回出力から type・軸・値候補を読み取ります。"
    )
    default = ss.context["data_dir"] if ss.context else ""
    path = st.text_input("測定結果ディレクトリのパス（必須）", value=default, key="data_dir_input")
    st.caption(
        "optimization設定jsonc と dVtBudget係数jsonc は通常 result_tmp には含まれないため、"
        "使う場合は個別にパスを指定してください（ディレクトリ内に置いてあれば自動検出もされます）。"
        "initial_temperature.csv は測定結果としてディレクトリ内から読み取ります。"
    )
    c1, c2 = st.columns(2)
    config_in = c1.text_input(
        "optimization設定jsonc のパス（任意）", key="config_path_input",
        help="Generation / WLgroup / selectionSets / 既存スコア設定の取り込み元。未指定でも設計は始められます",
    )
    coef_in = c2.text_input(
        "dVtBudget係数jsonc のパス（任意）", key="coef_path_input",
        help="未指定の場合は dVtBudget タイプが選択肢に出ません",
    )
    if st.button("読み込み", type="primary", key="load_btn"):
        try:
            ss.context = state.build_context(path, config_in, coef_in)
            st.toast("読み込みました")
        except Exception as err:
            st.error(str(err))
            return

    ctx = ss.context
    if not ctx:
        return

    st.subheader("認識結果")
    st.caption(f"走査したディレクトリ: `{ctx['data_dir']}`")
    if ctx["config_path"]:
        st.success(f"optimization設定jsonc（{ctx['config_source']}）: {ctx['config_path']}")
    else:
        st.info("optimization設定jsonc: なし — WLgroup / Generation / 既存スコア設定なしで設計を始めます")
    if ctx["coef_path"]:
        st.success(f"dVtBudget係数jsonc（{ctx['coef_source']}）: {ctx['coef_path']}")
    else:
        st.info("dVtBudget係数jsonc: なし — dVtBudget タイプは選択肢に出ません")
    if ctx["has_initial_temperature"]:
        st.success("initial_temperature.csv")
    else:
        st.warning("initial_temperature.csv: なし（dVtBudget のテスト計算に必要）")
    if ctx["generation"]:
        st.info(f"Generation: {ctx['generation']} / WLgroup: {list(ctx['wlgroup']) or 'なし'}")

    st.subheader(f"検出された type: {', '.join(ctx['part_types'])}")
    for t in ctx["part_types"]:
        with st.expander(f"type: {t} の軸と値候補"):
            rows = []
            for axis, cands in ctx["catalogs"][t].items():
                if cands is None:
                    preview = "（自由入力）"
                else:
                    preview = ", ".join(str(c) for c in cands[:10]) + ("…" if len(cands) > 10 else "")
                rows.append({"軸": axis, "値候補": preview})
            st.table(rows)

    if ctx["existing_score_file"] and not ss.score_file["score_parts"]:
        st.divider()
        if st.button("設定jsonc内の既存スコア設定を読み込んで編集を始める"):
            ss.score_file = ctx["existing_score_file"]
            state.ensure_uids(ss.score_file)
            st.rerun()


# ------------------------------------------------------ 画面2: スコアパーツ編集

def _order_entry_label(entry: str, part: dict) -> str:
    if entry == "__relative__":
        return "__relative__（相対化を実行）"
    if entry == "__dvtbudget__":
        return "__dvtbudget__（dVtBudget変換を実行）"
    spec = part.get("aggregations", {}).get(entry, {})
    detail = spec.get("op", "?")
    if spec.get("ref"):
        detail += f" (ref: {spec['ref']})"
    elif spec.get("value") is not None:
        detail += f" ({spec['value']})"
    return f"{entry} — {detail}"


def _add_entry_controls(part: dict, catalog: dict, uid: str) -> None:
    used: set = set()
    for e in part["order"]:
        if not e.startswith("__"):
            used.update(e.split(COMBINED_SEP))
    if part.get("relative"):
        used.add(part["relative"].get("split_axis"))
    unused = [a for a in catalog if a not in used and a != "InBatchEpoch"]

    st.markdown("**エントリの追加**")
    c1, c2 = st.columns([3, 1])
    axis = c1.selectbox("軸", ["（選択）"] + unused, key=f"{uid}_addax")
    if c2.button("追加", key=f"{uid}_addax_btn") and axis != "（選択）":
        part["order"].append(axis)
        part["aggregations"][axis] = state.default_aggregation(axis, catalog.get(axis))
        st.rerun()

    c3, c4 = st.columns([3, 1])
    combo = c3.multiselect("複合軸（複数選択して束ねる）", unused, key=f"{uid}_addcombo")
    if c4.button("束ねて追加", key=f"{uid}_addcombo_btn") and len(combo) >= 2:
        entry = COMBINED_SEP.join(combo)
        part["order"].append(entry)
        part["aggregations"][entry] = {"op": "sum"}
        st.rerun()

    if st.button("＋ 定数加算ステップを追加", key=f"{uid}_addvirt_btn",
                 help="値に定数を足すステップ（__offset__）を order に追加します。"
                      "典型例: オフセットを足してから相対化する。実行位置は上下ボタンで調整してください"):
        base, name, n = "__offset__", "__offset__", 2
        while name in part["order"]:
            name = f"__offset{n}__"
            n += 1
        part["order"].append(name)
        part["aggregations"][name] = {"op": "add", "value": 0}
        st.rerun()

    fixed_missing = []
    if part.get("relative") and "__relative__" not in part["order"]:
        fixed_missing.append("__relative__")
    if part.get("type") == "dVtBudget" and "__dvtbudget__" not in part["order"]:
        fixed_missing.append("__dvtbudget__")
    if fixed_missing:
        c7, c8 = st.columns([3, 1])
        step = c7.selectbox(
            "実行位置を明示する（省略時は先頭で実行）", fixed_missing, key=f"{uid}_addfixed",
            help="__relative__ / __dvtbudget__ を order に置くと、その位置で実行されます（例: 先にWL平均→相対化）",
        )
        if c8.button("orderに置く", key=f"{uid}_addfixed_btn"):
            part["order"].insert(0, step)
            st.rerun()


def _order_editor(part: dict, catalog: dict, sets: dict, wlgroup: dict, uid: str) -> None:
    """Order list: summary rows with reorder/delete, plus one always-open
    editor for the selected entry (an expander would collapse whenever its
    label changed, making value editing painful)."""
    sel_key = f"{uid}_sel_entry"
    order = part["order"]
    if st.session_state.get(sel_key) not in order:
        st.session_state[sel_key] = order[0] if order else None

    # One always-draggable list (案A): reordering needs no mode switch. The
    # community D&D component can only render plain string lists, so entry
    # selection lives in a selectbox and deletion in the editor below.
    labels = [_order_entry_label(e, part) for e in order]
    used_dnd = False
    if widgets.HAS_SORTABLES and order and len(set(labels)) == len(labels):
        st.markdown("**order（上から順に実行）** — リストをドラッグすると並べ替えられます")
        new_labels = widgets.sortable_list(labels, key=f"{uid}_dnd")
        if new_labels is not None:
            used_dnd = True
            if new_labels != labels:
                by_label = dict(zip(labels, order))
                part["order"] = [by_label[lbl] for lbl in new_labels]
                st.rerun()
            st.selectbox("編集するエントリ", order, key=sel_key)

    if not used_dnd:
        st.markdown("**order（上から順に実行）** — ✎ でエントリを選ぶと下に編集欄が出ます")
        for i, entry in enumerate(list(order)):
            c_sel, c_lbl, c_up, c_dn, c_rm = st.columns([1, 8, 1, 1, 1])
            selected = st.session_state.get(sel_key) == entry
            if c_sel.button("✎", key=f"{uid}_sel{i}", type="primary" if selected else "secondary",
                            help="このエントリを編集"):
                st.session_state[sel_key] = entry
                st.rerun()
            label = _order_entry_label(entry, part)
            c_lbl.markdown(f"**{label}**" if selected else label)
            if c_up.button("↑", key=f"{uid}_up{i}", disabled=i == 0):
                state.move_entry(order, i, -1)
                st.rerun()
            if c_dn.button("↓", key=f"{uid}_dn{i}", disabled=i == len(order) - 1):
                state.move_entry(order, i, +1)
                st.rerun()
            if c_rm.button("✕", key=f"{uid}_rm{i}", help="エントリを削除"):
                order.remove(entry)
                part["aggregations"].pop(entry, None)
                st.rerun()

    entry = st.session_state.get(sel_key)
    if entry and entry in order:
        with st.container(border=True):
            c_head, c_del = st.columns([8, 1])
            c_head.markdown(f"**編集中: {entry}**")
            if c_del.button("削除", key=f"{uid}_ed_del", help="このエントリを order から削除"):
                order.remove(entry)
                part["aggregations"].pop(entry, None)
                st.rerun()
            if entry in VIRTUAL_FIXED:
                st.caption("このステップに集計指示はありません（位置のみ意味を持ちます）")
            else:
                spec = part["aggregations"].setdefault(entry, {"op": "mean"})
                widgets.agg_editor(
                    entry, spec, catalog,
                    set_names=sorted(sets),
                    group_def_names=["WLgroup"] if wlgroup else [],
                    key=f"{uid}_{entry}",
                )


def screen_parts() -> None:
    ss = st.session_state
    ctx = ss.context
    st.header("スコアパーツ編集")
    if not ctx:
        st.info("先に画面1でデータを読み込んでください")
        return
    sf = ss.score_file
    state.ensure_uids(sf)

    rows = state.part_summary_rows(sf)
    parts_dnd = False
    if widgets.HAS_SORTABLES and len(sf["score_parts"]) > 1:
        labels = [
            f"{i + 1}. {r['名前']}（{r['type']}, 相対化{r['相対化']}）" for i, r in enumerate(rows)
        ]
        new_labels = widgets.sortable_list(labels, key="parts_dnd")
        if new_labels is not None:
            parts_dnd = True
            st.caption("パーツ一覧はドラッグで並べ替えられます")
            if new_labels != labels:
                sel_uid = sf["score_parts"][min(ss.selected_part, len(labels) - 1)]["_uid"]
                by_label = dict(zip(labels, sf["score_parts"]))
                sf["score_parts"] = [by_label[lbl] for lbl in new_labels]
                ss.selected_part = next(
                    i for i, p in enumerate(sf["score_parts"]) if p["_uid"] == sel_uid
                )
                st.rerun()
    if rows and not parts_dnd:
        st.dataframe(rows, use_container_width=True, hide_index=True)

    c1, c2, c3 = st.columns([2, 1, 1])
    new_type = c1.selectbox("新規パーツの type", ctx["part_types"], key="new_part_type")
    if c2.button("追加（雛形を生成）", type="primary", key="add_part_btn"):
        name = state.unique_part_name(sf)
        part = state.part_skeleton(name, new_type, ctx["catalogs"][new_type])
        sf["score_parts"].append(part)
        state.ensure_uids(sf)
        ss.selected_part = len(sf["score_parts"]) - 1
        st.rerun()
    with c3.popover("単体インポート"):
        up = st.file_uploader("エクスポートしたパーツjsonc", type=["jsonc", "json"], key="part_import")
        if up is not None and st.button("取り込む", key="part_import_btn"):
            try:
                imported = state.import_score_file(up.getvalue().decode("utf-8"))
                for p in imported["score_parts"]:
                    p["name"] = p["name"] if p["name"] not in state.part_names(sf) else state.unique_part_name(sf, p["name"])
                    sf["score_parts"].append(p)
                for k, v in imported.get("selectionSets", {}).items():
                    sf["selectionSets"].setdefault(k, v)
                state.ensure_uids(sf)
                st.rerun()
            except Exception as err:
                st.error(str(err))

    if not sf["score_parts"]:
        return
    st.divider()

    names = state.part_names(sf)
    idx = min(ss.selected_part, len(names) - 1)
    idx = st.selectbox("編集するパーツ", range(len(names)), index=idx, format_func=lambda i: names[i])
    ss.selected_part = idx
    part = sf["score_parts"][idx]
    uid = part["_uid"]
    catalog = _catalog_for_part(ctx, part)
    before = _snapshot(part)

    cup, cdn, cdup, cdel = st.columns(4)
    if cup.button("▲ 上へ", disabled=idx == 0, help="このパーツを一覧の1つ上へ"):
        ss.selected_part = state.move_entry(sf["score_parts"], idx, -1)
        st.rerun()
    if cdn.button("▼ 下へ", disabled=idx == len(names) - 1, help="このパーツを一覧の1つ下へ"):
        ss.selected_part = state.move_entry(sf["score_parts"], idx, +1)
        st.rerun()
    if cdup.button("このパーツを複製"):
        ss.selected_part = state.duplicate_part(sf, idx)
        state.ensure_uids(sf)
        st.rerun()
    if cdel.button("このパーツを削除"):
        sf["score_parts"].pop(idx)
        ss.selected_part = 0
        st.rerun()

    part["name"] = st.text_input("name", value=part.get("name", ""), key=f"{uid}_name")
    types = ctx["part_types"]
    cur_type = part.get("type")
    tsel, tregen = st.columns([3, 1])
    part["type"] = tsel.selectbox(
        "type", types, index=types.index(cur_type) if cur_type in types else 0, key=f"{uid}_type"
    )
    if part["type"] != cur_type:
        st.warning("type を変更しました。軸構成が異なる type の場合は雛形の再生成を推奨します")
    if tregen.button("雛形を再生成", key=f"{uid}_regen", help="現在の type の全軸でパーツを作り直します（編集内容は失われます）"):
        fresh = state.part_skeleton(part["name"], part["type"], ctx["catalogs"][part["type"]])
        fresh["_uid"] = uid
        sf["score_parts"][idx] = fresh
        st.rerun()

    st.divider()
    widgets.relative_editor(
        part, catalog,
        set_names=sorted(sf["selectionSets"]),
        group_def_names=["WLgroup"] if ctx["wlgroup"] else [],
        key=f"{uid}_rel",
    )
    st.divider()
    _order_editor(part, catalog, sf["selectionSets"], ctx["wlgroup"], uid)
    _add_entry_controls(part, catalog, uid)
    if _snapshot(part) != before:
        # a widget changed the part this run: rerun immediately so the
        # summary rows / labels above reflect it without a second click
        st.rerun()

    problems = state.validate_part(part, sf["selectionSets"])
    for p in problems:
        st.error(p)
    if not problems:
        st.success("このパーツの検証: OK")


# ------------------------------------------------------ 画面3: 選択セット管理

def screen_sets() -> None:
    ss = st.session_state
    ctx = ss.context
    st.header("選択セット管理")
    st.caption("複数パーツで使い回す値の組（例: State と Read_Label の対応リスト）に名前を付けて管理します。パーツ側からは ref で参照します。")
    sf = ss.score_file
    sets = sf["selectionSets"]

    if sets:
        st.dataframe(
            [
                {"名前": n, "件数": len(v), "参照しているパーツ": ", ".join(state.referencing_parts(sf, n)) or "（なし）"}
                for n, v in sets.items()
            ],
            use_container_width=True, hide_index=True,
        )

    c1, c2 = st.columns([3, 1])
    new_name = c1.text_input("新規セット名", key="new_set_name")
    if c2.button("作成") and new_name.strip():
        if new_name in sets:
            st.error(f"'{new_name}' は既に存在します")
        else:
            sets[new_name.strip()] = []
            st.rerun()

    if not sets:
        return
    st.divider()
    name = st.selectbox("編集するセット", sorted(sets), key="edit_set_name")
    values = sets[name]

    catalog = _merged_catalog(ctx) if ctx else {}
    all_axes = sorted(catalog) if catalog else []
    current_axes = sorted(values[0].keys()) if values and isinstance(values[0], dict) else []
    kind = st.radio(
        "形式", ["複合軸（軸ごとの値の組）", "単一軸の値リスト"],
        index=0 if (not values or isinstance(values[0], dict)) else 1, horizontal=True,
        key=f"set_{name}_kind",
    )
    if kind.startswith("複合軸"):
        axes = st.multiselect(
            "軸", all_axes or current_axes, default=current_axes or [a for a in ("State", "Read_Label") if a in catalog],
            key=f"set_{name}_axes",
        )
        if axes:
            if current_axes and sorted(axes) != current_axes and values:
                st.warning("軸構成を変えると既存の行は作り直しになります")
                if st.button("行をクリアして軸構成を変更", key=f"set_{name}_reset"):
                    values.clear()
                    st.rerun()
            else:
                sets[name] = widgets.selection_list_widget(axes, catalog, values, f"set_{name}_rows")
    else:
        axis = st.selectbox("軸", all_axes, key=f"set_{name}_axis") if all_axes else None
        sets[name] = widgets.selection_list_widget([axis] if axis else ["値"], catalog, values, f"set_{name}_vals")

    st.divider()
    c3, c4, c5 = st.columns([2, 1, 1])
    alias = c3.text_input("別名で保存（コピーを作成。参照は元の名前のまま）", key=f"set_{name}_alias")
    if c4.button("別名で保存", key=f"set_{name}_alias_btn"):
        try:
            state.save_set_as(sf, name, alias.strip())
            st.rerun()
        except ValueError as err:
            st.error(str(err))
    if c5.button("このセットを削除", key=f"set_{name}_del"):
        try:
            state.delete_selection_set(sf, name)
            st.rerun()
        except ValueError as err:
            st.error(str(err))


# ---------------------------------------------------- 画面4: スコア合成・制約

def screen_compose() -> None:
    ss = st.session_state
    sf = ss.score_file
    st.header("スコア合成・制約")
    names = state.part_names(sf)
    if not names:
        st.info("先に画面2でスコアパーツを作成してください")
        return

    st.subheader("expression（Score の式）")
    st.caption("パーツ名を演算子で組み合わせます。使える関数: log(=log10), ln, log2, exp, sqrt, min, max, mean, sum, abs")
    cols = st.columns(min(len(names), 6))
    for i, n in enumerate(names):
        if cols[i % len(cols)].button(n, key=f"ins_{n}", help="式の末尾に挿入"):
            new_expr = (sf["expression"] + " + " + n).strip(" +") if sf["expression"] else n
            sf["expression"] = new_expr
            # the input widget remembers its own state and would keep showing
            # the old expression; update it too (safe: buttons render first)
            st.session_state["expr_input"] = new_expr
            st.rerun()
    sf["expression"] = st.text_input("expression", value=sf["expression"], key="expr_input")

    st.subheader("constraintThreshold（制約）")
    st.caption("小さいほど良い前提のため演算子指定はありません。値を超えたパーツは最適化側で候補から除外されます。")
    ct = sf["constraintThreshold"]
    for key in list(ct):
        entry = ct[key] if isinstance(ct[key], dict) else {"value": ct[key]}
        c1, c2, c3, c4 = st.columns([3, 2, 3, 1])
        c1.markdown(f"**{key}**" + ("" if key in names else "　:red[⚠ パーツがありません]"))
        entry["value"] = c2.number_input("value", value=float(entry.get("value", 0)), key=f"ct_{key}_v")
        dynamic = c3.checkbox(
            "動的制約 (percentile)", value=str(entry.get("active", "")).lower() == "true", key=f"ct_{key}_dyn",
            help="実測値の percentile×coef と指定値の大きい方を閾値に使う",
        )
        if dynamic:
            entry["active"] = "True"
            entry["type"] = "percentile"
            entry["coef"] = c3.number_input("coef", value=float(entry.get("coef") or 20), key=f"ct_{key}_coef")
        else:
            entry.pop("active", None)
            entry.pop("type", None)
            entry.pop("coef", None)
        if c4.button("✕", key=f"ct_{key}_del"):
            ct.pop(key)
            st.rerun()
        ct[key] = entry

    c5, c6 = st.columns([3, 1])
    addable = [n for n in names if n not in ct]
    target = c5.selectbox("制約を追加するパーツ", addable or ["（追加できるパーツなし）"], key="ct_add_sel")
    if c6.button("制約を追加") and addable:
        ct[target] = {"value": 0.0}
        st.rerun()

    problems = state.validate_score_file(sf)
    for p in problems:
        st.error(p)
    if not problems:
        st.success("検証: OK")


# ------------------------------------------ 画面5: テスト実行・エクスポート

def screen_test_export() -> None:
    ss = st.session_state
    ctx = ss.context
    sf = ss.score_file
    st.header("テスト実行・エクスポート")

    st.subheader("テスト計算")
    st.caption("測定データのあるディレクトリでスコアを実際に計算します（エンジン compute_score_file を直接呼びます）")
    default_dir = ctx["data_dir"] if ctx else ""
    test_dir = st.text_input("データディレクトリ", value=default_dir, key="test_dir")
    c1, c2 = st.columns(2)
    generation = c1.text_input(
        "Generation（dVtBudget 使用時に必要）", value=(ctx["generation"] if ctx else "") or "", key="test_gen"
    )
    test_coef = c2.text_input(
        "dVtBudget係数jsonc のパス（dVtBudget 使用時に必要）",
        value=(ctx["coef_path"] if ctx else "") or "", key="test_coef",
    )
    if st.button("計算を実行", type="primary", key="run_btn"):
        if not sf["score_parts"]:
            st.error("スコアパーツがありません")
        else:
            problems = state.validate_score_file(sf)
            if problems:
                st.error("検証エラーがあるため実行できません（画面2/4を確認してください）")
                for p in problems:
                    st.error(p)
            else:
                try:
                    with st.spinner("計算中…"):
                        result = state.run_test_compute(
                            sf, test_dir, generation=generation or None,
                            wlgroup=ctx["wlgroup"] if ctx else None,
                            coef_path=test_coef or None,
                        )
                    st.dataframe(
                        [{"項目": k, "値": v} for k, v in result.items()],
                        use_container_width=True, hide_index=True,
                    )
                except Exception as err:
                    st.error(f"計算エラー: {err}")

    st.divider()
    st.subheader("エクスポート")
    problems = state.validate_score_file(sf) if sf["score_parts"] else ["スコアパーツがありません"]
    if problems:
        st.warning("検証エラーがあるためエクスポートできません: " + " / ".join(problems))
    else:
        st.download_button(
            "score.jsonc をダウンロード（selectionSets 同梱）",
            data=state.score_file_to_jsonc(sf), file_name="score.jsonc", mime="application/json",
        )
        names = state.part_names(sf)
        c1, c2 = st.columns([3, 1])
        pi = c1.selectbox("パーツ単体エクスポート", range(len(names)), format_func=lambda i: names[i], key="exp_part")
        try:
            c2.download_button(
                "ダウンロード", data=state.export_part(sf, pi),
                file_name=f"{names[pi]}.jsonc", mime="application/json", key="exp_part_btn",
            )
        except ValueError as err:
            st.error(str(err))

    st.divider()
    st.subheader("インポート")
    up = st.file_uploader("score.jsonc または optimization設定jsonc", type=["jsonc", "json"], key="import_all")
    if up is not None and st.button("読み込む（現在の編集内容を置き換え）"):
        try:
            ss.score_file = state.import_score_file(up.getvalue().decode("utf-8"))
            state.ensure_uids(ss.score_file)
            ss.selected_part = 0
            st.rerun()
        except Exception as err:
            st.error(str(err))


# --------------------------------------------------------------------- main

def main() -> None:
    st.set_page_config(page_title="スコア設計 (score_gui Phase1)", layout="wide")
    _init()

    with st.sidebar:
        st.title("スコア設計")
        screen = st.radio("画面", SCREENS, key="screen")
        sf = st.session_state.score_file
        st.divider()
        if st.button("↩ 元に戻す", key="undo_btn", disabled=not st.session_state.history,
                     help="直前の編集操作を取り消します（直近20操作まで）"):
            _undo()
        st.caption(f"パーツ: {len(sf['score_parts'])} / 選択セット: {len(sf['selectionSets'])}")
        if sf["score_parts"]:
            n = len(state.validate_score_file(sf))
            (st.success if n == 0 else st.error)("検証 OK" if n == 0 else f"検証エラー {n} 件")

    _offer_draft_restore()
    warning = st.session_state.pop("restore_warning", None)
    if warning:
        st.warning(warning)

    if screen == SCREENS[0]:
        screen_data()
    elif screen == SCREENS[1]:
        screen_parts()
    elif screen == SCREENS[2]:
        screen_sets()
    elif screen == SCREENS[3]:
        screen_compose()
    else:
        screen_test_export()

    _track_history()
    _autosave()


main()
