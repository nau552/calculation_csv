"""定義ファイル群から UI 向けメタデータ（type一覧・軸一覧・値候補）を導出する。

Phase1 の前提（docs/score_gui_ui_design.md 5.1節）: 対象ディレクトリは
「同系統の過去実験の出力一式」（result_tmp 相当）で、type・軸・値候補は
実ファイルから読み取れる。将来、現行GUIが将来出力のマニフェストを提供する
ようになったら、このモジュールのデータソースだけを差し替える（関数の
シグネチャは変えない）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import polars as pl

from . import jsonc
from .axis_resolve import JOIN_KEYS, OVERRIDE_SUFFIX, _map_file_for_axis, resolve_axes

# 測定typeとは決してみなさない csv の語幹
_RESERVED_STEMS = {"initial_temperature", "reference_param", "FBC_expanded"}
_TYPE_FILE_PREFIXES = ("parameterLabel_", "dataName_", "map_")


def detect_types(data_dir: str | Path) -> List[str]:
    """ディレクトリに存在する測定type。ファイル命名（parameterLabel_{t}.csv /
    dataName_{t}.csv）と、測定出力らしい素の {t}.csv（Measure 列を持つ）から
    検出する。"""
    data_dir = Path(data_dir)
    types: set[str] = set()
    for f in data_dir.glob("*.csv"):
        stem = f.stem
        if stem.startswith("parameterLabel_"):
            types.add(stem[len("parameterLabel_"):])
        elif stem.startswith("dataName_"):
            types.add(stem[len("dataName_"):])
    for f in data_dir.glob("*.csv"):
        stem = f.stem
        if stem in types or stem in _RESERVED_STEMS or stem.startswith(_TYPE_FILE_PREFIXES):
            continue
        try:
            cols = pl.scan_csv(f).collect_schema().names()
        except Exception:
            continue
        if "Measure" in cols:
            types.add(stem)
    return sorted(types)


def find_dvtbudget_coefs(data_dir: str | Path) -> List[Path]:
    """**中身の形**が dVtBudget 係数表（世代 → 温度 → State → {a, b}）に
    見える jsonc をすべて列挙する。判別はファイル名ではなく形で行う。
    複数マッチしたときの扱いは呼び出し側の責務（UIは黙って選ばずエラーにする）。
    """
    from .io_jsonc import load_dvtbudget_coef

    found = []
    for f in sorted(Path(data_dir).glob("*.jsonc")):
        try:
            coef = load_dvtbudget_coef(f)
        except Exception:
            continue
        if coef.root and all(
            temps and all(states for states in temps.values()) for temps in coef.root.values()
        ):
            # {a, b} のリーフ形が少なくとも1件マッチしていることまで要求する
            found.append(f)
    return found


def find_generation_info(data_dir: str | Path, generation: Optional[str]) -> Optional[Path]:
    """世代ごとのチップ情報json（{Generation}.json: numWLs, numStrings, ...）が
    データディレクトリに置かれていればそのパス。これだけファイル名ベース。"""
    if not generation:
        return None
    p = Path(data_dir) / f"{generation}.json"
    return p if p.is_file() else None


def find_run_configs(data_dir: str | Path) -> List[Path]:
    """トップレベルに optimization{} ブロックを持つ jsonc（sample.jsonc 形）を
    すべて列挙する。find_dvtbudget_coefs と同じく形ベース。2つの形は排他的
    （係数表に "optimization" キーは無く、設定は3段 {a, b} 表の検証に通らない）
    なので取り違えは起きない。"""
    found = []
    for f in sorted(Path(data_dir).glob("*.jsonc")):
        try:
            content = jsonc.load(f)
        except Exception:
            continue
        if isinstance(content, dict) and "optimization" in content:
            found.append(f)
    return found


def axis_catalog(data_dir: str | Path, type_: str) -> Dict[str, Optional[list]]:
    """`type_` のスコアパーツで使える軸の一覧（デフォルト表示順: 測定csvの
    ヘッダ順 → ラベル軸）を、各軸の値候補（None = 自由入力のみ）に対応付ける。

    type_ == "dVtBudget" のときは FBC のカタログを返す（FBC.csv を読むため）。
    """
    data_dir = Path(data_dir)
    source_type = "FBC" if type_ == "dVtBudget" else type_

    measured: List[str] = []
    tcsv = data_dir / f"{source_type}.csv"
    if tcsv.exists():
        cols = pl.scan_csv(tcsv).collect_schema().names()
        measured = [c for c in cols if c not in ("Measure", source_type)]

    label_axes: List[str] = []
    plabel = data_dir / f"parameterLabel_{source_type}.csv"
    if plabel.exists():
        cols = pl.scan_csv(plabel).collect_schema().names()
        label_axes = [c for c in cols if c not in JOIN_KEYS]

    catalog: Dict[str, Optional[list]] = {}
    for axis in measured + [a for a in label_axes if a not in measured]:
        catalog[axis] = _candidates(data_dir, source_type, axis, tcsv if axis in measured else None)
    return catalog


def _candidates(data_dir: Path, source_type: str, axis: str, tcsv: Optional[Path]) -> Optional[list]:
    if axis.endswith(OVERRIDE_SUFFIX):
        # False（非Override=基準測定）を先頭に: 常に存在する側だから
        return [False, True]
    map_path = _map_file_for_axis(data_dir, axis)
    if map_path is not None:
        m = pl.read_csv(map_path, has_header=False, new_columns=["code", "text"])
        full = m["text"].to_list()
        # map の全語彙ではなく、過去データに実在する値を優先する（map順は保持）:
        # 雛形パーツは先頭候補で filter するため、データに無い値を候補に出すと
        # 「filter が0行にマッチ」で雛形が壊れる。照会に失敗したら全語彙に
        # フォールバック。
        try:
            present = set(
                resolve_axes(data_dir, source_type, {axis})
                .select(axis).unique().collect()[axis].to_list()
            )
        except Exception:
            return full
        narrowed = [v for v in full if v in present]
        return narrowed or full
    if tcsv is not None:
        return pl.scan_csv(tcsv).select(axis).unique().sort(axis).collect()[axis].to_list()
    return None
