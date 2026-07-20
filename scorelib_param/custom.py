"""自作スコアパーツ関数（type="custom"）のロードと実行。

Pythonが書けるユーザは custom_parts.py に関数を書き、1関数=1スコアパーツと
して呼べる（戻り値は有限な1スカラー）。ファイルの場所は**固定**（リポジトリ
直下、scorelib_param パッケージの隣。SVNで版管理）: config にパスを持たせると
実験入力から任意コードを実行できてしまうため、あえて固定にしている。
関数の追加・変更は SVN コミット=レビューを通すのが意図したゲート。
設計UIは同じファイル（GUIが配る一式zipに同梱）を読み込んで関数一覧と
テスト計算を提供するので、リビジョンが一致していれば実行側と同じ関数になる。

関数の契約::

    def my_score(ctx) -> float:
        df = pl.read_csv(ctx.data_dir / "FBC.csv")
        ...
        return value

ctx は CustomContext: data_dir (Path) / generation (str | None) /
group_defs (名前 -> GroupDef) / params (そのパーツの params 辞書)。
"""
from __future__ import annotations

import importlib.util
import inspect
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .models import GroupDef, ScorePart

DEFAULT_FILENAME = "custom_parts.py"


@dataclass
class CustomContext:
    data_dir: Path
    generation: Optional[str] = None
    group_defs: Dict[str, GroupDef] = field(default_factory=dict)
    params: Dict[str, Any] = field(default_factory=dict)


def default_custom_parts_path() -> Path:
    """リポジトリ直下の custom_parts.py（scorelib_param パッケージの1つ上）。"""
    return Path(__file__).resolve().parent.parent / DEFAULT_FILENAME


def load_custom_module(path: str | Path):
    """ユーザ関数ファイルを import する。＝トップレベルコードが実行される。
    ファイルは SVN レビュー済みのもの（ユーザのアップロード入力ではない）が
    前提なので許容している。"""
    path = Path(path)
    spec = importlib.util.spec_from_file_location("scorelib_custom_parts", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot import custom parts file: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def list_custom_functions(module) -> list[str]:
    """モジュール自身で定義された公開関数名（import した名前・`_`始まりは除外）。"""
    return sorted(
        name
        for name, fn in vars(module).items()
        if not name.startswith("_")
        and inspect.isfunction(fn)
        and fn.__module__ == module.__name__
    )


def compute_custom_part(
    score_part: ScorePart,
    module,
    ctx: CustomContext,
) -> float:
    fname = score_part.function or score_part.name
    fn = getattr(module, fname, None)
    if not callable(fn):
        raise ValueError(
            f"custom function '{fname}' (score part '{score_part.name}') not found in "
            f"{getattr(module, '__file__', DEFAULT_FILENAME)} — available: {list_custom_functions(module)}"
        )
    try:
        value = fn(ctx)
    except Exception as err:
        raise ValueError(f"custom function '{fname}' raised: {err}") from err
    # bool は int のサブクラスなので明示的に弾く
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(
            f"custom function '{fname}' must return one finite number, got {value!r}"
        )
    return float(value)
