# テストガイド（考え方と各テストファイルの解説）

テストを普段書かない人向けの入門と、このリポジトリの全テストの解説です。
コード側の説明は `code_reference.md` を参照。**テストを追加・変更したら本ファイルも追随更新すること。**

## 1. テストとは何か・なぜ書くか

テストとは「**この入力ならこの結果になるはず**」という期待を、実行可能なコードとして書いたものです。

```python
def test_filter_then_mean():
    lf = pl.LazyFrame({"State": ["A", "A", "B"], "value": [10, 20, 30]})
    ...
    assert 結果 == 15.0  # State=A の平均は 15 のはず
```

`pytest` コマンドがこれらを全部実行し、期待と違えば FAILED と表示します。効能は2つ:

1. **今の正しさの確認** — 「Budg計算が現行スクリプトと一致する」等を人手でなく機械が毎回確認する
2. **将来の変更の安全網** — 機能追加やリファクタリングで**別の場所が壊れたら即座に検知**できる。
   このリポジトリは145本のテストを毎回全部回しており、大規模な変更(group_reduce廃止等)を
   安心してやれたのはこの網のおかげ

## 2. テストの種類（層）

明確な国際規格があるわけではなく現場ごとに言葉の揺れがありますが、一般的な整理:

| 層 | 意味 | このリポジトリでの例 |
|---|---|---|
| **単体テスト**（ユニットテスト。同じ意味） | 関数・クラス**1つ**を、手で答えを計算できる小さな入力で直接検証 | `test_aggregate.py`: 4行のデータで mean/filter/diff の結果を照合 |
| **結合テスト**（統合テスト） | **複数モジュールを連結**した動作を検証 | `test_cli.py`: csv読み込み→相対化→集計の全連結の結果を、テスト内に独立に書いた素朴な計算と照合 |
| **E2E（エンドツーエンド）テスト** | ユーザ操作相当を**一気通貫**で | `test_ui_app.py`: 仮想的にUIを起動し、ボタンを押して画面と内部状態を確認 |

補助的な概念:

- **回帰テスト**: バグを直したとき「そのバグを再現する入力」をテストにして再発を防ぐもの。
  層の名前ではなく目的の名前（単体でも E2E でもありうる）。例: 複製バグの
  `test_duplicate_then_switch_keeps_parts_independent` は修正前のコードなら失敗する
- **フィクスチャ（fixture）**: テストが使う共通の準備物（テストデータのパス等）。
  pytest では `@pytest.fixture` 関数を定義すると、**テスト関数の引数に書くだけで注入**される。
  複数ファイルで共有するものは `tests/conftest.py` に置く（pytestが自動で読む特別なファイル名）

## 3. pytest の基本文法（このリポジトリで使っているもの）

- **発見規則**: `tests/` 以下の `test_*.py` の中の `test_*` 関数が自動でテストと認識される
- **assert**: 素の `assert 式` が失敗すると、pytest が両辺の値を表示してくれる
- **`pytest.approx(x)`**: 浮動小数の比較（`==` の誤差問題を吸収）
- **`pytest.raises(ValueError, match="...")`**: 「このエラーが出るはず」を検証（エラー系のテスト）
- **`monkeypatch`**: テスト中だけ属性を差し替える標準fixture（例: 下書きの保存先をテスト用
  一時フォルダに向ける、関数を呼び出し回数カウンタでラップする）
- **`tmp_path`**: テストごとに用意される一時フォルダ（標準fixture）
- **AppTest**（`streamlit.testing.v1`）: Streamlitアプリをブラウザなしで実行し、
  `at.button(key=...).click().run()` のようにウィジェット操作をエミュレートする公式仕組み

実行方法:

```bash
.venv\Scripts\python -m pytest -q              # 全部
.venv\Scripts\python -m pytest tests\test_cli.py -q      # 1ファイル
.venv\Scripts\python -m pytest -k "custom" -q  # 名前に custom を含むものだけ
```

## 4. このリポジトリのテスト方針（経験から得た教訓を含む）

1. **正解は独立に計算する** — エンジンの検証は「エンジン自身の別経路」ではなく、
   テスト内に素朴な eager polars で書いた**別実装**や、現行スクリプト(reference_scripts/)の出力と照合する。
   同じバグを共有しない相手と比べることに意味がある
2. **検証ロジックはテストでも二重実装しない** — UIの検証はエンジンの pydantic を使う設計なので、
   テストも「エンジンのエラーがUIにそのまま出るか」を見る
3. **本番と同じ形の入力を使う**（教訓） — 複製バグは「_uid が付く前のパーツ」でテストしていて
   見逃した。state のテストは実アプリと同じ経路（ensure_uids 済み等）でデータを作る
4. **文脈切り替えを試す**（教訓） — Streamlit のウィジェット状態バグは「Aを編集→Bへ切替→A確認」
   で初めて出る。内部dictだけでなく**切替後のウィジェット表示値**まで assert する
5. **AppTest から見えないものは純関数に切り出して単体テスト**（教訓） — D&D部品の描画内容
   （⠿/⚠/← 編集中）は AppTest で観測できないため、ラベル組み立てを `state.part_list_labels`
   等に切り出してそこを検証する
6. **「同じrun内で最新か」のようなタイミング性質はテストで観測できない** — 構造（キー付き
   ウィジェット状態、main共通の変更検知rerun）で保証し、テストは構造の前提（状態の一致）を見る

## 5. 各テストファイルの解説

### 共通準備: `tests/conftest.py`
| fixture | 内容 |
|---|---|
| `data_dir_mini` | `tests/data/result_tmp_mini/`（リポジトリ同梱・git登録済みの小さな測定データ）。2026-07-29 に実験の実フォーマット確定を反映: Measure 無し type（KLD / dVthSGWLD / PROGLOOP / PROGSTATUS）と tPROG（parameterLabel の Read_Label が全行空欄）を追加。全7 type（テスト内の定数 `ALL_MINI_TYPES` / `MINI_TYPES`） |
| `fixtures_dir` / `dvtbudget_coef_path` | `tests/fixtures/`（config.jsonc / dvtbudget_coef.jsonc / B9LS.json / custom_parts.py） |
| `expanded_mini_dir` | **現行の展開スクリプト(reference_scripts/)をそのまま実行**して FBC_expanded.csv を生成した一時コピー。エンジンの結果を「現行方式の正解」と照合するための基準データ |

**移行ガードの寿命（2026-07-28 監査時のメモ）**: 次の3件は「廃止機能の残骸」では
なく「旧設定を読んだときに黙って誤動作せず正しい案内を出す」という現役仕様のテスト。
**旧設定が現場（SVN・過去実験の config）から根絶されたと判断できた時点で、
互換コードごと削除してよい**: `test_group_reduce_removed_with_guidance`（旧
group_reduce op のエラー案内）、`test_enabled_true/false_...`（旧 relative.enabled）。
`test_legacy_spellings_normalized`（mean_subset / values 別名）は現行スクリプトが
まだ使う表記のため当分現役。

### エンジン単体テスト
| ファイル | 層 | 内容 |
|---|---|---|
| `test_jsonc.py` | 単体 | コメント除去が文字列内の `//` を壊さないこと、末尾カンマ、実際の config.jsonc が読めること |
| `test_expression.py` | 単体 | 式評価（関数、変数参照、未定義参照のエラー） |
| `test_aggregate.py` | 単体 | 各op（filter/mean/subset/diff/expr）を手計算できる数行のデータで照合。潰し残しエラー。廃止した group_reduce が移行案内つきエラーになること。グループ派生列が普通の軸として集計できること |
| `test_axis_resolve.py` | 単体 | 必要な軸だけの結合・map解決・Override の Boolean 化 |
| `test_relative.py` | 単体 | 分母事前集計と offset の効き方を手計算値と照合。diff モード。分子/分母未設定（None）の明示エラー。labels 注記のラウンドトリップ |
| `test_dvtbudget.py` | 単体 | 温度の最近傍選択（-28.2℃→"-30"等）と変換式の値 |
| `test_introspect.py` | 単体 | type検出（予約ファイル無視・**値列ルール**: ファイル名と同名の値列を持つcsvのみ。Measure列だけでは type にならない）、中身の形による設定/係数jsonc検出、軸カタログ（**実データに存在する値への絞り込み**、tRにStateが漏れない等、Measure/DataName 軸の追加と Measure 列無し type での非表示、Param 候補 [ROM, Opt]、**全行空欄の parameterLabel 列の非表示**、**map_DataName.csv の両綴り対応**（実出力=大文字D。CI の ubuntu で意味を持つ検証））、measure_labels（番号→dataName。dataName無しで空）、**Override 候補も実データ由来**（評価側の測定が無いデータでは [False] のみ — [False, True] ハードコード廃止の回帰。fixture は conftest の `data_dir_mini_no_override_true`、2026-08-01） |
| `test_dummy.py` | 単体 | ダミー一式の Board/Chip 複製展開（scorelib_param/dummy.py）。行数・番号付け・Boardごとに違うChip数・initial_temperature/map の扱い・複数Board元データの拒否。**「mean 集計は複製に対して不変」という性質**で「展開は行の複製だけ」を検証し、展開一式で実計算が通ることも確認 |
| `test_agg_weight.py` | 単体 | 集計時重み（`weight`/`weight_ref`）: 「軸を潰す直前に値ごとの重みを乗算」が変換ステップ（by+mul）を直前に置いた場合と一致すること、weightSets からの解決、形状検査 |
| `test_transform_weights.py` | 単体 | 変換ステップの拡張（add/sub/mul/div・複数回・グループ別重み）と**単項op abs/log**（0.6.0: KLD 標準計算の polars 手組みとの一致、floor 必須等の検証）、Physical 記法グループ定義（definedInLogical / WLgroupDefinLogical の読み替え）。`{Generation}.json` 無しでの**データ由来の軸総数導出**が json ありと同値なこと、導出不能軸の明確なエラー |
| `test_config_vocab_diff.py` | 単体 | 実機診断スクリプト（config ファイル vs ローダ加工後 dict のエンジン語彙突き合わせ）: 「メモリのみ」（自動補完の疑い）/「加工あり」/「一致」/「ローダが除去」の判定、jsonc コメント除去、optimization の中身だけを渡された形の自動判別 |
| `test_bridge_example.py` | 単体 | ブリッジ見本の config 正規化（_jsonable）: ローダ加工済み config（pandas Series / numpy 型相当）が json 化でき、WLgroupWeight が手書きと同じ形に復元されて RunConfig で読めること。実装が振る舞い判定なのでフェイクで検証（pandas を test 依存にしない） |
| `test_wlgroup_legacy.py` | 単体 | WLgroup 定義の在り処の一本化（0.7.0）: エクスポートが WL 軸の WLgroup / WLgroupWeight を旧形式キーだけに書くこと（groupDefs に残らない・WL 以外の定義は残る）、エクスポート→インポートのラウンドトリップ、ScoreFile の旧形式キー吸収（groupDefs 優先・不正な文字列 bool の拒否）、to_score_file の統合、"WLgroup" 予約名ガード |
| `test_vthskip.py` | 単体〜結合 | vthSkip のダミー計算（0.6.0）: ダミー値=「変換後の値」の意味論（KLD 0 → 0.0、dVthSGWLD 1 → 残す8要素で 8.0）、relative パーツの拒否、compute_score_file のファイル不在分岐（ファイルがあれば vthSkip は無視）、batch のダミー埋め+dummy_used 報告・ダミー値なしでの failed、BatchRunner の事前検証免除 |

### エンジン結合テスト
| ファイル | 内容 |
|---|---|
| `test_combined_axis.py` | 複合軸（`State&Read_Label`）の filter/diff/sum が、軸を個別に扱った等価な計算と一致 |
| `test_selection_sets.py` | ref 参照の解決がインライン値と同じ結果・同じ検証エラーになる |
| `test_pipeline_steps.py` | 仮想ステップの配置換え（`__offset__`→相対化、`__relative__` を後ろに置く等）が数学的に等価な別表現と一致 |
| `test_shared_context.py` | 共有キャッシュあり/なしで**結果が完全一致**すること、csv読み込みが type ごとに1回だけになること、State 違いのパーツ間で相対化計算が再利用されること |
| `test_prefilter.py` | filter 前絞り最適化（`_hoistable_prefilters`）: 前出し対象の判定（split軸・分母事前集計軸とその派生軸・複合軸の除外）、**前絞りあり/なしの同値性**（相対化・dVtBudget・明示 `__relative__` 込み）、prefix_cache の混線防止、filter で絞った State 分だけの係数で計算できる診断上の変化 |
| `test_measure_split.py` | **新仕様（Measure 番号基準）の本丸**: Measure 1/0 分割の相対化が旧仕様（Read_Label filter + Read_Override 分割）と厳密同値、DataName 分割とも同値、labels 注記が計算に影響しないこと、Measure filter（単一・is_in）、is_in の前絞り同値性とキャッシュキー安全性。Read_Override 分割と Measure 軸が併用不可な理由（ペアキー衝突）もコメントで記録 |
| `test_cli.py` | 本丸。fixtures の config を実データで計算し、**テスト内に独立に書いた素朴な再計算と一致**することを照合（FBCパーツ）。遅延グループ集計のユーザシナリオ。範囲外WL値のエラー。custom パーツの計算・エラー・混在拒否（関数不在は TypeError — 2026-07-31 の品質向上パスで ValueError から変更）。**パーツ計算エラーの名指しと原因診断**（2026-08-01: compute_score_file が「score part '名前': 」を前置し、null/NaN の最終エラーは原因ステップ — filter 空振り / **相対化の評価側が無いデータ** / **係数・温度の部分欠け**（以前はエラーなしで値がズレていた）/ 係数表に無い Generation — を名指しすることをそれぞれ検証。**複数パーツの失敗は全件を1回の例外に列挙**し、正常パーツが混ざっていても部分結果を返さないこと）。**サブプロセスとして CLI を起動する完全E2E**（最適化側から呼ばれる形そのもの） |
| `test_batch.py` | バッチ計算（scorelib_param.batch）。**最重要は等価性**: 5 epoch（2実験、値・dVtBudget温度を全 epoch で変えた摂動データ）のバッチ一括計算が epoch ごとの `compute_score_file` と全パーツ一致 — epoch 混線（相対化ペア・集計・係数選択のまたがり）はどんな形でも不一致として現れる構成。ほか: Step/Loop ラベル導出、列挙（重複ラベル・空 history・junk 無視）、csv.gz 直読み、tar.gz 展開（フラット/ネスト flatten・ビュー削除・入力元無傷・不正パス拒否）、skip-and-report / strict / filter 空振り epoch の帰属 / 全滅エラー / 予約名 `Epoch` 衝突、batch-size advisory、CLI E2E、`__all__`(静的リスト)と遅延 import 辞書 `_EXPORTS` の整合（型チェッカー対応で静的化したため追加漏れをここで検出 — 2026-07-31） |

### UIテスト
| ファイル | 層 | 内容 |
|---|---|---|
| `test_ui_state.py` | 単体〜結合 | ui/state.py の全ロジック。雛形が**そのまま計算に通る**こと（エンジンで実計算。相対化プリセット無し・Measure filter 先頭・Label/Override 除外。**KLD/dVthSGWLD は標準計算入りの type 別雛形** — log/abs ステップ・SG系除外の選択つき sum・SGWLD 無しデータでの汎用フォールバック）、相対化ON/OFFの order 整合と既定 split（Measure > Param > Override > 先頭軸・分子/分母の位置初期化・相対化ONのままでも実計算が通ること・**PROGLOOP の Param 分割（分母 ROM/分子 Opt）で実計算が通ること**）、labels 注記の付与/除去、parse_chip_counts とダミー展開→build_context の通し、グループ定義（取り込み/削除ガード/本数警告 — 本数は**データ由来**が正で世代情報jsonは補完・食い違いは診断警告/エクスポート同梱）、検証エラーのパーツ名表示、build_context（空パス拒否・各ファイルの指定/自動検出/**候補複数エラー**）、データに無い type のパーツ検出（part_types_without_data — 設定のみ編集では警告しない）、**データに無い値の検出**（part_value_mismatches: filter/相対化の値が候補に無いパーツを読み込み直後から検出・ref は対象外 — 2026-08-01）、**「設定の誤り」への一本化**（config_problem_messages: 構造の誤り+データに無い値+データ無し type を1つのリストに集約、パーツ単位はパーツ名前置 — サイドバーとテスト実行前ガードの共通実体）、一式zip（フラット/ネスト/曖昧エラー）、custom（型切替の整合・実計算）、**設定のみ編集**（load_config_only: RunConfig/score.jsonc 両形式・旧WLgroup取り込み・軸名カタログ導出・データ無し検証/エクスポート・Measure雛形の入力促し）、**アップロード経路**（save_upload の保存とファイル名サニタイズ・ダミー一式 zip → 展開 → 読み込みの通し）、下書きの新旧形式、ラベル純関数（⚠/編集中/同名でも一意） |
| `test_ui_app.py` | E2E | AppTest でUIを実際に動かす。起動、エラー表示、パーツ作成→計算の一気通貫、**ダミー展開→雛形→相対化ON（Measure 分割+labels 注記）→テスト計算の一気通貫**、式挿入ボタンの即時反映、並べ替え、undo、**下書き保存→別セッションで復元**（ユーザ名ごとに分離: 未入力では保存されない・別名では提案されない・同名で入力欄まで復元）、config読み込みでの WLgroup 取り込み、本数警告の表示、custom パーツの作成→関数選択→計算、**設定のみ編集**（context 注入で画面2が開く・テスト計算はディレクトリ未入力エラー。file_uploader は AppTest 不可のため読み込みは state 層でカバーし、画面1の操作は開発者モード（SCORELIB_UI_DEV=1 を fixture で設定）のパス指定で行う。**一般ユーザ起動でパス指定トグルが存在しない**ことも検証）、**複製→切替のウィジェット独立性**（回帰）、`__relative__` 残留の回帰、**データに無い type のパーツで警告表示+パーツ維持**（別実験 config の読み込み時）。**2026-07-31 の品質向上パス前に19本増強**（UI 分割の安全網として配線を固定）: 集計エントリの追加/束ね/op変更/削除、変換ステップ（定数・軸の値ごと）、集計時重み（定数/値ごと/重みセット ref = 画面またぎ）、相対化の split 軸変更・分母事前集計の UI 操作、order ステップ順の↑↓✕✎（HAS_SORTABLES=False のフォールバック側）、グループ定義・選択セットの UI 作成/編集/参照ガード付き削除、設定のみ編集・パスエラー・既存スコア設定ボタン、custom params 行エディタ、画面5の実行ガードとダウンロード表示ゲート、**データ不一致パーツの三重固定**（filter の値がデータに無いとき: 選択前から一覧に「⚠ データ不一致」・エディタは値を保持して警告・テスト計算エラーがパーツ名を名指し — ユーザー報告シナリオ、2026-08-01）、**不変条件「描画は設定を変えない」**（`test_render_never_mutates_score_file`: 候補に無い値・存在しない参照を詰めた「全部盛り」設定で全画面を開き全パーツ・全エントリを選択しても score_file が1バイトも変わらない — 実機報告「開いただけで相対化の分子が化ける」の一般化。**運用ルール: エディタや設定の形を増やしたらこのテストの全部盛り設定にも足す**）、**改名の選択欄追随**（`test_part_rename_updates_selector`: ラベル変更でキーごと再マウント・新ラベル・選択保持 — streamlit#11268 対策。テストからは `_part_selector` ヘルパーで前方一致検索）、**「元に戻す」の表示・場所・データ一致**（`test_undo_restores_display_and_location` / `test_undo_restores_relative_selectbox_display`: undo 世代キーで全エディタが再マウントされ、入力欄・プルダウンの表示 = score_file = エクスポート jsonc が一致し、編集していた画面・パーツへ跳ぶ。世代1以降のキーはテスト側 `_ek` ヘルパーで組み立てる — 2026-08-01） |

### AppTest の限界（知っておくべきこと）
- **D&D部品（streamlit-sortables）は描画・操作できない** → ドラッグ操作は手動確認が必要。
  ラベル内容は純関数テストでカバー（方針5）
- **file_uploader へのファイル投入も不可** → zip読み込みは state 層のテストでカバー
- selectbox の**ラベル照合まわりのフロントエンド挙動は再現されない**（値を直接セットするため）
  → 同名パーツ問題はラベル一意性の単体テストでカバー
