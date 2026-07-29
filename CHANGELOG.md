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
