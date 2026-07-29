# scorelib_param — スコア計算エンジン (score_gui Phase1 バックエンド)

`docs/score_gui.md` の仕様・`docs/score_gui_design.md` の設計に基づく、スコア／スコアパーツ計算エンジンの実装。
`docs/score_gui_ui_design.md` に基づく Streamlit スコア設計UI（`ui/`）を同梱する。

## ディレクトリ構成

```
scorelib_param/                   # 本体パッケージ
  models.py                 # 設定ファイルのデータモデル（pydantic）
  jsonc.py                  # jsonc（コメント・末尾カンマ付きJSON）の低レベル読み書き
  io_jsonc.py               # jsonc <-> pydanticモデルの変換
  axis_resolve.py           # {type}.csv + parameterLabel/dataName/map系の遅延join
  aggregate.py              # 軸ごとの逐次集計エンジン
  relative.py               # 相対値（分子/分母）計算
  dummy.py                  # ダミー一式のBoard/Chip複製展開・正データの疑似ダミー化
  dvtbudget.py              # dVtBudget変換
  expression.py             # 自由記述式の評価（simpleeval）
  cli.py                    # サブプロセス起動用エントリポイント
  introspect.py             # 過去実験の出力からtype一覧・軸一覧・値候補を導出（UIの情報源。
                            #   streamlit非依存の純粋関数なのでscorelib側に置く）
  batch/                    # 過去実験データのバッチスコア計算（docs/batch_design.md）
    history.py              # result_history の列挙・Step/Loopラベル・Epoch ID
    staging.py              # tar.gz展開ビュー・事前検証・削除（csv.gz単体は直読み）
    compute.py              # Epoch列を通したバッチ一括計算（単一epoch計算と数値等価）
    runner.py               # 取得→計算→削除パイプライン（先行取得・メモリadvisory）
    __main__.py             # python -m scorelib_param.batch
ui/                         # Streamlitスコア設計UI（エンジンとはディレクトリを分離）
  app.py                    # エントリポイント。サイドバーで5画面を切替
  state.py                  # 編集状態の純粋ロジック（雛形生成・検証・下書き保存等。pytest対象）
  widgets.py                # 集計指示エディタ等の画面部品
scripts/
  convert_dvtbudget_coef.py # dVtBudget係数のPythonファイル → jsonc 変換
  make_pseudo_dummy.py      # 正データ→疑似ダミー一式（Board/Chipを1つに削る。開発用）
  benchmark_batch.py        # バッチサイズごとの所要時間・メモリ実測（実運用マシン用）
  batch_bridge_example.py   # 最適化側（py3.7）からバッチCLIを呼ぶブリッジ実装例
  get_score_bridge_example.py # 最適化側 get_score() 用の毎epoch計算ブリッジ実装例
tests/
  conftest.py               # 共通fixture（result_tmp等へのパス）
  fixtures/
    config.jsonc            # テスト用のconfig実例（後述）
    dvtbudget_coef.jsonc    # sample.py から変換した係数ファイル
    B9LS.json               # 世代情報jsonの実例（numWLs等）
    custom_parts.py         # type=custom テスト用の関数ファイル
  test_axis_resolve.py      # 軸解決の正しさ（FBC_expanded.csvとの全行一致）
  test_aggregate.py         # 各集計opの単体テスト
  test_relative.py          # 相対値計算の単体テスト
  test_measure_split.py     # Measure番号/DataNameによる相対化・filterのE2E（新仕様）
  test_dummy.py             # ダミー一式のBoard/Chip展開・疑似ダミー化
  test_agg_weight.py        # 集計時重み（weight/weight_ref）
  test_transform_weights.py # 変換ステップ拡張とPhysical記法グループ定義
  test_dvtbudget.py         # 温度最近傍選択と変換式のテスト
  test_expression.py        # 式評価のテスト（サンドボックス性含む）
  test_jsonc.py             # jsonc読み書き・ラウンドトリップ
  test_combined_axis.py     # 複合軸（State&Read_Label）の等価性
  test_selection_sets.py    # ref参照の解決がインラインと同結果
  test_pipeline_steps.py    # 仮想ステップの配置換えの数学的等価性
  test_prefilter.py         # filter前絞り最適化の判定と同値性
  test_shared_context.py    # 共有キャッシュあり/なしの結果一致
  test_batch.py             # バッチ計算とepoch個別計算の等価性
  test_cli.py               # 実データを使ったエンドツーエンドテスト
  test_introspect.py        # type検出・軸カタログ・値候補の導出
  test_ui_state.py          # UI編集ロジック（雛形が編集なしで計算可能なこと等）
  test_ui_app.py            # Streamlit AppTestによる画面のスモークテスト
pyproject.toml              # パッケージ定義（pip install -e . 用）
.venv/                      # ローカルvenv（Python 3.13 + polars/pydantic/simpleeval/pytest 等）
```

## セットアップ（開発環境）

本書のコマンドはすべて **Ubuntu（bash）表記**。Windows（サブ環境）では
`.venv/bin/` を `.venv/Scripts/` に読み替える。

```bash
git clone <社内GitLabのURL> scorelib && cd scorelib
python3.13 -m venv .venv                        # 3.13 が無ければ miniforge: conda create -n dev python=3.13
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest                      # 264件パスすること
git config core.hooksPath scripts/hooks         # push前テストのフック有効化（clone ごとに1回）
```

※ 開発環境も本番エンジン環境（miniforge）も Python 3.13（2026-07-28 に開発機を
3.11 → 3.13 へ移行済み。CI も本番と同じ 3.13 で検証する）。

## バージョンの上げ方

変更するのは **`scorelib_param/__init__.py` の `__version__` の1行だけ**。

- pyproject.toml は `dynamic = ["version"]` でこの値を参照する（二重管理しない）
- UIサイドバー・CLI（`--version` / stderr のログ）もこの値を表示する
- **版数はエンジン専用**: `scorelib_param/` に変更があるリリースでだけ上げる。
  **ui/ のみの変更では上げない**（UI リリースは番号を持たない `ui-YYYYMMDD` タグ —
  「リリース手順」参照）。理由: 版数は UIサイドバー・CLI stderr・SVN 内コードの
  すべてに表示され、見比べた人（SVN を共同開発する他の開発者含む）が
  「一致=正常、不一致=同期漏れ」とだけ解釈できる状態を保つため。
  UI の都合で上がると「SVN が古く見える」偽の不一致が生まれる
- 上げるタイミング:
  - **エンジンに変更のあるリリース（`ver.X.Y.Z` タグ）を切るとき**。SVN 同期は
    そのタグから行う（同期が後日になってもよい — 番号の差は実際のコード差を
    正しく表す）
  - **設定ファイル（jsonc）の語彙・意味が変わる機能を main に入れた時点**でも上げる
    （新フィールドや新しい値の形は旧エンジンで読み込みエラーになるため、
    「この設定を読めるエンジンか」を版で見分けられるようにする。
    例: 0.5.0 = Measure 相対化・filter リスト・labels 注記の導入、
    0.6.0 = abs/log 変換op・vthSkip ダミー計算の導入）
- 目安: 互換性に影響する変更（パッケージ名・出力契約・configの意味変更）で
  真ん中の数字、それ以外の機能追加・修正は最後の数字を上げる
- **上げ忘れ防止**: 機能実装の完了報告には「版数を上げたか・上げない理由」の判断を
  含める（AI開発時のチェックリストはリポジトリ直下の `AGENTS.md`）

## 開発の進め方（ブランチ・タグ・CI）

各仕組み（フック / .gitattributes / CHANGELOG / GitHub Actions / タグ）が
何者かの解説と FAQ は `docs/dev_workflow.md` を参照。

- **main は常に「テスト全パス・いつでも SVN 同期できる」状態を保つ**（唯一のルール）。
  数日がかりの機能（途中状態が main に乗ると困るもの）は `feature/<名前>` ブランチで
  作業し、テスト全パスを確認してから main へマージする。小さな修正・ドキュメントは
  main 直コミットでよい
- **CI**: push / PR のたびに GitHub Actions（`.github/workflows/test.yml`）が
  本番エンジン環境と同じ Python 3.13 で全テストを実行する。
  **社内 GitLab で運用する場合は `.gitlab-ci.yml`**（同内容の下書き作成済み。
  Runner・イメージ・pip ミラー等の実地確認手順は docs/dev_workflow.md
  「社内 GitLab での CI」を参照）
- **pre-push フック**: push 前にローカルでも全テストが走る。
  clone 後に1回 `git config core.hooksPath scripts/hooks` で有効化する

## リリース手順

**エンジンリリース（SVN 同期）** — `scorelib_param/` に変更があるとき:

1. main でテスト全パス（CI が green であること）
2. `scorelib_param/__init__.py` の `__version__` を上げる（上記の判断基準）
3. `CHANGELOG.md` の「未リリース」をこの版の `ver.X.Y.Z` 節として確定する
4. コミットして **タグを打つ**: `git tag -a ver.X.Y.Z -m "変更の要旨"` →
   `git push --follow-tags`（コミットと注釈付きタグを1コマンドで push。
   PowerShell 5.1 では `&&` が使えないため連結しない）
5. **タグの状態から** `scorelib_param/` + `custom_parts.py` を SVN のスクリプト領域へ
   同期する（タグ = SVN 側で動いている版の git 上の対応点。版ズレ調査・
   ロールバックは `git checkout ver.X.Y.Z`）
6. **UI サーバも同じタグから更新する**（UI はエンジンを同梱しており、放置すると
   UI 同梱エンジンと SVN が実際にズレる。UI に変更が無くても行う）。
   なおエンジンと UI が同時に変わったリリースは `ver.X.Y.Z` 1つでよい
   （UI 変更もその CHANGELOG 節に書く。`ui-*` タグを重ねない）

**UI のみのリリース（UI サーバ更新）** — 変更が ui/ に閉じているとき:

1. main でテスト全パス（CI が green であること）
2. `CHANGELOG.md` に `ui-YYYYMMDD` 節を書く（エンジン変更なしであることを明記）
3. コミットして `git tag -a ui-YYYYMMDD -m "要旨"` → `git push --follow-tags`
4. タグの状態から UI サーバへ配布する（「UI 実行サーバの立て方」。
   取り出すタグは **最新の `ver.*` または `ui-*`**）
5. **`__version__` は上げない**（版数はエンジン専用 — 「バージョンの上げ方」参照）

## 配置まとめ（どこに何を置くか）

3つの設置先で必要なものは異なる。コードの正は git（本リポジトリ）で、
そこから必要な部分だけを各所へ配る。

| 設置先 | 置くもの | インストールするもの |
|---|---|---|
| **開発環境（社内 Ubuntu サーバ）**（コードの正） | リポジトリ全体（社内 GitLab から git clone） | `pip install -e ".[dev]"`（venv） |
| **UI 実行サーバ（Ubuntu）** | **必要4点のみ**: `scorelib_param/` + `ui/` + `custom_parts.py` + `pyproject.toml`（docs/ tests/ 等は置かない — 取り出し方は下記「UI 実行サーバの立て方」） | `pip install -e ".[ui]"` → `streamlit run ui/app.py`（常駐化も下記） |
| **最適化サーバ（SVN kicOpt）** | `scorelib_param/`（パッケージ丸ごと）→ **kicOpt/scorelib_param/**、`custom_parts.py` → **kicOpt/custom_parts.py**、ブリッジ関数 → **kicOpt/optlib/turbo.py** に貼る | エンジン用 python 環境に `polars` `pydantic` `simpleeval` の3つだけ（下記）。**scorelib_param 自体は pip install しない**（ブリッジが PYTHONPATH で解決） |

補足:

- **パッケージは分割しない**: `introspect.py` は「UIの情報源」だが streamlit
  非依存の純粋関数なので意図的にエンジン側に同梱している（本ファイル冒頭の
  ディレクトリ構成の注記参照）。最適化サーバでは使われないだけで、置いて
  あっても害はない。**scorelib_param/ の中から一部ファイルを抜く運用はしない**
  （版ズレ・欠落事故のもと）。
- **SVN に入れないもの**: tests/ docs/ ui/ scripts/ result_tmp 等は git のみ。
  SVN へは scorelib_param/ + custom_parts.py だけを同期し、同期のたびに
  `__version__` を上げる（「バージョンの上げ方」参照）。
  `scripts/benchmark_batch.py` だけは実測に使うなら置いてもよい（任意）。
- `custom_parts.py` の探索位置は「scorelib_param/ の親」= kicOpt/ 直下
  （コードの固定規約。custom パーツ未使用なら無くても動く）。

### UI 実行サーバの立て方（Ubuntu）

方針: **サーバに置くのは必要4点だけ**（docs/ tests/ scripts/ 等のドキュメント・
開発物は持ち込まない）。UIサーバは社内 GitLab に到達できるので、タグから
必要部分だけを取り出して配置する:

```bash
# 配置（初回・更新とも同じ。タグは最新の ver.X.Y.Z または ui-YYYYMMDD）
git clone --depth 1 --branch ver.X.Y.Z <社内GitLabのURL> /tmp/scorelib-src
mkdir -p /opt/scorelib_ui
git -C /tmp/scorelib-src archive HEAD scorelib_param ui custom_parts.py pyproject.toml \
    | tar -x -C /opt/scorelib_ui
rm -rf /tmp/scorelib-src        # clone は一時利用のみ（docs等をサーバに残さない）

# 初回のみ: venv と依存（python3.13 が無ければ miniforge で用意）
cd /opt/scorelib_ui
python3.13 -m venv .venv
.venv/bin/python -m pip install -e ".[ui]"

# 起動（サーバ上にブラウザは無いので headless、他PCから届くよう 0.0.0.0）
.venv/bin/streamlit run ui/app.py \
    --server.address 0.0.0.0 --server.port 8501 --server.headless true
```

利用者は自分の PC のブラウザで `http://<UIサーバ>:8501` を開く。
常駐化する場合は systemd（例）:

```ini
# /etc/systemd/system/scorelib-ui.service
[Unit]
Description=scorelib score design UI
After=network.target

[Service]
WorkingDirectory=/opt/scorelib_ui
ExecStart=/opt/scorelib_ui/.venv/bin/streamlit run ui/app.py --server.address 0.0.0.0 --server.port 8501 --server.headless true
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

（`systemctl enable --now scorelib-ui` で起動・自動起動。
更新時は配置コマンド再実行 → `systemctl restart scorelib-ui`）

**初回に社内で確認が必要なこと**（未確認事項。判明したらここを更新する）:

1. 利用者の PC から UIサーバの 8501 番（または選んだポート）に届くか
   （ファイアウォール・社内NWポリシー）
2. 認証の要否: **Streamlit 自体に認証機能は無い**。社内NW内・認証なしで
   許容されるか。必要ならリバースプロキシ等の別対応になる
3. UIサーバの python3.13 の有無（無ければ miniforge:
   `conda create -n ui python=3.13` → venv の代わりにその環境へ pip install）
4. pip の到達性（開発サーバで普段どおり pip install できているなら
   同じ設定で通る見込み）

Docker 化（イメージに4点+依存を焼き込み、サーバにはイメージだけ置く形）は
上記が動いた後の選択肢: 環境をイメージに固定でき、更新が「イメージ差し替え」の
1操作になる。docker build の可否・base イメージの取得経路（閉域網の場合は
社内レジストリ）の確認が取れたら移行を検討する。

#### 複数ユーザでの利用

Streamlit は**ブラウザのタブごとに独立したセッション**を作るため、複数人が
同時に操作しても編集内容・画面状態が混ざることはない（フレームワークの基本設計）。
共有されるのは以下だけで、それぞれ対処済み・対処方法がある:

- **下書き**: サイドバーの名前ごとに `~/.scorelib_drafts/<名前>.jsonc` へ分離
  （名前未入力の間は自動保存されない）。認証を導入したら、認証ユーザ名を
  ヘッダ（既定 `X-Remote-User`、環境変数 `SCORELIB_UI_USER_HEADER` で変更可）で
  UI へ渡せば名前入力欄は消え、自動でユーザ別になる
- **一時ファイル**: zip 展開・ダミー展開・アップロードは毎回ユニークな
  一時ディレクトリを作るため衝突しないが、削除されず溜まる。サーバでは
  systemd-tmpfiles のルールを1つ置いて自動掃除する（毎日実行・3日より古いものを削除）:

  ```
  # /etc/tmpfiles.d/scorelib-ui.conf
  e /tmp/scorelib_* - - - 3d
  ```

- **同時のテスト計算**: CPU を取り合って遅くなるだけで、正しさには影響しない
  （数人規模なら対策不要）

#### 認証（必要になったら）: nginx リバースプロキシの例

Streamlit 自体に認証機能は無い。認証が必要なら、前段に nginx を置いて
Basic 認証（または社内 SSO）をかけるのが定石で、**アプリ側の変更は不要**。
設定例（要確認事項が解けたら実態に合わせて更新する下書き）:

```nginx
# /etc/nginx/sites-available/scorelib-ui
server {
    listen 80;                       # 社内標準が https ならそれに従う
    server_name <UIサーバのホスト名>;

    auth_basic "scorelib UI";
    auth_basic_user_file /etc/nginx/.htpasswd;   # htpasswd コマンドで作成

    location / {
        proxy_pass http://127.0.0.1:8501;
        proxy_http_version 1.1;
        # Streamlit は WebSocket（常時接続）を使うため、この2行が必須
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        # 認証ユーザ名を UI へ渡す → 下書きのユーザ別分離が自動化される
        proxy_set_header X-Remote-User $remote_user;
        proxy_read_timeout 3600;
    }
}
```

nginx を前段に置いたら、Streamlit 側は外から直接触れないよう
`--server.address 127.0.0.1` に変更する（systemd ユニットの ExecStart）。

### 最適化サーバのエンジン用 python 環境（miniforge）

最適化スクリプト用の既存環境（例: nb37 / python3.7）は**そのまま**にし、
エンジン用の環境を別に作る。subprocess は環境の python バイナリを
**フルパスで直接起動**するため、`conda activate` は不要:

```bash
conda create -n score313 python=3.13 -y
~/miniforge3/envs/score313/bin/python -m pip install "polars>=1.0" "pydantic>=2.0" "simpleeval>=1.0"

# 動作確認（kicOpt に scorelib_param を同期済みの前提）
PYTHONPATH=/path/to/kicOpt ~/miniforge3/envs/score313/bin/python -m scorelib_param.cli --version
```

turbo.py 側はブリッジの `engine_python` にこのフルパスを渡すだけ:

```python
ENGINE_PYTHON = os.path.expanduser("~/miniforge3/envs/score313/bin/python")
```

## 使い方

### CLI（本番の最適化ループから呼ばれる形）

```bash
python -m scorelib_param.cli \
    --config <config.jsonc> \
    --data-dir <そのepochの測定結果ディレクトリ（result_tmp相当）> \
    --dvtbudget-coef <dVtBudget係数.jsonc> \        # dVtBudgetパーツがある場合のみ必須
    --initial-temperature <initial_temperature.csv>  # 同上
    # --generation-info <{Generation}.json>          # 任意（無ければ本数はデータから導出）
    #                                                # （data-dir 内にあれば自動発見されるので省略可）
```

標準出力に1つのJSONオブジェクトを返す:

```json
{"Score": 160.408..., "FBC_A2B_upper1_rel": 1.344..., "dVtBudget_R2A": 159.736...}
```

- `Score`: `expression` の評価値
- それ以外: **定義された全スコアパーツ**の値（constraintThresholdに載っていないものも出力）

現行最適化スクリプト(python3.7)の `get_score()` からは、`score_function` に
`"gui_score"` 等の予約名が指定された場合の分岐としてこのCLIをsubprocess起動し、
標準出力をパースしてDataFrame化する。**そのままコピーして使える Python 3.7
互換のブリッジ実装例**が `scripts/get_score_bridge_example.py` にある
（`compute_epoch_score()` を turbo.py へコピーし、get_score() に数行の分岐を
足すだけ。テストで数値一致を保証済み）。

### Pythonから直接呼ぶ（Streamlit UIのテストボタン等）

```python
from scorelib_param import io_jsonc
from scorelib_param.cli import compute_score_file
from scorelib_param.dvtbudget import load_board_temperatures

config = io_jsonc.load_run_config("config.jsonc")
coef = io_jsonc.load_dvtbudget_coef("dvtbudget_coef.jsonc")
temps = load_board_temperatures("result_tmp/initial_temperature.csv")

result = compute_score_file("result_tmp", config, coef, temps)
# {"Score": ..., "FBC_A2B_upper1_rel": ..., "dVtBudget_R2A": ...}
```

### dVtBudget係数の変換

現行はPythonファイル直書き（`sample.py` の `dVtBudget_coef = {...}`）のため、jsoncへ変換する:

```bash
python scripts/convert_dvtbudget_coef.py sample.py dvtbudget_coef.jsonc
```

## スコア設計UI（Streamlit）

```bash
.venv/bin/streamlit run ui/app.py            # 通常起動（入力はアップロードのみ）
.venv/bin/streamlit run ui/app.py -- --dev   # 開発者モード（サーバ上のパス指定トグルが出る）
# 常駐運用で開発者モードにする場合は環境変数 SCORELIB_UI_DEV=1 でも可
```

**配置方針**（詳細は `docs/score_gui_ui_design.md` 2.1節）: コードの正は git（本リポジトリ、
UI+エンジン一体）。実験実行用に **SVN へは `scorelib_param/` + `custom_parts.py` のみを
リリース時に同期登録**する。一般ユーザ向けの正式な形は「サーバでUIを1つ立てて共用+
一式zipアップロード」（ユーザの環境構築なし）で、個人でのUI起動は開発者向けモード。
UIはエンジンに同梱依存する（検証・軸候補・テスト計算をエンジンのコードそのもので行う
設計のため分割しない）。エンジン版は `scorelib_param.__version__` で管理し、UIサイドバーと
CLI（stderr / `--version`）に表示される — SVN側エンジンとの版ズレ確認用。

サイドバーで切り替える5画面（詳細は `docs/score_gui_ui_design.md`）:

1. **データ読み込み** — 入力は**「① スコア設定」+「② データ」の2段**+読み込み
   ボタン1つ:
   - **① スコア設定**: 既存の設定 jsonc（score.jsonc / optimization設定）を
     アップロードすると、その内容から編集を始める（現在の編集内容は置き換え。
     ↩ アンドゥで戻せる）。未指定なら空から新規作成
   - **② データ**: **実測データ**（GUI からの一式 zip。設定・係数・custom_parts.py
     同梱可・サブディレクトリ探索）/ **ダミー（測定前）**（ダミー一式 zip +
     Board 数・Board ごとの Chip 数で複製展開。テスト計算は構造検証のみ —
     docs/spec_change_dataname_measure.md 9節）/ **なし**（①だけで式・グループ
     定義・パーツを修正してエクスポート）の3択。zip に入っていない係数・
     custom_parts.py は「係数・自作関数を追加する」から個別アップロードできる
   - **一般ユーザの画面はアップロードのみ**（ユーザは UI サーバ上で操作しない
     ため、ローカルパスは原理的に無意味）。**パス指定は開発者モード**
     （`streamlit run ui/app.py -- --dev` または `SCORELIB_UI_DEV=1`）でのみ
     「サーバ上のパスで指定する」トグルが現れ、オンにすると各アップローダが
     パス欄に置き換わる（併記はしない）。優先順位: 個別アップロード > ①の設定 >
     zip 内・パス指定

   共通の仕組み: 同梱ファイルの自動検出は**ファイル名ではなく中身の形**で判別する
   （設定jsonc = `optimization{}` キー、係数jsonc = 「世代→温度→State→{a,b}」の
   3段ネスト。両者は形が排他的。custom_parts.py は固定名。**同じ役割の候補が複数
   あるとエラー** — 黙って1つ選ばない）。値候補は map の全語彙ではなく**実データに
   存在する値だけ**（map順）。WL/STR 等の**軸の本数はデータから導出**され（本数は
   世代で固定・フローは全数を測定するため max+1 が総数）、グループ定義との整合を
   読み込み直後に警告チェックする。世代情報json（`{Generation}.json`）の入力欄は
   無し — ディレクトリ内にあれば自動検出し、データ由来の本数と食い違うときの
   診断警告にだけ使う
2. **スコアパーツ編集** — 「追加」で**そのまま計算が通る雛形**（全軸をデフォルト順に並べ、
   Measure は先頭番号の filter・カテゴリ軸は先頭候補のfilter・数値軸はmean）を生成し、
   差分編集していく。相対化のプリセットは無し（チェックで ON にすると split=Measure・
   分子/分母は候補の位置で初期セットされる）。Measure の値は常に
   **「dataName (Measure N)」の複合表示**で、保存は番号+labels 注記。
   filter は候補のある軸で**複数選択可**（複数 = is_in）。
   order は要約行リスト（✎で選択・上下ボタン・削除）＋選択エントリの常時表示エディタ。
   複合軸の束ね、定数演算ステップ（`__offset__` 等。加減乗除・グループ別重み）の追加、opごとに必要な入力欄だけを
   出す集計エディタ。相対化のON/OFF・split_axis変更時は order との整合を自動で取る
   （OFFにすると split_axis がデフォルトopで order に復帰する）。
   分母の事前集計にも同じ集計エディタをフルで使える。
   編集のたびにエンジンと同一の検証を実行（エラーには**パーツ名**が入り、
   検証NGのパーツは一覧・プルダウンに ⚠ が付く）。
   custom_parts.py を読み込んでいる場合は type に **custom** が並び、
   関数プルダウン+params 行エディタでPython関数パーツを設計できる
3. **選択セット・グループ定義** — ref で使い回す選択リストの作成・編集・**別名で保存**・
   削除（参照中のセットは削除不可）。グループ定義（WLgroup 等の派生軸）の作成・範囲編集・
   Logical/Physical 記法の切り替え・グループ別重みセットの編集もここで行う
   （設定jsoncの WLgroup / WLgroupDefinLogical / WLgroupWeight は読み込み時に
   編集可能な定義として自動取り込み）
4. **スコア合成・制約** — expression の編集（パーツ名クリック挿入・式の即時検証）と
   constraintThreshold の行エディタ（動的制約 active/type/coef 対応）
5. **テスト実行・エクスポート** — 実データディレクトリを指定して `compute_score_file` を
   直接呼び、Score+全パーツ値を表示。`score.jsonc`（selectionSets同梱）や
   パーツ単体（参照セット同梱）のダウンロード、既存jsoncのインポート

編集内容は**操作のたび**に自動保存され、次回アクセス時に復元を提案する
（復元するとデータ読み込みと画面1の入力欄も前回の状態に戻る）。
保存先は**サイドバーで入力した名前ごと**に `~/.scorelib_drafts/<名前>.jsonc`
（共用サーバで複数人の下書きが混ざらないための分離。名前未入力の間は保存されない。
リバースプロキシ認証を導入した場合はヘッダのユーザ名で自動化される — 下記
「複数ユーザでの利用」参照）。
サイドバーの「↩ 元に戻す」で直近20操作までアンドゥできる。

### 測定前設計（ダミー一式の Board/Chip 展開）

測定フロー側は、実データと同形式・**測定値のみダミー**の一式（result_tmp 相当）を
出力できる。ただしフローは Board/Chip を知らないため、ダミーは Board/Chip とも
**1つだけ**で出力される（docs/spec_change_dataname_measure.md 9節）。UI 画面1の
「ダミー一式から設計を始める」で、この実験の Board 数・Board ごとの Chip 数
（`4` = 全Board共通、`4,4,2,2` = Board別）を入力して展開・読み込みすると、
測定前でも通常と同じ操作でスコア設定を書き切れる。テスト計算は構造検証として
使える（**数値は無意味** — 画面にもその旨が表示される）。

ダミー一式が手元に無い開発・検証時は、正データから疑似ダミーを作れる:

```bash
.venv/bin/python scripts/make_pseudo_dummy.py result_tmp dummy_bundle
```

**ドラッグ&ドロップ並べ替え**: `streamlit-sortables` が入っていると（`pip install -e ".[ui]"` で入る）、
パーツ一覧と order の一覧が**常時ドラッグ可能なリスト**になる（モード切替なし。
編集対象の選択はプルダウンで、リスト上では ⠿ 付きの行のうち「← 編集中」の行が編集対象）。コミュニティ製コンポーネントのため soft dependency とし、
未インストール・故障時は自動的に ✎/上下ボタンの行リスト表示になる（アプリ本体は影響を受けない）。

## config.jsonc の書き方

現行のoptimization設定（更新版 `sample.jsonc` の形式）に `score_parts` と `expression` を
追加した形。テストで実際に使用している `tests/fixtures/config.jsonc` が完全な実例。
コードの中身（全ファイル・全関数）の解説は `docs/code_reference.md`、テストの考え方と
各テストの解説は `docs/testing_guide.md` を参照。

```jsonc
{
    "Generation": "B9LS",              // チップ世代（dVtBudget係数の選択に使用）
    "optimization": {
        "score_function": "gui_score", // 予約名。get_score()がこのエンジンに分岐する目印
        "constraintThreshold": {       // 既存フォーマットそのまま。キーはスコアパーツ名
            "FBC_A2B_upper1_rel": {"value": 25},
            "dVtBudget_R2A": {"value": 10, "active": "True", "type": "percentile", "coef": 20}
        },
        "WLgroup": {                   // 既存フォーマットそのまま。[min, max]（両端含む）
            "WLgroup01": [0, 3],
            "WLgroup02": [4, 8]
        },
        "WLgroupDefinLogical": "True", // 既存キー。"False"なら上の範囲はPhysical番号
                                       // （計算時にLogicalへ反転変換。総数Nはデータから自動導出）
        "WLgroupWeight": {             // 既存キー。グループ別重み（重みセット"WLgroupWeight"になる）
            "WLgroup01": 1.0,          // 数値1つ（全グループ共通）でもよい。
            "WLgroup02": 10.0          // パーツの__weight__ステップからrefで参照（後述）
        },
        "score_parts": [ /* 後述 */ ],
        "expression": "0.5 * FBC_A2B_upper1_rel + dVtBudget_R2A"
    }
}
```

### スコアパーツの定義

```jsonc
{
    "name": "FBC_A2B_upper1_rel",   // パーツ名。expressionやconstraintThresholdから参照
    "type": "FBC",                  // 読むcsv。"dVtBudget"のときはFBC.csvを読み自動で変換
    "relative": {                   // 相対値化。しない場合は省略
        "split_axis": "Measure",         // この軸の値で分子/分母を分ける（基本は Measure 番号）
        "numerator_when": 1,             // Measure 1 の行が分子（評価測定 = 提案パラ）
        "denominator_when": 0,           // Measure 0 の行が分母（基準測定）
        "labels": {                      // 任意: 番号の意味の注記（表示・検証用。実行には不使用）
            "1": "evaluation_param_read_level_1",
            "0": "reference_param_read_level_1"
        },
        "denominator_offset": 1,         // (分子+offset)/(分母+offset)。両方に加算【確認済み】
        "denominator_pre_aggregation": [ // 比を取る前に分母だけ先に集計（省略可）
            {"axis": "WL", "op": "mean"},
            {"axis": "STR", "op": "mean"}
        ]
    },
    "order": ["Read_Label", "State", "WL", "WLgroup", "STR", "Board", "Chip", "Block"],
    "aggregations": {
        "Read_Label": {"op": "filter", "value": "read_level_upper1"},
        "State":      {"op": "filter", "value": "A2B"},
        "WL":         {"op": "mean"},   // WLgroup列があるのでグループ内平均になる
        "WLgroup":    {"op": "max"},    // グループ間はmax（派生軸。下記グループ定義参照）
        "STR":        {"op": "mean", "value": [0, 1]},
        "Board":      {"op": "mean"},
        "Chip":       {"op": "mean"},
        "Block":      {"op": "max"}
    }
}
```

- `order` に列挙した軸を**この順番で**1つずつ集計して潰していき、全軸を潰し切ると
  パーツの値が1スカラーに定まる。潰し残しがあるとエラーになる（テスト機能を兼ねる）。
- InBatchEpochは実質未使用（常に0）のため通常orderに含めなくてよい。

#### グループ定義（groupDefs）— 軸のグループ分割派生軸

`groupDefs` に「名前 + 対象軸 + グループ名→[min, max] 範囲」を定義すると、
その名前を order に**普通の軸として**置ける（値候補はグループ名）。グループ列は
データ読み込み直後に作られるため、集計のタイミングを自由に選べる — 例えば
「WLgroup分割 → WL,STR平均 → Board max → **最後に** WLgroup max」が書ける:

```jsonc
"groupDefs": {
    "WLgroup":  { "axis": "WL",  "groups": { "WLgroup01": [0, 3], "WLgroup02": [4, 8] } },
    "STRgroup": { "axis": "STR", "groups": { "even": [0, 1], "odd": [2, 3] } }
}
```

- 従来の `optimization.WLgroup` は「WL に対する WLgroup 定義」として互換読み込みされる
  （`groupDefs` に同名があればそちらが優先）。
- **Logical / Physical 記法**: 範囲は既定では Logical 番号（csv の WL 列の値そのもの）。
  現行スクリプトの `WLgroupDefinLogical: "False"` 相当で **Physical 番号**で書きたい場合は、
  `optimization.WLgroupDefinLogical` を `False` に（groupDefs 側は各定義の
  `"definedInLogical": false`）。計算時に軸の総数 N を使って `[lo, hi]` → `[N-1-hi, N-1-lo]`
  へ読み替える。N は **測定csvから自動導出**される（max+1。本数は世代で固定・
  フローは全数を測定するため正確）。`{Generation}.json`（`numWLs` / `numStrings`）が
  データディレクトリ内にある・または CLI の `--generation-info` で指定された場合は
  そちらを優先する（互換動作）。どちらも無くても Physical 記法は使える。
- 定義名は対象軸名と同名にできない。定義名を order に置いていないパーツでは列は作られず、
  対象軸は通常どおり扱われる。
- パーツが定義名を参照していると、そのパーツ内ではグループ列が最初から存在する扱いになる
  （relative の分母事前集計などでもグループをまたいで混ざらない。またぎたい場合は
  そのステップにグループ軸自体を追加する）。
- 旧 `group_reduce` op は廃止（読み込み時に移行案内つきエラー）。inner/outer は
  「対象軸の集計の直後にグループ軸を置く」ことで等価に書ける。
- **範囲チェック**: どの範囲にも入らない値の行がデータにあると、値の一覧つきで
  計算エラーになる（名無しグループとして静かに混ざることはない）。逆に、データに
  該当値が無いグループは「存在しない軸の値」と同じ扱いで、単に現れないだけ。
  さらに UI ではデータ由来の軸本数と照合し、定義の範囲が本数を
  超えていたり、0〜本数-1 に未カバーの値があると事前に警告する。

#### 自作Python関数パーツ（type="custom"）

集計パイプラインで表現できない複雑な計算（複数csvの突き合わせ等）は、
**リポジトリ直下の `custom_parts.py`** に関数を書いて type="custom" のパーツとして呼べる:

```jsonc
{"name": "my_score", "type": "custom"}                        // name と同名の関数を呼ぶ
{"name": "s2", "type": "custom", "function": "my_score",
 "params": {"threshold": 3}}                                  // 関数名を明示・params も渡せる
```

```python
# custom_parts.py（リポジトリ直下、SVN管理）
def my_score(ctx):
    df = pl.read_csv(ctx.data_dir / "FBC.csv")   # ctx: data_dir / generation / group_defs / params
    return float(df["FBC"].mean())               # 1つの有限な数値を返す（エンジンが検証）
```

- 関数ファイルの場所は**固定**（`--custom-parts` はテスト用の上書き）。config にパスを
  書く形にはしない — 実験入力から任意コードを実行できてしまうため、関数の追加・変更は
  SVN コミット（レビュー）を通す設計
- custom パーツは order / aggregations / relative を持たない（混在は読み込みエラー）。
  expression や constraintThreshold からは通常パーツと同じに参照できる
- 設計UI用には、GUI からダウンロードする一式zipに custom_parts.py を同梱する
  （UIは同じ内容のファイルから関数一覧を出す。実行側はリポジトリ内のファイルを読むため、
  リビジョンが一致していれば設計時と同じ関数が走る）

#### relative の各フィールド

毎epochの測定には基準パラの測定と提案パラの測定が混在しており、それを見分けて比を取る。

`relative` ブロックが**書いてあれば相対化する**。絶対値のまま使いたい場合は
ブロックごと省略（またはコメントアウト）する。`enabled` フラグは無い
（旧ファイルの `enabled: true` は無視され、`enabled: false` は明確なエラーになる）。

| フィールド | 意味 |
|---|---|
| `split_axis` | 分子/分母を見分ける軸。**基本は `Measure`（測定番号）**。任意の軸を指定でき、旧仕様の `Read_Override` 等や、Measure 列の無い集計済み type での `Chip` 等も可（docs/spec_change_dataname_measure.md） |
| `numerator_when` | split_axisがこの値の行が分子（評価測定 = 提案パラ）。例: `1` |
| `denominator_when` | split_axisがこの値の行が分母（基準測定）。例: `0` |
| `labels` | 任意: 値 → 表示名（dataName 等）の注記。**実行には使われない**（UI 表示と将来の validate 照合用） |
| `mode` | `"ratio"`（デフォルト）: 比 `(分子+o)/(分母+o)` / `"diff"`: **delta値** `分子 - 分母` |
| `denominator_offset` | ratio時に**分子分母両方**に加算。ゼロ割・log発散防止。diff時は差で相殺されるため無視される |
| `denominator_pre_aggregation` | 比/差を取る前に**分母だけ**先に集計する指示のリスト（例: WL,STRを平均した値を分母にする） |

OverrideのTrueからFalseを引いたdelta値を取りたい場合は `"mode": "diff"` を指定する
（ペア照合の仕組みはratioと同一で、演算だけが引き算になる）。

分子行と分母行は、その時点で残っている全軸の値が一致するもの同士でペアになる。
`denominator_pre_aggregation` で分母側の軸を潰した場合は、残った軸で照合され
分母値が分子側にブロードキャストされる。

**Measure を order の軸に使う場合の注意**: Measure 軸と「Measure 以外の軸での相対化」は
併用できない。ペア照合キーに Measure が残り、分子（評価測定の番号）と分母（基準測定の
番号）で値が必ず異なるため0ペアになる。Measure で測定を選ぶなら分割も Measure で行う
（新仕様の基本形）。Label/Override 軸は Measure 番号が一意に決める測定メタデータなので、
Measure と並べて order に置く必要はない（UI の雛形にも入らない）。

#### パイプラインステップ（orderへの処理の組み込み）

`order` には軸名のほかに `__xxx__` 形式の**仮想ステップ**を置け、
相対化・dVtBudget変換・オフセット加算などの処理を任意の位置に挿入できる:

| ステップ | 意味 | 省略時のデフォルト位置 |
|---|---|---|
| `"__relative__"` | この位置で相対化 | 全集計より前（先頭） |
| `"__dvtbudget__"` | この位置でdVtBudget変換（type=dVtBudgetのみ） | `__relative__` の直後 |
| その他の `"__名前__"` | 値列への行単位変換。aggregationsに同名のエントリで内容を指定 | （明示したときのみ実行） |

現在使える変換op: `add` / `sub` / `mul` / `div`（値列に定数を 加算/減算/乗算/除算）。
仮想ステップは名前を変えれば**何個でも**置ける（例: `__offset__` で+1 →
相対化 → `__flip__` で×-1）。

`value` の形は2通り:

- **数値**: 全行に同じ定数。例: 正負反転 `{"op": "mul", "value": -1}`
- **`by` + 辞書**: 指定した軸の**値ごと**の定数。グループ別の重みがこれ:

```jsonc
"order": [..., "WL", "__weight__", "WLgroup", ...],
"aggregations": {
    "WL": {"op": "mean"},
    // WL平均の直後・WLgroup集計の前に、グループごとの重みを掛ける
    "__weight__": {"op": "mul", "by": "WLgroup", "value": {"WLgroup01": 1.0, "WLgroup02": 10.0}},
    "WLgroup": {"op": "max"}
}
```

- `by` の軸がまだ列として残っている位置にステップを置くこと（潰した後だとエラー）。
  重みを掛けるタイミングは order 上の位置で自由に選べる
- 辞書に無い値の行がデータにあると、値の一覧つきで計算エラーになる（重み定義の
  古さの検出）
- インライン辞書の代わりに `"ref": "WLgroupWeight"` で**名前付き重みセット**を参照できる。
  重みセットは `optimization.WLgroupWeight`（現行キー互換。辞書または数値1つ）か
  `optimization.weightSets`（任意個）で定義する。数値1つの重みセットは全行共通の定数として働く

```jsonc
"WLgroupWeight": { "WLgroup01": 1.0, "WLgroup02": 10.0 },   // または "WLgroupWeight": 2.0（全グループ共通）
...
"__weight__": {"op": "mul", "by": "WLgroup", "ref": "WLgroupWeight"}
```

**もっと簡単な書き方（推奨）— 集計時重み**: 「その軸を潰すときに重みを掛ける」だけなら、
変換ステップを置かずに集計指示へ直接 `weight`（辞書 or 数値1つ）/ `weight_ref`（重みセット参照）を書ける:

```jsonc
"WLgroup": {"op": "max", "weight_ref": "WLgroupWeight"}
// または重みを直接: {"op": "max", "weight": {"WLgroup01": 1.0, "WLgroup02": 10.0}}
```

意味は「その軸を潰す**直前**に、軸の値ごとの重みを値へ**乗じてから**集計」
（正規化された加重平均**ではない**: mean なら mean(重み×値)）。掛けるタイミングが
結果に影響しない通常のケースはこれで十分で、タイミングが意味を持つ場合
（dVtBudget変換の前に掛けたい等）だけ上の `__weight__` ステップを使う。
両方が適用可能な場面では結果は同一になる。

例:「オフセットを足す → WLで平均 → 相対化 → dVtBudget変換 → 残りを集計」という流れ:

```jsonc
{
    "name": "dVtBudget_custom_flow",
    "type": "dVtBudget",
    "relative": {
        "split_axis": "Read_Override",
        "numerator_when": true,
        "denominator_when": false,
        "denominator_offset": 0          // offsetは下の__offset__ステップで明示的に足すので0
    },
    "order": [
        "Read_Label",                     // filter（相対化前: 分子側/分母側それぞれに効く）
        "__offset__",                     // 全行のFBCに+1
        "WL",                             // WL平均（分子側・分母側それぞれで集計される）
        "__relative__",                   // ここで比を取る
        "__dvtbudget__",                  // ここで -log10(rel)/b*1000 変換
        "State", "STR", "Board", "Chip", "Block"
    ],
    "aggregations": {
        "__offset__": {"op": "add", "value": 1},
        "Read_Label": {"op": "filter", "value": "read_level_upper1"},
        "WL": {"op": "mean"},
        "State": {"op": "filter", "value": "A2B"},
        "STR": {"op": "mean"},
        "Board": {"op": "mean"},
        "Chip": {"op": "mean"},
        "Block": {"op": "mean"}
    }
}
```

注意点:
- `__relative__` より前に置いた軸集計は、分子グループ・分母グループ**それぞれの中で**
  実行される（split_axis が残っているため自然にそうなる）。
- `__dvtbudget__` の時点で **Board と State が軸として残っている**必要がある
  （係数の解決にBoard別温度とStateを使うため）。State のfilterは `__dvtbudget__` の
  後に置くこと。順序が悪い場合は明確なエラーメッセージで停止する。
- 平均のような線形集計とoffsetは順序を入れ替えても結果が変わらないが（テストで
  等価性を確認済み）、max/min等の非線形集計を挟む場合は順序で結果が変わるので、
  現行スクリプトの計算順に合わせて配置すること。

### 集計op一覧

**選ぶものはopに関わらず常に `value` に書く**。スカラーなら選択1個、リストなら選択の並び、
複合軸では軸名つき辞書が選択1個。opごとに違うのは「選択が何個必要か」だけで、
個数・形が合わない場合は読み込み時に正しい書き方を提示するエラーで止まる
（例: sumに `value` 単数のスカラーを書いた場合は1個のリストとして解釈、
diffに1個しか書かなければ「2個必要」とエラー）。
旧表記の `values` はエイリアスとして読み込み時に `value` へ自動変換される。

| op | 意味 | valueに書くもの |
|---|---|---|
| `filter` | 指定値の行だけ残す。リストは is_in（該当行を全部残し、後段の集計に複製として流す） | 選択1個以上 |
| `mean` / `sum` / `min` / `max` | 集計。`value` を付けると対象をその選択集合に限定 | なし or 選択のリスト |
| `diff` | 2つの選択の差で潰す: a − b | 選択ちょうど2個のリスト |
| `expr` | 自由記述式。全値のリスト `values` と、軸の値ごとの辞書 `by` が使える | なし（`expr`） |

グループ分割集計は op ではなく **groupDefs の派生軸**で表現する（前節）。

軸の値同士を組み合わせる例（Stateの集計指示として）:

```jsonc
"State": {"op": "filter", "value": "R2A"}                        // 通常: 1つのStateを選ぶ
"State": {"op": "diff", "value": ["R2A", "B2A"]}                 // R2A - B2A の差
"State": {"op": "sum", "value": ["R2A", "B2A"]}                  // R2A + B2A の和
"State": {"op": "expr", "expr": "0.5*by['R2A'] + 0.5*by['A2B']"} // 任意の重み付き合成
"Measure": {"op": "filter", "value": [3, 4, 5]}                  // is_in: 複数測定の行を残す
                                                                 //（同じdataNameのループ測定等）
```

dVtBudgetパーツでは変換がState集計より前に走るため、この書き方で
「あるStateのdVtBudgetと別のStateのdVtBudgetの和・差」をそのまま表現できる。

#### 複合軸（State と Read_Label の組で選ぶ）

「上方向のState（R2A, A2B）は read_level_upper1、下方向のState（A2R, B2A）は
read_level_lower1 で見て、それらを合成したい」のように、**複数の軸の組**に対して
選択・集計したい場合は、orderのエントリを `&` で束ねた**複合軸**にする。
束ねた軸は1つの軸として振る舞い、選択は**軸名つき辞書**で指定する
（位置指定のリスト `["R2A", "upper1"]` は「1つの組か複数の選択か」が
曖昧になるため使えない。書いた場合は辞書形式を促すエラーになる）:

```jsonc
{
    "name": "dVtBudget_updown_sum",
    "type": "dVtBudget",
    "relative": { ... },
    "order": ["State&Read_Label", "WL", "STR", "Board", "Chip", "Block"],
    "aggregations": {
        "State&Read_Label": {
            "op": "sum",
            "value": [
                {"State": "R2A", "Read_Label": "read_level_upper1"},   // 上方向はupper1で
                {"State": "A2B", "Read_Label": "read_level_upper1"},
                {"State": "A2R", "Read_Label": "read_level_lower1"},   // 下方向はlower1で
                {"State": "B2A", "Read_Label": "read_level_lower1"}
            ]
        },
        "WL": {"op": "mean"}, "STR": {"op": "mean"},
        "Board": {"op": "mean"}, "Chip": {"op": "mean"}, "Block": {"op": "mean"}
    }
}
```

- `filter`（辞書1個）、`diff`（辞書2個の差。異なるRead_Label同士でも可）、
  `sum`/`mean`/`min`/`max`（辞書のリスト）がそのまま使える。
- 辞書のキー名は複合軸名の構成軸と一致している必要があり、違うと読み込み時エラーになる。

#### 選択セット（selectionSets）— 選択リストの名前付き再利用

同じ選択リスト（上下方向の組など）を複数のパーツで使い回す場合は、
コピペせず **`optimization.selectionSets` に名前付きで定義して `ref` で参照**する:

```jsonc
"optimization": {
    "selectionSets": {
        "updown_pairs": [
            {"State": "R2A", "Read_Label": "read_level_upper1"},
            {"State": "A2B", "Read_Label": "read_level_upper1"},
            {"State": "A2R", "Read_Label": "read_level_lower1"},
            {"State": "B2A", "Read_Label": "read_level_lower1"}
        ],
        "upper_states": ["R2A", "A2B"]          // 複合軸専用ではなく通常軸のリストにも使える
    },
    "score_parts": [
        { ...,
          "aggregations": {
              "State&Read_Label": {"op": "sum", "ref": "updown_pairs"},   // valueの代わりにref
              ...
          }
        }
    ]
}
```

- `ref` は `value` の代わりに書く（両方書いたらエラー、存在しない名前もエラー）
- 参照は計算前に展開され、展開後の中身はインラインで書いた場合と**全く同じ検証**を通る
- セットの定義を直せば、参照している全パーツに一括で反映される（コピペ方式との違い）
- エクスポートされるスコアファイル（ScoreFile）には `selectionSets` が同梱されるため、
  `ref` を使うパーツを単体で持ち出しても自己完結する
- UI実装時の想定: パーツ編集画面では定義済みセットのプルダウン＋新規作成。
  セット編集画面では「別名で保存」（既存セットを複製してから編集）も提供する。
  セットはただの名前付きリストなので複製は自明で、別名保存しても既存パーツの
  参照は元の名前のまま変わらない
- 複合軸に含めた軸（上の例ではStateとRead_Label）は、orderに単独で
  重ねて書かない（複合軸で一緒に潰れるため）。
- 制約条件（constraintThreshold）はスコアパーツ名を参照するため、
  このように1パーツで書けることで上下方向合成値をそのまま制約に使える。
- 注意: 軸の値に `&` を含む文字列がある場合はこの記法は使えない（現状の
  State/Read_Label等の値には含まれないため実用上問題ない）。

### expression（スコア合成式）

スコアパーツ名を変数として参照する。使える関数:
`log`(=log10), `ln`, `log2`, `exp`, `sqrt`, `min`, `max`, `mean`, `sum`, `abs`。
simpleevalによるサンドボックス評価のため、任意のPythonコードは実行できない。

### dVtBudgetパーツ

`"type": "dVtBudget"` とすると、FBC.csvを読んで相対値化した後、自動で

```
dVtBudget = -log10(相対FBC) / b * 1000
```

を行ごとに適用してから `order` の集計に入る。`b` は
「configの `Generation`」×「`initial_temperature.csv` のBoard別実測温度に最も近い温度キー」
×「State」で係数ファイルから解決される（GUI側で世代や温度を選ぶ必要はない）。
変換時点でBoard・State列が必要なため、orderで `"__relative__"` を使う場合は
Board/Stateを相対化より後に集計すること。

## テスト

```bash
.venv/bin/python -m pytest tests/ -q     # 264件、全パス
```

### 何をどう検証しているか

- **軸解決の正しさ** (`test_axis_resolve.py`):
  `result_tmp` の実データ（FBC.csv 80,640行 + parameterLabel/dataName/map系）を
  本エンジンで解決した結果が、現行ロジック（`reference_scripts/expand_FBC_measure.py`）の出力である
  `reference_scripts/FBC_expanded.csv` と**全行一致**することを確認。展開せず遅延joinする新方式が
  現行の展開方式と同じ結果を返すことの保証。
- **各集計opの単体テスト** (`test_aggregate.py`): 手計算で答えの分かる小さなデータで
  filter/mean/subset/expr/グループ派生軸を検証。orderが全軸を潰し切らない場合の
  エラーも確認。
- **相対値** (`test_relative.py`): 分母の事前集計（WL→STRの順のmean）とoffsetが
  設計通りに効くことを手計算値と照合。
- **Measure番号/DataName指定** (`test_measure_split.py`): 新仕様（Measure 番号での
  相対化・filter）が旧仕様（Read_Label filter + Read_Override 分割）と厳密同値で
  あることを mini データで照合。is_in filter の前絞り最適化・キャッシュ安全性も確認。
- **ダミー展開** (`test_dummy.py`): Board/Chip 複製展開が行の複製「だけ」を行うことを
  「mean 集計は複製に対して不変」という性質（展開前後で同値）で検証。
- **dVtBudget** (`test_dvtbudget.py`): Board 0(-28.236℃)→係数キー"-30"、
  Board 1(82.934℃)→"85" の最近傍選択と、変換式の値を手計算と照合。
- **エンドツーエンド** (`test_cli.py`): `tests/fixtures/config.jsonc`
  （上記「config.jsoncの書き方」に載せた2パーツ+合成式の実例そのもの）を使い、
  `result_tmp` の実データに対して:
  1. FBCパーツの値が、テスト内で独立に書いた素朴なeager polars実装
     （FBC_expanded.csvから一歩ずつ集計）の結果と一致すること
  2. dVtBudgetパーツが有限値を返すこと
  3. `compute_score_file` が全パーツ+Scoreを返し、Scoreがexpressionの評価値と
     一致すること
  4. CLIをsubprocessとして起動してもJSONが正しく返ること（本番の呼ばれ方の再現）

### テストで発見・修正した問題

- 分子（提案パラ）のFBCが0のとき相対値が0となりdVtBudgetのlog10が-infに発散
  → offsetを分子・分母両方に加算する形に修正（**その後、この形で正しいと確認済み**）。
- map_Override.csvのTRUE/FALSE列がpolarsのCSV読み込みで自動的にBoolean型になるケースの対応。

## 計算エンジンの内部最適化（configの書き方は不変）

`compute_score_file`（CLI経由の計算）は以下の共有キャッシュを自動で使う。
**設定ファイルの書き方・計算結果は一切変わらない**（等価性はテストで検証済み。
129万行×15パーツの実測で 3.0s → 0.20s、約15倍）。

- **type単位の共有読み込み**: 同じ `{type}.csv` を使う全パーツの必要軸の和集合で
  1回だけ読み込み・join し、各パーツには自分の必要列だけを射影して渡す
  （射影により相対化のペア照合・集計のgroup keyは個別読み込み時と完全に同一になる）。
- **前段キャッシュ**: `__relative__` / `__dvtbudget__` 直後の中間結果を
  「type・必要軸集合・そこまでの全ステップ内容」をキーにキャッシュ。
  Stateフィルタだけが違うdVtBudgetパーツ群などは前段を1回だけ計算し、
  後段のフィルタ+集計（数ms）だけが各パーツで走る。設定が1つでも違う
  パーツ同士は共有されない（速度が落ちるだけで結果は常に正しい）。

キャッシュの寿命は1回の計算実行内のみで、epoch間で持ち越さない。
`compute_score_part` を単体で呼んだ場合（shared_ctx未指定）は従来通り毎回読み込む。

集計の最終収束は「識別軸（例: Epoch）を残して潰す」形に一般化されており
（`aggregate.collapse`）、これを使った**複数epochバッチ計算**が
`scorelib_param.batch` として実装済み（次節）。通常の単一epoch計算は
識別軸なし＝1スカラーで従来と同じ動作。

## 過去実験データのバッチスコア計算（scorelib_param.batch）

ベイズ最適化の初期モデル構築用に、過去実験の result_history 群
（`<実験ログ>/Step{N}/Loop{NN}/result_history/result.{NNNN}/` = 1 epoch）を
バッチ単位でまとめてスコア計算する。設計は `docs/batch_design.md`。

```bash
python -m scorelib_param.batch \
    --config config.jsonc \
    --history /data/expA/Step1/Loop01/result_history \
    --history expB=/data/expB/Step2/Loop03/result_history \   # label=path でラベル明示も可
    --dvtbudget-coef dvtbudget_coef.jsonc \                   # dVtBudgetパーツがある場合のみ
    --out scores.csv \
    [--batch-size 50 | --batch-size auto] [--max-prefetch 2] \
    [--staging-dir DIR] [--strict] [--keep-staging] [--max-threads N] \
    [--generation-info {Generation}.json]   # 任意（無ければ本数はデータから導出）
```

出力 CSV は 1 epoch = 1 行:
`Epoch`（一意ID: `expA/Step1/Loop01#0001`）, `History`, `EpochNo`, `Score`, 全パーツ値。
除外 epoch は stderr と `<out>.failed.csv` に理由つきで報告される
（既定は skip-and-report。`--strict` で最初の不良で停止）。

Python から:

```python
from scorelib_param.batch import BatchRunner
runner = BatchRunner([hist_path1, hist_path2], run_config, dvtbudget_coef=coef)
result = runner.run()        # result.scores: DataFrame / result.failed: {Epoch: 理由}
for batch in runner.run_iter():  # バッチごとに逐次受け取る場合
    ...
```

ポイント:

- **単一epoch計算と数値等価**（`tests/test_batch.py` の等価性テストで保証）。
  仕組みは「Epoch 列を1本通すだけ」— エンジンの「グループキー＝残っている
  全列」の性質により、全集計・相対化ペア照合・グループ派生軸が自動的に
  epoch 単位に分かれる。dVtBudget の係数は epoch ごとの
  `initial_temperature.csv` から epoch 別に解決される。
- `--initial-temperature` 指定は不要（各 result.NNNN 内のものを読む）。
- **圧縮対応**: csv.gz 単体は polars が直読み（解凍なし）。tar.gz / zip
  アーカイブのみステージング領域に展開し、計算後に削除する。入力元は
  一切変更・削除しない。
- **パイプライン**: 計算中に次の最大 `--max-prefetch` バッチを裏で取得。
  ディスク使用は「(1+prefetch)×1バッチ分」が上限。データ取得手段
  （scp 等）は `Fetcher` callable の差し替えで追加できる（当面はローカル/
  共有マウントの pass-through）。
- **メモリ**: `--batch-size auto` で利用可能メモリ（Linux は /proc/meminfo）
  と最初の epoch の実測から自動選択。数値指定時も過大/過小の助言を stderr
  に出す（実行はブロックしない）。バッチサイズは主に**ピークメモリ**を
  決め、所要時間はほぼ変わらない（実測: 129万行/epoch × 50 epoch × 15
  パーツで、batch_size 50/25/10 いずれも 10.5s。ピークは 15 / 8.1 / 3.7 GiB）。
- **CPU**: 計算中は polars が全コアを使う（CPU 100% は正常動作）。マシンを
  他の作業と共有する場合は `--max-threads N` で計算スレッド数を制限できる
  （メモリと違い、CPU はバッチサイズでは制御しない）。
- **実測ツール**: `python scripts/benchmark_batch.py --config ... --history ...
  --batch-sizes auto,10,25,50` で、実運用マシンでのバッチサイズごとの
  所要時間・ピークメモリの表を出せる（計測ごとに別プロセスで実行）。

### 最適化スクリプト（python3.7）からの呼び出し

通常の gui_score CLI と同じく **subprocess 起動**方式。エンジンは
Python 3.10+ で動くため、最適化スクリプト自身の python ではなく
scorelib_param 用の python 実行ファイルを指定して起動し、`--out` の CSV を
読み取る。**そのままコピーして使える Python 3.7 互換のブリッジ実装例**が
`scripts/batch_bridge_example.py` にある（テストで動作保証):

```python
scores, failed = compute_batch_scores(
    engine_python="/opt/py311/bin/python",  # scorelib_param が入っている python
    config=self.config,                      # 読み込み済み dict でもパスでも可
    histories=[".../Step1/Loop01/result_history", ...],
    out_csv="/tmp/past_scores.csv",
    dvtbudget_coef="dvtbudget_coef.jsonc",
    # scorelib_parent は省略可: 関数を kicOpt/ 内のスクリプトに貼れば
    # kicOpt/（scorelib_param/ が並ぶ場所）が自動で使われる
)   # scores: epoch ごとの dict のリスト / failed: {Epoch: 除外理由}
```

エンジンの進捗・警告は `<out_csv>.log` に保存され、失敗時は log 末尾つきの
RuntimeError になる。なお初期モデル構築は実験開始時の一度きりの前処理
なので、自動連携せず **事前に CLI を手で実行して scores.csv だけ渡す**
運用でもよい（どちらでも結果は同じ）。
- custom パーツはバッチ化されず epoch ごとに関数が呼ばれる（結果は同じ。
  遅くなるのは custom パーツのみ）。
- 予約名: 識別軸 `Epoch`。同名の軸・グループ定義があるとエラー。

## 現行スクリプトとの数値比較手順（tests/data/result_tmp_mini）

現行スクリプトの計算結果と数値一致を確認するための最小データが `tests/data/result_tmp_mini/` にある
（FBC.csv 1152行 + tR.csv 432行。tRはState軸の代わりに**Page軸**を持ち、`map_Page.csv` で
L/M/U に解決される。map系ファイルは `map_{軸名}.csv` の命名規則により自動発見されるため、
FBCに無い軸でもエンジン側の変更なしで扱える）。

そのまま実行できるスコア設計例として `config_mini.jsonc` をリポジトリ直下に用意した:

```bash
.venv/bin/python -m scorelib_param.cli \
    --config config_mini.jsonc \
    --data-dir tests/data/result_tmp_mini \
    --dvtbudget-coef dvtbudget_coef.jsonc \
    --initial-temperature tests/data/result_tmp_mini/initial_temperature.csv
```

出力例:

```json
{"Score": 2.6499..., "FBC_upper1_A2B_rel": 1.0246..., "FBC_upper1_A2B_worstWLg": 2.8507...,
 "tR_upper1_U_rel": 1.9990..., "dVtBudget_A2B": 1.3518...}
```

`config_mini.jsonc` には4パーツ（FBC相対値の全平均 / WLgroup worst / tRのPage=U抽出 /
dVtBudget）を定義してあり、コメント付きなので、現行スクリプトが計算しているスコアに
合わせて `score_parts` の filter 値・集計op・order を書き換えて比較する。
パーツを減らしたい場合は `score_parts` から削除し、`expression` から参照を外すだけでよい
（dVtBudgetパーツを消せば `--dvtbudget-coef` / `--initial-temperature` も不要になる）。

### 比較時の注意

- **offset**: 相対値は `(分子+offset)/(分母+offset)`。現行スクリプト側も同じoffset値
  （config上の `denominator_offset`）を使っているか確認すること。
- **温度の最近傍選択**: `tests/data/result_tmp_mini/initial_temperature.csv` は 25℃ / 30.83℃ だが、
  係数キーが -30 / 85 の場合、25℃→**-30**（|25-(-30)|=55 < |25-85|=60）、
  30.83℃→**85** に解決される。室温近辺のBoard同士でも異なる係数が選ばれるため、
  現行スクリプトの温度→係数の選択ルールが「最近傍」でない場合は数値がズレる。
- **dVtBudget係数**: `dvtbudget_coef.jsonc`（リポジトリ直下）は
  `tests/fixtures/dvtbudget_coef.jsonc`（実係数に更新済みのもの）と同内容にしてある。
  係数を変えたら両方更新するか、`--dvtbudget-coef` でどちらか一方を指すこと。

## 未確定事項（実装は差し替え可能な形にしてある）

`docs/score_gui_design.md` の11節を参照。残っているのは:

1. 相対値の分子/分母判定軸（Read_Override / Program_Override）のtype別デフォルト
   （担当者確認中。現状はスコアパーツ側の `split_axis` で明示指定）
2. `denominator_offset` の値の運用（パーツごと指定 or 全体デフォルト）

## 次のステップ

- Streamlit UI（スコアパーツ編集・order指定・テスト実行・jsoncダウンロード）
- 現行GUIからダウンロードする定義ファイル一式が整備され次第、
  ダミーデータ自動生成によるテスト機能
- 現行最適化スクリプト側 `get_score()` への分岐追加
  （ブリッジ実装例は `scripts/get_score_bridge_example.py` に用意済み。
  残りは turbo.py への数行の組み込みのみ）
