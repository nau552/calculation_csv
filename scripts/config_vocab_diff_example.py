# Copyright (c) 2026
# ruff: file-ignore[implicit-namespace-package] 単体実行スクリプト置き場でパッケージではない(__init__.py を持たない)
"""実験 config ファイルと、ローダ加工後の dict の「エンジン語彙」突き合わせ診断。

スコア計算エンジンは**元の config ファイル**を読む(ローダがメモリ上で行う
加工 — Series 化・範囲展開・自動補完 — には依存しない)。この診断は、その
運用で唯一の賭けである「ファイルに書かれていないのにメモリ上にだけ存在する
スコア関連の値は無い」を実機で答え合わせするためのもの。

使い方(Python 3.7・標準ライブラリのみ。実機の対話環境や turbo.py に貼る)::

    from config_vocab_diff_example import report_engine_vocab_diff
    report_engine_vocab_diff(config_path, self.optConf)   # 全体 dict でも可

出力の読み方:
- 「メモリのみ」 … ローダの自動補完の疑い。エンジンはファイルを読むため
  この値は計算に使われない。エンジン語彙として必要な値なら、ファイルに
  書く運用にする(または担当者に出所を確認する)
- 「加工あり」/「ローダが除去」 … エンジンはファイル側を読むので影響なし(参考情報)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

# エンジンが読む語彙(scorelib_param/models.py の RunConfig / OptimizationConfig)
ENGINE_TOP_KEYS = ("Generation",)
ENGINE_OPT_KEYS = (
    "score_function",
    "expression",
    "score_parts",
    "constraintThreshold",
    "WLgroup",
    "WLgroupDefinLogical",
    "WLgroupWeight",
    "weightSets",
    "selectionSets",
    "groupDefs",
    "vthSkip",
)


def _strip_jsonc(text: str) -> str:
    """コメント(// と /* */)・末尾カンマを除去する jsonc → json の簡易変換。

    診断用の簡易実装: 文字列リテラル内に "//" を含む config には非対応。

    Returns:
        コメントと末尾カンマを取り除いた、json.loads にそのまま渡せるテキスト。

    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"(?<!:)//[^\n\"]*$", "", text, flags=re.MULTILINE)
    return re.sub(r",\s*([}\]])", r"\1", text)


def load_config_file(path: str | Path) -> dict:
    """実験 config ファイル(jsonc)を読み込んで dict を返す。

    Returns:
        コメント・末尾カンマを除去してからパースした config の中身の dict。

    """
    return json.loads(_strip_jsonc(Path(path).read_text(encoding="utf-8")))


def _plain(obj: object) -> object:  # ruff: ignore[too-many-return-statements] — isinstance 早期 return の連鎖が最も読みやすい形のため容認
    """比較用の正規化: Series/ndarray/numpy スカラー等を素の Python 型へ変換する。

    ブリッジ見本の _jsonable と同じ振る舞い判定。

    Returns:
        素の Python 型(dict / list / スカラー)へ再帰変換した比較用の値。
        どの変換にも当てはまらない値は str 化される。

    """
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_plain(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    # 振る舞い判定は getattr(3引数)で行う(hasattr と同値。メソッドは None にならない)
    to_dict = getattr(obj, "to_dict", None)
    if to_dict is not None:
        return _plain(to_dict())
    tolist = getattr(obj, "tolist", None)
    if tolist is not None:
        return _plain(tolist())
    item = getattr(obj, "item", None)
    if item is not None:
        return item()
    return str(obj)


def report_engine_vocab_diff(
    config_path: str | Path,
    processed: dict,
    printer: Callable[[str], object] = print,
) -> list[tuple[str, str, str]]:
    """エンジン語彙キーごとに (場所, キー, 状態) を返し、printer で報告する。

    状態: "メモリのみ" / "ファイルのみ(ローダが除去)" / "加工あり" / "一致"。
    `processed` は config 全体でも optimization の中身だけでも良い(自動判別)。

    Returns:
        (場所, キー, 状態) タプルのリスト。ファイル側・メモリ側の少なくとも
        一方に存在したエンジン語彙キーだけを含む(どちらにも無いキーは載らない)。

    """
    file_cfg = load_config_file(config_path)
    file_opt = file_cfg.get("optimization", {})
    if isinstance(processed, dict) and "optimization" in processed:
        mem_top, mem_opt = processed, processed["optimization"]
    else:
        mem_top, mem_opt = {}, processed  # optimization の中身だけを渡された形
        printer("note: 渡された dict に 'optimization' キーが無いため、optimization の中身とみなして比較します")

    findings = []
    pairs = [("top", k, file_cfg, mem_top) for k in ENGINE_TOP_KEYS]
    pairs += [("optimization", k, file_opt, mem_opt) for k in ENGINE_OPT_KEYS]
    for scope, key, in_file, in_mem in pairs:
        f_has, m_has = key in in_file, key in in_mem
        if not f_has and not m_has:
            continue
        if m_has and not f_has:
            status = "メモリのみ"
        elif f_has and not m_has:
            status = "ファイルのみ(ローダが除去)"
        elif _plain(in_file[key]) == _plain(in_mem[key]):
            status = "一致"
        else:
            status = "加工あり"
        findings.append((scope, key, status))
        printer(f"{scope:<14} {key:<22} {status}")

    only_mem = [k for _, k, s in findings if s == "メモリのみ"]
    if only_mem:
        printer(f"!! ファイルに無くメモリにだけ存在するエンジン語彙: {', '.join(only_mem)}")
        printer(
            "   → エンジンはファイルを読むため、これらは計算に使われません。"
            "必要な値ならファイルに書く運用にしてください"
        )
    else:
        printer("OK: メモリにだけ存在するエンジン語彙はありません(エンジンはファイルを読めば十分)")
    return findings


if __name__ == "__main__":
    # ファイル同士の比較だけなら CLI でも可:
    #   python config_vocab_diff_example.py <config.jsonc> <加工後をdumpしたjson>
    import sys

    report_engine_vocab_diff(sys.argv[1], load_config_file(sys.argv[2]))
