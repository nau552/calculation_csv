# Copyright (c) 2026
"""dVtBudget 係数の Python ファイルを jsonc へ変換する。

入力は sample.py 形式: 世代 → 温度(int) → State → {"a":..., "b":...} の
辞書リテラル `dVtBudget_coef = {...}` 1個。

使い方: python scripts/convert_dvtbudget_coef.py sample.py out.jsonc
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scorelib_param import jsonc


def convert(py_path: str, out_path: str) -> None:
    """sample.py 形式の係数ファイルを読み込み、jsonc へ書き出す。"""
    # import せず ast.literal_eval で読む(係数ファイルに紛れたコードを実行しないため)
    src = Path(py_path).read_text(encoding="utf-8")
    tree = ast.parse(src)
    assign = next(n for n in tree.body if isinstance(n, ast.Assign))
    value = ast.literal_eval(assign.value)

    # 温度キーは json では文字列にしかできないため int → str に正規化
    normalized = {
        generation: {str(temp): states for temp, states in temps.items()} for generation, temps in value.items()
    }
    jsonc.dump(normalized, out_path)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    convert(sys.argv[1], sys.argv[2])
