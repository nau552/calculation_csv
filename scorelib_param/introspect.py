# Copyright (c) 2026
"""定義ファイル群から UI 向けメタデータ(type一覧・軸一覧・値候補)を導出する。

Phase1 の前提(docs/score_gui_ui_design.md 5.1節): 対象ディレクトリは
「同系統の過去実験の出力一式」(result_tmp 相当)で、type・軸・値候補は
実ファイルから読み取れる。将来、現行GUIが将来出力のマニフェストを提供する
ようになったら、このモジュールのデータソースだけを差し替える(関数の
シグネチャは変えない)。
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from . import jsonc
from .axis_resolve import JOIN_KEYS, OVERRIDE_SUFFIX, _map_file_for_axis, data_file, resolve_axes

# 測定typeとは決してみなさない csv の語幹
_RESERVED_STEMS = {"initial_temperature", "reference_param", "FBC_expanded"}
_TYPE_FILE_PREFIXES = ("parameterLabel_", "dataName_", "map_")


def _csv_stem(path: Path) -> str:
    """`FBC.csv` と `FBC.csv.gz` のどちらからも語幹 `FBC` を取り出す。

    Returns:
        拡張子(.csv / .csv.gz)を除いたファイル名。

    """
    if path.name.endswith(".csv.gz"):
        return path.name[: -len(".csv.gz")]
    return path.stem


def _csv_files(data_dir: Path) -> list[Path]:
    """走査対象の測定 csv 一覧(gzip 単体圧縮も含む。エンジンの data_file と同じ扱い)。

    Returns:
        `*.csv` と `*.csv.gz` のパスのソート済みリスト。

    """
    return sorted({*data_dir.glob("*.csv"), *data_dir.glob("*.csv.gz")})


def detect_types(data_dir: str | Path) -> list[str]:
    """ディレクトリに存在する測定type。

    ファイル命名(parameterLabel_{t}.csv / dataName_{t}.csv)と、測定出力
    らしい素の {t}.csv(ファイル名と同名の値列を持つ)から検出する。
    gzip 単体圧縮(.csv.gz)もエンジン(axis_resolve.data_file)と同様に対象。

    値列ルールは Measure 列の無い type(KLD / PROGLOOP など)も拾うために
    2026-07-29 に「Measure 列を持つ」から置き換えた: エンジン(resolve_axes)は
    値列 = type 名を前提とするので、値列を持たない csv は検出しても計算できない。

    Returns:
        検出した測定 type 名のソート済みリスト。

    """
    data_dir = Path(data_dir)
    types: set[str] = set()
    files = _csv_files(data_dir)  # .csv.gz も対象(エンジンは読めるのに UI だけ空になる非対称の解消)
    for f in files:
        stem = _csv_stem(f)
        if stem.startswith("parameterLabel_"):
            types.add(stem[len("parameterLabel_") :])
        elif stem.startswith("dataName_"):
            types.add(stem[len("dataName_") :])
    for f in files:
        stem = _csv_stem(f)
        if stem in types or stem in _RESERVED_STEMS or stem.startswith(_TYPE_FILE_PREFIXES):
            continue
        try:
            cols = pl.scan_csv(f).collect_schema().names()
        # ヘッダの読めない csv は測定 type 候補から静かに外し、残りの走査を続ける設計
        except Exception:  # ruff: ignore[S112, BLE001]
            continue
        if stem in cols:
            types.add(stem)
    return sorted(types)


def find_dvtbudget_coefs(data_dir: str | Path) -> list[Path]:
    """**中身の形**が dVtBudget 係数表に見える jsonc をすべて列挙する。

    係数表の形は 世代 → 温度 → State → {a, b}。判別はファイル名ではなく形で
    行う。複数マッチしたときの扱いは呼び出し側の責務(UIは黙って選ばず
    エラーにする)。

    Returns:
        係数表として読めた jsonc ファイルのパスのリスト(ファイル名順)。

    """
    # io_jsonc は pydantic モデル(models)を引き込む。係数表の判別を使うときだけ読み込み、import を軽く保つ
    from .io_jsonc import load_dvtbudget_coef  # ruff: ignore[PLC0415]

    found = []
    for f in sorted(Path(data_dir).glob("*.jsonc")):
        try:
            coef = load_dvtbudget_coef(f)
        # 係数表の形で読めない jsonc は候補から静かに外し、残りの走査を続ける設計
        except Exception:  # ruff: ignore[S112, BLE001]
            continue
        if coef.root and all(temps and all(states for states in temps.values()) for temps in coef.root.values()):
            # {a, b} のリーフ形が少なくとも1件マッチしていることまで要求する
            found.append(f)
    return found


def find_generation_info(data_dir: str | Path, generation: str | None) -> Path | None:
    """世代ごとのチップ情報json がデータディレクトリに置かれていればそのパス。

    対象は {Generation}.json(numWLs, numStrings, ...)。これだけファイル名ベース。

    Returns:
        存在すれば {Generation}.json のパス。generation 未指定または
        ファイルが無ければ None。

    """
    if not generation:
        return None
    p = Path(data_dir) / f"{generation}.json"
    return p if p.is_file() else None


def find_run_configs(data_dir: str | Path) -> list[Path]:
    """トップレベルに optimization{} ブロックを持つ jsonc(sample.jsonc 形)をすべて列挙する。

    find_dvtbudget_coefs と同じく形ベース。2つの形は排他的
    (係数表に "optimization" キーは無く、設定は3段 {a, b} 表の検証に通らない)
    なので取り違えは起きない。

    Returns:
        optimization ブロックを持つ jsonc のパスのリスト(ファイル名順)。

    """
    found = []
    for f in sorted(Path(data_dir).glob("*.jsonc")):
        try:
            content = jsonc.load(f)
        # jsonc として読めないファイルは候補から静かに外し、残りの走査を続ける設計
        except Exception:  # ruff: ignore[S112, BLE001]
            continue
        if isinstance(content, dict) and "optimization" in content:
            found.append(f)
    return found


def axis_catalog(data_dir: str | Path, type_: str) -> dict[str, list | None]:
    """`type_` のスコアパーツで使える軸の一覧を、各軸の値候補に対応付ける。

    軸のデフォルト表示順は 測定csvのヘッダ順 → ラベル軸 → DataName。
    値候補 None = 自由入力のみ。

    Measure は識別子軸として、測定csvに Measure 列を持つ type にだけ出す
    (集計済み type には無い — docs/spec_change_dataname_measure.md 6.4節)。
    DataName は dataName_{type}.csv がある場合のみ。

    type_ == "dVtBudget" のときは FBC のカタログを返す(FBC.csv を読むため)。

    Returns:
        表示順に並んだ {軸名: 値候補のリスト} の辞書。自由入力のみの軸は
        値が None。

    """
    data_dir = Path(data_dir)
    source_type = "FBC" if type_ == "dVtBudget" else type_

    measured: list[str] = []
    tcsv = data_file(data_dir, f"{source_type}.csv")
    if tcsv.exists():
        cols = pl.scan_csv(tcsv).collect_schema().names()
        # Measure もヘッダ位置のまま軸として出す(相対化・filter の識別子軸)
        measured = [c for c in cols if c != source_type]

    label_axes: list[str] = []
    plabel = data_file(data_dir, f"parameterLabel_{source_type}.csv")
    if plabel.exists():
        pdf = pl.read_csv(plabel)
        # 全行が空欄の列は「この type に存在しない設定」(例: tPROG の
        # Read_Label は空欄で出力される)なので、選べる軸として出さない
        label_axes = [c for c in pdf.columns if c not in JOIN_KEYS and pdf[c].null_count() < pdf.height]

    catalog: dict[str, list | None] = {}
    for axis in measured + [a for a in label_axes if a not in measured]:
        catalog[axis] = _candidates(data_dir, source_type, axis, tcsv if axis in measured else None)
    if data_file(data_dir, f"dataName_{source_type}.csv").exists():
        catalog["DataName"] = _candidates(data_dir, source_type, "DataName", None)
    return catalog


def measure_labels(data_dir: str | Path, type_: str) -> dict[int, str]:
    """Measure 番号 → dataName の対応。

    UI の複合表示「dataName (Measure N)」用
    (docs/spec_change_dataname_measure.md 6.4節)。dataName_{type}.csv が無い・
    読めない場合は空 dict(番号のみ表示になる)。1:多(同じ dataName が複数
    番号に付くループ測定)はそのまま番号ごとの対応になる。

    Returns:
        {Measure 番号: dataName} の辞書。対応表が無い・読めない場合は空。

    """
    data_dir = Path(data_dir)
    source_type = "FBC" if type_ == "dVtBudget" else type_
    if not data_file(data_dir, f"dataName_{source_type}.csv").exists():
        return {}
    try:
        df = (
            resolve_axes(data_dir, source_type, {"Measure", "DataName"})
            .select(["Measure", "DataName"])
            .unique()
            .sort("Measure")
            .collect()
        )
    # 対応表が読めない場合は空 dict = 番号のみ表示に落とす仕様(docstring 参照)
    except Exception:  # ruff: ignore[BLE001]
        return {}
    return {
        int(m): str(d)
        for m, d in zip(df["Measure"].to_list(), df["DataName"].to_list(), strict=False)
        if m is not None and d is not None
    }


def _candidates(data_dir: Path, source_type: str, axis: str, tcsv: Path | None) -> list | None:
    map_path = _map_file_for_axis(data_dir, axis)
    if map_path is not None:
        m = pl.read_csv(map_path, has_header=False, new_columns=["code", "text"])
        full = m["text"].to_list()
        if axis.endswith(OVERRIDE_SUFFIX) and not all(isinstance(v, bool) for v in full):
            # 解決後の Override 列は bool(axis_resolve 参照)。map のテキストが
            # 文字列で読まれた場合だけ同じ規則で bool へ正規化して突き合わせる
            full = [str(v).upper() in {"TRUE", "1"} for v in full]
        # map の全語彙ではなく、過去データに実在する値を優先する(map順は保持):
        # 雛形パーツは先頭候補で filter するため、データに無い値を候補に出すと
        # 「filter が0行にマッチ」で雛形が壊れる。照会に失敗したら全語彙に
        # フォールバック。
        try:
            present = set(resolve_axes(data_dir, source_type, {axis}).select(axis).unique().collect()[axis].to_list())
        # 実在値の照会に失敗したら map の全語彙へフォールバックする設計(上のコメント参照)
        except Exception:  # ruff: ignore[BLE001]
            return full
        narrowed = [v for v in full if v in present]
        return narrowed or full
    if tcsv is not None:
        return pl.scan_csv(tcsv).select(axis).unique().sort(axis).collect()[axis].to_list()
    return None
