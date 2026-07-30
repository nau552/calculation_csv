# Copyright (c) 2026
"""バッチ計算層: 複数 epoch をまとめた 1 バッチのスコア計算。

仕組み(docs/batch_design.md 5節):

各 epoch を従来の `axis_resolve.resolve_axes`(無変更)で解決し、識別列
`Epoch`(EPOCH_COL)を付与して lazy に縦結合する。エンジンの中核
「グループキー=その時点で残っている全列」により、Epoch を order に
載せない限り、全集計・相対化ペア照合・複合軸・グループ派生軸が
**自動的に epoch 単位で分かれて**実行される。最終収束だけ
`aggregate.collapse(..., identity_axes=("Epoch",))` で Epoch を残す。

このモジュールはローカルディレクトリ(StagedEpoch)だけを見る純粋な
計算層で、取得・削除の都合(runner.py)から独立している。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import polars as pl

from scorelib_param import axis_resolve, custom
from scorelib_param.cli import (
    CUSTOM_TYPE,
    SharedComputeContext,
    _named_axes,
    _source_type,
    compute_dummy_part,
    compute_score_file,
    compute_score_part,
    resolve_group_defs,
)
from scorelib_param.dvtbudget import load_board_temperatures
from scorelib_param.expression import evaluate_expression

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

    from scorelib_param.models import DvtBudgetCoefFile, GroupDef, RunConfig, ScorePart

    from .staging import StagedEpoch

# バッチ計算の識別軸(docs/batch_design.md 3.3節で確定した予約名)。
# データ由来の軸・グループ定義名と衝突した場合は計算前にエラーにする
EPOCH_COL = "Epoch"


@dataclass
class BatchResult:
    """1バッチ(または全バッチ結合)の結果。

    scores: 1 epoch = 1 行。列は Epoch / History / EpochNo / Score /
            全スコアパーツ(定義順)— docs/batch_design.md 7節の表。
    failed: 除外された epoch → 理由(skip-and-report の報告部分)
    dummy_used: vthSkip のダミー値で計算した epoch → パーツ名リスト
            (「静かに全部ダミーだった」に気づけるようにする報告)
    """

    scores: pl.DataFrame
    failed: dict[str, str] = field(default_factory=dict)
    dummy_used: dict[str, list[str]] = field(default_factory=dict)


class BatchComputeContext(SharedComputeContext):
    """SharedComputeContext のバッチ版。

    type ごとに「全 epoch を解決して Epoch 列付きで縦結合した」共有フレームを
    供給する。prefix_cache 等の仕組みは親のまま(キャッシュはバッチ内全 epoch の
    中間結果を共有する)。
    """

    def __init__(
        self,
        epochs: list[StagedEpoch],
        score_parts: list[ScorePart],
        group_defs: dict[str, GroupDef] | None = None,
    ) -> None:
        """バッチ対象の epoch 群でコンテキストを初期化する(data_dir は使わないため空)。"""
        super().__init__(data_dir="", score_parts=score_parts, group_defs=group_defs)
        self._epochs = epochs

    def resolved(self, source_type: str) -> pl.DataFrame:
        """全 epoch の source_type を解決し、Epoch 列付きで縦結合した共有フレームを返す。

        Returns:
            バッチ内全 epoch の解決結果を Epoch 列付きで縦結合した
            DataFrame(初回に collect してキャッシュし、以降は同じ実体)。

        Raises:
            ValueError: バッチ内のどの epoch にも {source_type}.csv が無いとき。

        """
        if source_type not in self._resolved:
            axes = self._union_axes[source_type]
            # type ファイルの無い epoch は結合対象外(vthSkip 中の epoch は
            # 呼び出し側=compute_score_batch がダミー値で埋める)
            frames = [
                axis_resolve.resolve_axes(se.data_dir, source_type, axes).with_columns(
                    pl.lit(se.ref.epoch_id).alias(EPOCH_COL)
                )
                for se in self._epochs
                if axis_resolve.data_file(se.data_dir, f"{source_type}.csv").exists()
            ]
            if not frames:
                msg = f"no epoch in this batch has {source_type}.csv"
                raise ValueError(msg)
            # epoch間で dtype 推論が割れる可能性に備え relaxed。collect は
            # streaming エンジンでピークメモリを抑える(結果は等価)
            lf = pl.concat(frames, how="vertical_relaxed")
            try:
                self._resolved[source_type] = lf.collect(engine="streaming")
            except TypeError:  # 古い polars(engine 引数なし)
                self._resolved[source_type] = lf.collect()
        return self._resolved[source_type]


def _check_epoch_col_free(run_config: RunConfig) -> None:
    """予約名 EPOCH_COL がスコア設計内の軸・グループ定義と衝突していないか。

    Raises:
        ValueError: グループ定義名またはスコアパーツの軸名が予約名
            EPOCH_COL と衝突しているとき。

    """
    if EPOCH_COL in run_config.group_defs():
        msg = f"group def name '{EPOCH_COL}' collides with the batch identity axis"
        raise ValueError(msg)
    for part in run_config.optimization.score_parts:
        if part.type != CUSTOM_TYPE and EPOCH_COL in _named_axes(part):
            msg = f"score part '{part.name}' uses axis '{EPOCH_COL}', which is reserved as the batch identity axis"
            raise ValueError(msg)


def _load_custom_module(run_config: RunConfig, custom_parts_path: str | Path | None) -> ModuleType | None:
    # Path はモジュールトップでは型注釈用(TYPE_CHECKING)のみ。実行時に使うこの関数内で読み込む
    from pathlib import Path  # ruff: ignore[PLC0415]

    if not any(p.type == CUSTOM_TYPE for p in run_config.optimization.score_parts):
        return None
    path = Path(custom_parts_path) if custom_parts_path else custom.default_custom_parts_path()
    if not path.is_file():
        msg = f"score parts with type='{CUSTOM_TYPE}' need the custom parts file: {path}"
        raise ValueError(msg)
    return custom.load_custom_module(path)


def _require_custom_module(part: ScorePart, custom_module: ModuleType | None) -> ModuleType:
    """読み込み済みの custom_parts モジュールを返す(custom パーツ計算前の防御)。

    custom パーツがあれば _load_custom_module が module を返している
    (ファイルが無ければその場で raise 済み)ため、到達しないパスの防御。

    Returns:
        読み込み済みの custom_parts モジュール(型の narrowing 用にそのまま返す)。

    Raises:
        ValueError: custom パーツがあるのに custom_parts モジュールが
            読み込まれていないとき。

    """
    if custom_module is None:
        msg = (
            f"score part '{part.name}' has type='{CUSTOM_TYPE}' but no custom "
            f"parts file was loaded (expected {custom.default_custom_parts_path()})"
        )
        raise ValueError(msg)
    return custom_module


def _epoch_row(se: StagedEpoch) -> dict[str, object]:
    return {
        EPOCH_COL: se.ref.epoch_id,
        "History": se.ref.label,
        "EpochNo": se.ref.epoch_no,
    }


def _result_frame(rows: list[dict[str, object]], part_names: list[str]) -> pl.DataFrame:
    schema: dict[str, type] = {
        EPOCH_COL: pl.Utf8,
        "History": pl.Utf8,
        "EpochNo": pl.Int64,
        "Score": pl.Float64,
        **dict.fromkeys(part_names, pl.Float64),
    }
    df = pl.DataFrame(rows, schema=schema)
    return df.sort(["History", "EpochNo"])


def _per_epoch_fallback(
    epochs: list[StagedEpoch],
    run_config: RunConfig,
    dvtbudget_coef: DvtBudgetCoefFile | None,
    custom_parts_path: str | Path | None,
    batch_error: Exception,
    generation_info_path: str | Path | None = None,
) -> BatchResult:
    """バッチ一括計算が失敗したときの切り分け。

    epoch ごとの逐次計算(compute_score_file — バッチと数値等価)に落とし、
    原因 epoch を特定して除外し、正常 epoch の値は救う
    (docs/batch_design.md 8節2項)。

    Returns:
        逐次計算で救えた epoch のスコア行と、原因 epoch → 理由の failed を
        収めた BatchResult。

    """
    print(
        f"batch computation failed ({batch_error}); retrying per epoch to locate the offending epoch(s)",
        file=sys.stderr,
    )
    part_names = [p.name for p in run_config.optimization.score_parts]
    rows: list[dict[str, object]] = []
    failed: dict[str, str] = {}
    for se in epochs:
        try:
            temps = None
            if any(p.type == "dVtBudget" for p in run_config.optimization.score_parts):
                temps = load_board_temperatures(axis_resolve.data_file(se.data_dir, "initial_temperature.csv"))
            values = compute_score_file(
                se.data_dir,
                run_config,
                dvtbudget_coef,
                temps,
                custom_parts_path=custom_parts_path,
                generation_info_path=generation_info_path,
            )
            rows.append({**_epoch_row(se), **values})
        except Exception as err:  # ruff: ignore[BLE001] — epoch単位で理由ごと報告する
            failed[se.ref.epoch_id] = str(err)
    return BatchResult(_result_frame(rows, part_names), failed)


def compute_score_batch(
    epochs: list[StagedEpoch],
    run_config: RunConfig,
    dvtbudget_coef: DvtBudgetCoefFile | None = None,
    custom_parts_path: str | Path | None = None,
    generation_info_path: str | Path | None = None,
) -> BatchResult:
    """1バッチ分の epoch 群のスコアをまとめて計算する。

    - staging 済みの epoch(error なし)だけを渡すこと(error 付きは
      呼び出し側=runner が事前に failed へ回す)。
    - バッチ一括計算がエラーになった場合は epoch 逐次計算へ自動フォール
      バックして原因 epoch を特定する(結果は数値等価)。
    - `generation_info_path`: Physical 記法のグループ定義の読み替えに使う
      世代情報 json。省略時は先頭 epoch のデータディレクトリ内を探す。

    Returns:
        このバッチの BatchResult(1 epoch = 1 行の scores と、
        除外 epoch → 理由の failed、ダミー値使用の dummy_used)。

    """
    _check_epoch_col_free(run_config)
    score_file = run_config.to_score_file()
    # Physical 記法のグループ定義は Logical へ読み替えてから使う。世代情報は
    # epoch 間で共通なので先頭 epoch のディレクトリで解決する
    group_defs = resolve_group_defs(
        run_config,
        epochs[0].data_dir if epochs else "",
        generation_info_path,
    )
    part_names = [p.name for p in score_file.score_parts]
    failed: dict[str, str] = {}

    # dVtBudget があれば epoch ごとの初期温度を読む(係数 b が epoch で変わりうる)
    needs_dvt = any(p.type == "dVtBudget" for p in score_file.score_parts)
    per_epoch_temps: dict[str, dict[int, float]] = {}
    if needs_dvt:
        ok_epochs: list[StagedEpoch] = []
        for se in epochs:
            try:
                per_epoch_temps[se.ref.epoch_id] = load_board_temperatures(
                    axis_resolve.data_file(se.data_dir, "initial_temperature.csv")
                )
                ok_epochs.append(se)
            except Exception as err:  # ruff: ignore[BLE001]
                failed[se.ref.epoch_id] = f"initial_temperature.csv unreadable: {err}"
        epochs = ok_epochs
    if not epochs:
        return BatchResult(_result_frame([], part_names), failed)

    custom_module = _load_custom_module(run_config, custom_parts_path)
    ctx = BatchComputeContext(epochs, score_file.score_parts, group_defs)
    epoch_ids = [se.ref.epoch_id for se in epochs]

    # vthSkip: type ファイルの無い epoch はダミー値で埋める(cli.compute_dummy_part)
    vth = run_config.optimization.vthSkip
    dummy_values = vth.dummy_values() if vth else {}
    dummy_used: dict[str, list[str]] = {}

    # パーツごとに {epoch_id: 値} を集める
    part_values: dict[str, dict[str, float]] = {}
    try:
        for part in score_file.score_parts:
            if part.type == CUSTOM_TYPE:
                module = _require_custom_module(part, custom_module)
                # custom パーツは data_dir 前提の関数なので epoch ごとに呼ぶ
                values: dict[str, float] = {}
                for se in epochs:
                    if se.ref.epoch_id in failed:
                        continue
                    try:
                        values[se.ref.epoch_id] = custom.compute_custom_part(
                            part,
                            module,
                            custom.CustomContext(
                                data_dir=se.data_dir,
                                generation=run_config.Generation,
                                group_defs=group_defs or {},
                                params=part.params or {},
                            ),
                        )
                    except Exception as err:  # ruff: ignore[BLE001]
                        failed[se.ref.epoch_id] = f"custom part '{part.name}': {err}"
                part_values[part.name] = values
                continue

            st = _source_type(part)
            missing = [se for se in epochs if not axis_resolve.data_file(se.data_dir, f"{st}.csv").exists()]
            values_map: dict[str, float] = {}
            if len(missing) < len(epochs):
                df = compute_score_part(
                    epochs[0].data_dir,  # shared_ctx 使用時は参照されない
                    part,
                    group_defs=group_defs,
                    generation=run_config.Generation,
                    dvtbudget_coef=dvtbudget_coef,
                    board_temperatures=per_epoch_temps if part.type == "dVtBudget" else None,
                    shared_ctx=ctx,
                    selection_sets=score_file.selectionSets,
                    weight_sets=score_file.weightSets,
                    identity_axes=(EPOCH_COL,),
                )
                value_col = st  # collapse 後の列は {EPOCH_COL, 値列} のみ
                values_map = dict(
                    zip(df[EPOCH_COL].to_list(), (float(v) for v in df[value_col].to_list()), strict=False)
                )
            if missing:
                if st in dummy_values:
                    # ダミー値はパーツと設定だけで決まり全 epoch で同一 —
                    # 1回計算して使い回す(map は欠けた epoch 側から読む)
                    dummy_val = compute_dummy_part(
                        missing[0].data_dir,
                        part,
                        dummy_values[st],
                        group_defs=group_defs,
                        selection_sets=score_file.selectionSets,
                        weight_sets=score_file.weightSets,
                    )
                    for se in missing:
                        values_map[se.ref.epoch_id] = dummy_val
                        dummy_used.setdefault(se.ref.epoch_id, []).append(part.name)
                else:
                    for se in missing:
                        failed.setdefault(
                            se.ref.epoch_id,
                            f"{st}.csv not found (no vthSkip dummy value configured)",
                        )
            part_values[part.name] = values_map
    except Exception as err:  # ruff: ignore[BLE001] — バッチ全体エラー → epoch 逐次で切り分け
        fb = _per_epoch_fallback(
            epochs,
            run_config,
            dvtbudget_coef,
            custom_parts_path,
            err,
            generation_info_path=generation_info_path,
        )
        fb.failed.update(failed)
        return fb

    # epoch 欠落の検出: filter が空振りした epoch は行ごと消える(nullに
    # ならない)ため、「パーツごとに全 epoch が揃っているか」で必ず捕まえる
    for name, values in part_values.items():
        for epoch_id in epoch_ids:
            if epoch_id not in failed and epoch_id not in values:
                failed[epoch_id] = (
                    f"part '{name}' produced no value for this epoch "
                    "(a filter probably matched no rows — check the data)"
                )

    # expression を epoch ごとに評価して行を組み立てる
    rows: list[dict[str, object]] = []
    for se in epochs:
        epoch_id = se.ref.epoch_id
        if epoch_id in failed:
            continue
        values = {name: part_values[name][epoch_id] for name in part_names}
        score = None
        if score_file.expression:
            try:
                score = evaluate_expression(score_file.expression, values)
            except Exception as err:  # ruff: ignore[BLE001]
                failed[epoch_id] = f"expression evaluation failed: {err}"
                continue
        rows.append({**_epoch_row(se), "Score": score, **values})

    # failed になった epoch の dummy_used は報告しない(値が結果に乗らないため)
    dummy_used = {e: parts for e, parts in dummy_used.items() if e not in failed}
    return BatchResult(_result_frame(rows, part_names), failed, dummy_used)
