"""Example custom score-part functions used by the tests (the shape users
would register in SVN as custom_parts.py at the repository root)."""
import polars as pl


def fixed_value(ctx):
    return 42.0


def mean_fbc_plus_offset(ctx):
    """Reads a csv directly — the whole point of custom parts is free-form
    aggregation over any files in the data directory."""
    df = pl.read_csv(ctx.data_dir / "FBC.csv")
    return float(df["FBC"].mean()) + float(ctx.params.get("offset", 0))


def broken_returns_nan(ctx):
    return float("nan")


def _private_helper(ctx):  # must NOT appear in the function list
    return 0.0
