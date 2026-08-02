# Copyright (c) 2026
from pathlib import Path

from scorelib_param import jsonc
from scorelib_param.io_jsonc import load_run_config


def test_strip_line_and_block_comments() -> None:
    """コメントと末尾カンマの除去を検証する。"""
    text = """
    {
        // line comment
        "a": 1, /* block comment */
        "url": "http://example.com/not-a-comment",
        "b": [1, 2, 3,],  // trailing comma above too
    }
    """
    assert jsonc.loads(text) == {"a": 1, "url": "http://example.com/not-a-comment", "b": [1, 2, 3]}


def test_load_sample_run_config(fixtures_dir: Path) -> None:
    """サンプル config.jsonc の読み込みを検証する。"""
    config = load_run_config(fixtures_dir / "config.jsonc")
    assert config.Generation == "B9LS"
    assert config.optimization.WLgroup["WLgroup01"] == (0, 3)
    assert config.optimization.constraintThreshold["dVtBudget_R2A"].type == "percentile"
    assert len(config.optimization.score_parts) == 2
    part = config.optimization.score_parts[0]
    assert part.type == "FBC"
    assert part.relative is not None
    assert part.relative.split_axis == "Read_Override"
