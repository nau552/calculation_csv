"""Streamlit AppTest によるE2Eテスト: アプリをブラウザなしで起動し、
データ読み込み → 雛形からパーツ作成 → テスト計算までを一気通貫で検証する。"""
from pathlib import Path

import pytest

from ui import state

APP = str(Path(__file__).resolve().parent.parent / "ui" / "app.py")
MINI_TYPES = ["FBC", "KLD", "PROGLOOP", "PROGSTATUS", "dVthSGWLD", "tPROG", "tR"]
SCREEN_PARTS = "2. スコアパーツ編集"
SCREEN_SETS = "3. 選択セット・グループ定義"
SCREEN_COMPOSE = "4. スコア合成・制約"
SCREEN_TEST = "5. テスト実行・エクスポート"


@pytest.fixture
def at(tmp_path, monkeypatch):
    apptest = pytest.importorskip("streamlit.testing.v1").AppTest
    # ユーザの実際の下書きファイルに触れないよう保存先を差し替える
    monkeypatch.setattr(state, "DRAFTS_DIR", tmp_path / "drafts")
    # AppTest は file_uploader を操作できないため、開発者モード
    # （パス指定トグル）でテストする。一般ユーザ表示のテストは
    # test_paths_hidden_without_dev_option が env を消して確認する
    monkeypatch.setenv("SCORELIB_UI_DEV", "1")
    t = apptest.from_file(APP, default_timeout=60)
    t.run()
    assert not t.exception
    return t


def _load_data(at, data_dir):
    # アップロードは AppTest から操作できないため「サーバ上のパスで指定」を使う
    at.toggle(key="paths_mode").set_value(True).run()
    at.text_input(key="data_dir_input").set_value(str(data_dir))
    at.button(key="load_btn").click().run()
    assert not at.exception
    assert at.session_state["context"]["types"] == MINI_TYPES


def test_app_starts(at):
    assert at.sidebar.radio(key="screen").value == "1. データ読み込み"


def test_load_without_inputs_shows_error(at):
    """アップロードモード（既定）で何も入れずに読み込み → zip を促すエラー。
    パスモードではパス入力を促すエラー。"""
    at.button(key="load_btn").click().run()
    assert not at.exception
    assert any("アップロードしてください" in e.value for e in at.error)
    at.toggle(key="paths_mode").set_value(True).run()
    at.button(key="load_btn").click().run()
    assert not at.exception
    assert any("入力してください" in e.value for e in at.error)


def test_paths_hidden_without_dev_option(at, monkeypatch):
    """一般ユーザ向けの起動（--dev / SCORELIB_UI_DEV 無し）ではパス指定の
    トグル自体が存在せず、画面はアップロードのみ。"""
    monkeypatch.delenv("SCORELIB_UI_DEV", raising=False)
    at.run()
    assert not at.exception
    assert len(at.toggle) == 0
    at.button(key="load_btn").click().run()
    assert any("アップロードしてください" in e.value for e in at.error)


def test_load_bad_dir_shows_error(at):
    at.toggle(key="paths_mode").set_value(True).run()
    at.text_input(key="data_dir_input").set_value("no/such/dir")
    at.button(key="load_btn").click().run()
    assert not at.exception
    assert any("見つかりません" in e.value for e in at.error)


def test_load_and_create_part(at, data_dir_mini):
    _load_data(at, data_dir_mini)
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.button(key="add_part_btn").click().run()
    assert not at.exception
    sf = at.session_state["score_file"]
    assert [p["name"] for p in sf["score_parts"]] == ["part_1"]
    # 雛形は検証に通る → 画面に OK マーカーが出る
    assert any("検証" in s.value and "OK" in s.value for s in at.success)


def test_part_with_absent_type_shows_warning(at, data_dir_mini):
    """データに無い type のパーツ（別実験の config 由来）: パーツは残し、
    編集画面に警告を出す（黙って落とさない・テスト計算まで気づけない、の両方を回避）。"""
    _load_data(at, data_dir_mini)
    # config 読み込み相当: パーツ編集画面が描画される前に score_file に入っている
    at.session_state["score_file"]["score_parts"] = [
        {"_uid": "x1", "name": "p_gone", "type": "GONE", "order": [], "aggregations": {}},
    ]
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    assert not at.exception
    assert any("測定データがありません" in w.value for w in at.warning)
    # パーツ自体は残る（黙って落とさない）
    assert at.session_state["score_file"]["score_parts"][0]["type"] == "GONE"


def test_create_part_and_compute(at, data_dir_mini):
    _load_data(at, data_dir_mini)
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.button(key="add_part_btn").click().run()

    at.sidebar.radio(key="screen").set_value(SCREEN_TEST).run()
    at.session_state["score_file"]["expression"] = "part_1"
    at.text_input(key="test_dir").set_value(str(data_dir_mini))
    at.button(key="run_btn").click().run()
    assert not at.exception
    assert not at.error
    assert len(at.dataframe) == 1  # Score + part values table


def test_expression_insert_button_updates_input(at, data_dir_mini):
    """パーツ名ボタンは内部 dict だけでなく、**見えている** expression 入力欄も
    即座に更新すること（ウィジェット状態が value= を上書きするバグの回帰）。"""
    _load_data(at, data_dir_mini)
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.button(key="add_part_btn").click().run()
    at.sidebar.radio(key="screen").set_value(SCREEN_COMPOSE).run()
    at.button(key="ins_part_1").click().run()
    assert not at.exception
    assert at.session_state["score_file"]["expression"] == "part_1"
    assert at.text_input(key="expr_input").value == "part_1"


def test_part_reorder_buttons(at, data_dir_mini):
    _load_data(at, data_dir_mini)
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.button(key="add_part_btn").click().run()
    at.button(key="add_part_btn").click().run()
    names = [p["name"] for p in at.session_state["score_file"]["score_parts"]]
    assert names == ["part_1", "part_2"]
    # part_2 is selected (creation selects the new part); move it up
    up = [b for b in at.button if b.label == "▲ 上へ"]
    assert up, "parts up-button not rendered"
    up[0].click().run()
    assert not at.exception
    assert [p["name"] for p in at.session_state["score_file"]["score_parts"]] == ["part_2", "part_1"]


def test_load_warns_when_groups_exceed_axis_count(at, data_dir_mini, fixtures_dir):
    """config.jsonc は WL 20 までのグループを定義しているが、mini データの WL は
    0..5（6本）: 世代情報 json 無しでも、データ由来の本数で読み込み直後に警告が
    出ること（本数はデータから導出 — 世代情報の入力欄は廃止済み）。"""
    at.toggle(key="paths_mode").set_value(True).run()
    at.text_input(key="data_dir_input").set_value(str(data_dir_mini))
    at.text_input(key="config_path_input").set_value(str(fixtures_dir / "config.jsonc"))
    at.button(key="load_btn").click().run()
    assert not at.exception
    assert any("範囲外" in w.value for w in at.warning)


def test_config_wlgroup_imported_as_group_def(at, data_dir_mini, fixtures_dir):
    at.toggle(key="paths_mode").set_value(True).run()
    at.text_input(key="data_dir_input").set_value(str(data_dir_mini))
    at.text_input(key="config_path_input").set_value(str(fixtures_dir / "config.jsonc"))
    at.button(key="load_btn").click().run()
    assert not at.exception
    gd = at.session_state["score_file"]["groupDefs"]["WLgroup"]
    assert gd["axis"] == "WL"
    # 取り込み経路により list / tuple どちらもありうる（内部 dict の許容形）
    assert list(gd["groups"]["WLgroup01"]) == [0, 3]
    # グループ定義エディタがエラーなく描画されること
    at.sidebar.radio(key="screen").set_value(SCREEN_SETS).run()
    assert not at.exception


def test_duplicate_then_switch_keeps_parts_independent(at, data_dir_mini):
    """それまで欠けていた「文脈切り替え」の検証パターン: パーツBを編集 →
    Aへ切り替え → Aのデータと**表示ウィジェット**が無事なことを確認する。
    _uid を共有していた複製バグでは、コピー同士が全ウィジェットを共有し、
    Aの名前がBのものになり、Bで相対化を外すとAの相対化設定まで消えた。"""
    _load_data(at, data_dir_mini)
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.button(key="add_part_btn").click().run()
    uid0_pre = at.session_state["score_file"]["score_parts"][0]["_uid"]
    # v1 の雛形は相対化プリセット無し → 元パーツで明示的に ON にしてから複製
    at.checkbox(key=f"{uid0_pre}_rel_on").set_value(True).run()
    assert not at.exception
    assert at.session_state["score_file"]["score_parts"][0].get("relative") is not None
    dup = next(b for b in at.button if b.label == "このパーツを複製")
    dup.click().run()
    assert not at.exception
    parts = at.session_state["score_file"]["score_parts"]
    assert [p["name"] for p in parts] == ["part_1", "part_1_1"]
    uid0, uid1 = parts[0]["_uid"], parts[1]["_uid"]
    assert uid0 != uid1

    # 複製直後はコピー側が選択されている: **コピー側の**相対化をOFFにする
    at.checkbox(key=f"{uid1}_rel_on").set_value(False).run()
    assert not at.exception
    parts = at.session_state["score_file"]["score_parts"]
    assert parts[1].get("relative") is None
    assert parts[0].get("relative") is not None  # 元パーツは無傷

    # 元パーツへ切り替える: そのウィジェットが自分自身の値を表示すること。
    # 選択は uid のキー付き状態なので、同じ実行の開始時点から新しい選択が
    # 有効（ドラッグリストの ← 編集中 マーカーはこれに依存している）
    sel = next(s for s in at.selectbox if s.label == "編集するパーツ")
    sel.set_value(uid0).run()
    assert not at.exception
    assert at.session_state["part_sel"] == uid0
    assert at.session_state["selected_part"] == 0
    parts = at.session_state["score_file"]["score_parts"]
    assert [p["name"] for p in parts] == ["part_1", "part_1_1"]
    assert parts[0].get("relative") is not None
    assert at.text_input(key=f"{uid0}_name").value == "part_1"
    assert at.checkbox(key=f"{uid0}_rel_on").value is True
    assert not at.error


def test_relative_off_removes_explicit_step_via_ui(at, data_dir_mini):
    """相対化を ON にし、専用ボタンで __relative__ を order に置いてから OFF に
    する: パーツは検証OKのままであること（孤児ステップが残らない）。"""
    _load_data(at, data_dir_mini)
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.button(key="add_part_btn").click().run()
    uid = at.session_state["score_file"]["score_parts"][0]["_uid"]
    at.checkbox(key=f"{uid}_rel_on").set_value(True).run()
    assert at.session_state["score_file"]["score_parts"][0].get("relative") is not None
    at.button(key=f"{uid}_addfixed_btn").click().run()
    assert "__relative__" in at.session_state["score_file"]["score_parts"][0]["order"]
    at.checkbox(key=f"{uid}_rel_on").set_value(False).run()
    assert not at.exception
    part = at.session_state["score_file"]["score_parts"][0]
    assert part.get("relative") is None
    assert "__relative__" not in part["order"]
    assert not at.error


def test_dummy_expand_flow_end_to_end(at, tmp_path, data_dir_mini, dvtbudget_coef_path):
    """画面1のダミー展開 → 雛形作成 → 相対化ON（Measure 分割の既定値と
    labels 注記）→ テスト計算、まで一気通貫（docs/spec_change_dataname_measure.md
    プラン4の実体）。"""
    from scorelib_param.dummy import make_pseudo_dummy

    pseudo = make_pseudo_dummy(data_dir_mini, tmp_path / "pseudo")
    at.toggle(key="paths_mode").set_value(True).run()
    at.radio(key="data_mode").set_value("ダミー（測定前）").run()
    at.text_input(key="dummy_dir_input").set_value(str(pseudo))
    at.number_input(key="dummy_boards").set_value(2)
    at.text_input(key="dummy_chips").set_value("2,3")
    # ダミーでも係数を渡せる（実測パスモードと同じ2欄 — dVtBudget の構造テスト用）
    at.text_input(key="coef_path_input").set_value(str(dvtbudget_coef_path))
    at.button(key="load_btn").click().run()
    assert not at.exception
    ctx = at.session_state["context"]
    assert ctx["dummy_source"] == str(pseudo)
    assert ctx["catalogs"]["FBC"]["Board"] == [0, 1]
    assert ctx["catalogs"]["FBC"]["Measure"] == [0, 1, 2, 3]
    assert "dVtBudget" in ctx["part_types"]

    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.button(key="add_part_btn").click().run()
    assert not at.exception
    uid = at.session_state["score_file"]["score_parts"][0]["_uid"]
    at.checkbox(key=f"{uid}_rel_on").set_value(True).run()
    assert not at.exception
    part = at.session_state["score_file"]["score_parts"][0]
    assert part["relative"]["split_axis"] == "Measure"
    assert part["relative"]["numerator_when"] == 1
    # エディタ描画時に dataName の labels 注記が付く（6.1節）
    assert part["relative"]["labels"]["1"] == "evaluation_param_read_level_1"

    at.sidebar.radio(key="screen").set_value(SCREEN_TEST).run()
    assert any("数値に意味はありません" in w.value for w in at.warning)
    at.session_state["score_file"]["expression"] = "part_1"
    at.button(key="run_btn").click().run()
    assert not at.exception
    assert not at.error
    assert len(at.dataframe) == 1


def test_config_only_editing_flow(at, fixtures_dir):
    """設定のみ編集: 設定から導出した context で画面2（パーツ編集）が開き、
    画面5 のテスト計算はディレクトリ未入力の明確なエラーになる
    （file_uploader は AppTest から操作できないため、読み込み自体は
    state.load_config_only の単体テストでカバーし、ここでは注入する）。"""
    text = (fixtures_dir / "config.jsonc").read_text(encoding="utf-8")
    sf, ctx = state.load_config_only(text)
    at.session_state["score_file"] = sf
    at.session_state["context"] = ctx
    at.run()
    assert not at.exception
    assert any("設定のみ編集中" in i.value for i in at.info)

    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    assert not at.exception  # データ無しでもパーツ編集が開ける

    at.sidebar.radio(key="screen").set_value(SCREEN_TEST).run()
    assert not at.exception
    at.button(key="run_btn").click().run()
    assert any("データディレクトリを入力" in e.value for e in at.error)


def test_custom_part_end_to_end(at, data_dir_mini, fixtures_dir):
    """custom_parts.py 付きで読み込み、type=custom パーツを作成して計算する。"""
    at.toggle(key="paths_mode").set_value(True).run()
    at.text_input(key="data_dir_input").set_value(str(data_dir_mini))
    at.text_input(key="custom_path_input").set_value(str(fixtures_dir / "custom_parts.py"))
    at.button(key="load_btn").click().run()
    assert not at.exception
    assert "custom" in at.session_state["context"]["part_types"]

    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.selectbox(key="new_part_type").set_value("custom").run()
    at.button(key="add_part_btn").click().run()
    assert not at.exception
    part = at.session_state["score_file"]["score_parts"][0]
    assert part["type"] == "custom"
    assert part["function"] in at.session_state["context"]["custom_functions"]

    # 「関数」プルダウンで特定の関数を選ぶ
    uid = part["_uid"]
    at.selectbox(key=f"{uid}_func").set_value("fixed_value").run()
    assert at.session_state["score_file"]["score_parts"][0]["function"] == "fixed_value"

    at.sidebar.radio(key="screen").set_value(SCREEN_TEST).run()
    at.session_state["score_file"]["expression"] = "part_1"
    at.text_input(key="test_dir").set_value(str(data_dir_mini))
    at.button(key="run_btn").click().run()
    assert not at.exception
    assert not at.error
    assert len(at.dataframe) == 1


def test_undo_reverts_last_action(at, data_dir_mini):
    _load_data(at, data_dir_mini)
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.button(key="add_part_btn").click().run()
    assert len(at.session_state["score_file"]["score_parts"]) == 1
    at.button(key="undo_btn").click().run()
    assert not at.exception
    assert at.session_state["score_file"]["score_parts"] == []


def test_draft_autosaved_and_restored(at, data_dir_mini, dvtbudget_coef_path, tmp_path):
    """下書きは**名前ごと**に保存・復元される（共用サーバで他人と混ざらない）。
    名前未入力の間は自動保存されない。"""
    # 名前未入力のまま編集 → 保存されない
    at.toggle(key="paths_mode").set_value(True).run()
    at.text_input(key="data_dir_input").set_value(str(data_dir_mini))
    at.text_input(key="coef_path_input").set_value(str(dvtbudget_coef_path))
    at.button(key="load_btn").click().run()
    assert not at.exception
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.button(key="add_part_btn").click().run()
    assert not state.draft_path_for("taro").exists()

    # 名前を入れると以降の操作で保存される
    at.text_input(key="draft_user_input").set_value("taro").run()
    at.button(key="add_part_btn").click().run()
    assert state.draft_path_for("taro").exists()
    draft = state.load_draft(state.draft_path_for("taro"))
    assert [p["name"] for p in draft["score_file"]["score_parts"]] == ["part_1", "part_2"]
    assert draft["context_inputs"]["data_dir"] == str(data_dir_mini.resolve())
    assert draft["context_inputs"]["coef_path"] == str(dvtbudget_coef_path.resolve())

    # 別セッション: 名前を入れるまで復元は提案されず、同じ名前を入れると
    # 復元で score file・データ読み込みコンテキスト・パスモードの入力欄まで戻る
    apptest = pytest.importorskip("streamlit.testing.v1").AppTest
    at2 = apptest.from_file(APP, default_timeout=60)
    at2.run()
    assert all(b.key != "restore_btn" for b in at2.button)  # 名前未入力 → 提案なし
    at2.text_input(key="draft_user_input").set_value("taro").run()
    at2.button(key="restore_btn").click().run()
    assert not at2.exception
    assert [p["name"] for p in at2.session_state["score_file"]["score_parts"]] == ["part_1", "part_2"]
    assert at2.session_state["context"]["types"] == MINI_TYPES
    assert "dVtBudget" in at2.session_state["context"]["part_types"]
    at2.toggle(key="paths_mode").set_value(True).run()
    assert at2.text_input(key="data_dir_input").value == str(data_dir_mini.resolve())
    assert at2.text_input(key="coef_path_input").value == str(dvtbudget_coef_path.resolve())

    # 別の名前では他人の下書きは提案されない
    at3 = apptest.from_file(APP, default_timeout=60)
    at3.run()
    at3.text_input(key="draft_user_input").set_value("jiro").run()
    assert all(b.key != "restore_btn" for b in at3.button)
