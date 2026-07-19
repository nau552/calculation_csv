"""テストが使う自作スコアパーツ関数の例（ユーザがリポジトリ直下の
custom_parts.py として SVN に登録するのと同じ形）。"""
import polars as pl


def fixed_value(ctx):
    return 42.0


def mean_fbc_plus_offset(ctx):
    """csv を直接読む — データディレクトリ内の任意のファイルを自由に
    集計できるのが custom パーツの存在意義そのもの。"""
    df = pl.read_csv(ctx.data_dir / "FBC.csv")
    return float(df["FBC"].mean()) + float(ctx.params.get("offset", 0))


def broken_returns_nan(ctx):
    return float("nan")


def _private_helper(ctx):  # must NOT appear in the function list
    return 0.0
