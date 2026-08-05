# Copyright (c) 2026
# ruff: file-ignore[implicit-namespace-package] 単体実行スクリプト置き場でパッケージではない(__init__.py を持たない)
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true] 起動するのは自リポジトリの CLI で引数も固定
"""現行最適化スクリプト(Python 3.7)からバッチ計算エンジンを subprocess で呼ぶブリッジの実装例。

エンジン本体は Python 3.10+ で動くため、最適化スクリプト自身の python では
なく **scorelib_param 用の python 実行ファイル**を指定して起動する — 通常の
gui_score CLI(python -m scorelib_param.cli)を呼ぶときと同じ方式。

このファイル自体は Python 3.7 で動く書き方にしてあり、現行スクリプトの
過去データ活用部(BO 初期モデル構築の前処理)へコピーして使う想定。
scorelib_param 本体には依存しない(subprocess と CSV 読みだけ)。

使用例::

    scores, failed = compute_batch_scores(
        engine_python=r"/opt/py311/bin/python",   # scorelib_param が入っている python
        config=self.config,                        # 読み込み済み dict でもパスでも可
        histories=[r"/data/expA/Step1/Loop01/result_history",
                   r"/data/expB/Step2/Loop03/result_history"],
        out_csv=r"/tmp/past_scores.csv",
        dvtbudget_coef=r"/svn/scripts/dvtbudget_coef.jsonc",
        # scorelib_parent は省略可: この関数を kicOpt/ 内のスクリプトに貼れば
        # kicOpt/(scorelib_param/ が並ぶ場所)が自動で使われる
    )
    # scores: 1 epoch = 1 dict のリスト
    #   [{"Epoch": "expA/Step1/Loop01#0001", "History": "...", "EpochNo": 1,
    #     "Score": 160.4, "<パーツ名>": ..., ...}, ...]
    # failed: {"expA/Step1/Loop01#0007": "理由", ...}(除外された epoch)
    #
    # pandas で受けたい場合は out_csv をそのまま読めばよい:
    #   df = pandas.read_csv(out_csv)
"""

from __future__ import annotations

import contextlib
import csv
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

    get_score_bridge_example.py と同じもの — 各ファイル単体で貼れるよう重複させている。

    現行の config ローダは読み込み時に一部の値を計算用に加工する(例:
    WLgroupWeight / KLDweight を {名前: 重み} の pandas Series 化、値は numpy
    数値型)。そのままでは json.dump が失敗するため、ここで素の Python 型へ
    戻す。エンジンが読むフィールドは to_dict で手書きの config と同じ形に戻り、
    読まないフィールドは dump を壊さなければ何でもよい(読み込み時に無視)。

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

    例: このコードが kicOpt/optlib/ 内のスクリプトに貼られていて scorelib_param が
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


def _dump_config_tmp(config: dict) -> str:
    """読み込み済み config dict を一時 jsonc ファイルへ書き出し、そのパスを返す。

    Returns:
        書き出した一時ファイルのパス。呼び出し側が使い終わったら削除する。

    """
    fd, tmp_config = tempfile.mkstemp(suffix=".jsonc", prefix="scorelib_cfg_")
    with os.fdopen(fd, "w") as f:
        # ローダ加工済みの dict(pandas Series / numpy 型入り)でも
        # 書き出せるよう正規化してから dump(JSON は jsonc として妥当)
        json.dump(_jsonable(config), f)
    return tmp_config


def _run_engine(cmd: list[str], env: dict, log_path: str, timeout: float | None) -> None:
    """エンジンの CLI を subprocess で実行し、出力を log_path へ保存する。

    Raises:
        RuntimeError: エンジンの subprocess が異常終了(returncode != 0)したとき。
            メッセージにログファイルの末尾を含める。

    """
    # stderr(版数表示・advisory・進捗・除外理由)はログファイルへ保存する。
    # コンソールで直接見たい場合は stderr=None にして継承させてもよい
    with Path(log_path).open("w", encoding="utf-8") as log:
        proc = subprocess.run(
            cmd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,  # returncode は直後に自前検査する
        )
    if proc.returncode != 0:
        with Path(log_path).open(encoding="utf-8") as f:
            tail = "".join(f.readlines()[-15:])
        msg = f"scorelib_param.batch failed (exit {proc.returncode}). log tail ({log_path}):\n{tail}"
        raise RuntimeError(msg)


def _read_scores(out_csv: str) -> list[dict]:
    """結果 CSV を読み戻し、epoch ごとの dict のリストへ変換する。

    Returns:
        epoch ごとの結果 dict のリスト。Epoch / History は文字列のまま、
        EpochNo は int、Score と各スコアパーツは float(空欄は None)。

    """
    scores = []
    with Path(out_csv).open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            parsed = {}
            for key, value in row.items():
                if key in {"Epoch", "History"}:
                    parsed[key] = value
                elif key == "EpochNo":
                    parsed[key] = int(value)
                else:  # Score と全スコアパーツ
                    parsed[key] = float(value) if value else None
            scores.append(parsed)
    return scores


def _read_failed(out_csv: str) -> dict[str, str]:
    """除外 epoch の一覧(<out_csv>.failed.csv)があれば読み戻す。

    Returns:
        {Epoch: 除外理由} の dict。failed.csv が無ければ空 dict(全 epoch 成功)。

    """
    failed = {}
    failed_csv = out_csv + ".failed.csv"
    if Path(failed_csv).exists():
        with Path(failed_csv).open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                failed[row["Epoch"]] = row["reason"]
    return failed


def compute_batch_scores(  # ruff: ignore[too-many-arguments] — 見本の公開関数: 多数の省略可能キーワード引数は設計(束ねない方針)
    engine_python: str,  # scorelib_param が入っている python 実行ファイルのパス
    config: str | dict,  # config.jsonc の**パス**(推奨)または読み込み済みの config dict。
    # 現行ローダは読み込み時に dict を加工するため、元ファイルの
    # パスを渡すのが正(詳細は get_score_bridge_example.py の同項目)
    histories: list[str],  # result_history ディレクトリのパスのリスト
    out_csv: str,  # 結果 CSV の書き出し先
    *,  # ここから下は省略可能なオプション(キーワード指定のみ。Python 3.7 でも有効な構文)
    dvtbudget_coef: str | None = None,  # dVtBudget パーツがある場合のみ必須
    batch_size: int | str = "auto",  # "auto" 推奨(メモリから自動選択)
    max_threads: int | None = None,  # マシンを共有する場合に CPU スレッド数を制限
    max_prefetch: int = 2,
    strict: bool = False,
    generation_info: str | None = None,  # {Generation}.json のパス。Physical記法のグループ定義
    # (WLgroupDefinLogical=False)がある場合のみ必要
    # (各 epoch ディレクトリ内にあれば省略可)
    scorelib_parent: str | None = None,  # scorelib_param/ を含む場所(例: kicOpt/)。省略時は
    # このファイルの場所から親方向に scorelib_param/ を自動探索
    timeout: float | None = None,  # 秒。None なら無制限(数千 epoch は分単位かかる)
) -> tuple[list[dict], dict[str, str]]:
    """バッチ計算 CLI を subprocess で起動し、(scores, failed) を返す。

    - scores: epoch ごとの dict のリスト(数値列は float/int に変換済み)
    - failed: {Epoch: 理由}。skip-and-report で除外された epoch(空 dict なら全成功)
    - config に dict を渡した場合は一時ファイル経由で CLI へ渡す(エンジンは
      Generation / optimization 以外のキーを無視する)
    - エンジンの進捗・警告(batch-size advisory 等)は <out_csv>.log に保存される
    - 失敗(returncode != 0)は RuntimeError(ログ末尾つき。_run_engine が送出)

    Returns:
        (scores, failed) のタプル。scores は epoch ごとの結果 dict のリスト
        (数値列は float/int に変換済み)、failed は {Epoch: 除外理由} の dict
        (空 dict なら全 epoch 成功)。

    """
    if scorelib_parent is None:
        scorelib_parent = _find_scorelib_parent()

    tmp_config = None
    try:
        if isinstance(config, dict):
            tmp_config = _dump_config_tmp(config)
            config_path = tmp_config
        else:
            config_path = config

        cmd = [
            engine_python,
            "-m",
            "scorelib_param.batch",
            "--config",
            config_path,
            "--out",
            out_csv,
            "--batch-size",
            str(batch_size),
            "--max-prefetch",
            str(max_prefetch),
        ]
        for h in histories:
            cmd += ["--history", h]
        if dvtbudget_coef:
            cmd += ["--dvtbudget-coef", dvtbudget_coef]
        if max_threads:
            cmd += ["--max-threads", str(max_threads)]
        if strict:
            cmd += ["--strict"]
        if generation_info:
            cmd += ["--generation-info", generation_info]

        # cwd は変えず、PYTHONPATH で scorelib_param を見つけさせる
        # (呼び出し側が相対パスを渡しても壊れないように)
        env = dict(os.environ)
        env["PYTHONPATH"] = scorelib_parent + os.pathsep + env.get("PYTHONPATH", "")

        _run_engine(cmd, env, out_csv + ".log", timeout)
    finally:
        if tmp_config is not None:
            with contextlib.suppress(OSError):
                Path(tmp_config).unlink()

    return _read_scores(out_csv), _read_failed(out_csv)


if __name__ == "__main__":
    # 動作確認用の最小実行(リポジトリ内のミニデータを使う場合の例):
    #   python scripts/batch_bridge_example.py <engine_python> <scorelib_parent> \
    #       <config.jsonc> <result_history> <out.csv> [dvtbudget_coef.jsonc]
    import sys

    COEF_ARG_INDEX = 6  # dvtbudget_coef.jsonc(任意)の argv 上の位置(その手前までが必須引数)
    engine, parent, config_arg, history, out = sys.argv[1:COEF_ARG_INDEX]
    coef = sys.argv[COEF_ARG_INDEX] if len(sys.argv) > COEF_ARG_INDEX else None
    scores, failed = compute_batch_scores(
        engine, config_arg, [history], out, dvtbudget_coef=coef, scorelib_parent=parent
    )
    print(f"scored epochs: {len(scores)}, failed: {len(failed)}")
    if scores:
        print("first row:", scores[0])
