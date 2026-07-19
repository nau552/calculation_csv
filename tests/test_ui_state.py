"""Tests for ui.state: pure editing logic behind the Streamlit UI."""
import shutil

import pytest

from scorelib.cli import compute_score_part
from scorelib.introspect import axis_catalog
from scorelib.models import ScorePart
from ui import state


@pytest.fixture
def catalog(data_dir_mini):
    return axis_catalog(data_dir_mini, "FBC")


@pytest.fixture
def sf():
    return state.empty_score_file()


# ------------------------------------------------------------------- skeleton

def test_skeleton_is_valid_and_relative_on(catalog):
    part = state.part_skeleton("p1", "FBC", catalog)
    assert part["relative"]["split_axis"] == "Read_Override"
    assert "Read_Override" not in part["order"]  # consumed by relative
    assert "InBatchEpoch" not in part["order"]
    assert part["order"][-3:] == ["Board", "Chip", "Block"]
    assert part["order"][0].endswith("_Label")  # labels first
    assert state.validate_part(part) == []


def test_skeleton_default_ops(catalog):
    part = state.part_skeleton("p1", "FBC", catalog)
    aggs = part["aggregations"]
    assert aggs["State"] == {"op": "filter", "value": "R2A"}
    assert aggs["Erase_Override"] == {"op": "filter", "value": False}
    assert aggs["WL"] == {"op": "mean"}


def test_skeleton_computes_as_is(data_dir_mini, catalog):
    """The whole point of the skeleton: computable without any edits."""
    part = ScorePart.model_validate(state.part_skeleton("p1", "FBC", catalog))
    value = compute_score_part(data_dir_mini, part)
    assert isinstance(value, float)


def test_skeleton_without_read_override_has_no_relative(data_dir_mini):
    catalog_tr = axis_catalog(data_dir_mini, "tR")
    part = state.part_skeleton("p1", "tR", catalog_tr)
    assert "relative" not in part or part["relative"] is None or "Read_Override" in catalog_tr
    # tR has Read_Override via parameterLabel, so relative should be ON there;
    # simulate a catalog without it instead:
    no_ovr = {a: c for a, c in catalog_tr.items() if a != "Read_Override"}
    part2 = state.part_skeleton("p2", "tR", no_ovr)
    assert "relative" not in part2


# ------------------------------------------------- relative on/off and order

def test_disable_relative_restores_split_axis(catalog):
    part = state.part_skeleton("p", "FBC", catalog)
    assert "Read_Override" not in part["order"]
    restored = state.disable_relative(part, catalog)
    assert restored == "Read_Override"
    assert "relative" not in part
    assert "Read_Override" in part["order"]
    # restored with the safe default: reference side (filter False)
    assert part["aggregations"]["Read_Override"] == {"op": "filter", "value": False}
    assert state.validate_part(part) == []


def test_enable_relative_removes_split_axis(catalog):
    part = state.part_skeleton("p", "FBC", catalog)
    state.disable_relative(part, catalog)
    state.enable_relative(part, catalog)
    assert part["relative"]["split_axis"] == "Read_Override"
    assert "Read_Override" not in part["order"]
    assert "Read_Override" not in part["aggregations"]


def test_change_split_axis_swaps_order_membership(catalog):
    part = state.part_skeleton("p", "FBC", catalog)
    assert "Erase_Override" in part["order"]
    state.change_split_axis(part, "Erase_Override", catalog)
    assert part["relative"]["split_axis"] == "Erase_Override"
    assert "Erase_Override" not in part["order"]
    assert "Read_Override" in part["order"]  # old split axis returned to order


def test_disable_relative_skips_axis_already_in_combined_entry(catalog):
    part = state.part_skeleton("p", "FBC", catalog)
    part["order"].insert(0, "State&Read_Override")
    restored = state.disable_relative(part, catalog)
    assert restored is None  # covered by the combined entry -> not re-added
    assert part["order"].count("Read_Override") == 0


# ------------------------------------------------------------------- editing

def test_unique_part_name(sf):
    sf["score_parts"].append({"name": "part_1"})
    assert state.unique_part_name(sf) == "part_2"


def test_duplicate_part(sf, catalog):
    sf["score_parts"].append(state.part_skeleton("p", "FBC", catalog))
    idx = state.duplicate_part(sf, 0)
    assert idx == 1
    assert sf["score_parts"][1]["name"] == "p_1"
    sf["score_parts"][1]["aggregations"]["WL"]["op"] = "sum"
    assert sf["score_parts"][0]["aggregations"]["WL"]["op"] == "mean"  # deep copy


def test_move_entry():
    lst = ["a", "b", "c"]
    assert state.move_entry(lst, 0, +1) == 1
    assert lst == ["b", "a", "c"]
    assert state.move_entry(lst, 0, -1) == 0  # no-op at the edge
    assert lst == ["b", "a", "c"]


# -------------------------------------------------------------- selection sets

def test_referencing_parts_and_guarded_delete(sf):
    sf["selectionSets"]["ud"] = [{"State": "R2A", "Read_Label": "read_level_upper1"}]
    sf["score_parts"].append(
        {
            "name": "p",
            "type": "FBC",
            "order": ["State&Read_Label"],
            "aggregations": {"State&Read_Label": {"op": "sum", "ref": "ud"}},
        }
    )
    assert state.referencing_parts(sf, "ud") == ["p"]
    with pytest.raises(ValueError, match="参照されている"):
        state.delete_selection_set(sf, "ud")
    sf["score_parts"].clear()
    state.delete_selection_set(sf, "ud")
    assert sf["selectionSets"] == {}


def test_save_set_as(sf):
    sf["selectionSets"]["ud"] = [{"State": "R2A", "Read_Label": "u1"}]
    state.save_set_as(sf, "ud", "ud2")
    sf["selectionSets"]["ud2"][0]["State"] = "A2B"
    assert sf["selectionSets"]["ud"][0]["State"] == "R2A"  # deep copy
    with pytest.raises(ValueError, match="既に存在"):
        state.save_set_as(sf, "ud", "ud2")


# ---------------------------------------------------------------- validation

def test_validate_score_file_ok(sf, catalog):
    sf["score_parts"].append(state.part_skeleton("p", "FBC", catalog))
    sf["expression"] = "p"
    assert state.validate_score_file(sf) == []


def test_validate_reports_engine_errors(sf):
    sf["score_parts"].append(
        {"name": "p", "type": "FBC", "order": ["State"], "aggregations": {"State": {"op": "filter"}}}
    )
    problems = state.validate_score_file(sf)
    assert any("filter" in p for p in problems)


def test_validate_expression_unknown_part(sf, catalog):
    sf["score_parts"].append(state.part_skeleton("p", "FBC", catalog))
    sf["expression"] = "p + nope"
    problems = state.validate_score_file(sf)
    assert any("expression" in p for p in problems)


def test_validate_duplicate_names_and_dangling_constraint(sf, catalog):
    sf["score_parts"] = [
        state.part_skeleton("p", "FBC", catalog),
        state.part_skeleton("p", "FBC", catalog),
    ]
    sf["constraintThreshold"] = {"gone": {"value": 1.0}}
    problems = state.validate_score_file(sf)
    assert any("重複" in p for p in problems)
    assert any("gone" in p for p in problems)


def test_validate_unknown_ref(sf, catalog):
    part = state.part_skeleton("p", "FBC", catalog)
    part["aggregations"]["State"] = {"op": "sum", "ref": "nope"}
    sf["score_parts"].append(part)
    problems = state.validate_score_file(sf)
    assert any("nope" in p for p in problems)


# ------------------------------------------------------------------- context

def test_build_context(data_dir_mini):
    ctx = state.build_context(str(data_dir_mini))
    assert ctx["types"] == ["FBC", "tR"]
    assert ctx["part_types"] == ["FBC", "tR"]  # no coef jsonc in mini dir
    assert "Page" in ctx["catalogs"]["tR"]
    assert ctx["has_initial_temperature"] is True
    assert ctx["config_path"] is None


def test_build_context_missing_dir():
    with pytest.raises(ValueError, match="見つかりません"):
        state.build_context("no/such/dir")


def test_build_context_empty_path_rejected():
    """Path('') means the current directory in Python; an empty input must
    not silently scan wherever the app happened to be launched from."""
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="入力してください"):
            state.build_context(bad)


def test_build_context_explicit_coef(data_dir_mini, dvtbudget_coef_path):
    """The coefficient jsonc normally lives outside result_tmp and is given
    as its own path."""
    ctx = state.build_context(str(data_dir_mini), coef_path=str(dvtbudget_coef_path))
    assert ctx["part_types"] == ["FBC", "tR", "dVtBudget"]
    assert ctx["coef_source"] == "指定"
    assert "dVtBudget" in ctx["catalogs"]


def test_build_context_explicit_config(data_dir_mini, fixtures_dir):
    ctx = state.build_context(str(data_dir_mini), config_path=str(fixtures_dir / "config.jsonc"))
    assert ctx["config_source"] == "指定"
    assert ctx["generation"]
    assert ctx["wlgroup"]


def test_build_context_missing_explicit_file_rejected(data_dir_mini):
    with pytest.raises(ValueError, match="dVtBudget係数jsonc が見つかりません"):
        state.build_context(str(data_dir_mini), coef_path="no/such.jsonc")


def test_build_context_in_dir_discovery_still_works(tmp_path, data_dir_mini, dvtbudget_coef_path):
    d = tmp_path / "run"
    shutil.copytree(data_dir_mini, d)
    shutil.copy(dvtbudget_coef_path, d / "dvtbudget_coef.jsonc")
    ctx = state.build_context(str(d))
    assert ctx["coef_source"] == "自動検出"
    assert "dVtBudget" in ctx["part_types"]


# ------------------------------------------------------------- draft / export

def test_draft_roundtrip(tmp_path, sf, catalog):
    sf["score_parts"].append(state.part_skeleton("p", "FBC", catalog))
    path = tmp_path / "draft.jsonc"
    state.save_draft(sf, {"data_dir": "somewhere"}, path)
    draft = state.load_draft(path)
    assert draft["score_file"] == sf
    assert draft["context_inputs"]["data_dir"] == "somewhere"
    assert state.load_draft(tmp_path / "none.jsonc") is None


def test_draft_legacy_format_accepted(tmp_path, sf, catalog):
    """Drafts written before context_inputs existed (bare ScoreFile dict)."""
    import json

    sf["score_parts"].append(state.part_skeleton("p", "FBC", catalog))
    path = tmp_path / "draft.jsonc"
    path.write_text(json.dumps(sf), encoding="utf-8")
    draft = state.load_draft(path)
    assert draft["score_file"] == sf
    assert draft["context_inputs"] == {}


def test_export_and_import_roundtrip(sf, catalog):
    sf["score_parts"].append(state.part_skeleton("p", "FBC", catalog))
    sf["expression"] = "p"
    text = state.score_file_to_jsonc(sf)
    back = state.import_score_file(text)
    assert [p["name"] for p in back["score_parts"]] == ["p"]
    assert back["expression"] == "p"


def test_import_run_config(fixtures_dir):
    text = (fixtures_dir / "config.jsonc").read_text(encoding="utf-8")
    imported = state.import_score_file(text)
    assert imported["score_parts"]
