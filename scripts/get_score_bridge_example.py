# Copyright (c) 2026
# ruff: file-ignore[implicit-namespace-package] 単体実行スクリプト置き場でパッケージではない(__init__.py を持たない)
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true] 起動するのは自リポジトリの CLI で引数も固定
"""現行最適化スクリプト(turbo.py / Python 3.7)の get_score() に差し込む、毎 epoch の通常スコア計算ブリッジの実装例。

docs/score_gui_design.md 2節の合意どおり: config の
`optimization.score_function` が予約名(例 "gui_score")のとき、score.py の
関数を呼ぶ代わりに `python -m scorelib_param.cli` を subprocess 起動し、
標準出力の JSON({"Score": ..., <パーツ名>: ...})をパースして返す。

このファイルは Python 3.7 で動く書き方・scorelib_param 非依存(標準ライブラリ
だけ)で、compute_epoch_score() を turbo.py へコピーし、get_score() に
数行の分岐を足すだけで使える::

    # get_score() 内の分岐イメージ:
    if self.config["optimization"]["score_function"] == "gui_score":
        result = compute_epoch_score(
            engine_python=ENGINE_PYTHON,   # scorelib_param が入っている python (3.10+)
            config=self.config,            # 読み込み済み dict をそのまま渡せる
            data_dir=result_tmp_dir,       # そのepochの測定結果ディレクトリ
            dvtbudget_coef=coef_jsonc_path,  # dVtBudgetパーツがある場合のみ
            # scorelib_parent は省略可: この関数を turbo.py に貼った場合、
            # kicOpt/(turbo.py と scorelib_param/ が並ぶ場所)が自動で使われる
        )
        return pandas.DataFrame([result])  # 1行: Score + 全スコアパーツ
        # 列名 = "Score" と各パーツ名。constraintThreshold のキーはパーツ名を
        # 参照するので、制約評価側からはこの列名でそのまま引ける
    # ...以降は既存の score_function 分岐...
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
from pathlib import Path


def _json_key(k: object) -> object:
    """キーを json.dump が受ける型へ変換する。

    json.dump は str/int/float/bool/None 以外のキーを受けない(int 等は json 側が
    文字列化する)ため、numpy int64 等のキーを Python 型へ戻す。

    Returns:
        json.dump がキーとして受けられる値。numpy スカラーは item() で素の
        Python 型へ戻し、元から受けられる型はそのまま返す。

    """
    if not isinstance(k, (str, int, float, bool)) and k is not None:
        item = getattr(k, "item", None)
        if item is not None:  # hasattr(k, "item") と同値(メソッドは None にならない)
            return item()
    return k


def _jsonable(obj: object) -> object:  # ruff: ignore[too-many-return-statements] — isinstance 早期 return の連鎖が最も読みやすい形のため容認
    """読み込み済み config dict を json.dump できる形へ再帰変換する。

    現行の config ローダは読み込み時に一部の値を計算用に加工する(例:
    WLgroupWeight / KLDweight を {名前: 重み} の pandas Series 化、値は numpy
    数値型)。そのままでは json.dump が失敗するため、ここで素の Python 型へ
    戻す。エンジンが読むフィールド(WLgroupWeight 等)は to_dict で手書きの
    config と同じ {名前: 数値} に戻り、エンジンが読まないフィールドは
    「dump を壊さない形」になってさえいればよい(読み込み時に無視される)。
    pandas / numpy は import せず振る舞いで判定する(このファイルを標準
    ライブラリだけで動く状態に保つため)。

    Returns:
        json.dump にそのまま渡せる形へ再帰変換した値(dict / list / 素のスカラー。
        どの変換にも当てはまらない値は str 化される)。

    """
    if isinstance(obj, dict):
        return {_json_key(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj  # numpy float64 は float のサブクラスなのでここを通る
    # 振る舞い判定は getattr(3引数)で行う(hasattr と同値。メソッドは None にならない)
    to_dict = getattr(obj, "to_dict", None)
    if to_dict is not None:  # pandas Series / DataFrame
        return _jsonable(to_dict())
    tolist = getattr(obj, "tolist", None)
    if tolist is not None:  # numpy ndarray
        return _jsonable(tolist())
    item = getattr(obj, "item", None)
    if item is not None:  # numpy スカラー(int64 / bool_ など)
        return item()
    return str(obj)  # 最後の砦(エンジンが読まないフィールドを想定)


def _find_scorelib_parent() -> str:
    """このファイルの場所から親方向へ3階層まで scorelib_param/ を探す。

    例: このコードが kicOpt/optlib/turbo.py に貼られていて scorelib_param が
    kicOpt/scorelib_param にある場合、optlib → kicOpt の順に探して見つかる。

    Returns:
        scorelib_param/ ディレクトリを直下に含む場所の絶対パス(str)。

    Raises:
        ValueError: 親方向へ3階層まで探しても scorelib_param/ が見つからないとき。

    """
    d = Path(__file__).resolve().parent
    cand = d
    for _ in range(4):
        if (cand / "scorelib_param").is_dir():
            return str(cand)
        parent = cand.parent
        if parent == cand:
            break
        cand = parent
    msg = f"scorelib_param/ not found in or above {d}; pass scorelib_parent explicitly"
    raise ValueError(msg)


def compute_epoch_score(  # ruff: ignore[too-many-arguments] — 見本の公開関数: 多数の省略可能キーワード引数は設計(束ねない方針)
    engine_python: str,  # scorelib_param が入っている python 実行ファイルのパス
    config: str | dict,  # config.jsonc の**パス**(推奨)または読み込み済みの config dict。
    # 現行ローダは読み込み時に dict を加工する(Series 化・
    # WLgroup の範囲展開・optimization だけの抽出など)ため、
    # 元ファイルのパスを渡すのが正。dict 渡しは _jsonable が
    # dump を通せる形に救済するが、逆変換不能な加工
    # (範囲展開等)は元に戻せない。ファイルとメモリの
    # 食い違いの診断は scripts/config_vocab_diff_example.py
    data_dir: str,  # この epoch の測定結果ディレクトリ(result_tmp 相当)
    *,  # ここから下は省略可能なオプション(キーワード指定のみ。Python 3.7 でも有効な構文)
    dvtbudget_coef: str | None = None,  # dVtBudget パーツがある場合のみ必須
    initial_temperature: str | None = None,  # 省略時: dvtbudget_coef 指定なら data_dir 内を使う
    custom_parts: str | None = None,  # テスト用の custom_parts.py 上書き(通常は不要)
    generation_info: str | None = None,  # {Generation}.json のパス。Physical記法のグループ定義
    # (WLgroupDefinLogical=False)がある場合のみ必要
    # (data_dir 内にあれば省略可 — 自動発見される)
    scorelib_parent: str | None = None,  # scorelib_param/ ディレクトリを含む場所(例: kicOpt/)。
    # 省略時はこのファイルの場所から親方向に scorelib_param/
    # を自動探索する(turbo.py が kicOpt/optlib/ に
    # あっても kicOpt/scorelib_param を見つけられる)
    timeout: float | None = None,  # 秒。None なら無制限
) -> dict:
    """1 epoch 分のスコアを CLI subprocess で計算して dict で返す。

    返り値: {"Score": float, "<パーツ名>": float, ...}

    - config に dict を渡した場合は一時ファイルに書き出して CLI へ渡す
      (エンジンは Generation / optimization 以外のキーを無視するので、
      最適化スクリプトが持つ config 全体をそのまま渡してよい)
    - stdout には結果 JSON だけが出る(版数表示などは stderr)
    - 失敗(returncode != 0)は stderr 末尾つきの RuntimeError

    Returns:
        CLI が stdout へ出した結果 JSON をパースした dict。キーは "Score" と
        各スコアパーツ名、値はそれぞれの計算値。

    Raises:
        RuntimeError: エンジンの subprocess が異常終了(returncode != 0)したとき。
            メッセージに stderr の末尾を含める。

    """
    if scorelib_parent is None:
        scorelib_parent = _find_scorelib_parent()

    tmp_config = None
    try:
        if isinstance(config, dict):
            fd, tmp_config = tempfile.mkstemp(suffix=".jsonc", prefix="scorelib_cfg_")
            with os.fdopen(fd, "w") as f:
                # ローダ加工済みの dict(pandas Series / numpy 型入り)でも
                # 書き出せるよう正規化してから dump(JSON は jsonc として妥当)
                json.dump(_jsonable(config), f)
            config_path = tmp_config
        else:
            config_path = config

        cmd = [
            engine_python,
            "-m",
            "scorelib_param.cli",
            "--config",
            config_path,
            "--data-dir",
            data_dir,
        ]
        if dvtbudget_coef:
            if initial_temperature is None:
                initial_temperature = str(Path(data_dir) / "initial_temperature.csv")
            cmd += ["--dvtbudget-coef", dvtbudget_coef, "--initial-temperature", initial_temperature]
        if custom_parts:
            cmd += ["--custom-parts", custom_parts]
        if generation_info:
            cmd += ["--generation-info", generation_info]

        # cwd は変えず、PYTHONPATH で scorelib_param を見つけさせる
        # (呼び出し側が相対パスを渡しても壊れないように)
        env = dict(os.environ)
        env["PYTHONPATH"] = scorelib_parent + os.pathsep + env.get("PYTHONPATH", "")

        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            timeout=timeout,
            check=False,  # returncode は直後に自前検査する
        )
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", errors="replace")[-2000:]
            msg = f"scorelib_param.cli failed (exit {proc.returncode}) for {data_dir}.\nstderr tail:\n{tail}"
            raise RuntimeError(msg)
        return json.loads(proc.stdout.decode("utf-8"))
    finally:
        if tmp_config is not None:
            with contextlib.suppress(OSError):
                Path(tmp_config).unlink()


if __name__ == "__main__":
    # 動作確認用の最小実行:
    #   python scripts/get_score_bridge_example.py <engine_python> <scorelib_parent> \
    #       <config.jsonc> <data_dir> [dvtbudget_coef.jsonc]
    import sys

    COEF_ARG_INDEX = 5  # dvtbudget_coef.jsonc(任意)の argv 上の位置(その手前までが必須引数)
    engine, parent, config_arg, data_dir = sys.argv[1:COEF_ARG_INDEX]
    coef = sys.argv[COEF_ARG_INDEX] if len(sys.argv) > COEF_ARG_INDEX else None
    result = compute_epoch_score(engine, config_arg, data_dir, dvtbudget_coef=coef, scorelib_parent=parent)
    print(f"Score = {result['Score']!r}  ({len(result) - 1} score parts)")
