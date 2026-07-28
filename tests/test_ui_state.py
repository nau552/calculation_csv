"""ui.state のテスト: Streamlit UI の背後にある純粋な編集ロジック。"""
import shutil

import pytest

from scorelib_param.cli import compute_score_part
from scorelib_param.introspect import axis_catalog
from scorelib_param.models import ScorePart
from ui import state


@pytest.fixture
def catalog(data_dir_mini):
    return axis_catalog(data_dir_mini, "FBC")


@pytest.fixture
def sf():
    return state.empty_score_file()


# ------------------------------------------------------------------- skeleton

def test_skeleton_is_valid_without_relative_preset(catalog):
    """v1: 相対化プリセットは無し — 旧「Read_Override があれば自動ON」は廃止
    （docs/spec_change_dataname_measure.md 9節）。"""
    part = state.part_skeleton("p1", "FBC", catalog)
    assert "relative" not in part
    assert part["order"][0] == "Measure"  # 「どの測定か」の選択が先頭
    assert "InBatchEpoch" not in part["order"]
    assert "DataName" not in part["order"]  # DataName は Measure の表示名の扱い
    # Label/Override は Measure が一意に決める測定メタデータ → 雛形に入れない
    assert not any(a.endswith(("_Label", "_Override")) for a in part["order"])
    assert part["order"][-3:] == ["Board", "Chip", "Block"]
    assert state.validate_part(part) == []


def test_skeleton_default_ops(catalog):
    part = state.part_skeleton("p1", "FBC", catalog)
    aggs = part["aggregations"]
    assert aggs["Measure"] == {"op": "filter", "value": 0}  # 識別子軸: 平均しない
    assert aggs["State"] == {"op": "filter", "value": "R2A"}
    assert aggs["WL"] == {"op": "mean"}


def test_skeleton_keeps_label_axes_for_types_without_measure():
    """Measure 軸の無いカタログ（集計済み type 等）では従来どおり全軸が雛形に
    入る（Label/Override 除外は Measure がある場合だけの規則）。"""
    catalog = {"Read_Override": [False, True], "Board": [0, 1]}
    part = state.part_skeleton("p", "X", catalog)
    assert "Read_Override" in part["order"]


def test_skeleton_computes_as_is(data_dir_mini, catalog):
    """雛形の存在意義そのもの: 一切編集せずに計算が通ること。"""
    part = ScorePart.model_validate(state.part_skeleton("p1", "FBC", catalog))
    value = compute_score_part(data_dir_mini, part)
    assert isinstance(value, float)


def test_default_split_axis_priority(catalog):
    """既定 split 軸: Measure > Read_Override > 他の Override > 先頭の軸。"""
    assert state.default_split_axis(catalog) == "Measure"
    no_measure = {a: c for a, c in catalog.items() if a != "Measure"}
    assert state.default_split_axis(no_measure) == "Read_Override"
    aggregated = {"Board": [0, 1], "Chip": [0, 1]}  # 集計済み type（4b回答の形）
    assert state.default_split_axis(aggregated) == "Board"


# ------------------------------------------------- relative on/off and order

def test_enable_relative_defaults_to_measure_split(catalog):
    part = state.part_skeleton("p", "FBC", catalog)
    state.enable_relative(part, catalog)
    rel = part["relative"]
    assert rel["split_axis"] == "Measure"
    # 位置既定: 候補の先頭 = 分母（基準）、2番目 = 分子（評価）
    assert rel["denominator_when"] == 0
    assert rel["numerator_when"] == 1
    assert "Measure" not in part["order"]  # 相対化が消費するため order には無い
    assert "Measure" not in part["aggregations"]
    assert state.validate_part(part) == []


def test_enable_relative_computes_as_is(data_dir_mini, catalog):
    """相対化ONの既定値（Measure 1/0）のまま計算が通ること。"""
    part = state.part_skeleton("p", "FBC", catalog)
    state.enable_relative(part, catalog)
    value = compute_score_part(data_dir_mini, ScorePart.model_validate(part))
    assert isinstance(value, float)


def test_enable_relative_on_aggregated_type_uses_any_axis():
    """Measure 列の無い集計済み type: split は任意軸（先頭）から。"""
    catalog = {"Board": [0, 1], "Chip": [0, 1, 2, 3]}
    part = state.part_skeleton("p", "SUMMARY", catalog)
    state.enable_relative(part, catalog)
    rel = part["relative"]
    assert rel["split_axis"] == "Board"
    assert (rel["numerator_when"], rel["denominator_when"]) == (1, 0)


def test_enable_relative_without_candidates_shows_validation_error():
    """候補の無い軸が split になった場合、分子/分母はユーザ入力まで None →
    エンジンの「both numerator_when and denominator_when」エラーが表示される。"""
    catalog = {"Foo": None, "Board": [0]}
    part = state.part_skeleton("p", "X", catalog)
    state.enable_relative(part, catalog)
    assert part["relative"]["numerator_when"] is None
    problems = state.validate_part(part)
    assert any("numerator_when" in p for p in problems)


def test_disable_relative_restores_split_axis(catalog):
    part = state.part_skeleton("p", "FBC", catalog)
    state.enable_relative(part, catalog)
    assert "Measure" not in part["order"]
    restored = state.disable_relative(part, catalog)
    assert restored == "Measure"
    assert "relative" not in part
    assert "Measure" in part["order"]
    # 識別子軸の安全なデフォルト（先頭番号の filter）で復帰する
    assert part["aggregations"]["Measure"] == {"op": "filter", "value": 0}
    assert state.validate_part(part) == []


def test_change_split_axis_swaps_order_membership_and_resets_sides(catalog):
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


def test_change_split_axis_drops_stale_labels(catalog):
    part = state.part_skeleton("p", "FBC", catalog)
    state.enable_relative(part, catalog)
    part["relative"]["labels"] = {"1": "evaluation_param_read_level_1"}
    state.change_split_axis(part, "Read_Override", catalog)
    assert "labels" not in part["relative"]


def test_disable_relative_removes_explicit_relative_step(catalog):
    """機能の組み合わせ: UIは __relative__ を order に明示配置できる。
    相対化をOFFにしたらそれも除去されること（相対化設定なしの __relative__
    は検証エラーになるため）。"""
    part = state.part_skeleton("p", "FBC", catalog)
    state.enable_relative(part, catalog)
    part["order"].insert(0, "__relative__")
    state.disable_relative(part, catalog)
    assert "__relative__" not in part["order"]
    assert state.validate_part(part) == []


def test_drop_stale_virtual_steps_on_type_change(catalog):
    part = state.part_skeleton("p", "FBC", catalog)
    part["order"].insert(0, "__dvtbudget__")
    part["type"] = "dVtBudget"
    assert state.drop_stale_virtual_steps(part) is None  # 整合しているので何も起きない
    part["type"] = "FBC"
    assert state.drop_stale_virtual_steps(part) == "__dvtbudget__"
    assert "__dvtbudget__" not in part["order"]
    assert state.validate_part(part) == []


def test_disable_relative_skips_axis_already_in_combined_entry(catalog):
    part = state.part_skeleton("p", "FBC", catalog)
    state.enable_relative(part, catalog)  # split = Measure
    part["order"].insert(0, "State&Measure")
    restored = state.disable_relative(part, catalog)
    assert restored is None  # 複合軸エントリがカバー済み → 再追加されない
    assert part["order"].count("Measure") == 0


# ------------------------------------------------- Measure の labels 注記

def test_annotate_measure_labels_relative():
    mlabels = {0: "reference_param_read_level_1", 1: "evaluation_param_read_level_1"}
    rel = {"split_axis": "Measure", "numerator_when": 1, "denominator_when": 0}
    state.annotate_measure_labels(rel, mlabels)
    assert rel["labels"] == {
        "1": "evaluation_param_read_level_1",
        "0": "reference_param_read_level_1",
    }


def test_annotate_measure_labels_filter_and_unnamed():
    mlabels = {1: "evaluation_param_read_level_1"}
    spec = {"op": "filter", "value": [1, 2]}
    state.annotate_measure_labels(spec, mlabels)
    assert spec["labels"] == {"1": "evaluation_param_read_level_1"}  # 名無し番号は入らない
    unnamed = {"op": "filter", "value": 5}
    state.annotate_measure_labels(unnamed, {})
    assert "labels" not in unnamed
    # 名前が無くなったら（ラベル無しデータへ切替等）注記ごと消える
    spec2 = {"op": "filter", "value": 3, "labels": {"3": "old"}}
    state.annotate_measure_labels(spec2, {})
    assert "labels" not in spec2


# ------------------------------------------------------- ダミー展開の入力

def test_parse_chip_counts():
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


def test_expand_dummy_bundle_roundtrip(tmp_path, data_dir_mini):
    """疑似ダミー → expand_dummy_bundle → build_context が通しで動くこと
    （画面1のダミー展開ボタンの中身）。"""
    from scorelib_param.dummy import make_pseudo_dummy

    pseudo = make_pseudo_dummy(data_dir_mini, tmp_path / "pseudo")
    expanded = state.expand_dummy_bundle(str(pseudo), [2, 3])
    ctx = state.build_context(expanded)
    assert "FBC" in ctx["types"]
    assert ctx["catalogs"]["FBC"]["Board"] == [0, 1]
    assert ctx["measure_labels"]["FBC"][1] == "evaluation_param_read_level_1"


# ------------------------------------------------------------------- editing

def test_unique_part_name(sf):
    sf["score_parts"].append({"name": "part_1"})
    assert state.unique_part_name(sf) == "part_2"


def test_duplicate_part(sf, catalog):
    """本番と同じ形の入力で検証する: アプリが複製する時点でパーツは _uid を
    持っている（_uid を共有するとウィジェットも共有され、2パーツが互いの
    名前・相対化を静かに上書きし合う実バグがあった）。"""
    sf["score_parts"].append(state.part_skeleton("p", "FBC", catalog))
    state.ensure_uids(sf)
    idx = state.duplicate_part(sf, 0)
    state.ensure_uids(sf)
    assert idx == 1
    assert sf["score_parts"][1]["name"] == "p_1"
    assert sf["score_parts"][0]["_uid"] != sf["score_parts"][1]["_uid"]
    sf["score_parts"][1]["aggregations"]["WL"]["op"] = "sum"
    assert sf["score_parts"][0]["aggregations"]["WL"]["op"] == "mean"  # 深いコピー（元は不変）


def test_part_list_labels_marker_handle_and_warning(sf, catalog):
    """ドラッグリストのラベルには ⠿ ハンドル・⚠ 検証マーカー・← 編集中 が
    付く。D&D部品自体は AppTest から観測できないため、この純関数の側で
    ロジックを検証する。"""
    a = state.part_skeleton("a", "FBC", catalog)
    b = state.part_skeleton("b", "FBC", catalog)
    sf["score_parts"] = [a, b]
    state.ensure_uids(sf)
    labels = state.part_list_labels(sf, b["_uid"], {a["_uid"]})
    assert labels[0].startswith("⠿ ⚠ 1. a（")
    assert "編集中" not in labels[0]
    assert labels[1].startswith("⠿ 2. b（")
    assert labels[1].endswith("← 編集中")


def test_part_select_labels_unique_even_with_duplicate_names(sf, catalog):
    """Streamlit の selectbox は表示ラベルで項目を照合する: 誤って同名に
    なった2パーツにも別々のプルダウンラベルが付かないと、片方をクリック
    したらもう片方が選ばれる（実バグ）。"""
    a = state.part_skeleton("dAR_margin", "FBC", catalog)
    b = state.part_skeleton("dAR_margin", "FBC", catalog)
    sf["score_parts"] = [a, b]
    state.ensure_uids(sf)
    labels = state.part_select_labels(sf, {a["_uid"], b["_uid"]})
    assert labels[a["_uid"]] == "1. ⚠ dAR_margin"
    assert labels[b["_uid"]] == "2. ⚠ dAR_margin"
    assert len(set(labels.values())) == 2


def test_ensure_uids_repairs_duplicated_ids(sf, catalog):
    """_uid 重複バグの期間に保存された下書きが、開くだけで治ること。"""
    a = state.part_skeleton("a", "FBC", catalog)
    b = state.part_skeleton("b", "FBC", catalog)
    a["_uid"] = b["_uid"] = "same1234"
    sf["score_parts"] = [a, b]
    state.ensure_uids(sf)
    assert a["_uid"] != b["_uid"]
    assert a["_uid"] == "same1234"  # 最初の1つは安定して保持される


def test_move_entry():
    lst = ["a", "b", "c"]
    assert state.move_entry(lst, 0, +1) == 1
    assert lst == ["b", "a", "c"]
    assert state.move_entry(lst, 0, -1) == 0  # no-op at the edge
    assert lst == ["b", "a", "c"]


# -------------------------------------------------------------- selection sets

def test_referencing_parts_and_guarded_delete(sf):
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


def test_save_set_as(sf):
    sf["selectionSets"]["ud"] = [{"State": "R2A", "Read_Label": "u1"}]
    state.save_set_as(sf, "ud", "ud2")
    sf["selectionSets"]["ud2"][0]["State"] = "A2B"
    assert sf["selectionSets"]["ud"][0]["State"] == "R2A"  # 深いコピー（元は不変）
    with pytest.raises(ValueError, match="既に存在"):
        state.save_set_as(sf, "ud", "ud2")


# ----------------------------------------------------------------- group defs

def test_import_config_group_defs(sf):
    wlgroup = {"g1": (0, 3), "g2": (4, 8)}
    assert state.import_config_group_defs(sf, wlgroup) is True
    assert sf["groupDefs"]["WLgroup"] == {
        "axis": "WL", "groups": {"g1": [0, 3], "g2": [4, 8]}, "definedInLogical": True,
    }
    # 再取り込み（や先の編集）で編集可能コピーが上書きされてはならない
    sf["groupDefs"]["WLgroup"]["groups"]["g1"] = [0, 5]
    assert state.import_config_group_defs(sf, wlgroup) is False
    assert sf["groupDefs"]["WLgroup"]["groups"]["g1"] == [0, 5]
    assert state.import_config_group_defs(sf, None) is False


def test_import_config_group_defs_physical_and_weight(sf):
    wlgroup = {"g1": (0, 3)}
    assert state.import_config_group_defs(
        sf, wlgroup, defin_logical=False, wlgroup_weight={"g1": 2.0}
    ) is True
    assert sf["groupDefs"]["WLgroup"]["definedInLogical"] is False
    assert sf["weightSets"]["WLgroupWeight"] == {"g1": 2.0}
    # 既に編集済みの重みセットは上書きしない
    sf["weightSets"]["WLgroupWeight"] = 5.0
    state.import_config_group_defs(sf, None, wlgroup_weight={"g1": 9.0})
    assert sf["weightSets"]["WLgroupWeight"] == 5.0


def test_group_def_delete_guarded_by_references(sf, catalog):
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


def test_add_group_def_rejects_collisions(sf, catalog):
    with pytest.raises(ValueError, match="入力してください"):
        state.add_group_def(sf, "  ", "WL", set(catalog))
    with pytest.raises(ValueError, match="軸名と衝突"):
        state.add_group_def(sf, "WL", "WL", set(catalog))
    state.add_group_def(sf, "STRgroup", "STR", set(catalog))
    assert sf["groupDefs"]["STRgroup"] == {"axis": "STR", "groups": {}}
    with pytest.raises(ValueError, match="既に存在"):
        state.add_group_def(sf, "STRgroup", "STR", set(catalog))


def test_export_part_bundles_group_defs(sf, catalog):
    part = state.part_skeleton("p", "FBC", catalog)
    part["order"].append("WLgroup")
    part["aggregations"]["WLgroup"] = {"op": "max"}
    sf["score_parts"].append(part)
    sf["groupDefs"]["WLgroup"] = {"axis": "WL", "groups": {"g1": [0, 100]}}
    sf["groupDefs"]["unused"] = {"axis": "STR", "groups": {"a": [0, 1]}}
    back = state.import_score_file(state.export_part(sf, 0))
    assert list(back["groupDefs"]) == ["WLgroup"]  # 参照している定義だけ同梱される


def test_validate_weight_ref_resolves_against_weight_sets(sf, catalog):
    """重みセット ref の検証: 定義済みなら通り、未定義なら分かるエラーになる。
    （回帰: 検証層が weightSets を渡さず、定義済みでも常に
    "unknown weight set" になっていた）"""
    part = state.part_skeleton("p", "FBC", catalog)
    part["order"].insert(0, "__weight__")
    part["aggregations"]["__weight__"] = {"op": "mul", "by": "WLgroup", "ref": "WLgroupWeight"}
    sf["score_parts"].append(part)
    sf["groupDefs"]["WLgroup"] = {"axis": "WL", "groups": {"g1": [0, 1000]}}

    # 未定義 → 検証で捕まる（計算まで行かない）
    assert any("WLgroupWeight" in p for p in state.validate_score_file(sf))
    assert state.validate_part(part, sf["selectionSets"], sf.get("weightSets"))

    # 定義済み → 検証を通る
    sf["weightSets"]["WLgroupWeight"] = {"g1": 2.0}
    assert state.validate_score_file(sf) == []
    assert state.validate_part(part, sf["selectionSets"], sf["weightSets"]) == []


def test_export_part_bundles_weight_sets(sf, catalog):
    part = state.part_skeleton("p", "FBC", catalog)
    part["order"] = ["WL", "__weight__", "WLgroup"] + [
        e for e in part["order"] if e not in ("WL",)
    ]
    part["aggregations"]["WLgroup"] = {"op": "max"}
    part["aggregations"]["__weight__"] = {"op": "mul", "by": "WLgroup", "ref": "WLgroupWeight"}
    sf["score_parts"].append(part)
    sf["groupDefs"]["WLgroup"] = {"axis": "WL", "groups": {"g1": [0, 100]}}
    sf["weightSets"]["WLgroupWeight"] = {"g1": 2.0}
    sf["weightSets"]["unusedW"] = 1.5
    back = state.import_score_file(state.export_part(sf, 0))
    assert back["weightSets"] == {"WLgroupWeight": {"g1": 2.0}}  # 参照分だけ同梱
    assert "WLgroup" in back["groupDefs"]


def test_run_test_compute_with_weight_step(sf, catalog, data_dir_mini):
    """UI経由の一気通貫: WLgroup 別の重みステップつきパーツが実データで
    計算でき、重み全1なら重みなしと一致する。"""
    def make(with_weight):
        f = state.empty_score_file()
        part = state.part_skeleton("p", "FBC", catalog)
        part["order"].append("WLgroup")
        part["aggregations"]["WLgroup"] = {"op": "max"}
        if with_weight:
            part["order"].insert(part["order"].index("WLgroup"), "__weight__")
            part["aggregations"]["__weight__"] = {
                "op": "mul", "by": "WLgroup", "ref": "WLgroupWeight",
            }
            f["weightSets"]["WLgroupWeight"] = {"low": 1.0, "high": 1.0}
        f["groupDefs"]["WLgroup"] = {"axis": "WL", "groups": {"low": [0, 10], "high": [11, 1000]}}
        f["score_parts"].append(part)
        f["expression"] = "p"
        return f

    weighted = state.run_test_compute(make(True), str(data_dir_mini))
    plain = state.run_test_compute(make(False), str(data_dir_mini))
    assert weighted["p"] == pytest.approx(plain["p"])


def test_run_test_compute_with_group_axis(sf, catalog, data_dir_mini):
    """UI経由の一気通貫: 派生グループ軸を使うパーツが実データで計算できる
    （グループ間集計を最後に回すユーザシナリオ）。"""
    part = state.part_skeleton("p", "FBC", catalog)
    part["order"].append("WLgroup")
    part["aggregations"]["WLgroup"] = {"op": "max"}
    sf["score_parts"].append(part)
    sf["expression"] = "p"
    sf["groupDefs"]["WLgroup"] = {"axis": "WL", "groups": {"low": [0, 10], "high": [11, 1000]}}
    result = state.run_test_compute(sf, str(data_dir_mini))
    assert isinstance(result["p"], float)


# ---------------------------------------------------------------- validation

def test_validate_score_file_ok(sf, catalog):
    sf["score_parts"].append(state.part_skeleton("p", "FBC", catalog))
    sf["expression"] = "p"
    assert state.validate_score_file(sf) == []


def test_validate_reports_engine_errors(sf):
    sf["score_parts"].append(
        {"name": "p", "type": "FBC", "order": ["State"], "aggregations": {"State": {"op": "filter"}}}
    )
    problems = state.validate_score_file(sf)
    assert any("filter" in p for p in problems)


def test_validate_expression_unknown_part(sf, catalog):
    sf["score_parts"].append(state.part_skeleton("p", "FBC", catalog))
    sf["expression"] = "p + nope"
    problems = state.validate_score_file(sf)
    assert any("expression" in p for p in problems)


def test_validate_duplicate_names_and_dangling_constraint(sf, catalog):
    sf["score_parts"] = [
        state.part_skeleton("p", "FBC", catalog),
        state.part_skeleton("p", "FBC", catalog),
    ]
    sf["constraintThreshold"] = {"gone": {"value": 1.0}}
    problems = state.validate_score_file(sf)
    assert any("重複" in p for p in problems)
    assert any("gone" in p for p in problems)


def test_validate_unknown_ref(sf, catalog):
    part = state.part_skeleton("p", "FBC", catalog)
    part["aggregations"]["State"] = {"op": "sum", "ref": "nope"}
    sf["score_parts"].append(part)
    problems = state.validate_score_file(sf)
    assert any("nope" in p for p in problems)


# ------------------------------------------------------------------- context

def test_build_context(data_dir_mini):
    ctx = state.build_context(str(data_dir_mini))
    assert ctx["types"] == ["FBC", "tR"]
    assert ctx["part_types"] == ["FBC", "tR"]  # mini ディレクトリに係数jsoncは無い
    assert "Page" in ctx["catalogs"]["tR"]
    assert ctx["has_initial_temperature"] is True
    assert ctx["config_path"] is None


def test_build_context_missing_dir():
    with pytest.raises(ValueError, match="見つかりません"):
        state.build_context("no/such/dir")


def test_build_context_empty_path_rejected():
    """Python では Path('') はカレントディレクトリ扱い。空入力のまま、
    アプリの起動場所を黙って走査してはならない。"""
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="入力してください"):
            state.build_context(bad)


def test_build_context_explicit_coef(data_dir_mini, dvtbudget_coef_path):
    """係数jsoncは通常 result_tmp の外にあり、独立したパスで指定される。"""
    ctx = state.build_context(str(data_dir_mini), coef_path=str(dvtbudget_coef_path))
    assert ctx["part_types"] == ["FBC", "tR", "dVtBudget"]
    assert ctx["coef_source"] == "指定"
    assert "dVtBudget" in ctx["catalogs"]


def test_build_context_explicit_config(data_dir_mini, fixtures_dir):
    ctx = state.build_context(str(data_dir_mini), config_path=str(fixtures_dir / "config.jsonc"))
    assert ctx["config_source"] == "指定"
    assert ctx["generation"]
    assert ctx["wlgroup"]


def test_build_context_missing_explicit_file_rejected(data_dir_mini):
    with pytest.raises(ValueError, match="dVtBudget係数jsonc が見つかりません"):
        state.build_context(str(data_dir_mini), coef_path="no/such.jsonc")


def test_build_context_in_dir_discovery_still_works(tmp_path, data_dir_mini, dvtbudget_coef_path):
    d = tmp_path / "run"
    shutil.copytree(data_dir_mini, d)
    shutil.copy(dvtbudget_coef_path, d / "dvtbudget_coef.jsonc")
    ctx = state.build_context(str(d))
    assert ctx["coef_source"] == "自動検出"
    assert "dVtBudget" in ctx["part_types"]


# ----------------------------------------------------------- generation info

def test_build_context_geninfo_explicit(data_dir_mini, fixtures_dir):
    ctx = state.build_context(str(data_dir_mini), geninfo_path=str(fixtures_dir / "B9LS.json"))
    assert ctx["geninfo_source"] == "指定"
    assert state.axis_counts(ctx["geninfo"]) == {"WL": 6, "STR": 3}


def test_build_context_geninfo_discovered_via_generation(tmp_path, data_dir_mini, fixtures_dir):
    d = tmp_path / "run"
    shutil.copytree(data_dir_mini, d)
    shutil.copy(fixtures_dir / "config.jsonc", d / "config.jsonc")  # Generation: B9LS
    shutil.copy(fixtures_dir / "B9LS.json", d / "B9LS.json")
    ctx = state.build_context(str(d))
    assert ctx["geninfo_source"] == "自動検出"
    assert ctx["geninfo"]["numWLs"] == 6


def test_build_context_missing_geninfo_rejected(data_dir_mini):
    with pytest.raises(ValueError, match="世代情報json が見つかりません"):
        state.build_context(str(data_dir_mini), geninfo_path="no/such.json")


def test_group_def_warnings(sf):
    counts = {"WL": 6, "STR": 3}
    sf["groupDefs"]["WLgroup"] = {"axis": "WL", "groups": {"g1": [0, 3], "g2": [4, 8]}}
    warns = state.group_def_warnings(sf, counts)
    assert any("範囲外" in w and "g2(4–8)" in w for w in warns)

    sf["groupDefs"]["WLgroup"]["groups"] = {"g1": [0, 3]}
    warns = state.group_def_warnings(sf, counts)
    assert any("4–5" in w and "どのグループにも入りません" in w for w in warns)

    # 範囲が本数と合っていれば無音。本数の分からない軸はチェック対象外
    sf["groupDefs"]["WLgroup"]["groups"] = {"g1": [0, 3], "g2": [4, 5]}
    sf["groupDefs"]["PageG"] = {"axis": "Page", "groups": {"x": [0, 99]}}
    assert state.group_def_warnings(sf, counts) == []
    assert state.group_def_warnings(sf, {}) == []


def test_data_axis_counts_and_validation_counts(data_dir_mini):
    """本数はデータ（カタログ）由来が正。世代情報 json は補完のみで、
    食い違いは診断警告になる。"""
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
    assert len(warns) == 1 and "100" in warns[0] and "6" in warns[0]


# ------------------------------------------------------- 設定のみ編集モード

def test_load_config_only_from_run_config(fixtures_dir):
    """設定 jsonc だけからの編集開始: データ無しで検証・編集・エクスポートに
    必要な情報が揃うこと。"""
    text = (fixtures_dir / "config.jsonc").read_text(encoding="utf-8")
    sf, ctx = state.load_config_only(text)
    assert ctx["config_only"] is True
    assert ctx["data_dir"] is None
    assert sf["score_parts"]
    # 旧来の optimization.WLgroup も編集可能なグループ定義として取り込まれる
    assert "WLgroup" in sf["groupDefs"]
    # カタログは設定が言及する軸名（値候補は無し = 自由入力）
    assert ctx["part_types"]
    cat = ctx["catalogs"][ctx["part_types"][0]]
    assert cat and all(v is None for v in cat.values())
    assert not any(a.startswith("__") for a in cat)  # 仮想ステップは軸ではない
    # 検証・エクスポートはデータ非依存で動く
    assert state.validate_score_file(sf) == []
    assert state.score_file_to_jsonc(sf)


def test_load_config_only_from_score_jsonc(sf, catalog):
    """エクスポートした score.jsonc 形式も読める（式・グループ定義の微修正 →
    再エクスポートの往復）。"""
    sf["score_parts"].append(state.part_skeleton("p", "FBC", catalog))
    sf["expression"] = "p"
    text = state.score_file_to_jsonc(sf)
    sf2, ctx = state.load_config_only(text)
    assert ctx["config_only"] is True
    sf2["expression"] = "p * 2"
    assert state.validate_score_file(sf2) == []
    assert "p * 2" in state.score_file_to_jsonc(sf2)


def test_is_run_config_text(fixtures_dir, sf, catalog):
    """①の設定を build_context の config として渡せる形式かの判定。"""
    assert state.is_run_config_text((fixtures_dir / "config.jsonc").read_text(encoding="utf-8"))
    sf["score_parts"].append(state.part_skeleton("p", "FBC", catalog))
    assert not state.is_run_config_text(state.score_file_to_jsonc(sf))  # ScoreFile 形式
    assert not state.is_run_config_text("not json at all")


def test_config_only_skeleton_measure_requires_input():
    """設定のみ編集の雛形: Measure は候補が無いので value 未入力の filter になり、
    番号を入れるまで検証エラーで促される（mean だと測定が静かに混ざるため）。"""
    catalog = {"Measure": None, "State": None, "Board": None}
    part = state.part_skeleton("p", "X", catalog)
    assert part["aggregations"]["Measure"] == {"op": "filter", "value": None}
    problems = state.validate_part(part)
    assert any("filter" in p and "value" in p for p in problems)


# ----------------------------------------------------- アップロード入力経路

def test_save_upload():
    from pathlib import Path

    p = Path(state.save_upload("config.jsonc", b"{}"))
    assert p.is_file()
    assert p.name == "config.jsonc"
    assert p.read_bytes() == b"{}"
    # パス成分は捨てられる（アップロード名による書き込み先操作の防止）
    p2 = Path(state.save_upload("../evil.py", b"x"))
    assert p2.name == "evil.py"


def test_dummy_zip_upload_flow(tmp_path, data_dir_mini):
    """ダミー一式を zip でアップロードする経路（画面1）: 展開 → Board/Chip
    複製 → 通常読み込み、が通しで動くこと。フォルダごと圧縮された zip の
    「トップに1フォルダ」形も extract_bundle_zip が吸収する。"""
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

def test_validation_error_names_the_part(sf):
    sf["score_parts"].append(
        {"name": "myPart", "type": "FBC", "order": ["State"],
         "aggregations": {"State": {"op": "filter"}}}
    )
    problems = state.validate_score_file(sf)
    assert any("パーツ 'myPart'" in p for p in problems)


def test_import_error_names_the_part():
    import json

    text = json.dumps(
        {
            "score_parts": [
                {"name": "old_part", "type": "FBC", "order": ["WL"],
                 "aggregations": {"WL": {"op": "group_reduce", "group_def": "WLgroup"}}}
            ],
            "expression": "old_part",
        }
    )
    with pytest.raises(ValueError, match="old_part"):
        state.import_score_file(text)


# -------------------------------------------------------- custom parts / zip

@pytest.fixture
def custom_parts_path(fixtures_dir):
    return fixtures_dir / "custom_parts.py"


def test_build_context_custom_explicit(data_dir_mini, custom_parts_path):
    ctx = state.build_context(str(data_dir_mini), custom_path=str(custom_parts_path))
    assert ctx["custom_source"] == "指定"
    assert "fixed_value" in ctx["custom_functions"]
    assert "custom" in ctx["part_types"]
    assert "custom" not in ctx["catalogs"]  # pseudo-type, no axis catalog


def test_build_context_without_custom_hides_type(data_dir_mini):
    ctx = state.build_context(str(data_dir_mini))
    assert "custom" not in ctx["part_types"]


def test_custom_part_skeleton_and_compute(sf, data_dir_mini, custom_parts_path):
    part = state.custom_part_skeleton("p", ["mean_fbc_plus_offset"])
    part["params"] = {"offset": 5}
    sf["score_parts"].append(part)
    sf["expression"] = "p"
    assert state.validate_score_file(sf) == []
    result = state.run_test_compute(sf, str(data_dir_mini), custom_path=str(custom_parts_path))
    assert isinstance(result["p"], float)


def test_switch_part_type_strips_mismatched_fields(sf, catalog):
    part = state.part_skeleton("p", "FBC", catalog)
    state.switch_part_type(part, "custom")
    assert "order" not in part and "relative" not in part and "aggregations" not in part
    assert part["function"] == "p"
    assert state.validate_part(part) == []
    state.switch_part_type(part, "FBC")
    assert "function" not in part and "params" not in part
    assert part["order"] == []
    assert state.validate_part(part) == []


def _bundle_zip(data_dir_mini, fixtures_dir, custom_parts_path, layout) -> bytes:
    """layout: arcname prefix for the measurement csvs (companions at root)."""
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


def test_bundle_zip_flat_layout(data_dir_mini, fixtures_dir, custom_parts_path):
    """全部1フォルダに入った形: そのフォルダ自体が測定ディレクトリになる。"""
    data = _bundle_zip(data_dir_mini, fixtures_dir, custom_parts_path, "bundle")
    found = state.locate_bundle_inputs(state.extract_bundle_zip(data))
    ctx = state.build_context(
        found["data_dir"], found["config_path"], found["coef_path"],
        found["geninfo_path"], found["custom_path"],
    )
    assert ctx["types"] == ["FBC", "tR"]
    assert ctx["geninfo"]["numWLs"] == 6
    assert "custom" in ctx["part_types"]


def test_bundle_zip_nested_layout(data_dir_mini, fixtures_dir, custom_parts_path):
    """GUIが作る自然な構成: 測定csvは result_tmp サブフォルダ内、同梱
    ファイルはルート — サブディレクトリも探索するのでこれも読めること。"""
    data = _bundle_zip(data_dir_mini, fixtures_dir, custom_parts_path, "bundle/result_tmp")
    found = state.locate_bundle_inputs(state.extract_bundle_zip(data))
    assert found["data_dir"].endswith("result_tmp")
    assert found["config_path"] and found["geninfo_path"] and found["custom_path"]
    ctx = state.build_context(
        found["data_dir"], found["config_path"], found["coef_path"],
        found["geninfo_path"], found["custom_path"],
    )
    assert ctx["types"] == ["FBC", "tR"]
    assert "custom" in ctx["part_types"]


def test_bundle_zip_ambiguous_data_dirs_rejected(data_dir_mini, fixtures_dir, custom_parts_path):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for f in data_dir_mini.iterdir():
            z.write(f, f"bundle/epoch1/{f.name}")
            z.write(f, f"bundle/epoch2/{f.name}")
    with pytest.raises(ValueError, match="測定結果ディレクトリの候補が複数"):
        state.locate_bundle_inputs(state.extract_bundle_zip(buf.getvalue()))


def test_in_dir_discovery_rejects_ambiguous_configs(tmp_path, data_dir_mini, fixtures_dir):
    """設定jsoncの形に合うファイルが2つ: 黙って選ばず拒否すること
    （以前はアルファベット順の先頭が無言で採用されていた）。"""
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

def test_draft_path_for_sanitizes(monkeypatch, tmp_path):
    """ユーザ名ごとの下書きパス: パス区切り等はファイル名安全な文字へ正規化
    （名前入力によるディレクトリ脱出の防止）。"""
    monkeypatch.setattr(state, "DRAFTS_DIR", tmp_path)
    p = state.draft_path_for("田中 ../evil")
    assert p.parent == tmp_path
    assert p.suffix == ".jsonc"
    assert "/" not in p.name and "\\" not in p.name and ".." not in p.name
    assert state.draft_path_for("  ") .name == "_.jsonc"  # 空相当は "_"


def test_draft_roundtrip(tmp_path, sf, catalog):
    sf["score_parts"].append(state.part_skeleton("p", "FBC", catalog))
    path = tmp_path / "draft.jsonc"
    state.save_draft(sf, {"data_dir": "somewhere"}, path)
    draft = state.load_draft(path)
    assert draft["score_file"] == sf
    assert draft["context_inputs"]["data_dir"] == "somewhere"
    assert state.load_draft(tmp_path / "none.jsonc") is None


def test_draft_legacy_format_accepted(tmp_path, sf, catalog):
    """context_inputs 導入前の旧形式（素の ScoreFile dict）の下書きも読めること。"""
    import json

    sf["score_parts"].append(state.part_skeleton("p", "FBC", catalog))
    path = tmp_path / "draft.jsonc"
    path.write_text(json.dumps(sf), encoding="utf-8")
    draft = state.load_draft(path)
    assert draft["score_file"] == sf
    assert draft["context_inputs"] == {}


def test_export_and_import_roundtrip(sf, catalog):
    sf["score_parts"].append(state.part_skeleton("p", "FBC", catalog))
    sf["expression"] = "p"
    text = state.score_file_to_jsonc(sf)
    back = state.import_score_file(text)
    assert [p["name"] for p in back["score_parts"]] == ["p"]
    assert back["expression"] == "p"


def test_import_run_config(fixtures_dir):
    text = (fixtures_dir / "config.jsonc").read_text(encoding="utf-8")
    imported = state.import_score_file(text)
    assert imported["score_parts"]
