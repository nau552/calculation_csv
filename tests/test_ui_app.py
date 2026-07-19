"""Streamlit AppTest smoke tests: the app starts, loads data, creates a
part from the skeleton, and runs a test computation end to end."""
from pathlib import Path

import pytest

from ui import state

APP = str(Path(__file__).resolve().parent.parent / "ui" / "app.py")
SCREEN_PARTS = "2. スコアパーツ編集"
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


def test_undo_reverts_last_action(at, data_dir_mini):
    _load_data(at, data_dir_mini)
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.button(key="add_part_btn").click().run()
    assert len(at.session_state["score_file"]["score_parts"]) == 1
    at.button(key="undo_btn").click().run()
    assert not at.exception
    assert at.session_state["score_file"]["score_parts"] == []


def test_draft_autosaved_and_restored(at, data_dir_mini, tmp_path):
    _load_data(at, data_dir_mini)
    at.sidebar.radio(key="screen").set_value(SCREEN_PARTS).run()
    at.button(key="add_part_btn").click().run()
    assert state.DRAFT_PATH.exists()
    draft = state.load_draft()
    assert [p["name"] for p in draft["score_file"]["score_parts"]] == ["part_1"]
    assert draft["context_inputs"]["data_dir"] == str(data_dir_mini.resolve())

    # a fresh session (same draft path): restoring brings back both the
    # score file AND the loaded-data context, so editing can continue
    apptest = pytest.importorskip("streamlit.testing.v1").AppTest
    at2 = apptest.from_file(APP, default_timeout=60)
    at2.run()
    at2.button(key="restore_btn").click().run()
    assert not at2.exception
    assert [p["name"] for p in at2.session_state["score_file"]["score_parts"]] == ["part_1"]
    assert at2.session_state["context"]["types"] == ["FBC", "tR"]
