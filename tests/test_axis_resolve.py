# Copyright (c) 2026
import shutil
from pathlib import Path

import polars as pl
import pytest

from scorelib_param.axis_resolve import resolve_axes

REQUIRED_AXES = {
    "Erase_Label",
    "Erase_Override",
    "Program_Label",
    "Program_Override",
    "Read_Label",
    "Read_Override",
    "DataName",
    "WL",
    "STR",
    "State",
    "Board",
    "Chip",
    "Block",
}


def test_resolved_axes_match_known_expansion(expanded_mini_dir: Path) -> None:
    """結合・map解決ロジックを現行スクリプトの正解データと突き合わせて検証する。

    現行の(一般化前の)展開スクリプトを result_tmp_mini の自己完結コピーに
    実行して作った FBC_expanded.csv と突き合わせる — 結合・map解決ロジックの
    正解データ。
    """
    data_dir = expanded_mini_dir
    expected = pl.read_csv(data_dir / "FBC_expanded.csv")
    expected = expected.with_columns(
        [pl.col(c).cast(pl.Boolean) for c in ("Erase_Override", "Program_Override", "Read_Override")]
    )

    resolved = resolve_axes(data_dir, "FBC", REQUIRED_AXES).collect()

    join_cols = [
        "Board",
        "Chip",
        "Block",
        "WL",
        "STR",
        "State",
        "Erase_Label",
        "Erase_Override",
        "Program_Label",
        "Program_Override",
        "Read_Label",
        "Read_Override",
        "DataName",
    ]
    merged = resolved.join(expected, on=join_cols, how="inner", suffix="_expected")

    assert resolved.height == expected.height
    assert merged.height == resolved.height
    assert (merged["FBC"] == merged["FBC_expected"]).all()


def test_resolve_only_requested_axes_present(data_dir_mini: Path) -> None:
    """要求した軸だけが列として残ることを検証する。"""
    resolved = resolve_axes(data_dir_mini, "FBC", {"State", "Board"}).collect()
    assert set(resolved.columns) == {"FBC", "State", "Board"}
    assert resolved["State"].dtype == pl.String


def test_generic_type_with_generic_map_axis(data_dir_mini: Path) -> None:
    """tR.csv has a Page axis (absent in FBC data); map_Page.csv must be discovered generically as map_{axis}.csv."""
    resolved = resolve_axes(data_dir_mini, "tR", {"Page", "Read_Label", "Read_Override", "WL"}).collect()
    assert set(resolved.columns) == {"tR", "Page", "Read_Label", "Read_Override", "WL"}
    assert set(resolved["Page"].unique().to_list()) == {"L", "M", "U"}
    assert resolved["Read_Override"].dtype == pl.Boolean
    assert resolved["WL"].dtype == pl.Int64  # map_WL.csv が無い → 数値のまま


def test_unresolvable_axis_raises(data_dir_mini: Path) -> None:
    """解決できない軸名がエラーになることを検証する。"""
    with pytest.raises(ValueError, match="NoSuchAxis"):
        resolve_axes(data_dir_mini, "tR", {"NoSuchAxis"})


def test_measure_kept_when_requested(data_dir_mini: Path) -> None:
    """Measure は識別子軸として要求されたら残す(docs/spec_change_dataname_measure.md)。"""
    resolved = resolve_axes(data_dir_mini, "FBC", {"Measure", "Board"}).collect()
    assert set(resolved.columns) == {"FBC", "Measure", "Board"}
    assert resolved["Measure"].dtype == pl.Int64
    assert set(resolved["Measure"].unique().to_list()) == {0, 1, 2, 3}


def test_measure_dropped_when_not_requested(data_dir_mini: Path) -> None:
    """要求されない Measure 列は残らないことを検証する。"""
    resolved = resolve_axes(data_dir_mini, "FBC", {"Board"}).collect()
    assert "Measure" not in resolved.columns


def test_missing_dataname_file_gives_clear_error(tmp_path: Path, data_dir_mini: Path) -> None:
    """dataName_* が無いのに DataName を要求すると軸名つきの ValueError になることを検証する。

    以前は存在確認なしで scan していたため、ファイル欠落時は polars の
    FileNotFoundError がパーツ名の名指しなしで漏れていた(compute_score_file の
    全件収集 (ValueError/TypeError) にも捕まらない型)。parameterLabel 側と同じ
    「解決できない軸」の扱いに揃える。
    """
    d = tmp_path / "run"
    shutil.copytree(data_dir_mini, d)
    (d / "dataName_FBC.csv").unlink()
    with pytest.raises(ValueError, match="DataName"):
        resolve_axes(d, "FBC", {"DataName", "Board"})


def test_measure_with_label_axes(data_dir_mini: Path) -> None:
    """ParameterLabel の join 後も Measure が生き残ること。

    (Measure は結合キーそのものなので、join に消費されても列として残る必要がある)。
    """
    resolved = resolve_axes(data_dir_mini, "FBC", {"Measure", "Read_Label", "Read_Override", "DataName"}).collect()
    assert set(resolved.columns) == {"FBC", "Measure", "Read_Label", "Read_Override", "DataName"}
    # mini データの構造: Measure 0/1 = upper1 の 基準/評価、2/3 = lower1 の 基準/評価
    pairs = set(resolved.select(["Measure", "Read_Label", "Read_Override"]).unique().iter_rows())
    assert pairs == {
        (0, "read_level_upper1", False),
        (1, "read_level_upper1", True),
        (2, "read_level_lower1", False),
        (3, "read_level_lower1", True),
    }
