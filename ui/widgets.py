# Copyright (c) 2026
"""スコア設計UIの再利用ウィジェット。

各エディタは渡された dict(session の score_file の一部)をその場で書き換える。
検証は呼び出し元の画面が後段で ui.state.validate_* を通して行うので、
エラーメッセージは常にエンジン自身のものになる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import streamlit as st

from scorelib_param.models import COMBINED_SEP, MULTI_OPS, STEP_OPS, TRANSFORM_OPS, UNARY_OPS

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from types import ModuleType

    from streamlit.delta_generator import DeltaGenerator

# ドラッグ&ドロップ並べ替えはソフト依存: コミュニティ製カスタムコンポーネント
# (streamlit-sortables)は Streamlit 本体のメジャー更新で壊れうる。
# 未インストール・故障時は呼び出し側が上下ボタンへフォールバックするので、
# アプリ本体はこれに依存しない(設計書 8-3節)。
try:
    from streamlit_sortables import sort_items as _sort_items
except Exception:  # ImportError またはコンポーネントの破損
    _sort_items = None

HAS_SORTABLES = _sort_items is not None

AXIS_OPS = ["filter", "mean", "sum", "min", "max", "diff", "expr"]

# diff の被演算数: 結果 = a - b の2値ペア
_DIFF_OPERANDS = 2

# コンポーネントの既定スタイルはテーマの primary 色(既定テーマだと真っ赤。
# ユーザ評: 目が痛い)+ 中央揃え(⠿ ハンドルが縦に揃わない)。
# 左揃え+周囲の枠と一段違う半透明グレーに上書きし、ライト/ダーク両テーマで
# 「項目が独立した物」に見えるようにする。
_SORTABLE_STYLE = """
.sortable-item, .sortable-item:hover {
    background-color: rgba(128, 128, 128, 0.2);
    color: var(--text-color);
    border: 1px solid rgba(128, 128, 128, 0.45);
    border-radius: 4px;
    padding-left: 8px;
    text-align: left;
}
"""


def sortable_list(items: list[str], key: str) -> list[str] | None:
    """`items` をドラッグ&ドロップのリストとして描画する。

    (並べ替え後かもしれない)リストを返す。コンポーネントが使えない・
    挙動が怪しいときは None を返し、呼び出し側が上下ボタンへ
    フォールバックする。

    key は項目テキスト由来にしてある: テキストが変わったら(別の場所での編集や
    こちらが適用した並べ替え)、古い内部状態を見せ続けずに新しい並びで
    再マウントさせるため。

    Returns:
        並べ替え後の項目リスト。コンポーネントが使えない・空リスト・
        結果が元の項目集合と一致しない時は None(上下ボタンへの
        フォールバック指示)。

    """
    if _sort_items is None or not items:
        return None
    try:
        result = _sort_items(
            list(items), direction="vertical", custom_style=_SORTABLE_STYLE, key=f"{key}_{abs(hash(tuple(items)))}"
        )
    except Exception:
        return None
    if isinstance(result, list) and sorted(result) == sorted(items):
        return result
    return None


def measure_format(measure_labels: dict[int, str] | None) -> Callable[[object], str]:
    """Measure 軸の値の表示関数を返す。

    表示は「dataName (Measure N)」、名無しの番号は
    「Measure N」(docs/spec_change_dataname_measure.md 6.4節の複合表示。
    選択・保存されるのは常に番号そのもの)。

    Returns:
        Measure 番号を受け取り複合表示の文字列を返す関数
        (selectbox 等の format_func にそのまま渡せる)。

    """
    # キーは selectbox 経由の任意型なので Mapping[Any, str] 扱いにする
    # (注釈だけでは lambda 内で dict[int, str] に絞り込まれたままになるため cast)
    m = cast("Mapping[Any, str]", measure_labels or {})
    return lambda v: f"{m[v]} (Measure {v})" if v in m else f"Measure {v}"


def _axis_format(axis: str, measure_labels: dict[int, str] | None) -> Callable[[object], str]:
    return measure_format(measure_labels) if axis == "Measure" else str


def _options_with_missing(cands: list, current: list) -> tuple[list, list]:
    """選択肢(multiselect 用)と「データに無い既存値」を作る。

    データに無い既存値も選択肢に残す — エディタを開いただけで設定値が黙って
    消えるのを防ぐ(外すかどうかはユーザーが決める)。

    Returns:
        (候補 + データに無い既存値, データに無い既存値)のタプル。

    """
    missing = [v for v in current if v is not None and v not in cands]
    return [*cands, *missing], missing


def _warn_missing_values(axis: str, missing: list) -> None:
    """既存の設定値がデータの候補に無いことを警告する(値は保持されている)。"""
    shown = ", ".join(repr(v) for v in missing)
    st.warning(f"{axis} の値 {shown} はデータにありません(選択から外すと設定から削除されます)")


def parse_scalar(text: str) -> bool | int | float | str:
    """自由入力テキスト → 型付きの軸の値(bool / int / float / str)。

    Returns:
        "true"/"false"(大文字小文字不問)は bool、数値に読めれば int または
        float、それ以外は前後空白を除いた文字列のまま。

    """
    t = text.strip()
    if t.lower() == "true":
        return True
    if t.lower() == "false":
        return False
    for conv in (int, float):
        try:
            return conv(t)
        except ValueError:
            pass
    return t


@dataclass(frozen=True)
class AxisInput:
    """軸の値1つぶんの入力欄の仕様(value_widget に渡す)。

    ラベル・値の候補・表示関数は軸ごとに一緒に決まるので束ねている。
    candidates が空(None 含む)のときは自由入力欄になる。
    """

    label: str
    candidates: list | None
    format_func: Callable[[object], str] = str


def value_widget(
    container: DeltaGenerator | ModuleType,
    axis_input: AxisInput,
    current: object,
    key: str,
) -> object:
    """軸の値1つの入力。

    候補が分かればプルダウン、無ければ自由入力
    (型付きスカラーにパース)。

    Returns:
        プルダウンなら選ばれた候補値、自由入力なら parse_scalar で型付けした
        値。自由入力が空の間は None。

    """
    if axis_input.candidates:
        options = list(axis_input.candidates)
        # 候補に無い既存値は「(データに無し)」の印つきで選択肢に残す —
        # エディタを描画しただけで index=0 の値へ黙って書き換わるのを防ぐ
        # (split_axis の選択肢・multiselect 系と同じ定石。実機で相対化の
        # 分子 True が描画だけで False に化けた 2026-07-31 の再発防止)
        marked = current is not None and current not in options
        if marked:
            options = [*options, current]

        def fmt(v: object) -> str:
            """選択肢を表示する(データに無い既存値には印を付ける)。

            Returns:
                通常の表示文字列(印の対象値のみ「(データに無し)」を付加)。

            """
            base = axis_input.format_func(v)
            return f"{base}(データに無し)" if marked and v == current else base

        index = options.index(current) if current in options else 0
        return container.selectbox(axis_input.label, options, index=index, key=key, format_func=fmt)
    text = container.text_input(axis_input.label, value="" if current is None else str(current), key=key)
    return parse_scalar(text) if text.strip() else None


def dict_selection_row(
    axes: list[str],
    catalog: dict[str, list | None],
    current: object,
    key: str,
    measure_labels: dict[int, str] | None = None,
) -> dict[str, Any]:
    """複合軸の選択1つ: 軸ごとのプルダウンを1行に並べて辞書を返す。

    Returns:
        軸名 → 入力された値 の辞書(全軸ぶん。未入力の軸は None)。

    """
    cols = st.columns(len(axes))
    current = current if isinstance(current, dict) else {}
    return {
        a: value_widget(
            col, AxisInput(a, catalog.get(a), _axis_format(a, measure_labels)), current.get(a), f"{key}_{a}"
        )
        for col, a in zip(cols, axes, strict=False)
    }


def selection_widget(
    axes: list[str],
    catalog: dict[str, list | None],
    current: object,
    key: str,
    measure_labels: dict[int, str] | None = None,
) -> object:
    """単一軸・複合軸どちらにも対応した選択1つぶんの入力。

    Returns:
        単一軸なら値1つ(未入力なら None)、複合軸なら 軸名 → 値 の辞書。

    """
    if len(axes) > 1:
        return dict_selection_row(axes, catalog, current, key, measure_labels)
    return value_widget(
        st, AxisInput(axes[0], catalog.get(axes[0]), _axis_format(axes[0], measure_labels)), current, key
    )


def selection_list_widget(
    axes: list[str],
    catalog: dict[str, list | None],
    values: list,
    key: str,
    measure_labels: dict[int, str] | None = None,
) -> list:
    """可変長の選択リスト(mean/sum/min/max の対象選択と選択セット編集で使用)。

    行の追加・削除ができる。

    Returns:
        現在の選択値のリスト。候補の分かる単一軸なら multiselect の選択値、
        それ以外は行ごとの selection_widget の値(複合軸なら辞書)を並べたもの。

    """
    cands = catalog.get(axes[0])
    if len(axes) == 1 and cands:
        options, missing = _options_with_missing(cands, values)
        if missing:
            _warn_missing_values(axes[0], missing)
        default = [v for v in values if v is not None]
        return st.multiselect(
            f"{axes[0]} の値", options, default=default, key=key, format_func=_axis_format(axes[0], measure_labels)
        )

    out = []
    for i, v in enumerate(values):
        row_cols = st.columns([10, 1])
        with row_cols[0]:
            out.append(selection_widget(axes, catalog, v, f"{key}_r{i}", measure_labels))
        if row_cols[1].button("✕", key=f"{key}_del{i}", help="この行を削除"):
            # 直後の st.rerun() が例外でループごと抜けるため、削除後に反復は続かない
            values.pop(i)
            st.rerun()
    if st.button("+ 行を追加", key=f"{key}_add"):
        values.append(dict.fromkeys(axes) if len(axes) > 1 else None)
        st.rerun()
    return out


def _ref_widget(spec: dict[str, Any], set_names: list[str], key: str) -> None:
    if not set_names:
        # 既存の参照は書き潰さない(描画だけでは設定を変えない — 検証エラーが別途出る)
        st.caption("選択セットが未定義です(画面3で作成)")
        return
    current = spec.get("ref")
    options: list = list(set_names)
    # 存在しないセット名の参照(インポート由来など)も印つきで選択肢に残す —
    # 描画しただけで先頭のセットへ黙って書き換わるのを防ぐ(value_widget と
    # 同じ定石。検証エラーは別途パーツ単位で表示される)
    if current is not None and current not in options:
        options = [*options, current]

    def _label(name: object) -> str:
        """存在しないセット参照の選択肢に印を付けて表示する。

        Returns:
            セット名(現在の参照先が存在しない場合のみ「(存在しません)」を付加)。

        """
        return f"{name}(存在しません)" if name == current and current not in set_names else str(name)

    index = options.index(current) if current in options else 0
    spec["ref"] = st.selectbox("選択セット", options, index=index, key=f"{key}_ref", format_func=_label)


_TRANSFORM_LABELS = {
    "add": "add(足す)",
    "sub": "sub(引く)",
    "mul": "mul(掛ける)",
    "div": "div(割る)",
    "abs": "abs(絶対値)",
    "log": "log(対数)",
}


@dataclass(frozen=True)
class EditorContext:
    """集計指示・相対化エディタに渡す画面側の文脈(読み取り専用)。

    パーツ編集画面(app.py)が組み立てる。catalog は軸名 → 値候補
    (不明なら None)、set_names は参照できる選択セット名。
    `by_candidates` / `by_value_labels` / `weight_set_names` は変換ステップ
    ("__xxx__")と集計時重みの「軸の値ごとの定数(重み)」入力用: 対象軸の
    候補・軸ごとの値ラベル・参照できる重みセット名。`measure_labels` は
    Measure 軸の複合表示「dataName (Measure N)」用の 番号 → dataName 対応。
    """

    catalog: dict[str, list | None]
    set_names: list[str]
    by_candidates: list[str] = field(default_factory=list)
    by_value_labels: dict[str, list] = field(default_factory=dict)
    weight_set_names: list[str] = field(default_factory=list)
    measure_labels: dict[int, str] = field(default_factory=dict)


def _transform_editor(spec: dict[str, Any], op: str, ctx: EditorContext, key: str) -> None:
    """変換ステップ(定数演算)の入力欄。

    全行共通の定数か、by 軸の値ごとの
    定数(グループ別重みなど)かを選ぶ。
    """
    modes = ["定数(全行共通)", "軸の値ごと(重み)"]
    mode = st.radio(
        "適用のしかた",
        modes,
        index=1 if spec.get("by") else 0,
        key=f"{key}_tmode",
        horizontal=True,
    )
    default = 1.0 if op in {"mul", "div"} else 0.0

    if mode == modes[0]:
        spec.pop("by", None)
        spec.pop("ref", None)
        v = spec.get("value")
        spec["value"] = st.number_input(
            "値", value=float(v) if isinstance(v, (int, float)) else default, key=f"{key}_tv"
        )
        return

    if not ctx.by_candidates:
        # 既存の by 軸は書き潰さない(描画だけでは設定を変えない)
        st.caption("値ごとの定数を使うにはグループ定義(画面3)を作成してください")
        return
    cur = spec.get("by")
    by_options: list = list(ctx.by_candidates)
    # 候補に無い既存の by 軸(定義が消された等)も印つきで選択肢に残す —
    # 描画しただけで先頭候補へ黙って書き換わるのを防ぐ(value_widget と同じ定石)
    if cur is not None and cur not in by_options:
        by_options = [*by_options, cur]

    def _by_label(axis: object) -> str:
        """定義の無い by 軸の選択肢に印を付けて表示する。

        Returns:
            軸名(現在の by 軸に定義が無い場合のみ「(定義がありません)」を付加)。

        """
        return f"{axis}(定義がありません)" if axis == cur and cur not in ctx.by_candidates else str(axis)

    spec["by"] = st.selectbox(
        "対象軸(この軸の値ごとに定数を変える)",
        by_options,
        index=by_options.index(cur) if cur in by_options else 0,
        key=f"{key}_tby",
        format_func=_by_label,
        help="通常はグループ派生軸(例: WLgroup)。この軸がまだ列として残っている位置にステップを置いてください",
    )
    if cur is not None and spec["by"] != cur:
        # ユーザーが by 軸を変えた: 旧軸の値ごと辞書は無効なので破棄し、下で
        # 新軸の候補から作り直す(操作起点の整理 — 描画だけでは消さない)
        spec.pop("value", None)

    sources = ["重みセット(ref)", "直接入力"]
    src = st.radio(
        "重みの指定",
        sources,
        index=0 if spec.get("ref") else 1,
        key=f"{key}_tsrc",
        horizontal=True,
    )
    if src == sources[0]:
        if not ctx.weight_set_names:
            # 既存の参照は書き潰さない(描画だけでは設定を変えない)
            st.caption("重みセットが未定義です(画面3のグループ定義で作成、または設定jsoncの WLgroupWeight を読み込み)")
            return
        spec.pop("value", None)
        cur_ref = spec.get("ref")
        ref_options: list = list(ctx.weight_set_names)
        # 存在しない重みセット参照も印つきで選択肢に残す(value_widget と同じ定石 —
        # 描画しただけで先頭のセットへ黙って書き換わるのを防ぐ)
        if cur_ref is not None and cur_ref not in ref_options:
            ref_options = [*ref_options, cur_ref]
        spec["ref"] = st.selectbox(
            "重みセット",
            ref_options,
            index=ref_options.index(cur_ref) if cur_ref in ref_options else 0,
            key=f"{key}_tref",
            format_func=lambda n, _cur=cur_ref, _known=ctx.weight_set_names: (
                f"{n}(存在しません)" if n == _cur and n not in _known else str(n)
            ),
        )
        return

    spec.pop("ref", None)
    by_axis = spec.get("by")
    labels = (ctx.by_value_labels.get(by_axis) if isinstance(by_axis, str) else None) or []
    if not per_value_dict_editor(spec, "value", labels, default, f"{key}_tw"):
        st.caption("この軸の値の候補が分かりません。グループ派生軸を選ぶか、jsonc で直接記入してください")


def per_value_dict_editor(spec: dict[str, Any], field: str, labels: list[Any], default: float, key: str) -> bool:
    """「値ごとの数値」辞書(集計時重み・変換の by 別定数)の共通編集欄。

    描画だけでは設定を変えない: 既存辞書は(候補に無いキーも印つきで)保持し、
    触っていない新規キー(中立値のまま)は足さない。辞書がまだ無いとき
    (=ユーザーがこのモードを選んだ直後)にだけ全候補を中立値で初期化する
    (エンジンは全値のカバーを要求するため)。

    Returns:
        入力欄を描画したら True。候補も既存辞書も無く何も描画しなかったら
        False(呼び出し側が案内文を出す)。

    """
    v = spec.get(field)
    current = v if isinstance(v, dict) else dict.fromkeys(labels, default)
    if labels:
        weights = {}
        for i, label in enumerate(labels):
            v = current.get(label)
            entered = st.number_input(
                str(label),
                value=float(v) if isinstance(v, (int, float)) else default,
                key=f"{key}{i}",
            )
            if label in current or entered != default:
                weights[label] = entered
        # 候補に無い既存キーも印つきで残す(描画だけで辞書から消えるのを防ぐ)
        for i, (k, v) in enumerate((k, v) for k, v in current.items() if k not in labels):
            weights[k] = st.number_input(
                f"{k}(データに無し)",
                value=float(v) if isinstance(v, (int, float)) else default,
                key=f"{key}x{i}",
            )
        spec[field] = weights
        return True
    if current:
        # 値の候補が分からない軸: 既存の辞書のキーだけ編集できる
        spec[field] = {
            k: st.number_input(str(k), value=float(v), key=f"{key}k{i}") for i, (k, v) in enumerate(current.items())
        }
        return True
    return False


def _weight_by_value_editor(spec: dict[str, Any], labels: list[Any], key: str) -> None:
    """集計時重みの「値ごとに入力」欄(spec['weight'] を辞書で編集する — per_value_dict_editor 共用)。"""
    if not per_value_dict_editor(spec, "weight", labels, 1.0, f"{key}_wv"):
        st.caption("この軸の値の候補が分かりません。重みセットを使うか、jsonc で直接記入してください")


def _agg_weight_editor(
    spec: dict[str, Any],
    labels: list[Any],
    weight_set_names: list[str],
    key: str,
) -> None:
    """集計時重み(任意)の入力欄。

    この軸を潰す直前に、軸の値ごとの重みを値へ乗じてから
    集計する(例: WLgroup の重み付き worst)。正規化された加重平均ではない
    (mean なら mean(重み * 値))。タイミングを制御したい場合は変換ステップ
    ("__xxx__" + by)を使う。
    """
    modes = ["なし", "重みセット(ref)", "値ごとに入力", "定数1つ"]
    if spec.get("weight_ref"):
        idx = 1
    elif isinstance(spec.get("weight"), dict):
        idx = 2
    elif spec.get("weight") is not None:
        idx = 3
    else:
        idx = 0
    mode = st.radio(
        "重み(集計の前に値へ掛ける)",
        modes,
        index=idx,
        key=f"{key}_wmode",
        horizontal=True,
        help="この軸の値ごとの重みを掛けてから集計します。"
        "掛けるタイミングを自分で制御したい場合は変換ステップ(__xxx__)を使ってください",
    )
    if mode == modes[0]:
        spec.pop("weight", None)
        spec.pop("weight_ref", None)
        return
    if mode == modes[1]:
        spec.pop("weight", None)
        if not weight_set_names:
            # 既存の参照は書き潰さない(描画だけでは設定を変えない)
            st.caption(
                "重みセットが未定義です(画面3のグループ定義で作成、または設定jsoncの WLgroupWeight / weightSets)"
            )
            return
        cur = spec.get("weight_ref")
        wref_options: list = list(weight_set_names)
        # 存在しない重みセット参照も印つきで選択肢に残す(value_widget と同じ定石)
        if cur is not None and cur not in wref_options:
            wref_options = [*wref_options, cur]
        spec["weight_ref"] = st.selectbox(
            "重みセット",
            wref_options,
            index=wref_options.index(cur) if cur in wref_options else 0,
            key=f"{key}_wref",
            format_func=lambda n, _cur=cur, _known=weight_set_names: (
                f"{n}(存在しません)" if n == _cur and n not in _known else str(n)
            ),
        )
        return
    spec.pop("weight_ref", None)
    if mode == modes[3]:
        v = spec.get("weight")
        spec["weight"] = st.number_input(
            "重み", value=float(v) if isinstance(v, (int, float)) else 1.0, key=f"{key}_wc"
        )
        return
    _weight_by_value_editor(spec, labels, key)


def _op_widget(entry: str, spec: dict[str, Any], key: str) -> str:
    """演算(op)の選択欄。op が変わったら op 固有のフィールドを掃除する。

    Returns:
        選ばれた op(`spec['op']` にも反映済み)。

    """
    is_virtual = entry.startswith("__")
    ops: list[str] = list(STEP_OPS) if is_virtual else AXIS_OPS
    raw_op = spec.get("op")
    cur_op = raw_op if raw_op in ops else ops[0]

    def _fmt(o: str) -> str:
        return _TRANSFORM_LABELS.get(o, o)

    op = st.selectbox(
        "op",
        ops,
        index=ops.index(cur_op),
        key=f"{key}_op",
        format_func=_fmt if is_virtual else str,
    )
    if op != spec.get("op") and not (op in TRANSFORM_OPS and spec.get("op") in TRANSFORM_OPS):
        # op が変わった: op 固有のフィールドが残らないよう掃除する
        # ("axis" だけは残す: 事前集計ステップは軸名を spec 内に持つため。
        # 変換op同士の切り替えでは by/value/ref を保つ — 演算だけ変える操作なので)
        axis_field = spec.get("axis")
        spec.clear()
        if axis_field is not None:
            spec["axis"] = axis_field
    spec["op"] = op
    return op


def _unary_editor(spec: dict[str, Any], op: str, key: str) -> None:
    """定数を取らない行単位の関数(abs/log)。log は床(floor)だけを入力する。"""
    for k in ("value", "by", "ref"):
        spec.pop(k, None)
    if op == "log":
        v = spec.get("floor")
        spec["floor"] = st.number_input(
            "floor",
            value=float(v) if isinstance(v, (int, float)) else 1e-6,
            format="%.1e",
            key=f"{key}_floor",
            help="log(max(|x|, floor)) を計算します。0 や負の値でも発散しないための床です",
        )
    else:
        spec.pop("floor", None)


def _filter_editor(spec: dict[str, Any], axes: list[str], ctx: EditorContext, key: str) -> None:
    """対象値で行を絞る filter op の入力欄(Measure 軸なら dataName 注記も付ける)。"""
    from ui import state as ui_state

    spec.pop("ref", None)
    cands = ctx.catalog.get(axes[0])
    if len(axes) == 1 and cands:
        # 候補が分かる単一軸は複数選択可(複数 = is_in: 選んだ値の行を
        # すべて残し、後段の集計に複製として流す)
        cur = spec.get("value")
        cur_list = cur if isinstance(cur, list) else ([cur] if cur is not None else [])
        options, missing = _options_with_missing(cands, cur_list)
        if missing:
            _warn_missing_values(axes[0], missing)
        picked = st.multiselect(
            f"{axes[0]} の値(複数選ぶと該当する値の行をすべて残します)",
            options,
            default=cur_list,
            key=f"{key}_fv",
            format_func=_axis_format(axes[0], ctx.measure_labels),
        )
        spec["value"] = picked[0] if len(picked) == 1 else picked
    else:
        spec["value"] = selection_widget(axes, ctx.catalog, spec.get("value"), f"{key}_fv", ctx.measure_labels)
    if axes == ["Measure"]:
        ui_state.annotate_measure_labels(spec, ctx.measure_labels)


def _diff_editor(spec: dict[str, Any], axes: list[str], ctx: EditorContext, key: str) -> None:
    """2値の差を取る diff op(結果 = a - b)の入力欄。直接指定と選択セット参照を選べる。"""
    source = st.radio(
        "選択方法",
        ["直接指定", "選択セット(ref)"],
        key=f"{key}_dsrc",
        horizontal=True,
        index=1 if spec.get("ref") else 0,
    )
    if source == "選択セット(ref)":
        spec.pop("value", None)
        _ref_widget(spec, ctx.set_names, key)
    else:
        spec.pop("ref", None)
        raw = spec.get("value")
        v = raw if isinstance(raw, list) and len(raw) == _DIFF_OPERANDS else [None, None]
        st.caption("結果 = a - b")
        a = selection_widget(axes, ctx.catalog, v[0], f"{key}_da", ctx.measure_labels)
        b = selection_widget(axes, ctx.catalog, v[1], f"{key}_db", ctx.measure_labels)
        spec["value"] = [a, b]


def _multi_op_editor(spec: dict[str, Any], axes: list[str], ctx: EditorContext, key: str) -> None:
    """mean/sum/min/max の対象選択(全値・値を選択・選択セット)と集計時重みの入力欄。"""
    modes = ["全値", "値を選択", "選択セット(ref)"]
    if spec.get("ref"):
        mode_idx = 2
    elif spec.get("value") is not None:
        mode_idx = 1
    else:
        mode_idx = 0
    mode = st.radio("対象", modes, index=mode_idx, key=f"{key}_mode", horizontal=True)
    if mode == "全値":
        spec.pop("value", None)
        spec.pop("ref", None)
    elif mode == "選択セット(ref)":
        spec.pop("value", None)
        _ref_widget(spec, ctx.set_names, key)
    else:
        spec.pop("ref", None)
        if not isinstance(spec.get("value"), list):
            spec["value"] = []
        # dict が持つリストそのものを渡す: 行の追加・削除の変更が
        # st.rerun() をまたいで生き残るように
        spec["value"] = selection_list_widget(axes, ctx.catalog, spec["value"], f"{key}_mv", ctx.measure_labels)
    if len(axes) == 1:
        # 集計時重み(複合軸は jsonc 直接記入のみ対応)。値ラベルは
        # by_value_labels 優先: グループ派生軸(WLgroup等)の名前は
        # catalog(データ軸の値)には無く、そちらにしか入っていない
        labels = ctx.by_value_labels.get(axes[0]) or ctx.catalog.get(axes[0]) or []
        _agg_weight_editor(spec, labels, ctx.weight_set_names, key)


def agg_editor(entry: str, spec: dict[str, Any], ctx: EditorContext, key: str) -> None:
    """Order エントリごとの集計指示エディタ(設計書 画面2)。

    選んだ op に関係する入力欄だけを出すので、value/values の混同は
    UI 上は構造的に起きない。`spec` をその場で書き換える。
    `ctx` は画面側が組み立てた文脈(EditorContext のフィールド説明を参照)。
    """
    axes = entry.split(COMBINED_SEP)
    op = _op_widget(entry, spec, key)

    if op in UNARY_OPS:
        _unary_editor(spec, op, key)
    elif op in TRANSFORM_OPS:
        _transform_editor(spec, op, ctx, key)
    elif op == "filter":
        _filter_editor(spec, axes, ctx, key)
    elif op == "diff":
        _diff_editor(spec, axes, ctx, key)
    elif op in MULTI_OPS:
        _multi_op_editor(spec, axes, ctx, key)
    elif op == "expr":
        spec["expr"] = st.text_input(
            "式",
            value=spec.get("expr") or "",
            key=f"{key}_expr",
            help="values = この軸の全値のリスト。by[値] で特定の値を参照。"
            "例: max(values) - min(values), by['R2A'] + by['A2R']",
        )


def _relative_toggle(part: dict[str, Any], ctx: EditorContext, key: str) -> dict[str, Any] | None:
    """相対化の ON/OFF チェックボックス。

    切り替え時の `order` の付け替え(ui.state の enable/disable)を経て
    st.rerun() する。

    Returns:
        ON のとき part['relative'] の dict。OFF のとき None。

    """
    from ui import state as ui_state

    prev_enabled = part.get("relative") is not None
    enabled = st.checkbox("相対化する(基準測定との比を取る)", value=prev_enabled, key=f"{key}_on")
    if not enabled:
        if prev_enabled:
            restored = ui_state.disable_relative(part, ctx.catalog)
            if restored:
                st.toast(f"'{restored}' を order に戻しました(filter {part['aggregations'][restored].get('value')})")
            st.rerun()
        return None
    if not prev_enabled:
        ui_state.enable_relative(part, ctx.catalog)
        st.rerun()
    return part["relative"]


def _relative_split_row(part: dict[str, Any], rel: dict[str, Any], ctx: EditorContext, key: str) -> None:
    """Split 軸の選択と分子/分母の値の入力(1行3列)。

    split 軸を変えたときは `order` の付け替え(ui.state)を経て st.rerun()
    する。Measure 軸なら選んだ番号の dataName を labels 注記として残す。
    """
    from ui import state as ui_state

    axis_opts = [a for a in ctx.catalog if a != "InBatchEpoch"]
    # split_axis は保存形式上は任意型になりうるが、UI が書くのは常に軸名(str)
    cur_split = cast("str | None", rel.get("split_axis"))
    if cur_split and cur_split not in axis_opts:
        axis_opts = [cur_split, *axis_opts]
    c1, c2, c3 = st.columns(3)
    new_split = c1.selectbox(
        "split_axis(分子/分母を分ける軸)",
        axis_opts,
        index=axis_opts.index(cur_split) if cur_split in axis_opts else 0,
        key=f"{key}_sa",
        help="通常は Measure(測定番号)。Measure 列の無い集計済み type では任意の軸(Chip 等)で割り算・引き算ができます",
    )
    if new_split != rel.get("split_axis"):
        ui_state.change_split_axis(part, new_split, ctx.catalog)
        st.rerun()
    fmt = _axis_format(new_split, ctx.measure_labels)
    cands = ctx.catalog.get(new_split)
    rel["numerator_when"] = value_widget(
        c2, AxisInput("分子側の値(評価側)", cands, fmt), rel.get("numerator_when"), f"{key}_num"
    )
    rel["denominator_when"] = value_widget(
        c3, AxisInput("分母側の値(基準側)", cands, fmt), rel.get("denominator_when"), f"{key}_den"
    )
    if new_split == "Measure":
        ui_state.annotate_measure_labels(rel, ctx.measure_labels)
    else:
        rel.pop("labels", None)


def _relative_mode_row(rel: dict[str, Any], key: str) -> None:
    """mode(ratio/diff)と ratio 用 offset の入力(1行2列)。"""
    c4, c5 = st.columns(2)
    modes = ["ratio", "diff"]
    cur_mode = rel.get("mode", "ratio")
    mode_opts: list = list(modes)
    # 不正な既存値も印つきで選択肢に残す(value_widget と同じ定石。検証エラーは別途表示)
    if cur_mode not in mode_opts:
        mode_opts = [*mode_opts, cur_mode]
    new_mode = c4.selectbox(
        "mode",
        mode_opts,
        index=mode_opts.index(cur_mode),
        key=f"{key}_mode",
        format_func=lambda m, _cur=cur_mode: f"{m}(不正な値)" if m == _cur and m not in modes else str(m),
        help="ratio: (分子+offset)/(分母+offset)　diff: 分子 - 分母",
    )
    if new_mode != cur_mode:
        # ユーザーが変えたときだけ書く(offset の削除も同時)。描画だけでは
        # キーを作らない・消さない
        rel["mode"] = new_mode
        if new_mode != "ratio":
            rel.pop("denominator_offset", None)
    if new_mode == "ratio":
        offset = c5.number_input(
            "offset(分子分母の両方に加算)", value=float(rel.get("denominator_offset", 0)), key=f"{key}_off"
        )
        if "denominator_offset" in rel or offset != 0.0:
            rel["denominator_offset"] = offset


def _denominator_pre_agg_editor(rel: dict[str, Any], ctx: EditorContext, key: str) -> None:
    """分母側だけの事前集計(denominator_pre_aggregation)の編集 expander。"""
    with st.expander(
        "分母の事前集計 (denominator_pre_aggregation)", expanded=bool(rel.get("denominator_pre_aggregation"))
    ):
        st.caption(
            "分母(基準測定)側だけに、比を取る前の集計を適用します(例: 分母はWL平均、分子はWLごと)。"
            "opごとの対象選択も通常の集計指示と同じように使えます"
        )
        # 描画だけでは rel を変えない: キーは作らない(追加ボタンで作る)・
        # 空になっても消さない(空リストは設定として無害で、消すと「描画した
        # だけで設定が変わる」不変条件が崩れる)
        steps = rel.get("denominator_pre_aggregation") or []
        known_axes = sorted(ctx.catalog)
        for i, step in enumerate(steps):
            c_axis, c_del = st.columns([8, 1])
            axis_opts: list = list(known_axes)
            cur_axis = step.get("axis")
            # カタログに無い既存の軸も印つきで選択肢に残す(value_widget と同じ定石)
            if cur_axis is not None and cur_axis not in axis_opts:
                axis_opts = [*axis_opts, cur_axis]
            step["axis"] = c_axis.selectbox(
                "軸",
                axis_opts,
                index=axis_opts.index(cur_axis) if cur_axis in axis_opts else 0,
                key=f"{key}_pre{i}_axis",
                format_func=lambda a, _cur=cur_axis, _known=known_axes: (
                    f"{a}(軸がありません)" if a == _cur and a not in _known else str(a)
                ),
            )
            if c_del.button("✕", key=f"{key}_pre{i}_del", help="この事前集計を削除"):
                # 直後の st.rerun() が例外でループごと抜けるため、削除後に反復は続かない
                steps.pop(i)
                if not steps:
                    # ユーザーの削除操作で空になったときだけキーごと消す(描画では消さない)
                    rel.pop("denominator_pre_aggregation", None)
                st.rerun()
            agg_editor(step["axis"], step, ctx, key=f"{key}_pre{i}")
            st.divider()
        # カタログが空(設定のみ編集で軸を1つも持たないパーツ)では初期軸を
        # 選べないため無効化する(押すと min() が空列で落ちる)
        if st.button("+ 事前集計を追加", key=f"{key}_pre_add", disabled=not ctx.catalog):
            rel.setdefault("denominator_pre_aggregation", []).append({"axis": min(ctx.catalog), "op": "mean"})
            st.rerun()


def relative_editor(part: dict[str, Any], ctx: EditorContext, key: str) -> None:
    """相対化ブロックのエディタ。

    part['relative'] の存在=ON。OFF にしたら split 軸を `order` へ復帰させる
    (放置するとエンジンが暗黙に集約して分子と分母の行が混ざる)。
    ON にしたとき・split 軸を変えたときは対称に、新しい split 軸を `order`
    から外す。

    split 軸は任意の軸から選べる(旧仕様の Override 限定は廃止 —
    docs/spec_change_dataname_measure.md)。基本形は Measure 番号での指定で、
    dataName が分かる番号は「dataName (Measure N)」と表示し、選んだ番号の
    dataName を labels 注記として設定に残す。
    """
    rel = _relative_toggle(part, ctx, key)
    if rel is None:
        return
    _relative_split_row(part, rel, ctx, key)
    _relative_mode_row(rel, key)
    _denominator_pre_agg_editor(rel, ctx, key)
