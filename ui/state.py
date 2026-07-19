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

from scorelib import custom as scorelib_custom
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
    return {
        "score_parts": [],
        "expression": "",
        "constraintThreshold": {},
        "selectionSets": {},
        "groupDefs": {},
    }


def ensure_uids(score_file: Dict[str, Any]) -> None:
    """Give each part a stable internal id used for widget keys (index-based
    keys would leak state across parts after a delete). pydantic ignores the
    extra field, so validation and export are unaffected.

    Duplicate ids are regenerated: two parts sharing an id would share every
    widget (name field, relative checkbox, ...), so editing one would silently
    rewrite the other. Also repairs drafts saved while that bug existed."""
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


def custom_part_skeleton(name: str, functions: List[str]) -> Dict[str, Any]:
    """A new type="custom" part: no pipeline fields, just the function to
    call (first available one) and an empty params dict."""
    return {
        "name": name,
        "type": "custom",
        "function": functions[0] if functions else name,
        "params": {},
    }


def switch_part_type(part: Dict[str, Any], new_type: str) -> Optional[str]:
    """Adjust a part's fields for its new type; returns a user notice or
    None. custom parts must not carry pipeline fields (and vice versa) —
    the engine rejects the mix, so the UI strips it at the switch."""
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
    `order` (so the UI can tell the user). An explicitly placed __relative__
    step is removed too — without a relative config it would be a validation
    error."""
    rel = part.pop("relative", None)
    if not rel:
        return None
    if "__relative__" in part.get("order", []):
        part["order"].remove("__relative__")
        part["aggregations"].pop("__relative__", None)
    axis = rel.get("split_axis")
    return axis if _restore_axis_to_order(part, axis, catalog) else None


def drop_stale_virtual_steps(part: Dict[str, Any]) -> Optional[str]:
    """After a type change away from dVtBudget, an explicitly placed
    __dvtbudget__ step would be a validation error; remove it. Returns the
    removed step name (for a UI notice) or None."""
    if part.get("type") != "dVtBudget" and "__dvtbudget__" in part.get("order", []):
        part["order"].remove("__dvtbudget__")
        part["aggregations"].pop("__dvtbudget__", None)
        return "__dvtbudget__"
    return None


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
    # the copy must get its own widget-key id: sharing one would make both
    # parts share every widget's remembered state
    copy.pop("_uid", None)
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


# ---------------------------------------------------------------- group defs

def import_config_group_defs(score_file: Dict[str, Any], wlgroup: Optional[Dict[str, Any]]) -> bool:
    """Bring the config jsonc's WLgroup in as an editable definition. The
    score file is self-contained (its groupDefs are what the engine uses);
    the config's WLgroup is only the initial template. Returns True when the
    definition was added."""
    defs = score_file.setdefault("groupDefs", {})
    if not wlgroup or "WLgroup" in defs:
        return False
    defs["WLgroup"] = {"axis": "WL", "groups": {k: list(v) for k, v in wlgroup.items()}}
    return True


def _part_axis_names(part: Dict[str, Any]) -> set:
    """Every axis-like name a part mentions (order entries incl. combined
    components, relative split axis, denominator pre-aggregation axes)."""
    axes = set(_axes_in_order(part))
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

def _format_pydantic_error(err: ValidationError, data: Optional[Dict[str, Any]] = None) -> List[str]:
    """Readable messages. With the raw `data`, a location like
    'score_parts.12.aggregations.WL' becomes パーツ '<name>' の aggregations.WL
    (an index tells the user nothing about which part is broken)."""
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
    """All problems the engine would report at load time, as readable strings
    (empty list = OK). Includes expression parse/reference checks and
    duplicate part names, which pydantic alone does not cover."""
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
    `discover` returns a candidate LIST — more than one match is an error
    (silently taking the alphabetically first would design against the
    wrong file). Returns (path or None, '指定' | '自動検出' | None)."""
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

    # custom_parts.py: SVN-versioned user functions (scorelib/custom.py).
    # Loading = importing = executing its top-level code; acceptable because
    # the file comes from the reviewed repository, not from arbitrary users.
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
        # after `catalogs`: the custom pseudo-type has no axis catalog
        ctx["part_types"] = ctx["part_types"] + ["custom"]
    return ctx


def extract_bundle_zip(data: bytes) -> str:
    """Extract an uploaded 一式zip (result_tmp files + config jsonc + coef
    jsonc + generation info json + custom_parts.py) into a temp directory and
    return the directory to hand to build_context — every companion file is
    then picked up by the normal in-directory auto-detection. Descends into a
    single top-level folder (zips made from a folder usually have one)."""
    import io
    import tempfile
    import zipfile

    target = Path(tempfile.mkdtemp(prefix="scorelib_bundle_"))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for m in z.infolist():
            parts = Path(m.filename).parts
            if not parts or m.filename.startswith(("/", "\\")) or ".." in parts:
                continue  # zip-slip guard
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
    """Locate the measurement directory and companion files anywhere inside
    an extracted 一式zip (depth-limited walk), using the same CONTENT-based
    identification as the in-directory auto-detection: measurement dir = a
    directory where types are detectable (parameterLabel_*/dataName_*/
    Measure-column csvs), config = jsonc with an optimization{} block,
    coef = jsonc validating as the 3-level {a, b} table, generation info =
    {Generation}.json (Generation read from the config), custom =
    custom_parts.py. Ambiguity (two candidates for one role) is an error —
    silently picking one would design against the wrong file."""
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
    """Axis -> number of values, from the per-generation chip info json
    (B9LS.json shape). Only the axes the file describes."""
    counts: Dict[str, int] = {}
    if isinstance(geninfo, dict):
        if isinstance(geninfo.get("numWLs"), int):
            counts["WL"] = geninfo["numWLs"]
        if isinstance(geninfo.get("numStrings"), int):
            counts["STR"] = geninfo["numStrings"]
    return counts


def _format_value_runs(values: List[int]) -> str:
    """[0,1,2,5] -> '0–2, 5' (compact display for uncovered-value lists)."""
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
    """Mismatches between group definitions and the generation's axis counts
    (numWLs / numStrings). Warnings, not errors: the score still computes,
    but a range disagreeing with the chip's WL/STR count is almost always a
    config mistake (joint WLs are NOT normally excluded from groups, per
    担当者確認)."""
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


def part_list_labels(
    score_file: Dict[str, Any],
    selected_uid: Optional[str],
    invalid_uids: set,
) -> List[str]:
    """Labels for the always-draggable parts list: ⠿ drag handle, ⚠ on parts
    failing validation, ← 編集中 on the selected part. Pure on purpose: the
    D&D component's rendering is invisible to AppTest, so the marker logic
    must be verifiable here."""
    labels = []
    for i, (row, p) in enumerate(zip(part_summary_rows(score_file), score_file["score_parts"])):
        labels.append(
            "⠿ "
            + ("⚠ " if p.get("_uid") in invalid_uids else "")
            + f"{i + 1}. {row['名前']}（{row['type']}, 相対化{row['相対化']}）"
            + (" ← 編集中" if p.get("_uid") == selected_uid else "")
        )
    return labels


def part_select_labels(score_file: Dict[str, Any], invalid_uids: set) -> Dict[str, str]:
    """uid -> display label for the part-selection pulldown. Numbered so the
    labels stay unique even when two parts (temporarily) share a name:
    Streamlit's selectbox frontend matches items by their displayed label,
    and duplicate labels make clicks resolve to the wrong part or not
    register at all. The numbering also matches the ⠿ list."""
    return {
        p.get("_uid"): (
            f"{i + 1}. "
            + ("⚠ " if p.get("_uid") in invalid_uids else "")
            + p.get("name", "")
        )
        for i, p in enumerate(score_file["score_parts"])
    }


def part_summary_rows(score_file: Dict[str, Any]) -> List[Dict[str, str]]:
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
    sets and group definitions bundled) so it can be re-imported elsewhere."""
    part = score_file["score_parts"][index]
    refs = _part_refs(part)
    missing = [r for r in refs if r not in score_file["selectionSets"]]
    if missing:
        raise ValueError(f"パーツ '{part.get('name')}' が参照する選択セットが未定義です: {missing}")
    all_defs = score_file.get("groupDefs", {})
    used_defs = sorted(_part_axis_names(part) & set(all_defs))
    bundle = {
        "score_parts": [part],
        "expression": part.get("name", ""),
        "constraintThreshold": {},
        "selectionSets": {r: score_file["selectionSets"][r] for r in refs},
        "groupDefs": {n: all_defs[n] for n in used_defs},
    }
    return score_file_to_jsonc(bundle)


def run_test_compute(
    score_file: Dict[str, Any],
    data_dir: str,
    generation: Optional[str] = None,
    wlgroup: Optional[Dict[str, Any]] = None,
    coef_path: Optional[str] = None,
    custom_path: Optional[str] = None,
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
    dump = sf.model_dump(exclude_none=True)
    run_config = RunConfig.model_validate(
        {
            "Generation": generation or "",
            "optimization": {
                "score_parts": dump["score_parts"],
                "expression": sf.expression,
                "constraintThreshold": {},  # thresholds do not change part values
                "selectionSets": sf.selectionSets,
                # the score file's own definitions win over the config WLgroup
                "WLgroup": wlgroup or {},
                "groupDefs": dump.get("groupDefs", {}),
            },
        }
    )
    coef_file, _ = _resolve_optional_file(
        coef_path, lambda: introspect.find_dvtbudget_coefs(d), "dVtBudget係数jsonc"
    )
    coef = io_jsonc.load_dvtbudget_coef(coef_file) if coef_file else None
    temp_csv = d / "initial_temperature.csv"
    temps = load_board_temperatures(temp_csv) if temp_csv.exists() else None
    return compute_score_file(d, run_config, coef, temps, custom_parts_path=custom_path)


def import_score_file(text: str) -> Dict[str, Any]:
    """Parse an exported score.jsonc (ScoreFile shape) or a full run config
    (Generation + optimization{...}) into the editable dict."""
    data = jsonc.loads(text)
    if not isinstance(data, dict):
        raise ValueError("jsoncのトップレベルがオブジェクトではありません")
    try:
        if "optimization" in data:
            from scorelib.models import RunConfig

            sf = RunConfig.model_validate(data).to_score_file()
        else:
            sf = ScoreFile.model_validate(data)
    except ValidationError as err:
        raise ValueError("\n".join(_format_pydantic_error(err, data)))
    return sf.model_dump(exclude_none=True)
