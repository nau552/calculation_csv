# Copyright (c) 2026
import csv
from pathlib import Path

BASE = Path(__file__).parent / ".." / "result_tmp"


def read_map(path: Path, *, has_header: bool = False) -> dict[str, str]:
    """マップ csv を {コード: テキスト} の辞書として読む(has_header=True なら非数値先頭セルの行を飛ばす)。

    Returns:
        1列目のコードをキー、2列目のテキスト(無い行は空文字)を値とする辞書。

    """
    m = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            # allow optional header: if non-numeric first cell, skip
            if has_header and not row[0].strip().isdigit():
                continue
            key = row[0].strip()
            val = row[1].strip() if len(row) > 1 else ""
            m[key] = val
    return m


def load_parameter_label(path: Path) -> dict[tuple[str, str, str, str, str], dict[str, str]]:
    """parameterLabel_FBC.csv を (InBatchEpoch,Board,Chip,Block,Measure) キーの行辞書として読む。

    Returns:
        5軸タプルをキー、csv の1行(列名 → 値の dict)を値とする辞書。

    """
    d = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            key = (
                r["InBatchEpoch"].strip(),
                r["Board"].strip(),
                r["Chip"].strip(),
                r["Block"].strip(),
                r["Measure"].strip(),
            )
            d[key] = r
    return d


# dataName は実験データのファイル名・列名の語彙そのもの(dataName_FBC.csv, DataName 列)。
# 綴りを揃えることを優先し snake_case 化しない(以降の noqa: N802/N806 も同じ理由)
def load_dataName(path: Path) -> dict[tuple[str, str, str, str, str], str]:  # ruff: ignore[N802]
    """dataName_FBC.csv を (InBatchEpoch,Board,Chip,Block,Measure) → DataName の辞書として読む。

    Returns:
        5軸タプルをキー、DataName 列の値(列が無い行は空文字)を値とする辞書。

    """
    d = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            key = (
                r["InBatchEpoch"].strip(),
                r["Board"].strip(),
                r["Chip"].strip(),
                r["Block"].strip(),
                r["Measure"].strip(),
            )
            d[key] = r.get("DataName", "").strip()
    return d


def _text(code: str, mapping: dict[str, str]) -> str:
    """コード値をマップ csv の辞書でテキストへ読み替える。

    Returns:
        mapping にある場合は対応するテキスト。code が空、または対応が無い場合は空文字。

    """
    return mapping.get(code, "") if code else ""


def _override_text(code: str, map_override: dict[str, str]) -> str:
    """Override のコード値を True/False 表記へ読み替える(Title case に正規化)。

    Returns:
        マップの値が真値系("true"/"t"/"1")なら "True"、偽値系("false"/"f"/"0")
        なら "False"。正規化できない値はそのまま返す(対応が無ければコード自身)。
        code が空なら空文字。

    """
    if not code:
        return ""
    v = map_override.get(code, code)
    vv = v.strip().lower()
    if vv in {"true", "t", "1"}:
        return "True"
    if vv in {"false", "f", "0"}:
        return "False"
    # fallback: return original
    return v


def _param_texts(
    param: dict[str, str] | None,
    map_label: dict[str, str],
    map_override: dict[str, str],
) -> dict[str, str]:
    """Erase/Program/Read の Label・Override テキスト6列を parameterLabel の1行から作る。

    parameterLabel_FBC.csv columns: Erase_Label, Erase_Override, Program_Label,
    Program_Override, Measure, Read_Label, Read_Override, ...

    Returns:
        出力 csv の6列({操作}_Label / {操作}_Override)の {列名: テキスト}。
        param が None(キーに対応する行が無い)場合はすべて空文字。

    """
    texts = {}
    for op in ("Erase", "Program", "Read"):
        label_num = param.get(f"{op}_Label", "") if param else ""
        override_num = param.get(f"{op}_Override", "") if param else ""
        texts[f"{op}_Label"] = _text(label_num, map_label)
        texts[f"{op}_Override"] = _override_text(override_num, map_override)
    return texts


def main() -> None:
    """FBC.csv を map/ラベル各 csv で読み替え、FBC_expanded.csv を書き出す。"""
    map_dataName = read_map(BASE / "map_DataName.csv")  # ruff: ignore[N806]
    map_label = read_map(BASE / "map_Label.csv")
    map_state = read_map(BASE / "map_State.csv")
    map_override = read_map(BASE / "map_Override.csv")

    param_map = load_parameter_label(BASE / "parameterLabel_FBC.csv")
    dataName_map = load_dataName(BASE / "dataName_FBC.csv")  # ruff: ignore[N806]

    out_path = BASE / "FBC_expanded.csv"

    with (
        (BASE / "FBC.csv").open(newline="", encoding="utf-8") as inf,
        out_path.open("w", newline="", encoding="utf-8") as outf,
    ):
        reader = csv.DictReader(inf)
        # Produce exactly the requested columns in this order:
        # InBatchEpoch, Board, Chip, Block, Measure, Erase_Label, Erase_Override,
        # Program_Label, Program_Override, Read_Label, Read_Override, DataName,
        # WL, STR, State, FBC
        fieldnames = [
            "InBatchEpoch",
            "Board",
            "Chip",
            "Block",
            "Measure",
            "Erase_Label",
            "Erase_Override",
            "Program_Label",
            "Program_Override",
            "Read_Label",
            "Read_Override",
            "DataName",
            "WL",
            "STR",
            "State",
            "FBC",
        ]
        writer = csv.DictWriter(outf, fieldnames=fieldnames)
        writer.writeheader()

        for r in reader:
            key = (
                r.get("InBatchEpoch", "").strip(),
                r.get("Board", "").strip(),
                r.get("Chip", "").strip(),
                r.get("Block", "").strip(),
                r.get("Measure", "").strip(),
            )

            # ラベル・Override(parameterLabel_FBC.csv の行を6列のテキストへ)
            texts = _param_texts(param_map.get(key), map_label, map_override)

            writer.writerow(
                {
                    "InBatchEpoch": r.get("InBatchEpoch", ""),
                    "Board": r.get("Board", ""),
                    "Chip": r.get("Chip", ""),
                    "Block": r.get("Block", ""),
                    "Measure": r.get("Measure", ""),
                    "Erase_Label": texts["Erase_Label"],
                    "Erase_Override": texts["Erase_Override"],
                    "Program_Label": texts["Program_Label"],
                    "Program_Override": texts["Program_Override"],
                    "Read_Label": texts["Read_Label"],
                    "Read_Override": texts["Read_Override"],
                    # DataName numeric from dataName_FBC.csv, map to text
                    "DataName": _text(dataName_map.get(key, ""), map_dataName),
                    "WL": r.get("WL", ""),
                    "STR": r.get("STR", ""),
                    # map State to text
                    "State": _text(r.get("State", "").strip(), map_state),
                    "FBC": r.get("FBC", ""),
                }
            )

    print("Wrote", out_path)


if __name__ == "__main__":
    main()
