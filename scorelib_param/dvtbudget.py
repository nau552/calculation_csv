"""dVtBudget 変換。

type == "dVtBudget" のスコアパーツは FBC.csv を読み（axis_resolve）、他の
相対値FBCパーツと同じく相対化（relative.py）された後、Board / State が
まだ列として残っている時点でここで行単位に変換される:

    dVtBudget = -log10(相対値) / b * 1000

`b` は、チップ世代（configの Generation）の係数表から、Board の実測温度
（initial_temperature.csv）に**最も近い温度キー**を選び、State ごとに引く
（docs/score_gui_design.md 3.5節）。`a` は使わない。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Mapping

import polars as pl

from .models import DvtBudgetCoefFile


def load_board_temperatures(initial_temperature_path: str | Path) -> Dict[int, float]:
    """initial_temperature.csv → {Board: 温度}。

    実機の形式はヘッダあり（InBatchEpoch, Board, Temp）。列は名前で拾うので
    列順や余分な列（InBatchEpoch等）は問わない。温度列名は Temp / Temperature
    のどちらでもよい。ヘッダなし2列（Board, 温度）の旧参照データ形式も
    受け付ける（1行目の先頭セルが数値ならヘッダなしと判定）。
    """
    df = pl.read_csv(initial_temperature_path, has_header=False, infer_schema=False)
    rows = [[("" if v is None else str(v).strip()) for v in r] for r in df.rows()]
    if not rows:
        raise ValueError(f"{initial_temperature_path}: empty file")

    try:
        float(rows[0][0])
        board_i, temp_i = 0, 1  # ヘッダなし旧形式
    except ValueError:
        header = [h.lower() for h in rows[0]]
        rows = rows[1:]

        def find(*names: str) -> int:
            for n in names:
                if n in header:
                    return header.index(n)
            raise ValueError(
                f"{initial_temperature_path}: column {'/'.join(names)} not found "
                f"in header {header}"
            )

        board_i, temp_i = find("board"), find("temp", "temperature")

    temps: Dict[int, float] = {}
    for r in rows:
        if not r[board_i]:
            continue
        board, temp = int(r[board_i]), float(r[temp_i])
        if board in temps and temps[board] != temp:
            raise ValueError(
                f"{initial_temperature_path}: Board {board} has conflicting "
                f"temperatures {temps[board]} and {temp}"
            )
        temps[board] = temp
    if not temps:
        raise ValueError(f"{initial_temperature_path}: no temperature rows")
    return temps


def _nearest_temp_key(available_keys, target_temp: float) -> str:
    return min(available_keys, key=lambda k: abs(float(k) - target_temp))


def apply_dvtbudget(
    lf: pl.LazyFrame,
    value_col: str,
    generation: str,
    coef: DvtBudgetCoefFile,
    board_temperatures: Mapping,
    epoch_col: str | None = None,
) -> pl.LazyFrame:
    """通常（単一epoch）は `board_temperatures = {Board: 温度}`。

    バッチ計算（scorelib_param.batch）では温度が epoch ごとの
    initial_temperature.csv から来て係数 `b` が epoch で変わりうるため、
    `epoch_col`（識別軸名。例: "Epoch"）を指定し
    `board_temperatures = {epoch値: {Board: 温度}}` の2段ネストで渡すと、
    係数対応表を (epoch, Board, State) キーで引く。
    """
    schema_cols = lf.collect_schema().names()
    needed = {"Board", "State"} | ({epoch_col} if epoch_col else set())
    missing = needed - set(schema_cols)
    if missing:
        raise ValueError(
            f"dVtBudget conversion needs columns {sorted(missing)} still present; "
            "aggregate Board/State only after the __dvtbudget__ step"
        )

    gen_coefs = coef.root[generation]
    temp_keys = list(gen_coefs.keys())

    # (Board, State)（epoch別温度なら (epoch, Board, State)）→ b の
    # 小さな対応表を作って結合する
    per_epoch = {None: board_temperatures} if epoch_col is None else board_temperatures
    rows = []
    for epoch_value, temps in per_epoch.items():
        for board, temp in temps.items():
            nearest = _nearest_temp_key(temp_keys, temp)
            for state, entry in gen_coefs[nearest].items():
                row = {"Board": board, "State": state, "__b__": entry.b}
                if epoch_col is not None:
                    row[epoch_col] = epoch_value
                rows.append(row)

    join_keys = (([epoch_col] if epoch_col else []) + ["Board", "State"])
    coef_lf = pl.DataFrame(rows).lazy()
    lf = lf.join(coef_lf, on=join_keys, how="left")
    lf = lf.with_columns(
        (-(pl.col(value_col).log10()) / pl.col("__b__") * 1000).alias(value_col)
    ).drop("__b__")
    return lf
