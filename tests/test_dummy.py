"""scorelib_param.dummy のテスト: ダミー一式の Board/Chip 複製展開と
正データの疑似ダミー化（docs/spec_change_dataname_measure.md 9節・プラン4）。"""
import math

import polars as pl
import pytest

from scorelib_param.cli import compute_score_part
from scorelib_param.dummy import expand_boards_chips, make_pseudo_dummy
from scorelib_param.introspect import axis_catalog, detect_types
from scorelib_param.models import ScorePart


@pytest.fixture
def pseudo_dir(tmp_path, data_dir_mini):
    return make_pseudo_dummy(data_dir_mini, tmp_path / "pseudo")


def _relative_measure_part() -> ScorePart:
    tail = ["WL", "STR", "State", "Board", "Chip", "Block"]
    return ScorePart.model_validate({
        "name": "p", "type": "FBC",
        "relative": {"split_axis": "Measure", "numerator_when": 1,
                     "denominator_when": 0, "denominator_offset": 1},
        "order": tail,
        "aggregations": {a: {"op": "mean"} for a in tail},
    })


class TestMakePseudoDummy:
    def test_keeps_single_board_chip(self, pseudo_dir, data_dir_mini):
        df = pl.read_csv(pseudo_dir / "FBC.csv")
        assert df["Board"].unique().to_list() == [0]
        assert df["Chip"].unique().to_list() == [0]
        # mini は Board 2 × Chip 2 → 1/4 に削れる
        assert df.height == pl.read_csv(data_dir_mini / "FBC.csv").height // 4

    def test_all_files_present_and_types_detected(self, pseudo_dir, data_dir_mini):
        assert {p.name for p in pseudo_dir.iterdir()} == {
            p.name for p in data_dir_mini.iterdir()
        }
        assert detect_types(pseudo_dir) == detect_types(data_dir_mini)

    def test_initial_temperature_single_row(self, pseudo_dir):
        temps = pl.read_csv(pseudo_dir / "initial_temperature.csv", has_header=False)
        assert temps.height == 1


class TestExpandBoardsChips:
    def test_row_counts_and_numbering(self, pseudo_dir, tmp_path):
        out = expand_boards_chips(pseudo_dir, tmp_path / "out", [1, 3])
        df = pl.read_csv(out / "FBC.csv")
        base = pl.read_csv(pseudo_dir / "FBC.csv").height
        assert df.height == base * (1 + 3)
        assert df["Board"].unique().sort().to_list() == [0, 1]
        # Board ごとに違う Chip 数
        assert df.filter(pl.col("Board") == 0)["Chip"].unique().to_list() == [0]
        assert df.filter(pl.col("Board") == 1)["Chip"].unique().sort().to_list() == [0, 1, 2]
        # 結合キーが揃うファイルも同じ形で展開されている
        for name in ("parameterLabel_FBC.csv", "dataName_FBC.csv"):
            sub = pl.read_csv(out / name)
            assert sub["Board"].unique().sort().to_list() == [0, 1]

    def test_initial_temperature_expanded_per_board(self, pseudo_dir, tmp_path):
        out = expand_boards_chips(pseudo_dir, tmp_path / "out", [2, 2, 2])
        temps = pl.read_csv(out / "initial_temperature.csv", has_header=False,
                            new_columns=["Board", "Temperature"])
        assert temps["Board"].to_list() == [0, 1, 2]
        assert temps["Temperature"].n_unique() == 1

    def test_map_files_copied_verbatim(self, pseudo_dir, tmp_path):
        out = expand_boards_chips(pseudo_dir, tmp_path / "out", [2])
        assert (out / "map_DataName.csv").read_bytes() == (
            pseudo_dir / "map_DataName.csv"
        ).read_bytes()

    def test_rejects_multi_board_source(self, data_dir_mini, tmp_path):
        with pytest.raises(ValueError, match="single Board"):
            expand_boards_chips(data_dir_mini, tmp_path / "out", [2, 2])

    def test_rejects_bad_chip_counts(self, pseudo_dir, tmp_path):
        with pytest.raises(ValueError, match="positive chip count"):
            expand_boards_chips(pseudo_dir, tmp_path / "out", [])
        with pytest.raises(ValueError, match="positive chip count"):
            expand_boards_chips(pseudo_dir, tmp_path / "out", [2, 0])

    def test_expanded_bundle_computes_and_mean_is_replication_invariant(
        self, pseudo_dir, tmp_path
    ):
        """展開一式で計算が通る（構造テストの実体）。mean 集計は複製に対して
        不変なので、展開前後で同じ値になるはず — 展開が行の複製「だけ」を
        していることの強い検証。"""
        out = expand_boards_chips(pseudo_dir, tmp_path / "out", [2, 3])
        part = _relative_measure_part()
        v_expanded = compute_score_part(out, part)
        v_pseudo = compute_score_part(pseudo_dir, part)
        assert math.isfinite(v_expanded)
        assert math.isclose(v_expanded, v_pseudo, rel_tol=1e-12)

    def test_expanded_catalog_shows_new_boards(self, pseudo_dir, tmp_path):
        out = expand_boards_chips(pseudo_dir, tmp_path / "out", [2, 2])
        catalog = axis_catalog(out, "FBC")
        assert catalog["Board"] == [0, 1]
        assert catalog["Chip"] == [0, 1]
        assert catalog["Measure"] == [0, 1, 2, 3]
