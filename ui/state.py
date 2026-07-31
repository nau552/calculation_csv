# Copyright (c) 2026
"""スコア設計UIの編集ロジック(純関数層)。

ここにあるものはすべて streamlit 非依存で pytest 可能
(docs/score_gui_ui_design.md 2節)。編集中の ScoreFile は素の dict
(scorelib_param.models.ScoreFile の形)として保持し、pydantic は検証にのみ使う。
これにより UI はエンジンが読み込み時に出すのと全く同じメッセージを表示する。
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Set as AbstractSet

from scorelib_param import custom as scorelib_custom
from scorelib_param import introspect, io_jsonc, jsonc
from scorelib_param.expression import evaluate_expression
from scorelib_param.models import COMBINED_SEP, TRANSFORM_OPS, ScoreFile

DRAFT_PATH = Path.home() / ".scorelib_draft.jsonc"  # 旧: 単一ユーザ時代の下書き(定数は互換のため残す)
DRAFTS_DIR = Path.home() / ".scorelib_drafts"  # ユーザ名ごとの下書き置き場(共用サーバ対応)


def draft_path_for(user: str) -> Path:
    """ユーザ名ごとの下書きパス(DRAFTS_DIR/<名前>.jsonc)。

    UI は共用サーバで複数人が使うため、下書きを1ファイルで取り合うと相互上書き・
    他人の下書きの復元提案が起きる。認証が無い間は自己申告の名前で分離し、
    リバースプロキシ認証の導入後はヘッダのユーザ名がそのままここに入る
    (app._sidebar_user)。名前はファイル名に安全な文字へ正規化する。

    Returns:
        DRAFTS_DIR/<正規化した名前>.jsonc のパス。

    """
    safe = re.sub(r"[^\w\-]", "_", str(user).strip())[:64] or "_"
    return DRAFTS_DIR / f"{safe}.jsonc"


# InBatchEpoch は集計対象にしない(ユーザ判断。設計書5節)。order に
# 入れないことで、エンジンは周囲の軸と一緒に暗黙に集約する。
# DataName は「Measure の表示名」の位置づけ(docs/spec_change_dataname_measure.md
# 6.4節): 指定は Measure 軸で行うため雛形には入れない(軸としての明示追加は可能)
_EXCLUDED_AXES = {"InBatchEpoch", "DataName"}
_LAST_AXES = ["Board", "Chip", "Block"]


# ------------------------------------------------------------ スコアファイル


def empty_score_file() -> dict[str, Any]:
    """空の ScoreFile dict(編集の初期状態)を返す。

    Returns:
        score_parts / expression / constraintThreshold / selectionSets /
        groupDefs / weightSets をすべて空で持つ dict。

    """
    return {
        "score_parts": [],
        "expression": "",
        "constraintThreshold": {},
        "selectionSets": {},
        "groupDefs": {},
        "weightSets": {},
    }


def ensure_uids(score_file: dict[str, Any]) -> None:
    """各パーツにウィジェットキー用の安定した内部ID(_uid)を付ける。

    添字ベースのキーだと削除後に別パーツへ状態が漏れる。pydantic は
    未知フィールドを無視するので検証・エクスポートには影響しない。

    重複IDは振り直す: IDを共有した2パーツは全ウィジェット(名前欄・相対化
    チェック等)を共有してしまい、片方の編集がもう片方を静かに書き換える。
    そのバグがあった期間に保存された下書きも、これで開くだけで治る。
    """
    seen: set = set()
    for p in score_file["score_parts"]:
        uid = p.get("_uid")
        if not uid or uid in seen:
            uid = uuid.uuid4().hex[:8]
            p["_uid"] = uid
        seen.add(uid)


def part_names(score_file: dict[str, Any]) -> list[str]:
    """全パーツの name のリスト(未設定は空文字)。

    Returns:
        score_parts の並び順どおりのパーツ名リスト。

    """
    return [p.get("name", "") for p in score_file["score_parts"]]


def unique_part_name(score_file: dict[str, Any], base: str = "part") -> str:
    """既存パーツ名と重複しない「{base}_{i}」形式の名前を返す。

    Returns:
        i を 1 から増やして最初に空いた「{base}_{i}」形式の名前。

    """
    names = set(part_names(score_file))
    i = 1
    while f"{base}_{i}" in names:
        i += 1
    return f"{base}_{i}"


# ------------------------------------------------------------------ 雛形生成


def default_axis_order(catalog: dict[str, list | None], exclude: AbstractSet[str] = frozenset()) -> list[str]:
    """雛形の軸順を決める。

    Label系 → Override系 → その他カテゴリ(State, Page, ...)
    → 数値(WL, STR, ...)→ Board, Chip, Block。

    Returns:
        除外軸を除いた全軸を上記の優先順に並べたリスト。

    """
    skip = _EXCLUDED_AXES | set(exclude)
    axes = [a for a in catalog if a not in skip]

    def bucket(a: str) -> int:
        if a in _LAST_AXES:
            return 4
        if a == "Measure":
            return -1  # 識別子軸: 「どの測定か」の選択が先頭に来るのが自然
        if a.endswith("_Label"):
            return 0
        if a.endswith("_Override"):
            return 1
        cands = catalog.get(a)
        if cands and isinstance(cands[0], str):
            return 2
        return 3

    ordered = sorted(axes, key=bucket)  # 安定ソート: 同バケツ内は csv 順のまま
    return [a for a in ordered if a not in _LAST_AXES] + [a for a in _LAST_AXES if a in axes]


def default_aggregation(axis: str, candidates: list | None) -> dict[str, Any]:
    """軸に応じたデフォルトの集計specを返す。

    カテゴリ/bool軸は先頭候補の filter から始める(意味があり、かつ必ず
    計算が通る)。数値・自由入力軸は mean から。Measure は数値だが識別子
    (どの測定か)なので、量として平均せず filter から始める — 候補が無い
    (設定のみ編集)場合は value 未入力の filter とし、ユーザが番号を入れる
    まで検証エラーで促す(mean にすると測定が静かに混ざるため)。

    Returns:
        {"op": ...} 形式の集計spec(filter の場合は value 込み)。

    """
    if axis == "Measure":
        return {"op": "filter", "value": candidates[0] if candidates else None}
    if candidates and isinstance(candidates[0], (str, bool)):
        return {"op": "filter", "value": candidates[0]}
    return {"op": "mean"}


# dVthSGWLD の標準計算で総和から省く SGWLD 要素(2026-07-29 ユーザー確認:
# 「これまで変更したことがない」慣習。map にある場合のみ除外し、雛形の初期値
# なのでユーザは普通の編集で変更できる)
_DVTH_EXCLUDED_SGWLD = ("SGSB", "SGS", "SGD", "SGDT")


def _typed_skeleton(name: str, type_: str, catalog: dict[str, list | None]) -> dict[str, Any] | None:
    """KLD / dVthSGWLD の「一般的な計算」が入った type 別雛形を返す。

    (2026-07-29 ユーザー確認 — docs/score_gui_design.md)。強制ではなく
    初期値: 生成後は普通のパーツとして編集できる。前提の SGWLD 軸が
    catalog に無ければ None(汎用雛形にフォールバック)。

    - KLD: Board/Chip mean → log(max(|x|, 1e-6)) → SGWLD sum(重み 0.1)
    - dVthSGWLD: Board/Chip/Block mean → abs → SGWLD sum(SG系4要素を除く選択)
    重みは集計時重みで持つ(変換ステップと違い vthSkip のダミー計算でも掛かる)。

    Returns:
        標準計算の order / aggregations が入ったパーツ dict。
        対象外の type や SGWLD 軸の無い catalog では None。

    """
    sgwld = catalog.get("SGWLD")
    if type_ not in {"KLD", "dVthSGWLD"} or not sgwld:
        return None
    if type_ == "KLD":
        mean_axes = [a for a in ("Board", "Chip") if a in catalog]
        order = [*mean_axes, "__log__", "SGWLD"]
        aggregations: dict[str, Any] = {a: {"op": "mean"} for a in mean_axes}
        aggregations["__log__"] = {"op": "log", "floor": 1e-6}
        aggregations["SGWLD"] = {"op": "sum", "weight": 0.1}
    else:
        mean_axes = [a for a in ("Board", "Chip", "Block") if a in catalog]
        order = [*mean_axes, "__abs__", "SGWLD"]
        aggregations = {a: {"op": "mean"} for a in mean_axes}
        aggregations["__abs__"] = {"op": "abs"}
        kept = [v for v in sgwld if v not in _DVTH_EXCLUDED_SGWLD]
        spec: dict[str, Any] = {"op": "sum"}
        if kept and len(kept) < len(sgwld):
            spec["value"] = kept
        aggregations["SGWLD"] = spec
    return {"name": name, "type": type_, "order": order, "aggregations": aggregations}


def part_skeleton(name: str, type_: str, catalog: dict[str, list | None]) -> dict[str, Any]:
    """**そのまま計算が通る**新規パーツの雛形を返す。

    全軸をデフォルトopつきで `order` に並べる。相対化のプリセットは
    無し — 旧仕様の「Read_Override があれば自動ON」は廃止
    (docs/spec_change_dataname_measure.md 9節。dataName 命名ルール確定後、
    ルールに合うペアが見つかったときの自動セットを第2弾で追加する)。

    KLD / dVthSGWLD は標準計算入りの type 別雛形(_typed_skeleton)を返す。

    Measure 軸のある type では parameterLabel 由来の Label/Override 軸を雛形に
    入れない(明示追加は可能): それらは Measure 番号が一意に決める測定メタ
    データで、Measure filter との二重指定になる上、Measure 分割の相対化では
    ペア結合キーに残って分子/分母の行を対にできなくする(Override は基準側と
    評価側で必ず値が異なるため)。

    Returns:
        name / type / order / aggregations の入った新規パーツ dict。

    """
    typed = _typed_skeleton(name, type_, catalog)
    if typed is not None:
        return typed
    exclude: set = set()
    if "Measure" in catalog:
        exclude = {a for a in catalog if a.endswith(("_Label", "_Override"))}
    order = default_axis_order(catalog, exclude)
    aggregations = {a: default_aggregation(a, catalog.get(a)) for a in order}
    return {"name": name, "type": type_, "order": order, "aggregations": aggregations}


def custom_part_skeleton(name: str, functions: list[str]) -> dict[str, Any]:
    """type="custom" の新規パーツの雛形を返す。

    パイプライン系フィールドは持たず、呼ぶ関数(先頭の候補)と空の params だけ。

    Returns:
        name / type / function / params だけを持つパーツ dict。

    """
    return {
        "name": name,
        "type": "custom",
        "function": functions[0] if functions else name,
        "params": {},
    }


def switch_part_type(part: dict[str, Any], new_type: str) -> str | None:
    """Type 変更に合わせてフィールドを整える。

    ユーザ向けの通知文字列(不要なら None)を返す。custom とパイプライン系
    フィールドの混在はエンジンが拒否するので、切替時点で UI 側が外す。

    Returns:
        外した設定をユーザに知らせる通知文字列(通知が不要なら None)。

    """
    part["type"] = new_type
    if new_type == "custom":
        dropped = [part.pop(k, None) for k in ("relative", "order", "aggregations")]
        part.setdefault("function", part.get("name"))
        part.setdefault("params", {})
        return "集計設定を外しました(custom パーツは関数が値を返します)" if any(dropped) else None
    part.pop("function", None)
    part.pop("params", None)
    part.setdefault("order", [])
    part.setdefault("aggregations", {})
    removed = drop_stale_virtual_steps(part)
    return f"{removed} を order から外しました(type が dVtBudget ではないため)" if removed else None


def _axes_in_order(part: dict[str, Any]) -> set:
    used: set = set()
    for e in part.get("order", []):
        if not e.startswith("__"):
            used.update(e.split(COMBINED_SEP))
    return used


def _remove_axis_from_order(part: dict[str, Any], axis: str | None) -> None:
    if axis and axis in part.get("order", []):
        part["order"].remove(axis)
        part["aggregations"].pop(axis, None)


def _restore_axis_to_order(part: dict[str, Any], axis: str | None, catalog: dict[str, list | None]) -> bool:
    """軸を(デフォルトopつきで)`order` に戻す。

    既にカバーされていれば何もしない。相対化OFF時に必要: エンジンは
    `order` に無い軸を黙って集約するため、放置すると分子と分母の行が
    混ざってしまう。

    Returns:
        軸を order に追加した場合 True。カタログに無い・カバー済み等で
        何もしなかった場合 False。

    """
    if not axis or axis not in catalog or axis in _axes_in_order(part):
        return False
    part["order"].append(axis)
    part["aggregations"][axis] = default_aggregation(axis, catalog.get(axis))
    return True


def default_split_axis(catalog: dict[str, list | None]) -> str | None:
    """相対化の既定 split 軸を返す。

    優先順: Measure(識別子軸 — 新仕様の基本形) >
    Param(ROM=基準パラ / Opt=提案パラ。PROGLOOP 系の Measure 無し type は
    ここで相対化するのが基本形 — 2026-07-29 ユーザー確認) >
    Override 系(旧仕様データ) > 先頭の軸。

    Returns:
        優先順で最初に見つかった軸名。カタログが空なら None。

    """
    if "Measure" in catalog:
        return "Measure"
    if "Param" in catalog:
        return "Param"
    overrides = [a for a in catalog if a.endswith("_Override")]
    if "Read_Override" in overrides:
        return "Read_Override"
    if overrides:
        return overrides[0]
    return next(iter(catalog), None)


# 相対化 split の両側 = 分子と分母の2値
_SPLIT_SIDES = 2


def _positional_sides(candidates: list | None) -> tuple:
    """Split 軸の値候補から分子/分母の初期値を位置で選ぶ。

    先頭 = 分母(基準側。Measure は基準測定が先に走る想定、Override 候補
    [False, True] では旧既定の分母=False と一致)、2番目 = 分子。
    候補が無い・足りない場合は (None, None) — ユーザが入力するまで
    検証エラーが表示される(エンジンが None を拒否)。

    Returns:
        (分子の初期値, 分母の初期値)。候補が2つ未満なら (None, None)。

    """
    if candidates and len(candidates) >= _SPLIT_SIDES:
        return candidates[1], candidates[0]
    return None, None


def enable_relative(part: dict[str, Any], catalog: dict[str, list | None]) -> None:
    """相対化をONにする: 既定の split 軸で初期化し、その軸を `order` から外す。"""
    split = default_split_axis(catalog)
    numerator, denominator = _positional_sides(catalog.get(split) if split is not None else None)
    part["relative"] = {
        "split_axis": split,
        "numerator_when": numerator,
        "denominator_when": denominator,
        "mode": "ratio",
        "denominator_offset": 1,
    }
    _remove_axis_from_order(part, split)


def disable_relative(part: dict[str, Any], catalog: dict[str, list | None]) -> str | None:
    """相対化をOFFにする。

    split 軸を `order` に復帰させた場合はその軸名を返す(UIがユーザに
    知らせるため)。明示配置された __relative__ ステップも除去する —
    相対化設定なしで残ると検証エラーになるため。

    Returns:
        order へ復帰させた split 軸名。復帰が起きなかった(相対化OFF済み・
        軸がカタログに無い等の)場合は None。

    """
    rel = part.pop("relative", None)
    if not rel:
        return None
    if "__relative__" in part.get("order", []):
        part["order"].remove("__relative__")
        part["aggregations"].pop("__relative__", None)
    axis = rel.get("split_axis")
    return axis if _restore_axis_to_order(part, axis, catalog) else None


def drop_stale_virtual_steps(part: dict[str, Any]) -> str | None:
    """不要になった明示配置の __dvtbudget__ ステップを除去する。

    Type を dVtBudget から他へ変えた後、明示配置の __dvtbudget__ が残ると
    検証エラーになるので除去する。除去したステップ名(UI通知用)か None を返す。

    Returns:
        除去した場合はステップ名 "__dvtbudget__"、除去が不要なら None。

    """
    if part.get("type") != "dVtBudget" and "__dvtbudget__" in part.get("order", []):
        part["order"].remove("__dvtbudget__")
        part["aggregations"].pop("__dvtbudget__", None)
        return "__dvtbudget__"
    return None


def change_split_axis(part: dict[str, Any], new_axis: str, catalog: dict[str, list | None]) -> None:
    """Split 軸の変更: 旧軸を `order` へ戻し、新軸を `order` から外す。

    分子/分母は旧軸の値のままでは無意味なので、新軸の候補から位置で初期化し
    直す(labels 注記も旧軸のものなので捨てる)。
    """
    rel = part.get("relative")
    if rel is None or rel.get("split_axis") == new_axis:
        return
    old = rel.get("split_axis")
    rel["split_axis"] = new_axis
    rel["numerator_when"], rel["denominator_when"] = _positional_sides(catalog.get(new_axis))
    rel.pop("labels", None)
    _restore_axis_to_order(part, old, catalog)
    _remove_axis_from_order(part, new_axis)


def annotate_measure_labels(spec: dict[str, Any], measure_labels: dict[int, str]) -> None:
    """Measure 番号で指定した相対化/filter に dataName 注記(labels)を付ける。

    (docs/spec_change_dataname_measure.md 6.1節: 保存は番号、名前は注記)。
    相対化 dict なら分子/分母の値、集計 spec なら value(リスト可)が対象。
    名前の分かる番号だけを残し、1つも無ければ labels 自体を外す。
    """
    raw = [spec.get("numerator_when"), spec.get("denominator_when")]
    v = spec.get("value")
    raw += v if isinstance(v, list) else [v]
    labels = {
        str(x): measure_labels[x]
        for x in raw
        if isinstance(x, int) and not isinstance(x, bool) and x in measure_labels
    }
    if labels:
        spec["labels"] = labels
    else:
        spec.pop("labels", None)


def duplicate_part(score_file: dict[str, Any], index: int) -> int:
    """パーツを複製して末尾に追加し、複製の添字を返す。

    Returns:
        末尾に追加された複製パーツの添字。

    """
    src = score_file["score_parts"][index]
    copy = json.loads(json.dumps(src))
    # コピーには必ず新しいウィジェットキー用IDを割り当てる: 共有すると
    # 2パーツが全ウィジェットの記憶状態を共有してしまう
    copy.pop("_uid", None)
    copy["name"] = unique_part_name(score_file, base=src.get("name", "part"))
    score_file["score_parts"].append(copy)
    return len(score_file["score_parts"]) - 1


def move_entry(lst: list, index: int, delta: int) -> int:
    """lst[index] を隣と入れ替える。新しい添字を返す(端では何もしない)。

    Returns:
        入れ替え後の要素の添字(端で動かせなかった場合は元の index)。

    """
    j = index + delta
    if 0 <= j < len(lst):
        lst[index], lst[j] = lst[j], lst[index]
        return j
    return index


# ------------------------------------------------------------- グループ定義


def import_config_group_defs(
    score_file: dict[str, Any],
    wlgroup: dict[str, Any] | None,
    *,
    defin_logical: bool = True,
    wlgroup_weight: object | None = None,
) -> bool:
    """設定jsoncの WLgroup 一式を編集可能な定義として取り込む。

    WLgroup(+ WLgroupDefinLogical / WLgroupWeight)が対象。score file は
    自己完結(エンジンが使うのは score file 側の groupDefs / weightSets)で、
    config の値はあくまで初期テンプレート。追加したら True を返す。

    Returns:
        WLgroup を groupDefs に追加した場合 True。WLgroup が空、または
        既に定義済みの場合 False。

    """
    weights = score_file.setdefault("weightSets", {})
    if wlgroup_weight is not None and "WLgroupWeight" not in weights:
        weights["WLgroupWeight"] = dict(wlgroup_weight) if isinstance(wlgroup_weight, dict) else wlgroup_weight
    defs = score_file.setdefault("groupDefs", {})
    if not wlgroup or "WLgroup" in defs:
        return False
    defs["WLgroup"] = {
        "axis": "WL",
        "groups": {k: list(v) for k, v in wlgroup.items()},
        "definedInLogical": bool(defin_logical),
    }
    return True


def _part_axis_names(part: dict[str, Any]) -> set:
    """パーツが言及する軸的な名前すべてを返す。

    order エントリ=複合軸の構成軸込み、相対化の split 軸、
    分母事前集計の軸が対象。

    scorelib_param/cli.py の _named_axes は計算時に使う pydantic モデル版の対。
    こちらは編集途中の不完全な dict に耐える — 意図的な並行実装であり、
    統合を試みないこと。

    Returns:
        パーツが言及する軸名の集合(__xxx__ の仮想ステップは含まない)。

    """
    axes = set(_axes_in_order(part))
    for s in _part_specs(part):
        if isinstance(s, dict) and s.get("by"):
            axes.add(s["by"])  # 変換ステップの重みが参照する軸(cli._named_axes と対)
    rel = part.get("relative") or {}
    if rel.get("split_axis"):
        axes.add(rel["split_axis"])
    axes.update(s.get("axis") for s in rel.get("denominator_pre_aggregation", []) if s.get("axis"))
    return axes


def parts_referencing_group_def(score_file: dict[str, Any], name: str) -> list[str]:
    """指定のグループ定義を参照しているパーツ名の一覧。

    Returns:
        該当パーツの name のリスト(name 未設定のパーツは "?")。

    """
    return [p.get("name", "?") for p in score_file["score_parts"] if name in _part_axis_names(p)]


def add_group_def(score_file: dict[str, Any], name: str, axis: str, axis_names: set) -> None:
    """グループ定義を追加する(名前の検証つき。問題があれば ValueError)。

    Raises:
        ValueError: 名前が空・禁止文字(複合軸区切りの '&' や先頭 '__')
            入り・既存の定義名/軸名と衝突・WL 以外の軸に予約名 'WLgroup' を
            使ったとき。

    """
    name = (name or "").strip()
    if not name:
        msg = "グループ定義名を入力してください"
        raise ValueError(msg)
    if COMBINED_SEP in name or name.startswith("__"):
        msg = f"グループ定義名に '{COMBINED_SEP}' や先頭の '__' は使えません"
        raise ValueError(msg)
    defs = score_file.setdefault("groupDefs", {})
    if name in defs:
        msg = f"グループ定義 '{name}' は既に存在します"
        raise ValueError(msg)
    if name in axis_names:
        msg = f"'{name}' は軸名と衝突しています(別の名前にしてください)"
        raise ValueError(msg)
    if name == "WLgroup" and axis != "WL":
        # "WLgroup" は設定 jsonc の旧形式キー(optimization.WLgroup = WL への
        # 定義)として書き出されるため、WL 以外の軸には使えない
        msg = "'WLgroup' は WL 軸の定義の予約名です(別の名前にしてください)"
        raise ValueError(msg)
    defs[name] = {"axis": axis, "groups": {}}


def delete_group_def(score_file: dict[str, Any], name: str) -> None:
    """グループ定義を削除する(パーツから参照されていれば ValueError)。

    Raises:
        ValueError: 定義がいずれかのパーツから参照されているとき。

    """
    users = parts_referencing_group_def(score_file, name)
    if users:
        msg = f"グループ定義 '{name}' はパーツ {users} から参照されているため削除できません"
        raise ValueError(msg)
    score_file.get("groupDefs", {}).pop(name, None)


# --------------------------------------------------------------- 選択セット


def _part_specs(part: dict[str, Any]) -> list[Any]:
    """パーツが持つ全集計spec(分母事前集計のステップ込み。ref の走査用)。

    Returns:
        aggregations の全 spec に分母事前集計のステップを足したリスト。

    """
    specs = list(part.get("aggregations", {}).values())
    specs += (part.get("relative") or {}).get("denominator_pre_aggregation", [])
    return specs


def referencing_parts(score_file: dict[str, Any], set_name: str) -> list[str]:
    """指定の選択セットを(分母事前集計も含めて)参照しているパーツ名の一覧。

    Returns:
        該当パーツの name のリスト(name 未設定のパーツは "?")。

    """
    return [
        part.get("name", "?")
        for part in score_file["score_parts"]
        if any(isinstance(s, dict) and s.get("ref") == set_name for s in _part_specs(part))
    ]


def delete_selection_set(score_file: dict[str, Any], name: str) -> None:
    """選択セットを削除する(パーツから参照されていれば ValueError)。

    Raises:
        ValueError: セットがいずれかのパーツから参照されているとき。

    """
    users = referencing_parts(score_file, name)
    if users:
        msg = f"選択セット '{name}' はパーツ {users} から参照されているため削除できません"
        raise ValueError(msg)
    score_file["selectionSets"].pop(name, None)


def save_set_as(score_file: dict[str, Any], src_name: str, new_name: str) -> None:
    """別名で保存: セットを新しい名前でコピーする。

    既存の ref は元の名前を指したまま変わらない。

    Raises:
        ValueError: 新しい名前が空、または既存のセット名と重複するとき。

    """
    if not new_name:
        msg = "新しいセット名を入力してください"
        raise ValueError(msg)
    if new_name in score_file["selectionSets"]:
        msg = f"選択セット '{new_name}' は既に存在します"
        raise ValueError(msg)
    score_file["selectionSets"][new_name] = json.loads(json.dumps(score_file["selectionSets"][src_name]))


# ---------------------------------------------------------------------- 検証


def _format_pydantic_error(err: ValidationError, data: dict[str, Any] | None = None) -> list[str]:
    """読めるエラーメッセージに整形する。

    元データ `data` があれば、
    'score_parts.12.aggregations.WL' のような位置表記を
    「パーツ '<名前>' の aggregations.WL」に変換する(添字ではどのパーツが
    壊れているのかユーザに伝わらない)。

    Returns:
        検証エラー1件につき1つの整形済みメッセージのリスト。

    """
    msgs = []
    for e in err.errors():
        loc = [x for x in e["loc"] if x != "__root__"]
        msg = e["msg"].removeprefix("Value error, ")
        name = None
        rest = ""
        if data is not None and "score_parts" in loc:
            k = loc.index("score_parts")
            try:
                container: Any = data
                for seg in loc[:k]:
                    container = container[seg]
                name = container["score_parts"][loc[k + 1]].get("name")
                rest = ".".join(str(x) for x in loc[k + 2 :])
            except Exception:
                name = None
        if name:
            msgs.append(f"パーツ '{name}'" + (f" の {rest}" if rest else "") + f": {msg}")
        else:
            joined = ".".join(str(x) for x in loc)
            msgs.append(f"{joined}: {msg}" if joined else msg)
    return msgs


def validate_score_file(data: dict[str, Any]) -> list[str]:
    """エンジンが読み込み時に報告する問題すべてを読める文字列のリストで返す。

    空リスト = OK。pydantic だけでは見ない expression の
    パース/参照チェックとパーツ名の重複も含む。

    Returns:
        問題1件につき1つのメッセージのリスト(問題が無ければ空リスト)。

    """
    try:
        sf = ScoreFile.model_validate(data)
    except ValidationError as err:
        return _format_pydantic_error(err, data)

    problems: list[str] = []
    names = [p.name for p in sf.score_parts]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        problems.append(f"スコアパーツ名が重複しています: {sorted(dupes)}")
    if sf.expression:
        problems += _expression_problems(sf.expression, names)
    for key in sf.constraintThreshold:
        if key not in names:
            problems.append(f"constraintThreshold のキー '{key}' に対応するスコアパーツがありません")
    for part in sf.score_parts:
        try:
            part.resolve_selection_refs(sf.selectionSets, sf.weightSets)
        except (ValueError, ValidationError) as err:
            problems.append(str(err) if isinstance(err, ValueError) else "; ".join(_format_pydantic_error(err)))
    return problems


def _expression_problems(expression: str, names: list[str]) -> list[str]:
    """全パーツ値を 1.0 のダミーにして式を評価してみる(構文と参照の検査)。

    Returns:
        評価に失敗すれば "expression: <理由>" 1件のリスト、成功なら空リスト。

    """
    try:
        evaluate_expression(expression, dict.fromkeys(names, 1.0))
    except Exception as err:
        return [f"expression: {err}"]
    return []


def validate_part(
    part: dict[str, Any],
    selection_sets: dict[str, list] | None = None,
    weight_sets: dict[str, Any] | None = None,
) -> list[str]:
    """編集中のパーツ1つぶんの検証。

    Returns:
        validate_score_file と同形式の問題メッセージのリスト(空 = OK)。

    """
    single = {
        "score_parts": [part],
        "expression": "",
        "constraintThreshold": {},
        "selectionSets": selection_sets or {},
        "weightSets": weight_sets or {},
    }
    return validate_score_file(single)


def part_types_without_data(score_file: dict[str, Any], ctx: dict[str, Any] | None) -> set:
    """読み込んだデータに測定ファイルの無い type を使うパーツの `_uid` 集合。

    別実験の config を読むとデータに無い type のパーツが残る — これは意図した
    設計(読み込んだ設定を黙って変えず、取捨選択はユーザーが行う)だが、
    テスト計算して初めてエラーで気づくのは遠回りなので、一覧の ⚠ と編集画面の
    警告に使う。custom はデータ type を持たないので対象外。設定のみ編集モード
    ではカタログが設定自身から導出されるため自然に空になる。

    Returns:
        カタログに type の無いパーツの `_uid` の集合。

    """
    catalogs = (ctx or {}).get("catalogs") or {}
    return {
        p.get("_uid")
        for p in score_file.get("score_parts", [])
        if p.get("type") not in {None, "custom"} and p.get("type") not in catalogs
    }


# -------------------------------------------------- コンテキスト(画面1)


def _resolve_optional_file(
    explicit: str | None, discover: Callable[[], list[Path]], label: str
) -> tuple[Path | None, str | None]:
    """任意の同梱ファイルの解決: 明示指定が最優先、無ければ自動検出。

    明示指定は存在必須。無ければデータディレクトリ内の自動検出に
    フォールバック。`discover` は候補の**リスト**を返し、2件以上はエラー
    (アルファベット順の先頭を黙って採用すると、意図と違うファイルで
    設計してしまう)。
    (パス or None, '指定' | '自動検出' | None) を返す。

    Returns:
        (解決したパス, 入手経路) のタプル。経路は '指定' か '自動検出'。
        該当ファイルが無ければ (None, None)。

    Raises:
        ValueError: 明示指定のファイルが存在しない、または自動検出の候補が
            2件以上見つかったとき。

    """
    if explicit and str(explicit).strip():
        p = Path(str(explicit).strip()).resolve()
        if not p.is_file():
            msg = f"{label} が見つかりません: {p}"
            raise ValueError(msg)
        return p, "指定"
    found = list(discover())
    if len(found) > 1:
        raise ValueError(
            f"{label} の候補が複数見つかりました(自動検出できません。パスを明示指定してください): "
            + ", ".join(str(p) for p in found)
        )
    return (found[0], "自動検出") if found else (None, None)


def _resolve_data_dir(data_dir: str) -> Path:
    """測定結果ディレクトリの入力を検証し、実在する絶対パスにして返す。

    Returns:
        存在確認済みの絶対パス。

    Raises:
        ValueError: パスが未入力、またはディレクトリが存在しないとき。

    """
    if not str(data_dir).strip():
        # Path("") はカレントディレクトリ扱いになり、起動場所を誤って走査してしまう
        msg = "測定結果ディレクトリのパスを入力してください"
        raise ValueError(msg)
    d = Path(str(data_dir).strip()).resolve()
    if not d.is_dir():
        msg = f"ディレクトリが見つかりません: {d}"
        raise ValueError(msg)
    return d


def _resolve_coef_file(coef_path: str | None, d: Path) -> tuple[Path | None, str | None]:
    """dVtBudget係数jsonc の解決と読み込み検証。

    Returns:
        (解決したパス, 入手経路) のタプル。該当ファイルが無ければ (None, None)。

    Raises:
        ValueError: 候補が複数見つかった、または読み込み・検証に失敗したとき。

    """
    coef_file, coef_source = _resolve_optional_file(
        coef_path, lambda: introspect.find_dvtbudget_coefs(d), "dVtBudget係数jsonc"
    )
    if coef_file is not None:
        try:
            io_jsonc.load_dvtbudget_coef(coef_file)
        except Exception as err:
            msg = f"dVtBudget係数jsoncを読み込めません ({coef_file}): {err}"
            raise ValueError(msg) from err
    return coef_file, coef_source


def _config_context_entries(config_file: Path) -> dict[str, Any]:
    """optimization設定jsonc を読み込み、context に載せる項目を返す。

    Returns:
        generation / wlgroup / wlgroup_defin_logical / wlgroup_weight
        (+ スコア設定があれば existing_score_file)の dict。

    Raises:
        ValueError: 設定の読み込み・検証に失敗したとき
            (pydantic の検証エラーはパーツ名つきの整形済みメッセージ入り)。

    """
    try:
        run_config = io_jsonc.load_run_config(config_file)
    except ValidationError as err:
        # パーツ名つきの読めるメッセージにするため、生の dict を読み直して整形する
        try:
            raw = jsonc.loads(Path(config_file).read_text(encoding="utf-8"))
        except Exception:
            raw = None
        details = "; ".join(_format_pydantic_error(err, raw if isinstance(raw, dict) else None))
        msg = f"optimization設定jsoncを読み込めません ({config_file}): {details}"
        raise ValueError(msg) from err
    except Exception as err:
        msg = f"optimization設定jsoncを読み込めません ({config_file}): {err}"
        raise ValueError(msg) from err
    entries: dict[str, Any] = {
        "generation": run_config.Generation,
        "wlgroup": dict(run_config.optimization.WLgroup),
        "wlgroup_defin_logical": run_config.optimization.WLgroupDefinLogical,
        "wlgroup_weight": run_config.optimization.WLgroupWeight,
    }
    existing = run_config.to_score_file()
    if existing.score_parts or existing.selectionSets or existing.expression:
        entries["existing_score_file"] = existing.model_dump(exclude_none=True)
    return entries


def _parse_geninfo(geninfo_file: Path) -> dict[str, Any]:
    """世代情報json を読み、トップレベルのオブジェクト(dict)を返す。

    Returns:
        世代情報の dict。

    Raises:
        TypeError: トップレベルがオブジェクトではないとき。

    """
    geninfo = jsonc.loads(geninfo_file.read_text(encoding="utf-8"))
    if not isinstance(geninfo, dict):
        msg = "トップレベルがオブジェクトではありません"
        raise TypeError(msg)
    return geninfo


def _resolve_geninfo(
    geninfo_path: str | None, d: Path, generation: str | None
) -> tuple[dict[str, Any] | None, Path | None, str | None]:
    """世代情報json の解決と読み込み。

    Returns:
        (世代情報 dict, 解決したパス, 入手経路) のタプル。
        該当ファイルが無ければ (None, None, None)。

    Raises:
        ValueError: 候補が複数見つかった、または読み込みに失敗したとき。

    """
    geninfo_file, geninfo_source = _resolve_optional_file(
        geninfo_path,
        lambda: [p for p in [introspect.find_generation_info(d, generation)] if p],
        "世代情報json",
    )
    if geninfo_file is None:
        return None, None, None
    try:
        geninfo = _parse_geninfo(geninfo_file)
    except Exception as err:
        msg = f"世代情報jsonを読み込めません ({geninfo_file}): {err}"
        raise ValueError(msg) from err
    return geninfo, geninfo_file, geninfo_source


def _resolve_custom_functions(custom_path: str | None, d: Path) -> tuple[Path | None, str | None, list[str]]:
    """自作関数ファイル(custom_parts.py)の解決と読み込み。

    SVN 管理された自作関数(scorelib_param/custom.py)。読み込み = import =
    トップレベルコードの実行になるが、レビュー済みリポジトリ由来のファイルが
    前提(任意ユーザの入力ではない)ので許容。

    Returns:
        (解決したパス, 入手経路, 関数名リスト) のタプル。
        該当ファイルが無ければ (None, None, [])。

    Raises:
        ValueError: 候補が複数見つかった、または読み込みに失敗したとき。

    """
    custom_file, custom_source = _resolve_optional_file(
        custom_path,
        lambda: [p for p in [d / scorelib_custom.DEFAULT_FILENAME] if p.is_file()],
        "自作関数ファイル",
    )
    if custom_file is None:
        return None, None, []
    try:
        module = scorelib_custom.load_custom_module(custom_file)
        functions = scorelib_custom.list_custom_functions(module)
    except Exception as err:
        msg = f"自作関数ファイルを読み込めません ({custom_file}): {err}"
        raise ValueError(msg) from err
    return custom_file, custom_source, functions


def build_context(
    data_dir: str,
    config_path: str | None = None,
    coef_path: str | None = None,
    geninfo_path: str | None = None,
    custom_path: str | None = None,
) -> dict[str, Any]:
    """画面1が測定結果ディレクトリから導出するもの一式。

    測定結果ディレクトリは同系統の過去実験の出力(設計書 5.1節)。
    result_tmp には通常測定結果しか入らないため、optimization設定jsonc と
    dVtBudget係数jsonc 等は別の(任意の)パスとして受け取る。たまたま
    ディレクトリ内に置いてある場合のための自動検出も残してある
    (解決と読み込みは同梱ファイルごとの _resolve_* ヘルパーが担う)。

    データディレクトリが未入力・不存在、同梱ファイルの候補が複数、または
    各同梱ファイルの読み込み・検証に失敗したときは、各ヘルパーが
    ValueError を送出する(メッセージは従来どおり)。

    Returns:
        data_dir / types / part_types / catalogs / measure_labels、各同梱
        ファイルのパスと入手経路、WLgroup 系、existing_score_file、
        custom_functions 等を持つ context dict。

    """
    d = _resolve_data_dir(data_dir)
    types = introspect.detect_types(d)
    coef_file, coef_source = _resolve_coef_file(coef_path, d)
    config_file, config_source = _resolve_optional_file(
        config_path, lambda: introspect.find_run_configs(d), "optimization設定jsonc"
    )

    part_types = list(types)
    if "FBC" in types and coef_file is not None:
        part_types.append("dVtBudget")

    ctx: dict[str, Any] = {
        "data_dir": str(d),
        "types": types,
        "part_types": part_types,
        "catalogs": {t: introspect.axis_catalog(d, t) for t in part_types},
        # Measure 番号 → dataName(UI の複合表示「dataName (Measure N)」と labels 注記用)
        "measure_labels": {t: introspect.measure_labels(d, t) for t in part_types},
        "coef_path": str(coef_file) if coef_file else None,
        "coef_source": coef_source,
        "config_path": str(config_file) if config_file else None,
        "config_source": config_source,
        "has_initial_temperature": (d / "initial_temperature.csv").exists(),
        "generation": None,
        "wlgroup": {},
        "wlgroup_defin_logical": True,
        "wlgroup_weight": None,
        "existing_score_file": None,
        "geninfo": None,
        "geninfo_path": None,
        "geninfo_source": None,
        "custom_path": None,
        "custom_source": None,
        "custom_functions": [],
    }
    if config_file:
        ctx.update(_config_context_entries(config_file))

    geninfo, geninfo_file, geninfo_source = _resolve_geninfo(geninfo_path, d, ctx["generation"])
    if geninfo is not None:
        ctx["geninfo"] = geninfo
        ctx["geninfo_path"] = str(geninfo_file)
        ctx["geninfo_source"] = geninfo_source

    custom_file, custom_source, custom_functions = _resolve_custom_functions(custom_path, d)
    if custom_file is not None:
        ctx["custom_path"] = str(custom_file)
        ctx["custom_source"] = custom_source
        ctx["custom_functions"] = custom_functions
        # `catalogs` を作った後に足す: custom 擬似typeには軸カタログが無い。
        # += でなく新リストを作る(元のリストを共有している参照を書き換えないため)
        ctx["part_types"] = ctx["part_types"] + ["custom"]
    return ctx


def parse_chip_counts(text: str, n_boards: int) -> list[int]:
    """「Board ごとの Chip 数」入力をパースする。

    数1つ = 全 Board 共通、カンマ区切り = Board 別(個数は Board 数と
    一致すること)。

    Returns:
        Board ごとの Chip 数のリスト(長さは n_boards)。

    Raises:
        ValueError: 入力が空・数値でない・個数が Board 数と不一致・
            1未満の値を含むとき。

    """
    t = (text or "").strip()
    if not t:
        msg = "Chip 数を入力してください(例: 4 または 4,4,2,2)"
        raise ValueError(msg)
    try:
        counts = [int(p.strip()) for p in t.split(",")]
    except ValueError as err:
        msg = f"Chip 数が数値ではありません: {text!r}"
        raise ValueError(msg) from err
    if len(counts) == 1:
        counts *= n_boards
    if len(counts) != n_boards:
        msg = f"Chip 数の個数({len(counts)})が Board 数({n_boards})と一致しません"
        raise ValueError(msg)
    if any(c < 1 for c in counts):
        msg = "Chip 数は1以上にしてください"
        raise ValueError(msg)
    return counts


def expand_dummy_bundle(dummy_dir: str, chip_counts: list[int]) -> str:
    """ダミー一式(Board/Chip は1つ)を一時ディレクトリへ Board/Chip 複製展開する。

    展開先のパスを返す(後段は通常の build_context がそのまま働く —
    docs/spec_change_dataname_measure.md プラン4)。

    Returns:
        Board/Chip を複製展開した一時ディレクトリのパス。

    Raises:
        ValueError: パスが未入力、またはディレクトリが存在しないとき。

    """
    import tempfile

    from scorelib_param import dummy

    if not str(dummy_dir or "").strip():
        msg = "ダミー一式ディレクトリのパスを入力してください"
        raise ValueError(msg)
    src = Path(str(dummy_dir).strip()).resolve()
    if not src.is_dir():
        msg = f"ディレクトリが見つかりません: {src}"
        raise ValueError(msg)
    dest = Path(tempfile.mkdtemp(prefix="scorelib_dummy_"))
    dummy.expand_boards_chips(src, dest, chip_counts)
    return str(dest)


def save_upload(filename: str, data: bytes) -> str:
    """アップロードされた1ファイルを一時ディレクトリへ保存してパスを返す。

    画面1: パス入力欄の代わりにアップロードで指定する経路。保存先のパスが
    そのままパス欄に入り、以降は通常のパス指定と同じに扱われる。

    Returns:
        一時ディレクトリに保存したファイルのパス。

    """
    import tempfile

    name = Path(filename).name or "uploaded"  # パス成分は捨てる(zip-slip と同種の対策)
    d = Path(tempfile.mkdtemp(prefix="scorelib_upload_"))
    p = d / name
    p.write_bytes(data)
    return str(p)


def extract_bundle_zip(data: bytes) -> str:
    """アップロードされた一式zipを一時ディレクトリへ展開してそのパスを返す。

    一式zip = result_tmp のファイル群 + 設定jsonc + 係数jsonc +
    世代情報json + custom_parts.py(後段は locate_bundle_inputs /
    build_context)。フォルダごと圧縮された zip によくある
    「トップに1フォルダ」の場合はその中へ降りる。

    Returns:
        展開先ディレクトリのパス(トップに1フォルダの zip はその中のパス)。

    """
    import io
    import tempfile
    import zipfile

    target = Path(tempfile.mkdtemp(prefix="scorelib_bundle_"))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for m in z.infolist():
            parts = Path(m.filename).parts
            if not parts or m.filename.startswith(("/", "\\")) or ".." in parts:
                continue  # zip-slip(絶対パス・.. による展開先脱出)対策
            z.extract(m, target)
    entries = list(target.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return str(entries[0])
    return str(target)


def _walk_dirs(root: Path, max_depth: int = 4) -> list[Path]:
    out = [root]
    if max_depth <= 0:
        return out
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        out += _walk_dirs(child, max_depth - 1)
    return out


def locate_bundle_inputs(extracted_dir: str) -> dict[str, str | None]:
    """展開済み一式zipの中から測定ディレクトリと同梱ファイルを探す。

    深さ制限つきのツリー探索。判別はディレクトリ内自動検出と同じ**中身ベース**:
    測定ディレクトリ = type が検出できるディレクトリ(parameterLabel_*/
    dataName_*/Measure列つきcsv)、設定 = optimization{} ブロックを持つ jsonc、
    係数 = 3段 {a, b} 表として検証が通る jsonc、世代情報 = {Generation}.json
    (Generation は設定から読む)、custom = custom_parts.py。
    同じ役割の候補が2つ以上あったらエラー — 黙って1つ選ぶと意図と違う
    ファイルで設計してしまう。

    Returns:
        data_dir / config_path / coef_path / geninfo_path / custom_path を
        キーに持つ dict(見つからなかった役割は None)。

    Raises:
        ValueError: 測定結果ディレクトリが見つからない、または同じ役割の
            候補が2つ以上あるとき。

    """
    root = Path(extracted_dir)
    dirs = _walk_dirs(root)

    def _at_most_one(paths: list[Path], label: str) -> str | None:
        if len(paths) > 1:
            raise ValueError(
                f"zip内に{label}の候補が複数あります(どれを使うか判断できません): " + ", ".join(str(p) for p in paths)
            )
        return str(paths[0]) if paths else None

    data_dirs = [d for d in dirs if introspect.detect_types(d)]
    if not data_dirs:
        msg = f"zip内に測定結果(parameterLabel_*/dataName_* 等のあるディレクトリ)が見つかりません: {root}"
        raise ValueError(msg)
    data_dir = _at_most_one(data_dirs, "測定結果ディレクトリ")

    config_path = _at_most_one([p for d in dirs for p in introspect.find_run_configs(d)], "optimization設定jsonc")
    coef_path = _at_most_one([p for d in dirs for p in introspect.find_dvtbudget_coefs(d)], "dVtBudget係数jsonc")
    custom_path = _at_most_one(
        [p for d in dirs for p in [d / scorelib_custom.DEFAULT_FILENAME] if p.is_file()],
        "自作関数ファイル",
    )

    generation = None
    if config_path:
        try:
            raw = jsonc.loads(Path(config_path).read_text(encoding="utf-8"))
            generation = raw.get("Generation") if isinstance(raw, dict) else None
        except Exception:
            generation = None
    geninfo_path = _at_most_one(
        [p for d in dirs for p in [introspect.find_generation_info(d, generation)] if p],
        "世代情報json",
    )

    return {
        "data_dir": data_dir,
        "config_path": config_path,
        "coef_path": coef_path,
        "geninfo_path": geninfo_path,
        "custom_path": custom_path,
    }


def axis_counts(geninfo: dict[str, Any] | None) -> dict[str, int]:
    """世代情報json(B9LS.json の形)から 軸 → 本数 の対応を取り出す。

    ファイルに記述のある軸だけ。

    Returns:
        軸名 → 本数の dict(numWLs → WL、numStrings → STR)。

    """
    counts: dict[str, int] = {}
    if isinstance(geninfo, dict):
        if isinstance(geninfo.get("numWLs"), int):
            counts["WL"] = geninfo["numWLs"]
        if isinstance(geninfo.get("numStrings"), int):
            counts["STR"] = geninfo["numStrings"]
    return counts


def data_axis_counts(catalogs: dict[str, dict[str, list | None]]) -> dict[str, int]:
    """カタログ(軸→値候補)から数値軸の本数を導出する(max+1)。

    WL/STR 等の本数は世代で固定で、測定フローが一部だけ測る設定は存在しない
    (2026-07-28 担当者確認 — docs/spec_change_dataname_measure.md 9節)ため、
    ダミー/実データの最大値+1 が総数として正確。Measure(実験ごとの測定数)と
    InBatchEpoch は「本数」の概念が違うため除外する。

    Returns:
        数値軸の軸名 → 本数(全カタログでの最大値+1)の dict。

    """
    counts: dict[str, int] = {}
    for cat in catalogs.values():
        for axis, cands in cat.items():
            if axis in {"Measure", "InBatchEpoch"} or not cands:
                continue
            if all(isinstance(v, int) and not isinstance(v, bool) for v in cands):
                counts[axis] = max(counts.get(axis, 0), max(cands) + 1)
    return counts


def validation_axis_counts(ctx: dict[str, Any] | None) -> dict[str, int]:
    """本数整合チェックに使う軸本数を返す。

    データ由来を正とし、世代情報 json(自動検出されていれば)は
    データに現れない軸の補完にだけ使う(観測 > 宣言)。

    Returns:
        軸名 → 本数の dict(ctx が無ければ空 dict)。

    """
    if not ctx:
        return {}
    counts = data_axis_counts(ctx.get("catalogs") or {})
    for axis, n in axis_counts(ctx.get("geninfo")).items():
        counts.setdefault(axis, n)
    return counts


def geninfo_mismatch_warnings(ctx: dict[str, Any] | None) -> list[str]:
    """自動検出された世代情報 json とデータ由来本数の食い違い警告。

    診断情報: 実験異常か json の古さ。検査自体はデータ由来の値で行う。

    Returns:
        食い違いのある軸ごとの警告文のリスト(食い違いが無ければ空)。

    """
    if not ctx or not ctx.get("geninfo"):
        return []
    data_counts = data_axis_counts(ctx.get("catalogs") or {})
    out: list[str] = []
    for axis, n in axis_counts(ctx["geninfo"]).items():
        d = data_counts.get(axis)
        if d is not None and d != n:
            out.append(
                f"{axis}: データからは {d} 本ですが、世代情報 json({ctx.get('geninfo_path')})"
                f"は {n} 本と宣言しています。データ由来の値で検査します"
            )
    return out


def _format_value_runs(values: list[int]) -> str:
    """[0,1,2,5] → '0-2, 5'(未カバー値一覧の圧縮表示)。

    Returns:
        連続区間を「開始-終了」にまとめたカンマ区切りの文字列。

    """
    runs: list[str] = []
    start = prev = values[0]
    for v in [*values[1:], None]:  # type: ignore[list-item]
        if v is not None and v == prev + 1:
            prev = v
            continue
        runs.append(str(start) if start == prev else f"{start}-{prev}")
        if v is not None:
            start = prev = v
    return ", ".join(runs)


def group_def_warnings(score_file: dict[str, Any], counts: dict[str, int]) -> list[str]:
    """グループ定義と軸本数の不整合を列挙する。

    `counts` は 軸 → 本数
    (通常は validation_axis_counts: データ由来が正、世代情報 json は補完)。
    エラーではなく警告: 計算自体は通るが、チップの WL/STR 本数と食い違う
    範囲はほぼ確実に設定ミス(joint WL をグループから除外する運用は基本
    無い、と担当者確認済みなので全値をチェックする)。

    Returns:
        範囲外グループ・未カバー値を指摘する警告文のリスト。

    """
    warnings: list[str] = []
    for name, gd in (score_file.get("groupDefs") or {}).items():
        n = counts.get(gd.get("axis"))
        groups = gd.get("groups") or {}
        if not n or not groups:
            continue
        axis = gd["axis"]
        covered: set = set()
        out_of_range = []
        for label, rng in groups.items():
            lo, hi = int(rng[0]), int(rng[1])
            covered.update(range(max(lo, 0), min(hi, n - 1) + 1))
            if lo < 0 or hi > n - 1:
                out_of_range.append(f"{label}({lo}-{hi})")
        if out_of_range:
            warnings.append(
                f"グループ定義 '{name}': {axis} は {n} 本(0-{n - 1})ですが、"
                f"範囲外を含むグループがあります: {', '.join(out_of_range)}"
            )
        missing = [v for v in range(n) if v not in covered]
        if missing:
            warnings.append(
                f"グループ定義 '{name}': {axis} の {_format_value_runs(missing)} がどのグループにも入りません"
            )
    return warnings


# -------------------------------------------------------------- 表示ラベル
# 純関数にしてある理由: D&D コンポーネントの描画内容は AppTest から観測でき
# ないため、マーカー等のロジックはここで単体テストできる形に切り出しておく


def part_list_labels(
    score_file: dict[str, Any],
    selected_uid: str | None,
    invalid_uids: set,
    rows: list[dict[str, str]] | None = None,
) -> list[str]:
    """常時ドラッグ可能なパーツ一覧のラベルを作る。

    ⠿ ドラッグハンドル、検証NGのパーツに ⚠、選択中のパーツに ← 編集中。
    `rows`(part_summary_rows の出力)は呼び出し元が計算済みなら渡せる。

    Returns:
        パーツごとの表示ラベルのリスト(score_parts と同順)。

    """
    rows = rows if rows is not None else part_summary_rows(score_file)
    labels = []
    for i, (row, p) in enumerate(zip(rows, score_file["score_parts"], strict=False)):
        labels.append(
            "⠿ "
            + ("⚠ " if p.get("_uid") in invalid_uids else "")
            + f"{i + 1}. {row['名前']}({row['type']}, 相対化{row['相対化']})"
            + (" ← 編集中" if p.get("_uid") == selected_uid else "")
        )
    return labels


def part_select_labels(score_file: dict[str, Any], invalid_uids: set) -> dict[str, str]:
    """パーツ選択プルダウンの uid → 表示ラベル。

    番号付きにして、2パーツが
    (一時的にでも)同名になってもラベルが一意になるようにする: Streamlit の
    selectbox フロントエンドは表示ラベルで項目を照合するため、重複ラベルが
    あるとクリックが別パーツに解決されたり無反応になったりする。番号は
    ⠿ 一覧とも揃う。

    Returns:
        パーツ _uid → 「番号. (⚠ )名前」形式の表示ラベルの dict。

    """
    return {
        p.get("_uid"): (f"{i + 1}. " + ("⚠ " if p.get("_uid") in invalid_uids else "") + p.get("name", ""))
        for i, p in enumerate(score_file["score_parts"])
    }


def part_summary_rows(score_file: dict[str, Any]) -> list[dict[str, str]]:
    """一覧表の行(名前 / type / 相対化 / 軸。custom パーツは関数名を表示)。

    Returns:
        パーツごとの {"名前", "type", "相対化", "軸"} 行の dict のリスト。

    """
    rows = []
    for p in score_file["score_parts"]:
        if p.get("type") == "custom":
            rows.append(
                {
                    "名前": p.get("name", ""),
                    "type": "custom",
                    "相対化": "—",
                    "軸": f"関数 {p.get('function') or p.get('name', '')}",
                }
            )
            continue
        axes = [e for e in p.get("order", []) if not e.startswith("__")]
        rows.append(
            {
                "名前": p.get("name", ""),
                "type": p.get("type", ""),
                "相対化": "あり" if p.get("relative") else "なし",
                "軸": ", ".join(axes),
            }
        )
    return rows


# ---------------------------------------------- 下書き・エクスポート・計算


def score_file_to_jsonc(score_file: dict[str, Any]) -> str:
    """ScoreFile dict を検証してエクスポート用の jsonc テキストにする。

    Returns:
        WLgroup 系を旧形式キーへ移した、インデント2・末尾改行つきの
        jsonc テキスト。

    """
    cleaned = ScoreFile.model_validate(score_file).model_dump(exclude_none=True)
    # WL 軸の "WLgroup" 定義と "WLgroupWeight" は**旧形式キーだけ**に書き出す
    # (0.7.0 — 定義の在り処を1つにする): 合成後 config では実験スクリプトが
    # 読む optimization.WLgroup / WLgroupWeight がそのまま編集後の内容になり、
    # 手編集時も「groupDefs とどちらが使われるか」の迷いが生じない。
    # 読み戻しは ScoreFile._absorb_legacy_wlgroup が対称に行う
    gd = (cleaned.get("groupDefs") or {}).get("WLgroup")
    if gd and gd.get("axis") == "WL":
        cleaned["groupDefs"].pop("WLgroup")
        if not cleaned["groupDefs"]:
            cleaned.pop("groupDefs")
        cleaned["WLgroup"] = gd.get("groups", {})
        # 現行 config の流儀("True"/"False" 文字列)に合わせる
        cleaned["WLgroupDefinLogical"] = "True" if gd.get("definedInLogical", True) else "False"
    weights = cleaned.get("weightSets") or {}
    if "WLgroupWeight" in weights:
        cleaned["WLgroupWeight"] = weights.pop("WLgroupWeight")
        if not weights:
            cleaned.pop("weightSets", None)
    return json.dumps(cleaned, indent=2, ensure_ascii=False) + "\n"


def save_draft(
    score_file: dict[str, Any],
    context_inputs: dict[str, str | None] | None = None,
    path: Path | None = None,
) -> None:
    """下書きを保存する。

    下書きには score file と一緒に画面1の入力(data_dir / config_path /
    coef_path / ...)も保存する。復元時にデータ読み込みまで再現でき、
    ユーザを画面1からやり直させずに済む。
    """
    path = path or DRAFT_PATH  # テストが DRAFT_PATH を差し替えられるよう呼び出し時に解決
    payload = {"score_file": score_file, "context_inputs": context_inputs or {}}
    path.parent.mkdir(parents=True, exist_ok=True)  # DRAFTS_DIR は初回保存時に作られる
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_draft(path: Path | None = None) -> dict[str, Any] | None:
    """{"score_file": ..., "context_inputs": {...}} か None を返す。

    context_inputs 導入前の旧形式(素の ScoreFile dict)も受け付ける。

    Returns:
        {"score_file": ..., "context_inputs": {...}} の dict。下書きが
        存在しない・読めない・形式が不正な場合は None。

    """
    path = path or DRAFT_PATH
    if not path.exists():
        return None
    try:
        data = jsonc.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("score_file"), dict) and "score_parts" in data["score_file"]:
        return {"score_file": data["score_file"], "context_inputs": data.get("context_inputs") or {}}
    if "score_parts" in data:  # 旧形式
        return {"score_file": data, "context_inputs": {}}
    return None


def _part_refs(part: dict[str, Any]) -> list[str]:
    """パーツが参照している選択セット名(エクスポート同梱用)。

    変換op(TRANSFORM_OPS)の ref は重みセット参照なので含めない。

    Returns:
        参照している選択セット名の重複なしソート済みリスト。

    """
    return sorted(
        {
            s["ref"]
            for s in _part_specs(part)
            if isinstance(s, dict) and s.get("ref") and s.get("op") not in TRANSFORM_OPS
        }
    )


def _part_weight_refs(part: dict[str, Any]) -> list[str]:
    """パーツが参照している重みセット名(変換opの ref + 集計時重みの weight_ref)。

    Returns:
        参照している重みセット名の重複なしソート済みリスト。

    """
    specs = [s for s in _part_specs(part) if isinstance(s, dict)]
    return sorted(
        {s["ref"] for s in specs if s.get("ref") and s.get("op") in TRANSFORM_OPS}
        | {s["weight_ref"] for s in specs if s.get("weight_ref")}
    )


def export_part(score_file: dict[str, Any], index: int) -> str:
    """パーツ1つを自己完結の score.jsonc としてエクスポートする。

    参照する選択セット・重みセットとグループ定義を同梱。
    別の場所で再インポートできる。

    Returns:
        パーツ1つ+参照セット+グループ定義を同梱した jsonc テキスト。

    Raises:
        ValueError: パーツが参照する選択セット・重みセットが未定義のとき。

    """
    part = score_file["score_parts"][index]
    refs = _part_refs(part)
    missing = [r for r in refs if r not in score_file["selectionSets"]]
    if missing:
        msg = f"パーツ '{part.get('name')}' が参照する選択セットが未定義です: {missing}"
        raise ValueError(msg)
    weight_refs = _part_weight_refs(part)
    all_weights = score_file.get("weightSets", {})
    missing_w = [r for r in weight_refs if r not in all_weights]
    if missing_w:
        msg = f"パーツ '{part.get('name')}' が参照する重みセットが未定義です: {missing_w}"
        raise ValueError(msg)
    all_defs = score_file.get("groupDefs", {})
    used_defs = sorted(_part_axis_names(part) & set(all_defs))
    bundle = {
        "score_parts": [part],
        "expression": part.get("name", ""),
        "constraintThreshold": {},
        "selectionSets": {r: score_file["selectionSets"][r] for r in refs},
        "groupDefs": {n: all_defs[n] for n in used_defs},
        "weightSets": {r: all_weights[r] for r in weight_refs},
    }
    return score_file_to_jsonc(bundle)


@dataclass(frozen=True)
class TestComputeInputs:
    """画面5のテスト計算に渡す任意入力(スコア設定・データディレクトリ以外)。

    generation と coef_path は画面5の入力欄から、wlgroup / custom_path /
    geninfo_path は context からの引き継ぎ。すべて未指定でも計算は動く
    (dVtBudget 等、必要とするパーツがあればエンジン側のエラーになる)。
    """

    generation: str | None = None
    wlgroup: dict[str, Any] | None = None
    coef_path: str | None = None
    custom_path: str | None = None
    geninfo_path: str | None = None


def run_test_compute(
    score_file: dict[str, Any],
    data_dir: str,
    inputs: TestComputeInputs | None = None,
) -> dict[str, float | None]:
    """画面5: 実データでエンジンを走らせる。

    係数ファイルは指定があれば
    `inputs.coef_path` から(通常 result_tmp の外にある)。
    initial_temperature.csv は測定出力なので常にデータディレクトリから読む。

    Returns:
        {"Score": 式の合成値, パーツ名: パーツ値, ...} の dict
        (エンジン compute_score_file の結果そのまま)。

    Raises:
        ValueError: データディレクトリが存在しない、または係数jsoncの
            候補が複数見つかったとき。

    """
    from scorelib_param.cli import compute_score_file
    from scorelib_param.dvtbudget import load_board_temperatures
    from scorelib_param.models import RunConfig

    inputs = inputs or TestComputeInputs()
    d = Path(data_dir)
    if not d.is_dir():
        msg = f"ディレクトリが見つかりません: {d}"
        raise ValueError(msg)
    sf = ScoreFile.model_validate(score_file)
    dump = sf.model_dump(exclude_none=True)
    run_config = RunConfig.model_validate(
        {
            "Generation": inputs.generation or "",
            "optimization": {
                "score_parts": dump["score_parts"],
                "expression": sf.expression,
                "constraintThreshold": {},  # 閾値はパーツの値に影響しない
                "selectionSets": sf.selectionSets,
                # score file 側の定義が config の WLgroup より優先される
                "WLgroup": inputs.wlgroup or {},
                "groupDefs": dump.get("groupDefs", {}),
                "weightSets": dump.get("weightSets", {}),
            },
        }
    )
    coef_file, _ = _resolve_optional_file(
        inputs.coef_path, lambda: introspect.find_dvtbudget_coefs(d), "dVtBudget係数jsonc"
    )
    coef = io_jsonc.load_dvtbudget_coef(coef_file) if coef_file else None
    temp_csv = d / "initial_temperature.csv"
    temps = load_board_temperatures(temp_csv) if temp_csv.exists() else None
    return compute_score_file(
        d,
        run_config,
        dvtbudget_coef=coef,
        board_temperatures=temps,
        custom_parts_path=inputs.custom_path,
        generation_info_path=inputs.geninfo_path,
    )


def _config_measure_labels(part: dict[str, Any]) -> dict[int, str]:
    """パーツの labels 注記から Measure 番号 → dataName を回収する。

    labels 注記は相対化・集計spec のもの(設定のみ編集モードの表示用。
    設定に書かれている範囲だけ)。

    Returns:
        Measure 番号 → dataName の dict(int に変換できない番号は捨てる)。

    """
    out: dict[int, str] = {}
    specs = [*list(part.get("aggregations", {}).values()), part.get("relative") or {}]
    for s in specs:
        if not isinstance(s, dict):
            continue
        for k, v in (s.get("labels") or {}).items():
            try:
                out[int(k)] = str(v)
            except (TypeError, ValueError):
                continue
    return out


def config_only_context(score_file: dict[str, Any], generation: str | None = None) -> dict[str, Any]:
    """「設定だけ編集」モードの context を作る。

    データを読まず、**設定自身が言及する軸名**からカタログを導出する
    (値候補は無し = 自由入力)。既存設定の微修正
    (式・グループ定義・filter 値の変更等)とエクスポートに必要な情報は設定
    ファイルが全部持っているため、これでエディタ・検証・エクスポートが動く。
    テスト計算だけはデータ(またはダミー一式)の読み込みが必要。

    Returns:
        build_context と同じキー構成の context dict(config_only=True、
        データ由来の項目は None / 空、カタログの値候補はすべて None)。

    """
    group_names = set(score_file.get("groupDefs") or {})
    catalogs: dict[str, dict[str, list | None]] = {}
    mlabels: dict[str, dict[int, str]] = {}
    for part in score_file.get("score_parts", []):
        type_ = part.get("type")
        if not type_ or type_ == "custom":
            continue
        cat = catalogs.setdefault(type_, {})
        for axis in sorted(_part_axis_names(part)):
            if axis not in group_names and not axis.startswith("__"):
                cat.setdefault(axis, None)
        mlabels.setdefault(type_, {}).update(_config_measure_labels(part))
    part_types = list(catalogs)
    return {
        "config_only": True,
        "data_dir": None,
        "types": part_types,
        "part_types": part_types,
        "catalogs": catalogs,
        "measure_labels": mlabels,
        "coef_path": None,
        "coef_source": None,
        "config_path": None,
        "config_source": None,
        "has_initial_temperature": False,
        "generation": generation,
        "wlgroup": {},
        "wlgroup_defin_logical": True,
        "wlgroup_weight": None,
        "existing_score_file": None,
        "geninfo": None,
        "geninfo_path": None,
        "geninfo_source": None,
        "custom_path": None,
        "custom_source": None,
        "custom_functions": [],
    }


def load_config_only(text: str) -> tuple:
    """設定 jsonc(score.jsonc または RunConfig 形式)だけから編集を開始する。

    (score_file, config_only_context) を返す。RunConfig 形式なら旧来の
    optimization.WLgroup も編集可能なグループ定義として取り込む
    (データ読み込み経路の import_config_group_defs と同じ扱い)。

    Returns:
        (編集用の score_file dict, config_only_context の context dict)
        のタプル。

    """
    from scorelib_param.models import RunConfig

    sf = import_score_file(text)
    generation = None
    raw = jsonc.loads(text)
    if isinstance(raw, dict) and "optimization" in raw:
        rc = RunConfig.model_validate(raw)  # import_score_file で検証済み = 成功する
        generation = rc.Generation or None
        import_config_group_defs(
            sf,
            dict(rc.optimization.WLgroup),
            defin_logical=rc.optimization.WLgroupDefinLogical,
            wlgroup_weight=rc.optimization.WLgroupWeight,
        )
    return sf, config_only_context(sf, generation)


def is_run_config_text(text: str) -> bool:
    """設定テキストが RunConfig 形式(トップに optimization{})かどうか。

    画面1の①でアップロードされた設定を build_context の config_path として
    渡せるか(Generation / WLgroup 等を持つか)の判定に使う。

    Returns:
        RunConfig 形式なら True。パース不能・別形式なら False。

    """
    try:
        raw = jsonc.loads(text)
    except Exception:
        return False
    return isinstance(raw, dict) and "optimization" in raw


def import_score_file(text: str) -> dict[str, Any]:
    """エクスポートされた設定テキストを編集用 dict にパースする。

    score.jsonc(ScoreFile 形)とフル run config
    (Generation + optimization{...})の両方を受け付ける。

    Returns:
        ScoreFile 形式に正規化した編集用 dict(None のフィールドは除く)。

    Raises:
        TypeError: トップレベルがオブジェクトでないとき。
        ValueError: pydantic の検証エラーのとき(整形済みメッセージ入り)。

    """
    data = jsonc.loads(text)
    if not isinstance(data, dict):
        msg = "jsoncのトップレベルがオブジェクトではありません"
        raise TypeError(msg)
    try:
        if "optimization" in data:
            from scorelib_param.models import RunConfig

            sf = RunConfig.model_validate(data).to_score_file()
        else:
            sf = ScoreFile.model_validate(data)
    except ValidationError as err:
        raise ValueError("\n".join(_format_pydantic_error(err, data))) from err
    return sf.model_dump(exclude_none=True)
