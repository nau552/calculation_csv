"""スコア設計UI (score_gui Phase1).

起動: .venv/bin/streamlit run ui/app.py（Windows は .venv/Scripts/）

サイドバーで5画面を切り替える。判断ロジックはすべて ui/state.py と
scorelib_param 側にあり、本ファイルはウィジェット配置と session_state の
受け渡しのみを行う（score_gui_ui_design.md 参照）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scorelib_param  # noqa: E402
from scorelib_param.models import COMBINED_SEP  # noqa: E402
from ui import state, widgets  # noqa: E402

SCREENS = [
    "1. データ読み込み",
    "2. スコアパーツ編集",
    "3. 選択セット・グループ定義",
    "4. スコア合成・制約",
    "5. テスト実行・エクスポート",
]
VIRTUAL_FIXED = ("__relative__", "__dvtbudget__")

# 開発者モード: `streamlit run ui/app.py -- --dev` または環境変数
# SCORELIB_UI_DEV=1 で起動したときだけ「サーバ上のパスで指定する」トグルを出す。
# 一般ユーザはブラウザから使う（UIサーバ上で実行することは無い）ため、
# パス指定は UI と同じマシンにファイルがある開発者・管理者専用の入力手段
_DEV_MODE = "--dev" in sys.argv or os.environ.get("SCORELIB_UI_DEV") == "1"

# リバースプロキシ認証が転送するユーザ名ヘッダ（README「UI 実行サーバの立て方」の
# nginx 例では X-Remote-User）。認証導入後は名前入力欄の代わりにこれを使う
_USER_HEADER = os.environ.get("SCORELIB_UI_USER_HEADER", "X-Remote-User")


def _header_user() -> str | None:
    try:
        name = st.context.headers.get(_USER_HEADER)
    except Exception:
        return None
    return name.strip() if name and name.strip() else None


def _sidebar_user() -> str | None:
    """下書きの持ち主となるユーザ名。認証ヘッダがあればそれ（表示のみ）、
    無ければ名前入力欄。未入力の間は None = 下書きの自動保存・復元は停止
    （共用サーバで1ファイルを取り合わないための分離 — state.draft_path_for）。"""
    header_user = _header_user()
    if header_user:
        st.caption(f"ユーザ: {header_user}")
        return header_user
    name = st.text_input(
        "名前（下書きの保存名）", key="draft_user_input",
        help="編集内容の自動保存・復元を名前ごとに分けます。未入力の間は自動保存されません",
    )
    return name.strip() or None


HISTORY_LIMIT = 20
# アプリデータを保持する session_state のキー。これ以外はウィジェット状態と
# みなし、undo 時に全消しして score_file から値を読み直させる
_RESERVED_STATE = {
    "score_file", "context", "selected_part", "draft_prompt_done",
    "history", "last_snapshot", "screen", "draft_user_input",
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
    """undo 履歴は「ユーザ操作1回につき1エントリ」: 落ち着いた（途中で
    st.rerun されなかった）実行の末尾でのみ記録する。スクリプト途中で
    rerun された実行はここに到達しないため、中間状態は積まれない。"""
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
    # ウィジェットは自分の状態を記憶しているので、これをしないと画面は
    # undo 前の値を表示し続けてしまう
    for k in list(ss.keys()):
        if k not in _RESERVED_STATE:
            del ss[k]
    st.rerun()


def _offer_draft_restore(user: str | None) -> None:
    ss = st.session_state
    if ss.draft_prompt_done or ss.score_file["score_parts"]:
        ss.draft_prompt_done = True
        return
    if user is None:
        # 名前が入るまで判定を保留（done にしない: 入力されたら次の実行で提案する）
        return
    draft = state.load_draft(state.draft_path_for(user))
    if draft is None:
        ss.draft_prompt_done = True
        return
    st.info(f"前回の編集内容（{user}）が残っています。復元しますか？")
    ci = draft.get("context_inputs") or {}
    if ci.get("data_dir"):
        st.caption(f"データ読み込みも復元されます: {ci['data_dir']}")
    c1, c2 = st.columns(2)
    if c1.button("復元する", key="restore_btn"):
        ss.score_file = draft["score_file"]
        state.ensure_uids(ss.score_file)
        # 画面1の入力欄にも書き戻す（この実行ではまだインスタンス化されて
        # いないので、キー付き状態への書き込みは安全）。やらないと jsonc の
        # 欄が空のまま表示され、読み込みボタンの再押下で復元した設定/係数の
        # パス指定が静かに外れてしまう
        ss["data_dir_input"] = ci.get("data_dir") or ""
        ss["config_path_input"] = ci.get("config_path") or ""
        ss["coef_path_input"] = ci.get("coef_path") or ""
        ss["custom_path_input"] = ci.get("custom_path") or ""
        if ci.get("data_dir"):
            try:
                ss.context = state.build_context(
                    ci["data_dir"], ci.get("config_path"), ci.get("coef_path"),
                    ci.get("geninfo_path"), ci.get("custom_path"),
                )
                state.import_config_group_defs(
                    ss.score_file, ss.context["wlgroup"],
                    ss.context.get("wlgroup_defin_logical", True),
                    ss.context.get("wlgroup_weight"),
                )
            except Exception as err:
                ss.restore_warning = (
                    f"編集内容は復元しましたが、データの再読み込みに失敗しました。"
                    f"画面1で読み込み直してください: {err}"
                )
        elif ss.score_file["score_parts"]:
            # データ無しで編集していたセッション（設定のみ編集）: 設定から
            # コンテキストを再導出して画面2以降を開けるようにする
            ss.context = state.config_only_context(ss.score_file)
            ss["data_mode"] = _DATA_MODES[2]
        ss.draft_prompt_done = True
        st.rerun()
    if c2.button("破棄して新規に始める", key="discard_btn"):
        ss.draft_prompt_done = True
        st.rerun()
    st.stop()


def _autosave(user: str | None) -> None:
    if user is None:
        return  # 名前未入力（共用サーバで誰の下書きか分からないため保存しない）
    ss = st.session_state
    sf = ss.score_file
    if sf["score_parts"] or sf["selectionSets"] or sf["expression"]:
        ctx = ss.context or {}
        context_inputs = {
            "data_dir": ctx.get("data_dir"),
            "config_path": ctx.get("config_path"),
            "coef_path": ctx.get("coef_path"),
            "geninfo_path": ctx.get("geninfo_path"),
            "custom_path": ctx.get("custom_path"),
        }
        try:
            state.save_draft(sf, context_inputs, state.draft_path_for(user))
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


def _with_group_axes(catalog: dict, sf: dict) -> dict:
    """エディタ用カタログ: 実在の軸 + グループ派生軸（値候補はグループ名）。
    雛形生成は生のカタログを使い続けるので、グループ軸が勝手にパーツへ
    入り込むことはない。"""
    merged = dict(catalog)
    for name, gd in sf.get("groupDefs", {}).items():
        merged.setdefault(name, list(gd.get("groups", {})))
    return merged


# ------------------------------------------------------------ 画面1: 読み込み
#
# 構成方針（docs/score_gui_ui_design.md 画面1）:
# - 入力は「① スコア設定（編集の出発点）+ ② データ（実測 / ダミー / なし）」の
#   2段 + 読み込みボタン1つ
# - **入力手段は併記しない**: 同じものを2通りの枠（アップロードとパス）で同時に
#   見せると、どちらを使えばよいかで必ず迷う。一般ユーザの画面はアップロードのみで、
#   パス指定は開発者モード（_DEV_MODE）のトグルでのみ現れ、オンにすると各
#   アップローダが**パス欄に置き換わる**（並ばない）
# - UI 文言は動作の説明だけを書く（「普段は〜」のようなメタ語りは書かない）

_DATA_MODES = ["実測データ", "ダミー（測定前）", "なし"]


def _import_wlgroup_toast(ss) -> None:
    if state.import_config_group_defs(
        ss.score_file, ss.context["wlgroup"],
        ss.context.get("wlgroup_defin_logical", True),
        ss.context.get("wlgroup_weight"),
    ):
        st.toast("設定jsoncの WLgroup をグループ定義として取り込みました（画面3で編集できます）")


def _handle_load(ss, paths_mode, data_mode, up_start, config_in,
                 up_zip, up_dummy, up_coef, up_custom, paths, n_boards, chips_text) -> None:
    """「読み込み」ボタンの本体。①（設定）と②（データ）を組み合わせて
    context / score_file を作る。①の設定が RunConfig 形式なら config としても
    最優先で使う（zip 内の設定より優先）。"""
    try:
        # ① の設定テキスト（アップロード or パス）
        start_text = None
        start_path = None
        if up_start is not None:
            start_text = up_start.getvalue().decode("utf-8")
            start_path = state.save_upload(up_start.name, up_start.getvalue())
        elif paths_mode and str(config_in or "").strip():
            p = Path(str(config_in).strip())
            if not p.is_file():
                raise ValueError(f"設定 jsonc が見つかりません: {p}")
            start_text = p.read_text(encoding="utf-8")
            start_path = str(p)

        if data_mode == _DATA_MODES[2]:  # データなし = 設定のみ編集
            if start_text is None:
                raise ValueError("① に設定 jsonc を入れてください（データを使う場合は ② で選択）")
            sf, cfg_ctx = state.load_config_only(start_text)
            ss.score_file = sf
            state.ensure_uids(ss.score_file)
            ss.context = cfg_ctx
            ss.selected_part = 0
            st.toast("設定を読み込みました（設定のみ編集）")
            st.rerun()
            return

        dummy_source = None
        if data_mode == _DATA_MODES[0]:  # 実測データ
            if up_zip is not None:
                extracted = state.extract_bundle_zip(up_zip.getvalue())
                found = state.locate_bundle_inputs(extracted)
            elif paths_mode and str(paths.get("data_dir") or "").strip():
                found = {
                    "data_dir": paths["data_dir"],
                    "config_path": None,
                    "coef_path": paths.get("coef") or None,
                    "geninfo_path": None,
                    "custom_path": paths.get("custom") or None,
                }
            elif paths_mode:
                raise ValueError("測定結果ディレクトリのパスを入力してください")
            else:
                raise ValueError("一式 zip をアップロードしてください")
        else:  # ダミー
            counts = state.parse_chip_counts(chips_text, int(n_boards))
            if up_dummy is not None:
                src = state.extract_bundle_zip(up_dummy.getvalue())
                dummy_source = up_dummy.name
            elif paths_mode and str(paths.get("dummy_dir") or "").strip():
                src = paths["dummy_dir"]
                dummy_source = str(paths["dummy_dir"]).strip()
            elif paths_mode:
                raise ValueError("ダミー一式ディレクトリのパスを入力してください")
            else:
                raise ValueError("ダミー一式 zip をアップロードしてください")
            found = {
                "data_dir": state.expand_dummy_bundle(src, counts),
                "config_path": None,
                "coef_path": paths.get("coef") or None,
                "geninfo_path": None,
                "custom_path": paths.get("custom") or None,
            }

        # ① の設定が RunConfig 形式なら config としても使う（zip 内の設定より優先）
        sf_new = None
        if start_text is not None:
            sf_new, _ = state.load_config_only(start_text)
            if state.is_run_config_text(start_text):
                found["config_path"] = start_path
        # 係数・自作関数の追加アップロードは zip 内・パス指定より優先
        if up_coef is not None:
            found["coef_path"] = state.save_upload(up_coef.name, up_coef.getvalue())
        if up_custom is not None:
            found["custom_path"] = state.save_upload(up_custom.name, up_custom.getvalue())

        # 世代情報 json の入力は無い: WL/STR 本数はデータから導出する
        # （zip 内で見つかった場合のみ食い違いの診断警告に使う）
        ss.context = state.build_context(
            found["data_dir"], found.get("config_path"), found.get("coef_path"),
            found.get("geninfo_path"), found.get("custom_path"),
        )
        if dummy_source is not None:
            ss.context["dummy_source"] = dummy_source
        if sf_new is not None:
            # ① を編集の出発点にする（既存の編集内容は置き換え。↩ で戻せる）
            ss.score_file = sf_new
            state.ensure_uids(ss.score_file)
            ss.selected_part = 0
        _import_wlgroup_toast(ss)
        st.toast("読み込みました")
        st.rerun()
    except Exception as err:
        st.error(str(err))


def screen_data() -> None:
    ss = st.session_state
    st.header("データ読み込み")
    paths_mode = False
    if _DEV_MODE:
        paths_mode = st.toggle(
            "サーバ上のパスで指定する", key="paths_mode",
            help="アップロードの代わりに、UI と同じマシンにあるファイル・ディレクトリの"
                 "パスを直接指定します（--dev / SCORELIB_UI_DEV=1 で起動したときのみ表示）",
        )

    st.subheader("① スコア設定")
    up_start = None
    config_in = ""
    if paths_mode:
        config_in = st.text_input(
            "設定 jsonc のパス", key="config_path_input",
            help="既存の設定（score.jsonc / optimization設定）から編集を始める場合に指定。"
                 "未指定なら新規作成。読み込むと現在の編集内容は置き換わります（↩ で戻せます）",
        )
    else:
        up_start = st.file_uploader(
            "設定 jsonc", type=["jsonc", "json"], key="config_start_up",
            help="既存の設定（score.jsonc / optimization設定）から編集を始める場合にアップロード。"
                 "未指定なら新規作成。読み込むと現在の編集内容は置き換わります（↩ で戻せます）",
        )

    st.subheader("② データ")
    data_mode = st.radio(
        "データ", _DATA_MODES, key="data_mode", horizontal=True,
        label_visibility="collapsed",
        captions=[
            "過去実験の測定結果一式",
            "Board/Chip を展開して構造検証",
            "設定だけ編集する",
        ],
    )

    up_zip = up_dummy = up_coef = up_custom = None
    paths = {}
    n_boards = chips_text = None
    if data_mode == _DATA_MODES[0]:
        if paths_mode:
            paths["data_dir"] = st.text_input("測定結果ディレクトリのパス", key="data_dir_input")
            c1, c2 = st.columns(2)
            paths["coef"] = c1.text_input(
                "dVtBudget係数jsonc のパス（任意）", key="coef_path_input",
                help="ディレクトリ内にあれば自動検出されます。係数が無い場合は dVtBudget タイプが選択肢に出ません",
            )
            paths["custom"] = c2.text_input(
                "custom_parts.py のパス（任意）", key="custom_path_input",
                help="Python関数をスコアパーツ（type=custom）として使う場合のみ。ディレクトリ内にあれば自動検出",
            )
        else:
            up_zip = st.file_uploader(
                "一式 zip", type=["zip"], key="bundle_zip",
                help="測定結果に設定・係数・custom_parts.py を同梱できます（サブディレクトリも探索）",
            )
            with st.expander("係数・自作関数を追加する（zip に入っていない場合）"):
                up_coef = st.file_uploader("dVtBudget係数jsonc", type=["jsonc", "json"], key="up_coef")
                up_custom = st.file_uploader("custom_parts.py", type=["py"], key="up_custom")
    elif data_mode == _DATA_MODES[1]:
        st.caption("測定値はダミーのため、テスト計算は構造検証のみです")
        if paths_mode:
            paths["dummy_dir"] = st.text_input("ダミー一式ディレクトリのパス", key="dummy_dir_input")
        else:
            up_dummy = st.file_uploader(
                "ダミー一式 zip", type=["zip"], key="dummy_zip",
                help="測定フローが出力する、Board/Chip が1つのダミー一式",
            )
        cd1, cd2 = st.columns(2)
        n_boards = cd1.number_input("Board 数", min_value=1, value=2, step=1, key="dummy_boards")
        chips_text = cd2.text_input(
            "Board ごとの Chip 数", value="2", key="dummy_chips",
            help="全 Board 共通なら数1つ（例: 4）。Board ごとに違う場合はカンマ区切りで Board の数と同じ個数（例: 4,4,2,2）",
        )
        if paths_mode:
            # 実測モードのパス欄と同じ2欄（手段が変わっても同じことができるように）
            c1, c2 = st.columns(2)
            paths["coef"] = c1.text_input(
                "dVtBudget係数jsonc のパス（任意）", key="coef_path_input",
                help="係数が無い場合は dVtBudget タイプが選択肢に出ません",
            )
            paths["custom"] = c2.text_input(
                "custom_parts.py のパス（任意）", key="custom_path_input",
                help="Python関数をスコアパーツ（type=custom）として使う場合のみ",
            )
        else:
            with st.expander("係数・自作関数を追加する（任意）"):
                up_coef = st.file_uploader("dVtBudget係数jsonc", type=["jsonc", "json"], key="up_coef")
                up_custom = st.file_uploader("custom_parts.py", type=["py"], key="up_custom")

    if st.button("読み込み", type="primary", key="load_btn"):
        _handle_load(ss, paths_mode, data_mode, up_start, config_in,
                     up_zip, up_dummy, up_coef, up_custom, paths, n_boards, chips_text)

    ctx = ss.context
    if not ctx:
        return

    st.subheader("認識結果")
    if ctx.get("config_only"):
        st.info(
            "設定のみ編集中（データ未読み込み）。式・グループ定義・パーツの修正と"
            "エクスポートができます。テスト計算と値の候補表示には、上でデータか"
            "ダミー一式を読み込んでください"
        )
        st.caption(
            f"パーツ: {len(ss.score_file['score_parts'])} / "
            f"type: {', '.join(ctx['part_types']) or 'なし'}"
        )
        return
    st.caption(f"走査したディレクトリ: `{ctx['data_dir']}`")
    if ctx.get("dummy_source"):
        st.info(f"ダミー一式 `{ctx['dummy_source']}` の Board/Chip 展開結果です（数値は無意味・構造検証のみ）")
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
    counts = state.data_axis_counts(ctx["catalogs"])
    if counts:
        st.caption("軸の本数（データから導出）: " + " / ".join(f"{a} {n}" for a, n in counts.items()))
    for w in state.geninfo_mismatch_warnings(ctx):
        st.warning(w)
    if ctx["custom_path"]:
        st.success(
            f"自作関数ファイル（{ctx['custom_source']}）: {ctx['custom_path']}"
            f"（関数: {', '.join(ctx['custom_functions']) or 'なし'}）"
        )
    else:
        st.info("自作関数ファイル: なし — type=custom（Python関数パーツ）は選択肢に出ません")
    if ctx["generation"]:
        st.info(f"Generation: {ctx['generation']} / WLgroup: {list(ctx['wlgroup']) or 'なし'}")
    for w in state.group_def_warnings(ss.score_file, state.validation_axis_counts(ctx)):
        st.warning(w)

    st.subheader(f"検出された type: {', '.join(ctx['part_types'])}")
    for t in ctx["part_types"]:
        if t not in ctx["catalogs"]:
            continue  # "custom" has no axis catalog
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
            state.import_config_group_defs(ss.score_file, ctx["wlgroup"])
            st.rerun()


# ------------------------------------------------------ 画面2: スコアパーツ編集

def _order_entry_label(entry: str, part: dict) -> str:
    if entry == "__relative__":
        return "__relative__（相対化を実行）"
    if entry == "__dvtbudget__":
        return "__dvtbudget__（dVtBudget変換を実行）"
    spec = part.get("aggregations", {}).get(entry, {})
    detail = spec.get("op", "?")
    if spec.get("by"):
        detail += f" by {spec['by']}"
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

    if st.button("＋ 変換ステップを追加", key=f"{uid}_addvirt_btn",
                 help="値を行単位で変換するステップ（__offset__ 等）を order に追加します: "
                      "足す/引く/掛ける/割る・絶対値(abs)・対数(log)。"
                      "典型例: オフセットを足してから相対化、-1を掛けて正負反転、"
                      "「軸の値ごと」を選んで WLgroup 別の重み。実行位置は上下ボタンで調整してください"):
        name, n = "__offset__", 2
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


def _order_editor(part: dict, catalog: dict, sf: dict, uid: str, measure_labels: dict) -> None:
    """order エディタ: 一覧（並べ替え・削除）+ 選択エントリ用の常時表示
    エディタ1つ。expander はラベルが変わるたびに閉じてしまい値の編集が
    苦痛になるため使わない。"""
    sets = sf["selectionSets"]
    sel_key = f"{uid}_sel_entry"
    order = part["order"]
    if st.session_state.get(sel_key) not in order:
        st.session_state[sel_key] = order[0] if order else None

    # 常時ドラッグ可能なリスト1本（案A）: 並べ替えにモード切替は不要。
    # コミュニティ製D&D部品は文字列リストしか描画できないため、エントリの
    # 選択は selectbox、削除は下のエディタ内に分離。⠿ はドラッグ可能の
    # 目印、← 編集中 がリストとエディタの対応を示す。
    sel = st.session_state.get(sel_key)
    labels = [
        "⠿ " + _order_entry_label(e, part) + (" ← 編集中" if e == sel else "")
        for e in order
    ]
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
                gdefs = sf.get("groupDefs", {})
                widgets.agg_editor(
                    entry, spec, catalog,
                    set_names=sorted(sets),
                    key=f"{uid}_{entry}",
                    # 変換ステップの「軸の値ごとの定数（重み）」用: グループ派生軸を
                    # 先頭に、次いで値候補が分かるカテゴリ軸
                    by_candidates=sorted(gdefs)
                    + [a for a in catalog if a != "InBatchEpoch" and a not in gdefs],
                    by_value_labels={
                        **{a: c for a, c in catalog.items() if c},
                        **{n: list(d.get("groups", {})) for n, d in gdefs.items()},
                    },
                    weight_set_names=sorted(sf.get("weightSets", {})),
                    measure_labels=measure_labels,
                )


def _custom_part_editor(part: dict, ctx, uid: str) -> None:
    st.markdown("**自作関数パーツ**")
    st.caption(
        "custom_parts.py の関数を1つ呼び、その戻り値（1スカラー）がこのパーツの値になります。"
        "実行側では SVN リポジトリ直下の custom_parts.py が使われるため、"
        "設計時と同じリビジョンのファイルを読み込んでください。"
    )
    funcs = ctx.get("custom_functions") or []
    cur = part.get("function") or part.get("name")
    if not funcs:
        st.error("custom_parts.py が読み込まれていません（画面1でパス指定するか、一式zipに同梱してください）")
    options = funcs + ([cur] if cur not in funcs else [])
    part["function"] = st.selectbox(
        "関数", options, index=options.index(cur) if cur in options else 0, key=f"{uid}_func"
    )
    if funcs and part["function"] not in funcs:
        st.error(f"関数 '{part['function']}' は読み込んだ custom_parts.py にありません")

    st.markdown("**params（ctx.params として関数に渡す追加パラメータ）**")
    # 行エディタの構造は _group_defs_section のグループ行と似ているが、
    # あえて別々の具体的なループのままにしている — 列構成が違い、共通化の
    # 抽象コードの方がどちらのループより長くなるため
    params = part.setdefault("params", {})

    def _reset_param_widgets() -> None:
        for k in list(st.session_state):
            if str(k).startswith(f"{uid}_prm"):
                del st.session_state[k]

    new_items = []
    for i, (pk, pv) in enumerate(list(params.items())):
        c_k, c_v, c_rm = st.columns([3, 4, 1])
        nk = c_k.text_input("名前", value=pk, key=f"{uid}_prm{i}_k")
        nv = c_v.text_input("値", value=str(pv), key=f"{uid}_prm{i}_v",
                            help="true/false・数値は型付きで渡されます（それ以外は文字列）")
        if c_rm.button("✕", key=f"{uid}_prm{i}_rm", help="このパラメータを削除"):
            params.pop(pk, None)
            _reset_param_widgets()
            st.rerun()
        new_items.append(((nk or "").strip() or pk, widgets.parse_scalar(nv)))
    keys = [k for k, _ in new_items]
    if len(set(keys)) != len(keys):
        st.error("パラメータ名が重複しています")
    else:
        part["params"] = dict(new_items)
        params = part["params"]
    if st.button("＋ パラメータを追加", key=f"{uid}_prm_add"):
        n = len(params) + 1
        pk = f"param{n}"
        while pk in params:
            n += 1
            pk = f"param{n}"
        params[pk] = 0
        st.rerun()


def screen_parts() -> None:
    ss = st.session_state
    ctx = ss.context
    st.header("スコアパーツ編集")
    if not ctx:
        st.info("先に画面1でデータを読み込んでください")
        return
    sf = ss.score_file
    state.ensure_uids(sf)

    # パーツ選択は _uid を**キー付き**ウィジェット状態（"part_sel"）で持つ。
    # これによりプルダウン操作が引き起こす再実行の**開始時点**で新しい選択が
    # 分かる — キー無しだと上に描画済みの一覧が1操作遅れてしまう。
    # プログラムからの選択移動（追加・複製）は part_sel_pending 経由:
    # 同一実行内でウィジェットのインスタンス化後にキー付き状態を書くと
    # 例外になるため、次の実行の冒頭で反映する。並べ替えは対応不要
    # （uid は変わらないので選択が自動で追従する）。
    uids = [p["_uid"] for p in sf["score_parts"]]
    pending = ss.pop("part_sel_pending", None)
    if pending in uids:
        ss["part_sel"] = pending
    if ss.get("part_sel") not in uids:
        ss["part_sel"] = uids[min(ss.selected_part, len(uids) - 1)] if uids else None
    sel_uid = ss.get("part_sel")
    ss.selected_part = uids.index(sel_uid) if sel_uid in uids else 0

    rows = state.part_summary_rows(sf)
    # 検証マーカー: D&D部品は文字列しか描画できず項目単位の色分けが
    # 不可能なため、⚠ の接頭記号を見た目の代替にする
    invalid = {
        p["_uid"] for p in sf["score_parts"]
        if state.validate_part(p, sf["selectionSets"], sf.get("weightSets"))
    }
    # データに測定ファイルの無い type のパーツも ⚠ 対象（設定としては有効な
    # ままなので検証NGとは別扱い — 編集画面に警告文が出る）
    no_data = state.part_types_without_data(sf, ctx)
    for r, p in zip(rows, sf["score_parts"]):
        uid_ = p["_uid"]
        r["検証"] = "⚠ NG" if uid_ in invalid else ("⚠ データ無し" if uid_ in no_data else "OK")
    invalid = invalid | no_data
    parts_dnd = False
    if widgets.HAS_SORTABLES and len(sf["score_parts"]) > 1:
        labels = state.part_list_labels(sf, sel_uid, invalid, rows=rows)
        new_labels = widgets.sortable_list(labels, key="parts_dnd")
        if new_labels is not None:
            parts_dnd = True
            st.caption("パーツ一覧はドラッグで並べ替えられます")
            if new_labels != labels:
                by_label = dict(zip(labels, sf["score_parts"]))
                sf["score_parts"] = [by_label[lbl] for lbl in new_labels]
                st.rerun()
    if rows and not parts_dnd:
        st.dataframe(rows, use_container_width=True, hide_index=True)

    c1, c2, c3 = st.columns([2, 1, 1])
    new_type = c1.selectbox("新規パーツの type", ctx["part_types"], key="new_part_type")
    # 設定のみ編集でパーツが無い設定を読んだ場合など、type 候補が空なら追加不可
    if c2.button("追加（雛形を生成）", type="primary", key="add_part_btn") and new_type:
        name = state.unique_part_name(sf)
        if new_type == "custom":
            part = state.custom_part_skeleton(name, ctx["custom_functions"])
        else:
            part = state.part_skeleton(name, new_type, ctx["catalogs"][new_type])
        sf["score_parts"].append(part)
        state.ensure_uids(sf)
        ss["part_sel_pending"] = sf["score_parts"][-1]["_uid"]
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
    select_labels = state.part_select_labels(sf, invalid)
    st.selectbox(
        "編集するパーツ", uids, key="part_sel",
        format_func=lambda u: select_labels.get(u, "?"),
    )
    idx = ss.selected_part  # 画面冒頭で part_sel から導出済み
    part = sf["score_parts"][idx]
    uid = part["_uid"]

    cup, cdn, cdup, cdel = st.columns(4)
    if cup.button("▲ 上へ", disabled=idx == 0, help="このパーツを一覧の1つ上へ"):
        state.move_entry(sf["score_parts"], idx, -1)
        st.rerun()
    if cdn.button("▼ 下へ", disabled=idx == len(names) - 1, help="このパーツを一覧の1つ下へ"):
        state.move_entry(sf["score_parts"], idx, +1)
        st.rerun()
    if cdup.button("このパーツを複製"):
        new_idx = state.duplicate_part(sf, idx)
        state.ensure_uids(sf)
        ss["part_sel_pending"] = sf["score_parts"][new_idx]["_uid"]
        st.rerun()
    if cdel.button("このパーツを削除"):
        sf["score_parts"].pop(idx)
        ss.selected_part = max(0, min(idx, len(sf["score_parts"]) - 1))
        st.rerun()

    part["name"] = st.text_input("name", value=part.get("name", ""), key=f"{uid}_name")
    cur_type = part.get("type")
    # 前提ファイル未読み込みの type（custom ファイルや係数 jsonc が無い等）
    # でも現在の type は選択肢に残す — 残さないと selectbox がパーツの type
    # を黙って別の値に書き換えてしまう
    types = ctx["part_types"] + ([cur_type] if cur_type not in ctx["part_types"] else [])
    tsel, tregen = st.columns([3, 1])
    new_type = tsel.selectbox(
        "type", types, index=types.index(cur_type) if cur_type in types else 0, key=f"{uid}_type"
    )
    if new_type != cur_type:
        notice = state.switch_part_type(part, new_type)
        if notice:
            st.toast(notice)
        if new_type != "custom":
            st.warning("type を変更しました。軸構成が異なる type の場合は雛形の再生成を推奨します")
    if tregen.button("雛形を再生成", key=f"{uid}_regen", help="現在の type に合わせてパーツを作り直します（編集内容は失われます）"):
        if part["type"] == "custom":
            fresh = state.custom_part_skeleton(part["name"], ctx["custom_functions"])
        else:
            fresh = state.part_skeleton(part["name"], part["type"], ctx["catalogs"][part["type"]])
        fresh["_uid"] = uid
        sf["score_parts"][idx] = fresh
        st.rerun()

    st.divider()
    if part.get("type") != "custom" and part.get("type") not in ctx.get("catalogs", {}):
        st.warning(
            f"type '{part.get('type')}' の測定データがありません — このパーツは"
            "テスト計算できません（編集・保存は可能。不要なら削除してください）"
        )
    if part.get("type") == "custom":
        _custom_part_editor(part, ctx, uid)
    else:
        catalog = _with_group_axes(_catalog_for_part(ctx, part), sf)
        mlabels = (ctx.get("measure_labels") or {}).get(part.get("type"), {})
        widgets.relative_editor(
            part, catalog,
            set_names=sorted(sf["selectionSets"]),
            key=f"{uid}_rel",
            measure_labels=mlabels,
        )
        st.divider()
        _order_editor(part, catalog, sf, uid, mlabels)
        _add_entry_controls(part, catalog, uid)

    problems = state.validate_part(part, sf["selectionSets"], sf.get("weightSets"))
    for p in problems:
        st.error(p)
    if not problems:
        st.success("このパーツの検証: OK")


# ---------------------------------------- 画面3: 選択セット・グループ定義

def screen_sets() -> None:
    ss = st.session_state
    ctx = ss.context
    sf = ss.score_file
    st.header("選択セット・グループ定義")
    _selection_sets_section(sf, ctx)
    st.divider()
    _group_defs_section(sf, ctx)


def _selection_sets_section(sf: dict, ctx) -> None:
    st.subheader("選択セット")
    st.caption("複数パーツで使い回す値の組（例: State と Read_Label の対応リスト）に名前を付けて管理します。パーツ側からは ref で参照します。")
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


def _group_defs_section(sf: dict, ctx) -> None:
    st.subheader("グループ定義（軸のグループ分割）")
    st.caption(
        "数値軸を範囲でまとめた派生軸を定義します。パーツの order に定義名を軸として置くと、"
        "好きな位置で集計できます（例: WLを平均 → Board を max → 最後に WLgroup を max）。"
        "定義はスコアと一緒に保存・エクスポートされます。"
    )
    defs = sf.setdefault("groupDefs", {})
    catalog = _merged_catalog(ctx) if ctx else {}
    numeric_axes = [
        a for a, c in catalog.items()
        if a != "InBatchEpoch" and not (c and isinstance(c[0], (str, bool)))
    ]

    for w in state.group_def_warnings(sf, state.validation_axis_counts(ctx)):
        st.warning(w)

    if defs:
        st.dataframe(
            [
                {
                    "名前": n,
                    "対象軸": d.get("axis", "?"),
                    "グループ数": len(d.get("groups", {})),
                    "参照しているパーツ": ", ".join(state.parts_referencing_group_def(sf, n)) or "（なし）",
                }
                for n, d in defs.items()
            ],
            use_container_width=True, hide_index=True,
        )

    c1, c2, c3 = st.columns([2, 2, 1])
    gname = c1.text_input("新規定義名", key="new_gdef_name")
    gaxis = c2.selectbox("対象軸", numeric_axes or ["WL"], key="new_gdef_axis")
    if c3.button("作成", key="new_gdef_btn"):
        try:
            state.add_group_def(sf, gname, gaxis, set(catalog))
            st.rerun()
        except ValueError as err:
            st.error(str(err))

    if not defs:
        return
    name = st.selectbox("編集する定義", sorted(defs), key="edit_gdef_name")
    gd = defs[name]
    axis_opts = numeric_axes or [gd.get("axis", "WL")]
    cur_axis = gd.get("axis")
    gd["axis"] = st.selectbox(
        "対象軸", axis_opts,
        index=axis_opts.index(cur_axis) if cur_axis in axis_opts else 0,
        key=f"gdef_{name}_axis",
    )
    physical = st.checkbox(
        "範囲を Physical 番号で記入する（現行スクリプトの WLgroupDefinLogical=False 相当）",
        value=not gd.get("definedInLogical", True),
        key=f"gdef_{name}_phys",
        help="データの csv は Logical 番号なので、計算時に軸の総数 N を使って N-1-p で"
             "読み替えます（N はデータから自動導出）",
    )
    gd["definedInLogical"] = not physical

    def _reset_row_widgets() -> None:
        # 行ウィジェットのキーは添字ベースなので、行の増減後は記憶された
        # 状態が別の行の値を表示してしまう → 構造変更時にキーごと破棄する
        for k in list(st.session_state):
            if str(k).startswith(f"gdef_{name}_"):
                del st.session_state[k]

    groups = gd.setdefault("groups", {})
    new_items = []
    for i, (label, rng) in enumerate(list(groups.items())):
        c_l, c_lo, c_hi, c_rm = st.columns([3, 2, 2, 1])
        lbl = c_l.text_input("グループ名", value=label, key=f"gdef_{name}_{i}_lbl")
        lo = c_lo.number_input("開始", value=int(rng[0]), step=1, key=f"gdef_{name}_{i}_lo")
        hi = c_hi.number_input("終了", value=int(rng[1]), step=1, key=f"gdef_{name}_{i}_hi")
        if c_rm.button("✕", key=f"gdef_{name}_{i}_rm", help="このグループを削除"):
            groups.pop(label, None)
            _reset_row_widgets()
            st.rerun()
        new_items.append(((lbl or "").strip() or label, [int(lo), int(hi)]))
    labels = [l for l, _ in new_items]
    if len(set(labels)) != len(labels):
        st.error("グループ名が重複しています")
    else:
        gd["groups"] = dict(new_items)
        groups = gd["groups"]

    c4, c5 = st.columns(2)
    if c4.button("＋ グループを追加", key=f"gdef_{name}_add"):
        n = len(groups) + 1
        label = f"g{n}"
        while label in groups:
            n += 1
            label = f"g{n}"
        groups[label] = [0, 0]
        st.rerun()
    if c5.button("この定義を削除", key=f"gdef_{name}_del"):
        try:
            state.delete_group_def(sf, name)
            _reset_row_widgets()
            st.rerun()
        except ValueError as err:
            st.error(str(err))

    st.divider()
    weights_all = sf.setdefault("weightSets", {})
    wname = f"{name}Weight"
    enabled = st.checkbox(
        f"重みセット {wname} を定義する", value=wname in weights_all,
        key=f"gdef_{name}_w_on",
        help="パーツの order に定数演算ステップ（掛け算など）を置き、「軸の値ごと」でこのセットを"
             " ref 参照すると、好きなタイミングでグループ別の重みを掛けられます。"
             "設定jsoncの WLgroupWeight はこの名前で取り込まれます",
    )
    if not enabled:
        weights_all.pop(wname, None)
    else:
        cur = weights_all.get(wname)
        scalar_mode = st.checkbox(
            "全グループ共通の1値にする", value=not isinstance(cur, dict),
            key=f"gdef_{name}_w_scalar",
        )
        if scalar_mode:
            weights_all[wname] = st.number_input(
                "重み", value=float(cur) if isinstance(cur, (int, float)) else 1.0,
                key=f"gdef_{name}_w_sv",
            )
        else:
            cur = cur if isinstance(cur, dict) else {}
            weights_all[wname] = {
                g: st.number_input(
                    g,
                    value=float(cur[g]) if isinstance(cur.get(g), (int, float)) else 1.0,
                    key=f"gdef_{name}_w_{i}",
                )
                for i, g in enumerate(groups)
            }


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
            # 入力ウィジェットは自分の状態を記憶していて古い式を表示し続ける
            # ため、こちらも更新する（安全: ボタンは入力欄より先に描画される）
            st.session_state["expr_input"] = new_expr
            st.rerun()
    sf["expression"] = st.text_input("expression", value=sf["expression"], key="expr_input")

    st.subheader("constraintThreshold（制約）")
    st.caption("指定したパーツの値がこの値を超えた提案パラメータは、解の候補になりません。")
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
    if ctx and ctx.get("dummy_source"):
        st.warning("ダミー展開データを読み込んでいます。テスト計算の数値に意味はありません（構造・設定の検証のみ）")
    if ctx and ctx.get("config_only"):
        st.caption("設定のみ編集中: テスト計算にはデータディレクトリの指定（または画面1でのデータ/ダミー読み込み）が必要です")
    default_dir = (ctx.get("data_dir") or "") if ctx else ""
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
        if not test_dir.strip():
            st.error("データディレクトリを入力してください")
        elif not sf["score_parts"]:
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
                            custom_path=ctx["custom_path"] if ctx else None,
                            geninfo_path=ctx["geninfo_path"] if ctx else None,
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
        if any(p.get("type") == "custom" for p in sf["score_parts"]):
            st.caption(
                "⚠ type=custom のパーツを含みます。関数本体は score.jsonc には入らないため、"
                "実行側の SVN リポジトリ直下に同じ custom_parts.py が必要です。"
            )
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
        user = _sidebar_user()
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
        st.caption(
            f"engine scorelib_param {scorelib_param.__version__}",
            help="このUIに同梱されたエンジンの版。実験実行側（SVNの scorelib）と一致しているかの確認用",
        )

    _offer_draft_restore(user)
    warning = st.session_state.pop("restore_warning", None)
    if warning:
        st.warning(warning)

    # 全画面共通の変更検知: この実行中にウィジェットが score file を変更して
    # いたら即座に再実行し、要約行・警告・ラベルが2度目のクリックなしで
    # 追従するようにする。画面ごとではなくここでやるのは、新しい画面を
    # 作ったときに入れ忘れられないようにするため — 画面3のグループ定義警告が
    # まさに写し忘れで1操作遅れた。
    before = _snapshot(st.session_state.score_file)
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
    if _snapshot(st.session_state.score_file) != before:
        st.rerun()

    _track_history()
    _autosave(user)


main()
