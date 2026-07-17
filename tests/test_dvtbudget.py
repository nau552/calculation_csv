import math

import polars as pl
import pytest

from scorelib import io_jsonc
from scorelib.dvtbudget import apply_dvtbudget, load_board_temperatures


def test_load_board_temperatures(data_dir):
    temps = load_board_temperatures(data_dir / "initial_temperature.csv")
    assert temps[0] == pytest.approx(-28.236)
    assert temps[1] == pytest.approx(82.934)


def test_apply_dvtbudget_nearest_temperature_and_formula(dvtbudget_coef_path, data_dir):
    coef = io_jsonc.load_dvtbudget_coef(dvtbudget_coef_path)
    board_temps = load_board_temperatures(data_dir / "initial_temperature.csv")

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
