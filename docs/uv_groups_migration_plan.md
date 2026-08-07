# 作業指示書: dev 依存の dependency-groups 移行 + uv.lock 更新（社内 scorelib_param 用）

対象: 社内 GitLab の scorelib_param リポジトリ（チーム devcontainer で開発しているもの）
実行者: 担当者本人 or リポジトリ内のエージェント（この文書だけで完結するように書いてある）
所要: 30分程度（テスト実行時間含む）
2026-08-07 作成。**本リポジトリ（この文書が入っている側）では同変更を適用・検証済み**
（commit 724d8ac: 全339テストパス、rebuild 再現試験クリア、CI 緑）— 手順の実物例として
本リポジトリの pyproject.toml / uv.lock / CHANGELOG「未リリース」を参照してよい。
**社内側での作業完了後、この文書は削除してよい**（役目が終わるため）。

---

## 1. 目的と背景

**直す現象**: コンテナを Rebuild すると streamlit 等が消えていて、手で入れ直すまで UI が動かない。

**原因**: 開発用依存（pytest, streamlit, ruff, pyright, psutil）が pyproject.toml の
`[project.optional-dependencies]`（extras）の `dev` に入っている。チームコンテナの
postCreateCommand は `uv sync --frozen` を実行するが、**uv sync は extras を含めず、
かつ「宣言にないパッケージは削除する」正確同期**のため、rebuild のたびに dev 一式が
入らない・消える。

**対策**: dev を `[dependency-groups]`（PEP 735）へ移す。`dev` という名前のグループは
**uv sync が既定で含める**ため、postCreate だけで開発ツール一式が揃うようになる。
`ui` extra は「UI 実行サーバが `pip install -e ".[ui]"` で使う公開機能」なので**動かさない**。

**付随修正（手順 3-0）**: pytest に `pythonpath = ["."]` を明示する。`ui/` は
インストールされないリポジトリローカルのパッケージで、その import は
「`python -m pytest` がカレントディレクトリを sys.path に足す副作用」に暗黙依存
している。このため **`uv run pytest`（直接起動）だと ui 系テスト4ファイルが
import エラーで落ちる** — 明示化でどの起動形でも動くようにする（本書の検証
コマンドの前提でもある）。

---

## 2. 前提確認（作業前に必ず）

リポジトリ直下で以下を確認する。**1つでも想定と違ったら作業を止めて報告**すること。

- [ ] `pyproject.toml` に `[project.optional-dependencies]` があり、その中に `dev = [...]` がある
      （既に `[dependency-groups]` に dev がある場合、この作業は適用済み。終了）
- [ ] `uv.lock` がリポジトリにコミットされている（`git ls-files uv.lock` で出る）
- [ ] `.devcontainer/postCreateCommand.sh` の sync 分岐が「lock あり → `uv sync --frozen`」である
- [ ] `uv --version` が通る（チームコンテナ内なら入っている）
- [ ] `git status` がクリーン（作業途中の変更と混ぜない）
- [ ] `pyproject.toml` の `[tool.pytest.ini_options]` に `pythonpath = ["."]` が**あるか確認**
      （無いのが想定 → 手順 3-0 で追加。既にあれば 3-0 はスキップ）
- [ ] 現時点のテストが通る: `uv run python -m pytest -q`（ベースライン確認。
      **`python -m` 経由で実行すること** — pythonpath 未整備でも動く形。
      pytest が無い環境なら一時的に `uv sync --extra dev` してから）

---

## 3. 変更手順

### 3-0. pytest の pythonpath 明示（無い場合のみ）

`pyproject.toml` の `[tool.pytest.ini_options]` に1行追加する:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
# ui/ はインストールされないリポジトリローカルのパッケージのため、テストが
# import できるようルートを明示的に sys.path へ足す（python -m の副作用に
# 依存させない: pytest 直接起動や uv run pytest でも動くように）
pythonpath = ["."]
```

### 3-1. pyproject.toml の編集

`[project.optional-dependencies]` から **dev の行だけ**を切り取り、新設する
`[dependency-groups]` セクションへ移す。**リストの中身は現状のまま動かすこと**
（社内側で依存が増えている可能性があるため、この文書の例を上書きコピーしない）。

変更後の形（例。バージョン指定等は現物に従う）:

```toml
[project.optional-dependencies]
# ui: UI 実行サーバ向けの公開 extra。動かさない
ui = ["streamlit>=1.50", "streamlit-sortables>=0.3"]

[dependency-groups]
# 開発ツール一式は extras ではなく dependency-groups(PEP 735)に置く:
# 配布物のメタデータに載らないチーム内の道具箱であり、uv sync が既定で含める
# ため、postCreate(uv sync --frozen)だけで揃い、コンテナ rebuild で消えない。
# pip での代替導入: pip install -e . --group dev (pip 25.1 以降)
dev = [
    # ← 既存の dev リストをそのまま移す
]
```

既存の dev に付いているコメント（ruff/pyright を固定版にする理由など）があれば一緒に移す。

### 3-2. uv.lock の再生成（pyproject と同一コミットに含めること・重要）

```bash
uv lock
```

lock はグループ構成を記録しているため、pyproject だけ変えて lock を変えないと、
postCreate の `--frozen`（lock を検査せずそのまま使う）と次回 rebuild で不整合になる。
**pyproject.toml と uv.lock は必ず同じコミットで動かす**。

### 3-3. .venv を新構成で作り直し

```bash
uv sync          # dev グループは既定で含まれる。--extra は不要
```

このとき extras 時代に入っていた分が一度削除→グループとして入り直す挙動は正常。

### 3-4. リポジトリ内の参照の追随

`.[dev]` / `--extra dev` を参照している箇所を洗い、置き換える:

```bash
grep -rn --exclude-dir=.venv --exclude-dir=.git -e '\[dev\]' -e '--extra dev' .
```

- `pip install -e ".[dev]"` → `pip install -e . --group dev`（pip 25.1+。
  併記するなら「または uv sync」）
- `uv sync --extra dev` / `uv run --extra dev` → `--extra dev` を削除
- `.[ui]` / `--extra ui` は **触らない**（ui extra は存続）
- `.devcontainer/` 配下（submodule）は**編集しない**。変更が要ると思ったら報告

該当が README・docs・CI 設定にあれば同様に追随し、変更ファイルを記録しておく。

---

## 4. 検証手順（元の不具合シナリオの再現確認まで）

1. **lock 整合**: `uv lock --check` がエラーなしで終わる
   （古い uv で --check が無ければ `uv sync --locked`）
2. **テスト**: `uv run pytest -q` — 全件パス（件数をベースラインと比較）。
   **直接起動で通ること自体が 3-0 の検証**（`uv run python -m pytest -q` でも同数になること）
3. **dev グループの実効確認**: `uv run python -c "import streamlit, pytest; print('ok')"`
4. **本丸 = rebuild 再現試験**: VS Code で `Dev Containers: Rebuild Container` を実行し、
   postCreate（uv sync --frozen）完了後に**手作業なしで**:
   - `uv run streamlit --version` が通る
   - `uv run pytest -q` が通る
   これが通れば「rebuild で消える」問題の根治確認完了。

---

## 5. コミット

- 変更対象: `pyproject.toml`（3-0 と 3-1）、`uv.lock`、手順 3-4 で追随したファイルのみ
- 1コミットにまとめる。メッセージ案:

```
dev 依存を dependency-groups へ移行し uv.lock を更新(rebuild 後の uv sync --frozen だけで開発ツールが揃うように。pytest の pythonpath も明示)
```

- 本文（任意）: 「uv sync は extras を含めず既存分も削除するため、extras の dev は
  rebuild のたびに消えていた。PEP 735 の dev グループは uv sync が既定で含める。
  ui extra は UI 実行サーバ向け公開機能のため存置」
- 版数（__version__）は**上げない**: エンジン挙動・設定ファイルの語彙に変更なし、
  開発基盤のみの変更のため（CHANGELOG 運用があるリポジトリなら「未リリース」相当に記録）

---

## 6. 注意点・ロールバック

- **チームメンバーへの周知**: この変更を pull した人は次の `uv sync` で環境が組み替わる
  （extras 時代の残りが削除→グループで入り直し）。挙動としては正常であること、
  以後 `--extra dev` は不要になることを一言伝える
- **ロールバック**: このコミットを revert して `uv sync` し直すだけ。データ破壊性なし
- pyproject に dev 以外の独自 extras やグループが既にある場合は、この指示書の範囲外なので
  構成を変えずに dev だけ移し、判断に迷ったら報告すること

---

## 付録: 自宅側で検証済みの内容（参考）

- 同一の移行を適用し、`uv sync`（--extra なし）のみの環境で全 339 テストがパス
- コンテナ rebuild 後、手作業なしで streamlit が起動することを確認
- CI は `uv sync` + `uv run` + `UV_LOCKED=1`（lock 更新忘れ検出）構成に変更して緑
  （社内はチーム CI 未整備のため該当作業なし。整備時の参考まで）
