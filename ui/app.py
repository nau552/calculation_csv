# Copyright (c) 2026
"""スコア設計UI (score_gui Phase1).

起動: .venv/bin/streamlit run ui/app.py(Windows は .venv/Scripts/)

サイドバーで5画面を切り替える。判断ロジックはすべて ui/state.py と
scorelib_param 側にあり、本ファイルはウィジェット配置と session_state の
受け渡しのみを行う(score_gui_ui_design.md 参照)。
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import contextlib

import scorelib_param
from scorelib_param.models import COMBINED_SEP
from ui import state, widgets

if TYPE_CHECKING:
    from streamlit.runtime.state import SessionStateProxy
    from streamlit.runtime.uploaded_file_manager import UploadedFile

SCREENS = [
    "1. データ読み込み",
    "2. スコアパーツ編集",
    "3. 選択セット・グループ定義",
    "4. スコア合成・制約",
    "5. テスト実行・エクスポート",
]
VIRTUAL_FIXED = ("__relative__", "__dvtbudget__")

# 軸の値候補プレビューの表示件数上限(超過分は「…」で省略)
_CANDS_PREVIEW_MAX = 10

# 複合軸は2軸以上を束ねたときだけ意味を持つ(1軸では単独エントリと同じ)
_MIN_COMBO_AXES = 2

# 開発者モード: `streamlit run ui/app.py -- --dev` または環境変数
# SCORELIB_UI_DEV=1 で起動したときだけ「サーバ上のパスで指定する」トグルを出す。
# 一般ユーザはブラウザから使う(UIサーバ上で実行することは無い)ため、
# パス指定は UI と同じマシンにファイルがある開発者・管理者専用の入力手段
_DEV_MODE = "--dev" in sys.argv or os.environ.get("SCORELIB_UI_DEV") == "1"

# リバースプロキシ認証が転送するユーザ名ヘッダ(README「UI 実行サーバの立て方」の
# nginx 例では X-Remote-User)。認証導入後は名前入力欄の代わりにこれを使う
_USER_HEADER = os.environ.get("SCORELIB_UI_USER_HEADER", "X-Remote-User")


def _header_user() -> str | None:
    try:
        name = st.context.headers.get(_USER_HEADER)
    except Exception:
        return None
    return name.strip() if name and name.strip() else None


def _sidebar_user() -> str | None:
    """下書きの持ち主となるユーザ名。

    認証ヘッダがあればそれ(表示のみ)、
    無ければ名前入力欄。未入力の間は None = 下書きの自動保存・復元は停止
    (共用サーバで1ファイルを取り合わないための分離 — state.draft_path_for)。

    Returns:
        認証ヘッダまたは名前入力欄から得たユーザ名(前後空白は除去)。
        どちらからも得られない間は None。

    """
    header_user = _header_user()
    if header_user:
        st.caption(f"ユーザ: {header_user}")
        return header_user
    name = st.text_input(
        "名前(下書きの保存名)",
        key="draft_user_input",
        help="編集内容の自動保存・復元を名前ごとに分けます。未入力の間は自動保存されません",
    )
    return name.strip() or None


HISTORY_LIMIT = 20


def _init() -> None:
    """アプリデータの session_state キーを初期化する(未設定のときだけ既定値を入れる)。"""
    ss = st.session_state
    ss.setdefault("score_file", state.empty_score_file())
    ss.setdefault("context", None)
    ss.setdefault("selected_part", 0)
    ss.setdefault("draft_prompt_done", False)
    ss.setdefault("history", [])
    ss.setdefault("last_snapshot", None)
    ss.setdefault("widget_epoch", 0)


def _snapshot(obj: dict[str, object]) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _wk(name: str) -> str:
    """設定(score_file)から表示を作るウィジェットのキー(undo 世代つき)。

    「元に戻す」のたびに世代番号が上がってキーが変わり、部品ごと作り直される —
    キーが同じままだとブラウザ側が古い表示を持ち続け、復元した設定と画面が
    食い違うため(streamlit の仕様。設計書 2026-08-01 その4)。表示は常に
    score_file から再生成されるので、見た目と計算・エクスポートは一致する。
    世代0では従来表記のまま(キーを直指定する既存テストを壊さない)。

    Returns:
        世代0なら name そのまま、以降は "name@世代番号"。

    """
    epoch = st.session_state.get("widget_epoch", 0)
    return name if epoch == 0 else f"{name}@{epoch}"


def _track_history() -> None:
    """Undo 履歴を記録する(ユーザ操作1回につき1エントリ)。

    落ち着いた(途中で
    st.rerun されなかった)実行の末尾でのみ記録する。スクリプト途中で
    rerun された実行はここに到達しないため、中間状態は積まれない。
    エントリには「どの画面で・どのパーツを編集していたか」も記録する —
    元に戻したとき、その場所へ跳んで取り消しが目の前で見えるように。
    """
    ss = st.session_state
    snap = _snapshot(ss.score_file)
    if ss.last_snapshot is None:
        ss.last_snapshot = snap
    elif snap != ss.last_snapshot:
        ss.history.append({"snap": ss.last_snapshot, "screen": ss.get("screen"), "part_sel": ss.get("part_sel")})
        del ss.history[:-HISTORY_LIMIT]
        ss.last_snapshot = snap


def _undo() -> None:
    ss = st.session_state
    if not ss.history:
        return
    entry = ss.history.pop()
    ss.score_file = json.loads(entry["snap"])
    ss.last_snapshot = entry["snap"]
    # 設定由来のウィジェットは世代キー(_wk)ごと作り直し、復元した score_file
    # から再表示する。旧方式の「状態の一括削除」は廃止: キーが同じ部品は
    # ブラウザ側の表示が残って食い違う上、画面1のパス入力など設定と無関係な
    # 状態まで巻き添えで消していた(古い世代のキーは streamlit が自動回収する)
    ss["widget_epoch"] = ss.get("widget_epoch", 0) + 1
    # 編集していた場所へ跳ぶ(screen の radio は描画済みのため pending で
    # 次の実行の冒頭に反映する — part_sel_pending と同じ理由)
    ss["screen_pending"] = entry.get("screen")
    if entry.get("part_sel"):
        ss["part_sel_pending"] = entry["part_sel"]
    st.rerun()


def _offer_draft_restore(user: str | None) -> None:
    ss = st.session_state
    if ss.draft_prompt_done or ss.score_file["score_parts"]:
        ss.draft_prompt_done = True
        return
    if user is None:
        # 名前が入るまで判定を保留(done にしない: 入力されたら次の実行で提案する)
        return
    draft = state.load_draft(state.draft_path_for(user))
    if draft is None:
        ss.draft_prompt_done = True
        return
    st.info(f"前回の編集内容({user})が残っています。復元しますか?")
    ci = draft.get("context_inputs") or {}
    if ci.get("data_dir"):
        st.caption(f"データ読み込みも復元されます: {ci['data_dir']}")
    c1, c2 = st.columns(2)
    if c1.button("復元する", key="restore_btn"):
        ss.score_file = draft["score_file"]
        state.ensure_uids(ss.score_file)
        # 画面1の入力欄にも書き戻す(この実行ではまだインスタンス化されて
        # いないので、キー付き状態への書き込みは安全)。やらないと jsonc の
        # 欄が空のまま表示され、読み込みボタンの再押下で復元した設定/係数の
        # パス指定が静かに外れてしまう
        ss["data_dir_input"] = ci.get("data_dir") or ""
        ss["config_path_input"] = ci.get("config_path") or ""
        ss["coef_path_input"] = ci.get("coef_path") or ""
        ss["custom_path_input"] = ci.get("custom_path") or ""
        if ci.get("data_dir"):
            try:
                ss.context = state.build_context(
                    ci["data_dir"],
                    ci.get("config_path"),
                    ci.get("coef_path"),
                    ci.get("geninfo_path"),
                    ci.get("custom_path"),
                )
                state.import_config_group_defs(
                    ss.score_file,
                    ss.context["wlgroup"],
                    defin_logical=ss.context.get("wlgroup_defin_logical", True),
                    wlgroup_weight=ss.context.get("wlgroup_weight"),
                )
            except Exception as err:
                ss.restore_warning = (
                    f"編集内容は復元しましたが、データの再読み込みに失敗しました。画面1で読み込み直してください: {err}"
                )
        elif ss.score_file["score_parts"]:
            # データ無しで編集していたセッション(設定のみ編集): 設定から
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
        return  # 名前未入力(共用サーバで誰の下書きか分からないため保存しない)
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
        with contextlib.suppress(OSError):
            state.save_draft(sf, context_inputs, state.draft_path_for(user))


def _merged_catalog(ctx: dict[str, Any]) -> dict:
    merged: dict = {}
    for cat in ctx["catalogs"].values():
        for axis, cands in cat.items():
            merged.setdefault(axis, cands)
    return merged


def _catalog_for_part(ctx: dict[str, Any], part: dict[str, object]) -> dict:
    # 実際に読む type で引く(dVtBudget パーツは FBC — state.source_data_type)。
    # データに無い type だけマージカタログへフォールバック
    return ctx["catalogs"].get(state.source_data_type(part.get("type")), _merged_catalog(ctx))


def _with_group_axes(catalog: dict, sf: dict) -> dict:
    """エディタ用カタログ: 実在の軸 + グループ派生軸(値候補はグループ名)。

    雛形生成は生のカタログを使い続けるので、グループ軸が勝手にパーツへ
    入り込むことはない。

    Returns:
        `catalog` の複製に、score_file の groupDefs 由来の派生軸
        (値候補 = グループ名のリスト)を加えた辞書。元の `catalog` は変えない。

    """
    merged = dict(catalog)
    for name, gd in sf.get("groupDefs", {}).items():
        merged.setdefault(name, list(gd.get("groups", {})))
    return merged


# ------------------------------------------------------------ 画面1: 読み込み
#
# 構成方針(docs/score_gui_ui_design.md 画面1):
# - 入力は「① スコア設定(編集の出発点)+ ② データ(実測 / ダミー / なし)」の
#   2段 + 読み込みボタン1つ
# - **入力手段は併記しない**: 同じものを2通りの枠(アップロードとパス)で同時に
#   見せると、どちらを使えばよいかで必ず迷う。一般ユーザの画面はアップロードのみで、
#   パス指定は開発者モード(_DEV_MODE)のトグルでのみ現れ、オンにすると各
#   アップローダが**パス欄に置き換わる**(並ばない)
# - UI 文言は動作の説明だけを書く(「普段は〜」のようなメタ語りは書かない)

_DATA_MODES = ["実測データ", "ダミー(測定前)", "なし"]


def _import_wlgroup_toast(ss: SessionStateProxy) -> None:
    if state.import_config_group_defs(
        ss.score_file,
        ss.context["wlgroup"],
        defin_logical=ss.context.get("wlgroup_defin_logical", True),
        wlgroup_weight=ss.context.get("wlgroup_weight"),
    ):
        st.toast("設定jsoncの WLgroup をグループ定義として取り込みました(画面3で編集できます)")


@dataclasses.dataclass(frozen=True)
class LoadInputs:
    """画面1のフォーム入力一式(「読み込み」ボタンが受け取る状態)。

    12個の入力欄の値を1つに束ねる(_handle_load と各 _resolve_* / _finish_*
    ヘルパーの引数)。パス系の欄(config_in / paths)は開発者モード
    (paths_mode=True)のときだけ値が入る。
    """

    paths_mode: bool
    data_mode: str
    up_start: UploadedFile | None = None
    config_in: str = ""
    up_zip: UploadedFile | None = None
    up_dummy: UploadedFile | None = None
    up_coef: UploadedFile | None = None
    up_custom: UploadedFile | None = None
    paths: dict[str, str] = dataclasses.field(default_factory=dict)
    n_boards: int | None = None
    chips_text: str | None = None


def _resolve_start_config(inputs: LoadInputs) -> tuple[str | None, str | None]:
    """① の設定テキストを得る(アップロード or パス)。

    Returns:
        (設定テキスト, 設定ファイルのパス) のタプル。①が未指定なら
        (None, None)。

    Raises:
        ValueError: パス指定された設定 jsonc の実体が無いとき。

    """
    if inputs.up_start is not None:
        return (
            inputs.up_start.getvalue().decode("utf-8"),
            state.save_upload(inputs.up_start.name, inputs.up_start.getvalue()),
        )
    if inputs.paths_mode and str(inputs.config_in or "").strip():
        p = Path(str(inputs.config_in).strip())
        if not p.is_file():
            msg = f"設定 jsonc が見つかりません: {p}"
            raise ValueError(msg)
        return p.read_text(encoding="utf-8"), str(p)
    return None, None


def _finish_config_only_load(ss: SessionStateProxy, start_text: str | None) -> None:
    """データなし(設定のみ編集)モードの読み込みを完了して再実行する。

    Raises:
        ValueError: ① に設定 jsonc が入っていないとき。

    """
    if start_text is None:
        msg = "① に設定 jsonc を入れてください(データを使う場合は ② で選択)"
        raise ValueError(msg)
    sf, cfg_ctx = state.load_config_only(start_text)
    ss.score_file = sf
    state.ensure_uids(ss.score_file)
    ss.context = cfg_ctx
    ss.selected_part = 0
    st.toast("設定を読み込みました(設定のみ編集)")
    st.rerun()


def _resolve_measured_data(inputs: LoadInputs) -> dict[str, str | None]:
    """実測データモードの入力(一式 zip or パス指定)からデータディレクトリを解決する。

    Returns:
        data_dir / config_path / coef_path / geninfo_path / custom_path を
        キーに持つ dict(build_context への入力)。

    Raises:
        ValueError: 一式 zip もパスも入っていない、または zip の中身が
            一式として認識できないとき。

    """
    if inputs.up_zip is not None:
        extracted = state.extract_bundle_zip(inputs.up_zip.getvalue())
        return state.locate_bundle_inputs(extracted)
    if inputs.paths_mode and str(inputs.paths.get("data_dir") or "").strip():
        return {
            "data_dir": inputs.paths["data_dir"],
            "config_path": None,
            "coef_path": inputs.paths.get("coef") or None,
            "geninfo_path": None,
            "custom_path": inputs.paths.get("custom") or None,
        }
    if inputs.paths_mode:
        msg = "測定結果ディレクトリのパスを入力してください"
        raise ValueError(msg)
    msg = "一式 zip をアップロードしてください"
    raise ValueError(msg)


def _resolve_dummy_data(inputs: LoadInputs) -> tuple[dict[str, str | None], str]:
    """ダミー(測定前)モードの入力を Board/Chip 複製展開して解決する。

    Returns:
        (build_context への入力 dict, ダミー一式の由来ラベル) のタプル。

    Raises:
        ValueError: Board/Chip 数の入力が不正、またはダミー一式の zip も
            パスも入っていないとき。

    """
    if inputs.n_boards is None or inputs.chips_text is None:
        # 到達しない防御: ダミーを選んだ rerun では画面側が両方の入力欄を出している
        msg = "Board 数と Board ごとの Chip 数を入力してください"
        raise ValueError(msg)
    counts = state.parse_chip_counts(inputs.chips_text, int(inputs.n_boards))
    if inputs.up_dummy is not None:
        src = state.extract_bundle_zip(inputs.up_dummy.getvalue())
        dummy_source = inputs.up_dummy.name
    elif inputs.paths_mode and str(inputs.paths.get("dummy_dir") or "").strip():
        src = inputs.paths["dummy_dir"]
        dummy_source = str(inputs.paths["dummy_dir"]).strip()
    elif inputs.paths_mode:
        msg = "ダミー一式ディレクトリのパスを入力してください"
        raise ValueError(msg)
    else:
        msg = "ダミー一式 zip をアップロードしてください"
        raise ValueError(msg)
    found: dict[str, str | None] = {
        "data_dir": state.expand_dummy_bundle(src, counts),
        "config_path": None,
        "coef_path": inputs.paths.get("coef") or None,
        "geninfo_path": None,
        "custom_path": inputs.paths.get("custom") or None,
    }
    return found, dummy_source


def _finish_data_load(
    ss: SessionStateProxy, inputs: LoadInputs, start_text: str | None, start_path: str | None
) -> None:
    """実測 / ダミーのデータ読み込みを完了して再実行する(context / score_file の構築)。

    Raises:
        ValueError: 選んだモードに必要な入力が欠けているとき(データの解決は
            _resolve_measured_data / _resolve_dummy_data が担い、同種の
            ValueError を送出する)。

    """
    dummy_source = None
    if inputs.data_mode == _DATA_MODES[0]:  # 実測データ
        found = _resolve_measured_data(inputs)
    else:  # ダミー
        found, dummy_source = _resolve_dummy_data(inputs)

    # ① の設定が RunConfig 形式なら config としても使う(zip 内の設定より優先)
    sf_new = None
    if start_text is not None:
        sf_new, _ = state.load_config_only(start_text)
        if state.is_run_config_text(start_text):
            found["config_path"] = start_path
    # 係数・自作関数の追加アップロードは zip 内・パス指定より優先
    if inputs.up_coef is not None:
        found["coef_path"] = state.save_upload(inputs.up_coef.name, inputs.up_coef.getvalue())
    if inputs.up_custom is not None:
        found["custom_path"] = state.save_upload(inputs.up_custom.name, inputs.up_custom.getvalue())

    data_dir = found["data_dir"]
    if data_dir is None:
        # 到達しない防御: ここに来る3経路すべてで data_dir は非 None
        msg = "測定結果ディレクトリのパスを入力してください"
        raise ValueError(msg)
    # 世代情報 json の入力は無い: WL/STR 本数はデータから導出する
    # (zip 内で見つかった場合のみ食い違いの診断警告に使う)
    ss.context = state.build_context(
        data_dir,
        found.get("config_path"),
        found.get("coef_path"),
        found.get("geninfo_path"),
        found.get("custom_path"),
    )
    if dummy_source is not None:
        ss.context["dummy_source"] = dummy_source
    if sf_new is not None:
        # ① を編集の出発点にする(既存の編集内容は置き換え。↩ で戻せる)
        ss.score_file = sf_new
        state.ensure_uids(ss.score_file)
        ss.selected_part = 0
    _import_wlgroup_toast(ss)
    st.toast("読み込みました")
    st.rerun()


def _handle_load(ss: SessionStateProxy, inputs: LoadInputs) -> None:
    """「読み込み」ボタンの本体。

    ①(設定)と②(データ)を組み合わせて
    context / score_file を作る。①の設定が RunConfig 形式なら config としても
    最優先で使う(zip 内の設定より優先)。読み込みエラー(選んだモードに必要な
    入力の不足や、指定パスの実体が無い場合の ValueError)は各ヘルパーが送出し、
    末尾の except で捕捉して st.error 表示に集約する。
    """
    try:
        start_text, start_path = _resolve_start_config(inputs)
        if inputs.data_mode == _DATA_MODES[2]:  # データなし = 設定のみ編集
            _finish_config_only_load(ss, start_text)
        else:
            _finish_data_load(ss, inputs, start_text, start_path)
    except Exception as err:
        st.error(str(err))


def _dev_paths_mode_toggle() -> bool:
    """開発者モードのときだけ「サーバ上のパスで指定する」トグルを描画する。

    Returns:
        トグルの状態(開発者モードでなければ常に False)。

    """
    if not _DEV_MODE:
        return False
    return st.toggle(
        "サーバ上のパスで指定する",
        key="paths_mode",
        help="アップロードの代わりに、UI と同じマシンにあるファイル・ディレクトリの"
        "パスを直接指定します(--dev / SCORELIB_UI_DEV=1 で起動したときのみ表示)",
    )


def _start_config_inputs(*, paths_mode: bool) -> tuple[UploadedFile | None, str]:
    """① スコア設定の入力欄(アップロード or パス欄)を描画する。

    Returns:
        (アップロードされた設定ファイル, パス欄の値) のタプル。
        使わなかった側の手段は None / 空文字列。

    """
    st.subheader("① スコア設定")
    if paths_mode:
        config_in = st.text_input(
            "設定 jsonc のパス",
            key="config_path_input",
            help="既存の設定(score.jsonc / optimization設定)から編集を始める場合に指定。"
            "未指定なら新規作成。読み込むと現在の編集内容は置き換わります(↩ で戻せます)",
        )
        return None, config_in
    up_start = st.file_uploader(
        "設定 jsonc",
        type=["jsonc", "json"],
        key="config_start_up",
        help="既存の設定(score.jsonc / optimization設定)から編集を始める場合にアップロード。"
        "未指定なら新規作成。読み込むと現在の編集内容は置き換わります(↩ で戻せます)",
    )
    return up_start, ""


def _measured_data_inputs(base: LoadInputs) -> LoadInputs:
    """② データ: 実測データモードの入力欄を描画する。

    Returns:
        一式 zip / パス欄の入力を base に反映した LoadInputs。

    """
    if base.paths_mode:
        paths = {"data_dir": st.text_input("測定結果ディレクトリのパス", key="data_dir_input")}
        c1, c2 = st.columns(2)
        paths["coef"] = c1.text_input(
            "dVtBudget係数jsonc のパス(任意)",
            key="coef_path_input",
            help="ディレクトリ内にあれば自動検出されます。係数が無い場合は dVtBudget タイプが選択肢に出ません",
        )
        paths["custom"] = c2.text_input(
            "custom_parts.py のパス(任意)",
            key="custom_path_input",
            help="Python関数をスコアパーツ(type=custom)として使う場合のみ。ディレクトリ内にあれば自動検出",
        )
        return dataclasses.replace(base, paths=paths)
    up_zip = st.file_uploader(
        "一式 zip",
        type=["zip"],
        key="bundle_zip",
        help="測定結果に設定・係数・custom_parts.py を同梱できます(サブディレクトリも探索)",
    )
    with st.expander("係数・自作関数を追加する(zip に入っていない場合)"):
        up_coef = st.file_uploader("dVtBudget係数jsonc", type=["jsonc", "json"], key="up_coef")
        up_custom = st.file_uploader("custom_parts.py", type=["py"], key="up_custom")
    return dataclasses.replace(base, up_zip=up_zip, up_coef=up_coef, up_custom=up_custom)


def _dummy_data_inputs(base: LoadInputs) -> LoadInputs:
    """② データ: ダミー(測定前)モードの入力欄を描画する。

    Returns:
        ダミー一式 zip / パス欄と Board/Chip 数の入力を base に反映した
        LoadInputs。

    """
    st.caption("測定値はダミーのため、テスト計算は構造検証のみです")
    up_dummy = None
    paths: dict[str, str] = {}
    if base.paths_mode:
        paths["dummy_dir"] = st.text_input("ダミー一式ディレクトリのパス", key="dummy_dir_input")
    else:
        up_dummy = st.file_uploader(
            "ダミー一式 zip",
            type=["zip"],
            key="dummy_zip",
            help="測定フローが出力する、Board/Chip が1つのダミー一式",
        )
    cd1, cd2 = st.columns(2)
    n_boards = cd1.number_input("Board 数", min_value=1, value=2, step=1, key="dummy_boards")
    chips_text = cd2.text_input(
        "Board ごとの Chip 数",
        value="2",
        key="dummy_chips",
        help="全 Board 共通なら数1つ(例: 4)。Board ごとに違う場合はカンマ区切りで Board の数と同じ個数(例: 4,4,2,2)",
    )
    up_coef = up_custom = None
    if base.paths_mode:
        # 実測モードのパス欄と同じ2欄(手段が変わっても同じことができるように)
        c1, c2 = st.columns(2)
        paths["coef"] = c1.text_input(
            "dVtBudget係数jsonc のパス(任意)",
            key="coef_path_input",
            help="係数が無い場合は dVtBudget タイプが選択肢に出ません",
        )
        paths["custom"] = c2.text_input(
            "custom_parts.py のパス(任意)",
            key="custom_path_input",
            help="Python関数をスコアパーツ(type=custom)として使う場合のみ",
        )
    else:
        with st.expander("係数・自作関数を追加する(任意)"):
            up_coef = st.file_uploader("dVtBudget係数jsonc", type=["jsonc", "json"], key="up_coef")
            up_custom = st.file_uploader("custom_parts.py", type=["py"], key="up_custom")
    return dataclasses.replace(
        base,
        up_dummy=up_dummy,
        up_coef=up_coef,
        up_custom=up_custom,
        paths=paths,
        n_boards=n_boards,
        chips_text=chips_text,
    )


def _load_inputs_form(*, paths_mode: bool) -> LoadInputs:
    """①(スコア設定)と②(データ)の入力フォームを描画する。

    Returns:
        全入力欄の値を束ねた LoadInputs(「読み込み」ボタンへの入力)。

    """
    up_start, config_in = _start_config_inputs(paths_mode=paths_mode)
    st.subheader("② データ")
    data_mode = st.radio(
        "データ",
        _DATA_MODES,
        key="data_mode",
        horizontal=True,
        label_visibility="collapsed",
        captions=[
            "過去実験の測定結果一式",
            "Board/Chip を展開して構造検証",
            "設定だけ編集する",
        ],
    )
    base = LoadInputs(paths_mode=paths_mode, data_mode=data_mode, up_start=up_start, config_in=config_in)
    if data_mode == _DATA_MODES[0]:
        return _measured_data_inputs(base)
    if data_mode == _DATA_MODES[1]:
        return _dummy_data_inputs(base)
    return base


def _detected_inputs_summary(ctx: dict[str, Any]) -> None:
    """認識結果: データディレクトリと同梱ファイルの検出状況を表示する。"""
    st.caption(f"走査したディレクトリ: `{ctx['data_dir']}`")
    if ctx.get("dummy_source"):
        st.info(f"ダミー一式 `{ctx['dummy_source']}` の Board/Chip 展開結果です(数値は無意味・構造検証のみ)")
    if ctx["config_path"]:
        st.success(f"optimization設定jsonc({ctx['config_source']}): {ctx['config_path']}")
    else:
        st.info("optimization設定jsonc: なし — WLgroup / Generation / 既存スコア設定なしで設計を始めます")
    if ctx["coef_path"]:
        st.success(f"dVtBudget係数jsonc({ctx['coef_source']}): {ctx['coef_path']}")
    else:
        st.info("dVtBudget係数jsonc: なし — dVtBudget タイプは選択肢に出ません")
    if ctx["has_initial_temperature"]:
        st.success("initial_temperature.csv")
    else:
        st.warning("initial_temperature.csv: なし(dVtBudget のテスト計算に必要)")
    counts = state.data_axis_counts(ctx["catalogs"])
    if counts:
        st.caption("軸の本数(データから導出): " + " / ".join(f"{a} {n}" for a, n in counts.items()))
    for w in state.geninfo_mismatch_warnings(ctx):
        st.warning(w)


def _detected_context_summary(ss: SessionStateProxy, ctx: dict[str, Any]) -> None:
    """認識結果: 自作関数・Generation と、グループ定義の食い違い警告を表示する。"""
    if ctx["custom_path"]:
        st.success(
            f"自作関数ファイル({ctx['custom_source']}): {ctx['custom_path']}"
            f"(関数: {', '.join(ctx['custom_functions']) or 'なし'})"
        )
    else:
        st.info("自作関数ファイル: なし — type=custom(Python関数パーツ)は選択肢に出ません")
    if ctx["generation"]:
        st.info(f"Generation: {ctx['generation']} / WLgroup: {list(ctx['wlgroup']) or 'なし'}")
    for w in state.group_def_warnings(ss.score_file, state.validation_axis_counts(ctx)):
        st.warning(w)


def _axis_catalog_preview(ctx: dict[str, Any]) -> None:
    """認識結果: 検出された type ごとの軸と値候補の表を表示する。"""
    st.subheader(f"検出された type: {', '.join(ctx['part_types'])}")
    for t in ctx["part_types"]:
        if t not in ctx["catalogs"]:
            continue  # "custom" has no axis catalog
        with st.expander(f"type: {t} の軸と値候補"):
            rows = []
            for axis, cands in ctx["catalogs"][t].items():
                if cands is None:
                    preview = "(自由入力)"
                else:
                    head = ", ".join(str(c) for c in cands[:_CANDS_PREVIEW_MAX])
                    preview = head + ("…" if len(cands) > _CANDS_PREVIEW_MAX else "")
                rows.append({"軸": axis, "値候補": preview})
            st.table(rows)


def _existing_score_offer(ss: SessionStateProxy, ctx: dict[str, Any]) -> None:
    """認識結果: 設定jsonc内の既存スコア設定から編集を始めるボタンを表示する。"""
    if ctx["existing_score_file"] and not ss.score_file["score_parts"]:
        st.divider()
        if st.button("設定jsonc内の既存スコア設定を読み込んで編集を始める"):
            ss.score_file = ctx["existing_score_file"]
            state.ensure_uids(ss.score_file)
            state.import_config_group_defs(ss.score_file, ctx["wlgroup"])
            st.rerun()


def _recognition_section(ss: SessionStateProxy, ctx: dict[str, Any]) -> None:
    """画面1下部: 読み込み済みデータの認識結果を表示する。"""
    st.subheader("認識結果")
    if ctx.get("config_only"):
        st.info(
            "設定のみ編集中(データ未読み込み)。式・グループ定義・パーツの修正と"
            "エクスポートができます。テスト計算と値の候補表示には、上でデータか"
            "ダミー一式を読み込んでください"
        )
        st.caption(f"パーツ: {len(ss.score_file['score_parts'])} / type: {', '.join(ctx['part_types']) or 'なし'}")
        return
    _detected_inputs_summary(ctx)
    _detected_context_summary(ss, ctx)
    _axis_catalog_preview(ctx)
    _existing_score_offer(ss, ctx)


def screen_data() -> None:
    """画面1: データ読み込み。"""
    ss = st.session_state
    st.header("データ読み込み")
    inputs = _load_inputs_form(paths_mode=_dev_paths_mode_toggle())
    if st.button("読み込み", type="primary", key="load_btn"):
        _handle_load(ss, inputs)

    ctx = ss.context
    if not ctx:
        return
    _recognition_section(ss, ctx)


# ------------------------------------------------------ 画面2: スコアパーツ編集


def _order_entry_label(entry: str, part: dict) -> str:
    if entry == "__relative__":
        return "__relative__(相対化を実行)"
    if entry == "__dvtbudget__":
        return "__dvtbudget__(dVtBudget変換を実行)"
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
    axis = c1.selectbox("軸", ["(選択)", *unused], key=f"{uid}_addax")
    if c2.button("追加", key=f"{uid}_addax_btn") and axis != "(選択)":
        part["order"].append(axis)
        part["aggregations"][axis] = state.default_aggregation(axis, catalog.get(axis))
        st.rerun()

    c3, c4 = st.columns([3, 1])
    combo = c3.multiselect("複合軸(複数選択して束ねる)", unused, key=f"{uid}_addcombo")
    if c4.button("束ねて追加", key=f"{uid}_addcombo_btn") and len(combo) >= _MIN_COMBO_AXES:
        entry = COMBINED_SEP.join(combo)
        part["order"].append(entry)
        part["aggregations"][entry] = {"op": "sum"}
        st.rerun()

    if st.button(
        "+ 変換ステップを追加",
        key=f"{uid}_addvirt_btn",
        help="値を行単位で変換するステップ(__offset__ 等)を order に追加します: "
        "足す/引く/掛ける/割る・絶対値(abs)・対数(log)。"
        "典型例: オフセットを足してから相対化、-1を掛けて正負反転、"
        "「軸の値ごと」を選んで WLgroup 別の重み。実行位置は上下ボタンで調整してください",
    ):
        name, n = "__offset__", 2
        while name in part["order"]:
            name = f"__offset{n}__"
            n += 1
        part["order"].append(name)
        part["aggregations"][name] = {"op": "add", "value": 0}
        st.rerun()

    _add_fixed_step_controls(part, uid)


def _add_fixed_step_controls(part: dict, uid: str) -> None:
    """__relative__ / __dvtbudget__ を order に明示配置する操作列を描画する。"""
    fixed_missing = []
    if part.get("relative") and "__relative__" not in part["order"]:
        fixed_missing.append("__relative__")
    if part.get("type") == "dVtBudget" and "__dvtbudget__" not in part["order"]:
        fixed_missing.append("__dvtbudget__")
    if not fixed_missing:
        return
    c7, c8 = st.columns([3, 1])
    step = c7.selectbox(
        "実行位置を明示する(省略時は先頭で実行)",
        fixed_missing,
        key=f"{uid}_addfixed",
        help="__relative__ / __dvtbudget__ を order に置くと、その位置で実行されます(例: 先にWL平均→相対化)",
    )
    if c8.button("orderに置く", key=f"{uid}_addfixed_btn"):
        part["order"].insert(0, step)
        st.rerun()


def _order_dnd_list(part: dict, sel_key: str, uid: str) -> bool:
    """Order 一覧をドラッグ並べ替えリストで描画する(使える場合のみ)。

    Returns:
        D&D リストで描画できたら True(ボタン式の一覧は不要)。

    """
    order = part["order"]
    # 常時ドラッグ可能なリスト1本(案A): 並べ替えにモード切替は不要。
    # コミュニティ製D&D部品は文字列リストしか描画できないため、エントリの
    # 選択は selectbox、削除は下のエディタ内に分離。⠿ はドラッグ可能の
    # 目印、← 編集中 がリストとエディタの対応を示す。
    sel = st.session_state.get(sel_key)
    labels = ["⠿ " + _order_entry_label(e, part) + (" ← 編集中" if e == sel else "") for e in order]
    used_dnd = False
    if widgets.HAS_SORTABLES and order and len(set(labels)) == len(labels):
        st.markdown("**order(上から順に実行)** — リストをドラッグすると並べ替えられます")
        new_labels = widgets.sortable_list(labels, key=f"{uid}_dnd")
        if new_labels is not None:
            used_dnd = True
            if new_labels != labels:
                by_label = dict(zip(labels, order, strict=False))
                part["order"] = [by_label[lbl] for lbl in new_labels]
                st.rerun()
            st.selectbox("編集するエントリ", order, key=sel_key)
    return used_dnd


def _order_button_list(part: dict, sel_key: str, uid: str) -> None:
    """Order 一覧をボタン式(✎ 選択・↑↓ 移動・✕ 削除)で描画する。"""
    order = part["order"]
    st.markdown("**order(上から順に実行)** — ✎ でエントリを選ぶと下に編集欄が出ます")
    for i, entry in enumerate(list(order)):
        c_sel, c_lbl, c_up, c_dn, c_rm = st.columns([1, 8, 1, 1, 1])
        selected = st.session_state.get(sel_key) == entry
        if c_sel.button(
            "✎", key=f"{uid}_sel{i}", type="primary" if selected else "secondary", help="このエントリを編集"
        ):
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


def _order_entry_editor(part: dict, catalog: dict, sf: dict, uid: str, measure_labels: dict) -> None:
    """選択中エントリの常時表示エディタ(枠つき)を描画する。"""
    order = part["order"]
    entry = st.session_state.get(_wk(f"{uid}_sel_entry"))
    if not entry or entry not in order:
        return
    with st.container(border=True):
        c_head, c_del = st.columns([8, 1])
        c_head.markdown(f"**編集中: {entry}**")
        if c_del.button("削除", key=f"{uid}_ed_del", help="このエントリを order から削除"):
            order.remove(entry)
            part["aggregations"].pop(entry, None)
            st.rerun()
        if entry in VIRTUAL_FIXED:
            st.caption("このステップに集計指示はありません(位置のみ意味を持ちます)")
        else:
            spec = part["aggregations"].setdefault(entry, {"op": "mean"})
            gdefs = sf.get("groupDefs", {})
            editor_ctx = widgets.EditorContext(
                catalog=catalog,
                set_names=sorted(sf["selectionSets"]),
                # 変換ステップの「軸の値ごとの定数(重み)」用: グループ派生軸を
                # 先頭に、次いで値候補が分かるカテゴリ軸
                by_candidates=sorted(gdefs) + [a for a in catalog if a != "InBatchEpoch" and a not in gdefs],
                by_value_labels={
                    **{a: c for a, c in catalog.items() if c},
                    **{n: list(d.get("groups", {})) for n, d in gdefs.items()},
                },
                weight_set_names=sorted(sf.get("weightSets", {})),
                measure_labels=measure_labels,
            )
            widgets.agg_editor(entry, spec, editor_ctx, key=_wk(f"{uid}_{entry}"))


def _order_editor(part: dict, catalog: dict, sf: dict, uid: str, measure_labels: dict) -> None:
    """Order エディタ: 一覧(並べ替え・削除)+ 選択エントリ用の常時表示エディタ1つ。

    expander はラベルが変わるたびに閉じてしまい値の編集が
    苦痛になるため使わない。
    """
    sel_key = _wk(f"{uid}_sel_entry")
    order = part["order"]
    if st.session_state.get(sel_key) not in order:
        st.session_state[sel_key] = order[0] if order else None
    if not _order_dnd_list(part, sel_key, uid):
        _order_button_list(part, sel_key, uid)
    _order_entry_editor(part, catalog, sf, uid, measure_labels)


def _custom_part_editor(part: dict[str, Any], ctx: dict[str, Any], uid: str) -> None:
    st.markdown("**自作関数パーツ**")
    st.caption(
        "custom_parts.py の関数を1つ呼び、その戻り値(1スカラー)がこのパーツの値になります。"
        "実行側では SVN リポジトリ直下の custom_parts.py が使われるため、"
        "設計時と同じリビジョンのファイルを読み込んでください。"
    )
    funcs = cast("list[str]", ctx.get("custom_functions") or [])
    cur = part.get("function") or part.get("name")
    if not funcs:
        st.error("custom_parts.py が読み込まれていません(画面1でパス指定するか、一式zipに同梱してください)")
    options = funcs + ([cur] if cur not in funcs else [])
    part["function"] = st.selectbox(
        "関数", options, index=options.index(cur) if cur in options else 0, key=_wk(f"{uid}_func")
    )
    if funcs and part["function"] not in funcs:
        st.error(f"関数 '{part['function']}' は読み込んだ custom_parts.py にありません")
    _custom_params_editor(part, uid)


def _custom_params_editor(part: dict[str, Any], uid: str) -> None:
    """params(ctx.params として関数に渡す追加パラメータ)の行エディタを描画する。"""
    st.markdown("**params(ctx.params として関数に渡す追加パラメータ)**")
    # 行エディタの構造は _group_rows_editor(画面3のグループ行)と似ているが、
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
        nk = c_k.text_input("名前", value=pk, key=_wk(f"{uid}_prm{i}_k"))
        nv = c_v.text_input(
            "値",
            value=str(pv),
            key=_wk(f"{uid}_prm{i}_v"),
            help="true/false・数値は型付きで渡されます(それ以外は文字列)",
        )
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
    if st.button("+ パラメータを追加", key=f"{uid}_prm_add"):
        n = len(params) + 1
        pk = f"param{n}"
        while pk in params:
            n += 1
            pk = f"param{n}"
        params[pk] = 0
        st.rerun()


def _sync_part_selection(ss: SessionStateProxy, sf: dict) -> list[str]:
    """パーツ選択状態("part_sel")を一覧と同期する。

    Returns:
        全パーツの _uid のリスト(表示順)。

    """
    # パーツ選択は _uid を ss["part_sel"](アプリ管理の状態)で持つ。プルダウン
    # 本体はラベル変更でキーごと作り直すため(streamlit#11268 対策 —
    # screen_parts 参照)、ウィジェット状態は選択の置き場にしない。選び直しは
    # selectbox 直後の st.rerun() で反映するので一覧のマーカーは遅れない。
    # プログラムからの選択移動(追加・複製)は part_sel_pending 経由で次の実行の
    # 冒頭(ここ)で反映する。並べ替えは対応不要(uid は変わらないので選択が
    # 自動で追従する)。
    uids = [p["_uid"] for p in sf["score_parts"]]
    pending = ss.pop("part_sel_pending", None)
    if pending in uids:
        ss["part_sel"] = pending
    if ss.get("part_sel") not in uids:
        ss["part_sel"] = uids[min(ss.selected_part, len(uids) - 1)] if uids else None
    sel_uid = ss.get("part_sel")
    ss.selected_part = uids.index(sel_uid) if sel_uid in uids else 0
    return uids


def _part_list_overview(sf: dict, ctx: dict[str, Any], sel_uid: str | None) -> set:
    """パーツ一覧(D&D 並べ替えリスト or 表)と状態マーカーを描画する。

    Returns:
        ⚠ 表示対象(設定エラー・データ無し type・データに無い値)のパーツ
        _uid の集合。

    """
    rows = state.part_summary_rows(sf)
    # 状態マーカー: D&D部品は文字列しか描画できず項目単位の色分けが
    # 不可能なため、⚠ の接頭記号を見た目の代替にする
    invalid = {
        p["_uid"] for p in sf["score_parts"] if state.validate_part(p, sf["selectionSets"], sf.get("weightSets"))
    }
    # データに測定ファイルの無い type のパーツも ⚠ 対象(設定としては有効な
    # ままなので検証NGとは別扱い — 編集画面に警告文が出る)
    no_data = state.part_types_without_data(sf, ctx)
    # filter 等の値がデータに無いパーツも ⚠ 対象(設定としては有効だが計算は
    # 必ず失敗する。編集対象に選ばなくても読み込み直後から気づけるように)
    mismatch = state.part_value_mismatches(sf, ctx)
    for r, p in zip(rows, sf["score_parts"], strict=False):
        uid_ = p["_uid"]
        if uid_ in invalid:
            r["状態"] = "⚠ 設定エラー"
        elif uid_ in no_data:
            r["状態"] = "⚠ データ無し"
        else:
            r["状態"] = "⚠ データに無い値" if uid_ in mismatch else "OK"
    invalid |= no_data | set(mismatch)
    parts_dnd = False
    if widgets.HAS_SORTABLES and len(sf["score_parts"]) > 1:
        labels = state.part_list_labels(sf, sel_uid, invalid, rows=rows)
        new_labels = widgets.sortable_list(labels, key="parts_dnd")
        if new_labels is not None:
            parts_dnd = True
            st.caption("パーツ一覧はドラッグで並べ替えられます")
            if new_labels != labels:
                by_label = dict(zip(labels, sf["score_parts"], strict=False))
                sf["score_parts"] = [by_label[lbl] for lbl in new_labels]
                st.rerun()
    if rows and not parts_dnd:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    return invalid


def _import_parts(sf: dict, text: str) -> None:
    """単体エクスポートされたパーツ jsonc を現在の score_file へ追記取り込みする。

    名前が重複するパーツは連番付きに改名し、selectionSets は既存名を
    優先して補う。解析・検証エラー(state.import_score_file の
    TypeError / ValueError)はそのまま呼び出し元へ伝える。
    """
    imported = state.import_score_file(text)
    for p in imported["score_parts"]:
        p["name"] = p["name"] if p["name"] not in state.part_names(sf) else state.unique_part_name(sf, p["name"])
        sf["score_parts"].append(p)
    for k, v in imported.get("selectionSets", {}).items():
        sf["selectionSets"].setdefault(k, v)
    state.ensure_uids(sf)


def _part_add_controls(ss: SessionStateProxy, sf: dict, ctx: dict[str, Any]) -> None:
    """新規パーツの追加(雛形生成)と単体インポートの操作列を描画する。"""
    c1, c2, c3 = st.columns([2, 1, 1])
    new_type = c1.selectbox("新規パーツの type", ctx["part_types"], key="new_part_type")
    # 設定のみ編集でパーツが無い設定を読んだ場合など、type 候補が空なら追加不可
    if c2.button("追加(雛形を生成)", type="primary", key="add_part_btn") and new_type:
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
                _import_parts(sf, up.getvalue().decode("utf-8"))
                st.rerun()
            except Exception as err:
                st.error(str(err))


def _part_row_actions(ss: SessionStateProxy, sf: dict, idx: int) -> None:
    """選択中パーツの移動・複製・削除ボタン列を描画する。"""
    cup, cdn, cdup, cdel = st.columns(4)
    if cup.button("▲ 上へ", disabled=idx == 0, help="このパーツを一覧の1つ上へ"):
        state.move_entry(sf["score_parts"], idx, -1)
        st.rerun()
    if cdn.button("▼ 下へ", disabled=idx == len(sf["score_parts"]) - 1, help="このパーツを一覧の1つ下へ"):
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


def _part_header_editor(sf: dict, ctx: dict[str, Any], idx: int) -> None:
    """選択中パーツの name / type 編集と雛形の再生成ボタンを描画する。"""
    part = sf["score_parts"][idx]
    uid = part["_uid"]
    part["name"] = st.text_input("name", value=part.get("name", ""), key=_wk(f"{uid}_name"))
    cur_type = part.get("type")
    # 前提ファイル未読み込みの type(custom ファイルや係数 jsonc が無い等)
    # でも現在の type は選択肢に残す — 残さないと selectbox がパーツの type
    # を黙って別の値に書き換えてしまう
    types = ctx["part_types"] + ([cur_type] if cur_type not in ctx["part_types"] else [])
    tsel, tregen = st.columns([3, 1])
    new_type = tsel.selectbox(
        "type", types, index=types.index(cur_type) if cur_type in types else 0, key=_wk(f"{uid}_type")
    )
    if new_type != cur_type:
        notice = state.switch_part_type(part, new_type)
        if notice:
            st.toast(notice)
        if new_type != "custom":
            st.warning("type を変更しました。軸構成が異なる type の場合は雛形の再生成を推奨します")
    if tregen.button(
        "雛形を再生成", key=f"{uid}_regen", help="現在の type に合わせてパーツを作り直します(編集内容は失われます)"
    ):
        if part["type"] == "custom":
            fresh = state.custom_part_skeleton(part["name"], ctx["custom_functions"])
        else:
            fresh = state.part_skeleton(part["name"], part["type"], ctx["catalogs"][part["type"]])
        fresh["_uid"] = uid
        sf["score_parts"][idx] = fresh
        st.rerun()


def _part_body_editor(part: dict, ctx: dict[str, Any], sf: dict, uid: str) -> None:
    """選択中パーツの本体エディタ(custom or 集計エディタ群)と問題の表示を描画する。"""
    no_data = part.get("type") != "custom" and part.get("type") not in ctx.get("catalogs", {})
    if no_data:
        st.warning(
            f"type '{part.get('type')}' の測定データがありません — このパーツは"
            "テスト計算できません(編集・保存は可能。不要なら削除してください)"
        )
    mismatches = state.part_value_mismatches({"score_parts": [part]}, ctx).get(uid, [])
    for w in mismatches:
        st.warning(w)
    if part.get("type") == "custom":
        _custom_part_editor(part, ctx, uid)
    else:
        catalog = _with_group_axes(_catalog_for_part(ctx, part), sf)
        mlabels = (ctx.get("measure_labels") or {}).get(part.get("type"), {})
        widgets.relative_editor(
            part,
            widgets.EditorContext(catalog=catalog, set_names=sorted(sf["selectionSets"]), measure_labels=mlabels),
            key=_wk(f"{uid}_rel"),
        )
        st.divider()
        _order_editor(part, catalog, sf, uid, mlabels)
        _add_entry_controls(part, catalog, uid)

    problems = state.validate_part(part, sf["selectionSets"], sf.get("weightSets"))
    for p in problems:
        st.error(p)
    # 「問題なし」は誤りが一切ないときだけ(サイドバーと同じ一本化 —
    # 上に警告が出ているのに OK と書いてある同居をなくす)
    if not problems and not mismatches and not no_data:
        st.success("このパーツ: 問題なし")


def screen_parts() -> None:
    """画面2: スコアパーツ編集。"""
    ss = st.session_state
    ctx = ss.context
    st.header("スコアパーツ編集")
    if not ctx:
        st.info("先に画面1でデータを読み込んでください")
        return
    sf = ss.score_file
    state.ensure_uids(sf)

    uids = _sync_part_selection(ss, sf)
    invalid = _part_list_overview(sf, ctx, ss.get("part_sel"))
    _part_add_controls(ss, sf, ctx)

    if not sf["score_parts"]:
        return
    st.divider()

    select_labels = state.part_select_labels(sf, invalid)
    sel_uid = ss.get("part_sel")
    # キー付き selectbox は「選択中のラベルだけが変わった」とき表示が更新されない
    # (改名しても欄が古い名前のまま — streamlit#11268)。sortable_list と同じく、
    # ラベルが変わったらキーごと部品を作り直す。選択の実体は _uid で
    # ss["part_sel"] に持ち続ける(同名パーツの誤解決を防ぐ — 進行記録 #12)
    labels_key = f"part_sel_{hash(tuple(select_labels.get(u, '?') for u in uids))}"
    picked = st.selectbox(
        "編集するパーツ",
        uids,
        index=uids.index(sel_uid) if sel_uid in uids else 0,
        key=labels_key,
        format_func=lambda u: select_labels.get(u, "?"),
    )
    if picked != sel_uid:
        # 選び直しは即再実行して、上に描画済みの一覧・マーカーを追随させる
        ss["part_sel"] = picked
        st.rerun()
    idx = ss.selected_part  # 画面冒頭で part_sel から導出済み
    part = sf["score_parts"][idx]
    uid = part["_uid"]

    _part_row_actions(ss, sf, idx)
    _part_header_editor(sf, ctx, idx)
    st.divider()
    _part_body_editor(part, ctx, sf, uid)


# ---------------------------------------- 画面3: 選択セット・グループ定義


def screen_sets() -> None:
    """画面3: 選択セット・グループ定義。"""
    ss = st.session_state
    ctx = ss.context
    sf = ss.score_file
    st.header("選択セット・グループ定義")
    _selection_sets_section(sf, ctx)
    st.divider()
    _group_defs_section(sf, ctx)


def _selection_sets_section(sf: dict, ctx: dict[str, object] | None) -> None:
    st.subheader("選択セット")
    st.caption(
        "複数パーツで使い回す値の組(例: State と Read_Label の対応リスト)に名前を付けて管理します。"
        "パーツ側からは ref で参照します。"
    )
    sets = sf["selectionSets"]

    if sets:
        st.dataframe(
            [
                {
                    "名前": n,
                    "件数": len(v),
                    "参照しているパーツ": ", ".join(state.referencing_parts(sf, n)) or "(なし)",
                }
                for n, v in sets.items()
            ],
            use_container_width=True,
            hide_index=True,
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
    name = st.selectbox("編集するセット", sorted(sets), key=_wk("edit_set_name"))
    if name is None:
        # 到達しない防御: options(sets)は上の early return で非空を保証済み
        return
    _selection_set_editor(sf, ctx, name)
    st.divider()
    _selection_set_actions(sf, name)


def _selection_set_editor(sf: dict, ctx: dict[str, object] | None, name: str) -> None:
    """選択セット1つ分の形式(複合軸 / 単一軸)選択と行エディタを描画する。"""
    sets = sf["selectionSets"]
    values = sets[name]
    catalog = _merged_catalog(ctx) if ctx else {}
    all_axes = sorted(catalog) if catalog else []
    current_axes = sorted(values[0].keys()) if values and isinstance(values[0], dict) else []
    kind = st.radio(
        "形式",
        ["複合軸(軸ごとの値の組)", "単一軸の値リスト"],
        index=0 if (not values or isinstance(values[0], dict)) else 1,
        horizontal=True,
        key=_wk(f"set_{name}_kind"),
    )
    if kind.startswith("複合軸"):
        axes = st.multiselect(
            "軸",
            all_axes or current_axes,
            default=current_axes or [a for a in ("State", "Read_Label") if a in catalog],
            key=_wk(f"set_{name}_axes"),
        )
        if axes:
            if current_axes and sorted(axes) != current_axes and values:
                st.warning("軸構成を変えると既存の行は作り直しになります")
                if st.button("行をクリアして軸構成を変更", key=f"set_{name}_reset"):
                    values.clear()
                    st.rerun()
            else:
                sets[name] = widgets.selection_list_widget(axes, catalog, values, _wk(f"set_{name}_rows"))
    else:
        axis = st.selectbox("軸", all_axes, key=_wk(f"set_{name}_axis")) if all_axes else None
        sets[name] = widgets.selection_list_widget(
            [axis] if axis else ["値"], catalog, values, _wk(f"set_{name}_vals")
        )


def _selection_set_actions(sf: dict, name: str) -> None:
    """選択セットの別名保存・削除のボタン列を描画する。"""
    c3, c4, c5 = st.columns([2, 1, 1])
    alias = c3.text_input("別名で保存(コピーを作成。参照は元の名前のまま)", key=f"set_{name}_alias")
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


def _group_defs_section(sf: dict, ctx: dict[str, object] | None) -> None:
    st.subheader("グループ定義(軸のグループ分割)")
    st.caption(
        "数値軸を範囲でまとめた派生軸を定義します。パーツの order に定義名を軸として置くと、"
        "好きな位置で集計できます(例: WLを平均 → Board を max → 最後に WLgroup を max)。"
        "定義はスコアと一緒に保存・エクスポートされます。"
    )
    defs = sf.setdefault("groupDefs", {})
    catalog = _merged_catalog(ctx) if ctx else {}
    numeric_axes = [a for a, c in catalog.items() if a != "InBatchEpoch" and not (c and isinstance(c[0], (str, bool)))]

    for w in state.group_def_warnings(sf, state.validation_axis_counts(ctx)):
        st.warning(w)

    if defs:
        st.dataframe(
            [
                {
                    "名前": n,
                    "対象軸": d.get("axis", "?"),
                    "グループ数": len(d.get("groups", {})),
                    "参照しているパーツ": ", ".join(state.parts_referencing_group_def(sf, n)) or "(なし)",
                }
                for n, d in defs.items()
            ],
            use_container_width=True,
            hide_index=True,
        )

    _group_def_create_controls(sf, catalog, numeric_axes)

    if not defs:
        return
    name = st.selectbox("編集する定義", sorted(defs), key=_wk("edit_gdef_name"))
    if name is None:
        # 到達しない防御: options(defs)は上の early return で非空を保証済み
        return
    _group_def_editor(sf, name, numeric_axes)
    st.divider()
    _group_weight_editor(sf, name)


def _group_def_create_controls(sf: dict, catalog: dict, numeric_axes: list[str]) -> None:
    """新規グループ定義の作成列(名前・対象軸・作成ボタン)を描画する。"""
    c1, c2, c3 = st.columns([2, 2, 1])
    gname = c1.text_input("新規定義名", key="new_gdef_name")
    gaxis = c2.selectbox("対象軸", numeric_axes or ["WL"], key="new_gdef_axis")
    if c3.button("作成", key="new_gdef_btn"):
        try:
            state.add_group_def(sf, gname, gaxis, set(catalog))
            st.rerun()
        except ValueError as err:
            st.error(str(err))


def _reset_gdef_row_widgets(name: str) -> None:
    """定義 name の行ウィジェット状態(キー gdef_{name}_*)を破棄する。

    行ウィジェットのキーは添字ベースなので、行の増減後は記憶された
    状態が別の行の値を表示してしまう → 構造変更時にキーごと破棄する。
    """
    for k in list(st.session_state):
        if str(k).startswith(f"gdef_{name}_"):
            del st.session_state[k]


def _group_rows_editor(gd: dict, name: str) -> None:
    """グループ行(名前・開始・終了・削除)のエディタを描画する。"""
    groups = gd.setdefault("groups", {})
    new_items = []
    for i, (label, rng) in enumerate(list(groups.items())):
        c_l, c_lo, c_hi, c_rm = st.columns([3, 2, 2, 1])
        lbl = c_l.text_input("グループ名", value=label, key=_wk(f"gdef_{name}_{i}_lbl"))
        lo = c_lo.number_input("開始", value=int(rng[0]), step=1, key=_wk(f"gdef_{name}_{i}_lo"))
        hi = c_hi.number_input("終了", value=int(rng[1]), step=1, key=_wk(f"gdef_{name}_{i}_hi"))
        if c_rm.button("✕", key=f"gdef_{name}_{i}_rm", help="このグループを削除"):
            groups.pop(label, None)
            _reset_gdef_row_widgets(name)
            st.rerun()
        new_items.append(((lbl or "").strip() or label, [int(lo), int(hi)]))
    labels = [label for label, _ in new_items]
    if len(set(labels)) != len(labels):
        st.error("グループ名が重複しています")
    else:
        gd["groups"] = dict(new_items)


def _group_def_editor(sf: dict, name: str, numeric_axes: list[str]) -> None:
    """グループ定義1つ分(対象軸・記法・行・追加/削除)のエディタを描画する。"""
    gd = sf["groupDefs"][name]
    axis_opts = numeric_axes or [gd.get("axis", "WL")]
    cur_axis = gd.get("axis")
    # データに無い既存の対象軸も印つきで選択肢に残す(widgets.value_widget と
    # 同じ定石 — 描画しただけで先頭の軸へ黙って書き換わるのを防ぐ)
    if cur_axis is not None and cur_axis not in axis_opts:
        axis_opts = [*axis_opts, cur_axis]
    gd["axis"] = st.selectbox(
        "対象軸",
        axis_opts,
        index=axis_opts.index(cur_axis) if isinstance(cur_axis, str) and cur_axis in axis_opts else 0,
        key=_wk(f"gdef_{name}_axis"),
        format_func=lambda a, _cur=cur_axis, _known=numeric_axes: (
            f"{a}(軸がありません)" if a == _cur and a not in _known else str(a)
        ),
    )
    physical = st.checkbox(
        "範囲を Physical 番号で記入する(現行スクリプトの WLgroupDefinLogical=False 相当)",
        value=not gd.get("definedInLogical", True),
        key=_wk(f"gdef_{name}_phys"),
        help="データの csv は Logical 番号なので、計算時に軸の総数 N を使って N-1-p で"
        "読み替えます(N はデータから自動導出)",
    )
    gd["definedInLogical"] = not physical

    _group_rows_editor(gd, name)
    groups = gd["groups"]

    c4, c5 = st.columns(2)
    if c4.button("+ グループを追加", key=f"gdef_{name}_add"):
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
            _reset_gdef_row_widgets(name)
            st.rerun()
        except ValueError as err:
            st.error(str(err))


def _group_weight_editor(sf: dict, name: str) -> None:
    """グループ定義に対応する重みセット {name}Weight のエディタを描画する。"""
    groups = sf["groupDefs"][name].get("groups", {})
    weights_all = sf.setdefault("weightSets", {})
    wname = f"{name}Weight"
    enabled = st.checkbox(
        f"重みセット {wname} を定義する",
        value=wname in weights_all,
        key=_wk(f"gdef_{name}_w_on"),
        help="パーツの order に定数演算ステップ(掛け算など)を置き、「軸の値ごと」でこのセットを"
        " ref 参照すると、好きなタイミングでグループ別の重みを掛けられます。"
        "設定jsoncの WLgroupWeight はこの名前で取り込まれます",
    )
    if not enabled:
        weights_all.pop(wname, None)
        return
    cur = weights_all.get(wname)
    scalar_mode = st.checkbox(
        "全グループ共通の1値にする",
        value=not isinstance(cur, dict),
        key=_wk(f"gdef_{name}_w_scalar"),
    )
    if scalar_mode:
        weights_all[wname] = st.number_input(
            "重み",
            value=float(cur) if isinstance(cur, (int, float)) else 1.0,
            key=_wk(f"gdef_{name}_w_sv"),
        )
    else:
        # 値ごと辞書の共通編集欄(widgets.per_value_dict_editor): 候補外の
        # 既存キー(改名前のグループ等)も印つきで保持し、描画だけでは辞書を
        # 育てない・消さない
        widgets.per_value_dict_editor(weights_all, wname, list(groups), 1.0, _wk(f"gdef_{name}_w_"))


# ---------------------------------------------------- 画面4: スコア合成・制約


def _constraints_section(sf: dict, names: list[str]) -> None:
    """constraintThreshold(制約)の行エディタと追加列を描画する。"""
    st.subheader("constraintThreshold(制約)")
    st.caption("指定したパーツの値がこの値を超えた提案パラメータは、解の候補になりません。")
    ct = sf["constraintThreshold"]
    for key in list(ct):
        entry = ct[key] if isinstance(ct[key], dict) else {"value": ct[key]}
        c1, c2, c3, c4 = st.columns([3, 2, 3, 1])
        c1.markdown(f"**{key}**" + ("" if key in names else "　:red[⚠ パーツがありません]"))
        entry["value"] = c2.number_input("value", value=float(entry.get("value", 0)), key=_wk(f"ct_{key}_v"))
        dynamic = c3.checkbox(
            "動的制約 (percentile)",
            value=str(entry.get("active", "")).lower() == "true",
            key=_wk(f"ct_{key}_dyn"),
            help="実測値の percentile*coef と指定値の大きい方を閾値に使う",
        )
        if dynamic:
            entry["active"] = "True"
            entry["type"] = "percentile"
            entry["coef"] = c3.number_input("coef", value=float(entry.get("coef") or 20), key=_wk(f"ct_{key}_coef"))
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
    target = c5.selectbox("制約を追加するパーツ", addable or ["(追加できるパーツなし)"], key="ct_add_sel")
    if c6.button("制約を追加") and addable:
        ct[target] = {"value": 0.0}
        st.rerun()


def screen_compose() -> None:
    """画面4: スコア合成・制約。"""
    ss = st.session_state
    sf = ss.score_file
    st.header("スコア合成・制約")
    names = state.part_names(sf)
    if not names:
        st.info("先に画面2でスコアパーツを作成してください")
        return

    st.subheader("expression(Score の式)")
    st.caption(
        "パーツ名を演算子で組み合わせます。使える関数: log(=log10), ln, log2, exp, sqrt, min, max, mean, sum, abs"
    )
    cols = st.columns(min(len(names), 6))
    for i, n in enumerate(names):
        if cols[i % len(cols)].button(n, key=f"ins_{n}", help="式の末尾に挿入"):
            new_expr = (sf["expression"] + " + " + n).strip(" +") if sf["expression"] else n
            sf["expression"] = new_expr
            # 入力ウィジェットは自分の状態を記憶していて古い式を表示し続ける
            # ため、こちらも更新する(安全: ボタンは入力欄より先に描画される)
            st.session_state[_wk("expr_input")] = new_expr
            st.rerun()
    sf["expression"] = st.text_input("expression", value=sf["expression"], key=_wk("expr_input"))

    _constraints_section(sf, names)

    problems = state.validate_score_file(sf)
    for p in problems:
        st.error(p)
    if not problems:
        st.success("問題なし")


# ------------------------------------------ 画面5: テスト実行・エクスポート


def _run_test_compute(ctx: dict[str, Any] | None, sf: dict, test_dir: str, generation: str, test_coef: str) -> None:
    """テスト計算を検証つきで実行し、結果表またはエラーを表示する。"""
    if not test_dir.strip():
        st.error("データディレクトリを入力してください")
    elif not sf["score_parts"]:
        st.error("スコアパーツがありません")
    else:
        problems = state.config_problem_messages(sf, ctx)
        if problems:
            st.error("設定に誤りがあるため実行できません(内容は以下とサイドバー)")
            for p in problems:
                st.error(p)
        else:
            try:
                with st.spinner("計算中…"):
                    result = state.run_test_compute(
                        sf,
                        test_dir,
                        state.TestComputeInputs(
                            generation=generation or None,
                            wlgroup=ctx["wlgroup"] if ctx else None,
                            coef_path=test_coef or None,
                            custom_path=ctx["custom_path"] if ctx else None,
                            geninfo_path=ctx["geninfo_path"] if ctx else None,
                        ),
                    )
                st.dataframe(
                    [{"項目": k, "値": v} for k, v in result.items()],
                    use_container_width=True,
                    hide_index=True,
                )
            except Exception as err:
                st.error(f"計算エラー: {err}")


def _test_compute_section(ctx: dict[str, Any] | None, sf: dict) -> None:
    """テスト計算(入力欄と実行ボタン)の区画を描画する。"""
    st.subheader("テスト計算")
    st.caption("測定データのあるディレクトリでスコアを実際に計算します(エンジン compute_score_file を直接呼びます)")
    if ctx and ctx.get("dummy_source"):
        st.warning("ダミー展開データを読み込んでいます。テスト計算の数値に意味はありません(構造・設定の検証のみ)")
    if ctx and ctx.get("config_only"):
        st.caption(
            "設定のみ編集中: テスト計算にはデータディレクトリの指定(または画面1でのデータ/ダミー読み込み)が必要です"
        )
    default_dir = (ctx.get("data_dir") or "") if ctx else ""
    test_dir = st.text_input("データディレクトリ", value=default_dir, key="test_dir")
    c1, c2 = st.columns(2)
    generation = c1.text_input(
        "Generation(dVtBudget 使用時に必要)", value=(ctx["generation"] if ctx else "") or "", key="test_gen"
    )
    test_coef = c2.text_input(
        "dVtBudget係数jsonc のパス(dVtBudget 使用時に必要)",
        value=(ctx["coef_path"] if ctx else "") or "",
        key="test_coef",
    )
    if st.button("計算を実行", type="primary", key="run_btn"):
        _run_test_compute(ctx, sf, test_dir, generation, test_coef)


def _export_section(sf: dict) -> None:
    """エクスポート(score.jsonc 一式 / パーツ単体)の区画を描画する。"""
    st.subheader("エクスポート")
    problems = state.validate_score_file(sf) if sf["score_parts"] else ["スコアパーツがありません"]
    if problems:
        st.warning("検証エラーがあるためエクスポートできません: " + " / ".join(problems))
        return
    if any(p.get("type") == "custom" for p in sf["score_parts"]):
        st.caption(
            "⚠ type=custom のパーツを含みます。関数本体は score.jsonc には入らないため、"
            "実行側の SVN リポジトリ直下に同じ custom_parts.py が必要です。"
        )
    st.download_button(
        "score.jsonc をダウンロード(selectionSets 同梱)",
        data=state.score_file_to_jsonc(sf),
        file_name="score.jsonc",
        mime="application/json",
    )
    names = state.part_names(sf)
    c1, c2 = st.columns([3, 1])
    pi = c1.selectbox("パーツ単体エクスポート", range(len(names)), format_func=lambda i: names[i], key="exp_part")
    try:
        c2.download_button(
            "ダウンロード",
            data=state.export_part(sf, pi),
            file_name=f"{names[pi]}.jsonc",
            mime="application/json",
            key="exp_part_btn",
        )
    except ValueError as err:
        st.error(str(err))


def _import_section(ss: SessionStateProxy) -> None:
    """インポート(現在の編集内容の置き換え)の区画を描画する。"""
    st.subheader("インポート")
    up = st.file_uploader("score.jsonc または optimization設定jsonc", type=["jsonc", "json"], key="import_all")
    if up is not None and st.button("読み込む(現在の編集内容を置き換え)"):
        try:
            ss.score_file = state.import_score_file(up.getvalue().decode("utf-8"))
            state.ensure_uids(ss.score_file)
            ss.selected_part = 0
            st.rerun()
        except Exception as err:
            st.error(str(err))


def screen_test_export() -> None:
    """画面5: テスト実行・エクスポート。"""
    ss = st.session_state
    ctx = ss.context
    sf = ss.score_file
    st.header("テスト実行・エクスポート")

    _test_compute_section(ctx, sf)
    st.divider()
    _export_section(sf)
    st.divider()
    _import_section(ss)


# --------------------------------------------------------------------- main


def _sidebar_status(sf: dict) -> None:
    """サイドバーの設定状態(問題なし / 設定の誤り N 件+全メッセージ)を描画する。

    構造の誤り・データに無い値・データ無し type を「設定の誤り」に一本化する
    (種類の書き分けはパーツ一覧の ⚠ ラベルと各メッセージが担う)。
    「問題なし」は誤りゼロのときだけ出す — OK と警告の同居をなくす。
    """
    problems = state.config_problem_messages(sf, st.session_state.get("context"))
    if problems:
        st.error(f"設定の誤り {len(problems)} 件")
        with st.expander("内容を表示"):
            for p_msg in problems:
                st.caption(p_msg)
    else:
        st.success("問題なし")


def main() -> None:
    """エントリポイント: サイドバーと現在の画面を描画する。"""
    st.set_page_config(page_title="スコア設計 (score_gui Phase1)", layout="wide")
    _init()

    # undo からの画面ジャンプ: radio("screen") の描画前に反映する必要がある
    pending_screen = st.session_state.pop("screen_pending", None)
    if pending_screen in SCREENS:
        st.session_state["screen"] = pending_screen

    with st.sidebar:
        st.title("スコア設計")
        user = _sidebar_user()
        screen = st.radio("画面", SCREENS, key="screen")
        sf = st.session_state.score_file
        st.divider()
        if st.button(
            "↩ 元に戻す",
            key="undo_btn",
            disabled=not st.session_state.history,
            help="直前の編集操作を取り消します(直近20操作まで)",
        ):
            _undo()
        st.caption(f"パーツ: {len(sf['score_parts'])} / 選択セット: {len(sf['selectionSets'])}")
        if sf["score_parts"]:
            _sidebar_status(sf)
        st.caption(
            f"engine scorelib_param {scorelib_param.__version__}",
            help="このUIに同梱されたエンジンの版。実験実行側(SVNの scorelib)と一致しているかの確認用",
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
