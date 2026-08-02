# Copyright (c) 2026
"""jsonc(`//` と `/* */` コメント・末尾カンマを許す JSON)の最小実装。

このプロジェクトで扱う形式(sample.jsonc)は「素の JSON + コメント +
たまに末尾カンマ」だけなので、外部ライブラリを増やさず小さな自前の
ストリッパで済ませている。
"""

from __future__ import annotations

import json
import re
from pathlib import Path


def strip_jsonc_comments(text: str) -> str:
    """文字列リテラル内の `//` を壊さないよう、1文字ずつ状態を追いながらコメントを除去する。

    正規表現一発では文字列内と区別できない。

    Returns:
        `//` 行コメントと `/* */` ブロックコメントを取り除いたテキスト
        (文字列リテラルの中身は保たれる)。

    """
    out = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def loads(text: str) -> object:
    """与えられた jsonc 文字列をパースする(コメントと末尾カンマを除去して json.loads)。

    Returns:
        json.loads の結果(トップレベルに応じて dict / list / スカラー)。

    """
    no_comments = strip_jsonc_comments(text)
    no_trailing_commas = _TRAILING_COMMA_RE.sub(r"\1", no_comments)
    return json.loads(no_trailing_commas)


def load(path: str | Path) -> object:
    """指定パスの jsonc ファイルを読み込んでパースする。

    Returns:
        ファイル内容のパース結果(トップレベルに応じて dict / list / スカラー)。

    """
    return loads(Path(path).read_text(encoding="utf-8"))


def dump(obj: object, path: str | Path, indent: int = 4) -> None:
    """オブジェクトを JSON としてファイルへ書き出す(indent つき・非ASCIIはそのまま)。"""
    Path(path).write_text(json.dumps(obj, indent=indent, ensure_ascii=False), encoding="utf-8")
