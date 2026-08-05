# Copyright (c) 2026
# ruff: file-ignore[implicit-namespace-package] 単体実行スクリプト置き場でパッケージではない(__init__.py を持たない)
import itertools

import pandas as pd


def create_dataframe_with_cyclic_fbc(
    column_ranges: dict[str, tuple[int, int]],
    fbc_max: int = 20,
    output_csv: str = "output.csv",
) -> pd.DataFrame:
    """軸範囲の全組み合わせに 0..fbc_max を巡回する tR 列を足して csv に保存し、DataFrame を返す。

    Returns:
        column_ranges の軸列と巡回 tR 列を持つ DataFrame(output_csv へ保存済みの内容と同じもの)。

    """
    columns = list(column_ranges.keys())
    value_lists = [range(start, end + 1) for start, end in column_ranges.values()]

    combinations = list(itertools.product(*value_lists))
    df = pd.DataFrame(combinations, columns=columns)

    cycle_length = fbc_max + 1
    df["tR"] = [i % cycle_length for i in range(len(df))]

    df.to_csv(output_csv, index=False, encoding="utf-8")
    return df


column_ranges = {
    "InBatchEpoch": (0, 0),
    "Board": (0, 1),
    "Chip": (0, 1),
    "Block": (0, 0),
    "Measure": (0, 1),
    "WL": (0, 5),
    "STR": (0, 2),
    "Page": (0, 2),
    # "DataName": (0, 11),
}

df = create_dataframe_with_cyclic_fbc(column_ranges, fbc_max=13, output_csv="tR_mini.csv")
