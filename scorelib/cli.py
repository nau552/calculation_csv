"""CLI entry point: computes Score + every ScorePart's value from real epoch
data, meant to be invoked as a subprocess from the (python3.7) optimizer's
`get_score()` -- see score_gui_design.md sections 2 and 7.

    python -m scorelib.cli --config config.jsonc --data-dir <epoch_dir> \
        [--dvtbudget-coef coef.jsonc] [--initial-temperature initial_temperature.csv]

Prints a single JSON object to stdout: {"Score": ..., "<part name>": ..., ...}
(no InBatchEpoch column - see score_gui_design.md section 5/7 for the output
contract).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Optional, Set

import polars as pl

from . import axis_resolve, custom, io_jsonc
from .aggregate import apply_aggregations, apply_transform, collapse_to_scalar, group_column_expr
from .dvtbudget import apply_dvtbudget, load_board_temperatures
from .expression import evaluate_expression
from .models import COMBINED_SEP, CUSTOM_TYPE, DvtBudgetCoefFile, GroupDef, RunConfig, ScorePart
from .relative import apply_relative

# Virtual entries usable in `order` alongside axis names (score_gui_design.md
# section 4.1). Entries starting with "__" are pipeline steps, not axes:
# - RELATIVE_STEP: where relative-ization happens (default: before everything)
# - DVTBUDGET_STEP: where the dVtBudget conversion happens (default: right
#   after relative). Board/State must still be un-aggregated at that point.
# - any other "__xxx__" name: a row-wise transform on the value column, whose
#   spec lives in `aggregations` under the same name (e.g.
#   "__offset__": {"op": "add", "value": 1}).
RELATIVE_STEP = "__relative__"
DVTBUDGET_STEP = "__dvtbudget__"

# An order entry may bundle several axes into one combined axis, e.g.
# "State&Read_Label". Its aggregation spec then takes dict selections:
#   {"op": "sum", "value": [{"State": "R2A", "Read_Label": "read_level_upper1"},
#                           {"State": "A2R", "Read_Label": "read_level_lower1"}]}
# The bundled axes collapse together as one axis, so filter/sum/diff/expr all
# work on (State, Read_Label) pairs. Axis values must not contain "&".


def _is_virtual(step: str) -> bool:
    return step.startswith("__")


def _step_axes(step: str) -> list[str]:
    return step.split(COMBINED_SEP)


def _named_axes(score_part: ScorePart) -> Set[str]:
    """Every axis-like name the part itself mentions (order entries incl.
    combined components, the relative split axis, denominator pre-aggregation
    axes). May contain derived group-axis names."""
    axes: Set[str] = set()
    for entry in score_part.order:
        if not _is_virtual(entry):
            axes.update(_step_axes(entry))
    if score_part.relative:
        axes.add(score_part.relative.split_axis)
        for step in score_part.relative.denominator_pre_aggregation:
            axes.add(step.axis)
    return axes


def _referenced_group_defs(
    score_part: ScorePart, group_defs: Optional[Dict[str, GroupDef]]
) -> Dict[str, GroupDef]:
    """The group definitions this part actually uses as derived axes."""
    if not group_defs:
        return {}
    used = {n: group_defs[n] for n in _named_axes(score_part) if n in group_defs}
    for name, gd in used.items():
        if gd.axis == name:
            raise ValueError(
                f"group def '{name}' must not have the same name as its source axis"
            )
    return used


def _required_axes(
    score_part: ScorePart, group_defs: Optional[Dict[str, GroupDef]] = None
) -> Set[str]:
    """Real csv/map axes to load: derived group-axis names are replaced by
    their source axis (the group column is built from it after loading)."""
    named = _named_axes(score_part)
    derived = _referenced_group_defs(score_part, group_defs)
    axes = {a for a in named if a not in derived} | {gd.axis for gd in derived.values()}
    if score_part.type == "dVtBudget":
        axes.update({"Board", "State"})
    return axes


def _with_group_columns(
    lf, score_part: ScorePart, group_defs: Optional[Dict[str, GroupDef]]
):
    """Create the derived group columns this part references; afterwards they
    aggregate like real axes. A source axis loaded only for the derivation is
    dropped again: axes absent from the part's own entries are implicitly
    mixed by design, and a leftover column would instead fail the final
    collapse."""
    derived = _referenced_group_defs(score_part, group_defs)
    if not derived:
        return lf
    lf = lf.with_columns(
        [group_column_expr(gd.axis, gd.groups).alias(name) for name, gd in derived.items()]
    )
    # rows outside every range would form a silent null-label group — that is
    # almost always a stale definition, so fail with the offending values
    for name, gd in derived.items():
        uncovered = lf.filter(pl.col(name).is_null()).select(pl.col(gd.axis).unique()).collect()
        if uncovered.height:
            vals = sorted(uncovered[gd.axis].to_list())
            raise ValueError(
                f"values of axis '{gd.axis}' not covered by any group of '{name}': {vals} "
                f"(extend the group ranges or filter those values out first)"
            )
    keep = {a for a in _named_axes(score_part) if a not in derived}
    if score_part.type == "dVtBudget":
        keep.update({"Board", "State"})
    drop = {gd.axis for gd in derived.values() if gd.axis not in keep}
    return lf.drop(drop) if drop else lf


def _combined_key(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _combine_selection(sel: dict, axes: list[str]) -> str:
    """Turn one dict selection (validated by ScorePart) into the internal
    joined key matching the fused column."""
    return COMBINED_SEP.join(_combined_key(sel[a]) for a in axes)


def _effective_order(score_part: ScorePart) -> list[str]:
    """Insert the implicit pipeline steps where the user did not place them
    explicitly: relative first, dVtBudget conversion right after relative."""
    order = list(score_part.order)
    relative_enabled = score_part.relative is not None

    if RELATIVE_STEP in order and not relative_enabled:
        raise ValueError(f"'{RELATIVE_STEP}' in order but '{score_part.name}' has no relative config")
    if relative_enabled and RELATIVE_STEP not in order:
        order.insert(0, RELATIVE_STEP)

    if score_part.type == "dVtBudget" and DVTBUDGET_STEP not in order:
        pos = order.index(RELATIVE_STEP) + 1 if RELATIVE_STEP in order else 0
        order.insert(pos, DVTBUDGET_STEP)
    if DVTBUDGET_STEP in order and score_part.type != "dVtBudget":
        raise ValueError(f"'{DVTBUDGET_STEP}' in order but type of '{score_part.name}' is not dVtBudget")
    return order


def _source_type(score_part: ScorePart) -> str:
    return "FBC" if score_part.type == "dVtBudget" else score_part.type


class SharedComputeContext:
    """Per-invocation caches shared across score parts. Purely an internal
    optimization: results are identical with or without it.

    - resolved(): each source type's csv is scanned/joined once with the
      union of all parts' axes; parts then project down to exactly the
      columns they would have gotten from a standalone resolve, so pairing
      and grouping semantics are untouched.
    - prefix_cache: intermediates collected right after a __relative__ or
      __dvtbudget__ step, keyed by (source type, required axes, and the full
      signature of every step applied so far). Only parts whose settings
      match byte-for-byte up to that point share an entry.

    Lifetime is one compute_score_file() call; nothing persists across
    epochs, so there is no staleness to manage.
    """

    def __init__(
        self,
        data_dir: str | Path,
        score_parts: list[ScorePart],
        group_defs: Optional[Dict[str, GroupDef]] = None,
    ):
        self.data_dir = data_dir
        self._union_axes: Dict[str, Set[str]] = {}
        for part in score_parts:
            if part.type == CUSTOM_TYPE:
                continue  # custom parts read data themselves
            st = _source_type(part)
            self._union_axes.setdefault(st, set()).update(_required_axes(part, group_defs))
        self._resolved: Dict[str, "object"] = {}
        self.prefix_cache: Dict[tuple, "object"] = {}

    def resolved(self, source_type: str):
        if source_type not in self._resolved:
            self._resolved[source_type] = axis_resolve.resolve_axes(
                self.data_dir, source_type, self._union_axes[source_type]
            ).collect()
        return self._resolved[source_type]


def _apply_axis_step(lf, value_col: str, step: str, score_part: ScorePart):
    """Apply one non-virtual order entry: a plain axis, or a combined axis
    ("A&B") whose component columns are fused into one temporary key column
    so the existing per-axis ops work on value tuples."""
    axes = _step_axes(step)
    if len(axes) == 1:
        return apply_aggregations(lf, value_col, [step], score_part.aggregations)

    spec = score_part.aggregations.get(step)
    if spec is None:
        raise ValueError(f"axis '{step}' listed in order but has no aggregation instruction")
    lf = lf.with_columns(
        pl.concat_str([pl.col(a).cast(pl.Utf8) for a in axes], separator=COMBINED_SEP).alias(step)
    ).drop(axes)
    if isinstance(spec.value, list):
        combined = [_combine_selection(v, axes) for v in spec.value]
    elif spec.value is not None:
        combined = _combine_selection(spec.value, axes)
    else:
        combined = None
    spec = spec.model_copy(update={"value": combined})
    return apply_aggregations(lf, value_col, [step], {step: spec})


def _step_signature(score_part: ScorePart, step: str) -> tuple:
    if step == RELATIVE_STEP:
        return ("relative", score_part.relative.model_dump_json())
    if step == DVTBUDGET_STEP:
        return ("dvtbudget",)
    spec = score_part.aggregations.get(step)
    kind = "transform" if _is_virtual(step) else "axis"
    return (kind, step, spec.model_dump_json() if spec else "")


def compute_score_part(
    data_dir: str | Path,
    score_part: ScorePart,
    group_defs: Optional[Dict[str, GroupDef]] = None,
    generation: Optional[str] = None,
    dvtbudget_coef: Optional[DvtBudgetCoefFile] = None,
    board_temperatures: Optional[Dict[int, float]] = None,
    shared_ctx: Optional[SharedComputeContext] = None,
    selection_sets: Optional[Dict[str, list]] = None,
    custom_module=None,
) -> float:
    if score_part.type == CUSTOM_TYPE:
        if custom_module is None:
            raise ValueError(
                f"score part '{score_part.name}' has type='{CUSTOM_TYPE}' but no custom "
                f"parts file was loaded (expected {custom.default_custom_parts_path()})"
            )
        return custom.compute_custom_part(
            score_part,
            custom_module,
            custom.CustomContext(
                data_dir=Path(data_dir),
                generation=generation,
                group_defs=group_defs or {},
                params=score_part.params or {},
            ),
        )

    score_part = score_part.resolve_selection_refs(selection_sets or {})
    source_type = _source_type(score_part)
    required_axes = _required_axes(score_part, group_defs)

    if shared_ctx is not None:
        base = shared_ctx.resolved(source_type)
        # Project down to exactly what a standalone resolve would return:
        # extra union columns would change relative pairing keys and
        # aggregation group keys, so this projection is load-bearing.
        cols = [source_type] + sorted(required_axes)
        lf = base.lazy().select(cols)
    else:
        lf = axis_resolve.resolve_axes(data_dir, source_type, required_axes)

    lf = _with_group_columns(lf, score_part, group_defs)

    steps = _effective_order(score_part)
    sigs = [_step_signature(score_part, s) for s in steps]

    # Cache points sit right after each __relative__/__dvtbudget__ step; the
    # key covers everything that influenced the frame up to that point
    # (including the content of any derived group axes).
    cache_keys: Dict[int, tuple] = {}
    if shared_ctx is not None:
        defs_sig = tuple(
            sorted(
                (name, gd.axis, tuple(sorted(gd.groups.items())))
                for name, gd in _referenced_group_defs(score_part, group_defs).items()
            )
        )
        base_sig = (source_type, tuple(sorted(required_axes)), defs_sig)
        cache_keys = {
            i: (base_sig, tuple(sigs[: i + 1]))
            for i, s in enumerate(steps)
            if s in (RELATIVE_STEP, DVTBUDGET_STEP)
        }

    start = 0
    for i in sorted(cache_keys, reverse=True):
        cached = shared_ctx.prefix_cache.get(cache_keys[i])
        if cached is not None:
            lf = cached.lazy()
            start = i + 1
            break

    for j in range(start, len(steps)):
        step = steps[j]
        if step == RELATIVE_STEP:
            lf = apply_relative(lf, source_type, score_part.relative)
        elif step == DVTBUDGET_STEP:
            if generation is None or dvtbudget_coef is None or board_temperatures is None:
                raise ValueError(
                    "dVtBudget score parts require generation, dvtbudget_coef, and board_temperatures"
                )
            lf = apply_dvtbudget(lf, source_type, generation, dvtbudget_coef, board_temperatures)
        elif _is_virtual(step):
            spec = score_part.aggregations.get(step)
            if spec is None:
                raise ValueError(f"virtual step '{step}' has no entry in aggregations for '{score_part.name}'")
            lf = apply_transform(lf, source_type, spec)
        else:
            lf = _apply_axis_step(lf, source_type, step, score_part)

        if j in cache_keys:
            df = lf.collect()
            shared_ctx.prefix_cache[cache_keys[j]] = df
            lf = df.lazy()

    return collapse_to_scalar(lf, source_type)


def compute_score_file(
    data_dir: str | Path,
    run_config: RunConfig,
    dvtbudget_coef: Optional[DvtBudgetCoefFile] = None,
    board_temperatures: Optional[Dict[int, float]] = None,
    custom_parts_path: Optional[str | Path] = None,
) -> Dict[str, float]:
    score_file = run_config.to_score_file()
    group_defs = run_config.group_defs()

    # type="custom" parts call user functions from the SVN-versioned
    # custom_parts.py at the repository root; the config never carries the
    # path (a config-supplied path would mean arbitrary code execution from
    # experiment input). `custom_parts_path` exists for tests/the design UI.
    custom_module = None
    if any(p.type == CUSTOM_TYPE for p in score_file.score_parts):
        path = Path(custom_parts_path) if custom_parts_path else custom.default_custom_parts_path()
        if not path.is_file():
            raise ValueError(
                f"score parts with type='{CUSTOM_TYPE}' need the custom parts file: {path}"
            )
        custom_module = custom.load_custom_module(path)

    part_names = {p.name for p in score_file.score_parts}
    for key in score_file.constraintThreshold:
        if key not in part_names:
            print(
                f"warning: constraintThreshold key '{key}' does not match any score part "
                f"(defined parts: {sorted(part_names)})",
                file=sys.stderr,
            )

    shared_ctx = SharedComputeContext(data_dir, score_file.score_parts, group_defs)
    values: Dict[str, float] = {}
    for score_part in score_file.score_parts:
        values[score_part.name] = compute_score_part(
            data_dir,
            score_part,
            group_defs=group_defs,
            generation=run_config.Generation,
            dvtbudget_coef=dvtbudget_coef,
            board_temperatures=board_temperatures,
            shared_ctx=shared_ctx,
            selection_sets=score_file.selectionSets,
            custom_module=custom_module,
        )

    score = evaluate_expression(score_file.expression, values) if score_file.expression else None
    return {"Score": score, **values}


def main(argv: Optional[list[str]] = None) -> None:
    from . import __version__

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"scorelib {__version__}")
    parser.add_argument("--config", required=True, help="run config jsonc (Generation + optimization{...})")
    parser.add_argument("--data-dir", required=True, help="directory containing {type}.csv etc. for this epoch")
    parser.add_argument("--dvtbudget-coef", help="dVtBudget coefficient jsonc (required if any score part uses type=dVtBudget)")
    parser.add_argument("--initial-temperature", help="initial_temperature.csv (Board,Temperature; required for dVtBudget)")
    parser.add_argument("--custom-parts", help="custom_parts.py override (default: repository root)")
    args = parser.parse_args(argv)

    run_config = io_jsonc.load_run_config(args.config)
    dvtbudget_coef = io_jsonc.load_dvtbudget_coef(args.dvtbudget_coef) if args.dvtbudget_coef else None
    board_temperatures = load_board_temperatures(args.initial_temperature) if args.initial_temperature else None

    result = compute_score_file(
        args.data_dir, run_config, dvtbudget_coef, board_temperatures,
        custom_parts_path=args.custom_parts,
    )
    # stdout carries ONLY the result JSON (the optimizer parses it); the
    # version marker goes to stderr for the run logs
    print(f"scorelib {__version__}", file=sys.stderr)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
