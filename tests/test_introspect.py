"""scorelib_param.introspect のテスト: 過去実験の出力ディレクトリからの
UI向けメタデータ（type・軸・値候補）の導出。"""
import shutil

import pytest

from scorelib_param.introspect import (
    axis_catalog,
    detect_types,
    find_dvtbudget_coefs,
    find_run_configs,
    measure_labels,
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


ALL_MINI_TYPES = ["FBC", "KLD", "PROGLOOP", "PROGSTATUS", "dVthSGWLD", "tPROG", "tR"]


def test_detect_types(data_dir_mini):
    assert detect_types(data_dir_mini) == ALL_MINI_TYPES


def test_detect_types_ignores_reserved_and_map_files(tmp_path, data_dir_mini):
    d = tmp_path / "run"
    shutil.copytree(data_dir_mini, d)
    (d / "FBC_expanded.csv").write_text("Board,Measure,FBC_expanded\n0,0,1\n")
    (d / "notes.csv").write_text("a,b\n1,2\n")  # no value column named "notes" -> not a type
    assert detect_types(d) == ALL_MINI_TYPES


def test_detect_types_requires_value_column(tmp_path):
    """値列ルール: {t}.csv は自分の名前の値列を持つときだけ type。
    Measure 列があっても値列が無ければ計算できないので type ではない。"""
    (tmp_path / "KLD.csv").write_text("Board,Chip,SGWLD,KLD\n0,0,0,10\n")
    (tmp_path / "broken.csv").write_text("Board,Measure,value\n0,0,1\n")
    assert detect_types(tmp_path) == ["KLD"]


def test_find_dvtbudget_coefs(full_dir, data_dir_mini):
    assert [p.name for p in find_dvtbudget_coefs(full_dir)] == ["dvtbudget_coef.jsonc"]
    assert find_dvtbudget_coefs(data_dir_mini) == []


def test_find_run_configs(full_dir, data_dir_mini):
    assert [p.name for p in find_run_configs(full_dir)] == ["config.jsonc"]
    assert find_run_configs(data_dir_mini) == []


def test_axis_catalog_fbc(data_dir_mini):
    catalog = axis_catalog(data_dir_mini, "FBC")
    # 測定軸は csv ヘッダ順（Measure 含む）、その後にラベル軸 → DataName
    assert list(catalog) == [
        "InBatchEpoch", "Board", "Chip", "Block", "Measure", "WL", "STR", "State",
        "Erase_Label", "Erase_Override", "Program_Label", "Program_Override",
        "Read_Label", "Read_Override", "DataName",
    ]
    assert catalog["State"] == ["R2A", "A2R", "A2B", "B2A"]
    assert catalog["Read_Override"] == [False, True]
    assert "read_level_upper1" in catalog["Read_Label"]
    assert catalog["WL"] == sorted(catalog["WL"])  # 数値軸はユニーク値の昇順
    assert catalog["Measure"] == [0, 1, 2, 3]  # 識別子軸: 実在番号の昇順
    assert catalog["DataName"] == [
        "reference_param_read_level_1", "evaluation_param_read_level_1",
        "reference_param_read_level_2", "evaluation_param_read_level_2",
    ]


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


def test_axis_catalog_progloop_param(data_dir_mini):
    """Measure 無し type（PROGLOOP）: 測定軸のみで、Param は map 由来の
    ROM（基準パラ）/ Opt（提案パラ）が候補。Measure/DataName 軸は出ない。"""
    catalog = axis_catalog(data_dir_mini, "PROGLOOP")
    assert list(catalog) == ["InBatchEpoch", "Board", "Chip", "Block", "Param", "WL", "STR"]
    assert catalog["Param"] == ["ROM", "Opt"]


def test_axis_catalog_hides_all_null_label_axis(data_dir_mini):
    """parameterLabel の全行空欄の列（tPROG の Read_Label）は「この type に
    存在しない設定」なので軸として出さない。値のある Label/Override 軸は出す。"""
    catalog = axis_catalog(data_dir_mini, "tPROG")
    assert "Read_Label" not in catalog
    assert catalog["Erase_Label"] == ["program_read_ref", "proposal"]
    assert catalog["Measure"] == [0, 1]


def test_axis_catalog_without_measure_column(tmp_path):
    """Measure 列の無い（集計済み）type には Measure 軸も DataName 軸も出ない
    （docs/spec_change_dataname_measure.md 6.4節）。"""
    (tmp_path / "SUMMARY.csv").write_text("Board,Chip,SUMMARY\n0,0,1.5\n0,1,2.5\n")
    catalog = axis_catalog(tmp_path, "SUMMARY")
    assert list(catalog) == ["Board", "Chip"]


def test_measure_labels_fbc(data_dir_mini):
    assert measure_labels(data_dir_mini, "FBC") == {
        0: "reference_param_read_level_1",
        1: "evaluation_param_read_level_1",
        2: "reference_param_read_level_2",
        3: "evaluation_param_read_level_2",
    }
    # dVtBudget は FBC のデータを読む
    assert measure_labels(data_dir_mini, "dVtBudget") == measure_labels(data_dir_mini, "FBC")


def test_measure_labels_empty_without_dataname_file(tmp_path, data_dir_mini):
    """dataName_* が無い（ラベル無し実験）では空 = UI は番号のみ表示。"""
    d = tmp_path / "run"
    shutil.copytree(data_dir_mini, d)
    (d / "dataName_tR.csv").unlink()
    assert measure_labels(d, "tR") == {}
