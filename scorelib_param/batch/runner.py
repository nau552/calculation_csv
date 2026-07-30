# Copyright (c) 2026
"""バッチ実行のオーケストレータ。

「取得(fetch) → ステージング(展開・検証) → バッチ計算 → ステージング削除」を
バッチ単位のパイプラインで回す(docs/batch_design.md 6節):

- 計算中に裏で次の最大 `max_prefetch` バッチを先行取得する
  (ディスク使用量の上限 = (1 + max_prefetch) * 1バッチ分)
- 削除するのはステージング領域(fetch 先・展開ビュー)のみ。
  ローカル既存データ(pass-through)は削除しない
- fetch 手段は差し替え可能な callable(Fetcher)。デフォルトはローカル/
  共有マウント済みパスの pass-through。scp 等はこのインターフェースの
  実装を1つ足すだけでよい
"""

from __future__ import annotations

import contextlib
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import polars as pl

from .compute import BatchComputeContext, BatchResult, _result_frame, compute_score_batch
from .history import EpochRef, enumerate_epochs
from .staging import StagedEpoch, cleanup_epoch, stage_epoch, validate_epoch

if TYPE_CHECKING:
    from scorelib_param.models import DvtBudgetCoefFile, RunConfig

DEFAULT_BATCH_SIZE = 50
# 1 epoch の解決済みフレーム実測サイズに対する、計算中の中間結果
# (相対化・キャッシュ点・group_by)を見込んだ安全係数
MEMORY_FACTOR = 3.0
# 推奨バッチサイズは「利用可能メモリのこの割合に収まる」ように選ぶ
MEMORY_BUDGET_RATIO = 1 / 3

# fetch の契約: (epoch参照, ステージング領域) → ローカルの epoch ディレクトリ。
# リモート実装は staging_root 配下に取得して返すこと(計算後に削除される)。
# ブロッキングでよい(Runner 側が並行化する)
Fetcher = Callable[[EpochRef, Path], Path]


# staging_root は差し替え可能な fetch 実装の共通シグネチャ(上の Fetcher 契約)。pass-through では使わない
def passthrough_fetcher(ref: EpochRef, staging_root: Path) -> Path:  # ruff: ignore[ARG001]
    """ローカル/共有マウント済みデータをそのまま使う(コピーも削除もしない)。

    Returns:
        `ref.source_dir` そのもの(ステージング領域には何も作らない)。

    """
    return ref.source_dir


class StrictBatchError(RuntimeError):
    """strict モードで不良 epoch を検出したとき送出される。"""


def available_memory_bytes() -> int | None:
    """利用可能メモリを返す。

    Linux は /proc/meminfo(追加依存なし・古い Ubuntu でも可)、それ以外は
    psutil があれば使用、どちらも無ければ None(advisory をスキップ)。

    Returns:
        利用可能メモリのバイト数。どの手段でも取得できなければ None。

    """
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    try:
        # psutil は任意依存(無ければ except に落ちて advisory をスキップする)。あるときだけ読み込む
        import psutil  # ruff: ignore[PLC0415]

        return int(psutil.virtual_memory().available)
    except Exception:  # ruff: ignore[BLE001] — advisory なので静かに諦める
        return None


def estimate_epoch_bytes(sample: StagedEpoch, run_config: RunConfig) -> int | None:
    """最初の epoch を実際に解決してメモリ足跡を実測する(advisory 用)。

    Returns:
        1 epoch 分の解決済みフレームの実測バイト数。custom のみの設定や
        見積もり中のエラーで測れなかった場合は None。

    """
    try:
        score_file = run_config.to_score_file()
        parts = [p for p in score_file.score_parts if p.type != "custom"]
        if not parts:
            return None
        ctx = BatchComputeContext([sample], parts, run_config.group_defs())
        # 同一パッケージ内部での意図的な利用(compute の実測サイズ見積もりを advisory に使う)。
        # estimated_size() の型は int | float だがバイト数なので int に包んでも値は不変
        return int(sum(ctx.resolved(st).estimated_size() for st in ctx._union_axes))  # ruff: ignore[SLF001]
    except Exception:  # ruff: ignore[BLE001] — 見積もり失敗は advisory を諦めるだけ
        return None


def _advise_batch_size(requested: int | str, epoch_bytes: int | None, n_epochs: int) -> tuple[int, list[str]]:
    """batch_size の解決と助言メッセージ(docs/batch_design.md 6.3節)。

    実行をブロックしない。

    Returns:
        (確定した batch_size, stderr に出す助言メッセージのリスト)のタプル。

    Raises:
        ValueError: batch_size に 1 未満の整数が指定されたとき。

    """
    msgs: list[str] = []
    available = available_memory_bytes()
    per_epoch = int(epoch_bytes * MEMORY_FACTOR) if epoch_bytes else None

    if requested == "auto":
        if per_epoch and available:
            size = max(1, int(available * MEMORY_BUDGET_RATIO / per_epoch))
            size = min(size, max(n_epochs, 1))
            msgs.append(
                f"batch-size auto: {size} epochs/batch "
                f"(~{per_epoch // 2**20} MiB/epoch estimated, "
                f"{available // 2**20} MiB available)"
            )
        else:
            size = DEFAULT_BATCH_SIZE
            msgs.append(f"batch-size auto: memory info unavailable, using default {size}")
        return size, msgs

    size = int(requested)
    if size < 1:
        msg = f"batch_size must be >= 1, got {size}"
        raise ValueError(msg)
    if per_epoch and available:
        needed = size * per_epoch
        if needed > available * 0.8:
            msgs.append(
                f"warning: batch_size {size} may need ~{needed // 2**20} MiB but only "
                f"{available // 2**20} MiB is available — consider a smaller batch size "
                f"(~{max(1, int(available * MEMORY_BUDGET_RATIO / per_epoch))})"
            )
        elif needed < available * 0.05 and size < n_epochs:
            msgs.append(
                f"note: plenty of memory headroom "
                f"(~{needed // 2**20} MiB of {available // 2**20} MiB); batch_size could "
                f"be raised to ~{int(available * MEMORY_BUDGET_RATIO / per_epoch)} for speed"
            )
    return size, msgs


class BatchRunner:
    """複数 result_history の全 epoch をバッチ単位で計算する。

    使い方::

        runner = BatchRunner(["/path/to/expA/Step1/Loop01/result_history", ...],
                             run_config, dvtbudget_coef=coef)
        result = runner.run()          # まとめて(scores: DataFrame, failed: dict)
        for batch in runner.run_iter():  # バッチごとに逐次受け取る
            ...
    """

    def __init__(
        self,
        histories: Sequence[str | Path] | Mapping[str, str | Path],
        run_config: RunConfig,
        *,
        dvtbudget_coef: DvtBudgetCoefFile | None = None,
        batch_size: int | str = DEFAULT_BATCH_SIZE,
        max_prefetch: int = 2,
        staging_dir: str | Path | None = None,
        strict: bool = False,
        keep_staging: bool = False,
        fetcher: Fetcher | None = None,
        custom_parts_path: str | Path | None = None,
        generation_info_path: str | Path | None = None,
    ) -> None:
        """実行設定を保持し、ステージング領域と事前検証の必須 type を準備する。"""
        self.histories = histories
        self.run_config = run_config
        self.dvtbudget_coef = dvtbudget_coef
        self.batch_size = batch_size
        self.max_prefetch = max(0, int(max_prefetch))
        self.strict = strict
        self.keep_staging = keep_staging
        self.fetcher: Fetcher = fetcher or passthrough_fetcher
        self.custom_parts_path = custom_parts_path
        self.generation_info_path = generation_info_path

        self._own_staging = staging_dir is None
        self.staging_root = Path(staging_dir or tempfile.mkdtemp(prefix="scorelib_batch_"))
        self.staging_root.mkdir(parents=True, exist_ok=True)

        # cli 側の補助関数はこの初期化経路でのみ使うため、使うときだけ読み込む
        from scorelib_param.cli import _source_type  # ruff: ignore[PLC0415]

        parts = run_config.optimization.score_parts
        # vthSkip でダミー値が設定された type はファイルが無くても計算できる
        # (compute.py が epoch ごとに埋める)ため、事前検証の必須対象から外す
        vth = run_config.optimization.vthSkip
        dummy_types = set(vth.dummy_values()) if vth else set()
        self._required_types = sorted({_source_type(p) for p in parts if p.type != "custom"} - dummy_types)
        self._needs_dvt = any(p.type == "dVtBudget" for p in parts)
        self._part_names = [p.name for p in parts]

    # --- 1 epoch の準備(fetch → ステージング → 検証)。worker スレッドで走る ---

    def _prepare_epoch(self, ref: EpochRef) -> tuple[StagedEpoch, Path | None]:
        """戻り値: (staged, 削除すべき fetch 先ディレクトリ or None)。

        Returns:
            (ステージング済み epoch, 計算後に削除すべき fetch 先ディレクトリ)の
            タプル。pass-through などで削除対象が無ければ後者は None。

        """
        fetched_dir: Path | None = None
        try:
            local = Path(self.fetcher(ref, self.staging_root))
        except Exception as err:  # ruff: ignore[BLE001] — epoch 単位で理由ごと報告
            return StagedEpoch(ref, ref.source_dir, error=f"fetch failed: {err}"), None
        if local != ref.source_dir:
            ref = replace(ref, source_dir=local)
            if self.staging_root in local.parents:
                fetched_dir = local
        staged = stage_epoch(ref, self.staging_root)
        err = validate_epoch(staged, self._required_types, self._needs_dvt)
        if err and not staged.error:
            staged.error = err
        return staged, fetched_dir

    def _prepare_batch(self, refs: list[EpochRef]) -> list[tuple[StagedEpoch, Path | None]]:
        return [self._prepare_epoch(ref) for ref in refs]

    def _cleanup_batch(self, prepared: list[tuple[StagedEpoch, Path | None]]) -> None:
        if self.keep_staging:
            return
        for staged, fetched_dir in prepared:
            cleanup_epoch(staged)
            if fetched_dir is not None:
                shutil.rmtree(fetched_dir, ignore_errors=True)

    def _resolve_batch_size(self, refs: list[EpochRef]) -> int:
        # メモリ情報が取れない環境では(auto でない限り)見積もり読み込み
        # 自体を省略する — advisory のために実データを読むのは無駄なので
        epoch_bytes = None
        if refs and (self.batch_size == "auto" or available_memory_bytes() is not None):
            sample, fetched = self._prepare_epoch(refs[0])
            if sample.error is None:
                epoch_bytes = estimate_epoch_bytes(sample, self.run_config)
            self._cleanup_batch([(sample, fetched)])
        size, msgs = _advise_batch_size(self.batch_size, epoch_bytes, len(refs))
        for m in msgs:
            print(m, file=sys.stderr)
        return size

    # --- 実行 ---

    def run_iter(self) -> Iterator[BatchResult]:
        """バッチごとに BatchResult を yield する。先行取得つきパイプライン。

        Yields:
            1バッチ分の計算結果(scores/failed/dummy_used を持つ BatchResult)。

        Raises:
            StrictBatchError: strict モードで不良 epoch を検出したとき。

        """
        refs = enumerate_epochs(self.histories)
        size = self._resolve_batch_size(refs)
        batches = [refs[i : i + size] for i in range(0, len(refs), size)]

        executor = ThreadPoolExecutor(max_workers=max(1, self.max_prefetch)) if self.max_prefetch > 0 else None
        futures: dict = {}
        try:
            for i in range(len(batches)):
                if executor is not None:
                    # 自分と、その先 max_prefetch 個までを投入しておく
                    for j in range(i, min(i + self.max_prefetch + 1, len(batches))):
                        if j not in futures:
                            futures[j] = executor.submit(self._prepare_batch, batches[j])
                    prepared = futures.pop(i).result()
                else:
                    prepared = self._prepare_batch(batches[i])

                try:
                    result = self._compute_prepared(prepared)
                finally:
                    self._cleanup_batch(prepared)

                if self.strict and result.failed:
                    epoch, reason = next(iter(result.failed.items()))
                    msg = f"{len(result.failed)} epoch(s) failed (strict mode); first: {epoch}: {reason}"
                    raise StrictBatchError(msg)
                yield result
        finally:
            for fut in futures.values():
                fut.cancel()
            if executor is not None:
                # 取得中のものは完了を待ってから片付ける(放置すると消せない)
                for fut in list(futures.values()):
                    if not fut.cancelled():
                        with contextlib.suppress(Exception):
                            self._cleanup_batch(fut.result())
                executor.shutdown(wait=True)
            if self._own_staging and not self.keep_staging:
                shutil.rmtree(self.staging_root, ignore_errors=True)

    def _compute_prepared(self, prepared: list[tuple[StagedEpoch, Path | None]]) -> BatchResult:
        good = [s for s, _ in prepared if s.error is None]
        pre_failed = {s.ref.epoch_id: s.error for s, _ in prepared if s.error is not None}
        result = compute_score_batch(
            good,
            self.run_config,
            self.dvtbudget_coef,
            self.custom_parts_path,
            generation_info_path=self.generation_info_path,
        )
        result.failed.update(pre_failed)
        return result

    def run(self) -> BatchResult:
        """全バッチを実行して結果を1つに結合する。全滅時はエラー。

        Returns:
            全バッチの scores を縦結合し failed / dummy_used を統合した
            BatchResult(History, EpochNo でソート済み)。

        Raises:
            RuntimeError: 全 epoch が失敗して返せるスコア行が1つも無いとき。

        """
        frames: list[pl.DataFrame] = []
        failed: dict = {}
        dummy_used: dict = {}
        for batch in self.run_iter():
            frames.append(batch.scores)
            failed.update(batch.failed)
            dummy_used.update(batch.dummy_used)
        scores = (
            pl.concat(frames, how="vertical").sort(["History", "EpochNo"])
            if frames
            else _result_frame([], self._part_names)
        )
        if scores.height == 0 and failed:
            msg = f"all {len(failed)} epochs failed — nothing to return. first failure: {next(iter(failed.items()))}"
            raise RuntimeError(msg)
        return BatchResult(scores, failed, dummy_used)
