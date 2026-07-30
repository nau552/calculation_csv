# Copyright (c) 2026
"""ダミー一式の Board/Chip 展開と、正データの疑似ダミー化。

docs/spec_change_dataname_measure.md 9節: 測定フローは Board/Chip を知らない
ため、ダミー一式(result_tmp 相当・測定値のみダミー)は Board/Chip とも1つで
出力される。スコア設計 UI は入力された「Board 数・Board ごとの Chip 数」に
従って行を複製展開し、構造テスト可能な一式を作る(測定値はダミーのまま =
テスト結果の数値に意味は無い。展開は行の複製であり数値は作らない)。

ファイルごとの扱い:
- ヘッダに Board 列を持つ csv({type}.csv / parameterLabel_* / dataName_*):
  行を Board(* Chip 列があれば Chip)で複製
- initial_temperature.csv(ヘッダ無しの Board,温度): Board ごとに1行へ複製
- map_*.csv(ヘッダ無しの対応表)・その他のファイル(json 等): そのままコピー

逆方向の make_pseudo_dummy は開発・検証用: 実データから Board/Chip を1つに
削り、ダミー一式が納品される前でも同じ経路を試せるようにする。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import polars as pl

if TYPE_CHECKING:
    from collections.abc import Sequence


def _read_header(path: Path) -> list[str]:
    try:
        return pl.scan_csv(path).collect_schema().names()
    # ヘッダの読めないファイルは [] = Board 列なしとして扱い、複製せずそのままコピーする側に回す
    except Exception:  # ruff: ignore[BLE001]
        return []


def _check_single(df: pl.DataFrame, col: str, filename: str) -> None:
    n = df[col].n_unique()
    if n > 1:
        msg = (
            f"{filename}: expected a dummy bundle with a single {col} "
            f"(found {n} distinct values) — expand_boards_chips replicates rows, "
            "so the source must not already contain multiple boards/chips"
        )
        raise ValueError(msg)


def expand_boards_chips(src_dir: str | Path, dest_dir: str | Path, chip_counts: Sequence[int]) -> Path:
    """ダミー一式 `src_dir` を Board/Chip 複製展開して `dest_dir` に書き出す。

    `chip_counts[b]` = Board b の Chip 数(長さ = Board 数。Board ごとに違う
    Chip 数を許す)。Board 番号・Chip 番号とも 0 始まり連番になる。

    Returns:
        展開後の一式を書き出したディレクトリ(`dest_dir` を Path にしたもの)。

    Raises:
        ValueError: `src_dir` がディレクトリとして存在しない時、`chip_counts` が
            空か 1 未満の値を含む時、または複製元の csv がすでに複数の
            Board/Chip を含んでいる時。

    """
    src = Path(src_dir)
    dest = Path(dest_dir)
    if not src.is_dir():
        msg = f"dummy bundle directory not found: {src}"
        raise ValueError(msg)
    chip_counts = [int(n) for n in chip_counts]
    if not chip_counts or any(n < 1 for n in chip_counts):
        msg = f"chip_counts must be one positive chip count per board, got {chip_counts}"
        raise ValueError(msg)
    dest.mkdir(parents=True, exist_ok=True)

    for f in sorted(p for p in src.iterdir() if p.is_file()):
        if f.name == "initial_temperature.csv":
            temps = pl.read_csv(f, has_header=False, new_columns=["Board", "Temperature"])
            # ダミーの温度1行を全 Board に配る(値はダミーのまま)
            temp = temps["Temperature"][0]
            out = pl.DataFrame({"Board": list(range(len(chip_counts))), "Temperature": [temp] * len(chip_counts)})
            out.write_csv(dest / f.name, include_header=False)
            continue
        if f.name.startswith("map_") or f.suffix != ".csv":
            shutil.copy(f, dest / f.name)
            continue
        cols = _read_header(f)
        if "Board" not in cols:
            shutil.copy(f, dest / f.name)
            continue
        df = pl.read_csv(f)
        _check_single(df, "Board", f.name)
        has_chip = "Chip" in cols
        if has_chip:
            _check_single(df, "Chip", f.name)
        parts = []
        for b, n_chips in enumerate(chip_counts):
            board_lit = pl.lit(b).cast(df.schema["Board"]).alias("Board")
            if has_chip:
                parts.extend(
                    df.with_columns(board_lit, pl.lit(c).cast(df.schema["Chip"]).alias("Chip")) for c in range(n_chips)
                )
            else:
                parts.append(df.with_columns(board_lit))
        pl.concat(parts).write_csv(dest / f.name)
    return dest


def make_pseudo_dummy(src_dir: str | Path, dest_dir: str | Path) -> Path:
    """実データ一式から Board/Chip を1つへ削った疑似ダミーを `dest_dir` に書き出す(開発・検証用)。

    Board/Chip とも最小番号の行のみ残し、番号は 0 に正規化する。

    Returns:
        疑似ダミー一式を書き出したディレクトリ(`dest_dir` を Path にしたもの)。

    Raises:
        ValueError: `src_dir` がディレクトリとして存在しない時。

    """
    src = Path(src_dir)
    dest = Path(dest_dir)
    if not src.is_dir():
        msg = f"data directory not found: {src}"
        raise ValueError(msg)
    dest.mkdir(parents=True, exist_ok=True)

    for f in sorted(p for p in src.iterdir() if p.is_file()):
        if f.name == "initial_temperature.csv":
            temps = pl.read_csv(f, has_header=False, new_columns=["Board", "Temperature"])
            pl.DataFrame({"Board": [0], "Temperature": [temps["Temperature"][0]]}).write_csv(
                dest / f.name, include_header=False
            )
            continue
        if f.name.startswith("map_") or f.suffix != ".csv":
            shutil.copy(f, dest / f.name)
            continue
        cols = _read_header(f)
        if "Board" not in cols:
            shutil.copy(f, dest / f.name)
            continue
        df = pl.read_csv(f)
        df = df.filter(pl.col("Board") == df["Board"].min()).with_columns(
            pl.lit(0).cast(df.schema["Board"]).alias("Board")
        )
        if "Chip" in cols:
            df = df.filter(pl.col("Chip") == df["Chip"].min()).with_columns(
                pl.lit(0).cast(df.schema["Chip"]).alias("Chip")
            )
        df.write_csv(dest / f.name)
    return dest
