"""Load/save the pydantic models in scorelib.models to/from jsonc files."""
from __future__ import annotations

from pathlib import Path

from . import jsonc
from .models import DvtBudgetCoefFile, RunConfig, ScoreFile


def load_run_config(path: str | Path) -> RunConfig:
    return RunConfig.model_validate(jsonc.load(path))


def save_run_config(config: RunConfig, path: str | Path) -> None:
    jsonc.dump(config.model_dump(mode="json"), path)


def load_score_file(path: str | Path) -> ScoreFile:
    return ScoreFile.model_validate(jsonc.load(path))


def save_score_file(score_file: ScoreFile, path: str | Path) -> None:
    jsonc.dump(score_file.model_dump(mode="json"), path)


def load_dvtbudget_coef(path: str | Path) -> DvtBudgetCoefFile:
    return DvtBudgetCoefFile.model_validate(jsonc.load(path))
