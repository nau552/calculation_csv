"""スコアパーツが実際に必要とする軸だけを解決する（FBC_expanded.csv のような
全展開 DataFrame は作らない）。

`type_`（例: "FBC"）に対して、`data_dir` に現行の命名規約
（docs/score_gui_design.md 3.1/3.2節）のファイル群があることを期待する:

- ``{type_}.csv``: 測定軸（Board, Chip, Block, WL, STR, State, ...）
  + ``Measure`` + type名の値列（例: ``FBC``）
- ``parameterLabel_{type_}.csv``: ``Measure`` → Erase/Program/Read の
  Label + Override への解決。結合キーは (InBatchEpoch, Board, Chip, Block, Measure)
- ``dataName_{type_}.csv``: ``Measure`` → DataName（数値コード）。結合キー同上
- ``map_*.csv``: 共有の数値→テキスト対応表（2列・ヘッダなし）。
  軸に対応する map ファイルは規約で決まる: ``{Erase,Program,Read}_Label``
  → ``map_Label.csv``、``*_Override`` → ``map_Override.csv``、``DataName`` →
  ``map_dataName.csv``、それ以外の軸 ``X`` はファイルがあれば ``map_X.csv``
  （例: ``State`` → ``map_State.csv``、``Page`` → ``map_Page.csv``）。
  map ファイルの無い軸（WL, STR, Board, ...）は数値のまま。

要求された軸に必要な map ファイルだけを読んで結合する。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import polars as pl

JOIN_KEYS = ["InBatchEpoch", "Board", "Chip", "Block", "Measure"]

OVERRIDE_SUFFIX = "_Override"


def _map_file_for_axis(data_dir: Path, axis: str) -> Path | None:
    if axis.endswith("_Label"):
        name = "map_Label.csv"
    elif axis.endswith(OVERRIDE_SUFFIX):
        name = "map_Override.csv"
    elif axis == "DataName":
        name = "map_dataName.csv"
    else:
        name = f"map_{axis}.csv"
    path = data_dir / name
    return path if path.exists() else None


def _scan_map_file(path: Path, code_col: str, text_col: str) -> pl.LazyFrame:
    return pl.scan_csv(path, has_header=False, new_columns=[code_col, text_col])


def resolve_axes(
    data_dir: str | Path,
    type_: str,
    required_axes: Iterable[str],
) -> pl.LazyFrame:
    """`type_`（値列）+ `required_axes` の各軸を持つ LazyFrame を返す。
    Label/Override/State/DataName などは人間可読なテキストへ解決済み。
    必要なところにだけ join を入れる。
    """
    data_dir = Path(data_dir)
    required_axes = set(required_axes)
    value_col = type_

    lf = pl.scan_csv(data_dir / f"{type_}.csv")
    base_cols = set(lf.collect_schema().names())

    # {type}.csv 自体に無い軸は parameterLabel_{type}.csv（Erase/Program/Read の
    # Label + Override）または dataName_{type}.csv（DataName）から来る。
    missing_axes = required_axes - base_cols

    label_path = data_dir / f"parameterLabel_{type_}.csv"
    if missing_axes - {"DataName"} and label_path.exists():
        label_lf = pl.scan_csv(label_path)
        label_cols = [c for c in label_lf.collect_schema().names() if c not in JOIN_KEYS]
        take = [c for c in label_cols if c in missing_axes]
        if take:
            lf = lf.join(label_lf.select(JOIN_KEYS + take), on=JOIN_KEYS, how="left")

    if "DataName" in missing_axes:
        dn_lf = pl.scan_csv(data_dir / f"dataName_{type_}.csv")
        lf = lf.join(dn_lf.select(JOIN_KEYS + ["DataName"]), on=JOIN_KEYS, how="left")

    unresolvable = required_axes - set(lf.collect_schema().names())
    if unresolvable:
        raise ValueError(
            f"axes {sorted(unresolvable)} not found for type '{type_}' "
            f"(not in {type_}.csv, parameterLabel_{type_}.csv, or dataName_{type_}.csv)"
        )

    # Measure は結合キーとしてのみ使い、軸としては決して公開しない
    if "Measure" in lf.collect_schema().names():
        lf = lf.drop("Measure")

    for axis in sorted(required_axes):
        map_path = _map_file_for_axis(data_dir, axis)
        if map_path is None:
            continue
        code_col, text_col = f"__code_{axis}", f"__text_{axis}"
        map_lf = _scan_map_file(map_path, code_col, text_col)
        lf = lf.join(map_lf, left_on=axis, right_on=code_col, how="left")
        lf = lf.drop(axis).rename({text_col: axis})
        if axis.endswith(OVERRIDE_SUFFIX) and lf.collect_schema()[axis] != pl.Boolean:
            # map_Override.csv のテキスト列は通常 csv リーダが Boolean と自動推論
            # する（TRUE/FALSE リテラル）。テキストのまま来た場合だけ手で正規化
            lf = lf.with_columns(
                pl.col(axis).cast(pl.Utf8).str.to_uppercase().is_in(["TRUE", "1"]).alias(axis)
            )

    # 値列 + 要求された軸だけ残す（要求されていない結合キー等の付随列は落とす）
    keep = [value_col] + sorted(a for a in required_axes if a in lf.collect_schema().names())
    lf = lf.select(keep)
    return lf
