# コードリファレンス（scorelib_param / ui / scripts）

このファイルは「**今のコードに何があるか**」を説明するリファレンスです（チーム内での報告・引き継ぎ用）。
「なぜこの設計にしたか」の経緯は `score_gui_design.md`（エンジン）と `score_gui_ui_design.md`（UI）、
使い方はリポジトリ直下の `README.md`、テストの解説は `testing_guide.md` を参照してください。
コメント・docstring はコード側もすべて日本語です。
**コードを変更したら本ファイルも追随更新すること。**

## 全体アーキテクチャ

```
config.jsonc（スコア定義）      測定結果ディレクトリ（result_tmp: csv群）
        │                              │
        ▼                              ▼
┌──────────────────────────── scorelib（エンジン）────────────────────────────┐
│ jsonc/io_jsonc → models(検証) → axis_resolve(csv結合) → aggregate/relative/ │
│ dvtbudget(計算) → expression(式) → cli(全体を束ねる入口)                     │
│ introspect: 測定ディレクトリから type/軸/値候補を導出（UI向けメタデータ）     │
│ custom: 自作Python関数パーツ（custom_parts.py）の読み込み・実行              │
└─────────────────────────────────────────────────────────────────────────────┘
        ▲ import（UIはエンジンの薄いラッパー。検証・計算はすべてエンジン側）
┌──────────────────────────────── ui（Streamlit）─────────────────────────────┐
│ state.py: 編集ロジック（純関数、pytest対象） / widgets.py: 再利用部品        │
│ app.py: 5画面のウィジェット配置と session_state の受け渡しのみ               │
└─────────────────────────────────────────────────────────────────────────────┘
```

- データの流れ（1パーツの計算）: `{type}.csv` を読み、`parameterLabel_{type}.csv` /
  `map_*.csv` を必要な軸だけ結合（axis_resolve）→ グループ派生列の生成（cli）→
  order の各ステップを上から順に適用（`__relative__` → relative.py、`__dvtbudget__` →
  dvtbudget.py、軸エントリ → aggregate.py）→ 全軸を潰し切って1スカラー。
- 全パーツの値を expression（expression.py）で合成したものが Score。

---

## scorelib_param/（エンジン）

### `scorelib_param/__init__.py`
- `__version__` — エンジンの版。SVNへ同期するたびに上げる（UIサイドバー・CLIに表示）。
  **版上げはこの1行だけ**: pyproject.toml は dynamic 設定でこの値を参照する
  （手順は README「バージョンの上げ方」）。

### `scorelib_param/jsonc.py` — コメント付きJSONの読み書き
| 関数 | 内容 |
|---|---|
| `strip_jsonc_comments(text)` | `//` と `/* */` コメントを文字列リテラル内を壊さずに除去（1文字ずつ走査） |
| `loads(text)` / `load(path)` | コメント・末尾カンマを除去してから `json.loads` |
| `dumps(obj)` / `dump(obj, path)` | 整形付き書き出し（ensure_ascii=False） |

設計: 外部ライブラリを増やさないための最小実装。この形式（JSON+コメント+末尾カンマ）で十分なため。

### `scorelib_param/io_jsonc.py` — pydanticモデル⇔jsoncファイルの入出力
`load_run_config` / `save_run_config` / `load_score_file` / `save_score_file` /
`load_dvtbudget_coef` の5関数。すべて「jsonc.load → models の model_validate」の薄い糊。

### `scorelib_param/expression.py` — 自由記述式の評価
| 関数 | 内容 |
|---|---|
| `evaluate_expression(expr, variables)` | simpleeval（サンドボックス評価器）で式を評価。パーツ合成式と `expr` op の両方で共用 |
| `_make_functions()` / `_mean()` | 式で使える関数の登録（log=log10, ln, log2, exp, sqrt, min, max, mean, sum, abs） |

### `scorelib_param/models.py` — データモデルと検証（エンジンの「文法」）
| 定義 | 内容 |
|---|---|
| `COMBINED_SEP = "&"` | 複合軸の区切り（`"State&Read_Label"`） |
| `CUSTOM_TYPE = "custom"` | 自作関数パーツの type 値 |
| `MULTI_OPS` | value で対象を絞れる集計op（mean/sum/min/max）。UIと共有 |
| `AggregationSpec` | 1エントリの集計指示。op/value/ref/expr/by + 集計時重み `weight`/`weight_ref`（mean系専用: その軸を潰す直前に値へ乗算。正規化された加重平均ではない）+ `labels` 注記（値→表示名。実行には不使用 — Measure 番号に dataName を残す用）。filter の value はスカラー（等値）またはリスト（is_in）。before検証で旧表記（`*_subset`、`values`、廃止済み `group_reduce`）を変換/エラー化、after検証で opごとの value/weight 形状を検査 |
| `AxisAggregation` | 上に axis 名が付いたもの（分母事前集計はリストなので軸名を自分で持つ） |
| `RelativeConfig` | 相対化設定（split_axis / numerator_when / denominator_when / mode / denominator_offset / denominator_pre_aggregation + `labels` 注記）。廃止済み `enabled: false` は明示エラー。分子/分母の None（未設定）は「必ず0行マッチになる設定忘れ」として明示エラー（UIの未入力表示の実体） |
| `ScorePart` | 1スコアパーツ。name/type/relative/order/aggregations + custom用の function/params。custom と集計フィールドの混在を拒否。複合軸の辞書選択の形状検査。`resolve_selection_refs()` で ref を選択セットの中身に展開して再検証 |
| `GroupDef` | グループ派生軸の定義（対象軸 + グループ名→[lo, hi]） |
| `ScoreFile` | ユーザが作る内容一式（score_parts / expression / constraintThreshold / selectionSets / groupDefs）。自己完結でエクスポートされる単位 |
| `OptimizationConfig` / `RunConfig` | 実行時 config（Generation + optimization{}）。`to_score_file()` で ScoreFile 部分を取り出し、`group_defs()` で旧 WLgroup（WL への定義として互換読み）と groupDefs を統合（groupDefs 優先） |
| `DvtBudgetCoefFile` | 係数表（世代→温度→State→{a, b}）のルートモデル |

設計: **検証はすべてここに集約**し、UIも同じモデルで検証する（二重実装しない）。

### `scorelib_param/axis_resolve.py` — 必要な軸だけのcsv結合
| 定義 | 内容 |
|---|---|
| `JOIN_KEYS` | `(InBatchEpoch, Board, Chip, Block, Measure)`。測定csvとラベルcsvの結合キー |
| `resolve_axes(data_dir, type_, required_axes)` | `{type}.csv` に、要求された軸のぶんだけ `parameterLabel_` / `dataName_` / `map_*` を lazy join し、値列+軸列の LazyFrame を返す。Override列は Boolean 正規化。要求されない列（InBatchEpoch等）は最後に落とす。**Measure は結合キーだが、軸として要求されたら残す**（相対化・filter の識別子軸 — docs/spec_change_dataname_measure.md） |
| `_map_file_for_axis` | 軸名→対応する map ファイル名の規約（`*_Label`→map_Label.csv 等） |

設計: 全展開（FBC_expanded.csv 相当）を作らず、パーツが言及した軸だけを結合する。

### `scorelib_param/aggregate.py` — orderの逐次集計
| 関数 | 内容 |
|---|---|
| `group_column_expr(axis, ranges)` | グループ派生列を作る polars 式（範囲→グループ名） |
| `_per_value_operand(lf, axis, mapping, what)` | {軸の値: 定数} を行ごとの定数式へ。辞書に無い値の行は一覧つきエラー（変換の by 重みと集計時重みで共用） |
| `apply_transform(lf, col, spec)` | 軸を潰さない行単位変換（add/sub/mul/div。`__offset__`/`__weight__` 等の仮想ステップ用） |
| `apply_axis_op(lf, col, axis, spec, group_keys)` | 1軸を1つの指示で潰す。filter（スカラー=等値 / リスト=is_in。どちらも軸列を落とし、is_in の残行は後段集計に複製として流れる）/ mean系（value で対象限定可、`weight` で集計直前に重み乗算）/ diff（a−b の自己結合）/ expr（グループごとに評価） |
| `apply_aggregations(lf, col, order, aggregations)` | order を上から順に適用。**残っている全列をグループキー**にするのが要（グループ派生列が自然にキーとして生き残る仕組み） |
| `collapse` / `collapse_to_scalar` | 潰し残しの列や null（filterが0行等）を検出してエラーにし、1スカラーを返す |
| `aggregate_score_part` | 上2つをつないだ入口 |

### `scorelib_param/relative.py` — 相対化
`apply_relative(lf, col, relative)` のみ。split_axis で分子/分母に分け、分母だけ事前集計
（denominator_pre_aggregation）してから、**その時点で残っている全列一致**で左結合し、
ratio（`(分子+o)/(分母+o)`）または diff（`分子−分母`）を計算する。

### `scorelib_param/dummy.py` — ダミー一式の Board/Chip 展開（測定前設計）
| 関数 | 内容 |
|---|---|
| `expand_boards_chips(src, dest, chip_counts)` | Board/Chip 1つのダミー一式を複製展開（`chip_counts[b]` = Board b の Chip 数。Board別可）。Board列を持つ csv は行複製、initial_temperature.csv（ヘッダ無し）は Board ごとに1行、map_*/json はコピー。元が複数 Board/Chip ならエラー |
| `make_pseudo_dummy(src, dest)` | 逆方向: 正データを Board/Chip 1つ（0 に正規化）へ削る。ダミー納品前の開発・検証用（scripts/make_pseudo_dummy.py から呼ばれる） |

設計: 展開は**行の複製だけ**を行い数値を作らない（mean 集計が展開前後で不変 —
tests/test_dummy.py がこの性質で検証）。UI 画面1の「ダミー一式から設計を始める」の実体
（docs/spec_change_dataname_measure.md 9節・プラン4）。

### `scorelib_param/dvtbudget.py` — dVtBudget変換
| 関数 | 内容 |
|---|---|
| `load_board_temperatures(path)` | initial_temperature.csv → {Board: 温度} |
| `apply_dvtbudget(lf, col, generation, coef, temps)` | Board の実測温度に最も近い温度キーの係数 b を State ごとに引き、`-log10(値)/b*1000` を行単位で適用。Board/State 列が既に潰されていたらエラー |

### `scorelib_param/custom.py` — 自作Python関数パーツ
| 定義 | 内容 |
|---|---|
| `CustomContext` | 関数に渡す入れ物（data_dir / generation / group_defs / params） |
| `default_custom_parts_path()` | リポジトリ直下の custom_parts.py（固定位置。configからパスは与えない=実験入力からの任意コード実行防止） |
| `load_custom_module(path)` | importlib でロード（=トップレベル実行。SVNレビュー済み前提） |
| `list_custom_functions(module)` | モジュール内で定義された公開関数名の一覧（import された名前・`_`始まりは除外） |
| `compute_custom_part(part, module, ctx)` | 関数を呼び、戻り値が有限な1スカラーであることを検証 |

### `scorelib_param/introspect.py` — UI向けメタデータの導出
| 関数 | 内容 |
|---|---|
| `detect_types(dir)` | 測定typeの検出（`parameterLabel_*`/`dataName_*` の命名 + Measure列を持つcsv） |
| `find_run_configs(dir)` | **中身の形**（optimization{}キー）で設定jsonc候補を全列挙 |
| `find_dvtbudget_coefs(dir)` | 中身の形（世代→温度→State→{a,b}）で係数jsonc候補を全列挙。設定jsoncとは形が排他的 |
| `find_generation_info(dir, generation)` | これだけファイル名ベース（`{Generation}.json`） |
| `axis_catalog(dir, type_)` | typeの軸一覧→値候補。dVtBudget は FBC のカタログ。**Measure 軸**（Measure 列を持つ type のみ・実在番号の昇順）と **DataName 軸**（dataName_{type}.csv がある場合）も出す |
| `measure_labels(dir, type_)` | Measure 番号 → dataName の対応（UI の複合表示「dataName (Measure N)」と labels 注記用。dataName_* が無ければ空 = 番号のみ表示） |
| `_candidates` | 値候補の導出。map系軸は**実データに存在する値だけ**（map順、失敗時は全語彙にフォールバック）、Override は [False, True]、数値軸は csv のユニーク値 |

複数候補の扱い（黙って選ばずエラー）は呼び出し側（ui/state.py）の責務。

### `scorelib_param/cli.py` — 計算の入口（最適化側からはサブプロセスで呼ばれる）
| 定義 | 内容 |
|---|---|
| `RELATIVE_STEP` / `DVTBUDGET_STEP` | order に置ける仮想ステップ名（`__relative__` / `__dvtbudget__`） |
| `_named_axes(part)` | パーツが言及する軸名の集合（ui/state.py の `_part_axis_names` と意図的に並行。対象がモデルか編集途中dictかの違い） |
| `_referenced_group_defs` / `_required_axes` | 派生軸名→元軸への読み替え。定義名=軸名の同名を拒否 |
| `_with_group_columns` | 読み込み直後にグループ派生列を生成。どの範囲にも入らない値の行は値一覧つきエラー。元軸がパーツに不要なら落とす（暗黙集約と同じ扱いに戻す） |
| `_effective_order(part)` | 明示されなかった `__relative__`（先頭）/`__dvtbudget__`（相対化直後）を補完 |
| `derive_axis_counts(data_dir, axes)` / `load_axis_counts(path)` / `resolve_group_defs(...)` | Physical 記法の軸総数 N の解決: `{Generation}.json` があればそこから（互換・優先）、**無ければ測定csvの max+1 から導出**（本数は世代で固定・フローは全数測定のため正確 — spec_change ノート9節）。どちらからも決まらなければ明確なエラー |
| `_hoistable_prefilters(part, group_defs)` | order 内の位置・`__relative__` の明示/暗黙によらず、可換な filter の行絞り [(軸,値),...] をパイプライン先頭に前出しする判定（リスト値=is_in も対象。キャッシュキーでは tuple 化）。除外は split軸・分母事前集計の軸とその `by`（派生軸は元軸と双方向対応）・複合軸の構成軸。列は落とさず行だけ先に絞る純最適化（結果不変、tests/test_prefilter.py）。診断上の変化: 後段の検証（dVtBudget係数カバレッジ等）は filter 後に残る値だけが対象になる |
| `SharedComputeContext` | 1回の compute_score_file 内でtype単位のcsv読み込みと `__relative__`/`__dvtbudget__` 直後の中間結果を共有するキャッシュ（結果は共有なしと同一。customパーツは対象外）。前絞りが異なるパーツは共有しない（キーに prefilters を含む） |
| `_apply_axis_step` | 複合軸なら列を `&` で融合してから aggregate に渡す |
| `compute_score_part(...)` | 1パーツの計算。type=custom は関数呼び出しへ分岐 |
| `compute_score_file(dir, run_config, ...)` | 全パーツ計算+expression 評価 → `{"Score": ..., パーツ名: ...}` |
| `main()` | argparse。`--config --data-dir --dvtbudget-coef --initial-temperature --custom-parts --version`。stdout は結果JSONのみ（版は stderr） |

---

## ui/（Streamlit UI）

### `ui/state.py` — 編集ロジック（streamlit 非依存の純関数。テストの主対象）
セクション順に:

**スコアファイル基本**
| 関数 | 内容 |
|---|---|
| `empty_score_file()` | 空の編集用 dict（ScoreFile の形） |
| `ensure_uids(sf)` | 各パーツにウィジェットキー用の内部ID `_uid` を付与。**重複IDは振り直す**（複製バグ期間の下書きも開くだけで修復） |
| `part_names` / `unique_part_name` | 名前一覧 / `part_N` 形式の未使用名 |

**雛形とパーツ操作**
| 関数 | 内容 |
|---|---|
| `default_axis_order(catalog)` | 雛形の軸順（Measure→Label→Override→カテゴリ→数値→Board/Chip/Block。InBatchEpoch と DataName は除外 — DataName は Measure の表示名の扱い） |
| `default_aggregation(axis, cands)` | カテゴリ/bool軸は先頭候補の filter、数値軸は mean。Measure は識別子軸なので filter（平均しない。候補が無い設定のみ編集では value 未入力の filter とし、入力まで検証エラーで促す） |
| `part_skeleton(name, type, catalog)` | **そのまま計算が通る**雛形（全軸+デフォルトop）。相対化プリセットは無し（旧 Read_Override 自動ONは廃止）。Measure 軸のある type では Label/Override 軸も除外（Measure が一意に決める測定メタデータで、Measure 分割のペア結合を壊すため — docs/spec_change_dataname_measure.md 4節） |
| `custom_part_skeleton(name, functions)` | custom パーツの雛形（先頭の関数+空params） |
| `switch_part_type(part, new_type)` | type変更時の不整合フィールド除去（custom⇔通常、dVtBudget離脱時の `__dvtbudget__` 除去） |
| `duplicate_part(sf, i)` | 深いコピー+新しい名前。**`_uid` は必ず新規**（共有するとウィジェット状態が2パーツで混線する） |
| `move_entry(lst, i, delta)` | リスト内の隣接入れ替え |

**相対化と order の整合**
| 関数 | 内容 |
|---|---|
| `default_split_axis(catalog)` | 相対化の既定 split 軸: Measure > Read_Override > 他の Override > 先頭の軸 |
| `_positional_sides(cands)` | split 軸の候補から分子/分母の初期値を位置で選ぶ（先頭=分母・2番目=分子。Override の [False, True] では旧既定と一致）。候補不足なら (None, None) = 入力まで検証エラー |
| `enable_relative` / `disable_relative` / `change_split_axis` | 相対化ON/OFF/split軸変更時に order との整合を自動で取る。OFF時は split軸をデフォルトopで order へ復帰+`__relative__` を除去（エンジンは order に無い軸を暗黙集約して混ぜるため、放置すると分子分母が混ざる）。split変更時は分子/分母を新軸の候補で初期化し直し、labels 注記も捨てる |
| `annotate_measure_labels(spec, mlabels)` | Measure 指定の相対化/filter に dataName の labels 注記を付与（名前の分かる番号のみ。無ければ注記ごと除去） |
| `drop_stale_virtual_steps(part)` | type≠dVtBudget で残った `__dvtbudget__` を除去 |

**グループ定義**
| 関数 | 内容 |
|---|---|
| `import_config_group_defs(sf, wlgroup)` | 設定jsoncの WLgroup を編集可能な定義として取り込み（既存があれば触らない） |
| `parts_referencing_group_def` / `add_group_def` / `delete_group_def` | 参照パーツ検出 / 名前衝突チェック付き作成 / 参照中は削除ガード |
| `axis_counts(geninfo)` | 世代情報json → {WL: numWLs, STR: numStrings} |
| `data_axis_counts(catalogs)` | カタログの数値軸から本数を導出（max+1。本数は世代で固定・フローは全数測定のため正確。Measure/InBatchEpoch は除外） |
| `validation_axis_counts(ctx)` | 本数チェックに使う軸本数: **データ由来が正**、自動検出された世代情報json はデータに無い軸の補完のみ（観測 > 宣言） |
| `geninfo_mismatch_warnings(ctx)` | 自動検出された世代情報json とデータ由来本数の食い違い警告（診断情報。検査はデータ由来で行う） |
| `group_def_warnings(sf, counts)` | 定義の範囲と本数の整合警告（範囲超過・未カバー値。`_format_value_runs` で 4–5 のような圧縮表示）。counts は通常 validation_axis_counts |

**選択セット**
`_part_specs`（パーツの全集計spec収集の共通ヘルパー）、`referencing_parts`、
`delete_selection_set`（参照中ガード）、`save_set_as`（別名コピー）。

**検証**
| 関数 | 内容 |
|---|---|
| `validate_score_file(data)` | エンジンの model_validate + 名前重複 + expression の参照チェック + 宙に浮いた constraint キー + ref 解決の再検証。**pydantic の位置表記をパーツ名に変換**（`_format_pydantic_error`） |
| `validate_part(part)` | 単一パーツ用の包み |

**コンテキスト（画面1）**
| 関数 | 内容 |
|---|---|
| `_resolve_optional_file(explicit, discover, label)` | 「明示指定優先・無ければ自動検出・**候補複数はエラー**」の共通ルール |
| `build_context(data_dir, config, coef, geninfo, custom)` | 画面1の読み込み本体。空パス拒否、type/カタログ導出（Measure→dataName 対応の `measure_labels` 含む）、同梱ファイル解決、custom関数一覧化。ctx dict を返す。世代情報json の UI 入力欄は廃止 — UI は geninfo に None を渡し、`{Generation}.json` がディレクトリ内にあれば自動検出して診断にだけ使う |
| `parse_chip_counts(text, n_boards)` | 「Board ごとの Chip 数」入力のパース（数1つ=全Board共通、カンマ区切り=Board別） |
| `load_config_only(text)` / `config_only_context(sf)` / `is_run_config_text(text)` | **設定だけ編集モード**: 設定 jsonc（score.jsonc / RunConfig 形式）だけから (score_file, context) を作る。カタログは設定が言及する軸名（値候補無し=自由入力）、measure_labels は labels 注記から回収（`_config_measure_labels`）。RunConfig なら旧 WLgroup も定義として取り込む。テスト計算以外の全機能がデータ無しで動く。is_run_config_text は①の設定を build_context の config として渡せる形式かの判定 |
| `expand_dummy_bundle(dir, chip_counts)` | ダミー一式を一時ディレクトリへ Board/Chip 展開（scorelib_param.dummy を呼ぶ。展開先は通常の build_context がそのまま読む） |
| `extract_bundle_zip(bytes)` | 一式zipを一時ディレクトリへ展開（zip-slip対策、単一トップフォルダ降下）。ダミー一式の zip アップロードにも使う |
| `save_upload(filename, data)` | アップロード1ファイルを一時ディレクトリへ保存してパスを返す（パス欄への自動反映用。ファイル名のパス成分は捨てる） |
| `locate_bundle_inputs(dir)` | 展開後ツリーを探索（深さ4）して測定ディレクトリ+同梱ファイルを特定。曖昧ならエラー |

**表示用ラベル（純関数にしてある理由: D&D部品の描画はAppTestから見えないため）**
| 関数 | 内容 |
|---|---|
| `part_summary_rows(sf)` | 一覧表の行（名前/type/相対化/軸。customは関数名表示） |
| `part_list_labels(sf, selected_uid, invalid)` | D&Dリストのラベル（⠿ / ⚠ / ← 編集中） |
| `part_select_labels(sf, invalid)` | 選択プルダウンのラベル。**番号付きで常に一意**（同名パーツがあると selectbox がラベル照合で誤動作するため） |

**下書き・エクスポート・テスト計算**
| 関数 | 内容 |
|---|---|
| `save_draft` / `load_draft` / `draft_path_for(user)` | 下書きは**ユーザ名ごと**に `DRAFTS_DIR`（`~/.scorelib_drafts/<名前>.jsonc`。名前はファイル名安全に正規化 — 共用サーバで混ざらないための分離）。score_file+画面1の入力（context_inputs）を保存。旧形式も読める |
| `export_part(sf, i)` | パーツ単体を自己完結jsoncに（参照する選択セット・グループ定義を同梱） |
| `score_file_to_jsonc` / `import_score_file` | 全体のエクスポート/インポート（RunConfig形式も受理。エラーはパーツ名付き） |
| `run_test_compute(sf, dir, ...)` | ScoreFile dict → RunConfig を組み立てて `compute_score_file` を直接呼ぶ |

### `ui/widgets.py` — 再利用ウィジェット
| 定義 | 内容 |
|---|---|
| `HAS_SORTABLES` / `sortable_list(items, key)` | streamlit-sortables のソフト依存。ラベル由来のキーで並び替え後に再マウント。失敗時 None（呼び出し側が↑↓ボタンにフォールバック） |
| `_SORTABLE_STYLE` | D&D項目のCSS（半透明グレー・左揃え。デフォルトの赤・中央揃えの上書き） |
| `parse_scalar(text)` | 自由入力 → bool/int/float/str |
| `measure_format(mlabels)` / `_axis_format` | Measure 値の複合表示「dataName (Measure N)」（名無しは「Measure N」）。選択・保存は常に番号 |
| `value_widget` / `dict_selection_row` / `selection_widget` / `selection_list_widget` | 値1個 / 複合軸1行 / どちらか自動 / 可変行リスト（単一軸+候補ありは multiselect）。Measure 軸は複合表示 |
| `agg_editor(entry, spec, catalog, set_names, key, ..., measure_labels)` | 集計指示エディタ。**opに応じた入力欄だけを出す**（value/values の混同がUI上起きない）。filter は候補のある単一軸で multiselect（複数=is_in、Measure 選択時は labels 注記も付与）。op変更時は古いフィールドを掃除。mean系の単一軸エントリには集計時重み欄（`_agg_weight_editor`: なし/重みセット/値ごと/定数。値ラベルは by_value_labels 優先=グループ派生軸対応） |
| `relative_editor(part, catalog, set_names, key, measure_labels)` | 相対化ブロックのエディタ。split軸は**任意の軸**から選択（旧 Override 限定は廃止）、分子/分母は候補プルダウン or 自由入力、Measure 分割時は labels 注記を自動付与。ON/OFF/split変更は state.py の整合関数を呼ぶ。分母事前集計は agg_editor をフル再利用 |

### `ui/app.py` — 5画面本体（ウィジェット配置と session_state だけ）
| 区分 | 内容 |
|---|---|
| `_RESERVED_STATE` / `_init` | アプリデータのキー宣言と初期化（それ以外はウィジェット状態とみなし undo 時に破棄） |
| `_snapshot` / `_track_history` / `_undo` | JSON文字列スナップショットによる undo（20件）。undo 時はウィジェット状態も破棄 |
| `_offer_draft_restore` / `_autosave` / `_sidebar_user` / `_header_user` | 下書きの復元提案と自動保存を**ユーザ名ごと**に行う（draft_path_for）。ユーザ名は認証ヘッダ（既定 X-Remote-User、SCORELIB_UI_USER_HEADER で変更）があればそれ、無ければサイドバーの名前入力。未入力の間は保存・復元とも停止 |
| `_merged_catalog` / `_catalog_for_part` / `_with_group_axes` | カタログの合成（パーツtype用+グループ派生軸の追加） |
| `screen_data` / `_handle_load` / `_DEV_MODE` | 画面1。**「① スコア設定（設定jsoncアップロード = 編集の出発点。両形式対応・編集状態を置き換え）+ ② データ（実測一式zip / ダミー一式+Board・Chip数 / なし の radio）」の2段 + 読み込みボタン1つ**。係数・custom の追加は「係数・自作関数を追加する」expander（パスモードではパス欄 — 実測・ダミー共通の2欄）。**一般ユーザの画面はアップロードのみ**で、パス指定は開発者モード（`-- --dev` / `SCORELIB_UI_DEV=1`）のトグルでのみ現れ、オン時は各アップローダがパス欄に置き換わる（併記しない）。優先順位: 個別アップロード > ①の設定（RunConfig 形式なら config_path としても使用 — `is_run_config_text`）> zip 内・パス指定。②なし+①あり = `load_config_only`（設定のみ編集）。認識結果（データ由来の軸本数と世代情報json の食い違い診断警告を含む）は共通で下部に表示 |
| `_order_entry_label` / `_order_editor` | orderの1行ラベル / 常時ドラッグ可能リスト+「編集するエントリ」プルダウン+常時表示エディタ（削除ボタン内蔵）。フォールバックは ✎/↑↓/✕ 行 |
| `_add_entry_controls` | 軸追加・複合軸束ね・`__offset__`・仮想ステップ配置 |
| `_custom_part_editor` | customパーツ用（関数プルダウン+params行エディタ） |
| `screen_parts` | 画面2。**パーツ選択は _uid をキー付き状態("part_sel")で保持**（run開始時に選択が確定=マーカー即時追従。追加/複製は part_sel_pending で次runに予約）。検証NGは ⚠ |
| `screen_sets` / `_selection_sets_section` / `_group_defs_section` | 画面3。選択セットとグループ定義の管理 |
| `screen_compose` | 画面4。expression（パーツ名クリック挿入）+ constraintThreshold 行エディタ |
| `screen_test_export` | 画面5。テスト計算（ダミー展開中は「数値は無意味」警告、設定のみ編集中はディレクトリ入力の案内・未入力は明確なエラー）/ score.jsonc・パーツ単体エクスポート / インポート |
| `main` | サイドバー（undo・検証件数・エンジン版）→ 復元プロンプト → 画面 → **変更検知したら即 rerun**（全画面共通。画面ごとの実装忘れを構造的に防ぐ）→ 履歴・自動保存 |

---

## scorelib_param/batch/（過去実験データのバッチスコア計算）

設計は `docs/batch_design.md`。複数の result_history（過去実験の epoch 群）を
受け取り、識別列 `Epoch` を通してバッチ単位に一括計算する。単一 epoch 計算と
数値等価（tests/test_batch.py で保証）。CLI: `python -m scorelib_param.batch`。

### `scorelib_param/batch/history.py` — result_history の列挙と Epoch ID
| 名前 | 内容 |
|---|---|
| `EpochRef` | 1 epoch への参照（label / epoch_no / source_dir）。`epoch_id` = `"{label}#{NNNN}"` |
| `derive_label(path)` | `<実験ログ>/Step{N}/Loop{NN}/result_history` から親3段でラベル導出（構造が違えば警告） |
| `enumerate_epochs(histories)` | パスのリスト or {ラベル: パス} → 全 EpochRef。ラベル重複・空 history はエラー、`result.NNNN` 以外は警告して無視 |

### `scorelib_param/batch/staging.py` — アーカイブ展開・検証・削除
| 名前 | 内容 |
|---|---|
| `StagedEpoch` | 計算可能になった 1 epoch（data_dir / created_dir=削除対象 / error=skip理由） |
| `stage_epoch(ref, staging_root)` | tar.gz/zip があればビューdir（展開+リンク）を作成。csv/csv.gz のみなら元dirをそのまま使う。例外は error に落とす |
| `validate_epoch(staged, types, needs_dvt)` | config が参照する type のファイル存在チェック（固定リストではなく config 駆動） |
| `cleanup_epoch(staged)` | 自分が作ったビューdirだけ削除（入力元は絶対に触らない） |

安全対策: アーカイブ内の絶対パス・`..` エントリは拒否。ディレクトリごと固めた
tar は展開後に1段持ち上げる（flatten）。symlink 不可の環境はコピーで代替。

### `scorelib_param/batch/compute.py` — バッチ計算層（純粋・polars）
| 名前 | 内容 |
|---|---|
| `EPOCH_COL` | 識別軸の予約名 `"Epoch"`。設計内の軸・グループ定義と衝突したらエラー |
| `BatchComputeContext` | SharedComputeContext のバッチ版: type ごとに全 epoch を resolve → `Epoch` 列付与 → lazy concat → streaming collect。prefix_cache は親のまま共有 |
| `compute_score_batch(epochs, config, coef)` | 1バッチ一括計算 → `BatchResult`。パーツごとに `compute_score_part(..., identity_axes=("Epoch",))`、custom は epoch ループ、expression は epoch ごとに評価 |
| `BatchResult` | scores（Epoch/History/EpochNo/Score/全パーツの DataFrame）+ failed（{Epoch: 理由}） |

エラー処理: バッチ一括計算が失敗したら epoch 逐次計算（compute_score_file）に
自動フォールバックして原因 epoch を特定・除外し、正常 epoch の値を救う。
filter 空振りで行ごと消えた epoch は「パーツごとに全 epoch が揃っているか」の
検証で捕まえる。

### `scorelib_param/batch/runner.py` — パイプライン実行
| 名前 | 内容 |
|---|---|
| `Fetcher` | `(EpochRef, staging_root) -> Path`。デフォルト `passthrough_fetcher`（ローカル/マウント済みをそのまま）。scp 等はこの実装を足すだけ |
| `BatchRunner` | バッチ分割 → 先行取得（最大 max_prefetch、ThreadPoolExecutor）→ 計算 → ステージング削除。`run()`（全結合）/ `run_iter()`（バッチごと） |
| `available_memory_bytes()` | /proc/meminfo（Linux、追加依存なし）→ psutil → None |
| `estimate_epoch_bytes()` / `_advise_batch_size()` | 最初の epoch を実測して batch_size auto / 過大・過小の助言（stderr。実行はブロックしない） |
| `StrictBatchError` | strict モードで不良 epoch を検出したときの例外 |

### `scorelib_param/batch/__main__.py` — CLI
`python -m scorelib_param.batch --config ... --history ... --out scores.csv`。
`--history` は繰り返し可・`label=path` 形式可。除外 epoch は stderr と
`<out>.failed.csv` に理由つきで出力。`--max-threads N` で計算スレッド数を
制限できる（POLARS_MAX_THREADS を polars の初回 import **前**に設定する
必要があるため、`batch/__init__.py` は PEP 562 の遅延インポートにし、
`__main__` は引数処理後に runner を import する構造。バッチサイズが
決めるのはメモリで、CPU はスレッド数で制御する）。

### エンジン本体への変更（すべて後方互換・オプショナル）
| 箇所 | 内容 |
|---|---|
| `axis_resolve.data_file()` | `{name}.csv` が無ければ `{name}.csv.gz` を解決（polars が scan_csv で直読みできるため解凍不要） |
| `dvtbudget.apply_dvtbudget(..., epoch_col=None)` | epoch_col 指定時は温度を `{epoch: {Board: 温度}}` で受け、係数 b を (Epoch, Board, State) で join（温度=係数が epoch で変わりうるため） |
| `cli.compute_score_part(..., identity_axes=())` | 識別列を潰さず残し、識別値ごとに1行の DataFrame を返す（空=従来どおり float） |

---

## ルート・その他

| ファイル | 内容 |
|---|---|
| `custom_parts.py` | 自作関数の登録テンプレート（SVN登録用。書き方の説明コメント入り） |
| `config_mini.jsonc` | UI動作確認用のサンプルスコア設定（tests/data/result_tmp_mini と組で使う） |
| `scripts/convert_dvtbudget_coef.py` | 現行の係数Pythonファイル（`dVtBudget_coef = {...}`）を ast で安全に読み jsonc へ変換 |
| `scripts/make_pseudo_dummy.py` | 正データ→疑似ダミー一式（Board/Chip を1つに削る。`scorelib_param.dummy.make_pseudo_dummy` の CLI 包み。担当者ダミー納品前の開発・検証用） |
| `scripts/benchmark_batch.py` | 実運用マシンでバッチサイズごとの所要時間・ピークメモリを実測する（計測1回=子プロセス1つ。`--batch-sizes auto,10,25,50` / `--max-threads` / `--repeat`） |
| `scripts/batch_bridge_example.py` | 最適化スクリプト（python3.7）からバッチCLIを subprocess 起動するブリッジ実装例。scorelib_param 非依存・py3.7互換で、そのまま現行スクリプトへコピーできる（stderr は `<out>.log` へ、失敗は log 末尾つき RuntimeError、`<out>.failed.csv` を failed dict として返す） |
| `scripts/get_score_bridge_example.py` | turbo.py の get_score() に差し込む**毎epochの通常スコア計算**ブリッジ実装例（score_function="gui_score" 分岐）。`python -m scorelib_param.cli` を subprocess 起動し stdout の JSON を dict で返す。py3.7互換・scorelib非依存。initial_temperature 省略時は data_dir 内を自動使用 |
| `tests/data/result_tmp_mini/` | テスト・動作確認用の小さな測定データ一式（git登録済み。実データ result_tmp/ は登録しない） |
| `tests/` | テスト（解説は `testing_guide.md`） |
| `reference_scripts/` | 現行スクリプトの参照用コピー（エンジン検証の正解データ生成に使用。expand_FBC_measure.py はテストが実行する） |
