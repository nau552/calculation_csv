"""vthSkip（ファイル不在 epoch のダミー計算）のテスト。

- ダミー値は「変換後の値」: 変換ステップはスキップ・集計だけ適用
  （cli.compute_dummy_part — models.VthSkipConfig）
- 典型例: KLD ダミー 0 → 0.0、dVthSGWLD ダミー 1 → 残す8要素の総和 = 8.0
- batch は epoch ごとにファイル有無を判定し、ダミー使用を dummy_used に報告する
"""
import shutil
from pathlib import Path

import pytest

from scorelib_param.batch import BatchRunner, compute_score_batch, enumerate_epochs, stage_epoch
from scorelib_param.cli import compute_dummy_part, compute_score_file
from scorelib_param.models import RunConfig, ScorePart

MINI = Path(__file__).resolve().parent / "data" / "result_tmp_mini"

KLD_PART = {
    "name": "kld", "type": "KLD",
    "order": ["Board", "Chip", "__log__", "SGWLD"],
    "aggregations": {
        "Board": {"op": "mean"}, "Chip": {"op": "mean"},
        "__log__": {"op": "log", "floor": 1e-6},
        "SGWLD": {"op": "sum", "weight": 0.1},
    },
}
DVTH_KEPT = ["DS0", "DS1", "DS2", "DL0", "DU0", "DD2", "DD1", "DD0"]  # SG系4要素を除く8つ
DVTH_PART = {
    "name": "dvth", "type": "dVthSGWLD",
    "order": ["Board", "Chip", "Block", "__abs__", "SGWLD"],
    "aggregations": {
        "Board": {"op": "mean"}, "Chip": {"op": "mean"}, "Block": {"op": "mean"},
        "__abs__": {"op": "abs"},
        "SGWLD": {"op": "sum", "value": DVTH_KEPT},
    },
}
VTHSKIP = {"epochs": 100, "dummyKLDValue": 0, "dummyDVthValue": 1}


def _run_config(vthskip=None) -> RunConfig:
    opt = {"score_parts": [KLD_PART, DVTH_PART], "expression": "kld + dvth"}
    if vthskip is not None:
        opt["vthSkip"] = vthskip
    return RunConfig.model_validate({"Generation": "B9LS", "optimization": opt})


def _dir_without_files(tmp_path, *names) -> Path:
    d = tmp_path / "epoch"
    shutil.copytree(MINI, d)
    for name in names:
        (d / name).unlink()
    return d


# --- compute_dummy_part（意味論の要）---------------------------------------


def test_dummy_kld_is_zero(data_dir_mini):
    """KLD ダミー 0（log 域の慣習値）: log はスキップ・重み 0.1 と総和は適用 → 0。"""
    assert compute_dummy_part(data_dir_mini, ScorePart.model_validate(KLD_PART), 0.0) == 0.0


def test_dummy_dvth_is_count_of_kept_elements(data_dir_mini):
    """dVthSGWLD ダミー 1: abs スキップ・選択（8要素）つき総和は適用 → 8。"""
    assert compute_dummy_part(data_dir_mini, ScorePart.model_validate(DVTH_PART), 1.0) == 8.0


def test_dummy_rejects_relative_parts(data_dir_mini):
    part = ScorePart.model_validate({
        **KLD_PART,
        "relative": {"split_axis": "SGWLD", "numerator_when": "DS0", "denominator_when": "DS1"},
    })
    with pytest.raises(ValueError, match="relative"):
        compute_dummy_part(data_dir_mini, part, 0.0)


# --- compute_score_file（単一 epoch = 最適化実行の形）-----------------------


def test_score_file_uses_dummy_when_files_missing(tmp_path):
    d = _dir_without_files(tmp_path, "KLD.csv", "dVthSGWLD.csv")
    values = compute_score_file(d, _run_config(VTHSKIP))
    assert values["kld"] == 0.0
    assert values["dvth"] == 8.0
    assert values["Score"] == 8.0


def test_score_file_computes_normally_when_files_exist(data_dir_mini):
    """ファイルがあれば vthSkip 設定は使われない（実測値で計算される）。"""
    with_skip = compute_score_file(data_dir_mini, _run_config(VTHSKIP))
    without = compute_score_file(data_dir_mini, _run_config())
    # 並列集計の加算順で最終ビットが揺れうるため approx 比較
    assert with_skip.keys() == without.keys()
    for k in without:
        assert with_skip[k] == pytest.approx(without[k]), k
    assert with_skip["dvth"] != pytest.approx(8.0)  # 実測値（ダミーの 8 とは異なる）


def test_score_file_errors_without_vthskip(tmp_path):
    d = _dir_without_files(tmp_path, "KLD.csv")
    with pytest.raises(Exception, match="KLD"):
        compute_score_file(d, _run_config())


# --- batch（過去データ流用の形）--------------------------------------------


@pytest.fixture
def mixed_history(tmp_path):
    """epoch1 = 全ファイルあり、epoch2 = KLD/dVthSGWLD 無し（vthSkip 中を模す）。"""
    hist = tmp_path / "exp" / "Step1" / "Loop01" / "result_history"
    shutil.copytree(MINI, hist / "result.0001")
    shutil.copytree(MINI, hist / "result.0002")
    (hist / "result.0002" / "KLD.csv").unlink()
    (hist / "result.0002" / "dVthSGWLD.csv").unlink()
    return hist


def test_batch_fills_dummy_and_reports(mixed_history, tmp_path):
    refs = enumerate_epochs([mixed_history])
    staged = [stage_epoch(r, tmp_path / "staging") for r in refs]
    result = compute_score_batch(staged, _run_config(VTHSKIP))
    assert result.failed == {}
    rows = {r["Epoch"]: r for r in result.scores.to_dicts()}
    e1, e2 = "exp/Step1/Loop01#0001", "exp/Step1/Loop01#0002"
    # epoch1 は実測値 = 単一 epoch 計算と一致、epoch2 はダミー値
    expected1 = compute_score_file(staged[0].data_dir, _run_config(VTHSKIP))
    assert rows[e1]["kld"] == pytest.approx(expected1["kld"])
    assert rows[e1]["dvth"] == pytest.approx(expected1["dvth"])
    assert rows[e2]["kld"] == 0.0
    assert rows[e2]["dvth"] == 8.0
    # ダミー使用の報告（epoch → パーツ名）
    assert result.dummy_used == {e2: ["kld", "dvth"]}


def test_batch_fails_missing_epoch_without_dummy(mixed_history, tmp_path):
    refs = enumerate_epochs([mixed_history])
    staged = [stage_epoch(r, tmp_path / "staging") for r in refs]
    result = compute_score_batch(staged, _run_config())
    e2 = "exp/Step1/Loop01#0002"
    assert e2 in result.failed
    assert "not found" in result.failed[e2]
    assert result.scores.height == 1  # epoch1 は救われる


def test_runner_exempts_dummy_types_from_validation(mixed_history):
    """BatchRunner 経由: 事前検証（validate_epoch）がダミー対象 type の欠落で
    epoch を落とさないこと。"""
    runner = BatchRunner([mixed_history], _run_config(VTHSKIP), batch_size=10, max_prefetch=0)
    result = runner.run()
    assert result.failed == {}
    assert result.scores.height == 2
    assert list(result.dummy_used) == ["exp/Step1/Loop01#0002"]
