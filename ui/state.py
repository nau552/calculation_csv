"""Pure editing-state logic for the score-design UI.

Everything here is streamlit-free and pytest-able (score_gui_ui_design.md
section 2). The ScoreFile being edited is held as a plain dict (the shape
of scorelib.models.ScoreFile); pydantic is used for validation only, so the
UI shows exactly the messages the engine would produce at load time.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from scorelib import introspect, io_jsonc, jsonc
from scorelib.expression import evaluate_expression
from scorelib.models import COMBINED_SEP, ScoreFile

DRAFT_PATH = Path.home() / ".scorelib_draft.jsonc"

# Preset used when a new part's type has a Read_Override axis
# (score_gui_ui_design.md screen 2: relative defaults to ON).
DEFAULT_RELATIVE = {
    "split_axis": "Read_Override",
    "numerator_when": True,
    "denominator_when": False,
    "mode": "ratio",
    "denominator_offset": 1,
}

# InBatchEpoch is never an aggregation target (user decision, design doc
# section 5); leaving it out of `order` makes the engine aggregate over it
# implicitly together with the surrounding axes.
_EXCLUDED_AXES = {"InBatchEpoch"}
_LAST_AXES = ["Board", "Chip", "Block"]


# ---------------------------------------------------------------- score file

def empty_score_file() -> Dict[str, Any]:
    return {"score_parts": [], "expression": "", "constraintThreshold": {}, "selectionSets": {}}


def ensure_uids(score_file: Dict[str, Any]) -> None:
    """Give each part a stable internal id used for widget keys (index-based
    keys would leak state across parts after a delete). pydantic ignores the
    extra field, so validation and export are unaffected."""
    for p in score_file["score_parts"]:
        p.setdefault("_uid", uuid.uuid4().hex[:8])


def part_names(score_file: Dict[str, Any]) -> List[str]:
    return [p.get("name", "") for p in score_file["score_parts"]]


def unique_part_name(score_file: Dict[str, Any], base: str = "part") -> str:
    names = set(part_names(score_file))
    i = 1
    while f"{base}_{i}" in names:
        i += 1
    return f"{base}_{i}"


# --------------------------------------------------------------- part skeleton

def default_axis_order(catalog: Dict[str, Optional[list]], exclude: set[str] = frozenset()) -> List[str]:
    """Skeleton axis order: Label axes -> Override axes -> other categorical
    (State, Page, ...) -> numeric (WL, STR, ...) -> Board, Chip, Block."""
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

    ordered = sorted(axes, key=lambda a: bucket(a))  # stable: keeps csv order inside buckets
    return [a for a in ordered if a not in _LAST_AXES] + [a for a in _LAST_AXES if a in axes]


def default_aggregation(axis: str, candidates: Optional[list]) -> Dict[str, Any]:
    """Categorical/bool axes start as filter-on-first-candidate (meaningful and
    always computable); numeric/free axes start as mean."""
    if candidates and isinstance(candidates[0], (str, bool)):
        return {"op": "filter", "value": candidates[0]}
    return {"op": "mean"}


def part_skeleton(name: str, type_: str, catalog: Dict[str, Optional[list]]) -> Dict[str, Any]:
    """A new part that is computable as-is: every axis is in `order` with a
    default op, relative is preset ON when a Read_Override axis exists
    (its split_axis is consumed by relative, so it stays out of `order`)."""
    relative = dict(DEFAULT_RELATIVE) if "Read_Override" in catalog else None
    exclude = {relative["split_axis"]} if relative else set()
    order = default_axis_order(catalog, exclude)
    aggregations = {a: default_aggregation(a, catalog.get(a)) for a in order}
    part: Dict[str, Any] = {"name": name, "type": type_, "order": order, "aggregations": aggregations}
    if relative:
        part["relative"] = relative
    return part


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
    """Put an axis back into `order` (with a default op) unless it is already
    covered. Needed when relative is turned off: the engine silently
    aggregates over axes missing from `order`, which would mix numerator and
    denominator rows."""
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
    """Turn relative off; returns the split axis if it was restored into
    `order` (so the UI can tell the user)."""
    rel = part.pop("relative", None)
    if not rel:
        return None
    axis = rel.get("split_axis")
    return axis if _restore_axis_to_order(part, axis, catalog) else None


def change_split_axis(part: Dict[str, Any], new_axis: str, catalog: Dict[str, Optional[list]]) -> None:
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
    copy["name"] = unique_part_name(score_file, base=src.get("name", "part"))
    score_file["score_parts"].append(copy)
    return len(score_file["score_parts"]) - 1


def move_entry(lst: list, index: int, delta: int) -> int:
    """Swap lst[index] with its neighbour; returns the new index."""
    j = index + delta
    if 0 <= j < len(lst):
        lst[index], lst[j] = lst[j], lst[index]
        return j
    return index


# ------------------------------------------------------------- selection sets

def referencing_parts(score_file: Dict[str, Any], set_name: str) -> List[str]:
    """Names of parts whose aggregations (incl. denominator_pre_aggregation)
    reference the given selection set."""
    users = []
    for part in score_file["score_parts"]:
        specs = list(part.get("aggregations", {}).values())
        specs += (part.get("relative") or {}).get("denominator_pre_aggregation", [])
        if any(isinstance(s, dict) and s.get("ref") == set_name for s in specs):
            users.append(part.get("name", "?"))
    return users


def delete_selection_set(score_file: Dict[str, Any], name: str) -> None:
    users = referencing_parts(score_file, name)
    if users:
        raise ValueError(f"選択セット '{name}' はパーツ {users} から参照されているため削除できません")
    score_file["selectionSets"].pop(name, None)


def save_set_as(score_file: Dict[str, Any], src_name: str, new_name: str) -> None:
    """別名で保存: copy a set under a new name; existing refs keep pointing
    at the original."""
    if not new_name:
        raise ValueError("新しいセット名を入力してください")
    if new_name in score_file["selectionSets"]:
        raise ValueError(f"選択セット '{new_name}' は既に存在します")
    score_file["selectionSets"][new_name] = json.loads(json.dumps(score_file["selectionSets"][src_name]))


# ------------------------------------------------------------------ validation

def _format_pydantic_error(err: ValidationError) -> List[str]:
    msgs = []
    for e in err.errors():
        loc = ".".join(str(x) for x in e["loc"] if x != "__root__")
        msg = e["msg"].removeprefix("Value error, ")
        msgs.append(f"{loc}: {msg}" if loc else msg)
    return msgs


def validate_score_file(data: Dict[str, Any]) -> List[str]:
    """All problems the engine would report at load time, as readable strings
    (empty list = OK). Includes expression parse/reference checks and
    duplicate part names, which pydantic alone does not cover."""
    try:
        sf = ScoreFile.model_validate(data)
    except ValidationError as err:
        return _format_pydantic_error(err)

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
            part.resolve_selection_refs(sf.selectionSets)
        except (ValueError, ValidationError) as err:
            problems.append(str(err) if isinstance(err, ValueError) else "; ".join(_format_pydantic_error(err)))
    return problems


def _expression_problems(expression: str, names: List[str]) -> List[str]:
    try:
        evaluate_expression(expression, {n: 1.0 for n in names})
    except Exception as err:
        return [f"expression: {err}"]
    return []


def validate_part(part: Dict[str, Any], selection_sets: Optional[Dict[str, list]] = None) -> List[str]:
    """Validation for a single part while it is being edited."""
    single = {
        "score_parts": [part],
        "expression": "",
        "constraintThreshold": {},
        "selectionSets": selection_sets or {},
    }
    return validate_score_file(single)


# --------------------------------------------------------------------- context

def _resolve_optional_file(explicit: Optional[str], discover, label: str):
    """An optional companion file: an explicitly given path wins (and must
    exist); otherwise fall back to discovery inside the data directory.
    Returns (path or None, '指定' | '自動検出' | None)."""
    if explicit and str(explicit).strip():
        p = Path(str(explicit).strip()).resolve()
        if not p.is_file():
            raise ValueError(f"{label} が見つかりません: {p}")
        return p, "指定"
    found = discover()
    return (found, "自動検出") if found else (None, None)


def build_context(
    data_dir: str,
    config_path: Optional[str] = None,
    coef_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Everything screen 1 derives from the measurement-output directory
    (a past run of the same experiment family -- design doc section 5.1).

    result_tmp normally holds measurement outputs only, so the optimization
    config jsonc and the dVtBudget coefficient jsonc are given as separate
    (optional) paths; in-directory discovery is kept as a convenience for
    when they do happen to be there."""
    if not str(data_dir).strip():
        raise ValueError("測定結果ディレクトリのパスを入力してください")
    d = Path(str(data_dir).strip()).resolve()
    if not d.is_dir():
        raise ValueError(f"ディレクトリが見つかりません: {d}")

    types = introspect.detect_types(d)

    coef_file, coef_source = _resolve_optional_file(
        coef_path, lambda: introspect.find_dvtbudget_coef(d), "dVtBudget係数jsonc"
    )
    if coef_file is not None:
        try:
            io_jsonc.load_dvtbudget_coef(coef_file)
        except Exception as err:
            raise ValueError(f"dVtBudget係数jsoncを読み込めません ({coef_file}): {err}")

    config_file, config_source = _resolve_optional_file(
        config_path, lambda: introspect.find_run_config(d), "optimization設定jsonc"
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
        "existing_score_file": None,
    }
    if config_file:
        try:
            run_config = io_jsonc.load_run_config(config_file)
        except Exception as err:
            raise ValueError(f"optimization設定jsoncを読み込めません ({config_file}): {err}")
        ctx["generation"] = run_config.Generation
        ctx["wlgroup"] = dict(run_config.optimization.WLgroup)
        existing = run_config.to_score_file()
        if existing.score_parts or existing.selectionSets or existing.expression:
            ctx["existing_score_file"] = existing.model_dump(exclude_none=True)
    return ctx


def part_summary_rows(score_file: Dict[str, Any]) -> List[Dict[str, str]]:
    rows = []
    for p in score_file["score_parts"]:
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


# ----------------------------------------------------------------- draft/export

def score_file_to_jsonc(score_file: Dict[str, Any]) -> str:
    cleaned = ScoreFile.model_validate(score_file).model_dump(exclude_none=True)
    return json.dumps(cleaned, indent=2, ensure_ascii=False) + "\n"


def save_draft(
    score_file: Dict[str, Any],
    context_inputs: Optional[Dict[str, Optional[str]]] = None,
    path: Optional[Path] = None,
) -> None:
    """The draft carries the screen-1 inputs (data_dir / config_path /
    coef_path) alongside the score file, so restoring a draft can also
    restore the loaded-data context instead of sending the user back to
    screen 1."""
    path = path or DRAFT_PATH  # resolved at call time so tests can repoint DRAFT_PATH
    payload = {"score_file": score_file, "context_inputs": context_inputs or {}}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_draft(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Returns {"score_file": ..., "context_inputs": {...}} or None.
    Drafts from before context_inputs existed (a bare ScoreFile dict) are
    still accepted."""
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
    if "score_parts" in data:  # legacy format
        return {"score_file": data, "context_inputs": {}}
    return None


def _part_refs(part: Dict[str, Any]) -> List[str]:
    specs = list(part.get("aggregations", {}).values())
    specs += (part.get("relative") or {}).get("denominator_pre_aggregation", [])
    return sorted({s["ref"] for s in specs if isinstance(s, dict) and s.get("ref")})


def export_part(score_file: Dict[str, Any], index: int) -> str:
    """A single part as a self-contained score.jsonc (referenced selection
    sets bundled) so it can be re-imported elsewhere."""
    part = score_file["score_parts"][index]
    refs = _part_refs(part)
    missing = [r for r in refs if r not in score_file["selectionSets"]]
    if missing:
        raise ValueError(f"パーツ '{part.get('name')}' が参照する選択セットが未定義です: {missing}")
    bundle = {
        "score_parts": [part],
        "expression": part.get("name", ""),
        "constraintThreshold": {},
        "selectionSets": {r: score_file["selectionSets"][r] for r in refs},
    }
    return score_file_to_jsonc(bundle)


def run_test_compute(
    score_file: Dict[str, Any],
    data_dir: str,
    generation: Optional[str] = None,
    wlgroup: Optional[Dict[str, Any]] = None,
    coef_path: Optional[str] = None,
) -> Dict[str, float]:
    """Screen 5: run the engine on real data. The coefficient file is taken
    from `coef_path` when given (it normally lives outside result_tmp);
    initial_temperature.csv is a measurement output, so it is always read
    from the data directory."""
    from scorelib.cli import compute_score_file
    from scorelib.dvtbudget import load_board_temperatures
    from scorelib.models import RunConfig

    d = Path(data_dir)
    if not d.is_dir():
        raise ValueError(f"ディレクトリが見つかりません: {d}")
    sf = ScoreFile.model_validate(score_file)
    run_config = RunConfig.model_validate(
        {
            "Generation": generation or "",
            "optimization": {
                "score_parts": sf.model_dump(exclude_none=True)["score_parts"],
                "expression": sf.expression,
                "constraintThreshold": {},  # thresholds do not change part values
                "selectionSets": sf.selectionSets,
                "WLgroup": wlgroup or {},
            },
        }
    )
    coef_file, _ = _resolve_optional_file(
        coef_path, lambda: introspect.find_dvtbudget_coef(d), "dVtBudget係数jsonc"
    )
    coef = io_jsonc.load_dvtbudget_coef(coef_file) if coef_file else None
    temp_csv = d / "initial_temperature.csv"
    temps = load_board_temperatures(temp_csv) if temp_csv.exists() else None
    return compute_score_file(d, run_config, coef, temps)


def import_score_file(text: str) -> Dict[str, Any]:
    """Parse an exported score.jsonc (ScoreFile shape) or a full run config
    (Generation + optimization{...}) into the editable dict."""
    data = jsonc.loads(text)
    if not isinstance(data, dict):
        raise ValueError("jsoncのトップレベルがオブジェクトではありません")
    if "optimization" in data:
        from scorelib.models import RunConfig

        sf = RunConfig.model_validate(data).to_score_file()
    else:
        sf = ScoreFile.model_validate(data)
    return sf.model_dump(exclude_none=True)
