# Copyright (c) 2026
"""scorelib_param.models の pydantic モデル ⇔ jsonc ファイルの読み書き。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import jsonc
from .models import DvtBudgetCoefFile, RunConfig, ScoreFile

if TYPE_CHECKING:
    from pathlib import Path


def load_run_config(path: str | Path) -> RunConfig:
    """RunConfig を jsonc ファイルから読み込む。"""
    return RunConfig.model_validate(jsonc.load(path))


def save_run_config(config: RunConfig, path: str | Path) -> None:
    """RunConfig を jsonc ファイルへ書き出す。"""
    jsonc.dump(config.model_dump(mode="json"), path)


def load_score_file(path: str | Path) -> ScoreFile:
    """ScoreFile を jsonc ファイルから読み込む。"""
    return ScoreFile.model_validate(jsonc.load(path))


def save_score_file(score_file: ScoreFile, path: str | Path) -> None:
    """ScoreFile を jsonc ファイルへ書き出す。"""
    jsonc.dump(score_file.model_dump(mode="json"), path)


def load_dvtbudget_coef(path: str | Path) -> DvtBudgetCoefFile:
    """係数表(dVtBudget)を jsonc ファイルから読み込む。"""
    return DvtBudgetCoefFile.model_validate(jsonc.load(path))
