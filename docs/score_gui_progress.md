# score_gui Phase1 進捗まとめ（引き継ぎ用）

最終更新: 2026-07-20（別タスク・別セッションからの再開用。まずこのファイルを読む）

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
- split_axis の型ごとのデフォルト（読み込み系=Read_Override / 書き込み系=Program_Override
  の確定。担当者確認中）。
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
