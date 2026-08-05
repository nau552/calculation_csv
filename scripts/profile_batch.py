# Copyright (c) 2026
# ruff: file-ignore[implicit-namespace-package] 単体実行スクリプト置き場でパッケージではない(__init__.py を持たない)
"""バッチスコア計算の時間内訳をプロファイルする(どこを最適化すべきかの判断用)。

benchmark_batch.py が「全体の所要時間・ピークメモリ」を測るのに対し、こちらは
同じ計算を段階に分けて測る:

    A. {type}.csv の素のパース時間(scan_csv().collect() を epoch 数分)
    B. resolved() = 軸解決 join + 文字列解決 + concat + collect(type ごと)
    C. compute_score_batch 全体 — C-B がパーツ計算(filter/相対化/集計)分

使い方(history は make_bench_history.py で作ったもの、または実運用のもの。
非圧縮 csv の epoch ディレクトリ前提):

    python scripts/profile_batch.py --config config_bench.jsonc \
        --history result_tmp_bench_history --dvtbudget-coef dvtbudget_coef.jsonc \
        [--epochs 10]

注意: OS のファイルキャッシュが効いた状態を測る(パース系は2回走らせて
2回目を採用)。初回コールドリードの I/O を含めたい場合は benchmark_batch.py
を使うこと。2026-07 の実測(i7-11700K, result_tmp_full, config_bench)では
パース ~3% / resolve+collect ~11% / パーツ計算 ~89% だった。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from scorelib_param.models import GroupDef, ScoreFile

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # ruff: ignore[module-import-not-at-top-of-file]

from scorelib_param import axis_resolve, io_jsonc  # ruff: ignore[module-import-not-at-top-of-file]
from scorelib_param.batch import compute  # ruff: ignore[module-import-not-at-top-of-file]
from scorelib_param.batch.history import enumerate_epochs  # ruff: ignore[module-import-not-at-top-of-file]
from scorelib_param.batch.staging import StagedEpoch  # ruff: ignore[module-import-not-at-top-of-file]
from scorelib_param.cli import resolve_group_defs  # ruff: ignore[module-import-not-at-top-of-file]


def _timeit[T](fn: Callable[[], T]) -> tuple[float, T]:
    """関数 fn を1回実行し、(所要秒, 返り値) を返す。

    Returns:
        (fn の実行にかかった秒数, fn が返した値) のタプル。秒数は
        time.perf_counter 基準。

    """
    t0 = time.perf_counter()
    r = fn()
    return time.perf_counter() - t0, r


def _load_epochs(args: argparse.Namespace) -> list[StagedEpoch]:
    """計測に使う epoch を history の先頭から --epochs 個列挙し、計算層へ渡せる形にする。

    Returns:
        StagedEpoch のリスト。非圧縮 csv の epoch ディレクトリ前提のため
        ステージング(アーカイブ展開)は通さず、元ディレクトリをそのまま使う。

    """
    refs = enumerate_epochs({"bench": args.history})[: args.epochs]
    if len(refs) < args.epochs:
        print(f"note: history has only {len(refs)} epochs", file=sys.stderr)
    return [StagedEpoch(ref=r, data_dir=r.source_dir) for r in refs]


def _measure_parse(epochs: list[StagedEpoch], source_types: list[str]) -> float:
    """段階 A: {type}.csv の素のパース時間を測る(2回走らせて2回目を採用しキャッシュ差を除く)。

    Returns:
        全 epoch・全 type の scan_csv().collect() の所要秒数(2回目)。

    """

    def parse_all() -> None:
        for se in epochs:
            for t in source_types:
                pl.scan_csv(axis_resolve.data_file(se.data_dir, f"{t}.csv")).collect()

    _timeit(parse_all)
    t_parse, _ = _timeit(parse_all)
    return t_parse


def _measure_resolved(
    epochs: list[StagedEpoch],
    score_file: ScoreFile,
    group_defs: dict[str, GroupDef],
    source_types: list[str],
) -> tuple[float, compute.BatchComputeContext]:
    """段階 B: resolved() = 軸解決 join + 文字列解決 + concat + collect の時間を type 分測る。

    Returns:
        (所要秒数, 全 type を collect 済みの BatchComputeContext) のタプル。

    """

    def run_resolved() -> compute.BatchComputeContext:
        c = compute.BatchComputeContext(epochs, score_file.score_parts, group_defs)
        for t in source_types:
            c.resolved(t)
        return c

    return _timeit(run_resolved)


def _print_type_sizes(ctx: compute.BatchComputeContext, source_types: list[str]) -> None:
    """各 type の resolved 済み DataFrame の行数・メモリサイズを表示する。"""
    for t in source_types:
        # _measure_resolved が全 type を collect 済みなので、resolved() はキャッシュ
        # (内部 dict)の同じ実体を返すだけ(再計算しない)
        df = ctx.resolved(t)
        print(f"     {t}: {df.height:,} rows, {df.estimated_size() / 2**30:.2f} GiB")


def main(argv: list[str] | None = None) -> None:
    """コマンドライン引数に従い、バッチ計算の時間内訳を段階別に計測して表示する。"""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True)
    parser.add_argument("--history", required=True)
    parser.add_argument("--dvtbudget-coef")
    parser.add_argument("--epochs", type=int, default=10, help="使う epoch 数(history の先頭から。default: 10)")
    args = parser.parse_args(argv)

    epochs = _load_epochs(args)
    run_config = io_jsonc.load_run_config(args.config)
    coef = io_jsonc.load_dvtbudget_coef(args.dvtbudget_coef) if args.dvtbudget_coef else None
    score_file = run_config.to_score_file()
    group_defs = resolve_group_defs(run_config, epochs[0].data_dir, None)

    ctx = compute.BatchComputeContext(epochs, score_file.score_parts, group_defs)
    source_types = sorted(ctx._union_axes)  # ruff: ignore[private-member-access] — プロファイル対象(同リポジトリ)の内部を意図的に参照

    t_parse = _measure_parse(epochs, source_types)
    t_resolved, ctx = _measure_resolved(epochs, score_file, group_defs, source_types)

    # --- C: 全体 ---
    t_full, result = _timeit(lambda: compute.compute_score_batch(epochs, run_config, coef))

    print(
        f"epochs={len(epochs)}  types={source_types}  parts={len(score_file.score_parts)}  failed={len(result.failed)}"
    )
    print(f"A. csv parse:            {t_parse:7.2f}s  ({t_parse / t_full:5.1%})")
    print(f"B. resolve+collect:      {t_resolved:7.2f}s  ({t_resolved / t_full:5.1%})")
    _print_type_sizes(ctx, source_types)
    print(
        f"C. compute_score_batch:  {t_full:7.2f}s  (パーツ計算分 ≈ {t_full - t_resolved:.2f}s, "
        f"{(t_full - t_resolved) / t_full:5.1%})"
    )
    if result.failed:
        print("failed epochs:", dict(list(result.failed.items())[:3]), file=sys.stderr)


if __name__ == "__main__":
    main()
