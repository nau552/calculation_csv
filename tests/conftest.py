# Copyright (c) 2026
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true] 起動するのは自リポジトリの CLI で引数も固定
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def data_dir_mini() -> Path:
    """ミニ実データディレクトリ result_tmp_mini のパス。

    Returns:
        リポジトリ同梱の tests/data/result_tmp_mini を指す絶対パス。

    """
    return Path(__file__).resolve().parent / "data" / "result_tmp_mini"


@pytest.fixture
def data_dir_mini_no_override_true(data_dir_mini: Path, tmp_path: Path) -> Path:
    """Read_Override が全行 False(基準側のみ)の mini データ複製。

    実機報告(2026-07-31)のダミーデータの形: parameterLabel に評価側
    (Override=True)の測定が1つも無く、相対化パーツが計算不能になる。

    Returns:
        複製した一時データディレクトリのパス。

    """
    dest = tmp_path / "no_override_true"
    shutil.copytree(data_dir_mini, dest)
    plabel = dest / "parameterLabel_FBC.csv"
    lines = plabel.read_text(encoding="utf-8").splitlines()
    header = lines[0].split(",")
    idx = header.index("Read_Override")
    for i, line in enumerate(lines[1:], start=1):
        cells = line.split(",")
        if len(cells) == len(header):
            cells[idx] = "0"
            lines[i] = ",".join(cells)
    plabel.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


@pytest.fixture
def fixtures_dir() -> Path:
    """テスト用フィクスチャディレクトリのパス。

    Returns:
        tests/fixtures を指す絶対パス。

    """
    return Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def dvtbudget_coef_path(fixtures_dir: Path) -> Path:
    """係数フィクスチャ dvtbudget_coef.jsonc のパス。

    Returns:
        tests/fixtures/dvtbudget_coef.jsonc を指す絶対パス。

    """
    return fixtures_dir / "dvtbudget_coef.jsonc"


@pytest.fixture(scope="session")
def expanded_mini_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """現行の展開スクリプトで正解データを生成した自己完結コピー。

    result_tmp_mini の自己完結コピーに、**現行の展開スクリプト**
    (reference_scripts/expand_FBC_measure.py) をそのまま実行して正解データ
    FBC_expanded.csv を生成したもの。エンジンの結果を「現行方式の答え」と
    照合するための基準データ(スクリプトは ../result_tmp を見るため、
    一時ディレクトリに同じ配置を再現して走らせる)。

    Returns:
        FBC_expanded.csv を含む result_tmp コピーのディレクトリパス。

    """
    root = tmp_path_factory.mktemp("expand")
    data_copy = root / "result_tmp"
    shutil.copytree(Path(__file__).resolve().parent / "data" / "result_tmp_mini", data_copy)

    script_dir = root / "scripts"
    script_dir.mkdir()
    script_copy = script_dir / "expand_FBC_measure.py"
    shutil.copy(REPO_ROOT / "reference_scripts" / "expand_FBC_measure.py", script_copy)

    subprocess.run([sys.executable, str(script_copy)], check=True, capture_output=True)
    assert (data_copy / "FBC_expanded.csv").exists()
    return data_copy
