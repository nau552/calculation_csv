import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def data_dir_mini() -> Path:
    return Path(__file__).resolve().parent / "data" / "result_tmp_mini"


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def dvtbudget_coef_path(fixtures_dir: Path) -> Path:
    return fixtures_dir / "dvtbudget_coef.jsonc"


@pytest.fixture(scope="session")
def expanded_mini_dir(tmp_path_factory) -> Path:
    """result_tmp_mini の自己完結コピーに、**現行の展開スクリプト**
    (reference_scripts/expand_FBC_measure.py) をそのまま実行して正解データ
    FBC_expanded.csv を生成したもの。エンジンの結果を「現行方式の答え」と
    照合するための基準データ（スクリプトは ../result_tmp を見るため、
    一時ディレクトリに同じ配置を再現して走らせる）。"""
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
