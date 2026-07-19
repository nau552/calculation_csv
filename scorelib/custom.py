"""User-defined score-part functions (type="custom").

A python-literate user writes functions in custom_parts.py — one function per
score part, returning one finite scalar. The file lives at a FIXED,
SVN-versioned location (repository root, next to the scorelib package): the
config never carries a path to it, because a config-supplied path would let
any experiment config execute arbitrary code on the optimization server.
Editing custom functions therefore goes through SVN, which is the intended
gate (review + history). The design UI loads the same file (bundled in the
zip the GUI serves) so the function list and test computation match the
revision that will run.

Function contract::

    def my_score(ctx) -> float:
        df = pl.read_csv(ctx.data_dir / "FBC.csv")
        ...
        return value

ctx is a CustomContext: data_dir (Path), generation (str | None),
group_defs (name -> GroupDef), params (the part's params dict).
"""
from __future__ import annotations

import importlib.util
import inspect
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .models import GroupDef, ScorePart

DEFAULT_FILENAME = "custom_parts.py"


@dataclass
class CustomContext:
    data_dir: Path
    generation: Optional[str] = None
    group_defs: Dict[str, GroupDef] = field(default_factory=dict)
    params: Dict[str, Any] = field(default_factory=dict)


def default_custom_parts_path() -> Path:
    """custom_parts.py at the repository root (next to the scorelib package)."""
    return Path(__file__).resolve().parent.parent / DEFAULT_FILENAME


def load_custom_module(path: str | Path):
    """Import the user functions file. This EXECUTES its top-level code —
    acceptable because the file is SVN-reviewed, never user-uploaded input."""
    path = Path(path)
    spec = importlib.util.spec_from_file_location("scorelib_custom_parts", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot import custom parts file: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def list_custom_functions(module) -> list[str]:
    """Public functions defined in the module itself (imports excluded)."""
    return sorted(
        name
        for name, fn in vars(module).items()
        if not name.startswith("_")
        and inspect.isfunction(fn)
        and fn.__module__ == module.__name__
    )


def compute_custom_part(
    score_part: ScorePart,
    module,
    ctx: CustomContext,
) -> float:
    fname = score_part.function or score_part.name
    fn = getattr(module, fname, None)
    if not callable(fn):
        raise ValueError(
            f"custom function '{fname}' (score part '{score_part.name}') not found in "
            f"{getattr(module, '__file__', DEFAULT_FILENAME)} — available: {list_custom_functions(module)}"
        )
    try:
        value = fn(ctx)
    except Exception as err:
        raise ValueError(f"custom function '{fname}' raised: {err}") from err
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(
            f"custom function '{fname}' must return one finite number, got {value!r}"
        )
    return float(value)
