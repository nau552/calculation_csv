# Copyright (c) 2026
"""Streamlit AppTest によるE2Eテスト。

アプリをブラウザなしで起動し、データ読み込み → 雛形からパーツ作成 →
テスト計算までを一気通貫で検証する。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from ui import state, widgets

if TYPE_CHECKING:
    from streamlit.testing.v1 import AppTest
    from streamlit.testing.v1.element_tree import Selectbox

APP = str(Path(__file__).resolve().parent.parent / "ui" / "app.py")
MINI_TYPES = ["FBC", "KLD", "PROGLOOP", "PROGSTATUS", "dVthSGWLD", "tPROG", "tR"]
SCREEN_PARTS = "2. スコアパーツ編集"
SCREEN_SETS = "3. 選択セット・グループ定義"
SCREEN_COMPOSE = "4. スコア合成・制約"
SCREEN_TEST = "5. テスト実行・エクスポート"


@pytest.fixture
def at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    """AppTest でアプリを起動して返す。

    Returns:
        初回 run を終えた(例外なしを確認済みの)AppTest インスタンス。

    """
    apptest = pytest.importorskip("streamlit.testing.v1").AppTest
    # ユーザの実際の下書きファイルに触れないよう保存先を差し替える
    monkeypatch.setattr(state, "DRAFTS_DIR", tmp_path / "drafts")
    # AppTest は file_uploader を操作できないため、開発者モード
    # (パス指定トグル)でテストする。一般ユーザ表示のテストは
    # test_paths_hidden_without_dev_option が env を消して確認する
    monkeypatch.setenv("SCORELIB_UI_DEV", "1")
    t = apptest.from_file(APP, default_timeout=60)
    t.run()
    assert not t.exception
    return t


def _part_selector(at: AppTest) -> Selectbox:
    """「編集するパーツ」欄の selectbox を返す。

    ラベルが変わるとキーごと再マウントされる(part_sel_<hash> —
    streamlit#11268 対策)ため、固定キーでなく前方一致で探す。

    Returns:
        AppTest の Selectbox 要素。

    """
    return next(s for s in at.selectbox if str(s.key).startswith("part_sel"))


def _ek(at: AppTest, name: str) -> str:
    """「元に戻す」の世代キー(app._wk)のテスト側対応。

    Returns:
        世代0なら name、以降は "name@世代番号"。

    """
    epoch = at.session_state["widget_epoch"]
    return name if epoch == 0 else f"{name}@{epoch}"


def _load_data(at: AppTest, data_dir: Path) -> None:
    # アップロードは AppTest から操作できないため「サーバ上のパスで指定」を使う
    at.toggle(key="paths_mode").set_value(True).run()
    at.text_input(key="data_dir_input").set_value(str(data_dir))
    at.button(key="load_btn").click().run()
    assert not at.exception
    assert at.session_state["context"]["types"] == MINI_TYPES


def _create_part(at: AppTest) -> str:
    # 画面2で雛形パーツを1つ作成し、その _uid を返す(配線テストの共通前段)
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.button(key="add_part_btn").click().run()
    assert not at.exception
    return at.session_state["score_file"]["score_parts"][-1]["_uid"]


def test_app_starts(at: AppTest) -> None:
    """起動直後はデータ読み込み画面が選択されていることを検証する。"""
    assert at.sidebar.radio(key="screen").value == "1. データ読み込み"


def test_load_without_inputs_shows_error(at: AppTest) -> None:
    """アップロードモード(既定)で何も入れずに読み込み → zip を促すエラー。

    パスモードではパス入力を促すエラー。
    """
    at.button(key="load_btn").click().run()
    assert not at.exception
    assert any("アップロードしてください" in e.value for e in at.error)
    at.toggle(key="paths_mode").set_value(True).run()
    at.button(key="load_btn").click().run()
    assert not at.exception
    assert any("入力してください" in e.value for e in at.error)


def test_paths_hidden_without_dev_option(at: AppTest, monkeypatch: pytest.MonkeyPatch) -> None:
    """一般ユーザ向けの起動ではパス指定が出ないことを検証する。

    --dev / SCORELIB_UI_DEV 無しではパス指定のトグル自体が存在せず、
    画面はアップロードのみ。
    """
    monkeypatch.delenv("SCORELIB_UI_DEV", raising=False)
    at.run()
    assert not at.exception
    assert len(at.toggle) == 0
    at.button(key="load_btn").click().run()
    assert any("アップロードしてください" in e.value for e in at.error)


def test_load_bad_dir_shows_error(at: AppTest) -> None:
    """存在しないディレクトリの読み込みがエラー表示になることを検証する。"""
    at.toggle(key="paths_mode").set_value(True).run()
    at.text_input(key="data_dir_input").set_value("no/such/dir")
    at.button(key="load_btn").click().run()
    assert not at.exception
    assert any("見つかりません" in e.value for e in at.error)


def test_load_and_create_part(at: AppTest, data_dir_mini: Path) -> None:
    """データ読み込み後に雛形からパーツを作成できることを検証する。"""
    _load_data(at, data_dir_mini)
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.button(key="add_part_btn").click().run()
    assert not at.exception
    sf = at.session_state["score_file"]
    assert [p["name"] for p in sf["score_parts"]] == ["part_1"]
    # 雛形は問題なく通る → サイドバーとパーツ編集画面に「問題なし」が出る
    assert any("問題なし" in s.value for s in at.success)


def test_part_with_absent_type_shows_warning(at: AppTest, data_dir_mini: Path) -> None:
    """データに無い type のパーツ(別実験の config 由来)の扱いを検証する。

    パーツは残し、編集画面に警告を出す(黙って落とさない・テスト計算まで
    気づけない、の両方を回避)。
    """
    _load_data(at, data_dir_mini)
    # config 読み込み相当: パーツ編集画面が描画される前に score_file に入っている
    at.session_state["score_file"]["score_parts"] = [
        {"_uid": "x1", "name": "p_gone", "type": "GONE", "order": [], "aggregations": {}},
    ]
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    assert not at.exception
    assert any("測定データがありません" in w.value for w in at.warning)
    # パーツ自体は残る(黙って落とさない)
    assert at.session_state["score_file"]["score_parts"][0]["type"] == "GONE"


def test_part_value_mismatch_visible_and_value_preserved(
    at: AppTest, data_dir_mini: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """データに無い値で filter するパーツの扱いを検証する(ユーザー報告のシナリオ)。

    従来は (1) 編集対象に選ぶまで一覧に ⚠ が出ない、(2) エディタを開くと候補に
    無い値が黙って消える、(3) テスト計算まで走ってから原因の分からないエラーに
    なる、の三重で原因に辿り着けなかった。3点とも固定する(計算前ガードの
    全メッセージ列挙は 2026-08-01 の一本化合意。エンジン側の名指しは
    test_cli.py が担保)。
    """
    from ui import widgets

    monkeypatch.setattr(widgets, "HAS_SORTABLES", False)  # 一覧の表(検証列)を読むため
    _load_data(at, data_dir_mini)
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.button(key="add_part_btn").click().run()  # 1つ目: 正常パーツ(こちらが選択される)
    tail = ["WL", "STR", "State", "Board", "Chip", "Block"]
    bad = {
        "_uid": "x2",
        "name": "p_bad",
        "type": "FBC",
        "order": ["Read_Label", *tail],
        "aggregations": {
            "Read_Label": {"op": "filter", "value": "upper1"},
            **{a: {"op": "mean"} for a in tail},
        },
    }
    at.session_state["score_file"]["score_parts"].append(bad)
    at.run()
    assert not at.exception
    # (1) 編集対象に選ばなくても一覧に「データに無い値」が出る(選択中は1つ目のパーツ)
    rows = at.dataframe[0].value
    assert list(rows["状態"]) == ["OK", "⚠ データに無い値"]
    # サイドバーは「設定の誤り」に一本化(誤りがあるので「問題なし」は出ない)
    assert any("設定の誤り 1 件" in e.value for e in at.error)
    assert not any("問題なし" in s.value for s in at.sidebar.success)
    # (2) 編集対象に選ぶと警告が出て、候補に無い値は保持される(黙って消えない)
    _part_selector(at).set_value("x2").run()
    assert any("データにありません" in w.value for w in at.warning)
    assert at.session_state["score_file"]["score_parts"][1]["aggregations"]["Read_Label"]["value"] == "upper1"
    # (3) テスト計算は誤りがあるうちは実行されず、原因がパーツ名つきで列挙される
    at.sidebar.radio(key="screen").set_value(SCREEN_TEST).run()
    at.text_input(key="test_dir").set_value(str(data_dir_mini))
    at.button(key="run_btn").click().run()
    assert any("設定に誤りがあるため実行できません" in e.value for e in at.error)
    assert any("p_bad" in e.value and "upper1" in e.value for e in at.error)


def test_render_never_mutates_score_file(at: AppTest, data_dir_mini_no_override_true: Path) -> None:
    """不変条件: 画面を開く・パーツ/エントリを選ぶだけでは設定は一切変わらない。

    実機報告(2026-08-01)「エディタを描画しただけで相対化の分子が False に
    書き換わる」の一般化。書き戻し系ウィジェットが候補外の既存値を差し替える/
    落とすバグのクラス全体をこの1本で捕まえる。データは評価側
    (Read_Override=True)の測定が無い形にし、候補に無い値・存在しない参照を
    意図的に詰めた「全部盛り」設定を使う。**エディタや設定の形を増やしたら
    この設定にも足すこと**(testing_guide 参照)。
    """
    import copy

    from ui import state

    _load_data(at, data_dir_mini_no_override_true)
    tail = {a: {"op": "mean"} for a in ["WL", "STR", "Board", "Chip", "Block"]}
    rel = {
        "split_axis": "Read_Override",
        "numerator_when": True,  # データに無い(候補は [False] のみ)
        "denominator_when": False,
        "mode": "ratio",
        "denominator_offset": 1.0,
    }
    measure_spec = {"op": "filter", "value": 1}
    state.annotate_measure_labels(measure_spec, at.session_state["context"]["measure_labels"]["FBC"])
    parts = [
        {  # 相対化(分子がデータに無し)+分母事前集計
            "_uid": "p1",
            "name": "rel_full",
            "type": "FBC",
            "relative": {**rel, "denominator_pre_aggregation": [{"axis": "WL", "op": "mean"}]},
            "order": ["State", "WL", "STR", "Board", "Chip", "Block"],
            "aggregations": {"State": {"op": "filter", "value": "R2A"}, **copy.deepcopy(tail)},
        },
        {  # 空の事前集計キーが描画で消えないこと
            "_uid": "p2",
            "name": "rel_empty_pre",
            "type": "FBC",
            "relative": {**rel, "denominator_pre_aggregation": []},
            "order": ["WL", "STR", "Board", "Chip", "Block"],
            "aggregations": copy.deepcopy(tail),
        },
        {  # データに無い filter 値・diff 複合軸(片側データに無し)・存在しない重みセット参照・
            # 候補に無いキーを含む重み辞書・by つき変換(候補外キー入り)
            "_uid": "p3",
            "name": "mixed",
            "type": "FBC",
            "order": ["Read_Label", "State&Read_Label", "__offset__", "WL", "STR", "Board", "Chip", "Block"],
            "aggregations": {
                "Read_Label": {"op": "filter", "value": "upper1"},
                "State&Read_Label": {
                    "op": "diff",
                    "value": [
                        {"State": "R2A", "Read_Label": "upper1"},
                        {"State": "A2R", "Read_Label": "read_level_lower1"},
                    ],
                },
                "__offset__": {"op": "mul", "value": {"g1": 2.0, "gx": 3.0}, "by": "WLG"},
                "WL": {"op": "mean", "weight_ref": "Wnope"},
                "STR": {"op": "mean", "weight": {0: 0.5, 99: 2.0}},
                "Board": {"op": "mean"},
                "Chip": {"op": "mean"},
                "Block": {"op": "mean"},
            },
        },
        {  # expr op と Measure filter(labels は正規の注記を事前付与)
            "_uid": "p4",
            "name": "exprpart",
            "type": "FBC",
            "order": ["Measure", "State", "WL", "STR", "Board", "Chip", "Block"],
            "aggregations": {
                "Measure": measure_spec,
                "State": {"op": "expr", "expr": "by['R2A']"},
                **copy.deepcopy(tail),
            },
        },
        {  # 読み込まれていない custom 関数と params
            "_uid": "p5",
            "name": "custompart",
            "type": "custom",
            "function": "nope_func",
            "params": {"a": 1.5},
        },
        {  # 存在しない選択セット参照(実在セット S1 と並ぶ)
            "_uid": "p6",
            "name": "refpart",
            "type": "FBC",
            "order": ["State", "WL", "STR", "Board", "Chip", "Block"],
            "aggregations": {"State": {"op": "diff", "ref": "Snope"}, **copy.deepcopy(tail)},
        },
    ]
    sf = at.session_state["score_file"]
    sf["score_parts"] = parts
    sf["selectionSets"]["S1"] = ["R2A", "A2R"]
    sf["groupDefs"]["WLG"] = {"axis": "WL", "definedInLogical": True, "groups": {"g1": [0, 2]}}
    sf["weightSets"] = {"W1": {"g1": 1.0}}
    sf["expression"] = "rel_full + mixed"
    sf["constraintThreshold"] = {"rel_full": {"value": 1.0}}
    at.run()
    assert not at.exception
    before = copy.deepcopy(at.session_state["score_file"])

    screens = at.sidebar.radio(key="screen").options
    for s in screens[1:]:
        at.sidebar.radio(key="screen").set_value(s).run()
        assert not at.exception, s
    at.sidebar.radio(key="screen").set_value(screens[1]).run()
    for p in parts:
        _part_selector(at).set_value(p["_uid"]).run()
        assert not at.exception, p["name"]
        entry_sel = [s for s in at.selectbox if s.key == f"{p['_uid']}_sel_entry"]
        for entry in entry_sel[0].options if entry_sel else []:
            at.selectbox(key=f"{p['_uid']}_sel_entry").set_value(entry).run()
            assert not at.exception, (p["name"], entry)

    assert at.session_state["score_file"] == before


def test_create_part_and_compute(at: AppTest, data_dir_mini: Path) -> None:
    """パーツ作成からテスト計算までの流れを検証する。"""
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


def test_expression_insert_button_updates_input(at: AppTest, data_dir_mini: Path) -> None:
    """パーツ名ボタンが expression 入力欄を即座に更新することを検証する。

    内部 dict だけでなく**見えている**入力欄も即座に更新すること
    (ウィジェット状態が value= を上書きするバグの回帰)。
    """
    _load_data(at, data_dir_mini)
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.button(key="add_part_btn").click().run()
    at.sidebar.radio(key="screen").set_value(SCREEN_COMPOSE).run()
    at.button(key="ins_part_1").click().run()
    assert not at.exception
    assert at.session_state["score_file"]["expression"] == "part_1"
    assert at.text_input(key="expr_input").value == "part_1"


def test_part_rename_updates_selector(at: AppTest, data_dir_mini: Path) -> None:
    """パーツ改名が「編集するパーツ」欄に追随することを検証する(実機報告 2026-08-01)。

    キー付き selectbox は選択中のラベルだけが変わると表示が更新されない
    (streamlit#11268。D&D 一覧は既に再マウント対策済みで追随していた)ため、
    ラベルが変わったらキーごと再マウントする。再マウント(キーの変化)・
    新ラベルの選択肢・選択の保持の3点を固定する。
    """
    _load_data(at, data_dir_mini)
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.button(key="add_part_btn").click().run()
    at.button(key="add_part_btn").click().run()
    uid0 = at.session_state["score_file"]["score_parts"][0]["_uid"]
    _part_selector(at).set_value(uid0).run()
    sel = _part_selector(at)
    key_before = sel.key
    assert "1. part_1" in sel.options

    at.text_input(key=f"{uid0}_name").set_value("renamed_part").run()
    sel = _part_selector(at)
    assert sel.key != key_before  # ラベル変更でキーごと再マウントされている
    assert any("renamed_part" in str(o) for o in sel.options)
    assert sel.value == uid0  # 選択は保持される
    assert at.session_state["part_sel"] == uid0


def test_part_reorder_buttons(at: AppTest, data_dir_mini: Path) -> None:
    """パーツの並べ替えボタンで順序が入れ替わることを検証する。"""
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


def test_load_warns_when_groups_exceed_axis_count(at: AppTest, data_dir_mini: Path, fixtures_dir: Path) -> None:
    """グループが WL 本数を超えると読み込み直後に警告が出ることを検証する。

    config.jsonc は WL 20 までのグループを定義しているが、mini データの WL は
    0..5(6本): 世代情報 json 無しでも、データ由来の本数で読み込み直後に警告が
    出ること(本数はデータから導出 — 世代情報の入力欄は廃止済み)。
    """
    at.toggle(key="paths_mode").set_value(True).run()
    at.text_input(key="data_dir_input").set_value(str(data_dir_mini))
    at.text_input(key="config_path_input").set_value(str(fixtures_dir / "config.jsonc"))
    at.button(key="load_btn").click().run()
    assert not at.exception
    assert any("範囲外" in w.value for w in at.warning)


def test_config_wlgroup_imported_as_group_def(at: AppTest, data_dir_mini: Path, fixtures_dir: Path) -> None:
    """読み込んだ config の WLgroup がグループ定義として取り込まれることを検証する。"""
    at.toggle(key="paths_mode").set_value(True).run()
    at.text_input(key="data_dir_input").set_value(str(data_dir_mini))
    at.text_input(key="config_path_input").set_value(str(fixtures_dir / "config.jsonc"))
    at.button(key="load_btn").click().run()
    assert not at.exception
    gd = at.session_state["score_file"]["groupDefs"]["WLgroup"]
    assert gd["axis"] == "WL"
    # 取り込み経路により list / tuple どちらもありうる(内部 dict の許容形)
    assert list(gd["groups"]["WLgroup01"]) == [0, 3]
    # グループ定義エディタがエラーなく描画されること
    at.sidebar.radio(key="screen").set_value(SCREEN_SETS).run()
    assert not at.exception


def test_duplicate_then_switch_keeps_parts_independent(at: AppTest, data_dir_mini: Path) -> None:
    """それまで欠けていた「文脈切り替え」の検証パターン。

    パーツBを編集 → Aへ切り替え → Aのデータと**表示ウィジェット**が無事な
    ことを確認する。_uid を共有していた複製バグでは、コピー同士が全ウィジェットを
    共有し、Aの名前がBのものになり、Bで相対化を外すとAの相対化設定まで消えた。
    """
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
    # 有効(ドラッグリストの ← 編集中 マーカーはこれに依存している)
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


def test_relative_off_removes_explicit_step_via_ui(at: AppTest, data_dir_mini: Path) -> None:
    """相対化 OFF で孤児ステップが残らないことを検証する。

    相対化を ON にし、専用ボタンで __relative__ を order に置いてから OFF に
    する: パーツは検証OKのままであること(孤児ステップが残らない)。
    """
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


def test_dummy_expand_flow_end_to_end(
    at: AppTest, tmp_path: Path, data_dir_mini: Path, dvtbudget_coef_path: Path
) -> None:
    """ダミー展開からテスト計算までを一気通貫で検証する。

    画面1のダミー展開 → 雛形作成 → 相対化ON(Measure 分割の既定値と
    labels 注記)→ テスト計算、まで一気通貫(docs/spec_change_dataname_measure.md
    プラン4の実体)。
    """
    from scorelib_param.dummy import make_pseudo_dummy

    pseudo = make_pseudo_dummy(data_dir_mini, tmp_path / "pseudo")
    at.toggle(key="paths_mode").set_value(True).run()
    at.radio(key="data_mode").set_value("ダミー(測定前)").run()
    at.text_input(key="dummy_dir_input").set_value(str(pseudo))
    at.number_input(key="dummy_boards").set_value(2)
    at.text_input(key="dummy_chips").set_value("2,3")
    # ダミーでも係数を渡せる(実測パスモードと同じ2欄 — dVtBudget の構造テスト用)
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
    # エディタ描画時に dataName の labels 注記が付く(6.1節)
    assert part["relative"]["labels"]["1"] == "evaluation_param_read_level_1"

    at.sidebar.radio(key="screen").set_value(SCREEN_TEST).run()
    assert any("数値に意味はありません" in w.value for w in at.warning)
    at.session_state["score_file"]["expression"] = "part_1"
    at.button(key="run_btn").click().run()
    assert not at.exception
    assert not at.error
    assert len(at.dataframe) == 1


def test_config_only_editing_flow(at: AppTest, fixtures_dir: Path) -> None:
    """設定のみ編集のフローを検証する。

    設定から導出した context で画面2(パーツ編集)が開き、画面5 のテスト計算は
    ディレクトリ未入力の明確なエラーになる(file_uploader は AppTest から
    操作できないため、読み込み自体は state.load_config_only の単体テストで
    カバーし、ここでは注入する)。
    """
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


def test_custom_part_end_to_end(at: AppTest, data_dir_mini: Path, fixtures_dir: Path) -> None:
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


def test_undo_reverts_last_action(at: AppTest, data_dir_mini: Path) -> None:
    """「元に戻す」(undo)が直前の操作を取り消すことを検証する。"""
    _load_data(at, data_dir_mini)
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.button(key="add_part_btn").click().run()
    assert len(at.session_state["score_file"]["score_parts"]) == 1
    at.button(key="undo_btn").click().run()
    assert not at.exception
    assert at.session_state["score_file"]["score_parts"] == []


def test_undo_restores_display_and_location(at: AppTest, data_dir_mini: Path) -> None:
    """「元に戻す」が表示・場所・データを全部戻すことを検証する(実機報告 2026-08-01)。

    旧実装は設定データだけ戻していた: キー固定のウィジェットはブラウザ側の
    表示が残り(改名した入力欄が古い文字列のまま等)、別の画面/パーツを見て
    いると取り消しが見えなかった。世代キー(_wk)でエディタを作り直し、編集
    していた場所へ跳ぶ。表示は常に score_file から再生成されるため、見た目と
    計算・エクスポートは食い違わない(エクスポート一致までここで固定)。
    """
    _load_data(at, data_dir_mini)
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.button(key="add_part_btn").click().run()
    uid = at.session_state["score_file"]["score_parts"][0]["_uid"]
    at.text_input(key=f"{uid}_name").set_value("renamed").run()
    # 別のパーツを足し、別の画面へ移動してから戻す
    at.button(key="add_part_btn").click().run()
    at.sidebar.radio(key="screen").set_value(SCREEN_COMPOSE).run()

    at.button(key="undo_btn").click().run()  # パーツ追加の取り消し(編集していた画面2へ跳ぶ)
    assert not at.exception
    assert at.sidebar.radio(key="screen").value == SCREEN_PARTS
    assert len(at.session_state["score_file"]["score_parts"]) == 1

    at.button(key="undo_btn").click().run()  # 改名の取り消し
    sf = at.session_state["score_file"]
    assert sf["score_parts"][0]["name"] == "part_1"
    assert at.session_state["widget_epoch"] == 2  # undo のたびにエディタが作り直される
    name_input = at.text_input(key=_ek(at, f"{uid}_name"))
    assert name_input.value == "part_1"  # 表示 = データ
    assert '"part_1"' in state.score_file_to_jsonc(sf)  # 表示 = エクスポート jsonc
    assert "renamed" not in state.score_file_to_jsonc(sf)


def test_undo_restores_relative_selectbox_display(at: AppTest, data_dir_mini_no_override_true: Path) -> None:
    """相対化の分子を変えて元に戻したとき、プルダウン表示も設定と一致して戻ることを検証する。

    実機報告: True を False に変えて undo すると「True はデータに無い」の警告は
    出るのにプルダウンは False のまま、という表示とデータの食い違いが起きていた。
    """
    _load_data(at, data_dir_mini_no_override_true)
    rel_part = {
        "_uid": "r1",
        "name": "rel1",
        "type": "FBC",
        "relative": {"split_axis": "Read_Override", "numerator_when": True, "denominator_when": False},
        "order": ["WL", "STR", "Board", "Chip", "Block"],
        "aggregations": {a: {"op": "mean"} for a in ["WL", "STR", "Board", "Chip", "Block"]},
    }
    at.session_state["score_file"]["score_parts"] = [rel_part]
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.selectbox(key=f"{rel_part['_uid']}_rel_num").set_value(False).run()
    assert at.session_state["score_file"]["score_parts"][0]["relative"]["numerator_when"] is False

    at.button(key="undo_btn").click().run()
    sf_rel = at.session_state["score_file"]["score_parts"][0]["relative"]
    assert sf_rel["numerator_when"] is True  # データが戻る
    # 世代サフィックスは相対化エディタのキー接頭辞(f"{uid}_rel")側に付く
    sel = at.selectbox(key=f"{_ek(at, f'{rel_part["_uid"]}_rel')}_num")
    assert sel.value is True  # プルダウン表示も戻る(表示 = データ)
    assert any("データにありません" in w.value for w in at.warning)  # 警告と表示が矛盾しない


def test_draft_autosaved_and_restored(
    at: AppTest, data_dir_mini: Path, dvtbudget_coef_path: Path, tmp_path: Path
) -> None:
    """下書きは**名前ごと**に保存・復元される(共用サーバで他人と混ざらない)。

    名前未入力の間は自動保存されない。
    """
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
    draft = cast("dict[str, Any]", state.load_draft(state.draft_path_for("taro")))
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


# 以降は UI 分割リファクタリングの安全網: 分割対象の画面・エディタの
# 「ウィジェット操作 → score_file 反映」の配線を AppTest で固定する。
# state 関数単体の挙動は tests/test_ui_state.py が持つので、ここでは
# 画面から呼ばれる経路(キー・表示・エラー文)だけを検証する。


def test_add_axis_entry_with_default_agg_and_delete(at: AppTest, data_dir_mini: Path) -> None:
    """エントリの追加 → 既定の集計spec → 削除、の配線を検証する。

    軸を選んで「追加」すると order の末尾に既定 op(カテゴリ軸は filter)付きで
    入り、使用済みの軸は追加候補から消える。エディタの「削除」で order と
    aggregations の両方から消える。
    """
    _load_data(at, data_dir_mini)
    uid = _create_part(at)
    at.selectbox(key=f"{uid}_addax").set_value("Read_Label").run()
    at.button(key=f"{uid}_addax_btn").click().run()
    assert not at.exception
    part = at.session_state["score_file"]["score_parts"][0]
    assert part["order"][-1] == "Read_Label"
    assert part["aggregations"]["Read_Label"] == {"op": "filter", "value": "read_level_upper1"}
    # 使用済みになった軸は追加候補から消える
    assert "Read_Label" not in at.selectbox(key=f"{uid}_addax").options
    # 「編集するエントリ」で選択 → エディタ内の削除ボタン
    at.selectbox(key=f"{uid}_sel_entry").set_value("Read_Label").run()
    at.button(key=f"{uid}_ed_del").click().run()
    assert not at.exception
    part = at.session_state["score_file"]["score_parts"][0]
    assert "Read_Label" not in part["order"]
    assert "Read_Label" not in part["aggregations"]


def test_add_combined_axis_entry(at: AppTest, data_dir_mini: Path) -> None:
    """複合軸(束ねて追加)の配線を検証する。

    複数選択して「束ねて追加」すると '&' 連結のエントリが op=sum で入り、
    束ねた軸は個別軸としても追加候補から消える。
    """
    _load_data(at, data_dir_mini)
    uid = _create_part(at)
    at.multiselect(key=f"{uid}_addcombo").set_value(["Erase_Override", "Program_Override"]).run()
    at.button(key=f"{uid}_addcombo_btn").click().run()
    assert not at.exception
    part = at.session_state["score_file"]["score_parts"][0]
    assert part["order"][-1] == "Erase_Override&Program_Override"
    assert part["aggregations"]["Erase_Override&Program_Override"] == {"op": "sum"}
    assert "Erase_Override" not in at.selectbox(key=f"{uid}_addax").options


def test_agg_op_change_cleans_spec_and_value_selection(at: AppTest, data_dir_mini: Path) -> None:
    """集計エディタの op 変更と対象選択の配線を検証する。

    State の filter → max で op 固有フィールド(value)が掃除され、
    「値を選択」モードの multiselect が spec["value"] に反映される。
    """
    _load_data(at, data_dir_mini)
    uid = _create_part(at)
    at.selectbox(key=f"{uid}_sel_entry").set_value("State").run()
    at.selectbox(key=f"{uid}_State_op").set_value("max").run()
    assert not at.exception
    part = at.session_state["score_file"]["score_parts"][0]
    assert part["aggregations"]["State"] == {"op": "max"}
    at.radio(key=f"{uid}_State_mode").set_value("値を選択").run()
    at.multiselect(key=f"{uid}_State_mv").set_value(["R2A", "A2B"]).run()
    assert not at.exception
    part = at.session_state["score_file"]["score_parts"][0]
    assert part["aggregations"]["State"] == {"op": "max", "value": ["R2A", "A2B"]}


def test_transform_step_add_and_constant_edit(at: AppTest, data_dir_mini: Path) -> None:
    """変換ステップの追加と定数編集の配線を検証する。

    「+ 変換ステップを追加」で __offset__(add, 0)が order に入り、
    変換op同士の切り替え(add → mul)では値を保ったまま演算だけ変わる。
    """
    _load_data(at, data_dir_mini)
    uid = _create_part(at)
    at.button(key=f"{uid}_addvirt_btn").click().run()
    assert not at.exception
    part = at.session_state["score_file"]["score_parts"][0]
    assert part["order"][-1] == "__offset__"
    assert part["aggregations"]["__offset__"] == {"op": "add", "value": 0}
    at.selectbox(key=f"{uid}_sel_entry").set_value("__offset__").run()
    at.selectbox(key=f"{uid}___offset___op").set_value("mul").run()
    assert not at.exception
    at.number_input(key=f"{uid}___offset___tv").set_value(2.5).run()
    assert not at.exception
    part = at.session_state["score_file"]["score_parts"][0]
    assert part["aggregations"]["__offset__"] == {"op": "mul", "value": 2.5}


def test_transform_step_per_axis_values(at: AppTest, data_dir_mini: Path) -> None:
    """変換ステップの「軸の値ごと(重み)」入力の配線を検証する。

    by 軸を選ぶと軸の値ごとの数値欄が出て、spec に by と値の辞書が入る。
    """
    _load_data(at, data_dir_mini)
    uid = _create_part(at)
    at.button(key=f"{uid}_addvirt_btn").click().run()
    at.selectbox(key=f"{uid}_sel_entry").set_value("__offset__").run()
    at.radio(key=f"{uid}___offset___tmode").set_value("軸の値ごと(重み)").run()
    assert not at.exception
    at.selectbox(key=f"{uid}___offset___tby").set_value("State").run()
    assert not at.exception
    at.number_input(key=f"{uid}___offset___tw0").set_value(2.0).run()
    assert not at.exception
    part = at.session_state["score_file"]["score_parts"][0]
    assert part["aggregations"]["__offset__"] == {
        "op": "add",
        "by": "State",
        "value": {"R2A": 2.0, "A2R": 0.0, "A2B": 0.0, "B2A": 0.0},
    }


def test_agg_weight_constant_per_value_and_off(at: AppTest, data_dir_mini: Path) -> None:
    """集計時重みの入力モード切替の配線を検証する。

    「定数1つ」で weight がスカラー、「値ごとに入力」で軸の値ごとの辞書になり、
    「なし」に戻すと weight が消える。
    """
    _load_data(at, data_dir_mini)
    uid = _create_part(at)
    at.selectbox(key=f"{uid}_sel_entry").set_value("WL").run()
    at.radio(key=f"{uid}_WL_wmode").set_value("定数1つ").run()
    at.number_input(key=f"{uid}_WL_wc").set_value(0.5).run()
    assert not at.exception
    part = at.session_state["score_file"]["score_parts"][0]
    assert part["aggregations"]["WL"] == {"op": "mean", "weight": 0.5}
    at.radio(key=f"{uid}_WL_wmode").set_value("値ごとに入力").run()
    at.number_input(key=f"{uid}_WL_wv0").set_value(2.0).run()
    assert not at.exception
    part = at.session_state["score_file"]["score_parts"][0]
    assert part["aggregations"]["WL"]["weight"] == {0: 2.0, 1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0, 5: 1.0}
    at.radio(key=f"{uid}_WL_wmode").set_value("なし").run()
    assert not at.exception
    assert at.session_state["score_file"]["score_parts"][0]["aggregations"]["WL"] == {"op": "mean"}


def test_agg_weight_ref_from_group_def_weight_set(at: AppTest, data_dir_mini: Path) -> None:
    """グループ定義の重みセットを集計時重みが参照する配線を検証する。

    画面3のグループ定義で作った重みセット(WLgWeight)が、画面2の
    「重みセット(ref)」の候補に現れ、weight_ref として保存される。
    """
    _load_data(at, data_dir_mini)
    uid = _create_part(at)
    at.sidebar.radio(key="screen").set_value(SCREEN_SETS).run()
    at.text_input(key="new_gdef_name").set_value("WLg")
    at.selectbox(key="new_gdef_axis").set_value("WL")
    at.button(key="new_gdef_btn").click().run()
    assert not at.exception
    at.checkbox(key="gdef_WLg_w_on").set_value(True).run()
    assert not at.exception
    assert at.session_state["score_file"]["weightSets"] == {"WLgWeight": 1.0}
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.selectbox(key=f"{uid}_sel_entry").set_value("WL").run()
    at.radio(key=f"{uid}_WL_wmode").set_value("重みセット(ref)").run()
    assert not at.exception
    part = at.session_state["score_file"]["score_parts"][0]
    assert part["aggregations"]["WL"] == {"op": "mean", "weight_ref": "WLgWeight"}


def test_relative_split_axis_change_via_ui(at: AppTest, data_dir_mini: Path) -> None:
    """相対化エディタの split 軸変更・mode 変更の画面配線を検証する。

    Measure → State に変えると旧軸が既定 op つきで order へ戻り、新軸が order
    から外れ、分子/分母は新軸の候補で初期化し直され、labels 注記は捨てられる。
    mode を diff にすると offset が消える。
    """
    _load_data(at, data_dir_mini)
    uid = _create_part(at)
    at.checkbox(key=f"{uid}_rel_on").set_value(True).run()
    assert not at.exception
    part = at.session_state["score_file"]["score_parts"][0]
    assert part["relative"]["split_axis"] == "Measure"
    assert "Measure" not in part["order"]
    assert part["relative"]["labels"]["1"] == "evaluation_param_read_level_1"
    at.selectbox(key=f"{uid}_rel_sa").set_value("State").run()
    assert not at.exception
    part = at.session_state["score_file"]["score_parts"][0]
    rel = part["relative"]
    assert rel["split_axis"] == "State"
    assert (rel["numerator_when"], rel["denominator_when"]) == ("A2R", "R2A")
    assert "labels" not in rel
    assert "State" not in part["order"]
    assert part["order"][-1] == "Measure"
    assert part["aggregations"]["Measure"] == {"op": "filter", "value": 0}
    at.selectbox(key=f"{uid}_rel_mode").set_value("diff").run()
    assert not at.exception
    rel = at.session_state["score_file"]["score_parts"][0]["relative"]
    assert rel["mode"] == "diff"
    assert "denominator_offset" not in rel
    at.selectbox(key=f"{uid}_rel_num").set_value("A2B").run()
    assert not at.exception
    assert at.session_state["score_file"]["score_parts"][0]["relative"]["numerator_when"] == "A2B"


def test_relative_denominator_pre_aggregation_via_ui(at: AppTest, data_dir_mini: Path) -> None:
    """分母の事前集計の追加・編集・削除の配線を検証する。"""
    _load_data(at, data_dir_mini)
    uid = _create_part(at)
    at.checkbox(key=f"{uid}_rel_on").set_value(True).run()
    at.button(key=f"{uid}_rel_pre_add").click().run()
    assert not at.exception
    rel = at.session_state["score_file"]["score_parts"][0]["relative"]
    assert rel["denominator_pre_aggregation"] == [{"axis": "Block", "op": "mean"}]
    at.selectbox(key=f"{uid}_rel_pre0_axis").set_value("WL").run()
    at.selectbox(key=f"{uid}_rel_pre0_op").set_value("sum").run()
    assert not at.exception
    rel = at.session_state["score_file"]["score_parts"][0]["relative"]
    assert rel["denominator_pre_aggregation"] == [{"axis": "WL", "op": "sum"}]
    at.button(key=f"{uid}_rel_pre0_del").click().run()
    assert not at.exception
    rel = at.session_state["score_file"]["score_parts"][0]["relative"]
    assert "denominator_pre_aggregation" not in rel


def test_order_step_move_buttons_without_sortables(
    at: AppTest, data_dir_mini: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Order ステップの上下移動・削除・✎ 選択(フォールバックUI)を検証する。

    AppTest では D&D 部品を操作できないため、HAS_SORTABLES を False にして
    streamlit-sortables 無し環境の上下ボタン側の配線を固定する。
    """
    monkeypatch.setattr(widgets, "HAS_SORTABLES", False)
    _load_data(at, data_dir_mini)
    uid = _create_part(at)
    at.button(key=f"{uid}_up2").click().run()  # 3番目の WL を1つ上へ
    assert not at.exception
    part = at.session_state["score_file"]["score_parts"][0]
    assert part["order"][:4] == ["Measure", "WL", "State", "STR"]
    at.button(key=f"{uid}_dn1").click().run()  # WL を1つ下へ(元に戻る)
    assert not at.exception
    part = at.session_state["score_file"]["score_parts"][0]
    assert part["order"][:4] == ["Measure", "State", "WL", "STR"]
    at.button(key=f"{uid}_rm3").click().run()  # STR を削除
    assert not at.exception
    part = at.session_state["score_file"]["score_parts"][0]
    assert "STR" not in part["order"]
    assert "STR" not in part["aggregations"]
    at.button(key=f"{uid}_sel4").click().run()  # ✎ で Chip を編集対象に
    assert not at.exception
    assert at.session_state[f"{uid}_sel_entry"] == "Chip"


def test_group_def_create_and_edit_rows_via_ui(at: AppTest, data_dir_mini: Path) -> None:
    """グループ定義の作成・行の追加と編集・Physical 記法切替の配線を検証する。"""
    _load_data(at, data_dir_mini)
    at.sidebar.radio(key="screen").set_value(SCREEN_SETS).run()
    at.text_input(key="new_gdef_name").set_value("WLg")
    at.selectbox(key="new_gdef_axis").set_value("WL")
    at.button(key="new_gdef_btn").click().run()
    assert not at.exception
    gd = at.session_state["score_file"]["groupDefs"]["WLg"]
    assert gd == {"axis": "WL", "groups": {}, "definedInLogical": True}
    at.button(key="gdef_WLg_add").click().run()
    assert not at.exception
    assert at.session_state["score_file"]["groupDefs"]["WLg"]["groups"] == {"g1": [0, 0]}
    at.text_input(key="gdef_WLg_0_lbl").set_value("low")
    at.number_input(key="gdef_WLg_0_hi").set_value(2).run()
    assert not at.exception
    assert at.session_state["score_file"]["groupDefs"]["WLg"]["groups"] == {"low": [0, 2]}
    at.checkbox(key="gdef_WLg_phys").set_value(True).run()
    assert not at.exception
    assert at.session_state["score_file"]["groupDefs"]["WLg"]["definedInLogical"] is False


def test_group_def_delete_guarded_by_part_reference_via_ui(at: AppTest, data_dir_mini: Path) -> None:
    """参照ありのグループ定義削除が UI 上でガードされるフローを検証する。

    グループ派生軸はパーツのエントリ追加候補に現れ、order に置いている間は
    画面3の削除がエラーになる。エントリを外すと削除できる。
    """
    _load_data(at, data_dir_mini)
    uid = _create_part(at)
    at.sidebar.radio(key="screen").set_value(SCREEN_SETS).run()
    at.text_input(key="new_gdef_name").set_value("WLg")
    at.selectbox(key="new_gdef_axis").set_value("WL")
    at.button(key="new_gdef_btn").click().run()
    at.button(key="gdef_WLg_add").click().run()
    assert not at.exception
    # 画面2: グループ派生軸をエントリとして order に置く
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.selectbox(key=f"{uid}_addax").set_value("WLg").run()
    at.button(key=f"{uid}_addax_btn").click().run()
    assert not at.exception
    part = at.session_state["score_file"]["score_parts"][0]
    assert part["order"][-1] == "WLg"
    assert part["aggregations"]["WLg"] == {"op": "filter", "value": "g1"}
    # 画面3: 参照がある間は削除できない
    at.sidebar.radio(key="screen").set_value(SCREEN_SETS).run()
    at.button(key="gdef_WLg_del").click().run()
    assert any("参照されているため削除できません" in e.value for e in at.error)
    assert "WLg" in at.session_state["score_file"]["groupDefs"]
    # エントリを外すと削除できる
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.selectbox(key=f"{uid}_sel_entry").set_value("WLg").run()
    at.button(key=f"{uid}_ed_del").click().run()
    at.sidebar.radio(key="screen").set_value(SCREEN_SETS).run()
    at.button(key="gdef_WLg_del").click().run()
    assert not at.exception
    assert "WLg" not in at.session_state["score_file"]["groupDefs"]


def test_selection_set_create_edit_and_alias_via_ui(at: AppTest, data_dir_mini: Path) -> None:
    """選択セットの作成・単一軸の値編集・別名保存の配線を検証する。"""
    _load_data(at, data_dir_mini)
    at.sidebar.radio(key="screen").set_value(SCREEN_SETS).run()
    at.text_input(key="new_set_name").set_value("states")
    next(b for b in at.button if b.label == "作成" and b.key is None).click().run()
    assert not at.exception
    assert at.session_state["score_file"]["selectionSets"]["states"] == []
    at.radio(key="set_states_kind").set_value("単一軸の値リスト").run()
    at.selectbox(key="set_states_axis").set_value("State").run()
    at.multiselect(key="set_states_vals").set_value(["R2A", "A2B"]).run()
    assert not at.exception
    assert at.session_state["score_file"]["selectionSets"]["states"] == ["R2A", "A2B"]
    at.text_input(key="set_states_alias").set_value("states2")
    at.button(key="set_states_alias_btn").click().run()
    assert not at.exception
    sets = at.session_state["score_file"]["selectionSets"]
    assert sets["states2"] == ["R2A", "A2B"]
    assert sets["states"] == ["R2A", "A2B"]  # 元は残る(コピー)


def test_selection_set_delete_guarded_via_ui(at: AppTest, data_dir_mini: Path) -> None:
    """参照ありの選択セット削除が UI 上でガードされるフローを検証する。

    diff の「選択セット(ref)」で参照している間は削除がエラーになり、
    参照を直接指定へ戻すと削除できる。
    """
    _load_data(at, data_dir_mini)
    uid = _create_part(at)
    at.sidebar.radio(key="screen").set_value(SCREEN_SETS).run()
    at.text_input(key="new_set_name").set_value("pairs")
    next(b for b in at.button if b.label == "作成" and b.key is None).click().run()
    assert not at.exception
    # 画面2: State を diff にして選択セットを参照する
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.selectbox(key=f"{uid}_sel_entry").set_value("State").run()
    at.selectbox(key=f"{uid}_State_op").set_value("diff").run()
    at.radio(key=f"{uid}_State_dsrc").set_value("選択セット(ref)").run()
    assert not at.exception
    part = at.session_state["score_file"]["score_parts"][0]
    assert part["aggregations"]["State"] == {"op": "diff", "ref": "pairs"}
    # 画面3: 参照がある間は削除できない
    at.sidebar.radio(key="screen").set_value(SCREEN_SETS).run()
    at.button(key="set_pairs_del").click().run()
    assert any("参照されているため削除できません" in e.value for e in at.error)
    assert "pairs" in at.session_state["score_file"]["selectionSets"]
    # 参照を直接指定へ戻すと削除できる(画面3を経ると編集エントリの
    # ウィジェット状態は破棄されるので、State を選び直してから操作する)
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.selectbox(key=f"{uid}_sel_entry").set_value("State").run()
    at.radio(key=f"{uid}_State_dsrc").set_value("直接指定").run()
    assert "ref" not in at.session_state["score_file"]["score_parts"][0]["aggregations"]["State"]
    at.sidebar.radio(key="screen").set_value(SCREEN_SETS).run()
    at.button(key="set_pairs_del").click().run()
    assert not at.exception
    assert "pairs" not in at.session_state["score_file"]["selectionSets"]


def test_load_config_only_via_ui(at: AppTest, fixtures_dir: Path) -> None:
    """「データなし(設定のみ編集)」の読み込みボタンの配線を検証する。

    設定未入力なら設定を促すエラー、設定 jsonc のパスを入れると config 由来の
    context(config_only)とパーツが入り、画面1に設定のみ編集中の案内が出る。
    """
    at.toggle(key="paths_mode").set_value(True).run()
    at.radio(key="data_mode").set_value("なし").run()
    at.button(key="load_btn").click().run()
    assert not at.exception
    assert any("設定 jsonc を入れてください" in e.value for e in at.error)
    at.text_input(key="config_path_input").set_value(str(fixtures_dir / "config.jsonc"))
    at.button(key="load_btn").click().run()
    assert not at.exception
    assert at.session_state["context"]["config_only"] is True
    sf = at.session_state["score_file"]
    assert [p["name"] for p in sf["score_parts"]] == ["FBC_A2B_upper1_rel", "dVtBudget_R2A"]
    assert any("設定のみ編集中" in i.value for i in at.info)


def test_load_path_error_messages(at: AppTest, data_dir_mini: Path) -> None:
    """パス指定読み込みの未カバーのエラー表示を検証する。

    設定 jsonc の実体なし・ダミーのディレクトリ未入力・Chip 数と Board 数の
    個数不一致、それぞれが明確なエラーになる。
    """
    at.toggle(key="paths_mode").set_value(True).run()
    at.text_input(key="data_dir_input").set_value(str(data_dir_mini))
    at.text_input(key="config_path_input").set_value("no/such/config.jsonc")
    at.button(key="load_btn").click().run()
    assert not at.exception
    assert any("設定 jsonc が見つかりません" in e.value for e in at.error)
    at.text_input(key="config_path_input").set_value("")
    at.radio(key="data_mode").set_value("ダミー(測定前)").run()
    at.button(key="load_btn").click().run()
    assert not at.exception
    assert any("ダミー一式ディレクトリのパスを入力してください" in e.value for e in at.error)
    at.text_input(key="dummy_chips").set_value("2,3,4")
    at.button(key="load_btn").click().run()
    assert not at.exception
    assert any("一致しません" in e.value for e in at.error)


def test_custom_part_params_editor_via_ui(at: AppTest, data_dir_mini: Path, fixtures_dir: Path) -> None:
    """自作関数パーツの params 行エディタの配線を検証する。

    追加 → 改名と型付き値入力 → 名前重複のエラー(dict は壊れない)→ 行削除。
    """
    at.toggle(key="paths_mode").set_value(True).run()
    at.text_input(key="data_dir_input").set_value(str(data_dir_mini))
    at.text_input(key="custom_path_input").set_value(str(fixtures_dir / "custom_parts.py"))
    at.button(key="load_btn").click().run()
    assert not at.exception
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.selectbox(key="new_part_type").set_value("custom").run()
    at.button(key="add_part_btn").click().run()
    assert not at.exception
    uid = at.session_state["score_file"]["score_parts"][0]["_uid"]
    at.button(key=f"{uid}_prm_add").click().run()
    assert not at.exception
    assert at.session_state["score_file"]["score_parts"][0]["params"] == {"param1": 0}
    at.text_input(key=f"{uid}_prm0_k").set_value("thresh")
    at.text_input(key=f"{uid}_prm0_v").set_value("1.5").run()
    assert not at.exception
    assert at.session_state["score_file"]["score_parts"][0]["params"] == {"thresh": 1.5}
    # 2行目を追加して同名に改名 → エラー表示になり、params は壊れない
    at.button(key=f"{uid}_prm_add").click().run()
    at.text_input(key=f"{uid}_prm1_k").set_value("thresh").run()
    assert any("パラメータ名が重複しています" in e.value for e in at.error)
    assert at.session_state["score_file"]["score_parts"][0]["params"] == {"thresh": 1.5, "param2": 0}
    at.button(key=f"{uid}_prm1_rm").click().run()
    assert not at.exception
    assert at.session_state["score_file"]["score_parts"][0]["params"] == {"thresh": 1.5}


def test_run_and_export_guards_via_ui(at: AppTest, data_dir_mini: Path) -> None:
    """テスト計算の前提チェックとエクスポートのゲートの配線を検証する。

    パーツ無しは明確なエラー、検証エラーありは実行拒否+ダウンロードボタン
    非表示、検証 OK でダウンロードボタンが2つ(全体・パーツ単体)出る。
    """
    at.sidebar.radio(key="screen").set_value(SCREEN_TEST).run()
    at.text_input(key="test_dir").set_value(str(data_dir_mini))
    at.button(key="run_btn").click().run()
    assert not at.exception
    assert any("スコアパーツがありません" in e.value for e in at.error)
    assert len(at.download_button) == 0
    at.sidebar.radio(key="screen").set_value("1. データ読み込み").run()
    _load_data(at, data_dir_mini)
    _create_part(at)
    at.session_state["score_file"]["expression"] = "part_1 + ghost"
    at.sidebar.radio(key="screen").set_value(SCREEN_TEST).run()
    at.button(key="run_btn").click().run()
    assert not at.exception
    assert any("設定に誤りがあるため実行できません" in e.value for e in at.error)
    assert any("エクスポートできません" in w.value for w in at.warning)
    assert len(at.download_button) == 0
    at.session_state["score_file"]["expression"] = "part_1"
    at.run()
    assert not at.exception
    assert len(at.download_button) == 2  # score.jsonc 全体 + パーツ単体


def test_existing_score_config_in_data_dir_button(
    at: AppTest, tmp_path: Path, data_dir_mini: Path, fixtures_dir: Path
) -> None:
    """データ内で自動検出された設定の「既存スコア設定を読み込む」ボタンを検証する。

    設定欄で指定せずデータディレクトリ内で検出された設定は score_file を勝手に
    置き換えず、ボタンを押したときだけ既存スコア設定とグループ定義を取り込む。
    """
    d = tmp_path / "run"
    shutil.copytree(data_dir_mini, d)
    shutil.copy(fixtures_dir / "config.jsonc", d / "config.jsonc")
    _load_data(at, d)
    assert at.session_state["context"]["config_source"] == "自動検出"
    assert at.session_state["score_file"]["score_parts"] == []  # 勝手に置き換えない
    next(b for b in at.button if b.label == "設定jsonc内の既存スコア設定を読み込んで編集を始める").click().run()
    assert not at.exception
    sf = at.session_state["score_file"]
    assert [p["name"] for p in sf["score_parts"]] == ["FBC_A2B_upper1_rel", "dVtBudget_R2A"]
    assert "WLgroup" in sf["groupDefs"]
