"""スコア設計UIの編集ロジック（純関数層）。

ここにあるものはすべて streamlit 非依存で pytest 可能
（docs/score_gui_ui_design.md 2節）。編集中の ScoreFile は素の dict
（scorelib_param.models.ScoreFile の形）として保持し、pydantic は検証にのみ使う。
これにより UI はエンジンが読み込み時に出すのと全く同じメッセージを表示する。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from scorelib_param import custom as scorelib_custom
from scorelib_param import introspect, io_jsonc, jsonc
from scorelib_param.expression import evaluate_expression
from scorelib_param.models import COMBINED_SEP, TRANSFORM_OPS, ScoreFile

DRAFT_PATH = Path.home() / ".scorelib_draft.jsonc"

# 新規パーツの type に Read_Override 軸があるときのプリセット
# （docs/score_gui_ui_design.md 画面2: 相対化はデフォルトON）
DEFAULT_RELATIVE = {
    "split_axis": "Read_Override",
    "numerator_when": True,
    "denominator_when": False,
    "mode": "ratio",
    "denominator_offset": 1,
}

# InBatchEpoch は集計対象にしない（ユーザ判断。設計書5節）。order に
# 入れないことで、エンジンは周囲の軸と一緒に暗黙に集約する
_EXCLUDED_AXES = {"InBatchEpoch"}
_LAST_AXES = ["Board", "Chip", "Block"]


# ------------------------------------------------------------ スコアファイル

def empty_score_file() -> Dict[str, Any]:
    return {
        "score_parts": [],
        "expression": "",
        "constraintThreshold": {},
        "selectionSets": {},
        "groupDefs": {},
        "weightSets": {},
    }


def ensure_uids(score_file: Dict[str, Any]) -> None:
    """各パーツにウィジェットキー用の安定した内部ID（_uid）を付ける
    （添字ベースのキーだと削除後に別パーツへ状態が漏れる）。pydantic は
    未知フィールドを無視するので検証・エクスポートには影響しない。

    重複IDは振り直す: IDを共有した2パーツは全ウィジェット（名前欄・相対化
    チェック等）を共有してしまい、片方の編集がもう片方を静かに書き換える。
    そのバグがあった期間に保存された下書きも、これで開くだけで治る。"""
    seen: set = set()
    for p in score_file["score_parts"]:
        uid = p.get("_uid")
        if not uid or uid in seen:
            uid = uuid.uuid4().hex[:8]
            p["_uid"] = uid
        seen.add(uid)


def part_names(score_file: Dict[str, Any]) -> List[str]:
    return [p.get("name", "") for p in score_file["score_parts"]]


def unique_part_name(score_file: Dict[str, Any], base: str = "part") -> str:
    names = set(part_names(score_file))
    i = 1
    while f"{base}_{i}" in names:
        i += 1
    return f"{base}_{i}"


# ------------------------------------------------------------------ 雛形生成

def default_axis_order(catalog: Dict[str, Optional[list]], exclude: set[str] = frozenset()) -> List[str]:
    """雛形の軸順: Label系 → Override系 → その他カテゴリ（State, Page, ...）
    → 数値（WL, STR, ...）→ Board, Chip, Block。"""
    skip = _EXCLUDED_AXES | set(exclude)
    axes = [a for a in catalog if a not in skip]

    def bucket(a: str) -> int:
        if a in _LAST_AXES:
            return 4
        if a.endswith("_Label"):
            return 0
        if a.endswith("_Override"):
            return 1
        cands = catalog.get(a)
        if cands and isinstance(cands[0], str):
            return 2
        return 3

    ordered = sorted(axes, key=lambda a: bucket(a))  # 安定ソート: 同バケツ内は csv 順のまま
    return [a for a in ordered if a not in _LAST_AXES] + [a for a in _LAST_AXES if a in axes]


def default_aggregation(axis: str, candidates: Optional[list]) -> Dict[str, Any]:
    """カテゴリ/bool軸は先頭候補の filter から始める（意味があり、かつ必ず
    計算が通る）。数値・自由入力軸は mean から。"""
    if candidates and isinstance(candidates[0], (str, bool)):
        return {"op": "filter", "value": candidates[0]}
    return {"op": "mean"}


def part_skeleton(name: str, type_: str, catalog: Dict[str, Optional[list]]) -> Dict[str, Any]:
    """**そのまま計算が通る**新規パーツ: 全軸をデフォルトopつきで `order` に
    並べ、Read_Override 軸があれば相対化をプリセットON（split 軸は相対化が
    消費するので `order` には入れない）。"""
    relative = dict(DEFAULT_RELATIVE) if "Read_Override" in catalog else None
    exclude = {relative["split_axis"]} if relative else set()
    order = default_axis_order(catalog, exclude)
    aggregations = {a: default_aggregation(a, catalog.get(a)) for a in order}
    part: Dict[str, Any] = {"name": name, "type": type_, "order": order, "aggregations": aggregations}
    if relative:
        part["relative"] = relative
    return part


def custom_part_skeleton(name: str, functions: List[str]) -> Dict[str, Any]:
    """type="custom" の新規パーツ: パイプライン系フィールドは持たず、
    呼ぶ関数（先頭の候補）と空の params だけ。"""
    return {
        "name": name,
        "type": "custom",
        "function": functions[0] if functions else name,
        "params": {},
    }


def switch_part_type(part: Dict[str, Any], new_type: str) -> Optional[str]:
    """type 変更に合わせてフィールドを整える。ユーザ向けの通知文字列
    （不要なら None）を返す。custom とパイプライン系フィールドの混在は
    エンジンが拒否するので、切替時点で UI 側が外す。"""
    part["type"] = new_type
    if new_type == "custom":
        dropped = [part.pop(k, None) for k in ("relative", "order", "aggregations")]
        part.setdefault("function", part.get("name"))
        part.setdefault("params", {})
        return "集計設定を外しました（custom パーツは関数が値を返します）" if any(dropped) else None
    part.pop("function", None)
    part.pop("params", None)
    part.setdefault("order", [])
    part.setdefault("aggregations", {})
    removed = drop_stale_virtual_steps(part)
    return f"{removed} を order から外しました（type が dVtBudget ではないため）" if removed else None


def _axes_in_order(part: Dict[str, Any]) -> set:
    used: set = set()
    for e in part.get("order", []):
        if not e.startswith("__"):
            used.update(e.split(COMBINED_SEP))
    return used


def _remove_axis_from_order(part: Dict[str, Any], axis: Optional[str]) -> None:
    if axis and axis in part.get("order", []):
        part["order"].remove(axis)
        part["aggregations"].pop(axis, None)


def _restore_axis_to_order(part: Dict[str, Any], axis: Optional[str], catalog: Dict[str, Optional[list]]) -> bool:
    """軸を（デフォルトopつきで）`order` に戻す。既にカバーされていれば
    何もしない。相対化OFF時に必要: エンジンは `order` に無い軸を黙って
    集約するため、放置すると分子と分母の行が混ざってしまう。"""
    if not axis or axis not in catalog or axis in _axes_in_order(part):
        return False
    part["order"].append(axis)
    part["aggregations"][axis] = default_aggregation(axis, catalog.get(axis))
    return True


def enable_relative(part: Dict[str, Any], catalog: Dict[str, Optional[list]]) -> None:
    overrides = [a for a in catalog if a.endswith("_Override")]
    split = "Read_Override" if "Read_Override" in catalog else (overrides[0] if overrides else DEFAULT_RELATIVE["split_axis"])
    rel = dict(DEFAULT_RELATIVE)
    rel["split_axis"] = split
    part["relative"] = rel
    _remove_axis_from_order(part, split)


def disable_relative(part: Dict[str, Any], catalog: Dict[str, Optional[list]]) -> Optional[str]:
    """相対化をOFFにする。split 軸を `order` に復帰させた場合はその軸名を
    返す（UIがユーザに知らせるため）。明示配置された __relative__ ステップも
    除去する — 相対化設定なしで残ると検証エラーになるため。"""
    rel = part.pop("relative", None)
    if not rel:
        return None
    if "__relative__" in part.get("order", []):
        part["order"].remove("__relative__")
        part["aggregations"].pop("__relative__", None)
    axis = rel.get("split_axis")
    return axis if _restore_axis_to_order(part, axis, catalog) else None


def drop_stale_virtual_steps(part: Dict[str, Any]) -> Optional[str]:
    """type を dVtBudget から他へ変えた後、明示配置の __dvtbudget__ が残ると
    検証エラーになるので除去する。除去したステップ名（UI通知用）か None を返す。"""
    if part.get("type") != "dVtBudget" and "__dvtbudget__" in part.get("order", []):
        part["order"].remove("__dvtbudget__")
        part["aggregations"].pop("__dvtbudget__", None)
        return "__dvtbudget__"
    return None


def change_split_axis(part: Dict[str, Any], new_axis: str, catalog: Dict[str, Optional[list]]) -> None:
    """split 軸の変更: 旧軸を `order` へ戻し、新軸を `order` から外す。"""
    rel = part.get("relative")
    if rel is None or rel.get("split_axis") == new_axis:
        return
    old = rel.get("split_axis")
    rel["split_axis"] = new_axis
    _restore_axis_to_order(part, old, catalog)
    _remove_axis_from_order(part, new_axis)


def duplicate_part(score_file: Dict[str, Any], index: int) -> int:
    src = score_file["score_parts"][index]
    copy = json.loads(json.dumps(src))
    # コピーには必ず新しいウィジェットキー用IDを割り当てる: 共有すると
    # 2パーツが全ウィジェットの記憶状態を共有してしまう
    copy.pop("_uid", None)
    copy["name"] = unique_part_name(score_file, base=src.get("name", "part"))
    score_file["score_parts"].append(copy)
    return len(score_file["score_parts"]) - 1


def move_entry(lst: list, index: int, delta: int) -> int:
    """lst[index] を隣と入れ替える。新しい添字を返す（端では何もしない）。"""
    j = index + delta
    if 0 <= j < len(lst):
        lst[index], lst[j] = lst[j], lst[index]
        return j
    return index


# ------------------------------------------------------------- グループ定義

def import_config_group_defs(
    score_file: Dict[str, Any],
    wlgroup: Optional[Dict[str, Any]],
    defin_logical: bool = True,
    wlgroup_weight: Optional[Any] = None,
) -> bool:
    """設定jsoncの WLgroup（+ WLgroupDefinLogical / WLgroupWeight）を編集可能な
    定義として取り込む。score file は自己完結（エンジンが使うのは score file
    側の groupDefs / weightSets）で、config の値はあくまで初期テンプレート。
    追加したら True を返す。"""
    weights = score_file.setdefault("weightSets", {})
    if wlgroup_weight is not None and "WLgroupWeight" not in weights:
        weights["WLgroupWeight"] = (
            dict(wlgroup_weight) if isinstance(wlgroup_weight, dict) else wlgroup_weight
        )
    defs = score_file.setdefault("groupDefs", {})
    if not wlgroup or "WLgroup" in defs:
        return False
    defs["WLgroup"] = {
        "axis": "WL",
        "groups": {k: list(v) for k, v in wlgroup.items()},
        "definedInLogical": bool(defin_logical),
    }
    return True


def _part_axis_names(part: Dict[str, Any]) -> set:
    """パーツが言及する軸的な名前すべて（order エントリ=複合軸の構成軸込み、
    相対化の split 軸、分母事前集計の軸）。

    scorelib_param/cli.py の _named_axes は計算時に使う pydantic モデル版の対。
    こちらは編集途中の不完全な dict に耐える — 意図的な並行実装であり、
    統合を試みないこと。"""
    axes = set(_axes_in_order(part))
    for s in _part_specs(part):
        if isinstance(s, dict) and s.get("by"):
            axes.add(s["by"])  # 変換ステップの重みが参照する軸（cli._named_axes と対）
    rel = part.get("relative") or {}
    if rel.get("split_axis"):
        axes.add(rel["split_axis"])
    axes.update(s.get("axis") for s in rel.get("denominator_pre_aggregation", []) if s.get("axis"))
    return axes


def parts_referencing_group_def(score_file: Dict[str, Any], name: str) -> List[str]:
    return [
        p.get("name", "?")
        for p in score_file["score_parts"]
        if name in _part_axis_names(p)
    ]


def add_group_def(score_file: Dict[str, Any], name: str, axis: str, axis_names: set) -> None:
    name = (name or "").strip()
    if not name:
        raise ValueError("グループ定義名を入力してください")
    if COMBINED_SEP in name or name.startswith("__"):
        raise ValueError(f"グループ定義名に '{COMBINED_SEP}' や先頭の '__' は使えません")
    defs = score_file.setdefault("groupDefs", {})
    if name in defs:
        raise ValueError(f"グループ定義 '{name}' は既に存在します")
    if name in axis_names:
        raise ValueError(f"'{name}' は軸名と衝突しています（別の名前にしてください）")
    defs[name] = {"axis": axis, "groups": {}}


def delete_group_def(score_file: Dict[str, Any], name: str) -> None:
    users = parts_referencing_group_def(score_file, name)
    if users:
        raise ValueError(f"グループ定義 '{name}' はパーツ {users} から参照されているため削除できません")
    score_file.get("groupDefs", {}).pop(name, None)


# --------------------------------------------------------------- 選択セット

def _part_specs(part: Dict[str, Any]) -> List[Any]:
    """パーツが持つ全集計spec（分母事前集計のステップ込み。ref の走査用）。"""
    specs = list(part.get("aggregations", {}).values())
    specs += (part.get("relative") or {}).get("denominator_pre_aggregation", [])
    return specs


def referencing_parts(score_file: Dict[str, Any], set_name: str) -> List[str]:
    """指定の選択セットを（分母事前集計も含めて）参照しているパーツ名の一覧。"""
    return [
        part.get("name", "?")
        for part in score_file["score_parts"]
        if any(isinstance(s, dict) and s.get("ref") == set_name for s in _part_specs(part))
    ]


def delete_selection_set(score_file: Dict[str, Any], name: str) -> None:
    users = referencing_parts(score_file, name)
    if users:
        raise ValueError(f"選択セット '{name}' はパーツ {users} から参照されているため削除できません")
    score_file["selectionSets"].pop(name, None)


def save_set_as(score_file: Dict[str, Any], src_name: str, new_name: str) -> None:
    """別名で保存: セットを新しい名前でコピーする。既存の ref は元の名前を
    指したまま変わらない。"""
    if not new_name:
        raise ValueError("新しいセット名を入力してください")
    if new_name in score_file["selectionSets"]:
        raise ValueError(f"選択セット '{new_name}' は既に存在します")
    score_file["selectionSets"][new_name] = json.loads(json.dumps(score_file["selectionSets"][src_name]))


# ---------------------------------------------------------------------- 検証

def _format_pydantic_error(err: ValidationError, data: Optional[Dict[str, Any]] = None) -> List[str]:
    """読めるエラーメッセージに整形する。元データ `data` があれば、
    'score_parts.12.aggregations.WL' のような位置表記を
    「パーツ '<名前>' の aggregations.WL」に変換する（添字ではどのパーツが
    壊れているのかユーザに伝わらない）。"""
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
                rest = ".".join(str(x) for x in loc[k + 2:])
            except Exception:
                name = None
        if name:
            msgs.append(f"パーツ '{name}'" + (f" の {rest}" if rest else "") + f": {msg}")
        else:
            joined = ".".join(str(x) for x in loc)
            msgs.append(f"{joined}: {msg}" if joined else msg)
    return msgs


def validate_score_file(data: Dict[str, Any]) -> List[str]:
    """エンジンが読み込み時に報告する問題すべてを、読める文字列のリストで
    返す（空リスト = OK）。pydantic だけでは見ない expression の
    パース/参照チェックとパーツ名の重複も含む。"""
    try:
        sf = ScoreFile.model_validate(data)
    except ValidationError as err:
        return _format_pydantic_error(err, data)

    problems: List[str] = []
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


def _expression_problems(expression: str, names: List[str]) -> List[str]:
    """全パーツ値を 1.0 のダミーにして式を評価してみる（構文と参照の検査）。"""
    try:
        evaluate_expression(expression, {n: 1.0 for n in names})
    except Exception as err:
        return [f"expression: {err}"]
    return []


def validate_part(
    part: Dict[str, Any],
    selection_sets: Optional[Dict[str, list]] = None,
    weight_sets: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """編集中のパーツ1つぶんの検証。"""
    single = {
        "score_parts": [part],
        "expression": "",
        "constraintThreshold": {},
        "selectionSets": selection_sets or {},
        "weightSets": weight_sets or {},
    }
    return validate_score_file(single)


# -------------------------------------------------- コンテキスト（画面1）

def _resolve_optional_file(explicit: Optional[str], discover, label: str):
    """任意の同梱ファイルの解決: 明示指定が最優先（存在必須）、無ければ
    データディレクトリ内の自動検出にフォールバック。`discover` は候補の
    **リスト**を返し、2件以上はエラー（アルファベット順の先頭を黙って
    採用すると、意図と違うファイルで設計してしまう）。
    (パス or None, '指定' | '自動検出' | None) を返す。"""
    if explicit and str(explicit).strip():
        p = Path(str(explicit).strip()).resolve()
        if not p.is_file():
            raise ValueError(f"{label} が見つかりません: {p}")
        return p, "指定"
    found = list(discover())
    if len(found) > 1:
        raise ValueError(
            f"{label} の候補が複数見つかりました（自動検出できません。パスを明示指定してください）: "
            + ", ".join(str(p) for p in found)
        )
    return (found[0], "自動検出") if found else (None, None)


def build_context(
    data_dir: str,
    config_path: Optional[str] = None,
    coef_path: Optional[str] = None,
    geninfo_path: Optional[str] = None,
    custom_path: Optional[str] = None,
) -> Dict[str, Any]:
    """画面1が測定結果ディレクトリ（同系統の過去実験の出力 — 設計書 5.1節）
    から導出するもの一式。

    result_tmp には通常測定結果しか入らないため、optimization設定jsonc と
    dVtBudget係数jsonc 等は別の（任意の）パスとして受け取る。たまたま
    ディレクトリ内に置いてある場合のための自動検出も残してある。"""
    if not str(data_dir).strip():
        # Path("") はカレントディレクトリ扱いになり、起動場所を誤って走査してしまう
        raise ValueError("測定結果ディレクトリのパスを入力してください")
    d = Path(str(data_dir).strip()).resolve()
    if not d.is_dir():
        raise ValueError(f"ディレクトリが見つかりません: {d}")

    types = introspect.detect_types(d)

    coef_file, coef_source = _resolve_optional_file(
        coef_path, lambda: introspect.find_dvtbudget_coefs(d), "dVtBudget係数jsonc"
    )
    if coef_file is not None:
        try:
            io_jsonc.load_dvtbudget_coef(coef_file)
        except Exception as err:
            raise ValueError(f"dVtBudget係数jsoncを読み込めません ({coef_file}): {err}")

    config_file, config_source = _resolve_optional_file(
        config_path, lambda: introspect.find_run_configs(d), "optimization設定jsonc"
    )

    part_types = list(types)
    if "FBC" in types and coef_file is not None:
        part_types.append("dVtBudget")
    catalogs = {t: introspect.axis_catalog(d, t) for t in part_types}

    ctx: Dict[str, Any] = {
        "data_dir": str(d),
        "types": types,
        "part_types": part_types,
        "catalogs": catalogs,
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
        try:
            run_config = io_jsonc.load_run_config(config_file)
        except ValidationError as err:
            # パーツ名つきの読めるメッセージにするため、生の dict を読み直して整形する
            try:
                raw = jsonc.loads(Path(config_file).read_text(encoding="utf-8"))
            except Exception:
                raw = None
            details = "; ".join(_format_pydantic_error(err, raw if isinstance(raw, dict) else None))
            raise ValueError(f"optimization設定jsoncを読み込めません ({config_file}): {details}")
        except Exception as err:
            raise ValueError(f"optimization設定jsoncを読み込めません ({config_file}): {err}")
        ctx["generation"] = run_config.Generation
        ctx["wlgroup"] = dict(run_config.optimization.WLgroup)
        ctx["wlgroup_defin_logical"] = run_config.optimization.WLgroupDefinLogical
        ctx["wlgroup_weight"] = run_config.optimization.WLgroupWeight
        existing = run_config.to_score_file()
        if existing.score_parts or existing.selectionSets or existing.expression:
            ctx["existing_score_file"] = existing.model_dump(exclude_none=True)

    geninfo_file, geninfo_source = _resolve_optional_file(
        geninfo_path,
        lambda: [p for p in [introspect.find_generation_info(d, ctx["generation"])] if p],
        "世代情報json",
    )
    if geninfo_file is not None:
        try:
            geninfo = jsonc.loads(Path(geninfo_file).read_text(encoding="utf-8"))
            if not isinstance(geninfo, dict):
                raise ValueError("トップレベルがオブジェクトではありません")
        except Exception as err:
            raise ValueError(f"世代情報jsonを読み込めません ({geninfo_file}): {err}")
        ctx["geninfo"] = geninfo
        ctx["geninfo_path"] = str(geninfo_file)
        ctx["geninfo_source"] = geninfo_source

    # custom_parts.py: SVN 管理された自作関数（scorelib_param/custom.py）。
    # 読み込み = import = トップレベルコードの実行になるが、レビュー済み
    # リポジトリ由来のファイルが前提（任意ユーザの入力ではない）ので許容
    custom_file, custom_source = _resolve_optional_file(
        custom_path,
        lambda: [p for p in [d / scorelib_custom.DEFAULT_FILENAME] if p.is_file()],
        "自作関数ファイル",
    )
    if custom_file is not None:
        try:
            module = scorelib_custom.load_custom_module(custom_file)
            functions = scorelib_custom.list_custom_functions(module)
        except Exception as err:
            raise ValueError(f"自作関数ファイルを読み込めません ({custom_file}): {err}")
        ctx["custom_path"] = str(custom_file)
        ctx["custom_source"] = custom_source
        ctx["custom_functions"] = functions
        # `catalogs` を作った後に足す: custom 擬似typeには軸カタログが無い
        ctx["part_types"] = ctx["part_types"] + ["custom"]
    return ctx


def extract_bundle_zip(data: bytes) -> str:
    """アップロードされた一式zip（result_tmp のファイル群 + 設定jsonc +
    係数jsonc + 世代情報json + custom_parts.py）を一時ディレクトリへ展開し、
    そのディレクトリを返す（後段は locate_bundle_inputs / build_context）。
    フォルダごと圧縮された zip によくある「トップに1フォルダ」の場合は
    その中へ降りる。"""
    import io
    import tempfile
    import zipfile

    target = Path(tempfile.mkdtemp(prefix="scorelib_bundle_"))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for m in z.infolist():
            parts = Path(m.filename).parts
            if not parts or m.filename.startswith(("/", "\\")) or ".." in parts:
                continue  # zip-slip（絶対パス・.. による展開先脱出）対策
            z.extract(m, target)
    entries = list(target.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return str(entries[0])
    return str(target)


def _walk_dirs(root: Path, max_depth: int = 4) -> List[Path]:
    out = [root]
    if max_depth <= 0:
        return out
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        out += _walk_dirs(child, max_depth - 1)
    return out


def locate_bundle_inputs(extracted_dir: str) -> Dict[str, Optional[str]]:
    """展開済み一式zipの中から測定ディレクトリと同梱ファイルを探す（深さ制限
    つきのツリー探索）。判別はディレクトリ内自動検出と同じ**中身ベース**:
    測定ディレクトリ = type が検出できるディレクトリ（parameterLabel_*/
    dataName_*/Measure列つきcsv）、設定 = optimization{} ブロックを持つ jsonc、
    係数 = 3段 {a, b} 表として検証が通る jsonc、世代情報 = {Generation}.json
    （Generation は設定から読む）、custom = custom_parts.py。
    同じ役割の候補が2つ以上あったらエラー — 黙って1つ選ぶと意図と違う
    ファイルで設計してしまう。"""
    root = Path(extracted_dir)
    dirs = _walk_dirs(root)

    def _at_most_one(paths: List[Path], label: str) -> Optional[str]:
        if len(paths) > 1:
            raise ValueError(
                f"zip内に{label}の候補が複数あります（どれを使うか判断できません）: "
                + ", ".join(str(p) for p in paths)
            )
        return str(paths[0]) if paths else None

    data_dirs = [d for d in dirs if introspect.detect_types(d)]
    if not data_dirs:
        raise ValueError(
            f"zip内に測定結果（parameterLabel_*/dataName_* 等のあるディレクトリ）が見つかりません: {root}"
        )
    data_dir = _at_most_one(data_dirs, "測定結果ディレクトリ")

    config_path = _at_most_one(
        [p for d in dirs for p in introspect.find_run_configs(d)], "optimization設定jsonc"
    )
    coef_path = _at_most_one(
        [p for d in dirs for p in introspect.find_dvtbudget_coefs(d)], "dVtBudget係数jsonc"
    )
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


def axis_counts(geninfo: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """世代情報json（B9LS.json の形）から 軸 → 本数 の対応を取り出す。
    ファイルに記述のある軸だけ。"""
    counts: Dict[str, int] = {}
    if isinstance(geninfo, dict):
        if isinstance(geninfo.get("numWLs"), int):
            counts["WL"] = geninfo["numWLs"]
        if isinstance(geninfo.get("numStrings"), int):
            counts["STR"] = geninfo["numStrings"]
    return counts


def _format_value_runs(values: List[int]) -> str:
    """[0,1,2,5] → '0–2, 5'（未カバー値一覧の圧縮表示）。"""
    runs: List[str] = []
    start = prev = values[0]
    for v in values[1:] + [None]:  # type: ignore[list-item]
        if v is not None and v == prev + 1:
            prev = v
            continue
        runs.append(str(start) if start == prev else f"{start}–{prev}")
        if v is not None:
            start = prev = v
    return ", ".join(runs)


def group_def_warnings(score_file: Dict[str, Any], geninfo: Optional[Dict[str, Any]]) -> List[str]:
    """グループ定義と世代の軸本数（numWLs / numStrings）の不整合を列挙する。
    エラーではなく警告: 計算自体は通るが、チップの WL/STR 本数と食い違う
    範囲はほぼ確実に設定ミス（joint WL をグループから除外する運用は基本
    無い、と担当者確認済みなので全値をチェックする）。"""
    counts = axis_counts(geninfo)
    warnings: List[str] = []
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
                out_of_range.append(f"{label}({lo}–{hi})")
        if out_of_range:
            warnings.append(
                f"グループ定義 '{name}': {axis} は {n} 本（0–{n - 1}）ですが、"
                f"範囲外を含むグループがあります: {', '.join(out_of_range)}"
            )
        missing = [v for v in range(n) if v not in covered]
        if missing:
            warnings.append(
                f"グループ定義 '{name}': {axis} の {_format_value_runs(missing)} が"
                f"どのグループにも入りません"
            )
    return warnings


# -------------------------------------------------------------- 表示ラベル
# 純関数にしてある理由: D&D コンポーネントの描画内容は AppTest から観測でき
# ないため、マーカー等のロジックはここで単体テストできる形に切り出しておく

def part_list_labels(
    score_file: Dict[str, Any],
    selected_uid: Optional[str],
    invalid_uids: set,
    rows: Optional[List[Dict[str, str]]] = None,
) -> List[str]:
    """常時ドラッグ可能なパーツ一覧のラベル: ⠿ ドラッグハンドル、検証NGの
    パーツに ⚠、選択中のパーツに ← 編集中。`rows`（part_summary_rows の
    出力）は呼び出し元が計算済みなら渡せる。"""
    rows = rows if rows is not None else part_summary_rows(score_file)
    labels = []
    for i, (row, p) in enumerate(zip(rows, score_file["score_parts"])):
        labels.append(
            "⠿ "
            + ("⚠ " if p.get("_uid") in invalid_uids else "")
            + f"{i + 1}. {row['名前']}（{row['type']}, 相対化{row['相対化']}）"
            + (" ← 編集中" if p.get("_uid") == selected_uid else "")
        )
    return labels


def part_select_labels(score_file: Dict[str, Any], invalid_uids: set) -> Dict[str, str]:
    """パーツ選択プルダウンの uid → 表示ラベル。番号付きにして、2パーツが
    （一時的にでも）同名になってもラベルが一意になるようにする: Streamlit の
    selectbox フロントエンドは表示ラベルで項目を照合するため、重複ラベルが
    あるとクリックが別パーツに解決されたり無反応になったりする。番号は
    ⠿ 一覧とも揃う。"""
    return {
        p.get("_uid"): (
            f"{i + 1}. "
            + ("⚠ " if p.get("_uid") in invalid_uids else "")
            + p.get("name", "")
        )
        for i, p in enumerate(score_file["score_parts"])
    }


def part_summary_rows(score_file: Dict[str, Any]) -> List[Dict[str, str]]:
    """一覧表の行（名前 / type / 相対化 / 軸。custom パーツは関数名を表示）。"""
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

def score_file_to_jsonc(score_file: Dict[str, Any]) -> str:
    cleaned = ScoreFile.model_validate(score_file).model_dump(exclude_none=True)
    return json.dumps(cleaned, indent=2, ensure_ascii=False) + "\n"


def save_draft(
    score_file: Dict[str, Any],
    context_inputs: Optional[Dict[str, Optional[str]]] = None,
    path: Optional[Path] = None,
) -> None:
    """下書きには score file と一緒に画面1の入力（data_dir / config_path /
    coef_path / ...）も保存する。復元時にデータ読み込みまで再現でき、
    ユーザを画面1からやり直させずに済む。"""
    path = path or DRAFT_PATH  # テストが DRAFT_PATH を差し替えられるよう呼び出し時に解決
    payload = {"score_file": score_file, "context_inputs": context_inputs or {}}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_draft(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """{"score_file": ..., "context_inputs": {...}} か None を返す。
    context_inputs 導入前の旧形式（素の ScoreFile dict）も受け付ける。"""
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


def _part_refs(part: Dict[str, Any]) -> List[str]:
    """パーツが参照している選択セット名（エクスポート同梱用）。
    変換op（TRANSFORM_OPS）の ref は重みセット参照なので含めない。"""
    return sorted(
        {
            s["ref"]
            for s in _part_specs(part)
            if isinstance(s, dict) and s.get("ref") and s.get("op") not in TRANSFORM_OPS
        }
    )


def _part_weight_refs(part: Dict[str, Any]) -> List[str]:
    """パーツが参照している重みセット名（変換opの ref）。"""
    return sorted(
        {
            s["ref"]
            for s in _part_specs(part)
            if isinstance(s, dict) and s.get("ref") and s.get("op") in TRANSFORM_OPS
        }
    )


def export_part(score_file: Dict[str, Any], index: int) -> str:
    """パーツ1つを自己完結の score.jsonc としてエクスポートする（参照する
    選択セット・重みセットとグループ定義を同梱）。別の場所で再インポートできる。"""
    part = score_file["score_parts"][index]
    refs = _part_refs(part)
    missing = [r for r in refs if r not in score_file["selectionSets"]]
    if missing:
        raise ValueError(f"パーツ '{part.get('name')}' が参照する選択セットが未定義です: {missing}")
    weight_refs = _part_weight_refs(part)
    all_weights = score_file.get("weightSets", {})
    missing_w = [r for r in weight_refs if r not in all_weights]
    if missing_w:
        raise ValueError(f"パーツ '{part.get('name')}' が参照する重みセットが未定義です: {missing_w}")
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


def run_test_compute(
    score_file: Dict[str, Any],
    data_dir: str,
    generation: Optional[str] = None,
    wlgroup: Optional[Dict[str, Any]] = None,
    coef_path: Optional[str] = None,
    custom_path: Optional[str] = None,
    geninfo_path: Optional[str] = None,
) -> Dict[str, float]:
    """画面5: 実データでエンジンを走らせる。係数ファイルは指定があれば
    `coef_path` から（通常 result_tmp の外にある）。initial_temperature.csv
    は測定出力なので常にデータディレクトリから読む。"""
    from scorelib_param.cli import compute_score_file
    from scorelib_param.dvtbudget import load_board_temperatures
    from scorelib_param.models import RunConfig

    d = Path(data_dir)
    if not d.is_dir():
        raise ValueError(f"ディレクトリが見つかりません: {d}")
    sf = ScoreFile.model_validate(score_file)
    dump = sf.model_dump(exclude_none=True)
    run_config = RunConfig.model_validate(
        {
            "Generation": generation or "",
            "optimization": {
                "score_parts": dump["score_parts"],
                "expression": sf.expression,
                "constraintThreshold": {},  # 閾値はパーツの値に影響しない
                "selectionSets": sf.selectionSets,
                # score file 側の定義が config の WLgroup より優先される
                "WLgroup": wlgroup or {},
                "groupDefs": dump.get("groupDefs", {}),
                "weightSets": dump.get("weightSets", {}),
            },
        }
    )
    coef_file, _ = _resolve_optional_file(
        coef_path, lambda: introspect.find_dvtbudget_coefs(d), "dVtBudget係数jsonc"
    )
    coef = io_jsonc.load_dvtbudget_coef(coef_file) if coef_file else None
    temp_csv = d / "initial_temperature.csv"
    temps = load_board_temperatures(temp_csv) if temp_csv.exists() else None
    return compute_score_file(
        d, run_config, coef, temps, custom_parts_path=custom_path,
        generation_info_path=geninfo_path,
    )


def import_score_file(text: str) -> Dict[str, Any]:
    """エクスポートされた score.jsonc（ScoreFile 形）またはフル run config
    （Generation + optimization{...}）を編集用 dict にパースする。"""
    data = jsonc.loads(text)
    if not isinstance(data, dict):
        raise ValueError("jsoncのトップレベルがオブジェクトではありません")
    try:
        if "optimization" in data:
            from scorelib_param.models import RunConfig

            sf = RunConfig.model_validate(data).to_score_file()
        else:
            sf = ScoreFile.model_validate(data)
    except ValidationError as err:
        raise ValueError("\n".join(_format_pydantic_error(err, data)))
    return sf.model_dump(exclude_none=True)
