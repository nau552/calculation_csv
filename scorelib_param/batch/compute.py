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
    _load_custom_module_if_needed,
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

    from scorelib_param.models import DvtBudgetCoefFile, GroupDef, RunConfig, ScoreFile, ScorePart

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


@dataclass(frozen=True)
class _BatchInputs:
    """compute_score_batch の引数のうち、各ヘルパーへそのまま流れる共通入力。"""

    run_config: RunConfig
    dvtbudget_coef: DvtBudgetCoefFile | None
    custom_parts_path: str | Path | None
    generation_info_path: str | Path | None


@dataclass(frozen=True)
class _BatchState:
    """1バッチ分の計算で各段階のヘルパーが共有する状態。

    compute_score_batch が構築して各段階に渡す。failed / dummy_used は
    各段階が理由・使用実績を書き足す集約先(dict の実体を全段階で共有する)。
    """

    inputs: _BatchInputs
    epochs: list[StagedEpoch]
    score_file: ScoreFile
    group_defs: dict[str, GroupDef] | None
    ctx: BatchComputeContext
    per_epoch_temps: dict[str, dict[int, float]]
    dummy_values: dict[str, float]
    custom_module: ModuleType | None
    failed: dict[str, str]
    dummy_used: dict[str, list[str]]


def _per_epoch_fallback(
    epochs: list[StagedEpoch],
    inputs: _BatchInputs,
    batch_error: Exception,
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
    run_config = inputs.run_config
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
                dvtbudget_coef=inputs.dvtbudget_coef,
                board_temperatures=temps,
                custom_parts_path=inputs.custom_parts_path,
                generation_info_path=inputs.generation_info_path,
            )
            rows.append({**_epoch_row(se), **values})
        except Exception as err:  # ruff: ignore[BLE001] — epoch単位で理由ごと報告する
            failed[se.ref.epoch_id] = str(err)
    return BatchResult(_result_frame(rows, part_names), failed)


def _load_epoch_temperatures(
    epochs: list[StagedEpoch], failed: dict[str, str]
) -> tuple[list[StagedEpoch], dict[str, dict[int, float]]]:
    """各 epoch の初期温度を読む(dVtBudget 用 — 係数 b が epoch で変わりうる)。

    Returns:
        (温度を読めた epoch のリスト, {epoch_id: 初期温度})のタプル。
        読めなかった epoch は failed に理由を書き足して除外する。

    """
    per_epoch_temps: dict[str, dict[int, float]] = {}
    ok_epochs: list[StagedEpoch] = []
    for se in epochs:
        try:
            per_epoch_temps[se.ref.epoch_id] = load_board_temperatures(
                axis_resolve.data_file(se.data_dir, "initial_temperature.csv")
            )
            ok_epochs.append(se)
        except Exception as err:  # ruff: ignore[BLE001]
            failed[se.ref.epoch_id] = f"initial_temperature.csv unreadable: {err}"
    return ok_epochs, per_epoch_temps


def _custom_part_values(state: _BatchState, part: ScorePart) -> dict[str, float]:
    """各 epoch で custom パーツの値を計算して集める(data_dir 前提の関数のため)。

    Returns:
        {epoch_id: 値}。計算に失敗した epoch は state.failed に理由を
        書き足して除外する。

    """
    module = _require_custom_module(part, state.custom_module)
    values: dict[str, float] = {}
    for se in state.epochs:
        if se.ref.epoch_id in state.failed:
            continue
        try:
            values[se.ref.epoch_id] = custom.compute_custom_part(
                part,
                module,
                custom.CustomContext(
                    data_dir=se.data_dir,
                    generation=state.inputs.run_config.Generation,
                    group_defs=state.group_defs or {},
                    params=part.params or {},
                ),
            )
        except Exception as err:  # ruff: ignore[BLE001]
            state.failed[se.ref.epoch_id] = f"custom part '{part.name}': {err}"
    return values


def _fill_missing_epochs(
    state: _BatchState,
    part: ScorePart,
    source_type: str,
    missing: list[StagedEpoch],
    values_map: dict[str, float],
) -> None:
    """測定 type ファイルの無い epoch を vthSkip のダミー値で埋める。

    ダミー値が設定されていない type なら、該当 epoch を state.failed へ回す。
    """
    if source_type in state.dummy_values:
        # ダミー値はパーツと設定だけで決まり全 epoch で同一 —
        # 1回計算して使い回す(map は欠けた epoch 側から読む)
        dummy_val = compute_dummy_part(
            missing[0].data_dir,
            part,
            state.dummy_values[source_type],
            group_defs=state.group_defs,
            selection_sets=state.score_file.selectionSets,
            weight_sets=state.score_file.weightSets,
        )
        for se in missing:
            values_map[se.ref.epoch_id] = dummy_val
            state.dummy_used.setdefault(se.ref.epoch_id, []).append(part.name)
    else:
        for se in missing:
            state.failed.setdefault(
                se.ref.epoch_id,
                f"{source_type}.csv not found (no vthSkip dummy value configured)",
            )


def _typed_part_values(state: _BatchState, part: ScorePart) -> dict[str, float]:
    """通常(custom 以外)のパーツの値をバッチ一括で計算して集める。

    type ファイルの無い epoch(vthSkip)は _fill_missing_epochs で埋める。

    Returns:
        {epoch_id: 値}(ダミー値で埋めた epoch も含む)。

    """
    st = _source_type(part)
    missing = [se for se in state.epochs if not axis_resolve.data_file(se.data_dir, f"{st}.csv").exists()]
    values_map: dict[str, float] = {}
    if len(missing) < len(state.epochs):
        df = compute_score_part(
            state.epochs[0].data_dir,  # shared_ctx 使用時は参照されない
            part,
            group_defs=state.group_defs,
            generation=state.inputs.run_config.Generation,
            dvtbudget_coef=state.inputs.dvtbudget_coef,
            board_temperatures=state.per_epoch_temps if part.type == "dVtBudget" else None,
            shared_ctx=state.ctx,
            selection_sets=state.score_file.selectionSets,
            weight_sets=state.score_file.weightSets,
            identity_axes=(EPOCH_COL,),
        )
        value_col = st  # collapse 後の列は {EPOCH_COL, 値列} のみ
        values_map = dict(zip(df[EPOCH_COL].to_list(), (float(v) for v in df[value_col].to_list()), strict=False))
    if missing:
        _fill_missing_epochs(state, part, st, missing, values_map)
    return values_map


def _collect_part_values(state: _BatchState) -> dict[str, dict[str, float]]:
    """パーツごとに {epoch_id: 値} を集める(compute_score_batch の try 節本体)。

    ここから送出された例外は compute_score_batch が捕まえ、epoch 逐次の
    フォールバック(_per_epoch_fallback)に切り替える。

    Returns:
        {パーツ名: {epoch_id: 値}}(パーツ定義順)。

    """
    part_values: dict[str, dict[str, float]] = {}
    for part in state.score_file.score_parts:
        if part.type == CUSTOM_TYPE:
            part_values[part.name] = _custom_part_values(state, part)
        else:
            part_values[part.name] = _typed_part_values(state, part)
    return part_values


def _mark_epochs_without_values(part_values: dict[str, dict[str, float]], state: _BatchState) -> None:
    """パーツごとに値の無い epoch を検出して state.failed へ回す。

    filter が空振りした epoch は行ごと消える(null にならない)ため、
    「パーツごとに全 epoch が揃っているか」で必ず捕まえる。
    """
    epoch_ids = [se.ref.epoch_id for se in state.epochs]
    for name, values in part_values.items():
        for epoch_id in epoch_ids:
            if epoch_id not in state.failed and epoch_id not in values:
                state.failed[epoch_id] = (
                    f"part '{name}' produced no value for this epoch "
                    "(a filter probably matched no rows — check the data)"
                )


def _assemble_rows(state: _BatchState, part_values: dict[str, dict[str, float]]) -> list[dict[str, object]]:
    """各 epoch で expression を評価してスコア表の行を組み立てる。

    Returns:
        _result_frame に渡す行 dict のリスト。expression の評価に失敗した
        epoch は state.failed に理由を書き足して除外する。

    """
    part_names = [p.name for p in state.score_file.score_parts]
    rows: list[dict[str, object]] = []
    for se in state.epochs:
        epoch_id = se.ref.epoch_id
        if epoch_id in state.failed:
            continue
        values = {name: part_values[name][epoch_id] for name in part_names}
        score = None
        if state.score_file.expression:
            try:
                score = evaluate_expression(state.score_file.expression, values)
            except Exception as err:  # ruff: ignore[BLE001]
                state.failed[epoch_id] = f"expression evaluation failed: {err}"
                continue
        rows.append({**_epoch_row(se), "Score": score, **values})
    return rows


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

    per_epoch_temps: dict[str, dict[int, float]] = {}
    if any(p.type == "dVtBudget" for p in score_file.score_parts):
        epochs, per_epoch_temps = _load_epoch_temperatures(epochs, failed)
    if not epochs:
        return BatchResult(_result_frame([], part_names), failed)

    custom_module = _load_custom_module_if_needed(score_file.score_parts, custom_parts_path)
    inputs = _BatchInputs(run_config, dvtbudget_coef, custom_parts_path, generation_info_path)
    # vthSkip: type ファイルの無い epoch はダミー値で埋める(cli.compute_dummy_part)
    vth = run_config.optimization.vthSkip
    state = _BatchState(
        inputs=inputs,
        epochs=epochs,
        score_file=score_file,
        group_defs=group_defs,
        ctx=BatchComputeContext(epochs, score_file.score_parts, group_defs),
        per_epoch_temps=per_epoch_temps,
        dummy_values=vth.dummy_values() if vth else {},
        custom_module=custom_module,
        failed=failed,
        dummy_used={},
    )

    # パーツごとに {epoch_id: 値} を集める
    try:
        part_values = _collect_part_values(state)
    except Exception as err:  # ruff: ignore[BLE001] — バッチ全体エラー → epoch 逐次で切り分け
        fb = _per_epoch_fallback(epochs, inputs, err)
        fb.failed.update(failed)
        return fb

    _mark_epochs_without_values(part_values, state)
    rows = _assemble_rows(state, part_values)
    # failed になった epoch の dummy_used は報告しない(値が結果に乗らないため)
    dummy_used = {e: parts for e, parts in state.dummy_used.items() if e not in failed}
    return BatchResult(_result_frame(rows, part_names), failed, dummy_used)
