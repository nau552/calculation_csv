# Copyright (c) 2026
"""dVtBudget 変換。

type == "dVtBudget" のスコアパーツは FBC.csv を読み(axis_resolve)、他の
相対値FBCパーツと同じく相対化(relative.py)された後、Board / State が
まだ列として残っている時点でここで行単位に変換される:

    dVtBudget = -log10(相対値) / b * 1000

`b` は、チップ世代(configの Generation)の係数表から、Board の実測温度
(initial_temperature.csv)に**最も近い温度キー**を選び、State ごとに引く
(docs/score_gui_design.md 3.5節)。`a` は使わない。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import polars as pl

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from .models import DvtBudgetCoefFile


def parse_initial_temperature(path: str | Path) -> tuple[list[str] | None, list[list[str]], int, int]:
    """initial_temperature.csv を形式判定つきで文字列の行列に読む。

    実機の形式はヘッダあり(InBatchEpoch, Board, Temp)。列は名前で拾うので
    列順や余分な列(InBatchEpoch等)は問わない。温度列名は Temp / Temperature
    のどちらでもよい。ヘッダなし2列(Board, 温度)の旧参照データ形式も
    受け付ける(1行目の先頭セルが数値ならヘッダなしと判定)。
    形式の判定はこの関数に一本化してある — 値を読む load_board_temperatures と、
    元の形式を保ったまま行を複製する dummy.py の両方が使う。

    Returns:
        (ヘッダ行(ヘッダなし形式では None), Board セルが空でないデータ行,
        Board の列番号, 温度の列番号)。セルは前後空白を落とした文字列。

    Raises:
        ValueError: ファイルが空の時、ヘッダに Board / Temp(Temperature)列が
            見つからない時、または温度の行が1行も無い時。

    """
    df = pl.read_csv(path, has_header=False, infer_schema=False)
    rows = [[("" if v is None else str(v).strip()) for v in r] for r in df.rows()]
    if not rows:
        msg = f"{path}: empty file"
        raise ValueError(msg)

    header: list[str] | None = None
    try:
        float(rows[0][0])
        board_i, temp_i = 0, 1  # ヘッダなし旧形式
    except ValueError:
        header = rows[0]
        lowered = [h.lower() for h in header]
        rows = rows[1:]

        def find(*names: str) -> int:
            for n in names:
                if n in lowered:
                    return lowered.index(n)
            msg = f"{path}: column {'/'.join(names)} not found in header {lowered}"
            raise ValueError(msg)

        board_i, temp_i = find("board"), find("temp", "temperature")

    rows = [r for r in rows if r[board_i]]
    if not rows:
        msg = f"{path}: no temperature rows"
        raise ValueError(msg)
    return header, rows, board_i, temp_i


def load_board_temperatures(initial_temperature_path: str | Path) -> dict[int, float]:
    """initial_temperature.csv → {Board: 温度}。

    受け付ける形式(実機のヘッダあり / 旧参照データのヘッダなし)は
    parse_initial_temperature 参照。

    Returns:
        Board 番号 → 実測温度(float)の辞書。

    Raises:
        ValueError: parse_initial_temperature が形式を読めない時、または
            同じ Board に矛盾する温度が並んでいる時。

    """
    _, rows, board_i, temp_i = parse_initial_temperature(initial_temperature_path)
    temps: dict[int, float] = {}
    for r in rows:
        board, temp = int(r[board_i]), float(r[temp_i])
        if board in temps and temps[board] != temp:
            msg = f"{initial_temperature_path}: Board {board} has conflicting temperatures {temps[board]} and {temp}"
            raise ValueError(msg)
        temps[board] = temp
    return temps


def _nearest_temp_key(available_keys: list[str], target_temp: float) -> str:
    return min(available_keys, key=lambda k: abs(float(k) - target_temp))


def apply_dvtbudget(  # ruff: ignore[PLR0913] — 公開 API: 多数の省略可能キーワード引数は設計(束ねない方針 — docs/dev_workflow.md)
    lf: pl.LazyFrame,
    value_col: str,
    generation: str,
    coef: DvtBudgetCoefFile,
    board_temperatures: Mapping,
    *,
    epoch_col: str | None = None,
) -> pl.LazyFrame:
    """通常(単一epoch)は `board_temperatures = {Board: 温度}`。

    バッチ計算(scorelib_param.batch)では温度が epoch ごとの
    initial_temperature.csv から来て係数 `b` が epoch で変わりうるため、
    `epoch_col`(識別軸名。例: "Epoch")を指定し
    `board_temperatures = {epoch値: {Board: 温度}}` の2段ネストで渡すと、
    係数対応表を (epoch, Board, State) キーで引く。

    Returns:
        `value_col` を dVtBudget 値(-log10(相対値) / b * 1000)に置き換えた
        LazyFrame。係数の作業列は落としてある。

    Raises:
        ValueError: Board / State(epoch_col 指定時はその列も)がすでに
            集計で潰れていて `lf` に残っていない時、または係数表に
            `generation` のエントリが無い時。

    """
    schema_cols = lf.collect_schema().names()
    needed = {"Board", "State"} | ({epoch_col} if epoch_col else set())
    missing = needed - set(schema_cols)
    if missing:
        msg = (
            f"dVtBudget conversion needs columns {sorted(missing)} still present; "
            "aggregate Board/State only after the __dvtbudget__ step"
        )
        raise ValueError(msg)

    if generation not in coef.root:
        msg = f"dvtbudget_coef has no entries for generation '{generation}' (available: {sorted(coef.root)})"
        raise ValueError(msg)
    gen_coefs = coef.root[generation]
    temp_keys = list(gen_coefs.keys())

    # (Board, State)(epoch別温度なら (epoch, Board, State))→ b の
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

    join_keys = ([epoch_col] if epoch_col else []) + ["Board", "State"]
    coef_lf = pl.DataFrame(rows).lazy()
    lf = lf.join(coef_lf, on=join_keys, how="left")
    converted = -(pl.col(value_col).log10()) / pl.col("__b__") * 1000
    # 係数・温度の照会に失敗した行(__b__ が null)は null になる。後段の集計が
    # null を黙って除外して「エラーなしで値がズレる」ため、NaN に変えて最終
    # collapse まで伝播させる(原因は compute_score_part が診断する)
    return lf.with_columns(converted.fill_null(float("nan")).alias(value_col)).drop("__b__")
