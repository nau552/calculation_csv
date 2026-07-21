import math

import polars as pl
import pytest

from scorelib_param import io_jsonc
from scorelib_param.dvtbudget import apply_dvtbudget, load_board_temperatures


def _write_temps(tmp_path):
    """実データの温度と同じ内容の initial_temperature.csv をその場で作る
    （リポジトリに登録しない実データディレクトリへの依存を避ける）。"""
    p = tmp_path / "initial_temperature.csv"
    p.write_text("0,-28.236\n1,82.934\n", encoding="utf-8")
    return p


def test_load_board_temperatures(tmp_path):
    temps = load_board_temperatures(_write_temps(tmp_path))
    assert temps[0] == pytest.approx(-28.236)
    assert temps[1] == pytest.approx(82.934)


def test_load_board_temperatures_real_header_format(tmp_path):
    # 実機の形式: ヘッダあり・InBatchEpoch 列つき・温度列名は Temp
    p = tmp_path / "initial_temperature.csv"
    p.write_text(
        "InBatchEpoch, Board, Temp\n0,0,-28.236\n0,1,82.934\n", encoding="utf-8"
    )
    assert load_board_temperatures(p) == load_board_temperatures(_write_temps(tmp_path))


def test_load_board_temperatures_header_temperature_column(tmp_path):
    p = tmp_path / "initial_temperature.csv"
    p.write_text("Board,Temperature\n3,25.0\n", encoding="utf-8")
    assert load_board_temperatures(p) == {3: 25.0}


def test_load_board_temperatures_header_missing_column(tmp_path):
    p = tmp_path / "initial_temperature.csv"
    p.write_text("InBatchEpoch,Board,Humidity\n0,0,50\n", encoding="utf-8")
    with pytest.raises(ValueError, match="temp"):
        load_board_temperatures(p)


def test_load_board_temperatures_conflicting_rows(tmp_path):
    p = tmp_path / "initial_temperature.csv"
    p.write_text("Board,Temp\n0,25.0\n0,30.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="conflicting"):
        load_board_temperatures(p)


def test_apply_dvtbudget_nearest_temperature_and_formula(dvtbudget_coef_path, tmp_path):
    coef = io_jsonc.load_dvtbudget_coef(dvtbudget_coef_path)
    board_temps = load_board_temperatures(_write_temps(tmp_path))

    lf = pl.LazyFrame(
        {
            "Board": [0, 1],
            "State": ["R2A", "R2A"],
            "value": [2.0, 2.0],
        }
    )
    out = apply_dvtbudget(lf, "value", "B9LS", coef, board_temps).collect()
    rows = {r["Board"]: r["value"] for r in out.to_dicts()}

    # b values are read from the coef fixture rather than hardcoded, so the
    # fixture's numbers can be swapped for real ones without breaking this
    # test; what is asserted is the nearest-temperature selection (Board 0 at
    # -28.236C -> key "-30", Board 1 at 82.934C -> key "85") and the formula.
    gen = coef.root["B9LS"]
    b_low = gen["-30"]["R2A"].b
    assert rows[0] == pytest.approx(-math.log10(2.0) / b_low * 1000)

    b_high = gen["85"]["R2A"].b
    assert rows[1] == pytest.approx(-math.log10(2.0) / b_high * 1000)
    assert b_low != b_high, "fixture must have distinct coefs to prove temperature selection"
