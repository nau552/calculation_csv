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

from . import axis_resolve, io_jsonc
from .aggregate import GroupDefs, apply_aggregations, apply_transform, collapse_to_scalar
from .dvtbudget import apply_dvtbudget, load_board_temperatures
from .expression import evaluate_expression
from .models import DvtBudgetCoefFile, RunConfig, ScorePart
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


def _is_virtual(step: str) -> bool:
    return step.startswith("__")


def _required_axes(score_part: ScorePart) -> Set[str]:
    axes = {a for a in score_part.order if not _is_virtual(a)}
    if score_part.relative:
        axes.add(score_part.relative.split_axis)
        for step in score_part.relative.denominator_pre_aggregation:
            axes.add(step.axis)
    if score_part.type == "dVtBudget":
        axes.update({"Board", "State"})
    return axes


def _effective_order(score_part: ScorePart) -> list[str]:
    """Insert the implicit pipeline steps where the user did not place them
    explicitly: relative first, dVtBudget conversion right after relative."""
    order = list(score_part.order)
    relative_enabled = score_part.relative is not None and score_part.relative.enabled

    if RELATIVE_STEP in order and not relative_enabled:
        raise ValueError(f"'{RELATIVE_STEP}' in order but relative is not enabled for '{score_part.name}'")
    if relative_enabled and RELATIVE_STEP not in order:
        order.insert(0, RELATIVE_STEP)

    if score_part.type == "dVtBudget" and DVTBUDGET_STEP not in order:
        pos = order.index(RELATIVE_STEP) + 1 if RELATIVE_STEP in order else 0
        order.insert(pos, DVTBUDGET_STEP)
    if DVTBUDGET_STEP in order and score_part.type != "dVtBudget":
        raise ValueError(f"'{DVTBUDGET_STEP}' in order but type of '{score_part.name}' is not dVtBudget")
    return order


def compute_score_part(
    data_dir: str | Path,
    score_part: ScorePart,
    group_defs: Optional[GroupDefs] = None,
    generation: Optional[str] = None,
    dvtbudget_coef: Optional[DvtBudgetCoefFile] = None,
    board_temperatures: Optional[Dict[int, float]] = None,
) -> float:
    source_type = "FBC" if score_part.type == "dVtBudget" else score_part.type
    lf = axis_resolve.resolve_axes(data_dir, source_type, _required_axes(score_part))

    for step in _effective_order(score_part):
        if step == RELATIVE_STEP:
            lf = apply_relative(lf, source_type, score_part.relative, group_defs)
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
            lf = apply_aggregations(lf, source_type, [step], score_part.aggregations, group_defs)

    return collapse_to_scalar(lf, source_type)


def compute_score_file(
    data_dir: str | Path,
    run_config: RunConfig,
    dvtbudget_coef: Optional[DvtBudgetCoefFile] = None,
    board_temperatures: Optional[Dict[int, float]] = None,
) -> Dict[str, float]:
    score_file = run_config.to_score_file()
    group_defs = run_config.group_defs()

    part_names = {p.name for p in score_file.score_parts}
    for key in score_file.constraintThreshold:
        if key not in part_names:
            print(
                f"warning: constraintThreshold key '{key}' does not match any score part "
                f"(defined parts: {sorted(part_names)})",
                file=sys.stderr,
            )

    values: Dict[str, float] = {}
    for score_part in score_file.score_parts:
        values[score_part.name] = compute_score_part(
            data_dir,
            score_part,
            group_defs=group_defs,
            generation=run_config.Generation,
            dvtbudget_coef=dvtbudget_coef,
            board_temperatures=board_temperatures,
        )

    score = evaluate_expression(score_file.expression, values) if score_file.expression else None
    return {"Score": score, **values}


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="run config jsonc (Generation + optimization{...})")
    parser.add_argument("--data-dir", required=True, help="directory containing {type}.csv etc. for this epoch")
    parser.add_argument("--dvtbudget-coef", help="dVtBudget coefficient jsonc (required if any score part uses type=dVtBudget)")
    parser.add_argument("--initial-temperature", help="initial_temperature.csv (Board,Temperature; required for dVtBudget)")
    args = parser.parse_args(argv)

    run_config = io_jsonc.load_run_config(args.config)
    dvtbudget_coef = io_jsonc.load_dvtbudget_coef(args.dvtbudget_coef) if args.dvtbudget_coef else None
    board_temperatures = load_board_temperatures(args.initial_temperature) if args.initial_temperature else None

    result = compute_score_file(args.data_dir, run_config, dvtbudget_coef, board_temperatures)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
