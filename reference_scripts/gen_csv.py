# Copyright (c) 2026
import itertools
import random

import pandas as pd


def generate_csv(
    column_ranges: dict[str, tuple[int, int]],
    random_column: str,
    random_range: tuple[int, int],
    output_file: str,
) -> None:
    """軸範囲の全組み合わせにランダム値列を足した csv を生成する。

    column_ranges:
        {
            "A": (0, 1),  # 0~1
            "B": (0, 5),  # 0~5
            ...
        }

    random_column:
        ランダム値を入れる列名

    random_range:
        (min, max)
    """
    columns = list(column_ranges.keys())

    value_lists = [range(start, end + 1) for start, end in column_ranges.values()]

    rows = []
    for values in itertools.product(*value_lists):
        row = dict(zip(columns, values, strict=False))
        row[random_column] = random.randint(*random_range)
        rows.append(row)

    df = pd.DataFrame(rows)
    df.to_csv(output_file, index=False)

    print(f"{len(df):,} rows saved to {output_file}")


generate_csv(
    column_ranges={
        "InBatchEpoch": (0, 0),
        "Board": (0, 1),
        "Chip": (0, 7),
        "Block": (0, 1),
        # "Param": (0, 1),
        "Measure": (0, 1),
        "WL": (0, 119),
        "STR": (0, 5),
        # "SGWLD": (0,11),
        # "State": (0, 13),
        # "DataName": (0, 11),
    },
    random_column="tPROG",
    random_range=(750, 950),
    # output_file="./tests/data/result_tmp_mini/dVthSGWLD.csv"
    output_file="./result_tmp_full_2/tPROG.csv",
)
