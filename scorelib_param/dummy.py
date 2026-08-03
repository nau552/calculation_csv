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
- initial_temperature.csv: 実機形式(ヘッダあり InBatchEpoch,Board,Temp)・
  旧参照データ形式(ヘッダなし Board,温度)の両方を受け付け(形式判定は
  dvtbudget.parse_initial_temperature に一本化)、**元の形式のまま** Board
  セルだけ書き換えて Board ごとに行を複製する
- map_*.csv(ヘッダ無しの対応表)・その他のファイル(json 等): そのままコピー

逆方向の make_pseudo_dummy は開発・検証用: 実データから Board/Chip を1つに
削り、ダミー一式が納品される前でも同じ経路を試せるようにする。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import polars as pl

from .dvtbudget import parse_initial_temperature

if TYPE_CHECKING:
    from collections.abc import Sequence


def _read_header(path: Path) -> list[str]:
    try:
        return pl.scan_csv(path).collect_schema().names()
    # ヘッダの読めないファイルは [] = Board 列なしとして扱い、複製せずそのままコピーする側に回す
    except Exception:  # ruff: ignore[BLE001]
        return []


def _check_single(n: int, col: str, filename: str) -> None:
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
            header, rows, board_i, _ = parse_initial_temperature(f)
            _check_single(len({r[board_i] for r in rows}), "Board", f.name)
            # 元の形式(ヘッダ・列構成・値の表記)を保ったまま、Board セルだけ
            # 書き換えて Board ごとに複製する(温度はダミーのまま)
            replicated = [
                [str(b) if i == board_i else cell for i, cell in enumerate(r)]
                for b in range(len(chip_counts))
                for r in rows
            ]
            lines = ([",".join(header)] if header is not None else []) + [",".join(r) for r in replicated]
            (dest / f.name).write_text("\n".join(lines) + "\n", encoding="utf-8")
            continue
        if f.name.startswith("map_") or f.suffix != ".csv":
            shutil.copy(f, dest / f.name)
            continue
        cols = _read_header(f)
        if "Board" not in cols:
            shutil.copy(f, dest / f.name)
            continue
        df = pl.read_csv(f)
        _check_single(df["Board"].n_unique(), "Board", f.name)
        has_chip = "Chip" in cols
        if has_chip:
            _check_single(df["Chip"].n_unique(), "Chip", f.name)
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
            header, rows, board_i, _ = parse_initial_temperature(f)
            # 元の形式のまま、最小 Board の行だけ残して Board セルを 0 に書き換える
            min_board = min(int(r[board_i]) for r in rows)
            kept = [
                ["0" if i == board_i else cell for i, cell in enumerate(r)]
                for r in rows
                if int(r[board_i]) == min_board
            ]
            lines = ([",".join(header)] if header is not None else []) + [",".join(r) for r in kept]
            (dest / f.name).write_text("\n".join(lines) + "\n", encoding="utf-8")
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
