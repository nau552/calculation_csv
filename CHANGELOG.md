# 変更履歴（CHANGELOG）

リリースごとの変更点。見出しは2種類:

- **ver.X.Y.Z** — エンジンのリリース（`scorelib_param/__init__.py` の
  `__version__` と一致。**SVN 同期の単位**。SVN 側の利用者は「同期で何が
  変わるか」をここで読む）
- **ui-YYYYMMDD** — UI のみのリリース（**エンジン変更なし = SVN 同期不要**。
  UI サーバへの配布タグ）。UI の更新でエンジン版数は動かさない — SVN 側と
  番号がズレて見える混乱を防ぐため

開発の時系列記録は `docs/score_gui_progress.md`、設計判断は各設計書。
リリース手順（README）の一部としてここに追記する。

## ver.0.7.1 — 2026-08-01

エンジンの数値結果・config(jsonc)の語彙は 0.7.0 から不変。**呼び出し互換と
エラー時の挙動が変わる**(各項の互換性注意を参照): 公開 API の位置渡しは
不可に、欠けデータの計算は「黙って値がズレる」代わりにエラーで止まる。

- **パーツ計算のエラーを全件まとめて報告**（2026-08-01。挙動変更）。従来は1個目の失敗で即座に落ちたため「1つ直して動かしたら
  次のエラー」の往復になっていた。全パーツを計算し終えてから、失敗した全パーツを
  1行ずつ名指しした例外で落とす(1件なら従来と同じ形・同じ型)。**落ちるか
  どうかは不変**: 失敗が1件でもあれば値は返さない(部分結果で実験が続く経路は
  無い)。UI 側の対応(次の ui-YYYYMMDD 節の材料): 「検証」という語を全廃し
  「設定の誤り」に一本化(サイドバーは「問題なし」または「設定の誤り N 件」+
  展開で全メッセージ。構造の誤り・データに無い値・データ無し type を合算 —
  ダミーは本番構造を模す前提のため。一覧の列は「状態」で ⚠ の理由を書き分け。
  テスト実行前ガードも種類を問わず拒否+全列挙)
- **計算エラーの原因診断+「黙って値がズレる」経路の遮断**（2026-08-01。
  **挙動変更あり = 次リリースの版上げ対象**）。実機報告(相対化の評価側が無い
  ダミーデータで、誤誘導のエラー文言により原因究明が迷走)への対応:
  - 最終結果が null / NaN のとき、エラー経路限定でパイプラインを歩き直し、
    **原因ステップを名指し**する(「filter X == 'v' matched no rows」「relative
    split 'Read_Override': no rows where … (evaluation side)」「dVtBudget
    coefficient/temperature lookup failed for N of M rows (missing (Board,
    State) = …)」など)。推測の決め打ち文言「a filter value probably matched
    no rows」は廃止。成功時の実行コストは不変
  - **照会失敗の部分欠けをエラーに**: dVtBudget 係数/温度・相対化ペア・diff の
    相手が一部の行で見つからない場合、以前は null が mean/max で黙って除外され
    **エラーなしで別の値**が出ていた(実測で確認)。NaN として伝播させ必ず検出
    する(polars の min/max は NaN を飛ばすため集計側で毒化を保証)。
    **注意: 今まで「動いていた」計算が止まる可能性があるが、その値は元々
    誤っていた**。集計時重みの欠けは従来から明示エラー(変更なし)
  - 係数表に無い Generation は生の KeyError でなく利用可能な世代一覧つきの
    ValueError に
  - UI の値候補: **Override 軸も実データ由来に**([False, True] ハードコード
    廃止 — 評価側の測定を含まないデータで True が候補に出てしまい、
    「候補には在るが行が無い」を UI が検出できなかった)。これにより相対化の
    分子/分母の不一致も読み込み直後の「⚠ データ不一致」で分かる
- **計算エラーが失敗したスコアパーツを常に名指しするように**（2026-08-01。
  数値結果は不変・エラーメッセージのみ変更 = この変更も次リリースの版上げ
  対象に含む）。filter の空振り（データに無い値）などの深部エラーはパーツ名を
  知らず「aggregation produced null for 'FBC'…」のように type 名しか出なかった
  （ユーザー報告）。`compute_score_file` がパーツ計算中の ValueError に
  「score part '名前': 」を前置して再送出する（既に名指し済みのメッセージは
  そのまま）。バッチの epoch 逐次フォールバックも同関数経由のため同様に名指し
  される。UI 側の対応（読み込み直後からの「⚠ データ不一致」表示・エディタが
  候補に無い値を黙って消さない変更）は次の ui-YYYYMMDD 節の材料:
  state.part_value_mismatches 新設(カタログは実際に読む type で引く —
  dVtBudget パーツは FBC)、サイドバーに「データ不一致 N パーツ」の件数表示
  (構造検証の「検証 OK」とは別勘定)、multiselect 系エディタは候補に無い
  既存値を選択肢に残して警告表示。**さらに「描画は設定を変えない」を原則化**
  (2026-08-01 実機報告: 候補が実データ由来になったことで、プルダウンの
  index=0 スナップが「画面を開いただけで相対化の分子 True → False」の設定破壊
  として顕在化)。書き戻しウィジェット全数を監査し、候補外の既存値・参照・
  辞書キーを印つき(「(データに無し)」「(存在しません)」等)で保持する定石に
  統一。設定を変えるのはユーザー操作起点のみ。不変条件 AppTest
  (全部盛り設定×全画面×全選択で score_file 不変)で機械的に担保。
  **パーツ改名が「編集するパーツ」欄に追随しない問題も修正**(streamlit#11268:
  キー付き selectbox は選択中ラベルだけの変更で表示が更新されない —
  D&D 一覧と同じ「ラベルが変わったらキーごと再マウント」方式に変更。
  選択の実体は _uid のまま・マーカーの即時追従も維持)。
  **「元に戻す」を作り直し**(2026-08-01 実機報告: 一部の操作しか戻らない
  ように見える): 旧実装の「ウィジェット状態の一括削除」ではキーが同じ部品の
  ブラウザ表示が残り、復元した設定と画面が食い違っていた。undo 世代キーで
  設定由来の全エディタを作り直し(表示は常に score_file から再生成 = 見た目と
  計算・エクスポートは一致)、履歴に記録した「編集していた画面・パーツ」へ
  跳んで取り消しが目の前で見えるように。グループ定義の重みセット欄も
  値ごと辞書の共通編集欄に統合(候補外キーの保持・描画で育てない)
- **品質向上パス: 関数サイズ規約の免除を解消（96 件 → 10 件）・公開 API の
  省略可能引数をキーワード専用化**（2026-07-31。数値結果・エラー文言は不変。
  **互換性注意: エンジン API の呼び出し形が一部変わるため、この変更を含む
  リリースでは版数（最後の数字）を上げること**）。内容:
  - **公開 API のキーワード専用化**: `compute_score_part` / `compute_score_file` /
    `compute_dummy_part` は省略可能引数、`apply_dvtbudget` は epoch_col、
    `validate_epoch` は needs_dvtbudget を `*,` の後ろへ。**位置引数で渡して
    いた呼び出しは動かなくなる**（キーワード渡しは従来どおり。リポジトリ内・
    README・ブリッジ見本は追随済み）。dataclass への束ね直しは公開 API では
    行わない（組み込み側の互換資産のため — docs/dev_workflow.md 参照）
  - **例外型の変更（TRY004）**: 型の誤りの検査 3 箇所を ValueError → TypeError
    に（custom 関数が見つからない/呼び出し可能でない・世代情報 json の
    トップレベル型・import_score_file の入力型）。except Exception で受けて
    いる UI・バッチ経路には影響なし
  - **関数分割・内部の引数束ね**: エンジン（models._check_value_shape 分岐41、
    cli.compute_score_part、batch.compute_score_batch ほか）・UI（画面1/2/3/5
    の大関数）・scripts を、挙動を固定するテスト（AppTest 19 本増強、計 324 件）
    の下で分割。try 節の抽出は本体丸ごと1呼び出しで捕捉範囲不変
  - ruff.toml の関数サイズ系免除（ディレクトリ単位)を全廃。意図して残す例外
    （公開 API の PLR0913・変換関数の PLR0911 のみ）は該当する関数の def 行の
    行単位抑止に理由付きで置き、同じファイルの新しい関数に免除が及ばない形に
    （ラチェット方式）。CI の ruff 検査に素の（--preview 無し）実行を追加し
    「両モードで警告ゼロ」を機械的に担保
- **型チェック(Pylance/pyright)を警告ゼロ化し CI に組み込み**（2026-07-31。
  挙動・config 語彙の変更なし = 版数は動かさない）。チームの水準
  「Pylance の警告が出ないコードを書く」に合わせ、pyright 1.1.411 実測
  416 件を抑止なしでゼロ化。主因はコード規約対応時に `Any` → `object` に
  していた注釈で、TypedDict 化（テストの fixture）・注釈の精密化・
  `@overload`（compute_score_part: identity_axes 空 → float / 指定 →
  DataFrame）・同値変形で解消。**本物のバグは 0 件**（全 51 候補を個別確認）。
  付随: `batch/__init__.py` の `__all__` を静的リスト化（遅延 import は維持、
  `_EXPORTS` との整合はテストで検証）・到達しないパスへの防御 raise 数箇所・
  compute_score_file の戻り値注釈を実態（expression 無しなら Score=None）に
  修正・pyright[nodejs] と psutil を dev extras に追加・CI に型チェック追加
- **ruff の検査基準をコンテナのエディタ表示(preview)まで拡張**（2026-07-31。
  挙動・config 語彙の変更なし = 版数は動かさない）。コンテナの VS Code は
  ruff 拡張が preview ルールで動いており、CLI ゼロでもエディタに警告が残って
  いた（実機で 24 件確認 → 全体では 496 件）。CLI・CI に `--preview` を付けて
  基準を一致させ再ゼロ化（ruff.toml 本体はチーム原本の転記のまま）。主な修正:
  docstring に Returns/Raises/Yields 節を追記（252 件）・`# noqa` を
  `# ruff: ignore[コード]` 表記へ一括変換（preview の推奨形式のうち、素の
  実行でも抑止が効くコード表記を採用）・テストメソッドの
  @staticmethod 化（32 件）・`open()` の encoding="utf-8" 明示（5 件 —
  Linux の既定と同じで挙動不変、Windows だけ本番と同じ UTF-8 に揃う）・
  set リテラル化等の細かい書き換え。関数サイズ系の新ルール
  （PLR0914・PLW0717）は既存方針どおり理由付き免除にし品質向上パスの
  作業リストへ
- **コード規約をチーム共通コンテナの ruff 設定に統一**（2026-07-31。挙動・config
  語彙の変更なし = 版数は動かさない）。コンテナの `.devcontainer/ruff.toml` を
  リポジトリ直下 `ruff.toml` に転記し、リポジトリ全体を警告ゼロ化
  （当初 4822 件 → 0 件）。主な変更: 全ファイル `ruff format` 整形・
  copyright ヘッダ付与・コメント/文言の全角記号を半角化・全関数に型注釈と
  docstring・例外メッセージの変数化・os.path → pathlib。「直すと挙動が変わる」
  指摘は行単位 noqa（理由付き）と ruff.toml の scorelib 固有節（理由明記）で
  容認し、将来の品質向上パスの作業リストとして残した。ruff は dev extras に
  0.16.0 固定で追加し、CI（GitHub Actions / GitLab 下書き）に
  `ruff check` + `ruff format --check` を組み込み
- **ブリッジ見本（scripts/*_bridge_example.py）: config の dump 前正規化を追加**。
  現行の config ローダは読み込み時に WLgroupWeight / KLDweight 等を
  pandas Series（値は numpy 型）へ加工するため、加工済み dict の
  json.dump が失敗していた（実機で判明）。to_dict / item 等の振る舞い判定で
  素の Python 型へ再帰変換してから書き出す（エンジンが読むフィールドは
  手書き config と同じ形に復元される）。**実機の kicOpt 側へ貼ったコピーにも
  反映が必要**。エンジン本体は変更なし（版数は動かさない）
- **ブリッジの config はファイルパス渡しを正に**（docstring 明記）: ローダの
  加工には範囲展開（[0,3]→[0,1,2,3]）など逆変換不能なものがあり、加工済み
  dict に依存しない。「メモリにだけ存在するエンジン語彙（自動補完）が無い」
  ことを実機で確認する診断 `scripts/config_vocab_diff_example.py` を追加

## ver.0.7.0 — 2026-07-29

**WLgroup 定義の在り処を1つにする**（score.jsonc の語彙が変わるため真ん中を
上げる）。従来はエクスポートが編集後の WLgroup を `groupDefs` に入れるだけで
`optimization.WLgroup`（実験スクリプトが読む場所）に書き戻さなかったため、
合成後 config に新旧2つの定義が並び、①実験のパラメータ割り当てが編集前の
定義のまま、②手編集時にどちらが使われるか分からず変え忘れが起きる、という
問題があった（ユーザー指摘）。

- **UI エクスポート**: WL 軸の "WLgroup" 定義（と WLgroupWeight）を旧形式キー
  （`WLgroup` / `WLgroupDefinLogical` / `WLgroupWeight`）**だけ**に書き出し、
  groupDefs / weightSets には残さない。合成すると実験スクリプトが読む場所が
  そのまま編集後の内容になり、定義は常に1つ
- **ScoreFile が旧形式キーを解釈**（score.jsonc 単体形式でも読める）。
  groupDefs / weightSets に同名があればそちらが勝つ（RunConfig と同じ優先）
- `RunConfig.to_score_file()` が旧 WLgroup も groupDefs へ統合（weightSets と
  対称に。エンジン計算は従来から統合済みのため挙動不変 — UI 取り込み経路の
  取りこぼし修正）
- UI: グループ定義名 "WLgroup" は WL 軸の予約名に（旧形式キーで表現できない
  ため WL 以外の軸には使えない）
- **互換性**: 合成後 config は昔からある語彙のみなので旧エンジンでも同じに
  動く。旧エンジンが**新しい score.jsonc 単体**を読んだ場合のみ WLgroup が
  読み飛ばされ、参照パーツが明示エラーになる（静かな誤計算にはならない）。
  旧形式（groupDefs に WLgroup 入り）の score.jsonc は引き続き読める

## ver.0.6.1 — 2026-07-29

- **DataName の map の綴りを実出力に追随**: 実験フローの実出力は
  `map_DataName.csv`（大文字D）と実環境で判明。従来エンジンは
  `map_dataName.csv` 固定だったため、大文字小文字を区別する Linux の
  実環境でだけ「DataName の候補が出ず自由入力になる」不具合になっていた
  （Windows 開発機では再現しない）。両方の綴りを読むよう修正し、
  リポジトリ内サンプルは実出力の綴りへリネーム

## ver.0.6.0 — 2026-07-29

KLD / dVthSGWLD の標準計算（log・絶対値・要素選択つき総和）と、vthSkip
（測定されない epoch のダミー値計算）への対応。**設定 jsonc の語彙が増える**
（真ん中の数字を上げる条件）: `abs` / `log`（+`floor`）op と
`optimization.vthSkip` の解釈が旧エンジンには無い。

- **変換ステップに単項op `abs` / `log` を追加**: `abs` = \|x\|、
  `log` = ln(max(\|x\|, floor))（`floor` 必須 — 0 や負値で発散しない安全な対数。
  KLD の標準計算 `np.log(np.maximum(np.fabs(x), 1e-6))` が1ステップで書ける）
- **vthSkip（実験 config の既存項目 `optimization.vthSkip`）をエンジンが解釈**:
  スコアパーツの type ファイル（KLD.csv / dVthSGWLD.csv）が無い epoch は、
  `dummyKLDValue` / `dummyDVthValue` を「変換後の値」として敷き詰めて計算する
  （変換ステップはスキップ・集計は通常どおり。例: KLD 0 → 0.0、
  dVthSGWLD 1 → 残す8要素の総和 8.0）。`epochs` は使わない（ファイル不在が
  トリガー — batch で過去データを流用しても正しく働く）。単一 epoch 計算は
  stderr note、batch は `BatchResult.dummy_used` + stderr で使用を報告。
  ダミー値の設定が無い場合は従来どおり（skip-and-report / strict エラー）
- **UI: KLD / dVthSGWLD の type 別雛形**: パーツ追加・雛形再生成で標準計算
  （KLD = Board/Chip mean → log → 0.1 重みの SGWLD 総和、dVthSGWLD =
  Board/Chip/Block mean → abs → SG系4要素を除く8要素の総和）が入った状態で
  生成される（初期値であって強制ではない）。変換ステップのエディタに
  abs / log（floor 入力）を追加
- **UI: データに無い type のパーツに警告表示**: 別実験の config を読むと今の
  データに無い type（例: tR）のパーツが残る — これは意図した設計（取捨選択は
  ユーザーが行う）のまま、一覧に「⚠ データ無し」、編集画面に警告文を出して
  テスト計算前に気づけるようにした
- sample.jsonc に vthSkip の注釈つき見本を追加

## ver.0.5.3 — 2026-07-29

新計算対象（KLD / dVthSGWLD / PROGLOOP / PROGSTATUS / tPROG）の実フォーマット
確定に伴う対応。設定 jsonc の語彙は不変（機能追加のみ = 最後の数字）。

- **type 検出を値列ルールへ変更**（`introspect.detect_types`）: 「Measure 列を
  持つ csv」から「**ファイル名と同名の値列を持つ csv**」へ。KLD / PROGLOOP など
  Measure 無し type が検出されるようになった。値列の無い csv は計算できないため
  type にしない（従来の Measure ルールを包含する厳密化）
- **軸カタログ: parameterLabel の全行空欄の列を軸として出さない**
  （`introspect.axis_catalog`）: tPROG の Read_Label のように「この type に
  存在しない設定」が空欄列で出力されるケースに対応
- UI 雛形: 相対化の既定 split 軸に **Param（ROM=基準パラ / Opt=提案パラ）を
  Measure の次の優先で追加**（`ui/state.default_split_axis`）。PROGLOOP 系は
  分母 ROM・分子 Opt が初期値になる
- テストデータ（tests/data/result_tmp_mini）を実験の実フォーマットに追随:
  PROGLOOP / PROGSTATUS 差し替え（SGWLD 列は実在しない・Param 列あり）、
  dVthSGWLD / tPROG / parameterLabel_tPROG（Read_Label 空欄）を追加、
  map_Label に要素 3, 4 を追加

## ver.0.5.2 — 2026-07-29

- batch の tar.gz 展開に `filter="data"` を指定（既存の自前パス検査への多重防御 +
  Python 3.14 で必須になる引数の先回り。通常のアーカイブの挙動は不変）

## ui-20260729 — 画面1の再設計・マルチユーザ対応

エンジン変更なし（同梱エンジンは 0.5.1 + 上記 tar 展開修正 = 0.5.2 相当。
挙動互換のため差は運用上無害）。

### 追加
- **入力の全面アップロード対応**（サーバ運用の正規経路）: 一式 zip /
  ダミー一式 zip / 設定 jsonc / 係数 jsonc / custom_parts.py
- **下書きのユーザ別分離**: サイドバーの名前ごとに
  `~/.scorelib_drafts/<名前>.jsonc`（未入力の間は自動保存・復元なし）。
  リバースプロキシ認証のユーザ名ヘッダ（既定 `X-Remote-User`、
  `SCORELIB_UI_USER_HEADER` で変更可）による自動化に対応
- **開発者モード**（`streamlit run ui/app.py -- --dev` / `SCORELIB_UI_DEV=1`）:
  「サーバ上のパスで指定する」トグル。一般ユーザの画面には出ない

### 変更
- 画面1を **「① スコア設定（編集の出発点）+ ② データ（実測 / ダミー / なし）」
  の2段 + 読み込みボタン1つ**に再設計（既存 config からの取捨選択・調整という
  実際の使い方を主役に。「設定だけ編集」は独立モードから「② = なし」へ統合）
- 文言整理: メタ語り・過大な見出し・「上級者向け」表記を廃止

### 運用
- README に「複数ユーザでの利用」（セッション分離の説明・一時ファイルの
  tmpfiles.d 掃除ルール）と nginx + Basic 認証の設定例（WebSocket 転送・
  ユーザ名ヘッダ転送込み）を追加

## ver.0.5.1 — 2026-07-28

相対化仕様変更 v1（docs/spec_change_dataname_measure.md 9節の合意の実装）。
※ 0.5.0 は同日中の中間版数で、単独では配布されていない。

### 追加
- **Measure 番号基準の相対化・filter**: `split_axis: "Measure"` と Measure filter。
  dataName は「dataName (Measure N)」の複合表示と `labels` 注記（表示・検証用）
- **filter の複数値（is_in）**: `{"op": "filter", "value": [1, 3]}` — 該当行を
  すべて残し、後段の集計に複製として流す
- **ダミー一式からの測定前設計**: `scorelib_param/dummy.py`（Board/Chip 複製展開・
  正データの疑似ダミー化）+ UI 画面1の展開入力（Board 数・Board ごとの Chip 数）。
  `scripts/make_pseudo_dummy.py`
- **「設定だけ編集」経路**: 設定 jsonc のアップロードだけで式・グループ定義・
  パーツを修正しエクスポートできる（データ・ダミー・テスト計算不要）
- introspect: Measure / DataName 軸のカタログ、`measure_labels`（番号→dataName）

### 変更
- **UI の相対化プリセット廃止**: 旧「Read_Override があれば自動ON」を撤去。
  ONにすると split=Measure・分子/分母は候補位置で初期セット
- **世代情報 json（{Generation}.json）を非必須化**: WL/STR 本数はデータから導出
  （本数は世代で固定・フローの部分測定は無い、の確定を受けて）。UI の入力欄を
  廃止し、自動検出時は食い違い診断のみ。エンジンの Physical 記法も
  データ由来へフォールバック（json があれば互換優先）
- Measure 軸のある type の雛形は Label/Override 軸を除外（Measure 番号が
  一意に決める測定メタデータのため）
- 相対化の分子/分母未設定（None）は読み込み時に明確なエラー

### 互換性
- 旧設定（Read_Override 分割等）はそのまま動く。新設定（Measure 相対化・
  filter リスト・labels）は 0.4.0 以前のエンジンでは読めない
- **Measure 軸と「Measure 以外の軸での相対化」は併用不可**（ペア結合キーに
  Measure が残り0ペアになる。仕様どおりの帰結）

## ver.0.4.0 — 2026-07 中旬

- バッチスコア計算 `scorelib_param.batch`（過去実験 epoch 群の一括計算。
  単一 epoch 計算と数値等価）
- 集計時重み（`weight` / `weight_ref`）・変換ステップ拡張（add/sub/mul/div・
  グループ別重み）・Physical 記法のグループ定義
- filter 前出し最適化（結果不変の高速化）・type 単位の共有読み込みキャッシュ

## それ以前

git log と `docs/score_gui_progress.md` を参照（エンジン初版〜UI 5画面・
グループ定義・custom パーツ・一式 zip 対応など）。
