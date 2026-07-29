# -*- coding: utf-8 -*-
"""現行最適化スクリプト（turbo.py / Python 3.7）の get_score() に差し込む、
毎 epoch の通常スコア計算ブリッジの実装例。

docs/score_gui_design.md 2節の合意どおり: config の
`optimization.score_function` が予約名（例 "gui_score"）のとき、score.py の
関数を呼ぶ代わりに `python -m scorelib_param.cli` を subprocess 起動し、
標準出力の JSON（{"Score": ..., <パーツ名>: ...}）をパースして返す。

このファイルは Python 3.7 で動く書き方・scorelib_param 非依存（標準ライブラリ
だけ）で、compute_epoch_score() を turbo.py へコピーし、get_score() に
数行の分岐を足すだけで使える::

    # get_score() 内の分岐イメージ:
    if self.config["optimization"]["score_function"] == "gui_score":
        result = compute_epoch_score(
            engine_python=ENGINE_PYTHON,   # scorelib_param が入っている python (3.10+)
            config=self.config,            # 読み込み済み dict をそのまま渡せる
            data_dir=result_tmp_dir,       # そのepochの測定結果ディレクトリ
            dvtbudget_coef=coef_jsonc_path,  # dVtBudgetパーツがある場合のみ
            # scorelib_parent は省略可: この関数を turbo.py に貼った場合、
            # kicOpt/（turbo.py と scorelib_param/ が並ぶ場所）が自動で使われる
        )
        return pandas.DataFrame([result])  # 1行: Score + 全スコアパーツ
        # 列名 = "Score" と各パーツ名。constraintThreshold のキーはパーツ名を
        # 参照するので、制約評価側からはこの列名でそのまま引ける
    # ...以降は既存の score_function 分岐...
"""
import json
import os
import subprocess
import tempfile


def _json_key(k):
    """dict キーの JSON 化: numpy int64 等のキーを Python 型へ（json.dump は
    str/int/float/bool/None 以外のキーを受けない。int 等は json 側が文字列化する）。"""
    if not isinstance(k, (str, int, float, bool)) and k is not None and hasattr(k, "item"):
        return k.item()
    return k


def _jsonable(obj):
    """config dict を json.dump できる形へ再帰変換する。

    現行の config ローダは読み込み時に一部の値を計算用に加工する（例:
    WLgroupWeight / KLDweight を {名前: 重み} の pandas Series 化、値は numpy
    数値型）。そのままでは json.dump が失敗するため、ここで素の Python 型へ
    戻す。エンジンが読むフィールド（WLgroupWeight 等）は to_dict で手書きの
    config と同じ {名前: 数値} に戻り、エンジンが読まないフィールドは
    「dump を壊さない形」になってさえいればよい（読み込み時に無視される）。
    pandas / numpy は import せず振る舞いで判定する（このファイルを標準
    ライブラリだけで動く状態に保つため）。"""
    if isinstance(obj, dict):
        return {_json_key(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj  # numpy float64 は float のサブクラスなのでここを通る
    if hasattr(obj, "to_dict"):  # pandas Series / DataFrame
        return _jsonable(obj.to_dict())
    if hasattr(obj, "tolist"):  # numpy ndarray
        return _jsonable(obj.tolist())
    if hasattr(obj, "item"):  # numpy スカラー（int64 / bool_ など）
        return obj.item()
    return str(obj)  # 最後の砦（エンジンが読まないフィールドを想定）


def _find_scorelib_parent():
    """このファイルの場所から親方向へ3階層まで scorelib_param/ を探す。
    例: このコードが kicOpt/optlib/turbo.py に貼られていて scorelib_param が
    kicOpt/scorelib_param にある場合、optlib → kicOpt の順に探して見つかる。"""
    d = os.path.dirname(os.path.abspath(__file__))
    cand = d
    for _ in range(4):
        if os.path.isdir(os.path.join(cand, "scorelib_param")):
            return cand
        parent = os.path.dirname(cand)
        if parent == cand:
            break
        cand = parent
    raise ValueError(
        "scorelib_param/ not found in or above %s; pass scorelib_parent explicitly" % d
    )


def compute_epoch_score(
    engine_python,      # scorelib_param が入っている python 実行ファイルのパス
    config,             # config.jsonc のパス **または** 読み込み済みの config dict
    data_dir,           # この epoch の測定結果ディレクトリ（result_tmp 相当）
    dvtbudget_coef=None,       # dVtBudget パーツがある場合のみ必須
    initial_temperature=None,  # 省略時: dvtbudget_coef 指定なら data_dir 内を使う
    custom_parts=None,         # テスト用の custom_parts.py 上書き（通常は不要）
    generation_info=None,      # {Generation}.json のパス。Physical記法のグループ定義
                               # （WLgroupDefinLogical=False）がある場合のみ必要
                               # （data_dir 内にあれば省略可 — 自動発見される）
    scorelib_parent=None,      # scorelib_param/ ディレクトリを含む場所（例: kicOpt/）。
                               # 省略時はこのファイルの場所から親方向に scorelib_param/
                               # を自動探索する（turbo.py が kicOpt/optlib/ に
                               # あっても kicOpt/scorelib_param を見つけられる）
    timeout=None,              # 秒。None なら無制限
):
    """1 epoch 分のスコアを CLI subprocess で計算して dict で返す:
    {"Score": float, "<パーツ名>": float, ...}

    - config に dict を渡した場合は一時ファイルに書き出して CLI へ渡す
      （エンジンは Generation / optimization 以外のキーを無視するので、
      最適化スクリプトが持つ config 全体をそのまま渡してよい）
    - stdout には結果 JSON だけが出る（版数表示などは stderr）
    - 失敗（returncode != 0）は stderr 末尾つきの RuntimeError
    """
    if scorelib_parent is None:
        scorelib_parent = _find_scorelib_parent()

    tmp_config = None
    try:
        if isinstance(config, dict):
            fd, tmp_config = tempfile.mkstemp(suffix=".jsonc", prefix="scorelib_cfg_")
            with os.fdopen(fd, "w") as f:
                # ローダ加工済みの dict（pandas Series / numpy 型入り）でも
                # 書き出せるよう正規化してから dump（JSON は jsonc として妥当）
                json.dump(_jsonable(config), f)
            config_path = tmp_config
        else:
            config_path = config

        cmd = [
            engine_python, "-m", "scorelib_param.cli",
            "--config", config_path,
            "--data-dir", data_dir,
        ]
        if dvtbudget_coef:
            if initial_temperature is None:
                initial_temperature = os.path.join(data_dir, "initial_temperature.csv")
            cmd += ["--dvtbudget-coef", dvtbudget_coef,
                    "--initial-temperature", initial_temperature]
        if custom_parts:
            cmd += ["--custom-parts", custom_parts]
        if generation_info:
            cmd += ["--generation-info", generation_info]

        # cwd は変えず、PYTHONPATH で scorelib_param を見つけさせる
        # （呼び出し側が相対パスを渡しても壊れないように）
        env = dict(os.environ)
        env["PYTHONPATH"] = scorelib_parent + os.pathsep + env.get("PYTHONPATH", "")

        proc = subprocess.run(  # noqa: S603 — 固定コマンド
            cmd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", errors="replace")[-2000:]
            raise RuntimeError(
                "scorelib_param.cli failed (exit %d) for %s.\nstderr tail:\n%s"
                % (proc.returncode, data_dir, tail)
            )
        return json.loads(proc.stdout.decode("utf-8"))
    finally:
        if tmp_config is not None:
            try:
                os.unlink(tmp_config)
            except OSError:
                pass


if __name__ == "__main__":
    # 動作確認用の最小実行:
    #   python scripts/get_score_bridge_example.py <engine_python> <scorelib_parent> \
    #       <config.jsonc> <data_dir> [dvtbudget_coef.jsonc]
    import sys

    engine, parent, config_arg, data_dir = sys.argv[1:5]
    coef = sys.argv[5] if len(sys.argv) > 5 else None
    result = compute_epoch_score(
        engine, config_arg, data_dir, dvtbudget_coef=coef, scorelib_parent=parent
    )
    print("Score = %r  (%d score parts)" % (result["Score"], len(result) - 1))
