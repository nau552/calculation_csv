# Copyright (c) 2026
import itertools
import random

import pandas as pd

# 独立なカラム
column_ranges = {
    "InBatchEpoch": (0, 0),
    "Board": (0, 1),
    "Chip": (0, 1),
    "Block": (0, 1),
    "Erase_Label": (0, 0),
    "Erase_Override": (0, 0),
    "Program_Label": (0, 0),
    "Program_Override": (0, 0),
}

# Measureに対応する値
measure_table = [
    {"Measure": 0, "Read_Label": 1, "Read_Override": 0},
    {"Measure": 1, "Read_Label": 1, "Read_Override": 1},
    {"Measure": 2, "Read_Label": 2, "Read_Override": 0},
    {"Measure": 3, "Read_Label": 2, "Read_Override": 1},
    {"Measure": 4, "Read_Label": 3, "Read_Override": 0},
    {"Measure": 5, "Read_Label": 3, "Read_Override": 1},
    {"Measure": 6, "Read_Label": 4, "Read_Override": 0},
    {"Measure": 7, "Read_Label": 4, "Read_Override": 1},
    {"Measure": 8, "Read_Label": 5, "Read_Override": 0},
    {"Measure": 9, "Read_Label": 5, "Read_Override": 1},
    {"Measure": 10, "Read_Label": 6, "Read_Override": 0},
    {"Measure": 11, "Read_Label": 6, "Read_Override": 1},
]

rows = []

# 独立カラムの全組み合わせ
base_columns = list(column_ranges.keys())
base_values = [range(start, end + 1) for start, end in column_ranges.values()]

for base_combo in itertools.product(*base_values):
    base_row = dict(zip(base_columns, base_combo, strict=False))

    # Measure関連を展開
    for measure_info in measure_table:
        row = {
            **base_row,
            **measure_info,
            "FBC": random.randint(0, 200),
        }

        rows.append(row)

df = pd.DataFrame(rows)

df.to_csv("fbc_data.csv", index=False)

print(f"{len(df):,} rows generated")
