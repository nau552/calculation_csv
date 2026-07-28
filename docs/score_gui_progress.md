# score_gui Phase1 進捗まとめ（引き継ぎ用）

最終更新: 2026-07-28（別タスク・別セッションからの再開用。まずこのファイルを読む。
§2〜8 は 2026-07-20 時点のスナップショット。以降の変更は §9 の追記と
docs/spec_change_dataname_measure.md を参照）

## 1. プロジェクトの目的

NAND フラッシュ実験のベイズ最適化パラメータチューニングにおいて、
最適化スコアを Python 手書きではなく **GUI で一般ユーザーが設計できる** ようにする。

- エンジン部（`scorelib_param/`）: 完成。jsonc のスコア定義を polars で計算。
  現行スクリプトとの数値一致をテストで担保済み。
- UI 部（`ui/`）: 完成。Streamlit 製 5 画面。ユーザーのブラウザ確認と
  フィードバック → 修正を十数ラウンド実施済み。
- ドキュメントは `docs/` に集約（`docs/README.md` が対象読者つき索引）。
  **設計判断は設計書（score_gui_design.md / score_gui_ui_design.md）が正**。
  コードの全関数解説は `docs/code_reference.md`、テスト解説は `docs/testing_guide.md`
  （どちらも**コード変更時に追随更新する**運用）。

## 2. 現在の状態

- 全 **145 テストがパス**（`.venv/Scripts/python -m pytest`、約10秒）。
- git: main ブランチ、**コミット済み**（直近 1613ba3「ファイルの場所を整理。
  コメントやDocStringを日本語に変更。」。§5 の全ラウンドがコミットに含まれる。
  未コミットは本ファイルの最終更新のみ）。
- コメント・docstring は全コード日本語化済み（エンジンのエラーメッセージのみ英語）。

## 3. ファイル構成と役割

```
scorelib_param/              # 計算エンジン（Streamlit 非依存。SVNへはここ+custom_parts.pyのみ同期）
  __init__.py          # __version__（SVN同期のたびに上げる。UI/CLIに表示）
  models.py            # 全データモデルと検証（UIも同じモデルで検証=二重実装しない）
  cli.py               # 計算の入口（最適化側からサブプロセス起動。type=custom分岐あり）
  aggregate.py axis_resolve.py relative.py dvtbudget.py expression.py
  jsonc.py io_jsonc.py
  custom.py            # 自作Python関数パーツ（custom_parts.py のロード・実行・検証）
  introspect.py        # UI用メタデータ導出（type検出・軸カタログ・中身の形でのファイル判別）
ui/                    # Streamlit UI（scorelib_param の薄いラッパ。gitのみ、SVNに入れない）
  state.py             # 純ロジック層。判断ロジックは全部ここ（pytest 可能）
  widgets.py           # 再利用ウィジェット（agg_editor / relative_editor / sortable_list 等）
  app.py               # 5 画面本体 + undo/自動保存/下書き復元
custom_parts.py        # 自作関数の登録テンプレート（SVN登録用。説明コメント入り）
tests/                 # フラット配置、モジュール名対応
  data/result_tmp_mini/  # テスト用最小データ（git登録済み）
  fixtures/            # config.jsonc / dvtbudget_coef.jsonc / B9LS.json / custom_parts.py
reference_scripts/     # 現行スクリプトの参照コピー（正解データ生成にテストが実行。旧 gomi/）
docs/                  # 全ドキュメント（README.md が索引）
```

### 5 画面（`ui/app.py`）
1. **データ読み込み** — 入力: 測定結果ディレクトリ（必須）+ 任意4つ
   （optimization設定jsonc / dVtBudget係数jsonc / 世代情報json / custom_parts.py）
   + **一式zipアップロード**（サブディレクトリ探索つき。展開→自動検出→入力欄へ自動記入）。
   自動検出は**中身の形**で判別し、同役割の候補が複数あればエラー（黙って選ばない）。
   世代情報があればグループ定義と WL/STR 本数の整合を警告チェック。
2. **スコアパーツ編集** — 雛形自動生成（**そのまま計算可能**が保証）、相対化エディタ、
   order エディタ（常時ドラッグ可能リスト+「編集するエントリ」プルダウン+常時表示編集欄）、
   パーツ一覧も常時ドラッグ可能（⠿/⚠/← 編集中 マーカー）。type=custom は関数+params エディタ。
3. **選択セット・グループ定義** — 参照パーツ表示・削除ガード・別名保存。
   グループ定義（派生軸）の作成・範囲行編集・本数警告。
4. **スコア合成・制約** — expression 入力（パーツ名ボタンで即時挿入）、制約行エディタ、動的制約(percentile)。
5. **テスト実行・エクスポート** — テスト計算、score.jsonc / パーツ単体（参照セット・
   グループ定義同梱）エクスポート、インポート。custom 使用時は注記表示。

### 横断機能
- **自動保存**: 確定 run 末尾ごとに `~/.scorelib_draft.jsonc` へ。下書きは
  `{"score_file":…, "context_inputs":{画面1の5入力}}` 形式。復元でデータ読み込み・
  入力欄まで戻る（旧形式も読める）。
- **undo**: スナップショット履歴 20 件。復元時に非予約 widget state を全消し。
- **即時表示更新**: main 共通の変更検知 → 差分があれば st.rerun()（全画面）。
- **エンジン版表示**: サイドバーに `scorelib_param.__version__`（SVN側との版ズレ確認用）。

## 4. 重要な技術知見（Streamlit のハマりどころ）

- widget は key で自分の状態を記憶し `value=` 引数を上書きする
  → expression 挿入ボタンは `st.session_state["expr_input"]` も直接書く（widget 描画前なら安全）。
- expander の同一性はラベル文字列由来 → ラベルに op/値を入れると編集のたびに閉じる
  → エントリごとの expander は廃止（事前集計だけ固定ラベル expander）。
- 描画されなかった widget の state は GC される。
- エンジンは order に無い軸を**暗黙集約**する → relative OFF 時に split_axis が宙に浮くと
  True/False 行が混ざる → `disable_relative` が自動で order へ復帰 + `{"op":"filter","value":False}` 付与。
- 軸の値候補は **データに実在する値**に絞る（map の語彙全体だと雛形 filter が 0 行になる）。
  map 順維持、絞り込み結果が空なら全語彙フォールバック（`scorelib_param/introspect.py` `_candidates`）。
- D&D: `streamlit-sortables` 0.3.1 をソフト依存（`pyproject.toml` の `ui` extra）。
  `ui/widgets.py` `sortable_list` は key に `hash(tuple(items))` を混ぜてラベル変更時に強制リマウント、
  失敗時は None → ✎/↑↓/✕ 行リストへ自動フォールバック。
  「三本線ハンドル付きインラインリスト」は自前カスタムコンポーネントが必要なため Phase1 見送り（設計書 8-3 に記載）。

## 5. 本セッションで実施した修正ラウンド（時系列）

1. UI 初期実装（5 画面 + テスト）→ コミット済み。
2. データ読み込み修正: result_tmp の中身の前提を訂正（3 入力化、空パス拒否、絶対パス表示）。
3. 8 項目フィードバック対応: 自動保存タイミング説明 / undo 追加 / 分母事前集計のフルエディタ化 /
   事前集計軸と order の関係説明 / relative OFF 時の軸自動復帰 / op 変更の即時表示反映 /
   expander 閉じ問題の解消 / 変換ステップ名の簡素化（`__offset__` 自動命名）。
4. expression 即時反映修正 + パーツ並べ替え追加（折衷案: sortables ソフト依存 + ボタンフォールバック）。
5. 下書き復元がデータ読み込みまで復元するよう修正（draft v2 形式）。
6. D&D 案A 採用: モード切替廃止、常時ドラッグ可能リスト + 選択プルダウン + 編集欄内削除ボタン。
7. 復元時に画面1の入力欄3つ（データディレクトリ/設定jsonc/係数jsonc）へパスを書き戻し
   （空欄のまま再読み込みすると指定が外れる罠の解消）+ D&D見た目改善:
   赤をやめテーマ追従グレー（custom_style + CSS変数）、各行に ⠿ ハンドル記号、
   選択行に「← 編集中」マーカー。折りたたみ案は「並べ替えは頻繁な操作で、
   リストはパイプラインの見取り図を兼ねる」ためユーザーと合意の上で却下。
   README / 設計書も更新済み。
8. **グループ定義の一般化（大型変更）**: `group_reduce` op 廃止 → **派生軸方式**へ一本化。
   - エンジン: `GroupDef`(axis+groups) を ScoreFile/OptimizationConfig の `groupDefs` に追加。
     パーツが定義名を参照すると読み込み直後にグループ列を生成し、以降は普通の軸として
     order の任意の位置で集計可能（例: WL平均→Board max→WLgroup max）。
     旧 `optimization.WLgroup` は WL への定義として互換読み込み（groupDefs 優先）。
     旧 group_reduce は移行案内つきエラー。定義名=対象軸名は禁止。
     注意: グループ列は最初から存在するため relative の分母事前集計もグループ内で閉じる
     （またぎたい場合は事前集計にグループ軸を足す）— test_cli の期待値もこの意味論。
   - UI: 画面3を「選択セット・グループ定義」に拡張（定義の作成/範囲行編集/削除ガード/
     参照パーツ表示）。設定jsonc の WLgroup は読み込み時に編集可能な定義として自動取り込み。
     軸候補・値候補(グループ名)に定義が並ぶ。エクスポート/パーツ単体エクスポートに同梱。
     STR 等 WL 以外の軸のグループ化も可能（ユーザー要望）。
   - D&D 見た目: 項目背景を半透明グレーにして枠と区別、text-align 左揃えで ⠿ を整列。
   - 復元時に画面1の入力欄3つへパスを書き戻し（空欄のまま再読み込みで指定が外れる罠の解消）。
   - config_mini.jsonc / tests/fixtures/config.jsonc も派生軸方式へ移行済み。
9. **世代情報json対応+エラー表示改善**:
   - 画面1に4つ目の任意入力「世代情報json」（tests/fixtures/B9LS.json が例。
     numWLs/numStrings/jointLogicalWLs/numTiers/ROP。世代ごとに存在する実運用ファイル）。
     自動検出は `{Generation}.json`。下書き・復元にも対応。
   - グループ定義の本数整合警告（WL=numWLs, STR=numStrings。範囲超過/未カバー。
     画面1読み込み直後+画面3エディタ。jointLogicalWLs の除外運用は無いと確認済み→全値チェック）。
   - エンジン: どの範囲にも入らない実データ値は値一覧つきエラー（null グループ事故防止）。
   - 検証エラーの `score_parts.12.…` を**パーツ名表示**に変換（validate/インポート/設定jsonc読込）。
   - パーツ一覧（D&D・プルダウン・フォールバック表）に検証NGの ⚠ マーク
     （D&D部品は項目単位の色分け不可のため記号で代替）。
10. **複製バグ修正+テスト方針の反省**:
   - 複製が _uid ごとコピー→2パーツがウィジェット状態を共有し、名前・相対化が
     相互に書き換わる実バグ。修正: 複製時に新IDを割当 + ensure_uids が重複IDを自動修復。
   - 相対化オフで order の `__relative__` が残り検証エラー→削除するよう修正
     （type変更時の `__dvtbudget__` も同様）。
   - 変更検知→即時rerun を画面ごとのコピペから **main 共通**へ移動
     （画面3のグループ定義警告が1操作遅れた原因の構造的解消）。
   - **テスト反省**（ユーザー指摘）: (1)stateテストが本番と違う形（_uidなし）の入力を
     使っていた (2)「A編集→B切替→A確認」の文脈切り替えテストがゼロだった
     （ウィジェット状態バグ族が最大リスクなのに）(3)仮想ステップ×相対化オフ等の
     機能ペア未検証 (4)即時反映はテストでなく仕組みで守るべき。
     → 対応するテストを追加（複製→切替の独立性AppTest、__relative__残留、ID修復等）。
     今後のUIテストは「内部dictの確認」だけでなく**切替後のウィジェット表示値**まで見ること。
11. **パーツ選択のキー付き化**: 「← 編集中」マーカーが1操作遅れる問題。パーツ選択を
   _uid ベースのキー付き selectbox（"part_sel"）に変更し、run開始時点で選択が確定する
   構造に（order側と同方式）。追加・複製は part_sel_pending で次runに選択を予約。
   並べ替えの選択追従は自動化。ラベル組み立ては state.part_list_labels に切り出して
   単体テスト化（D&D部品の描画はAppTestから見えない、というテスト盲点への代償措置）。
12. **名前重複時の選択不能バグ**: selectbox フロントエンドは表示ラベルで照合するため、
   同名パーツがあると選択が誤解決/無反応になる。プルダウンのラベルを番号付き
   （state.part_select_labels、⠿一覧と同じ番号）で常に一意にして解消。
   AppTest では再現不能（値直接セット）のため、ラベル一意性を単体テストで保証。
13. **type=custom（自作Python関数パーツ）+ 一式zip読み込み**:
   - 運用整理（ユーザーと合意）: 現行GUIサーバがSVNリビジョンをチェックアウトして
     実験実行→スコア計算もそこで走る。よって custom_parts.py は**リポジトリ直下の
     固定位置（SVN管理）**とし、config にパスは持たせない（実験入力からの任意コード
     実行を防ぐ。編集はSVNコミット=レビュー経由）。configは関数名+paramsのみ。
   - エンジン: scorelib_param/custom.py（load/list/戻り値検証）、ScorePart.function/params
     （customのみ許可・混在拒否）、cli分岐+--custom-parts、共有キャッシュはcustom除外。
     リポジトリ直下に説明入りテンプレート custom_parts.py を配置。
   - UI: 画面1に custom_parts.py 入力+一式zipアップロード（展開→既存自動検出。
     zip-slip対策+単一トップフォルダ降下）、type=custom エディタ（関数プルダウン+
     params行）、type切替時の不整合フィールド自動除去（state.switch_part_type）、
     エクスポート時の「custom_parts.py が実行側に必要」注記。
   - 前提ファイルが無いtype（custom/dVtBudget）の既存パーツを開いても type が
     勝手に書き換わらないよう選択肢に現typeを保持（潜在バグ修正）。
14. **zipのサブディレクトリ探索+自動検出の複数候補エラー化**:
   - zip内で result_tmp がフォルダのままでも読めるよう、展開後ツリーを探索（深さ4）して
     測定ディレクトリ（type検出できるディレクトリ）と同梱ファイルを特定、入力欄へ自動記入。
   - 判別ルールを整理してドキュメント化（ui設計書 画面1節+README）: 設定jsonc/係数jsonc は
     **中身の形**で判別（排他的）、世代情報のみ `{Generation}.json` のファイル名、custom は固定名。
   - 同じ役割の候補が複数 → zip・通常ディレクトリ検出とも**候補一覧つきエラー**
     （従来はアルファベット先頭を無言採用。明示パス指定で解決可能）。
15. **配置・デプロイ方針の確定+バージョン表示**（ユーザーと合意）:
   - 一般ユーザはSVNに触れない（GUI経由のみ）・UIはSVNに入れない（gitで管理、現行GUIと同様）。
   - 方針: コードの正はgit（UI+エンジン一体）。SVNへは scorelib_param/+custom_parts.py のみ
     リリース時に一方向同期。一般ユーザ向けは「サーバでUI共用+一式zipアップロード」が正式形
     （ユーザ自身のstreamlit構築は開発者向けモードに格下げ）。UI/エンジン分割は却下
     （検証の二重実装になる。共通コア3分割案も版ズレを解決しないため見送り）。
   - `scorelib_param.__version__` 新設（SVN同期時に更新する運用）。UIサイドバー+CLI
     （stderr / --version）に表示。stdoutの結果JSONは汚さない。
   - ui設計書2.1節「配置・起動形態」+README に明記。
16. **リファクタリング+コード/テスト解説ドキュメント**（ユーザーと合意の上で実施）:
   - 削除: app.py の未使用変数 `base`、introspect.py の本番未使用3関数
     （available_part_types / 単数形 find_run_config / find_dvtbudget_coef —
     「複数候補で先頭を無言採用」の古い動作を残していたため危険だった）。
   - 統一: `_part_specs` ヘルパー（state.py の2箇所コピペ解消）、`MULTI_OPS` を
     models 公開にして widgets と共有、part_summary_rows の二重計算解消、
     custom パーツ時の不要なカタログ構築を分岐内へ。
   - 意図的な並行実装（cli._named_axes ⇔ state._part_axis_names、行エディタ2種）には
     「統合するな」の相互参照コメントを明文化。state.py 分割は見送り（利益<コスト）。
   - 新規ドキュメント: **code_reference.md**（全ファイル・全関数の解説。チーム報告用）と
     **testing_guide.md**（テスト概念入門+全テストファイル解説+AppTestの限界）。
     どちらも「コード変更時に追随更新」を冒頭に明記。
17. **リポジトリ整理+日本語化**（ユーザーと合意の上で実施）:
   - 構成変更: gomi/ → reference_scripts/、result_tmp_mini/ → tests/data/result_tmp_mini/
     （**git登録**。従来 .gitignore の result_tmp* に巻き込まれて未追跡=クローンでテスト全滅だった）、
     設計書類 → docs/（docs/README.md に対象読者つき索引）。.gitignore は実データ
     （result_tmp/・ルートの csv・reference_scripts/*.csv）だけを除外する形に整理。
   - テストの実データ依存を解消（test_axis_resolve/test_dvtbudget が result_tmp を
     参照していた → mini とその場生成の csv に変更。data_dir fixture 削除）。
   - **全コードのコメント・docstring を日本語化**（scorelib_param 全部・ui 全部・scripts・
     conftest・全テストの説明文）。エンジンの**エラーメッセージは英語のまま**
     （pydantic 組み込みメッセージと混ざるため+テストが文字列照合しているため。対象外と合意）。
   - パスの追随: conftest（reference_scripts 実行、tests/data 参照）、README、docs 内相互参照。
   - 日本語化の過程で scripts/convert_dvtbudget_coef.py の潜在バグ（変数取り違えで
     必ず NameError）を発見・修正、実ファイルで動作確認。
   → **ここまで完了**。本セッション（2026-07-19〜20）はここで終了。

## 6. 未解決・保留事項（次にやること候補）

**すぐやるもの**
- **ブラウザでの最終確認**: 直近ラウンド（custom パーツ、一式zip、グループ定義警告、
  ⚠マーク、D&D配色）の操作感をユーザーが未確認。`streamlit run ui/app.py`。

**運用整備（合意済み・未実施）**
- SVN への初回同期（scorelib_param/ + custom_parts.py。手順は ui設計書 2.1節の4ステップ。
  同期時に `__version__` を上げる）。
- 現行GUI側への依頼: 「スコア設計用一式を zip でダウンロード」ボタンの実装
  （result_tmp + 設定jsonc + 係数jsonc + {Generation}.json + custom_parts.py を同梱）。
- サーバでの UI 共用ホスティング（最終形。当面は開発者のローカル起動で可）。

**仕様の未確定（担当者・現行スクリプト側待ち）**
- ~~split_axis の型ごとのデフォルト（読み込み系=Read_Override / 書き込み系=Program_Override
  の確定。担当者確認中）。~~ → **解消（2026-07-28）**: Override 判定自体が使えないと
  判明し Measure 番号基準へ仕様変更・実装済み（9節と
  docs/spec_change_dataname_measure.md）。
- denominator_offset の運用ルール（値の決め方）未確定。
- python3.7 最適化側の `get_score()` ブリッジ（score_function="gui_score" で
  scorelib_param.cli をサブプロセス起動する分岐。現行スクリプト側の整備後に実装）。
- Phase2: 測定前に将来出力を記述するマニフェスト形式（introspect のデータソース
  差し替えで対応する設計にしてある）。
- result_tmp データ不整合（Chip 0-7 vs parameterLabel 0-1）はユーザーへ報告済み・未対応
  （サンプルデータ側の問題）。
- エンジンのエラーメッセージ日本語化は**意図的に対象外**とした（pydantic 組み込み
  メッセージと混在するため）。必要になったら別途検討。

## 7. 作業ルール（ユーザーが明示的に要求）

**設計相談・バグ報告には、まず口頭で回答 → 実装方針を提示 → 同意を得てから実装する。**
いきなりコードを書かない。AskUserQuestion より先に選択肢を文章で説明すること。

## 8. 環境・実行方法

- Windows 10 / PowerShell。コンソールは cp932 → `PYTHONIOENCODING=utf-8` が必要。
- venv: `.venv`（Python 3.11）。polars 1.42.1 / pydantic 2.13.4 / streamlit 1.59.2 / streamlit-sortables 0.3.1。
- テスト: `.venv/Scripts/python -m pytest`
- UI 起動: `.venv/Scripts/streamlit run ui/app.py`
- UI の動作確認は `tests/data/result_tmp_mini` + `config_mini.jsonc` +
  `tests/fixtures/`（係数・B9LS.json・custom_parts.py）の組み合わせが便利。
- 圧縮前の完全な会話ログ:
  `C:\Users\naugh\.claude\projects\C--Users-naugh-Desktop-dev-scorelib_param\57035457-a4ad-442d-835a-d954accd1458.jsonl`

## 9. 追記: 2026-07-28 相対化仕様変更 v1 実装セッション

（2026-07-20〜27 の間のセッション記録は本ファイルに追記されていない。この間の
主な作業 = バッチスコア計算 `scorelib_param/batch` の実装（docs/batch_design.md）、
v0.4.0 の機能群（集計時重み・変換ステップ拡張・Physical 記法）、filter 前出し
最適化、性能調査 — 内容は各設計書・code_reference・git log を参照。）

担当者ヒアリング（2026-07-23〜28）で Override 判定の廃止と Measure 番号基準への
仕様変更が確定し、v1 を実装した。**経緯・合意・設計は
`docs/spec_change_dataname_measure.md` が正**（本ファイルは要点のみ）。

- **実装済み（同ノート4節プラン1〜4）**: エンジン（Measure 軸の条件付き公開・
  filter の is_in・labels 注記・relative の None 拒否）、introspect
  （Measure/DataName 軸カタログ・measure_labels）、`scorelib_param/dummy.py`
  （ダミー一式の Board/Chip 複製展開・疑似ダミー化。`scripts/make_pseudo_dummy.py`）、
  UI（相対化プリセット廃止・split 軸の Override 限定解除・「dataName (Measure N)」
  複合表示・filter 複数選択・画面1のダミー展開）。
- **テスト 258 件全パス**。新規: test_measure_split.py（新旧仕様の厳密同値）、
  test_dummy.py（複製不変性による展開検証）。
- 実装で確定した追加仕様: **Measure 軸と他軸分割の相対化は併用不可**
  （ペア結合キーに Measure が残り0ペア）→ Measure のある type の雛形は
  Label/Override 軸を除外。
- **未着手**: 同ノートプラン5〜8（validate モード・0行マッチ時の候補列挙・
  被覆情報表示）と、dataName 命名ルール確定後の第2弾（ペア候補提示・自動セット）。
- ドキュメント反映済み: README / code_reference / testing_guide /
  score_gui_design 3.3節注記 / score_gui_ui_design 画面1・2。
- **エンジン版を 0.5.0 へ**（設定語彙の拡張: Measure 相対化・filter リスト・
  labels は 0.4.0 以前のエンジンでは読めないため、版で見分けられるようにした）。
- **続報（同日・0.5.1）**: 「WL/STR 本数は世代で固定・フローの部分測定は無い」の
  確定を受け、**世代情報 json（{Generation}.json）を非必須化**。UI の入力欄を廃止
  （簡潔 UI 優先のユーザ方針）し、本数整合チェックはデータ由来で常時実行、
  自動検出された json は食い違い診断のみ。エンジンの Physical 記法 N も
  データ由来へフォールバック（json があれば互換優先。cli.derive_axis_counts）。
  spec ノート 9.1節に記録。テスト 260 件パス。
- **続報（同日）: 「設定だけ編集」経路を正式化**。画面1に設定jsoncアップロードの
  入口を追加（state.load_config_only）。カタログを設定自身が言及する軸名から導出し、
  データもダミーも無しで式・グループ定義・パーツの修正とエクスポートができる
  （モデル検証はデータ非依存で全部働く。テスト計算のみデータ読み込みが必要 —
  ディレクトリ未入力は明確なエラー）。ユースケース: WLgroup や式だけの微修正。
  版数は 0.5.1 のまま（ui/ のみの変更でエンジン・設定語彙は不変）。テスト 264 件パス。
- **続報（同日）: 開発機を Python 3.13 へ移行**（winget で 3.13.14 導入・.venv
  再構築。polars 1.43 / streamlit 1.60 / pytest 9 に更新、264 件パス）。
  開発と本番の版ズレが消えたため **CI は 3.13 のみに簡素化**。移行で表面化した
  tarfile の 3.14 非互換予告は staging.py に filter="data" を指定して先回り
  （CHANGELOG「未リリース」）。§8 の「venv は 3.11」記載は旧情報。
- **続報（2026-07-29）: エンジン 0.5.2 リリース**。未リリースだった tar 展開の
  filter="data" 修正を ver.0.5.2 として確定（SVN 初回同期は未実施のため番号を
  先行させても不一致を見る相手がいない。同期はこのタグ以降から行う）。
  版上げのタイミングを「SVN 同期時」から「エンジン変更のあるリリース時
  （同期は後日でも可）」に README で明確化。
- **続報（2026-07-29）: リリースタグを2系統に整理**。UI のみのリリースで
  エンジン版数を上げると、SVN を共同開発する他の開発者に「SVN が古く見える」
  偽の不一致を見せる問題（ユーザ指摘）→ **版数・ver.* タグはエンジン専用**、
  **UI のみのリリースは番号なしの ui-YYYYMMDD タグ**に分離。CHANGELOG も
  2種類の見出しに再構成し、今日までの UI 改修を `ui-20260729` 節として記録。
  README リリース手順・CLAUDE.md・dev_workflow を更新。
- **続報（2026-07-29）: マルチユーザ対応 — 下書きのユーザ別分離**。Streamlit は
  セッション分離が基本設計のため同時操作で編集は混ざらないが、下書きだけが
  単一ファイル共有で相互上書き・他人の下書き復元が起きるところだった。
  サイドバーの名前入力ごとに `~/.scorelib_drafts/<名前>.jsonc` へ分離
  （state.draft_path_for。未入力の間は自動保存・復元停止）。認証ヘッダ
  （既定 X-Remote-User / SCORELIB_UI_USER_HEADER）があれば名前入力を自動置換。
  README に「複数ユーザでの利用」（一時ファイルの tmpfiles.d 掃除ルール込み）と
  nginx + Basic 認証 + WebSocket の設定例を追加。ローカルに溜まっていた
  scorelib_* 一時ディレクトリ345個も掃除。テスト 269 件パス。版数 0.5.1 のまま。
- **続報（〜2026-07-29）: 画面1を「① スコア設定 + ② データ」の2段構成に再構成**
  （ユーザ要望を3往復反映: 「入力方法が複数見えて混乱しない画面に」「既存 config を
  読み込んで取捨選択・調整するのがほぼ毎回の使い方」「ユーザは UI サーバ上で
  操作しない → パス指定は開発者専用に」）。最終形: ①設定jsoncアップロード =
  編集の出発点（両形式対応・置き換えは ↩ で復帰可）、②データ = 実測 zip /
  ダミー / なし の radio、読み込みボタン1つ。**一般ユーザの画面はアップロード
  のみ**で、パス指定は開発者モード（`-- --dev` / `SCORELIB_UI_DEV=1`）のトグルで
  のみ表示（オン時はアップローダがパス欄に置き換わる — 手段は併記しない）。
  「普段は〜」等のメタ語り文言・「上級者向け」文言は廃止。中間案（並列 expander
  群 → 目的の3択）の経緯は ui設計書 画面1。テスト 268 件パス。版数 0.5.1 のまま
  （ui/ のみ）。
- **続報（同日）: アップロード入力の拡充**（ユーザ要望）。ダミー一式を zip
  アップロードで受け付け（パス指定と併存）、設定jsonc・係数jsonc・custom_parts.py
  の個別アップロード（save_upload で一時保存 → パス欄へ自動反映）を追加。
  サーバ運用ではローカルパスが使えないため、アップロードが正規の入力経路になる。
  「Board 数ぶん」の曖昧な文言も修正。テスト 266 件パス。版数は 0.5.1 のまま
  （ui/ のみの変更）。
- **続報（同日）: 社内配置の具体化**。実運用トポロジ（開発=社内 Ubuntu サーバ、
  UI=Ubuntu 実行サーバ、エンジン=SVN+miniforge）に合わせ、README のコマンド表記を
  **Ubuntu（.venv/bin/）主体**に統一（Windows は読み替え注記でサブ扱い）。
  「UI 実行サーバの立て方」を新設: GitLab のタグから **必要4点のみ**
  （scorelib_param / ui / custom_parts.py / pyproject.toml）を git archive で取り出し、
  docs 等をサーバに持ち込まない配置+ headless 起動 + systemd 常駐化の例。
  未確認事項（ポート開放・認証の要否・python3.13 の有無・Docker の可否）は
  README 内に「初回に確認が必要なこと」として明記。
- **続報（同日）: 開発運用の整備**。trunk-based のブランチ方針と リリース手順
  （版上げ→CHANGELOG→タグ→SVN同期）を README に明文化。GitHub Actions
  （Python 3.11/3.13 マトリクスで pytest）、CHANGELOG.md 新設、pre-push フック
  （scripts/hooks。`git config core.hooksPath scripts/hooks` で有効化）、
  タグ `ver.0.5.1` を付与。
