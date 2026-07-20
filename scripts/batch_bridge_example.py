# -*- coding: utf-8 -*-
"""現行最適化スクリプト（Python 3.7）からバッチ計算エンジンを subprocess で
呼ぶブリッジの実装例。

エンジン本体は Python 3.10+ で動くため、最適化スクリプト自身の python では
なく **scorelib_param 用の python 実行ファイル**を指定して起動する — 通常の
gui_score CLI（python -m scorelib_param.cli）を呼ぶときと同じ方式。

このファイル自体は Python 3.7 で動く書き方にしてあり、現行スクリプトの
過去データ活用部（BO 初期モデル構築の前処理）へコピーして使う想定。
scorelib_param 本体には依存しない（subprocess と CSV 読みだけ）。

使用例::

    scores, failed = compute_batch_scores(
        engine_python=r"/opt/py311/bin/python",   # scorelib_param が入っている python
        config=self.config,                        # 読み込み済み dict でもパスでも可
        histories=[r"/data/expA/Step1/Loop01/result_history",
                   r"/data/expB/Step2/Loop03/result_history"],
        out_csv=r"/tmp/past_scores.csv",
        dvtbudget_coef=r"/svn/scripts/dvtbudget_coef.jsonc",
        # scorelib_parent は省略可: この関数を kicOpt/ 内のスクリプトに貼れば
        # kicOpt/（scorelib_param/ が並ぶ場所）が自動で使われる
    )
    # scores: 1 epoch = 1 dict のリスト
    #   [{"Epoch": "expA/Step1/Loop01#0001", "History": "...", "EpochNo": 1,
    #     "Score": 160.4, "<パーツ名>": ..., ...}, ...]
    # failed: {"expA/Step1/Loop01#0007": "理由", ...}（除外された epoch）
    #
    # pandas で受けたい場合は out_csv をそのまま読めばよい:
    #   df = pandas.read_csv(out_csv)
"""
import csv
import json
import os
import subprocess
import tempfile


def _find_scorelib_parent():
    """このファイルの場所から親方向へ3階層まで scorelib_param/ を探す。
    例: このコードが kicOpt/optlib/ 内のスクリプトに貼られていて scorelib_param が
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


def compute_batch_scores(
    engine_python,      # scorelib_param が入っている python 実行ファイルのパス
    config,             # config.jsonc のパス **または** 読み込み済みの config dict
    histories,          # result_history ディレクトリのパスのリスト
    out_csv,            # 結果 CSV の書き出し先
    dvtbudget_coef=None,   # dVtBudget パーツがある場合のみ必須
    batch_size="auto",     # "auto" 推奨（メモリから自動選択）
    max_threads=None,      # マシンを共有する場合に CPU スレッド数を制限
    max_prefetch=2,
    strict=False,
    scorelib_parent=None,  # scorelib_param/ を含む場所（例: kicOpt/）。省略時は
                           # このファイルの場所から親方向に scorelib_param/ を自動探索
    timeout=None,          # 秒。None なら無制限（数千 epoch は分単位かかる）
):
    """バッチ計算 CLI を subprocess で起動し、(scores, failed) を返す。

    - scores: epoch ごとの dict のリスト（数値列は float/int に変換済み）
    - failed: {Epoch: 理由}。skip-and-report で除外された epoch（空 dict なら全成功）
    - config に dict を渡した場合は一時ファイル経由で CLI へ渡す（エンジンは
      Generation / optimization 以外のキーを無視する）
    - エンジンの進捗・警告（batch-size advisory 等）は <out_csv>.log に保存される
    - 失敗（returncode != 0）は RuntimeError（ログ末尾つき）
    """
    if scorelib_parent is None:
        scorelib_parent = _find_scorelib_parent()

    tmp_config = None
    try:
        if isinstance(config, dict):
            fd, tmp_config = tempfile.mkstemp(suffix=".jsonc", prefix="scorelib_cfg_")
            with os.fdopen(fd, "w") as f:
                json.dump(config, f)  # JSON は jsonc として妥当
            config_path = tmp_config
        else:
            config_path = config

        cmd = [
            engine_python, "-m", "scorelib_param.batch",
            "--config", config_path,
            "--out", out_csv,
            "--batch-size", str(batch_size),
            "--max-prefetch", str(max_prefetch),
        ]
        for h in histories:
            cmd += ["--history", h]
        if dvtbudget_coef:
            cmd += ["--dvtbudget-coef", dvtbudget_coef]
        if max_threads:
            cmd += ["--max-threads", str(max_threads)]
        if strict:
            cmd += ["--strict"]

        # cwd は変えず、PYTHONPATH で scorelib_param を見つけさせる
        # （呼び出し側が相対パスを渡しても壊れないように）
        env = dict(os.environ)
        env["PYTHONPATH"] = scorelib_parent + os.pathsep + env.get("PYTHONPATH", "")

        # stderr（版数表示・advisory・進捗・除外理由）はログファイルへ保存する。
        # コンソールで直接見たい場合は stderr=None にして継承させてもよい
        log_path = out_csv + ".log"
        with open(log_path, "w") as log:
            proc = subprocess.run(  # noqa: S603 — 固定コマンド
                cmd, env=env,
                stdout=log, stderr=subprocess.STDOUT,
                timeout=timeout,
            )
        if proc.returncode != 0:
            with open(log_path) as f:
                tail = "".join(f.readlines()[-15:])
            raise RuntimeError(
                "scorelib_param.batch failed (exit %d). log tail (%s):\n%s"
                % (proc.returncode, log_path, tail)
            )
    finally:
        if tmp_config is not None:
            try:
                os.unlink(tmp_config)
            except OSError:
                pass

    scores = []
    with open(out_csv, newline="") as f:
        for row in csv.DictReader(f):
            parsed = {}
            for key, value in row.items():
                if key in ("Epoch", "History"):
                    parsed[key] = value
                elif key == "EpochNo":
                    parsed[key] = int(value)
                else:  # Score と全スコアパーツ
                    parsed[key] = float(value) if value != "" else None
            scores.append(parsed)

    failed = {}
    failed_csv = out_csv + ".failed.csv"
    if os.path.exists(failed_csv):
        with open(failed_csv, newline="") as f:
            for row in csv.DictReader(f):
                failed[row["Epoch"]] = row["reason"]
    return scores, failed


if __name__ == "__main__":
    # 動作確認用の最小実行（リポジトリ内のミニデータを使う場合の例）:
    #   python scripts/batch_bridge_example.py <engine_python> <scorelib_parent> \
    #       <config.jsonc> <result_history> <out.csv> [dvtbudget_coef.jsonc]
    import sys

    engine, parent, config_arg, history, out = sys.argv[1:6]
    coef = sys.argv[6] if len(sys.argv) > 6 else None
    scores, failed = compute_batch_scores(
        engine, config_arg, [history], out, dvtbudget_coef=coef, scorelib_parent=parent
    )
    print("scored epochs: %d, failed: %d" % (len(scores), len(failed)))
    if scores:
        print("first row:", scores[0])
