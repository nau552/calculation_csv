# Copyright (c) 2026
"""バッチ計算 CLI: ``python -m scorelib_param.batch``(docs/batch_design.md 9節)。

現行 CLI(scorelib_param.cli)の入出力契約はそのまま。こちらは過去実験の
result_history 群を受け取り、epoch ごとのスコア表を CSV に書く:

    python -m scorelib_param.batch \
        --config config.jsonc \
        --history /data/expA/Step1/Loop01/result_history \
        --history expB=/data/expB/Step2/Loop03/result_history \
        --dvtbudget-coef dvtbudget_coef.jsonc \
        --out scores.csv

- --initial-temperature は不要(epoch ごとに各 result.NNNN 内のものを読む)
- 除外 epoch は stderr と <out>.failed.csv に理由つきで出力される
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from scorelib_param import __version__, io_jsonc

# 注意: polars を import するモジュール(runner 等)はここで import しない。
# --max-threads(POLARS_MAX_THREADS)は polars の初回 import 前に設定する
# 必要があるため、main() の中で引数処理の後に import する
DEFAULT_BATCH_SIZE = 50  # runner.DEFAULT_BATCH_SIZE と同値(テストで一致を検証)

# vthSkip ダミー値を使った epoch の stderr 報告に列挙する最大件数(残りは件数のみ)
_MAX_DUMMY_EPOCHS_LISTED = 10


def _parse_histories(entries: list[str]) -> list | dict[str, str]:
    """エントリに "label=path" 形式が1つでもあれば {label: path}、無ければパスのリスト。

    (リスト形式のラベルは Step/Loop 構造から自動導出される)

    Returns:
        全エントリにラベルがあれば {ラベル: パス} の辞書、ラベルが1つも
        無ければパスのリスト(入力の並び順を保つ)。

    Raises:
        SystemExit: "label=path" と素のパスが混在したとき、または
            ラベルが重複したとき。

    """
    if not any("=" in e for e in entries):
        return entries
    labeled: dict[str, str] = {}
    for e in entries:
        if "=" not in e:
            msg = f"--history: mixing 'label=path' and plain paths is ambiguous ({e}); give every history a label"
            raise SystemExit(msg)
        label, path = e.split("=", 1)
        if label in labeled:
            msg = f"--history: duplicate label '{label}'"
            raise SystemExit(msg)
        labeled[label] = path
    return labeled


def _batch_size(value: str) -> int | str:
    if value == "auto":
        return value
    try:
        return int(value)
    except ValueError as err:
        msg = f"batch-size must be an integer or 'auto', got {value!r}"
        raise argparse.ArgumentTypeError(msg) from err


def main(argv: list[str] | None = None) -> None:
    """CLI エントリポイント: 引数を解析してバッチ計算を実行し、CSV に書き出す。

    Raises:
        SystemExit: --max-threads が 1 未満のとき、または strict モードで
            不良 epoch を検出したとき(いずれもエラーメッセージ付きで終了)。

    """
    parser = argparse.ArgumentParser(
        prog="python -m scorelib_param.batch",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"scorelib_param {__version__}")
    parser.add_argument("--config", required=True, help="run config jsonc (Generation + optimization{...})")
    parser.add_argument(
        "--history",
        required=True,
        action="append",
        help="result_history directory (repeatable; 'label=path' to name it explicitly)",
    )
    parser.add_argument("--out", required=True, help="output CSV path (Epoch,History,EpochNo,Score,<parts>...)")
    parser.add_argument(
        "--dvtbudget-coef", help="dVtBudget coefficient jsonc (required if any part uses type=dVtBudget)"
    )
    parser.add_argument("--custom-parts", help="custom_parts.py override (default: repository root)")
    parser.add_argument(
        "--generation-info",
        help="{Generation}.json with numWLs etc. (default: found in each epoch dir; "
        "needed only when group defs use physical numbering)",
    )
    parser.add_argument(
        "--batch-size",
        type=_batch_size,
        default=DEFAULT_BATCH_SIZE,
        help=f"epochs per batch, or 'auto' to size from available memory (default {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--max-prefetch",
        type=int,
        default=2,
        help="batches to fetch ahead while computing (0 = sequential; default 2)",
    )
    parser.add_argument("--staging-dir", help="work area for archive extraction / fetched data (default: a temp dir)")
    parser.add_argument("--strict", action="store_true", help="fail on the first bad epoch instead of skip-and-report")
    parser.add_argument("--keep-staging", action="store_true", help="do not delete staged data (debugging)")
    parser.add_argument(
        "--max-threads",
        type=int,
        help="limit CPU threads used by the compute engine (default: all cores). "
        "Use this when the machine is shared with other work",
    )
    args = parser.parse_args(argv)

    if args.max_threads is not None:
        if args.max_threads < 1:
            msg = "--max-threads must be >= 1"
            raise SystemExit(msg)
        # polars はプロセス内で最初に import された時点でスレッドプールを
        # 固定するため、計算モジュールの import より前に設定する
        os.environ["POLARS_MAX_THREADS"] = str(args.max_threads)
    # POLARS_MAX_THREADS の設定(上)を polars の初回 import より前に済ませるため、ここで読み込む
    from .runner import BatchRunner, StrictBatchError  # ruff: ignore[PLC0415]

    run_config = io_jsonc.load_run_config(args.config)
    coef = io_jsonc.load_dvtbudget_coef(args.dvtbudget_coef) if args.dvtbudget_coef else None

    runner = BatchRunner(
        _parse_histories(args.history),
        run_config,
        dvtbudget_coef=coef,
        batch_size=args.batch_size,
        max_prefetch=args.max_prefetch,
        staging_dir=args.staging_dir,
        strict=args.strict,
        keep_staging=args.keep_staging,
        custom_parts_path=args.custom_parts,
        generation_info_path=args.generation_info,
    )
    print(f"scorelib_param {__version__} (batch)", file=sys.stderr)
    try:
        result = runner.run()
    except StrictBatchError as err:
        msg = f"error: {err}"
        raise SystemExit(msg) from err

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    result.scores.write_csv(out)
    print(f"wrote {result.scores.height} epoch scores to {out}", file=sys.stderr)

    if result.dummy_used:
        # vthSkip: ファイルが無くダミー値で計算した epoch の報告
        # (静かに全部ダミーだった、に気づけるように)
        print(
            f"note: {len(result.dummy_used)} epoch(s) used vthSkip dummy values (measurement files absent):",
            file=sys.stderr,
        )
        for epoch, parts in sorted(result.dummy_used.items())[:_MAX_DUMMY_EPOCHS_LISTED]:
            print(f"  {epoch}: {', '.join(parts)}", file=sys.stderr)
        if len(result.dummy_used) > _MAX_DUMMY_EPOCHS_LISTED:
            print(f"  ... and {len(result.dummy_used) - _MAX_DUMMY_EPOCHS_LISTED} more", file=sys.stderr)

    if result.failed:
        failed_path = out.with_suffix(out.suffix + ".failed.csv")
        with failed_path.open("w", encoding="utf-8", newline="") as f:
            f.write("Epoch,reason\n")
            for epoch, reason in sorted(result.failed.items()):
                reason_csv = '"' + reason.replace('"', '""') + '"'
                f.write(f"{epoch},{reason_csv}\n")
        print(
            f"warning: {len(result.failed)} epoch(s) skipped — see {failed_path}",
            file=sys.stderr,
        )
        for epoch, reason in sorted(result.failed.items())[:10]:
            print(f"  {epoch}: {reason}", file=sys.stderr)


if __name__ == "__main__":
    main()
