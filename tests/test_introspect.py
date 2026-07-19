"""Tests for scorelib.introspect: deriving UI-facing metadata (types, axes,
value candidates) from a past experiment's output directory."""
import shutil

import pytest

from scorelib.introspect import (
    available_part_types,
    axis_catalog,
    detect_types,
    find_dvtbudget_coef,
    find_run_config,
)

REPO_FILES = {"coef": "dvtbudget_coef.jsonc", "sample": "sample.jsonc"}


@pytest.fixture
def full_dir(tmp_path, data_dir_mini, dvtbudget_coef_path, fixtures_dir):
    """mini data + coef jsonc + a run-config jsonc, like a real run directory."""
    d = tmp_path / "run"
    shutil.copytree(data_dir_mini, d)
    shutil.copy(dvtbudget_coef_path, d / "dvtbudget_coef.jsonc")
    shutil.copy(fixtures_dir / "config.jsonc", d / "config.jsonc")
    return d


def test_detect_types(data_dir_mini):
    assert detect_types(data_dir_mini) == ["FBC", "tR"]


def test_detect_types_ignores_reserved_and_map_files(tmp_path, data_dir_mini):
    d = tmp_path / "run"
    shutil.copytree(data_dir_mini, d)
    (d / "FBC_expanded.csv").write_text("Board,Measure,x\n0,0,1\n")
    (d / "notes.csv").write_text("a,b\n1,2\n")  # no Measure column -> not a type
    assert detect_types(d) == ["FBC", "tR"]


def test_available_part_types_without_coef(data_dir_mini):
    # result_tmp_mini has no coefficient jsonc -> no dVtBudget offered
    assert available_part_types(data_dir_mini) == ["FBC", "tR"]


def test_available_part_types_with_coef(full_dir):
    assert available_part_types(full_dir) == ["FBC", "tR", "dVtBudget"]


def test_find_dvtbudget_coef(full_dir, data_dir_mini):
    found = find_dvtbudget_coef(full_dir)
    assert found is not None and found.name == "dvtbudget_coef.jsonc"
    assert find_dvtbudget_coef(data_dir_mini) is None


def test_find_run_config(full_dir, data_dir_mini):
    found = find_run_config(full_dir)
    assert found is not None and found.name == "config.jsonc"
    assert find_run_config(data_dir_mini) is None


def test_axis_catalog_fbc(data_dir_mini):
    catalog = axis_catalog(data_dir_mini, "FBC")
    # measured axes in csv-header order, then label axes
    assert list(catalog) == [
        "InBatchEpoch", "Board", "Chip", "Block", "WL", "STR", "State",
        "Erase_Label", "Erase_Override", "Program_Label", "Program_Override",
        "Read_Label", "Read_Override",
    ]
    assert catalog["State"] == ["R2A", "A2R", "A2B", "B2A"]
    assert catalog["Read_Override"] == [False, True]
    assert "read_level_upper1" in catalog["Read_Label"]
    assert catalog["WL"] == sorted(catalog["WL"])  # numeric uniques, sorted


def test_axis_catalog_candidates_narrowed_to_present_values(data_dir_mini):
    """map_Label lists the full vocabulary, but only values present in the
    past data are offered (skeleton filters on the first candidate)."""
    catalog = axis_catalog(data_dir_mini, "FBC")
    assert catalog["Erase_Label"] == ["program_read_ref"]
    assert catalog["Read_Label"] == ["read_level_upper1", "read_level_lower1"]


def test_axis_catalog_tr_has_page(data_dir_mini):
    catalog = axis_catalog(data_dir_mini, "tR")
    assert "Page" in catalog
    assert catalog["Page"] == ["L", "M", "U"]
    assert "State" not in catalog  # FBC-only axis must not leak into tR


def test_axis_catalog_dvtbudget_is_fbc(data_dir_mini):
    assert axis_catalog(data_dir_mini, "dVtBudget") == axis_catalog(data_dir_mini, "FBC")
