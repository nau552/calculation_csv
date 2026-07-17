import polars as pl
import pytest

from scorelib.axis_resolve import resolve_axes

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


def test_resolved_axes_match_known_expansion(expanded_mini_dir):
    """Cross-check against FBC_expanded.csv produced by the original
    (non-generalized) expansion script, run on a self-contained copy of
    result_tmp_mini -- ground truth for the join/mapping logic.
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


def test_resolve_only_requested_axes_present(data_dir):
    resolved = resolve_axes(data_dir, "FBC", {"State", "Board"}).collect()
    assert set(resolved.columns) == {"FBC", "State", "Board"}
    assert resolved["State"].dtype == pl.String


def test_generic_type_with_generic_map_axis(data_dir_mini):
    """tR.csv has a Page axis (absent in FBC data); map_Page.csv must be
    discovered generically as map_{axis}.csv.
    """
    resolved = resolve_axes(data_dir_mini, "tR", {"Page", "Read_Label", "Read_Override", "WL"}).collect()
    assert set(resolved.columns) == {"tR", "Page", "Read_Label", "Read_Override", "WL"}
    assert set(resolved["Page"].unique().to_list()) == {"L", "M", "U"}
    assert resolved["Read_Override"].dtype == pl.Boolean
    assert resolved["WL"].dtype == pl.Int64  # no map_WL.csv -> stays numeric


def test_unresolvable_axis_raises(data_dir_mini):
    with pytest.raises(ValueError, match="NoSuchAxis"):
        resolve_axes(data_dir_mini, "tR", {"NoSuchAxis"})
