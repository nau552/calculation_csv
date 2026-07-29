"""ブリッジ見本（scripts/*_bridge_example.py）の config 正規化のテスト。

現行の config ローダは読み込み時に一部の値を pandas Series / numpy 型へ
加工する（実機で判明 — 2026-07-29）。ブリッジは dump 前に _jsonable で素の
Python 型へ戻す。実装は pandas / numpy を import しない振る舞い判定なので、
テストも同じ振る舞いを持つフェイクで検証する（pandas を test 依存にしない）。
"""
import importlib.util
import json
from pathlib import Path

import pytest

from scorelib_param.models import RunConfig

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), SCRIPTS / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeSeries:
    """pandas.Series 相当（to_dict を持つ）。値は numpy スカラー相当。"""

    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return dict(self._data)


class FakeScalar:
    """numpy int64/float64 相当（item を持つ）。"""

    def __init__(self, v):
        self._v = v

    def item(self):
        return self._v


class FakeArray:
    """numpy ndarray 相当（tolist を持つ）。"""

    def __init__(self, values):
        self._values = list(values)

    def tolist(self):
        return list(self._values)


@pytest.mark.parametrize("script", ["get_score_bridge_example.py", "batch_bridge_example.py"])
def test_jsonable_restores_engine_readable_config(script):
    """ローダ加工済み config（Series/numpy入り）が json 化でき、エンジンが
    読むフィールド（WLgroupWeight）は手書き config と同じ形に戻ること。"""
    bridge = _load(script)
    config = {
        "Generation": "B9LS",
        "optimization": {
            "score_function": "gui_score",
            # ローダが Series 化したエンジン語彙（値は numpy スカラー相当）
            "WLgroupWeight": FakeSeries({"WLgroup01": FakeScalar(2.0), "WLgroup02": FakeScalar(1.0)}),
            "WLgroup": {"WLgroup01": (0, 3), "WLgroup02": (4, 8)},  # tuple もありうる
            # エンジンが読まない旧スコア用フィールド（dump を壊さなければ形は不問）
            "KLDweight": FakeSeries({"SGS": FakeScalar(0.1)}),
            "someArray": FakeArray([FakeScalar(1), FakeScalar(2)]),
            "someObject": object(),
        },
        "epochNum": FakeScalar(100),
    }
    text = json.dumps(bridge._jsonable(config))  # dump が例外にならない
    rc = RunConfig.model_validate(json.loads(text))
    # エンジンの重みセットとして手書きと同じ形に復元されている
    assert rc.weight_sets()["WLgroupWeight"] == {"WLgroup01": 2.0, "WLgroup02": 1.0}
    assert rc.group_defs()["WLgroup"].groups["WLgroup01"] == (0, 3)


def test_jsonable_converts_numpy_like_dict_keys():
    bridge = _load("get_score_bridge_example.py")
    out = bridge._jsonable({FakeScalar(3): FakeScalar(1.5)})
    assert json.loads(json.dumps(out)) == {"3": 1.5}
