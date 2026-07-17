"""Data models for score / score-part definitions.

See score_gui_design.md sections 3-6 for the design rationale.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, RootModel

AggOp = Literal[
    "filter",
    "mean",
    "sum",
    "min",
    "max",
    "mean_subset",
    "sum_subset",
    "min_subset",
    "max_subset",
    "group_reduce",
    "expr",
    # transform ops: applied to the value column row-wise without collapsing
    # an axis; used by virtual "__xxx__" steps in `order` (e.g. __offset__)
    "add",
]


class AggregationSpec(BaseModel):
    op: AggOp
    value: Optional[Any] = None
    values: Optional[List[Any]] = None
    group_def: Optional[str] = None
    inner_op: Optional[Literal["mean", "sum", "min", "max"]] = None
    outer_op: Optional[Literal["mean", "sum", "min", "max"]] = None
    expr: Optional[str] = None


class AxisAggregation(AggregationSpec):
    """Same shape as AggregationSpec but carries its own axis name.

    Used for denominator_pre_aggregation, where aggregation steps are a list
    rather than a dict keyed by axis (order matters, an axis could in theory
    appear only here and not in the main `order`).
    """

    axis: str


class RelativeConfig(BaseModel):
    enabled: bool = True
    split_axis: str
    numerator_when: Any
    denominator_when: Any
    denominator_offset: float = 0.0
    denominator_pre_aggregation: List[AxisAggregation] = Field(default_factory=list)


class ScorePart(BaseModel):
    name: str
    type: str
    relative: Optional[RelativeConfig] = None
    order: List[str] = Field(default_factory=list)
    aggregations: Dict[str, AggregationSpec] = Field(default_factory=dict)


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


class OptimizationConfig(BaseModel):
    score_function: Optional[str] = None
    constraintThreshold: Dict[str, ConstraintThresholdEntry] = Field(default_factory=dict)
    WLgroup: Dict[str, Tuple[int, int]] = Field(default_factory=dict)
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
        )

    def group_defs(self) -> Dict[str, Dict[str, Tuple[int, int]]]:
        return {"WLgroup": self.optimization.WLgroup}


class DvtBudgetCoefEntry(BaseModel):
    a: float
    b: float


# generation -> temperature(as string key, e.g. "-30", "85") -> state -> {a, b}
class DvtBudgetCoefFile(RootModel[Dict[str, Dict[str, Dict[str, DvtBudgetCoefEntry]]]]):
    pass
