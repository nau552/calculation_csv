# Copyright (c) 2026
# ruff: file-ignore[import-outside-top-level] そのテストだけが使う依存は関数内で import する
# ruff: file-ignore[magic-value-comparison] テストの期待値は生の数値で書く(定数名に隠すと期待値が読めない)
# ruff: file-ignore[float-equality-comparison] 期待値は2進浮動小数で正確に表せる値で、厳密一致そのものを検査する
"""WLgroup 定義の在り処の一本化(0.7.0)のテスト。

WLgroup / WLgroupDefinLogical / WLgroupWeight は実験スクリプトが読む config の
正式なキー。エクスポートは WL 軸の "WLgroup" 定義をこの WLgroup 系キーだけに
書き、groupDefs と二重にしない — 合成後 config では実験スクリプトが読む
optimization.WLgroup がそのまま編集後の内容になり、手編集でも「どちらが
使われるか」の迷いが生じない。読み込みは ScoreFile / RunConfig の両形式で
WLgroup 系キーと groupDefs のどちらも受ける。
"""

import pytest

from scorelib_param.models import RunConfig, ScoreFile
from ui import state


def _sf_dict() -> dict[str, object]:
    sf = state.empty_score_file()
    sf["groupDefs"] = {
        "WLgroup": {"axis": "WL", "groups": {"g1": [0, 2], "g2": [3, 5]}, "definedInLogical": False},
        "STRgroup": {"axis": "STR", "groups": {"even": [0, 1]}},
    }
    sf["weightSets"] = {"WLgroupWeight": {"g1": 2.0, "g2": 1.0}, "other": 3.0}
    return sf


def test_export_writes_wlgroup_to_wlgroup_keys_only() -> None:
    """エクスポートが WL 軸の WLgroup を WLgroup 系キーだけに書くことを検証する。"""
    text = state.score_file_to_jsonc(_sf_dict())
    import json

    out = json.loads(text)
    # WL 軸の WLgroup は WLgroup 系キーへ(groupDefs には残らない)
    assert out["WLgroup"] == {"g1": [0, 2], "g2": [3, 5]}
    assert out["WLgroupDefinLogical"] == "False"  # 現行 config の文字列流儀
    assert "WLgroup" not in out.get("groupDefs", {})
    assert "STRgroup" in out["groupDefs"]  # WL 以外の定義は groupDefs のまま
    assert out["WLgroupWeight"] == {"g1": 2.0, "g2": 1.0}
    assert out["weightSets"] == {"other": 3.0}


def test_export_import_roundtrip() -> None:
    """エクスポート → インポートで内部表現(groupDefs / weightSets)に戻る。"""
    text = state.score_file_to_jsonc(_sf_dict())
    sf = state.import_score_file(text)
    wl = sf["groupDefs"]["WLgroup"]
    assert wl["axis"] == "WL"
    assert {k: list(v) for k, v in wl["groups"].items()} == {"g1": [0, 2], "g2": [3, 5]}
    assert wl["definedInLogical"] is False
    assert "STRgroup" in sf["groupDefs"]
    assert sf["weightSets"]["WLgroupWeight"] == {"g1": 2.0, "g2": 1.0}
    assert sf["weightSets"]["other"] == 3.0


def test_scorefile_accepts_wlgroup_keys() -> None:
    """ScoreFile が WLgroup 系キー(WLgroup / WLgroupDefinLogical / WLgroupWeight)を受けることを検証する。"""
    sf = ScoreFile.model_validate(
        {
            "score_parts": [],
            "WLgroup": {"g1": [0, 5]},
            "WLgroupDefinLogical": "True",
            "WLgroupWeight": 2,
        }
    )
    assert sf.groupDefs["WLgroup"].axis == "WL"
    assert sf.groupDefs["WLgroup"].definedInLogical is True
    assert sf.weightSets["WLgroupWeight"] == 2


def test_scorefile_groupdefs_wins_over_wlgroup_keys() -> None:
    """両方書かれていたら groupDefs が勝つ(RunConfig の統合と同じ優先)。"""
    sf = ScoreFile.model_validate(
        {
            "score_parts": [],
            "WLgroup": {"old": [0, 1]},
            "groupDefs": {"WLgroup": {"axis": "WL", "groups": {"new": [0, 2]}}},
        }
    )
    assert list(sf.groupDefs["WLgroup"].groups) == ["new"]


def test_scorefile_rejects_bad_defin_logical_string() -> None:
    """不正な WLgroupDefinLogical 文字列が拒否されることを検証する。"""
    with pytest.raises(Exception, match="WLgroupDefinLogical"):
        ScoreFile.model_validate({"WLgroup": {"g": [0, 1]}, "WLgroupDefinLogical": "maybe"})


def test_runconfig_to_score_file_merges_wlgroup_keys() -> None:
    """RunConfig 形式の取り込みでも WLgroup 系キーが groupDefs に統合される。

    (weightSets の WLgroupWeight 統合と対称)。
    """
    rc = RunConfig.model_validate(
        {
            "Generation": "B9LS",
            "optimization": {"WLgroup": {"g1": [0, 3]}, "WLgroupWeight": 5},
        }
    )
    sf = rc.to_score_file()
    assert sf.groupDefs["WLgroup"].axis == "WL"
    assert sf.weightSets["WLgroupWeight"] == 5


def test_add_group_def_reserves_wlgroup_name() -> None:
    """WLgroup 名が WL 軸以外の定義では予約されていることを検証する。"""
    sf = state.empty_score_file()
    with pytest.raises(ValueError, match="予約名"):
        state.add_group_def(sf, "WLgroup", "STR", axis_names=set())
    state.add_group_def(sf, "WLgroup", "WL", axis_names=set())  # WL 軸なら可
    assert sf["groupDefs"]["WLgroup"]["axis"] == "WL"
