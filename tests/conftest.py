from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def data_dir() -> Path:
    return REPO_ROOT / "result_tmp"


@pytest.fixture
def data_dir_mini() -> Path:
    return REPO_ROOT / "result_tmp_mini"


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def dvtbudget_coef_path(fixtures_dir: Path) -> Path:
    return fixtures_dir / "dvtbudget_coef.jsonc"
