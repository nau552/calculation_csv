# Copyright (c) 2026
"""config_vocab_diff_example(ファイル vs ローダ加工後 dict の語彙診断)のテスト。"""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from importlib.abc import Loader
    from importlib.machinery import ModuleSpec

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load() -> ModuleType:
    spec = cast(
        "ModuleSpec",
        importlib.util.spec_from_file_location("config_vocab_diff_example", SCRIPTS / "config_vocab_diff_example.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    cast("Loader", spec.loader).exec_module(mod)
    return mod


class FakeSeries:
    """pandas.Series 相当(to_dict を持つ)のフェイク。"""

    def __init__(self, data: dict[str, float]) -> None:
        """保持するデータを受け取る。"""
        self._data = data

    def to_dict(self) -> dict[str, float]:
        """素の dict を返す(pandas.Series.to_dict 相当)。

        Returns:
            保持しているデータのコピー(キーと値はそのまま)。

        """
        return dict(self._data)


CONFIG_TEXT = """{
    // コメントつき jsonc
    "Generation": "B9LS",
    "optimization": {
        "score_function": "gui_score",
        "WLgroup": {"g1": [0, 3]},   /* 範囲表記 */
        "WLgroupWeight": {"g1": 2.0},
    }
}
"""


def test_report_finds_memory_only_and_transformed(tmp_path: Path) -> None:
    """ファイルのみ・加工あり・メモリのみの各判定を検証する。"""
    mod = _load()
    cfg = tmp_path / "config.jsonc"
    cfg.write_text(CONFIG_TEXT, encoding="utf-8")
    # ローダ加工後を模す: optimization の中身だけ・範囲展開・Series 化・
    # ファイルに無い vthSkip をメモリ上でだけ自動補完
    processed = {
        "score_function": "gui_score",
        "WLgroup": {"g1": [0, 1, 2, 3]},
        "WLgroupWeight": FakeSeries({"g1": 2.0}),
        "vthSkip": {"epochs": 100, "dummyKLDValue": 0},
    }
    lines = []
    findings = {
        (scope, key): status
        for scope, key, status in mod.report_engine_vocab_diff(cfg, processed, printer=lines.append)
    }
    assert findings["top", "Generation"] == "ファイルのみ(ローダが除去)"
    assert findings["optimization", "score_function"] == "一致"
    assert findings["optimization", "WLgroup"] == "加工あり"  # [0,3] vs [0,1,2,3]
    assert findings["optimization", "WLgroupWeight"] == "一致"  # Series は to_dict で一致
    assert findings["optimization", "vthSkip"] == "メモリのみ"  # 自動補完の疑い
    assert any("メモリにだけ存在" in ln for ln in lines)


def test_report_ok_when_no_memory_only_keys(tmp_path: Path) -> None:
    """メモリにだけ存在するキーが無ければ OK 表示になることを検証する。"""
    mod = _load()
    cfg = tmp_path / "config.jsonc"
    cfg.write_text(CONFIG_TEXT, encoding="utf-8")
    full = mod.load_config_file(cfg)  # ファイルそのまま = 全体 dict 形式
    lines = []
    mod.report_engine_vocab_diff(cfg, full, printer=lines.append)
    assert any(ln.startswith("OK:") for ln in lines)
