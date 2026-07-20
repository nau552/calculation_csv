"""過去実験データのバッチスコア計算（docs/batch_design.md）。

複数の result_history（過去実験の epoch 群）を受け取り、現行のスコア設計
（config.jsonc）でバッチ単位に一括計算する。エンジン本体（scorelib_param/*.py）の
単一 epoch 計算とは数値等価（tests/test_batch.py で保証）。

CLI: ``python -m scorelib_param.batch --config ... --history ... --out scores.csv``

このモジュールは**遅延インポート**（PEP 562）にしてある: CLI の
``--max-threads`` は環境変数 POLARS_MAX_THREADS を **polars の初回 import
より前に**設定する必要があり、パッケージ import 時点で polars を
読み込んでしまうと効かなくなるため。``from scorelib_param.batch import X`` は
従来どおり動く。
"""
import importlib

_EXPORTS = {
    "EPOCH_COL": "compute",
    "BatchComputeContext": "compute",
    "BatchResult": "compute",
    "compute_score_batch": "compute",
    "EpochRef": "history",
    "derive_label": "history",
    "enumerate_epochs": "history",
    "BatchRunner": "runner",
    "DEFAULT_BATCH_SIZE": "runner",
    "Fetcher": "runner",
    "StrictBatchError": "runner",
    "passthrough_fetcher": "runner",
    "StagedEpoch": "staging",
    "cleanup_epoch": "staging",
    "stage_epoch": "staging",
    "validate_epoch": "staging",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name):
    if name in _EXPORTS:
        module = importlib.import_module(f".{_EXPORTS[name]}", __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return __all__
