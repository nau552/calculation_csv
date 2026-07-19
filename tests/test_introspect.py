"""scorelib.introspect のテスト: 過去実験の出力ディレクトリからの
UI向けメタデータ（type・軸・値候補）の導出。"""
import shutil

import pytest

from scorelib.introspect import (
    axis_catalog,
    detect_types,
    find_dvtbudget_coefs,
    find_run_configs,
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


def test_find_dvtbudget_coefs(full_dir, data_dir_mini):
    assert [p.name for p in find_dvtbudget_coefs(full_dir)] == ["dvtbudget_coef.jsonc"]
    assert find_dvtbudget_coefs(data_dir_mini) == []


def test_find_run_configs(full_dir, data_dir_mini):
    assert [p.name for p in find_run_configs(full_dir)] == ["config.jsonc"]
    assert find_run_configs(data_dir_mini) == []


def test_axis_catalog_fbc(data_dir_mini):
    catalog = axis_catalog(data_dir_mini, "FBC")
    # 測定軸は csv ヘッダ順、その後にラベル軸
    assert list(catalog) == [
        "InBatchEpoch", "Board", "Chip", "Block", "WL", "STR", "State",
        "Erase_Label", "Erase_Override", "Program_Label", "Program_Override",
        "Read_Label", "Read_Override",
    ]
    assert catalog["State"] == ["R2A", "A2R", "A2B", "B2A"]
    assert catalog["Read_Override"] == [False, True]
    assert "read_level_upper1" in catalog["Read_Label"]
    assert catalog["WL"] == sorted(catalog["WL"])  # 数値軸はユニーク値の昇順


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
