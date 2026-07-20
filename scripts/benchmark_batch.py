"""実運用マシンでバッチスコア計算の所要時間・ピークメモリを実測するスクリプト。

バッチサイズごとに**別プロセス**で1回ずつ計算を走らせ（ピークメモリを
正確に測るため）、表にまとめる。結果 CSV は書かない（計測が目的）。

使い方（Ubuntu / Windows どちらでも可）:

    python scripts/benchmark_batch.py \
        --config config.jsonc \
        --history /data/expA/Step1/Loop01/result_history \
        --dvtbudget-coef dvtbudget_coef.jsonc \
        --batch-sizes auto,10,25,50 \
        [--max-threads 4] [--max-prefetch 2] [--staging-dir DIR]

出力例:

    batch_size  time[s]  peak[GiB]  epochs  failed
          auto     10.5       3.1       50       0
            10     10.5       3.7       50       0
            ...

注意:
- 1回目の実行は OS のファイルキャッシュが冷えているため I/O 分だけ遅く
  出ることがある。傾向を見るには同じサイズを2回測る（--repeat 2）とよい。
- バッチサイズは主に**ピークメモリ**を決め、所要時間はほぼ変わらない
  （epoch 数に対して線形）。CPU 使用を抑えたい場合は --max-threads を使う。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def _peak_memory_gib():
    """自プロセスのピークメモリ（working set / RSS）。取れない環境は None。"""
    try:
        import resource  # Unix

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux は KiB、macOS はバイト
        return peak / 2**30 if sys.platform == "darwin" else peak * 1024 / 2**30
    except ImportError:
        pass
    if sys.platform == "win32":
        import ctypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32)] + [
                (n, ctypes.c_size_t)
                for n in ("PeakWorkingSetSize", "WorkingSetSize",
                          "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                          "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                          "PagefileUsage", "PeakPagefileUsage")]

        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        fn = k32.K32GetProcessMemoryInfo
        fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        pmc = PMC(cb=ctypes.sizeof(PMC))
        if fn(k32.GetCurrentProcess(), ctypes.byref(pmc), ctypes.sizeof(PMC)):
            return pmc.PeakWorkingSetSize / 2**30
    return None


def _run_one(args) -> None:
    """子プロセスモード: 1つのバッチサイズで計算して結果を1行出力する。"""
    from scorelib_param import io_jsonc
    from scorelib_param.batch import BatchRunner
    from scorelib_param.batch.__main__ import _parse_histories

    config = io_jsonc.load_run_config(args.config)
    coef = io_jsonc.load_dvtbudget_coef(args.dvtbudget_coef) if args.dvtbudget_coef else None
    batch_size = args.one if args.one == "auto" else int(args.one)

    t0 = time.perf_counter()
    runner = BatchRunner(
        _parse_histories(args.history),
        config,
        dvtbudget_coef=coef,
        batch_size=batch_size,
        max_prefetch=args.max_prefetch,
        staging_dir=args.staging_dir,
        custom_parts_path=args.custom_parts,
    )
    result = runner.run()
    elapsed = time.perf_counter() - t0
    peak = _peak_memory_gib()
    print(
        f"RESULT\t{args.one}\t{elapsed:.1f}\t"
        f"{'n/a' if peak is None else f'{peak:.2f}'}\t"
        f"{result.scores.height}\t{len(result.failed)}"
    )


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--history", required=True, action="append",
                        help="result_history directory (repeatable; 'label=path' also ok)")
    parser.add_argument("--dvtbudget-coef")
    parser.add_argument("--custom-parts")
    parser.add_argument("--batch-sizes", default="auto,10,25,50",
                        help="comma-separated sizes to test (integers and/or 'auto')")
    parser.add_argument("--repeat", type=int, default=1, help="measurements per size (default 1)")
    parser.add_argument("--max-prefetch", type=int, default=2)
    parser.add_argument("--staging-dir")
    parser.add_argument("--max-threads", type=int,
                        help="limit compute threads (sets POLARS_MAX_THREADS in each run)")
    parser.add_argument("--one", help=argparse.SUPPRESS)  # 内部用: 子プロセスモード
    args = parser.parse_args(argv)

    if args.one:
        _run_one(args)
        return

    env = dict(os.environ)
    if args.max_threads:
        env["POLARS_MAX_THREADS"] = str(args.max_threads)

    child_args = [sys.executable, str(Path(__file__).resolve())]
    for k in ("config", "dvtbudget_coef", "custom_parts", "staging_dir"):
        v = getattr(args, k)
        if v:
            child_args += [f"--{k.replace('_', '-')}", str(v)]
    for h in args.history:
        child_args += ["--history", h]
    child_args += ["--max-prefetch", str(args.max_prefetch)]

    rows = []
    sizes = [s.strip() for s in args.batch_sizes.split(",") if s.strip()]
    for size in sizes:
        for _ in range(args.repeat):
            # ピークメモリはプロセス単位でしか正確に取れないため、
            # 計測1回 = 子プロセス1つ（stderr には runner の advisory がそのまま出る）
            proc = subprocess.run(
                child_args + ["--one", size], env=env,
                capture_output=True, text=True,
            )
            result_lines = [l for l in proc.stdout.splitlines() if l.startswith("RESULT\t")]
            if proc.returncode != 0 or not result_lines:
                print(f"batch_size={size}: FAILED", file=sys.stderr)
                sys.stderr.write(proc.stderr[-2000:] if proc.stderr else "(no stderr)\n")
                rows.append((size, None))
                continue
            rows.append((size, result_lines[-1].split("\t")[1:]))

    print(f"\n{'batch_size':>10}  {'time[s]':>7}  {'peak[GiB]':>9}  {'epochs':>6}  {'failed':>6}")
    for size, r in rows:
        if r is None:
            print(f"{size:>10}  {'FAILED':>7}")
        else:
            _, elapsed, peak, epochs, failed = r
            print(f"{size:>10}  {elapsed:>7}  {peak:>9}  {epochs:>6}  {failed:>6}")


if __name__ == "__main__":
    main()
