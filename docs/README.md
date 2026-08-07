# ドキュメント索引

リポジトリに入っている文書はすべてチームの共有物です。対象読者を明記します。

| ファイル | 対象読者 | 内容 |
|---|---|---|
| [`../README.md`](../README.md) | **全員（まずここ）** | 使い方: セットアップ、config.jsonc の書き方、UIの起動と5画面、CLI、開発の進め方・リリース手順 |
| [`../CHANGELOG.md`](../CHANGELOG.md) | SVN 側の利用者・全員 | エンジン版数ごとの変更点（同期で何が変わるか） |
| [`code_reference.md`](code_reference.md) | チーム報告・引き継ぎ | **今のコードに何があるか**: 全ファイル・全関数の解説（コード変更時に追随更新） |
| [`testing_guide.md`](testing_guide.md) | テストに馴染みのない開発者 | テストの考え方入門+全テストファイルの解説（テスト変更時に追随更新） |
| [`dev_workflow.md`](dev_workflow.md) | git・CI に馴染みのない開発者 | 開発運用の解説: フック / .gitattributes / CHANGELOG / CI（GitHub Actions・社内 GitLab）/ タグの正体と日々のフロー・FAQ |
| [`score_gui_design.md`](score_gui_design.md) | 開発者 | エンジンの設計書: **なぜこの設計にしたか**の意思決定記録 |
| [`score_gui_ui_design.md`](score_gui_ui_design.md) | 開発者 | UIの設計書: 画面構成・状態管理・配置/デプロイ方針の意思決定記録 |
| [`batch_design.md`](batch_design.md) | 開発者 | バッチスコア計算（scorelib_param/batch）の設計書 |
| [`spec_change_dataname_measure.md`](spec_change_dataname_measure.md) | 開発者 | 相対化の Measure 番号基準化・ダミー一式方式の仕様変更ノート（担当者合意の記録。実装完了後に score_gui_design.md へ統合予定） |
| [`uv_groups_migration_plan.md`](uv_groups_migration_plan.md) | 社内リポジトリの作業者 | dev 依存の dependency-groups 移行の作業指示書（本リポジトリでは適用済み。社内側の実施後に削除予定） |
| [`flow_guide.html`](flow_guide.html) | 全員（コードの中身を知りたいとき） | 処理フロー図解: 13ケースの呼び出しチェーンを関数名+行番号つきの図で追う（ブラウザで開く。図の描画にはネット接続が必要 — 冒頭の注記参照） |
| [`score_gui.md`](score_gui.md) | 参考（歴史資料） | プロジェクト初期の要件整理・検討の記録 |
| [`score_gui_progress.md`](score_gui_progress.md) | 開発引き継ぎ用 | 開発セッションの時系列記録（AI開発の途中経過。別タスクからの再開用） |

個人的なメモはリポジトリに入れない（入れたい場合は .gitignore したディレクトリを使う）。

## 変更時の追随更新ルール

コード変更を「完了」と呼ぶ条件（AI開発時のチェックリストはリポジトリ直下の
`AGENTS.md`。人が開発する場合も同じ）:

1. テスト追加+全件パス
2. **ドキュメント追随**: code_reference.md（コード変更時は必ず）、
   testing_guide.md（テスト変更時は必ず）、README（使い方・config に影響する場合）、
   設計書（設計判断を伴う場合。過去の記録は消さず日付つき注記で更新）、
   score_gui_progress.md（セッション記録）
3. **バージョン判断**: `scorelib_param/__init__.py` の `__version__` を上げるかを
   毎回明示的に判断する。**設定 jsonc の語彙が変わる変更は必ず上げる**
   （詳細は ../README.md「バージョンの上げ方」）
