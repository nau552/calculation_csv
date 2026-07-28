# 変更履歴（CHANGELOG）

エンジン版数（`scorelib_param/__init__.py` の `__version__`）ごとの変更点。
SVN 側の利用者が「同期で何が変わるか」を読む場所（開発の時系列記録は
`docs/score_gui_progress.md`、設計判断は各設計書）。

書式の目安: 版ごとに「追加 / 変更 / 修正」を箇条書き。版を上げたら
リリース手順（README「リリース手順」）の一部としてここに追記する。

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
