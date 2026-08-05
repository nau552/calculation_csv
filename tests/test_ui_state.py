# Copyright (c) 2026
# ruff: file-ignore[import-outside-top-level] そのテストだけが使う依存は関数内で import する
# ruff: file-ignore[magic-value-comparison] テストの期待値は生の数値で書く(定数名に隠すと期待値が読めない)
# ruff: file-ignore[float-equality-comparison] 期待値は2進浮動小数で正確に表せる値で、厳密一致そのものを検査する
"""ui.state のテスト: Streamlit UI の背後にある純粋な編集ロジック。"""

import shutil
from pathlib import Path
from typing import Any, cast

import pytest

from scorelib_param.cli import compute_score_part
from scorelib_param.introspect import axis_catalog
from scorelib_param.models import ScorePart
from ui import state

# mini データに実在する全 type(値列ルールで検出される。test_introspect と対)
MINI_TYPES = ["FBC", "KLD", "PROGLOOP", "PROGSTATUS", "dVthSGWLD", "tPROG", "tR"]


@pytest.fixture
def catalog(data_dir_mini: Path) -> dict[str, list | None]:
    """FBC の軸カタログ(mini データ)を返す。

    Returns:
        軸名をキーに、その軸の取りうる値一覧(列挙しない軸は None)を並べた dict。

    """
    return axis_catalog(data_dir_mini, "FBC")


@pytest.fixture
def sf() -> dict[str, Any]:
    """空の ScoreFile dict を返す。

    Returns:
        パーツも式も入っていない初期状態の ScoreFile dict。

    """
    return state.empty_score_file()


# ------------------------------------------------------------------- skeleton


def test_skeleton_is_valid_without_relative_preset(catalog: dict[str, list | None]) -> None:
    """v1: 相対化プリセットは無し — 旧「Read_Override があれば自動ON」は廃止。

    (docs/spec_change_dataname_measure.md 9節)
    """
    part = state.part_skeleton("p1", "FBC", catalog)
    assert "relative" not in part
    assert part["order"][0] == "Measure"  # 「どの測定か」の選択が先頭
    assert "InBatchEpoch" not in part["order"]
    assert "DataName" not in part["order"]  # DataName は Measure の表示名の扱い
    # Label/Override は Measure が一意に決める測定メタデータ → 雛形に入れない
    assert not any(a.endswith(("_Label", "_Override")) for a in part["order"])
    assert part["order"][-3:] == ["Board", "Chip", "Block"]
    assert state.validate_part(part) == []


def test_skeleton_default_ops(catalog: dict[str, list | None]) -> None:
    """FBC 雛形の既定op: Measure は filter 0、State は filter R2A、WL は mean になることを検証する。"""
    part = state.part_skeleton("p1", "FBC", catalog)
    aggs = part["aggregations"]
    assert aggs["Measure"] == {"op": "filter", "value": 0}  # 識別子軸: 平均しない
    assert aggs["State"] == {"op": "filter", "value": "R2A"}
    assert aggs["WL"] == {"op": "mean"}


def test_skeleton_keeps_label_axes_for_types_without_measure() -> None:
    """Measure 軸の無いカタログ(集計済み type 等)では従来どおり全軸が雛形に入る。

    (Label/Override 除外は Measure がある場合だけの規則)
    """
    catalog = {"Read_Override": [False, True], "Board": [0, 1]}
    part = state.part_skeleton("p", "X", catalog)
    assert "Read_Override" in part["order"]


def test_skeleton_computes_as_is(data_dir_mini: Path, catalog: dict[str, list | None]) -> None:
    """雛形の存在意義そのもの: 一切編集せずに計算が通ること。"""
    part = ScorePart.model_validate(state.part_skeleton("p1", "FBC", catalog))
    value = compute_score_part(data_dir_mini, part)
    assert isinstance(value, float)


def test_kld_skeleton_standard_computation(data_dir_mini: Path) -> None:
    """KLD の type 別雛形: Board/Chip mean → log(床 1e-6) → 0.1 重みの SGWLD 総和。そのまま計算が通る。"""
    part = state.part_skeleton("p", "KLD", axis_catalog(data_dir_mini, "KLD"))
    assert part["order"] == ["Board", "Chip", "__log__", "SGWLD"]
    assert part["aggregations"]["__log__"] == {"op": "log", "floor": 1e-6}
    assert part["aggregations"]["SGWLD"] == {"op": "sum", "weight": 0.1}
    value = compute_score_part(data_dir_mini, ScorePart.model_validate(part))
    assert isinstance(value, float)


def test_dvth_skeleton_excludes_sg_elements(data_dir_mini: Path) -> None:
    """DVthSGWLD の type 別雛形: mean → abs → SG系4要素を除く8要素の総和。"""
    catalog = axis_catalog(data_dir_mini, "dVthSGWLD")
    part = state.part_skeleton("p", "dVthSGWLD", catalog)
    assert part["order"] == ["Board", "Chip", "Block", "__abs__", "SGWLD"]
    assert part["aggregations"]["__abs__"] == {"op": "abs"}
    sgwld = part["aggregations"]["SGWLD"]
    assert sgwld["op"] == "sum"
    assert set(sgwld["value"]) == set(cast("list[str]", catalog["SGWLD"])) - {"SGSB", "SGS", "SGD", "SGDT"}
    assert len(sgwld["value"]) == 8
    value = compute_score_part(data_dir_mini, ScorePart.model_validate(part))
    assert isinstance(value, float)


def test_typed_skeleton_falls_back_without_sgwld() -> None:
    """SGWLD 軸が無いデータでは KLD でも汎用雛形にフォールバックする。"""
    part = state.part_skeleton("p", "KLD", {"Board": [0, 1], "Chip": [0, 1]})
    assert part["order"] == ["Board", "Chip"]
    assert "__log__" not in part["aggregations"]


def test_default_split_axis_priority(catalog: dict[str, list | None]) -> None:
    """既定 split 軸: Measure > Param > Read_Override > 他の Override > 先頭の軸。"""
    assert state.default_split_axis(catalog) == "Measure"
    no_measure = {a: c for a, c in catalog.items() if a != "Measure"}
    assert state.default_split_axis(no_measure) == "Read_Override"
    progloop: dict[str, list | None] = {"Board": [0, 1], "Param": ["ROM", "Opt"], "WL": [0, 1]}
    assert state.default_split_axis(progloop) == "Param"
    aggregated: dict[str, list | None] = {"Board": [0, 1], "Chip": [0, 1]}  # 集計済み type(4b回答の形)
    assert state.default_split_axis(aggregated) == "Board"


# ------------------------------------------------- relative on/off and order


def test_enable_relative_defaults_to_measure_split(catalog: dict[str, list | None]) -> None:
    """相対化ONの既定: split=Measure・分母0/分子1になり、Measure が order と aggregations から消えることを検証する。"""
    part = state.part_skeleton("p", "FBC", catalog)
    state.enable_relative(part, catalog)
    rel = part["relative"]
    assert rel["split_axis"] == "Measure"
    # 位置既定: 候補の先頭 = 分母(基準)、2番目 = 分子(評価)
    assert rel["denominator_when"] == 0
    assert rel["numerator_when"] == 1
    assert "Measure" not in part["order"]  # 相対化が消費するため order には無い
    assert "Measure" not in part["aggregations"]
    assert state.validate_part(part) == []


def test_enable_relative_computes_as_is(data_dir_mini: Path, catalog: dict[str, list | None]) -> None:
    """相対化ONの既定値(Measure 1/0)のまま計算が通ること。"""
    part = state.part_skeleton("p", "FBC", catalog)
    state.enable_relative(part, catalog)
    value = compute_score_part(data_dir_mini, ScorePart.model_validate(part))
    assert isinstance(value, float)


def test_enable_relative_param_split_computes(data_dir_mini: Path) -> None:
    """Param 軸を持つ Measure 無し type(PROGLOOP)の相対化既定で、そのまま計算が通ることを検証する。

    既定は split=Param・分母 ROM(基準パラ)・分子 Opt(提案パラ)
    (2026-07-29 ユーザー確認: この系の相対化は基本 Param)。
    """
    catalog = axis_catalog(data_dir_mini, "PROGLOOP")
    part = state.part_skeleton("p", "PROGLOOP", catalog)
    state.enable_relative(part, catalog)
    rel = part["relative"]
    assert rel["split_axis"] == "Param"
    assert rel["denominator_when"] == "ROM"
    assert rel["numerator_when"] == "Opt"
    value = compute_score_part(data_dir_mini, ScorePart.model_validate(part))
    assert isinstance(value, float)


def test_enable_relative_on_aggregated_type_uses_any_axis() -> None:
    """Measure 列の無い集計済み type: split は任意軸(先頭)から。"""
    catalog: dict[str, list | None] = {"Board": [0, 1], "Chip": [0, 1, 2, 3]}
    part = state.part_skeleton("p", "SUMMARY", catalog)
    state.enable_relative(part, catalog)
    rel = part["relative"]
    assert rel["split_axis"] == "Board"
    assert (rel["numerator_when"], rel["denominator_when"]) == (1, 0)


def test_enable_relative_without_candidates_shows_validation_error() -> None:
    """候補の無い軸が split になった場合、分子/分母はユーザ入力まで None になる。

    エンジンの「both numerator_when and denominator_when」エラーが表示される。
    """
    catalog = {"Foo": None, "Board": [0]}
    part = state.part_skeleton("p", "X", catalog)
    state.enable_relative(part, catalog)
    assert part["relative"]["numerator_when"] is None
    problems = state.validate_part(part)
    assert any("numerator_when" in p for p in problems)


def test_disable_relative_restores_split_axis(catalog: dict[str, list | None]) -> None:
    """相対化OFFで split 軸 Measure が order へ戻り、filter の安全な既定つきで検証も通ることを検証する。"""
    part = state.part_skeleton("p", "FBC", catalog)
    state.enable_relative(part, catalog)
    assert "Measure" not in part["order"]
    restored = state.disable_relative(part, catalog)
    assert restored == "Measure"
    assert "relative" not in part
    assert "Measure" in part["order"]
    # 識別子軸の安全なデフォルト(先頭番号の filter)で復帰する
    assert part["aggregations"]["Measure"] == {"op": "filter", "value": 0}
    assert state.validate_part(part) == []


def test_change_split_axis_swaps_order_membership_and_resets_sides(catalog: dict[str, list | None]) -> None:
    """相対化の split 軸を変更すると新旧軸の order 所属が入れ替わることを検証する。

    分子/分母は新しい軸の候補の位置から初期化し直される。
    """
    part = state.part_skeleton("p", "FBC", catalog)
    state.enable_relative(part, catalog)
    state.change_split_axis(part, "Read_Override", catalog)
    rel = part["relative"]
    assert rel["split_axis"] == "Read_Override"
    # 分子/分母は新しい軸の候補 [False, True] から位置で初期化し直される
    assert rel["numerator_when"] is True
    assert rel["denominator_when"] is False
    assert "Read_Override" not in part["order"]
    assert "Measure" in part["order"]  # 旧 split 軸は order へ戻る
    # 逆へ戻すと Read_Override は order へ復帰する
    state.change_split_axis(part, "Measure", catalog)
    assert "Read_Override" in part["order"]
    assert "Measure" not in part["order"]


def test_change_split_axis_drops_stale_labels(catalog: dict[str, list | None]) -> None:
    """相対化の split 軸を変更したら古い Measure 用の labels 注記が消えることを検証する。"""
    part = state.part_skeleton("p", "FBC", catalog)
    state.enable_relative(part, catalog)
    part["relative"]["labels"] = {"1": "evaluation_param_read_level_1"}
    state.change_split_axis(part, "Read_Override", catalog)
    assert "labels" not in part["relative"]


def test_disable_relative_removes_explicit_relative_step(catalog: dict[str, list | None]) -> None:
    """機能の組み合わせ: UIは __relative__ を order に明示配置できる。

    相対化をOFFにしたらそれも除去されること(相対化設定なしの
    __relative__ は検証エラーになるため)。
    """
    part = state.part_skeleton("p", "FBC", catalog)
    state.enable_relative(part, catalog)
    part["order"].insert(0, "__relative__")
    state.disable_relative(part, catalog)
    assert "__relative__" not in part["order"]
    assert state.validate_part(part) == []


def test_drop_stale_virtual_steps_on_type_change(catalog: dict[str, list | None]) -> None:
    """パーツ type の変更で不整合になった仮想ステップだけが order から除去されることを検証する。"""
    part = state.part_skeleton("p", "FBC", catalog)
    part["order"].insert(0, "__dvtbudget__")
    part["type"] = "dVtBudget"
    assert state.drop_stale_virtual_steps(part) is None  # 整合しているので何も起きない
    part["type"] = "FBC"
    assert state.drop_stale_virtual_steps(part) == "__dvtbudget__"
    assert "__dvtbudget__" not in part["order"]
    assert state.validate_part(part) == []


def test_disable_relative_skips_axis_already_in_combined_entry(catalog: dict[str, list | None]) -> None:
    """複合軸エントリが split 軸をカバー済みなら、相対化OFFでもその軸を order に再追加しないことを検証する。"""
    part = state.part_skeleton("p", "FBC", catalog)
    state.enable_relative(part, catalog)  # split = Measure
    part["order"].insert(0, "State&Measure")
    restored = state.disable_relative(part, catalog)
    assert restored is None  # 複合軸エントリがカバー済み → 再追加されない
    assert part["order"].count("Measure") == 0


# ------------------------------------------------- Measure の labels 注記


def test_annotate_measure_labels_relative() -> None:
    """相対化設定の分子/分母の Measure 番号に対応する名前が labels に注記されることを検証する。"""
    mlabels = {0: "reference_param_read_level_1", 1: "evaluation_param_read_level_1"}
    rel = {"split_axis": "Measure", "numerator_when": 1, "denominator_when": 0}
    state.annotate_measure_labels(rel, mlabels)
    assert rel["labels"] == {
        "1": "evaluation_param_read_level_1",
        "0": "reference_param_read_level_1",
    }


def test_annotate_measure_labels_filter_and_unnamed() -> None:
    """Measure の filter では名前を持つ番号だけが labels に注記されることを検証する。

    名前が無くなれば注記ごと消える。
    """
    mlabels = {1: "evaluation_param_read_level_1"}
    spec = {"op": "filter", "value": [1, 2]}
    state.annotate_measure_labels(spec, mlabels)
    assert spec["labels"] == {"1": "evaluation_param_read_level_1"}  # 名無し番号は入らない
    unnamed = {"op": "filter", "value": 5}
    state.annotate_measure_labels(unnamed, {})
    assert "labels" not in unnamed
    # 名前が無くなったら(ラベル無しデータへ切替等)注記ごと消える
    spec2 = {"op": "filter", "value": 3, "labels": {"3": "old"}}
    state.annotate_measure_labels(spec2, {})
    assert "labels" not in spec2


# ------------------------------------------------------- ダミー展開の入力


def test_parse_chip_counts() -> None:
    """Chip 数入力の解釈を検証する: 単一値は全 Board 共通になる。

    個数不一致・非数値・0以下・空入力は ValueError になる。
    """
    assert state.parse_chip_counts("4", 3) == [4, 4, 4]  # 数1つ = 全 Board 共通
    assert state.parse_chip_counts("4, 4, 2, 2", 4) == [4, 4, 2, 2]
    with pytest.raises(ValueError, match="一致しません"):
        state.parse_chip_counts("4,4", 3)
    with pytest.raises(ValueError, match="数値ではありません"):
        state.parse_chip_counts("a,b", 2)
    with pytest.raises(ValueError, match="1以上"):
        state.parse_chip_counts("0", 2)
    with pytest.raises(ValueError, match="入力してください"):
        state.parse_chip_counts("", 2)


def test_expand_dummy_bundle_roundtrip(tmp_path: Path, data_dir_mini: Path) -> None:
    """疑似ダミー → expand_dummy_bundle → build_context が通しで動くこと(画面1のダミー展開ボタンの中身)。"""
    from scorelib_param.dummy import make_pseudo_dummy

    pseudo = make_pseudo_dummy(data_dir_mini, tmp_path / "pseudo")
    expanded = state.expand_dummy_bundle(str(pseudo), [2, 3])
    ctx = state.build_context(expanded)
    assert "FBC" in ctx["part_types"]
    assert ctx["catalogs"]["FBC"]["Board"] == [0, 1]
    assert ctx["measure_labels"]["FBC"][1] == "evaluation_param_read_level_1"


# ------------------------------------------------------------------- editing


def test_unique_part_name(sf: dict[str, Any]) -> None:
    """既存の part_1 と重複しない新規パーツ名 part_2 が生成されることを検証する。"""
    sf["score_parts"].append({"name": "part_1"})
    assert state.unique_part_name(sf) == "part_2"


def test_duplicate_part(sf: dict[str, Any], catalog: dict[str, list | None]) -> None:
    """本番と同じ形の入力で検証する: アプリが複製する時点でパーツは _uid を持っている。

    (_uid を共有するとウィジェットも共有され、2パーツが互いの
    名前・相対化を静かに上書きし合う実バグがあった)
    """
    sf["score_parts"].append(state.part_skeleton("p", "FBC", catalog))
    state.ensure_uids(sf)
    idx = state.duplicate_part(sf, 0)
    state.ensure_uids(sf)
    assert idx == 1
    assert sf["score_parts"][1]["name"] == "p_1"
    assert sf["score_parts"][0]["_uid"] != sf["score_parts"][1]["_uid"]
    sf["score_parts"][1]["aggregations"]["WL"]["op"] = "sum"
    assert sf["score_parts"][0]["aggregations"]["WL"]["op"] == "mean"  # 深いコピー(元は不変)


def test_part_list_labels_marker_handle_and_warning(sf: dict[str, Any], catalog: dict[str, list | None]) -> None:
    """ドラッグリストのラベルには ⠿ ハンドル・⚠ 検証マーカー・← 編集中 が付く。

    D&D部品自体は AppTest から観測できないため、この純関数の側で
    ロジックを検証する。
    """
    a = state.part_skeleton("a", "FBC", catalog)
    b = state.part_skeleton("b", "FBC", catalog)
    sf["score_parts"] = [a, b]
    state.ensure_uids(sf)
    labels = state.part_list_labels(sf, b["_uid"], {a["_uid"]})
    assert labels[0].startswith("⠿ ⚠ 1. a(")
    assert "編集中" not in labels[0]
    assert labels[1].startswith("⠿ 2. b(")
    assert labels[1].endswith("← 編集中")


def test_part_select_labels_unique_even_with_duplicate_names(
    sf: dict[str, Any], catalog: dict[str, list | None]
) -> None:
    """誤って同名になった2パーツにも別々のプルダウンラベルが付くことを検証する。

    Streamlit の selectbox は表示ラベルで項目を照合するため、ラベルが
    同一だと片方をクリックしたらもう片方が選ばれる(実バグ)。
    """
    a = state.part_skeleton("dAR_margin", "FBC", catalog)
    b = state.part_skeleton("dAR_margin", "FBC", catalog)
    sf["score_parts"] = [a, b]
    state.ensure_uids(sf)
    labels = state.part_select_labels(sf, {a["_uid"], b["_uid"]})
    assert labels[a["_uid"]] == "1. ⚠ dAR_margin"
    assert labels[b["_uid"]] == "2. ⚠ dAR_margin"
    assert len(set(labels.values())) == 2


def test_ensure_uids_repairs_duplicated_ids(sf: dict[str, Any], catalog: dict[str, list | None]) -> None:
    """_uid 重複バグの期間に保存された下書きが、開くだけで治ること。"""
    a = state.part_skeleton("a", "FBC", catalog)
    b = state.part_skeleton("b", "FBC", catalog)
    a["_uid"] = b["_uid"] = "same1234"
    sf["score_parts"] = [a, b]
    state.ensure_uids(sf)
    assert a["_uid"] != b["_uid"]
    assert a["_uid"] == "same1234"  # 最初の1つは安定して保持される


def test_move_entry() -> None:
    """move_entry がリスト要素を前後に移動し、端では no-op になることを検証する。"""
    lst = ["a", "b", "c"]
    assert state.move_entry(lst, 0, +1) == 1
    assert lst == ["b", "a", "c"]
    assert state.move_entry(lst, 0, -1) == 0  # no-op at the edge
    assert lst == ["b", "a", "c"]


# -------------------------------------------------------------- selection sets


def test_referencing_parts_and_guarded_delete(sf: dict[str, Any]) -> None:
    """選択セットを参照するパーツがある間は削除が拒否され、参照が無くなれば削除できることを検証する。"""
    sf["selectionSets"]["ud"] = [{"State": "R2A", "Read_Label": "read_level_upper1"}]
    sf["score_parts"].append(
        {
            "name": "p",
            "type": "FBC",
            "order": ["State&Read_Label"],
            "aggregations": {"State&Read_Label": {"op": "sum", "ref": "ud"}},
        }
    )
    assert state.referencing_parts(sf, "ud") == ["p"]
    with pytest.raises(ValueError, match="参照されている"):
        state.delete_selection_set(sf, "ud")
    sf["score_parts"].clear()
    state.delete_selection_set(sf, "ud")
    assert sf["selectionSets"] == {}


def test_save_set_as(sf: dict[str, Any]) -> None:
    """選択セットの別名保存が深いコピーで行われ、既存名への保存は拒否されることを検証する。"""
    sf["selectionSets"]["ud"] = [{"State": "R2A", "Read_Label": "u1"}]
    state.save_set_as(sf, "ud", "ud2")
    sf["selectionSets"]["ud2"][0]["State"] = "A2B"
    assert sf["selectionSets"]["ud"][0]["State"] == "R2A"  # 深いコピー(元は不変)
    with pytest.raises(ValueError, match="既に存在"):
        state.save_set_as(sf, "ud", "ud2")


# ----------------------------------------------------------------- group defs


def test_import_config_group_defs(sf: dict[str, Any]) -> None:
    """設定ファイルの WLgroup が groupDefs に取り込まれることを検証する。

    再取り込みで編集済みコピーは上書きされない。
    """
    wlgroup = {"g1": (0, 3), "g2": (4, 8)}
    assert state.import_config_group_defs(sf, wlgroup) is True
    assert sf["groupDefs"]["WLgroup"] == {
        "axis": "WL",
        "groups": {"g1": [0, 3], "g2": [4, 8]},
        "definedInLogical": True,
    }
    # 再取り込み(や先の編集)で編集可能コピーが上書きされてはならない
    sf["groupDefs"]["WLgroup"]["groups"]["g1"] = [0, 5]
    assert state.import_config_group_defs(sf, wlgroup) is False
    assert sf["groupDefs"]["WLgroup"]["groups"]["g1"] == [0, 5]
    assert state.import_config_group_defs(sf, None) is False


def test_import_config_group_defs_physical_and_weight(sf: dict[str, Any]) -> None:
    """Physical 定義と重みの取り込みを検証する: definedInLogical=False と weightSets が入る。

    既に編集済みの重みセットは上書きされない。
    """
    wlgroup = {"g1": (0, 3)}
    assert state.import_config_group_defs(sf, wlgroup, defin_logical=False, wlgroup_weight={"g1": 2.0}) is True
    assert sf["groupDefs"]["WLgroup"]["definedInLogical"] is False
    assert sf["weightSets"]["WLgroupWeight"] == {"g1": 2.0}
    # 既に編集済みの重みセットは上書きしない
    sf["weightSets"]["WLgroupWeight"] = 5.0
    state.import_config_group_defs(sf, None, wlgroup_weight={"g1": 9.0})
    assert sf["weightSets"]["WLgroupWeight"] == 5.0


def test_group_def_delete_guarded_by_references(sf: dict[str, Any], catalog: dict[str, list | None]) -> None:
    """グループ定義を参照するパーツがある間は削除が拒否され、参照が無くなれば削除できることを検証する。"""
    part = state.part_skeleton("p", "FBC", catalog)
    part["order"].append("WLgroup")
    part["aggregations"]["WLgroup"] = {"op": "max"}
    sf["score_parts"].append(part)
    sf["groupDefs"]["WLgroup"] = {"axis": "WL", "groups": {"g1": [0, 100]}}
    assert state.parts_referencing_group_def(sf, "WLgroup") == ["p"]
    with pytest.raises(ValueError, match="参照されている"):
        state.delete_group_def(sf, "WLgroup")
    sf["score_parts"].clear()
    state.delete_group_def(sf, "WLgroup")
    assert sf["groupDefs"] == {}


def test_add_group_def_rejects_collisions(sf: dict[str, Any], catalog: dict[str, list | None]) -> None:
    """グループ定義の追加で、空名・軸名との衝突・既存名の重複がそれぞれ拒否されることを検証する。"""
    with pytest.raises(ValueError, match="入力してください"):
        state.add_group_def(sf, "  ", "WL", set(catalog))
    with pytest.raises(ValueError, match="軸名と衝突"):
        state.add_group_def(sf, "WL", "WL", set(catalog))
    state.add_group_def(sf, "STRgroup", "STR", set(catalog))
    assert sf["groupDefs"]["STRgroup"] == {"axis": "STR", "groups": {}}
    with pytest.raises(ValueError, match="既に存在"):
        state.add_group_def(sf, "STRgroup", "STR", set(catalog))


def test_export_part_bundles_group_defs(sf: dict[str, Any], catalog: dict[str, list | None]) -> None:
    """パーツ単体エクスポートに、そのパーツが参照するグループ定義だけが同梱されることを検証する。"""
    part = state.part_skeleton("p", "FBC", catalog)
    part["order"].append("WLgroup")
    part["aggregations"]["WLgroup"] = {"op": "max"}
    sf["score_parts"].append(part)
    sf["groupDefs"]["WLgroup"] = {"axis": "WL", "groups": {"g1": [0, 100]}}
    sf["groupDefs"]["unused"] = {"axis": "STR", "groups": {"a": [0, 1]}}
    back = state.import_score_file(state.export_part(sf, 0))
    assert list(back["groupDefs"]) == ["WLgroup"]  # 参照している定義だけ同梱される


def test_validate_weight_ref_resolves_against_weight_sets(sf: dict[str, Any], catalog: dict[str, list | None]) -> None:
    """重みセット ref の検証: 定義済みなら通り、未定義なら分かるエラーになる。

    (回帰: 検証層が weightSets を渡さず、定義済みでも常に
    "unknown weight set" になっていた)
    """
    part = state.part_skeleton("p", "FBC", catalog)
    part["order"].insert(0, "__weight__")
    part["aggregations"]["__weight__"] = {"op": "mul", "by": "WLgroup", "ref": "WLgroupWeight"}
    sf["score_parts"].append(part)
    sf["groupDefs"]["WLgroup"] = {"axis": "WL", "groups": {"g1": [0, 1000]}}

    # 未定義 → 検証で捕まる(計算まで行かない)
    assert any("WLgroupWeight" in p for p in state.validate_score_file(sf))
    assert state.validate_part(part, sf["selectionSets"], sf.get("weightSets"))

    # 定義済み → 検証を通る
    sf["weightSets"]["WLgroupWeight"] = {"g1": 2.0}
    assert state.validate_score_file(sf) == []
    assert state.validate_part(part, sf["selectionSets"], sf["weightSets"]) == []


def test_export_part_bundles_weight_sets(sf: dict[str, Any], catalog: dict[str, list | None]) -> None:
    """パーツ単体エクスポートに、そのパーツが参照する重みセットだけが同梱されることを検証する。"""
    part = state.part_skeleton("p", "FBC", catalog)
    part["order"] = ["WL", "__weight__", "WLgroup"] + [e for e in part["order"] if e != "WL"]
    part["aggregations"]["WLgroup"] = {"op": "max"}
    part["aggregations"]["__weight__"] = {"op": "mul", "by": "WLgroup", "ref": "WLgroupWeight"}
    sf["score_parts"].append(part)
    sf["groupDefs"]["WLgroup"] = {"axis": "WL", "groups": {"g1": [0, 100]}}
    sf["weightSets"]["WLgroupWeight"] = {"g1": 2.0}
    sf["weightSets"]["unusedW"] = 1.5
    back = state.import_score_file(state.export_part(sf, 0))
    assert back["weightSets"] == {"WLgroupWeight": {"g1": 2.0}}  # 参照分だけ同梱
    assert "WLgroup" in back["groupDefs"]


def test_run_test_compute_with_weight_step(catalog: dict[str, list | None], data_dir_mini: Path) -> None:
    """UI経由の一気通貫: WLgroup 別重みステップつきパーツが実データで計算でき、重み全1なら重みなしと一致する。"""

    def make(with_weight: bool) -> dict[str, Any]:
        f = state.empty_score_file()
        part = state.part_skeleton("p", "FBC", catalog)
        part["order"].append("WLgroup")
        part["aggregations"]["WLgroup"] = {"op": "max"}
        if with_weight:
            part["order"].insert(part["order"].index("WLgroup"), "__weight__")
            part["aggregations"]["__weight__"] = {
                "op": "mul",
                "by": "WLgroup",
                "ref": "WLgroupWeight",
            }
            f["weightSets"]["WLgroupWeight"] = {"low": 1.0, "high": 1.0}
        f["groupDefs"]["WLgroup"] = {"axis": "WL", "groups": {"low": [0, 10], "high": [11, 1000]}}
        f["score_parts"].append(part)
        f["expression"] = "p"
        return f

    weighted = state.run_test_compute(make(with_weight=True), str(data_dir_mini))
    plain = state.run_test_compute(make(with_weight=False), str(data_dir_mini))
    assert weighted["p"] == pytest.approx(plain["p"])


def test_run_test_compute_with_group_axis(
    sf: dict[str, Any], catalog: dict[str, list | None], data_dir_mini: Path
) -> None:
    """UI経由の一気通貫: 派生グループ軸を使うパーツが実データで計算できる。

    (グループ間集計を最後に回すユーザシナリオ)
    """
    part = state.part_skeleton("p", "FBC", catalog)
    part["order"].append("WLgroup")
    part["aggregations"]["WLgroup"] = {"op": "max"}
    sf["score_parts"].append(part)
    sf["expression"] = "p"
    sf["groupDefs"]["WLgroup"] = {"axis": "WL", "groups": {"low": [0, 10], "high": [11, 1000]}}
    result = state.run_test_compute(sf, str(data_dir_mini))
    assert isinstance(result["p"], float)


# ---------------------------------------------------------------- validation


def test_validate_score_file_ok(sf: dict[str, Any], catalog: dict[str, list | None]) -> None:
    """雛形パーツと式だけの ScoreFile が検証エラーなしで通ることを検証する。"""
    sf["score_parts"].append(state.part_skeleton("p", "FBC", catalog))
    sf["expression"] = "p"
    assert state.validate_score_file(sf) == []


def test_validate_reports_engine_errors(sf: dict[str, Any]) -> None:
    """エンジン層の検証エラー(value 無しの filter)が validate_score_file から報告されることを検証する。"""
    sf["score_parts"].append(
        {"name": "p", "type": "FBC", "order": ["State"], "aggregations": {"State": {"op": "filter"}}}
    )
    problems = state.validate_score_file(sf)
    assert any("filter" in p for p in problems)


def test_validate_expression_unknown_part(sf: dict[str, Any], catalog: dict[str, list | None]) -> None:
    """式が未知のパーツ名を参照していると expression のエラーになることを検証する。"""
    sf["score_parts"].append(state.part_skeleton("p", "FBC", catalog))
    sf["expression"] = "p + nope"
    problems = state.validate_score_file(sf)
    assert any("expression" in p for p in problems)


def test_validate_duplicate_names_and_dangling_constraint(sf: dict[str, Any], catalog: dict[str, list | None]) -> None:
    """パーツ名の重複と、存在しないパーツへの制約しきい値がそれぞれ検証エラーになることを検証する。"""
    sf["score_parts"] = [
        state.part_skeleton("p", "FBC", catalog),
        state.part_skeleton("p", "FBC", catalog),
    ]
    sf["constraintThreshold"] = {"gone": {"value": 1.0}}
    problems = state.validate_score_file(sf)
    assert any("重複" in p for p in problems)
    assert any("gone" in p for p in problems)


def test_validate_unknown_ref(sf: dict[str, Any], catalog: dict[str, list | None]) -> None:
    """未定義の選択セット ref がエラーとして報告されることを検証する。"""
    part = state.part_skeleton("p", "FBC", catalog)
    part["aggregations"]["State"] = {"op": "sum", "ref": "nope"}
    sf["score_parts"].append(part)
    problems = state.validate_score_file(sf)
    assert any("nope" in p for p in problems)


# ------------------------------------------------------------------- context


def test_build_context(data_dir_mini: Path) -> None:
    """build_context で type 一覧・カタログ・初期温度の有無が揃うことを検証する(mini データ)。"""
    ctx = state.build_context(str(data_dir_mini))
    assert ctx["part_types"] == MINI_TYPES  # mini ディレクトリに係数jsoncは無い
    assert "Page" in ctx["catalogs"]["tR"]
    assert ctx["has_initial_temperature"] is True
    assert ctx["config_path"] is None


def test_part_types_without_data(data_dir_mini: Path) -> None:
    """別実験の config 由来でデータに無い type のパーツを検出する(custom は対象外)。"""
    ctx = state.build_context(str(data_dir_mini))
    sf = state.empty_score_file()
    sf["score_parts"] = [
        {"_uid": "a", "name": "p1", "type": "FBC", "order": [], "aggregations": {}},
        {"_uid": "b", "name": "p2", "type": "GONE", "order": [], "aggregations": {}},
        {"_uid": "c", "name": "p3", "type": "custom", "function": "f", "params": {}},
    ]
    assert state.part_types_without_data(sf, ctx) == {"b"}


def test_part_types_without_data_config_only_never_flags() -> None:
    """設定のみ編集モードはカタログが設定自身から導出されるため警告しない。"""
    sf = state.empty_score_file()
    sf["score_parts"] = [
        {"_uid": "a", "name": "p", "type": "tR", "order": ["WL"], "aggregations": {"WL": {"op": "mean"}}},
    ]
    ctx = state.config_only_context(sf)
    assert state.part_types_without_data(sf, ctx) == set()


def test_part_value_mismatches_flags_values_not_in_data(data_dir_mini: Path, sf: dict[str, Any]) -> None:
    """filter・相対化の値がデータに無いパーツを読み込み直後から検出することを検証する。

    ユーザー報告のシナリオ: config は Read_Label='upper1' で filter するが
    データの実値は 'read_level_upper1' — 従来は編集対象に選ぶまで気づけなかった。
    """
    ctx = state.build_context(str(data_dir_mini))
    part = {
        "_uid": "u1",
        "name": "p1",
        "type": "FBC",
        "order": ["Read_Label", "WL"],
        "aggregations": {"Read_Label": {"op": "filter", "value": "upper1"}, "WL": {"op": "mean"}},
    }
    sf["score_parts"].append(part)
    got = state.part_value_mismatches(sf, ctx)
    assert set(got) == {"u1"}
    assert "Read_Label" in got["u1"][0]
    assert "upper1" in got["u1"][0]

    # データに実在する値なら検出されない
    part["aggregations"]["Read_Label"]["value"] = "read_level_upper1"
    assert state.part_value_mismatches(sf, ctx) == {}

    # 相対化の分子/分母も対象(Measure 99 はデータに無い)
    part["relative"] = {"split_axis": "Measure", "numerator_when": 99, "denominator_when": 0}
    got = state.part_value_mismatches(sf, ctx)
    assert any("99" in m for m in got["u1"])

    # 選択セット参照(ref)は判定対象外(候補との突き合わせは直接指定の値だけ)
    del part["relative"]
    part["aggregations"]["Read_Label"] = {"op": "filter", "value": "upper1", "ref": "s1"}
    assert state.part_value_mismatches(sf, ctx) == {}

    # dVtBudget パーツは FBC.csv を読む(エンジンの _source_type と同じ対応)
    # — 型名のままカタログを引くと検査から漏れる、の回帰
    sf["score_parts"] = [
        {
            "_uid": "u2",
            "name": "pd",
            "type": "dVtBudget",
            "order": ["Read_Label"],
            "aggregations": {"Read_Label": {"op": "filter", "value": "upper1"}},
        }
    ]
    assert set(state.part_value_mismatches(sf, ctx)) == {"u2"}


def test_config_problem_messages_unifies_all_kinds(data_dir_mini: Path, sf: dict[str, Any]) -> None:
    """「設定の誤り」に構造の誤り・データに無い値・データ無し type が一本化されることを検証する。

    サイドバーの件数・展開表示とテスト実行前ガードの共通実体(2026-07-31
    ユーザー合意: ダミーは本番構造を模す前提なので、データに無い要素を使うのも
    設定の誤り)。パーツ単位のメッセージにはパーツ名が前置される。
    """
    ctx = state.build_context(str(data_dir_mini))
    sf["score_parts"] = [
        # 構造の誤り(filter に value が無い)
        {"_uid": "a", "name": "p_broken", "type": "FBC", "order": ["WL"], "aggregations": {"WL": {"op": "filter"}}},
        # データに無い値
        {
            "_uid": "b",
            "name": "p_missing",
            "type": "FBC",
            "order": ["Read_Label", "WL"],
            "aggregations": {"Read_Label": {"op": "filter", "value": "upper1"}, "WL": {"op": "mean"}},
        },
        # データに無い type
        {"_uid": "c", "name": "p_gone", "type": "GONE", "order": [], "aggregations": {}},
    ]
    msgs = state.config_problem_messages(sf, ctx)
    assert len(msgs) == 3
    assert any("p_broken" in m for m in msgs)
    assert any(m.startswith("p_missing: ") and "upper1" in m for m in msgs)
    assert any(m.startswith("p_gone: ") and "GONE" in m for m in msgs)
    sf["score_parts"] = []
    assert state.config_problem_messages(sf, ctx) == []


def test_build_context_missing_dir() -> None:
    """存在しないディレクトリ指定が ValueError で拒否されることを検証する。"""
    with pytest.raises(ValueError, match="見つかりません"):
        state.build_context("no/such/dir")


def test_build_context_empty_path_rejected() -> None:
    """Python では Path('') はカレントディレクトリ扱い。空入力のまま、アプリの起動場所を黙って走査してはならない。"""
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="入力してください"):
            state.build_context(bad)


def test_build_context_explicit_coef(data_dir_mini: Path, dvtbudget_coef_path: Path) -> None:
    """係数jsoncは通常 result_tmp の外にあり、独立したパスで指定される。"""
    ctx = state.build_context(str(data_dir_mini), coef_path=str(dvtbudget_coef_path))
    assert ctx["part_types"] == [*MINI_TYPES, "dVtBudget"]
    assert ctx["coef_source"] == "指定"
    assert "dVtBudget" in ctx["catalogs"]


def test_build_context_explicit_config(data_dir_mini: Path, fixtures_dir: Path) -> None:
    """config_path 明示指定で config_source が「指定」になり、generation と wlgroup が読めることを検証する。"""
    ctx = state.build_context(str(data_dir_mini), config_path=str(fixtures_dir / "config.jsonc"))
    assert ctx["config_source"] == "指定"
    assert ctx["generation"]
    assert ctx["wlgroup"]


def test_build_context_missing_explicit_file_rejected(data_dir_mini: Path) -> None:
    """明示指定した係数jsoncが存在しない場合に ValueError で拒否されることを検証する。"""
    with pytest.raises(ValueError, match="dVtBudget係数jsonc が見つかりません"):
        state.build_context(str(data_dir_mini), coef_path="no/such.jsonc")


def test_build_context_in_dir_discovery_still_works(
    tmp_path: Path, data_dir_mini: Path, dvtbudget_coef_path: Path
) -> None:
    """測定ディレクトリ内に置かれた係数jsoncが自動検出されることを検証する。"""
    d = tmp_path / "run"
    shutil.copytree(data_dir_mini, d)
    shutil.copy(dvtbudget_coef_path, d / "dvtbudget_coef.jsonc")
    ctx = state.build_context(str(d))
    assert ctx["coef_source"] == "自動検出"
    assert "dVtBudget" in ctx["part_types"]


# ----------------------------------------------------------- generation info


def test_build_context_geninfo_explicit(data_dir_mini: Path, fixtures_dir: Path) -> None:
    """geninfo_path 明示指定で世代情報が読まれ、軸本数が得られることを検証する。"""
    ctx = state.build_context(str(data_dir_mini), geninfo_path=str(fixtures_dir / "B9LS.json"))
    assert ctx["geninfo_source"] == "指定"
    assert state.axis_counts(ctx["geninfo"]) == {"WL": 6, "STR": 3}


def test_build_context_geninfo_discovered_via_generation(
    tmp_path: Path, data_dir_mini: Path, fixtures_dir: Path
) -> None:
    """設定ファイルの Generation 名から同名の世代情報 json が自動検出されることを検証する。"""
    d = tmp_path / "run"
    shutil.copytree(data_dir_mini, d)
    shutil.copy(fixtures_dir / "config.jsonc", d / "config.jsonc")  # Generation: B9LS
    shutil.copy(fixtures_dir / "B9LS.json", d / "B9LS.json")
    ctx = state.build_context(str(d))
    assert ctx["geninfo_source"] == "自動検出"
    assert ctx["geninfo"]["numWLs"] == 6


def test_build_context_missing_geninfo_rejected(data_dir_mini: Path) -> None:
    """明示指定した世代情報 json が存在しない場合に ValueError で拒否されることを検証する。"""
    with pytest.raises(ValueError, match="世代情報json が見つかりません"):
        state.build_context(str(data_dir_mini), geninfo_path="no/such.json")


def test_group_def_warnings(sf: dict[str, Any]) -> None:
    """グループ範囲の軸本数チェック: 範囲外・取りこぼしを警告し、整合時や本数不明の軸は無音であることを検証する。"""
    counts = {"WL": 6, "STR": 3}
    sf["groupDefs"]["WLgroup"] = {"axis": "WL", "groups": {"g1": [0, 3], "g2": [4, 8]}}
    warns = state.group_def_warnings(sf, counts)
    assert any("範囲外" in w and "g2(4-8)" in w for w in warns)

    sf["groupDefs"]["WLgroup"]["groups"] = {"g1": [0, 3]}
    warns = state.group_def_warnings(sf, counts)
    assert any("4-5" in w and "どのグループにも入りません" in w for w in warns)

    # 範囲が本数と合っていれば無音。本数の分からない軸はチェック対象外
    sf["groupDefs"]["WLgroup"]["groups"] = {"g1": [0, 3], "g2": [4, 5]}
    sf["groupDefs"]["PageG"] = {"axis": "Page", "groups": {"x": [0, 99]}}
    assert state.group_def_warnings(sf, counts) == []
    assert state.group_def_warnings(sf, {}) == []


def test_data_axis_counts_and_validation_counts(data_dir_mini: Path) -> None:
    """本数はデータ(カタログ)由来が正。世代情報 json は補完のみで、食い違いは診断警告になる。"""
    ctx = state.build_context(str(data_dir_mini))
    counts = state.data_axis_counts(ctx["catalogs"])
    assert counts["WL"] == 6  # mini データの WL 最大値 5 → 6本
    assert "Measure" not in counts  # 測定数は「本数」の概念が違うため除外

    # 世代情報なし → データ由来がそのまま検証に使われる
    assert state.validation_axis_counts(ctx)["WL"] == 6
    assert state.geninfo_mismatch_warnings(ctx) == []

    # 食い違う世代情報が自動検出された場合: データ由来が勝ち、診断警告が出る
    ctx["geninfo"] = {"numWLs": 100}
    ctx["geninfo_path"] = "B9LS.json"
    assert state.validation_axis_counts(ctx)["WL"] == 6
    warns = state.geninfo_mismatch_warnings(ctx)
    assert len(warns) == 1
    assert "100" in warns[0]
    assert "6" in warns[0]


# ------------------------------------------------------- 設定のみ編集モード


def test_load_config_only_from_run_config(fixtures_dir: Path) -> None:
    """設定 jsonc だけからの編集開始を検証する。

    データ無しで検証・編集・エクスポートに必要な情報が揃うこと。
    """
    text = (fixtures_dir / "config.jsonc").read_text(encoding="utf-8")
    sf, ctx = state.load_config_only(text)
    assert ctx["config_only"] is True
    assert ctx["data_dir"] is None
    assert sf["score_parts"]
    # 旧来の optimization.WLgroup も編集可能なグループ定義として取り込まれる
    assert "WLgroup" in sf["groupDefs"]
    # カタログは設定が言及する軸名(値候補は無し = 自由入力)
    assert ctx["part_types"]
    cat = ctx["catalogs"][ctx["part_types"][0]]
    assert cat
    assert all(v is None for v in cat.values())
    assert not any(a.startswith("__") for a in cat)  # 仮想ステップは軸ではない
    # 検証・エクスポートはデータ非依存で動く
    assert state.validate_score_file(sf) == []
    assert state.score_file_to_jsonc(sf)


def test_load_config_only_from_score_jsonc(sf: dict[str, Any], catalog: dict[str, list | None]) -> None:
    """エクスポートした score.jsonc 形式も読める(式・グループ定義の微修正 → 再エクスポートの往復)。"""
    sf["score_parts"].append(state.part_skeleton("p", "FBC", catalog))
    sf["expression"] = "p"
    text = state.score_file_to_jsonc(sf)
    sf2, ctx = state.load_config_only(text)
    assert ctx["config_only"] is True
    sf2["expression"] = "p * 2"
    assert state.validate_score_file(sf2) == []
    assert "p * 2" in state.score_file_to_jsonc(sf2)


def test_is_run_config_text(fixtures_dir: Path, sf: dict[str, Any], catalog: dict[str, list | None]) -> None:
    """①の設定を build_context の config として渡せる形式かの判定。"""
    assert state.is_run_config_text((fixtures_dir / "config.jsonc").read_text(encoding="utf-8"))
    sf["score_parts"].append(state.part_skeleton("p", "FBC", catalog))
    assert not state.is_run_config_text(state.score_file_to_jsonc(sf))  # ScoreFile 形式
    assert not state.is_run_config_text("not json at all")


def test_config_only_skeleton_measure_requires_input() -> None:
    """設定のみ編集の雛形: Measure は候補が無いので value 未入力の filter になる。

    番号を入れるまで検証エラーで促される(mean だと測定が静かに混ざるため)。
    """
    catalog: dict[str, list | None] = {"Measure": None, "State": None, "Board": None}
    part = state.part_skeleton("p", "X", catalog)
    assert part["aggregations"]["Measure"] == {"op": "filter", "value": None}
    problems = state.validate_part(part)
    assert any("filter" in p and "value" in p for p in problems)


# ----------------------------------------------------- アップロード入力経路


def test_save_upload() -> None:
    """アップロード保存で内容がそのまま書かれ、ファイル名のパス成分が捨てられることを検証する。"""
    from pathlib import Path

    p = Path(state.save_upload("config.jsonc", b"{}"))
    assert p.is_file()
    assert p.name == "config.jsonc"
    assert p.read_bytes() == b"{}"
    # パス成分は捨てられる(アップロード名による書き込み先操作の防止)
    p2 = Path(state.save_upload("../evil.py", b"x"))
    assert p2.name == "evil.py"


def test_dummy_zip_upload_flow(tmp_path: Path, data_dir_mini: Path) -> None:
    """ダミー一式を zip でアップロードする経路(画面1): 展開 → Board/Chip 複製 → 通常読み込み、が通しで動くこと。

    フォルダごと圧縮された zip の「トップに1フォルダ」形も
    extract_bundle_zip が吸収する。
    """
    import io
    import zipfile

    from scorelib_param.dummy import make_pseudo_dummy

    pseudo = make_pseudo_dummy(data_dir_mini, tmp_path / "pseudo")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for f in pseudo.iterdir():
            z.write(f, f"dummy_bundle/{f.name}")
    src = state.extract_bundle_zip(buf.getvalue())
    expanded = state.expand_dummy_bundle(src, [2, 2])
    ctx = state.build_context(expanded)
    assert ctx["catalogs"]["FBC"]["Board"] == [0, 1]
    assert ctx["catalogs"]["FBC"]["Measure"] == [0, 1, 2, 3]


# --------------------------------------------------- readable error messages


def test_validation_error_names_the_part(sf: dict[str, Any]) -> None:
    """検証エラーメッセージに問題のパーツ名が含まれることを検証する。"""
    sf["score_parts"].append(
        {"name": "myPart", "type": "FBC", "order": ["State"], "aggregations": {"State": {"op": "filter"}}}
    )
    problems = state.validate_score_file(sf)
    assert any("パーツ 'myPart'" in p for p in problems)


def test_import_error_names_the_part() -> None:
    """読み込みエラーメッセージに問題のパーツ名が含まれることを検証する。"""
    import json

    text = json.dumps(
        {
            "score_parts": [
                {
                    "name": "old_part",
                    "type": "FBC",
                    "order": ["WL"],
                    "aggregations": {"WL": {"op": "group_reduce", "group_def": "WLgroup"}},
                }
            ],
            "expression": "old_part",
        }
    )
    with pytest.raises(ValueError, match="old_part"):
        state.import_score_file(text)


# -------------------------------------------------------- custom parts / zip


@pytest.fixture
def custom_parts_path(fixtures_dir: Path) -> Path:
    """カスタムパーツ定義 fixtures/custom_parts.py のパスを返す。

    Returns:
        フィクスチャ内の custom_parts.py の絶対パス。

    """
    return fixtures_dir / "custom_parts.py"


def test_build_context_custom_explicit(data_dir_mini: Path, custom_parts_path: Path) -> None:
    """custom_path 明示指定でカスタム関数が読まれ、custom type が part_types に追加されることを検証する。"""
    ctx = state.build_context(str(data_dir_mini), custom_path=str(custom_parts_path))
    assert ctx["custom_source"] == "指定"
    assert "fixed_value" in ctx["custom_functions"]
    assert "custom" in ctx["part_types"]
    assert "custom" not in ctx["catalogs"]  # pseudo-type, no axis catalog


def test_build_context_without_custom_hides_type(data_dir_mini: Path) -> None:
    """custom_path 無しでは custom type が part_types に現れないことを検証する。"""
    ctx = state.build_context(str(data_dir_mini))
    assert "custom" not in ctx["part_types"]


def test_custom_part_skeleton_and_compute(sf: dict[str, Any], data_dir_mini: Path, custom_parts_path: Path) -> None:
    """カスタムパーツの雛形が検証を通り、実データで計算できることを検証する。"""
    part = state.custom_part_skeleton("p", ["mean_fbc_plus_offset"])
    part["params"] = {"offset": 5}
    sf["score_parts"].append(part)
    sf["expression"] = "p"
    assert state.validate_score_file(sf) == []
    result = state.run_test_compute(
        sf, str(data_dir_mini), state.TestComputeInputs(custom_path=str(custom_parts_path))
    )
    assert isinstance(result["p"], float)


def test_switch_part_type_strips_mismatched_fields(catalog: dict[str, list | None]) -> None:
    """パーツ type を custom と通常の間で切り替えると、合わない側のフィールドが除去されることを検証する。"""
    part = state.part_skeleton("p", "FBC", catalog)
    state.switch_part_type(part, "custom")
    assert "order" not in part
    assert "relative" not in part
    assert "aggregations" not in part
    assert part["function"] == "p"
    assert state.validate_part(part) == []
    state.switch_part_type(part, "FBC")
    assert "function" not in part
    assert "params" not in part
    assert part["order"] == []
    assert state.validate_part(part) == []


def _bundle_zip(data_dir_mini: Path, fixtures_dir: Path, custom_parts_path: Path, layout: str) -> bytes:
    """layout: arcname prefix for the measurement csvs (companions at root).

    Returns:
        測定 csv 一式と同梱ファイル(config・世代 json・custom_parts.py)を
        詰めた zip アーカイブのバイト列。

    """
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for f in data_dir_mini.iterdir():
            z.write(f, f"{layout}/{f.name}")
        z.write(fixtures_dir / "config.jsonc", "bundle/config.jsonc")
        z.write(fixtures_dir / "B9LS.json", "bundle/B9LS.json")
        z.write(custom_parts_path, "bundle/custom_parts.py")
    return buf.getvalue()


def test_bundle_zip_flat_layout(data_dir_mini: Path, fixtures_dir: Path, custom_parts_path: Path) -> None:
    """全部1フォルダに入った形: そのフォルダ自体が測定ディレクトリになる。"""
    data = _bundle_zip(data_dir_mini, fixtures_dir, custom_parts_path, "bundle")
    found = cast("dict[str, str]", state.locate_bundle_inputs(state.extract_bundle_zip(data)))
    ctx = state.build_context(
        found["data_dir"],
        found["config_path"],
        found["coef_path"],
        found["geninfo_path"],
        found["custom_path"],
    )
    assert set(MINI_TYPES) <= set(ctx["part_types"])
    assert ctx["geninfo"]["numWLs"] == 6
    assert "custom" in ctx["part_types"]


def test_bundle_zip_nested_layout(data_dir_mini: Path, fixtures_dir: Path, custom_parts_path: Path) -> None:
    """GUIが作る自然な構成: 測定csvは result_tmp サブフォルダ内、同梱ファイルはルート。

    サブディレクトリも探索するのでこれも読めること。
    """
    data = _bundle_zip(data_dir_mini, fixtures_dir, custom_parts_path, "bundle/result_tmp")
    found = cast("dict[str, str]", state.locate_bundle_inputs(state.extract_bundle_zip(data)))
    assert found["data_dir"].endswith("result_tmp")
    assert found["config_path"]
    assert found["geninfo_path"]
    assert found["custom_path"]
    ctx = state.build_context(
        found["data_dir"],
        found["config_path"],
        found["coef_path"],
        found["geninfo_path"],
        found["custom_path"],
    )
    assert set(MINI_TYPES) <= set(ctx["part_types"])
    assert "custom" in ctx["part_types"]


def test_bundle_zip_ambiguous_data_dirs_rejected(data_dir_mini: Path) -> None:
    """測定結果ディレクトリの候補が zip 内に複数あると ValueError で拒否されることを検証する。"""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for f in data_dir_mini.iterdir():
            z.write(f, f"bundle/epoch1/{f.name}")
            z.write(f, f"bundle/epoch2/{f.name}")
    with pytest.raises(ValueError, match="測定結果ディレクトリの候補が複数"):
        state.locate_bundle_inputs(state.extract_bundle_zip(buf.getvalue()))


def test_in_dir_discovery_rejects_ambiguous_configs(tmp_path: Path, data_dir_mini: Path, fixtures_dir: Path) -> None:
    """設定jsoncの形に合うファイルが2つ: 黙って選ばず拒否すること。

    (以前はアルファベット順の先頭が無言で採用されていた)
    """
    import shutil as sh

    d = tmp_path / "run"
    sh.copytree(data_dir_mini, d)
    sh.copy(fixtures_dir / "config.jsonc", d / "config_a.jsonc")
    sh.copy(fixtures_dir / "config.jsonc", d / "config_b.jsonc")
    with pytest.raises(ValueError, match="候補が複数"):
        state.build_context(str(d))
    # 明示パス指定で曖昧さは解消できる
    ctx = state.build_context(str(d), config_path=str(d / "config_a.jsonc"))
    assert ctx["config_source"] == "指定"


# ------------------------------------------------------------- draft / export


def test_draft_path_for_sanitizes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ユーザ名ごとの下書きパス: パス区切り等はファイル名安全な文字へ正規化(名前入力によるディレクトリ脱出の防止)。"""
    monkeypatch.setattr(state, "DRAFTS_DIR", tmp_path)
    p = state.draft_path_for("田中 ../evil")
    assert p.parent == tmp_path
    assert p.suffix == ".jsonc"
    assert "/" not in p.name
    assert "\\" not in p.name
    assert ".." not in p.name
    assert state.draft_path_for("  ").name == "_.jsonc"  # 空相当は "_"


def test_draft_roundtrip(tmp_path: Path, sf: dict[str, Any], catalog: dict[str, list | None]) -> None:
    """下書きの保存→読込の往復で ScoreFile と context_inputs が復元され、無いパスは None になることを検証する。"""
    sf["score_parts"].append(state.part_skeleton("p", "FBC", catalog))
    path = tmp_path / "draft.jsonc"
    state.save_draft(sf, {"data_dir": "somewhere"}, path)
    draft = cast("dict[str, Any]", state.load_draft(path))
    assert draft["score_file"] == sf
    assert draft["context_inputs"]["data_dir"] == "somewhere"
    assert state.load_draft(tmp_path / "none.jsonc") is None


def test_export_and_import_roundtrip(sf: dict[str, Any], catalog: dict[str, list | None]) -> None:
    """エクスポートした jsonc を import_score_file で読み戻すとパーツ名と式が復元されることを検証する。"""
    sf["score_parts"].append(state.part_skeleton("p", "FBC", catalog))
    sf["expression"] = "p"
    text = state.score_file_to_jsonc(sf)
    back = state.import_score_file(text)
    assert [p["name"] for p in back["score_parts"]] == ["p"]
    assert back["expression"] == "p"


def test_import_run_config(fixtures_dir: Path) -> None:
    """実行 config.jsonc 形式を import_score_file が読めて score_parts が得られることを検証する。"""
    text = (fixtures_dir / "config.jsonc").read_text(encoding="utf-8")
    imported = state.import_score_file(text)
    assert imported["score_parts"]
