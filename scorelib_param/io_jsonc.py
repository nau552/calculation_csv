# Copyright (c) 2026
"""scorelib_param.models の pydantic モデル ⇔ jsonc ファイルの読み書き。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import jsonc
from .models import DvtBudgetCoefFile, RunConfig

if TYPE_CHECKING:
    from pathlib import Path


def load_run_config(path: str | Path) -> RunConfig:
    """RunConfig を jsonc ファイルから読み込む。

    Returns:
        pydantic の検証を通った RunConfig インスタンス。

    """
    return RunConfig.model_validate(jsonc.load(path))


def load_dvtbudget_coef(path: str | Path) -> DvtBudgetCoefFile:
    """係数表(dVtBudget)を jsonc ファイルから読み込む。

    Returns:
        pydantic の検証を通った DvtBudgetCoefFile インスタンス。

    """
    return DvtBudgetCoefFile.model_validate(jsonc.load(path))
