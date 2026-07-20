"""バッチスコア計算（scorelib.batch — docs/batch_design.md）のテスト。

最重要は**等価性**: 複数 epoch をバッチ一括計算した結果が、epoch ごとに
compute_score_file を呼んだ結果（現行の単一 epoch 計算）と全パーツ一致する
こと。epoch 混線（相対化ペア・集計・dVtBudget 係数が epoch をまたいで
混ざる）を検出できるよう、各 epoch の値・初期温度は**すべて別物**にする。
"""
import gzip
import shutil
import tarfile
import warnings
from pathlib import Path

import polars as pl
import pytest

from scorelib import io_jsonc
from scorelib.batch import (
    BatchRunner,
    StrictBatchError,
    compute_score_batch,
    derive_label,
    enumerate_epochs,
    stage_epoch,
)
from scorelib.batch.compute import EPOCH_COL
from scorelib.batch.runner import _advise_batch_size
from scorelib.batch import runner as runner_mod
from scorelib.cli import compute_score_file
from scorelib.dvtbudget import load_board_temperatures
from scorelib.models import GroupDef

MINI = Path(__file__).resolve().parent / "data" / "result_tmp_mini"
REPO_ROOT = Path(__file__).resolve().parent.parent


# --- 過去実験ツリーの構築 -------------------------------------------------


def _make_epoch(dst: Path, shift: int) -> None:
    """result_tmp_mini のコピーに epoch 固有の摂動を入れる:
    値列に +shift、初期温度は shift の偶奇で最近傍係数キーが入れ替わる
    （偶数: 25/30.83℃ → -30/85、奇数: 90/-20℃ → 85/-30）。
    これにより epoch 混線はどんな形でも数値不一致として現れる。"""
    shutil.copytree(MINI, dst)
    for name, col in (("FBC.csv", "FBC"), ("tR.csv", "tR")):
        df = pl.read_csv(dst / name)
        df.with_columns((pl.col(col) + shift).alias(col)).write_csv(dst / name)
    temps = "0,25\n1,30.8333\n" if shift % 2 == 0 else "0,90\n1,-20\n"
    (dst / "initial_temperature.csv").write_text(temps)


def _make_history(root: Path, exp: str, step: str, loop: str, shifts: dict) -> Path:
    hist = root / exp / step / loop / "result_history"
    for no, shift in shifts.items():
        _make_epoch(hist / f"result.{no:04d}", shift)
    return hist


@pytest.fixture(scope="module")
def config_and_coef():
    config = io_jsonc.load_run_config(REPO_ROOT / "config_mini.jsonc")
    coef = io_jsonc.load_dvtbudget_coef(
        Path(__file__).resolve().parent / "fixtures" / "dvtbudget_coef.jsonc"
    )
    return config, coef


@pytest.fixture(scope="module")
def history_tree(tmp_path_factory):
    """2つの実験（計5 epoch、摂動は全 epoch で異なる）。読み取り専用で共有。"""
    root = tmp_path_factory.mktemp("histories")
    hist_a = _make_history(root, "expA", "Step1", "Loop01", {1: 0, 2: 1, 3: 2})
    hist_b = _make_history(root, "expB", "Step2", "Loop03", {1: 3, 2: 4})
    return root, hist_a, hist_b


def _sequential_expected(history_tree, config, coef) -> dict:
    """現行の単一 epoch 計算による正解: {epoch_id: {"Score": ..., パーツ: ...}}"""
    _, hist_a, hist_b = history_tree
    expected = {}
    for hist, label in ((hist_a, "expA/Step1/Loop01"), (hist_b, "expB/Step2/Loop03")):
        for epoch_dir in sorted(hist.iterdir()):
            no = int(epoch_dir.name.split(".")[1])
            temps = load_board_temperatures(epoch_dir / "initial_temperature.csv")
            expected[f"{label}#{no:04d}"] = compute_score_file(
                epoch_dir, config, coef, temps
            )
    return expected


# --- ラベル・列挙 ---------------------------------------------------------


def test_derive_label_from_step_loop_layout(history_tree):
    _, hist_a, _ = history_tree
    assert derive_label(hist_a) == "expA/Step1/Loop01"


def test_derive_label_warns_on_unexpected_layout(tmp_path):
    hist = tmp_path / "somewhere" / "result_history"
    hist.mkdir(parents=True)
    with pytest.warns(UserWarning, match="does not follow"):
        derive_label(hist)


def test_enumerate_epochs(history_tree):
    _, hist_a, hist_b = history_tree
    refs = enumerate_epochs([hist_a, hist_b])
    assert [r.epoch_id for r in refs] == [
        "expA/Step1/Loop01#0001",
        "expA/Step1/Loop01#0002",
        "expA/Step1/Loop01#0003",
        "expB/Step2/Loop03#0001",
        "expB/Step2/Loop03#0002",
    ]
    assert refs[0].source_dir.name == "result.0001"


def test_enumerate_epochs_ignores_junk_and_rejects_duplicates(tmp_path, history_tree):
    _, hist_a, _ = history_tree
    hist = tmp_path / "exp" / "Step1" / "Loop01" / "result_history"
    shutil.copytree(hist_a, hist)
    (hist / "notes.txt").write_text("junk")
    with pytest.warns(UserWarning, match="ignoring non-epoch"):
        refs = enumerate_epochs({"x": hist})
    assert len(refs) == 3

    with pytest.raises(ValueError, match="duplicate history label"):
        enumerate_epochs([hist_a, hist_a])


def test_enumerate_epochs_empty_history_is_error(tmp_path):
    hist = tmp_path / "exp" / "Step1" / "Loop01" / "result_history"
    hist.mkdir(parents=True)
    with pytest.raises(ValueError, match="no result.NNNN"):
        enumerate_epochs({"exp": hist})


# --- 等価性（最重要） -----------------------------------------------------


def test_batch_equals_sequential(history_tree, config_and_coef, tmp_path):
    """バッチ一括計算 = epoch ごとの現行計算、が全パーツ・Score で一致する。
    epoch ごとに値も dVtBudget 温度も違うため、相対化ペア・集計・係数選択の
    どれが epoch をまたいで混ざっても不一致として検出される。"""
    root, hist_a, hist_b = history_tree
    config, coef = config_and_coef
    expected = _sequential_expected(history_tree, config, coef)

    runner = BatchRunner(
        [hist_a, hist_b], config, dvtbudget_coef=coef,
        batch_size=2, max_prefetch=2, staging_dir=tmp_path / "staging",
    )
    result = runner.run()

    assert result.failed == {}
    assert result.scores.height == 5
    part_names = [p.name for p in config.optimization.score_parts]
    assert result.scores.columns == [EPOCH_COL, "History", "EpochNo", "Score"] + part_names

    for row in result.scores.iter_rows(named=True):
        exp_values = expected[row[EPOCH_COL]]
        for key, exp_val in exp_values.items():
            assert row[key] == pytest.approx(exp_val, rel=1e-9), (
                f"{row[EPOCH_COL]} / {key}: batch={row[key]} sequential={exp_val}"
            )

    # 摂動が効いているか（epoch間で実際に値が異なる = 混線検出力の確認）
    scores = result.scores["Score"].to_list()
    assert len(set(scores)) == len(scores)

    # pass-through 入力は削除されていない
    assert (hist_a / "result.0001" / "FBC.csv").exists()


def test_compute_score_batch_direct(history_tree, config_and_coef, tmp_path):
    """runner を介さない計算層単体の等価性（1 history のみ）。"""
    _, hist_a, _ = history_tree
    config, coef = config_and_coef
    expected = _sequential_expected(history_tree, config, coef)

    refs = enumerate_epochs([hist_a])
    staged = [stage_epoch(r, tmp_path) for r in refs]
    result = compute_score_batch(staged, config, coef)
    assert result.failed == {}
    for row in result.scores.iter_rows(named=True):
        assert row["Score"] == pytest.approx(expected[row[EPOCH_COL]]["Score"], rel=1e-9)


# --- 圧縮 -----------------------------------------------------------------


def test_csv_gz_direct_read(history_tree, config_and_coef, tmp_path):
    """csv.gz 単体は解凍せず直読みできる（測定csv・ラベルcsv・map を圧縮）。"""
    root, hist_a, _ = history_tree
    config, coef = config_and_coef
    expected = _sequential_expected(history_tree, config, coef)

    hist = tmp_path / "expGz" / "Step1" / "Loop01" / "result_history"
    shutil.copytree(hist_a / "result.0001", hist / "result.0001")
    epoch_dir = hist / "result.0001"
    for name in ("FBC.csv", "parameterLabel_FBC.csv", "map_State.csv"):
        src = epoch_dir / name
        with open(src, "rb") as f_in, gzip.open(src.with_suffix(".csv.gz"), "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        src.unlink()

    runner = BatchRunner({"gz": hist}, config, dvtbudget_coef=coef,
                         staging_dir=tmp_path / "staging")
    result = runner.run()
    assert result.failed == {}
    row = result.scores.row(0, named=True)
    assert row["Score"] == pytest.approx(
        expected["expA/Step1/Loop01#0001"]["Score"], rel=1e-9
    )
    # ステージング（展開）は発生していない
    assert list((tmp_path / "staging").rglob("result.*")) == []


def test_targz_staging(history_tree, config_and_coef, tmp_path):
    """tar.gz はビューdirに展開して計算し、計算後にビューは削除される。
    フラット詰めとディレクトリごと詰め（flatten 救済）の両方を確認。"""
    root, hist_a, _ = history_tree
    config, coef = config_and_coef
    expected = _sequential_expected(history_tree, config, coef)

    hist = tmp_path / "expTar" / "Step1" / "Loop01" / "result_history"
    shutil.copytree(hist_a / "result.0001", hist / "result.0001")
    shutil.copytree(hist_a / "result.0002", hist / "result.0002")

    # epoch1: csv群をフラットに tar.gz へ（initial_temperature.csv は非圧縮のまま残す）
    e1 = hist / "result.0001"
    packed = [p for p in e1.iterdir() if p.name != "initial_temperature.csv"]
    with tarfile.open(e1 / "data.tar.gz", "w:gz") as tf:
        for p in packed:
            tf.add(p, arcname=p.name)
    for p in packed:
        p.unlink()

    # epoch2: ディレクトリごと固める（展開すると1段ネストする → flatten）
    e2 = hist / "result.0002"
    inner_files = list(e2.iterdir())
    with tarfile.open(e2 / "data.tar.gz", "w:gz") as tf:
        for p in inner_files:
            tf.add(p, arcname=f"result.0002/{p.name}")
    for p in inner_files:
        p.unlink()

    staging = tmp_path / "staging"
    runner = BatchRunner({"expA/Step1/Loop01": hist}, config, dvtbudget_coef=coef,
                         staging_dir=staging)
    result = runner.run()
    assert result.failed == {}
    assert result.scores.height == 2
    for row in result.scores.iter_rows(named=True):
        assert row["Score"] == pytest.approx(expected[row[EPOCH_COL]]["Score"], rel=1e-9)

    # ビューは削除済み・入力元のアーカイブは無傷
    assert list(staging.rglob("result.*")) == []
    assert (e1 / "data.tar.gz").exists() and (e2 / "data.tar.gz").exists()


def test_targz_rejects_unsafe_member(tmp_path):
    hist = tmp_path / "exp" / "Step1" / "Loop01" / "result_history"
    epoch = hist / "result.0001"
    epoch.mkdir(parents=True)
    evil = tmp_path / "evil.csv"
    evil.write_text("x\n1\n")
    with tarfile.open(epoch / "data.tar.gz", "w:gz") as tf:
        tf.add(evil, arcname="../evil.csv")
    refs = enumerate_epochs({"exp": hist})
    staged = stage_epoch(refs[0], tmp_path / "staging")
    assert staged.error is not None and "unsafe path" in staged.error


# --- エラー処理（skip-and-report / strict / 帰属） ------------------------


def test_missing_file_skip_and_report(history_tree, config_and_coef, tmp_path):
    root, hist_a, _ = history_tree
    config, coef = config_and_coef
    hist = tmp_path / "expM" / "Step1" / "Loop01" / "result_history"
    shutil.copytree(hist_a / "result.0001", hist / "result.0001")
    shutil.copytree(hist_a / "result.0002", hist / "result.0002")
    (hist / "result.0002" / "tR.csv").unlink()

    runner = BatchRunner({"m": hist}, config, dvtbudget_coef=coef,
                         staging_dir=tmp_path / "staging")
    result = runner.run()
    assert result.scores.height == 1
    assert result.scores["EpochNo"].to_list() == [1]
    assert "m#0002" in result.failed and "tR.csv" in result.failed["m#0002"]

    strict_runner = BatchRunner({"m": hist}, config, dvtbudget_coef=coef,
                                staging_dir=tmp_path / "staging2", strict=True)
    with pytest.raises(StrictBatchError, match="m#0002"):
        strict_runner.run()


def test_filter_no_match_epoch_is_attributed(history_tree, config_and_coef, tmp_path):
    """ある epoch にだけ A2B の行が無い場合、その epoch だけが理由つきで
    除外され、他の epoch の値は正しいまま救われる。"""
    root, hist_a, _ = history_tree
    config, coef = config_and_coef
    expected = _sequential_expected(history_tree, config, coef)

    hist = tmp_path / "expF" / "Step1" / "Loop01" / "result_history"
    shutil.copytree(hist_a / "result.0001", hist / "result.0001")
    shutil.copytree(hist_a / "result.0002", hist / "result.0002")
    # epoch2 から State=A2B(コード2) の行を消す
    fbc = hist / "result.0002" / "FBC.csv"
    pl.read_csv(fbc).filter(pl.col("State") != 2).write_csv(fbc)

    runner = BatchRunner({"expA/Step1/Loop01": hist}, config, dvtbudget_coef=coef,
                         staging_dir=tmp_path / "staging")
    result = runner.run()
    assert "expA/Step1/Loop01#0002" in result.failed
    assert result.scores["EpochNo"].to_list() == [1]
    row = result.scores.row(0, named=True)
    assert row["Score"] == pytest.approx(
        expected["expA/Step1/Loop01#0001"]["Score"], rel=1e-9
    )


def test_all_epochs_failed_raises(history_tree, config_and_coef, tmp_path):
    root, hist_a, _ = history_tree
    config, coef = config_and_coef
    hist = tmp_path / "expX" / "Step1" / "Loop01" / "result_history"
    shutil.copytree(hist_a / "result.0001", hist / "result.0001")
    (hist / "result.0001" / "FBC.csv").unlink()
    (hist / "result.0001" / "tR.csv").unlink()

    runner = BatchRunner({"x": hist}, config, dvtbudget_coef=coef,
                         staging_dir=tmp_path / "staging")
    with pytest.raises(RuntimeError, match="all 1 epochs failed"):
        runner.run()


def test_epoch_reserved_name_collision(config_and_coef):
    config, _ = config_and_coef
    bad = config.model_copy(deep=True)
    bad.optimization.groupDefs["Epoch"] = GroupDef(axis="WL", groups={"g": (0, 5)})
    with pytest.raises(ValueError, match="collides with the batch identity axis"):
        compute_score_batch([], bad)


# --- バッチサイズ advisory ------------------------------------------------


def test_advise_batch_size_auto(monkeypatch):
    monkeypatch.setattr(runner_mod, "available_memory_bytes", lambda: 3 * 2**30)
    # 100 MiB/epoch 実測 → ×3 = 300 MiB 必要 → 1 GiB 予算で 3 epoch
    size, msgs = _advise_batch_size("auto", epoch_bytes=100 * 2**20, n_epochs=1000)
    assert size == 3
    assert any("auto" in m for m in msgs)


def test_advise_batch_size_warns_when_too_big(monkeypatch):
    monkeypatch.setattr(runner_mod, "available_memory_bytes", lambda: 2**30)
    size, msgs = _advise_batch_size(50, epoch_bytes=100 * 2**20, n_epochs=1000)
    assert size == 50  # ブロックはしない
    assert any("warning" in m for m in msgs)


def test_advise_batch_size_auto_without_meminfo(monkeypatch):
    monkeypatch.setattr(runner_mod, "available_memory_bytes", lambda: None)
    size, msgs = _advise_batch_size("auto", epoch_bytes=None, n_epochs=10)
    assert size == runner_mod.DEFAULT_BATCH_SIZE


# --- CLI ------------------------------------------------------------------


def test_cli_default_batch_size_matches_runner():
    """__main__ は polars 未import で argparse を組むため定数を持つ。
    runner 側とズレたら気づけるように一致を検証する。"""
    from scorelib.batch import __main__ as cli_mod

    assert cli_mod.DEFAULT_BATCH_SIZE == runner_mod.DEFAULT_BATCH_SIZE


def test_cli_max_threads_sets_env(history_tree, config_and_coef, tmp_path, monkeypatch):
    """--max-threads は POLARS_MAX_THREADS を（polars import 前に）設定する。
    このテストプロセスでは polars が既に import 済みなので効果は子プロセス
    でしか出ないが、設定の伝播だけをここで確認する。"""
    import os

    from scorelib.batch.__main__ import main

    monkeypatch.setenv("POLARS_MAX_THREADS", "sentinel")  # 終了時に復元される
    _, hist_a, _ = history_tree
    out = tmp_path / "scores.csv"
    main([
        "--config", str(REPO_ROOT / "config_mini.jsonc"),
        "--history", str(hist_a),
        "--dvtbudget-coef", str(Path(__file__).resolve().parent / "fixtures" / "dvtbudget_coef.jsonc"),
        "--out", str(out),
        "--staging-dir", str(tmp_path / "staging"),
        "--max-threads", "2",
    ])
    assert os.environ["POLARS_MAX_THREADS"] == "2"
    assert pl.read_csv(out).height == 3


def test_benchmark_script(history_tree, config_and_coef, tmp_path):
    """scripts/benchmark_batch.py が実測表を出力する（子プロセス経由）。"""
    import subprocess
    import sys

    _, hist_a, _ = history_tree
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "benchmark_batch.py"),
         "--config", str(REPO_ROOT / "config_mini.jsonc"),
         "--history", str(hist_a),
         "--dvtbudget-coef", str(Path(__file__).resolve().parent / "fixtures" / "dvtbudget_coef.jsonc"),
         "--batch-sizes", "2,auto",
         "--staging-dir", str(tmp_path / "staging"),
         "--max-threads", "2"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    assert "batch_size" in proc.stdout
    lines = [l for l in proc.stdout.splitlines() if l.strip().startswith(("2 ", "auto"))]
    assert len(lines) == 2, proc.stdout
    # epochs 列 = 3（mini history の epoch 数）、failed 列 = 0
    for line in lines:
        cols = line.split()
        assert cols[-2] == "3" and cols[-1] == "0", proc.stdout


def test_batch_cli(history_tree, config_and_coef, tmp_path):
    from scorelib.batch.__main__ import main

    _, hist_a, hist_b = history_tree
    out = tmp_path / "scores.csv"
    main([
        "--config", str(REPO_ROOT / "config_mini.jsonc"),
        "--history", str(hist_a),
        "--history", str(hist_b),
        "--dvtbudget-coef", str(Path(__file__).resolve().parent / "fixtures" / "dvtbudget_coef.jsonc"),
        "--out", str(out),
        "--batch-size", "3",
        "--staging-dir", str(tmp_path / "staging"),
    ])
    df = pl.read_csv(out)
    assert df.height == 5
    assert df.columns[:4] == ["Epoch", "History", "EpochNo", "Score"]
    assert not out.with_suffix(out.suffix + ".failed.csv").exists()
