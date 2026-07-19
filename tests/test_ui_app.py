"""Streamlit AppTest smoke tests: the app starts, loads data, creates a
part from the skeleton, and runs a test computation end to end."""
from pathlib import Path

import pytest

from ui import state

APP = str(Path(__file__).resolve().parent.parent / "ui" / "app.py")
SCREEN_PARTS = "2. スコアパーツ編集"
SCREEN_SETS = "3. 選択セット・グループ定義"
SCREEN_COMPOSE = "4. スコア合成・制約"
SCREEN_TEST = "5. テスト実行・エクスポート"


@pytest.fixture
def at(tmp_path, monkeypatch):
    apptest = pytest.importorskip("streamlit.testing.v1").AppTest
    # keep the test away from the user's real draft file
    monkeypatch.setattr(state, "DRAFT_PATH", tmp_path / "draft.jsonc")
    t = apptest.from_file(APP, default_timeout=60)
    t.run()
    assert not t.exception
    return t


def _load_data(at, data_dir):
    at.text_input(key="data_dir_input").set_value(str(data_dir))
    at.button(key="load_btn").click().run()
    assert not at.exception
    assert at.session_state["context"]["types"] == ["FBC", "tR"]


def test_app_starts(at):
    assert at.sidebar.radio(key="screen").value == "1. データ読み込み"


def test_load_empty_dir_shows_error(at):
    at.button(key="load_btn").click().run()
    assert not at.exception
    assert any("入力してください" in e.value for e in at.error)


def test_load_bad_dir_shows_error(at):
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
    # skeleton passes validation -> the screen shows the OK marker
    assert any("検証" in s.value and "OK" in s.value for s in at.success)


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
    """The part-name button must update the visible expression input
    immediately, not only the internal dict (widget-state override bug)."""
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
    """config.jsonc defines WLgroups up to WL 20 but B9LS.json says numWLs=6:
    screen 1 must warn right after loading."""
    at.text_input(key="data_dir_input").set_value(str(data_dir_mini))
    at.text_input(key="config_path_input").set_value(str(fixtures_dir / "config.jsonc"))
    at.text_input(key="geninfo_path_input").set_value(str(fixtures_dir / "B9LS.json"))
    at.button(key="load_btn").click().run()
    assert not at.exception
    assert any("範囲外" in w.value for w in at.warning)


def test_config_wlgroup_imported_as_group_def(at, data_dir_mini, fixtures_dir):
    at.text_input(key="data_dir_input").set_value(str(data_dir_mini))
    at.text_input(key="config_path_input").set_value(str(fixtures_dir / "config.jsonc"))
    at.button(key="load_btn").click().run()
    assert not at.exception
    gd = at.session_state["score_file"]["groupDefs"]["WLgroup"]
    assert gd["axis"] == "WL"
    assert gd["groups"]["WLgroup01"] == [0, 3]
    # the group-defs editor renders without errors
    at.sidebar.radio(key="screen").set_value(SCREEN_SETS).run()
    assert not at.exception


def test_duplicate_then_switch_keeps_parts_independent(at, data_dir_mini):
    """The widget-state isolation pattern that was never tested before:
    edit part B, switch back to part A, and check A's data AND its visible
    widgets survived. With a shared _uid (the duplicate bug) the copies
    shared every widget: A's name became B's, and unchecking relative on B
    also stripped A's relative config."""
    _load_data(at, data_dir_mini)
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.button(key="add_part_btn").click().run()
    dup = next(b for b in at.button if b.label == "このパーツを複製")
    dup.click().run()
    assert not at.exception
    parts = at.session_state["score_file"]["score_parts"]
    assert [p["name"] for p in parts] == ["part_1", "part_1_1"]
    uid0, uid1 = parts[0]["_uid"], parts[1]["_uid"]
    assert uid0 != uid1

    # the copy is selected: turn ITS relative off
    at.checkbox(key=f"{uid1}_rel_on").set_value(False).run()
    assert not at.exception
    parts = at.session_state["score_file"]["score_parts"]
    assert parts[1].get("relative") is None
    assert parts[0].get("relative") is not None  # original untouched

    # switch back to the original: its widgets must show its own values.
    # selection is keyed by uid so it is fresh at the start of that same run
    # (the ← 編集中 marker in the drag list depends on this)
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
    """Place __relative__ in order via the dedicated button, then turn
    relative off: the part must stay valid (no orphan step)."""
    _load_data(at, data_dir_mini)
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.button(key="add_part_btn").click().run()
    uid = at.session_state["score_file"]["score_parts"][0]["_uid"]
    at.button(key=f"{uid}_addfixed_btn").click().run()
    assert "__relative__" in at.session_state["score_file"]["score_parts"][0]["order"]
    at.checkbox(key=f"{uid}_rel_on").set_value(False).run()
    assert not at.exception
    part = at.session_state["score_file"]["score_parts"][0]
    assert part.get("relative") is None
    assert "__relative__" not in part["order"]
    assert not at.error


def test_custom_part_end_to_end(at, data_dir_mini, fixtures_dir):
    """Load with custom_parts.py, create a type=custom part, compute it."""
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

    # pick a specific function via the 関数 pulldown
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
    at.text_input(key="data_dir_input").set_value(str(data_dir_mini))
    at.text_input(key="coef_path_input").set_value(str(dvtbudget_coef_path))
    at.button(key="load_btn").click().run()
    assert not at.exception
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.button(key="add_part_btn").click().run()
    assert state.DRAFT_PATH.exists()
    draft = state.load_draft()
    assert [p["name"] for p in draft["score_file"]["score_parts"]] == ["part_1"]
    assert draft["context_inputs"]["data_dir"] == str(data_dir_mini.resolve())
    assert draft["context_inputs"]["coef_path"] == str(dvtbudget_coef_path.resolve())

    # a fresh session (same draft path): restoring brings back the score
    # file, the loaded-data context AND the visible screen-1 inputs (blank
    # inputs would drop the coef path on the next 読み込み click)
    apptest = pytest.importorskip("streamlit.testing.v1").AppTest
    at2 = apptest.from_file(APP, default_timeout=60)
    at2.run()
    at2.button(key="restore_btn").click().run()
    assert not at2.exception
    assert [p["name"] for p in at2.session_state["score_file"]["score_parts"]] == ["part_1"]
    assert at2.session_state["context"]["types"] == ["FBC", "tR"]
    assert "dVtBudget" in at2.session_state["context"]["part_types"]
    assert at2.text_input(key="data_dir_input").value == str(data_dir_mini.resolve())
    assert at2.text_input(key="coef_path_input").value == str(dvtbudget_coef_path.resolve())
