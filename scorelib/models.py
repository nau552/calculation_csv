"""Data models for score / score-part definitions.

See score_gui_design.md sections 3-6 for the design rationale.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, RootModel, model_validator

# Separator for combined axes in `order` entries (e.g. "State&Read_Label").
COMBINED_SEP = "&"

# ScorePart.type value that calls a user-defined python function
# (custom_parts.py, see scorelib/custom.py) instead of the pipeline.
CUSTOM_TYPE = "custom"

AggOp = Literal[
    "filter",
    # mean/sum/min/max reduce the axis; an optional `value` list restricts
    # the reduction to those selections first (e.g. {"op": "sum",
    # "value": [0, 1]} sums only rows where the axis is 0 or 1)
    "mean",
    "sum",
    "min",
    "max",
    "expr",
    # collapse the axis by combining exactly two selections (ordered):
    # {"op": "diff", "value": [a, b]} -> value(a) - value(b)
    "diff",
    # transform ops: applied to the value column row-wise without collapsing
    # an axis; used by virtual "__xxx__" steps in `order` (e.g. __offset__)
    "add",
]

# Legacy spellings from before `value` became a modifier on the plain ops.
_SUBSET_ALIASES = {
    "mean_subset": "mean",
    "sum_subset": "sum",
    "min_subset": "min",
    "max_subset": "max",
}

_MULTI_OPS = ("mean", "sum", "min", "max")


class AggregationSpec(BaseModel):
    """One axis's (or virtual step's) instruction.

    Selections always go in `value` regardless of op:
    - a scalar selects one axis value ({"op": "filter", "value": "A2B"})
    - a dict selects one combination on a combined axis
      ({"op": "filter", "value": {"State": "A2B", "Read_Label": "..."}})
    - a list is always a sequence of selections
      ({"op": "diff", "value": ["R2A", "B2A"]})
    What varies per op is only how many selections it needs (filter: 1,
    diff: 2, mean/sum/min/max: any number or none). `values` is accepted as
    a compatibility alias for `value`.
    """

    op: AggOp
    value: Optional[Any] = None
    # Reference to a named selection set (optimization.selectionSets) used
    # instead of an inline `value`; resolved before computation, after which
    # the resolved content passes exactly the same shape checks as an inline
    # value would.
    ref: Optional[str] = None
    expr: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_spellings(cls, data):
        if isinstance(data, dict):
            if data.get("op") == "group_reduce":
                raise ValueError(
                    "op 'group_reduce' has been removed; define the group in groupDefs and "
                    "put its name (e.g. 'WLgroup') in `order` as a derived axis instead "
                    "(inner op on the source axis, outer op on the group axis)"
                )
            if data.get("op") in _SUBSET_ALIASES:
                data = {**data, "op": _SUBSET_ALIASES[data["op"]]}
            if data.get("values") is not None:
                if data.get("value") is not None:
                    raise ValueError("give selections in 'value' ('values' is an alias) — not both")
                values = data["values"]
                data = {k: v for k, v in data.items() if k != "values"}
                data["value"] = values
        return data

    @model_validator(mode="after")
    def _check_value_shape(self):
        op, v = self.op, self.value
        if self.ref is not None:
            if v is not None:
                raise ValueError("give either 'value' or 'ref' (a named selection set), not both")
            if op in ("add", "expr"):
                raise ValueError(f"op '{op}' takes no selections, so 'ref' is not applicable")
            # value-shape checks run again after the ref is resolved
            return self
        if op == "filter":
            if isinstance(v, list):
                if len(v) == 1 and not isinstance(v[0], list):
                    self.value = v[0]
                else:
                    raise ValueError(
                        "op 'filter' selects exactly one value (a scalar, or a dict for "
                        "combined axes); to reduce over several values use mean/sum/min/max"
                    )
            elif v is None:
                raise ValueError("op 'filter' requires 'value'")
        elif op == "add":
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise ValueError("op 'add' requires a numeric 'value'")
        elif op == "diff":
            if not isinstance(v, list) or len(v) != 2:
                raise ValueError(
                    "op 'diff' requires 'value': [a, b] — exactly two selections (result = a - b)"
                )
            if any(isinstance(x, list) for x in v):
                raise ValueError(
                    "op 'diff': each selection must be a scalar or, for combined axes, a "
                    "dict like {\"State\": ..., \"Read_Label\": ...} — not a nested list"
                )
        elif op in _MULTI_OPS:
            if v is not None:
                if not isinstance(v, list):
                    self.value = v = [v]
                if any(isinstance(x, list) for x in v):
                    raise ValueError(
                        f"op '{op}': each selection must be a scalar or, for combined axes, "
                        "a dict {axis: value} — not a nested list"
                    )
        elif op == "expr":
            if not self.expr:
                raise ValueError("op 'expr' requires 'expr'")
            if v is not None:
                raise ValueError("op 'expr' takes no 'value'; select inside the expression via by[...]")
        return self


class AxisAggregation(AggregationSpec):
    """Same shape as AggregationSpec but carries its own axis name.

    Used for denominator_pre_aggregation, where aggregation steps are a list
    rather than a dict keyed by axis (order matters, an axis could in theory
    appear only here and not in the main `order`).
    """

    axis: str


class RelativeConfig(BaseModel):
    """Presence of a `relative` block on a ScorePart means relative-ization
    happens; to compute absolute values, omit (or comment out) the block.
    There is no `enabled` flag."""

    split_axis: str
    numerator_when: Any
    denominator_when: Any
    # "ratio": (num + offset) / (den + offset)   (default)
    # "diff":  num - den                          (offset is irrelevant and ignored)
    mode: Literal["ratio", "diff"] = "ratio"
    denominator_offset: float = 0.0
    denominator_pre_aggregation: List[AxisAggregation] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_enabled(cls, data):
        if isinstance(data, dict) and "enabled" in data:
            data = dict(data)
            enabled = data.pop("enabled")
            # A leftover `enabled: true` is harmless -- drop it silently.
            # `enabled: false` must NOT silently become enabled: fail loudly.
            if not enabled or str(enabled).strip().lower() == "false":
                raise ValueError(
                    "relative.enabled has been removed; to compute without "
                    "relative-ization, delete (or comment out) the whole relative block"
                )
        return data


class ScorePart(BaseModel):
    name: str
    type: str
    relative: Optional[RelativeConfig] = None
    order: List[str] = Field(default_factory=list)
    aggregations: Dict[str, AggregationSpec] = Field(default_factory=dict)
    # type="custom" only: the function in custom_parts.py to call (defaults
    # to the part name) and the params dict handed to it
    function: Optional[str] = None
    params: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def _check_custom_fields(self):
        if self.type == CUSTOM_TYPE:
            if self.order or self.aggregations or self.relative:
                raise ValueError(
                    f"custom part '{self.name}' takes no order/aggregations/relative — "
                    "its function computes the value directly"
                )
        elif self.function is not None or self.params:
            raise ValueError(
                f"'function'/'params' are only valid on type='{CUSTOM_TYPE}' (part '{self.name}')"
            )
        return self

    @model_validator(mode="after")
    def _check_combined_axis_selections(self):
        """Dict selections must name exactly the axes of their combined-axis
        entry; combined axes must use dict selections (never positional
        lists, which would be ambiguous); plain axes must not use dicts."""
        for entry in self.order:
            if entry.startswith("__"):
                continue
            axes = entry.split(COMBINED_SEP)
            spec = self.aggregations.get(entry)
            if spec is None or spec.value is None:
                continue
            selections = spec.value if isinstance(spec.value, list) else [spec.value]
            for sel in selections:
                if isinstance(sel, dict):
                    if len(axes) == 1:
                        raise ValueError(
                            f"axis '{entry}' in '{self.name}': dict selections are only valid "
                            f"on combined axes (e.g. 'State{COMBINED_SEP}Read_Label')"
                        )
                    if set(sel) != set(axes):
                        raise ValueError(
                            f"combined axis '{entry}' in '{self.name}' expects keys {axes}, "
                            f"got {sorted(sel)}"
                        )
                elif len(axes) > 1:
                    raise ValueError(
                        f"combined axis '{entry}' in '{self.name}': each selection must be a "
                        f"dict naming its axes, e.g. {{{', '.join(repr(a) + ': ...' for a in axes)}}}; "
                        f"got {sel!r}"
                    )
        return self

    def resolve_selection_refs(self, selection_sets: Dict[str, List[Any]]) -> "ScorePart":
        """Return a copy with every `ref` replaced by the referenced
        selection set's content. Re-validates the whole part so the resolved
        selections pass exactly the same checks as inline ones."""
        specs = list(self.aggregations.values())
        if self.relative:
            specs += list(self.relative.denominator_pre_aggregation)
        if not any(s.ref is not None for s in specs):
            return self

        def _resolve(spec_dict: dict) -> None:
            ref = spec_dict.get("ref")
            if ref is None:
                return
            if ref not in selection_sets:
                raise ValueError(
                    f"score part '{self.name}': unknown selection set '{ref}' "
                    f"(defined sets: {sorted(selection_sets)})"
                )
            spec_dict["value"] = deepcopy(selection_sets[ref])
            spec_dict["ref"] = None

        data = self.model_dump()
        for spec_dict in data["aggregations"].values():
            _resolve(spec_dict)
        if data.get("relative"):
            for step_dict in data["relative"]["denominator_pre_aggregation"]:
                _resolve(step_dict)
        return ScorePart.model_validate(data)


class GroupDef(BaseModel):
    """A derived axis: rows get a group label from integer ranges over a
    source axis (e.g. WLgroup: WL 0-3 -> "WLgroup01"). The group column is
    created at data-load time, so the definition's name can be placed in a
    part's `order` and aggregated at any position like a real axis (e.g.
    WL mean -> Board max -> WLgroup max). The name must differ from the
    source axis."""

    axis: str
    groups: Dict[str, Tuple[int, int]] = Field(default_factory=dict)


class ConstraintThresholdEntry(BaseModel):
    value: float
    active: Optional[str] = None
    type: Optional[str] = None
    coef: Optional[float] = None


class ScoreFile(BaseModel):
    """The user-authored content: score parts + composition expression + constraints.

    This is merged into the `optimization{}` block of the run config (see
    RunConfig below) rather than kept as a fully separate file.
    """

    score_parts: List[ScorePart] = Field(default_factory=list)
    expression: str = ""
    constraintThreshold: Dict[str, ConstraintThresholdEntry] = Field(default_factory=dict)
    # bundled so an exported score file stays self-contained when its parts
    # use `ref` or derived group axes
    selectionSets: Dict[str, List[Any]] = Field(default_factory=dict)
    groupDefs: Dict[str, GroupDef] = Field(default_factory=dict)


class OptimizationConfig(BaseModel):
    score_function: Optional[str] = None
    constraintThreshold: Dict[str, ConstraintThresholdEntry] = Field(default_factory=dict)
    WLgroup: Dict[str, Tuple[int, int]] = Field(default_factory=dict)
    selectionSets: Dict[str, List[Any]] = Field(default_factory=dict)
    groupDefs: Dict[str, GroupDef] = Field(default_factory=dict)
    score_parts: List[ScorePart] = Field(default_factory=list)
    expression: str = ""


class RunConfig(BaseModel):
    """The config.jsonc consumed by the engine at compute time (sample.jsonc shape)."""

    Generation: str
    optimization: OptimizationConfig

    def to_score_file(self) -> ScoreFile:
        return ScoreFile(
            score_parts=self.optimization.score_parts,
            expression=self.optimization.expression,
            constraintThreshold=self.optimization.constraintThreshold,
            selectionSets=self.optimization.selectionSets,
            groupDefs=self.optimization.groupDefs,
        )

    def group_defs(self) -> Dict[str, GroupDef]:
        """All group definitions: the legacy optimization.WLgroup (implicitly
        a definition over WL) plus groupDefs, which wins on a name clash."""
        defs: Dict[str, GroupDef] = {}
        if self.optimization.WLgroup:
            defs["WLgroup"] = GroupDef(axis="WL", groups=self.optimization.WLgroup)
        defs.update(self.optimization.groupDefs)
        return defs


class DvtBudgetCoefEntry(BaseModel):
    a: float
    b: float


# generation -> temperature(as string key, e.g. "-30", "85") -> state -> {a, b}
class DvtBudgetCoefFile(RootModel[Dict[str, Dict[str, Dict[str, DvtBudgetCoefEntry]]]]):
    pass
