"""1 epoch 分のデータディレクトリから、ベンチマーク用の result_history を組み立てる。

同じ epoch ディレクトリを result.0001 .. result.NNNN として複製する。複製は
NTFS ハードリンク（同一ボリューム）なのでディスク消費は実質ゼロ。クロス
ボリューム等でハードリンクできない場合はコピーにフォールバックする。

使い方:

    python scripts/make_bench_history.py                 # result_tmp_full -> result_tmp_bench_history に 100 epoch
    python scripts/make_bench_history.py --source result_tmp_full --dest my_bench --epochs 50

でき上がった history はそのまま benchmark_batch.py / profile_batch.py に渡せる:

    python scripts/benchmark_batch.py --config config_bench.jsonc \
        --history bench=result_tmp_bench_history --dvtbudget-coef dvtbudget_coef.jsonc \
        --batch-sizes auto,10,50
    python scripts/profile_batch.py --config config_bench.jsonc \
        --history result_tmp_bench_history --dvtbudget-coef dvtbudget_coef.jsonc
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source", default=str(REPO / "result_tmp_full"),
                        help="1 epoch 分のデータディレクトリ (default: result_tmp_full)")
    parser.add_argument("--dest", default=str(REPO / "result_tmp_bench_history"),
                        help="作成する result_history のパス (default: result_tmp_bench_history)")
    parser.add_argument("--epochs", type=int, default=100)
    args = parser.parse_args(argv)

    source = Path(args.source)
    dest = Path(args.dest)
    if not source.is_dir():
        sys.exit(f"source not found: {source}")
    if dest.exists():
        sys.exit(f"dest already exists: {dest} (delete it first to rebuild)")

    files = sorted(p for p in source.iterdir() if p.is_file())
    if not files:
        sys.exit(f"no files in {source}")

    dest.mkdir(parents=True)
    linked = True
    for n in range(1, args.epochs + 1):
        d = dest / f"result.{n:04d}"
        d.mkdir()
        for src in files:
            try:
                os.link(src, d / src.name)
            except OSError:
                shutil.copy(src, d / src.name)
                linked = False

    how = "hardlink" if linked else "copy (hardlink unavailable)"
    print(f"{args.epochs} epochs x {len(files)} files ({how}) -> {dest}")


if __name__ == "__main__":
    main()
