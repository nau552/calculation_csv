"""バッチ実行のオーケストレータ。

「取得(fetch) → ステージング(展開・検証) → バッチ計算 → ステージング削除」を
バッチ単位のパイプラインで回す（docs/batch_design.md 6節）:

- 計算中に裏で次の最大 `max_prefetch` バッチを先行取得する
  （ディスク使用量の上限 = (1 + max_prefetch) × 1バッチ分）
- 削除するのはステージング領域（fetch 先・展開ビュー）のみ。
  ローカル既存データ（pass-through）は削除しない
- fetch 手段は差し替え可能な callable（Fetcher）。デフォルトはローカル/
  共有マウント済みパスの pass-through。scp 等はこのインターフェースの
  実装を1つ足すだけでよい
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Sequence, Tuple, Union

import polars as pl

from ..models import DvtBudgetCoefFile, RunConfig
from .compute import BatchComputeContext, BatchResult, _result_frame, compute_score_batch
from .history import EpochRef, enumerate_epochs
from .staging import StagedEpoch, cleanup_epoch, stage_epoch, validate_epoch

DEFAULT_BATCH_SIZE = 50
# 1 epoch の解決済みフレーム実測サイズに対する、計算中の中間結果
# （相対化・キャッシュ点・group_by）を見込んだ安全係数
MEMORY_FACTOR = 3.0
# 推奨バッチサイズは「利用可能メモリのこの割合に収まる」ように選ぶ
MEMORY_BUDGET_RATIO = 1 / 3

# fetch の契約: (epoch参照, ステージング領域) → ローカルの epoch ディレクトリ。
# リモート実装は staging_root 配下に取得して返すこと（計算後に削除される）。
# ブロッキングでよい（Runner 側が並行化する）
Fetcher = Callable[[EpochRef, Path], Path]


def passthrough_fetcher(ref: EpochRef, staging_root: Path) -> Path:
    """ローカル/共有マウント済みデータをそのまま使う（コピーも削除もしない）。"""
    return ref.source_dir


class StrictBatchError(RuntimeError):
    """strict モードで不良 epoch を検出したとき送出される。"""


def available_memory_bytes() -> Optional[int]:
    """利用可能メモリ。Linux は /proc/meminfo（追加依存なし・古い Ubuntu でも
    可）、それ以外は psutil があれば使用、どちらも無ければ None（advisory を
    スキップ）。"""
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    try:
        import psutil

        return int(psutil.virtual_memory().available)
    except Exception:  # noqa: BLE001 — advisory なので静かに諦める
        return None


def estimate_epoch_bytes(sample: StagedEpoch, run_config: RunConfig) -> Optional[int]:
    """最初の epoch を実際に解決してメモリ足跡を実測する（advisory 用）。"""
    try:
        score_file = run_config.to_score_file()
        parts = [p for p in score_file.score_parts if p.type != "custom"]
        if not parts:
            return None
        ctx = BatchComputeContext([sample], parts, run_config.group_defs())
        return sum(ctx.resolved(st).estimated_size() for st in ctx._union_axes)
    except Exception:  # noqa: BLE001 — 見積もり失敗は advisory を諦めるだけ
        return None


def _advise_batch_size(
    requested: Union[int, str], epoch_bytes: Optional[int], n_epochs: int
) -> Tuple[int, List[str]]:
    """batch_size の解決と助言メッセージ（docs/batch_design.md 6.3節）。
    実行をブロックしない。"""
    msgs: List[str] = []
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
            msgs.append(
                f"batch-size auto: memory info unavailable, using default {size}"
            )
        return size, msgs

    size = int(requested)
    if size < 1:
        raise ValueError(f"batch_size must be >= 1, got {size}")
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
        result = runner.run()          # まとめて（scores: DataFrame, failed: dict）
        for batch in runner.run_iter():  # バッチごとに逐次受け取る
            ...
    """

    def __init__(
        self,
        histories: Union[Sequence[Union[str, Path]], Mapping[str, Union[str, Path]]],
        run_config: RunConfig,
        *,
        dvtbudget_coef: Optional[DvtBudgetCoefFile] = None,
        batch_size: Union[int, str] = DEFAULT_BATCH_SIZE,
        max_prefetch: int = 2,
        staging_dir: Optional[Union[str, Path]] = None,
        strict: bool = False,
        keep_staging: bool = False,
        fetcher: Optional[Fetcher] = None,
        custom_parts_path=None,
        generation_info_path=None,
    ):
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
        self.staging_root = Path(
            staging_dir if staging_dir else tempfile.mkdtemp(prefix="scorelib_batch_")
        )
        self.staging_root.mkdir(parents=True, exist_ok=True)

        from ..cli import _source_type

        parts = run_config.optimization.score_parts
        # vthSkip でダミー値が設定された type はファイルが無くても計算できる
        # （compute.py が epoch ごとに埋める）ため、事前検証の必須対象から外す
        vth = run_config.optimization.vthSkip
        dummy_types = set(vth.dummy_values()) if vth else set()
        self._required_types = sorted(
            {_source_type(p) for p in parts if p.type != "custom"} - dummy_types
        )
        self._needs_dvt = any(p.type == "dVtBudget" for p in parts)
        self._part_names = [p.name for p in parts]

    # --- 1 epoch の準備（fetch → ステージング → 検証）。worker スレッドで走る ---

    def _prepare_epoch(self, ref: EpochRef) -> Tuple[StagedEpoch, Optional[Path]]:
        """戻り値: (staged, 削除すべき fetch 先ディレクトリ or None)。"""
        fetched_dir: Optional[Path] = None
        try:
            local = Path(self.fetcher(ref, self.staging_root))
        except Exception as err:  # noqa: BLE001 — epoch 単位で理由ごと報告
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

    def _prepare_batch(self, refs: List[EpochRef]) -> List[Tuple[StagedEpoch, Optional[Path]]]:
        return [self._prepare_epoch(ref) for ref in refs]

    def _cleanup_batch(self, prepared: List[Tuple[StagedEpoch, Optional[Path]]]) -> None:
        if self.keep_staging:
            return
        for staged, fetched_dir in prepared:
            cleanup_epoch(staged)
            if fetched_dir is not None:
                shutil.rmtree(fetched_dir, ignore_errors=True)

    def _resolve_batch_size(self, refs: List[EpochRef]) -> int:
        # メモリ情報が取れない環境では（auto でない限り）見積もり読み込み
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

    def run_iter(self):
        """バッチごとに BatchResult を yield する。先行取得つきパイプライン。"""
        refs = enumerate_epochs(self.histories)
        size = self._resolve_batch_size(refs)
        batches = [refs[i : i + size] for i in range(0, len(refs), size)]

        executor = ThreadPoolExecutor(max_workers=max(1, self.max_prefetch)) \
            if self.max_prefetch > 0 else None
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
                    raise StrictBatchError(
                        f"{len(result.failed)} epoch(s) failed (strict mode); "
                        f"first: {epoch}: {reason}"
                    )
                yield result
        finally:
            for fut in futures.values():
                fut.cancel()
            if executor is not None:
                # 取得中のものは完了を待ってから片付ける（放置すると消せない）
                for fut in list(futures.values()):
                    if not fut.cancelled():
                        try:
                            self._cleanup_batch(fut.result())
                        except Exception:  # noqa: BLE001
                            pass
                executor.shutdown(wait=True)
            if self._own_staging and not self.keep_staging:
                shutil.rmtree(self.staging_root, ignore_errors=True)

    def _compute_prepared(
        self, prepared: List[Tuple[StagedEpoch, Optional[Path]]]
    ) -> BatchResult:
        good = [s for s, _ in prepared if s.error is None]
        pre_failed = {s.ref.epoch_id: s.error for s, _ in prepared if s.error is not None}
        result = compute_score_batch(
            good, self.run_config, self.dvtbudget_coef, self.custom_parts_path,
            generation_info_path=self.generation_info_path,
        )
        result.failed.update(pre_failed)
        return result

    def run(self) -> BatchResult:
        """全バッチを実行して結果を1つに結合する。全滅時はエラー。"""
        frames: List[pl.DataFrame] = []
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
            raise RuntimeError(
                f"all {len(failed)} epochs failed — nothing to return. "
                f"first failure: {next(iter(failed.items()))}"
            )
        return BatchResult(scores, failed, dummy_used)
