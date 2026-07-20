# 過去実験データのバッチスコア計算 — 設計方針

ステータス: **承認済み・実装完了**（2026-07-21）。実装は `scorelib_param/batch/`、
テストは `tests/test_batch.py`（等価性・圧縮・エラー系・advisory、18件）。
（改訂1: type一般化の明記 / 圧縮の扱い / Step-Loop構造対応 / 配置 /
ローカル・ダウンロード両対応 / バッチサイズ推奨 / 出力列の説明。
改訂2=実装時: Fetcher の実引数は `(EpochRef, staging_root) -> Path` —
リモート実装が取得先を staging 配下に置き、計算後の削除対象にできるように
した。実スケール実測: 129万行/epoch × 3 epoch を 0.59s で一括計算し
逐次計算と全値一致）

## 1. 目的・背景

ベイズ最適化の初期モデル構築に、過去に行った同系統実験の生データを活用する
（`docs/score_gui.md` 134-135行の「過去データ活用」）。過去実験の測定結果
（epoch単位の result_tmp 相当）を大量に読み込み、現行のスコア設計
（config.jsonc）でスコアを計算する。

epoch ごとに現行 CLI を subprocess 起動する方式では、プロセス起動・config
パース・CSV 読み込みが epoch 数だけ繰り返され、数百〜数千 epoch では
実用にならない。そこで **50〜100 epoch 程度を1バッチとしてまとめて計算し、
バッチ内の epoch 数ぶんのスコアを一括で返す**機能を追加する。

## 2. スコープと前提

- **データ選出は本機能の外**。データサーバから「同一パラメータ系の使える
  過去実験」を選び出す機能は別途依頼される。本機能は**使えると判断された
  result_history の集合を受け取る**前提とする。
  ただし判断漏れはあり得るため、エンジン側で検出できる不整合
  （ファイル欠落・軸値の不一致・グループ範囲外の値など）は
  **どの epoch が原因かを特定できる形で**報告する（§8）。
- 現行エンジン（`scorelib_param/`）・現行 CLI・Streamlit UI・既存テストは
  **一切変更せずに動き続ける**。追加はすべてオプショナル引数・新規モジュール。
- バッチ計算の結果は「バッチ化しない逐次計算と数値完全一致」を
  テストで保証する（§10）。

## 3. 入力仕様

### 3.1 過去実験のディレクトリ構造

実験ログディレクトリは Step / Loop の階層を持ち、result_history は
その中にある:

```
<実験ログディレクトリ>/          # 例: expA
  Step1/
    Loop01/
      result_history/
        result.0001/             # 1 epoch 目のデータ（result_tmp 相当）
          FBC.csv                # ← 例。実際は tR.csv などスコア設計が参照する
          tR.csv                 #    任意の {type}.csv 群（未知のtypeも可）
          parameterLabel_FBC.csv # {type} ごとの付随ファイル
          dataName_FBC.csv
          map_*.csv
          initial_temperature.csv
        result.0002/
        ...
    Loop02/
      result_history/...
  Step2/...
```

- ファイル名は**例であり固定リストではない**。実際に必要なファイルは
  「config の score_parts が参照する type の集合」から決まる:
  各 type の `{type}.csv` と、その軸解決に必要な
  `parameterLabel_{type}.csv` / `dataName_{type}.csv` / `map_*.csv`。
  エンジンの軸解決（`axis_resolve`）は type 汎用・命名規約ベースなので、
  **提示されたことのない測定結果 csv でも命名規約に従っていれば
  そのまま扱える**。検証（§8）も固定リストではなく config 駆動で行う。
- 入力は **複数の result_history ディレクトリへのパスのリスト**
  （ローカルパスまたはリモート参照。§6.1）。自動発見はしない。
- 各 result_history 内の `result.NNNN` を昇順に列挙して epoch とする。
  `result.NNNN` 以外のエントリは無視する（警告のみ）。

### 3.2 圧縮への対応

`result.NNNN` 内の csv は tar.gz 等に固められている場合がある。
polars の対応状況（1.42 で実機確認済み）に合わせて扱いを分ける:

| 形式 | polars直読み | 扱い |
|---|---|---|
| 非圧縮 `.csv` | lazy scan 可 | そのまま使用（コピーもしない） |
| 単体 `.csv.gz` | **lazy scan 可** | **解凍せず直接読む**（scan_csv がそのまま読める） |
| `.tar.gz` / `.tgz` / `.zip`（複数ファイルのアーカイブ） | 不可 | ステージング領域に展開してから読む |

- つまり**解凍が必要なのはアーカイブ形式のみ**。gz 単体はそのまま読める
  ため、ステージング（ディスク書き出し）を省略できる。
  ※ gz は分割読みできない形式のため scan 時に内部でファイル単位の
  展開が走る。バッチ内で各 type を読むのは1回だけなので問題にならないが、
  もし実測でメモリ問題が出た場合に備え「gz も展開する」フォールバック
  フラグを用意する。
- アーカイブ展開が必要な epoch は、ステージング領域に
  「展開されたファイル + 非圧縮ファイルへのシンボリックリンク
  （Linux。Windows開発機ではコピー）」で **result_tmp 相当のビューdir** を
  作り、エンジンにはそれを渡す（エンジンは常に普通のディレクトリを見る
  だけで、圧縮の存在を知らない）。
- 展開不要な epoch（全ファイルが非圧縮 or gz 単体）はビューdirを作らず
  元ディレクトリを直接渡す（ステージングも削除も発生しない）。
- 展開後に「config が参照する type のファイルが揃っているか」を検証し、
  不足はその epoch のエラーとして記録する。
  **入力元のファイルは一切変更・削除しない**。

### 3.3 Epoch 識別子

- 識別軸の名前は **`Epoch`**（確定）。データ列・グループ定義名と衝突した
  場合は明確なエラーにする（既存の `InBatchEpoch` とは別物。バッチ提案が
  将来行われる場合は `(Epoch, InBatchEpoch)` の複合識別に拡張できるが、
  現時点では対応しない）。
- result_history を識別する**ラベル**のデフォルトは、Step/Loop 構造を
  反映した `実験ログディレクトリ名/Step{N}/Loop{NN}`
  （result_history から親を3段さかのぼって構成。例: `expA/Step1/Loop01`）。
  構造が想定と違う場合はフルパスからの相対で代替し警告する。
  呼び出し側が `{ラベル: パス}` で明示指定も可能。ラベル重複はエラー
  （黙って連番を振らない）。
- Epoch の値は **`{ラベル}#{番号}`** の文字列
  （例: `expA/Step1/Loop01#0001`）。全 epoch を通して一意。

## 4. 配置 — `scorelib_param/batch/` サブパッケージ

バッチ関連は **`scorelib_param` パッケージ内のサブディレクトリ**に置く:

```
scorelib_param/
  batch/
    __init__.py      # 公開API: compute_score_batch, BatchRunner 等を再エクスポート
    __main__.py      # python -m scorelib_param.batch で起動する CLI（§9）
    history.py       # result_history の列挙・ラベル付け・Epoch ID 生成（§3）
    staging.py       # アーカイブ展開・ビューdir・検証・削除（§3.2, §6）
    compute.py       # 計算層: BatchComputeContext / バッチスコア計算（§5）
    runner.py        # オーケストレータ: バッチ分割・先行取得・削除（§6）
```

理由:
- SVN へのリリースは「`scorelib_param/` + `custom_parts.py` の同期」なので、
  パッケージ内に置けば**リリース手順を変えずに**最適化サーバへ届く。
- 既存のエンジン中核（フラットな `scorelib_param/*.py`）と新機能の境界が
  ディレクトリで明確になり、既存ファイルが散らからない。
- 既存モジュールへの変更は `cli.compute_score_part` へのオプショナル引数
  追加と `dvtbudget` の拡張のみ（§5.1）。

## 5. 計算層の設計（エンジン拡張）

エンジンの中核「グループキー＝その時点で残っている全列」により、
**Epoch 列を入力フレームに1本足して order に載せなければ、全集計・
相対化ペア照合・複合軸・グループ派生軸が自動的に epoch 単位で分かれて
実行される**。これは現行設計が意図的に準備していた性質であり
（`aggregate.collapse` の `identity_axes` 引数、README「将来の過去データ
活用」節）、集計エンジン本体は無変更で済む。

### 5.1 変更・追加点

1. **`batch/compute.py` の `BatchComputeContext`**
   （`cli.SharedComputeContext` を拡張）
   - source type ごとに: バッチ内の各 epoch ディレクトリを従来の
     `axis_resolve.resolve_axes()`（無変更）で解決 →
     `pl.lit(epoch_id).alias("Epoch")` を付与 → **lazy のまま** `pl.concat`。
   - type単位の共有読み込み・prefix_cache の仕組みはそのまま働く
     （キャッシュはバッチ内の全 epoch 分の中間結果を1エントリで共有）。
2. **`cli.compute_score_part` にオプショナル引数 `identity_axes=()`**
   - 射影列 `[type] + sorted(required_axes)` に identity 軸を追加。
   - 最終収束を `collapse_to_scalar` から
     `collapse(lf, col, identity_axes)`（既存）に切替。空なら従来動作。
   - 戻り値: identity 軸ありのときは epoch→値 の対応（DataFrame）。
3. **`dvtbudget.apply_dvtbudget` の拡張**（唯一の実質的修正）
   - 温度は epoch ごとの `initial_temperature.csv` から来るため、
     係数 `b` が epoch で変わりうる。係数対応表を
     `(Epoch, Board, State)` キーで作って join する分岐を追加
     （単一 epoch 時は現行の `(Board, State)` join のまま）。
4. **expression 評価**: パーツ値が epoch ごとになるため、epoch ごとに
   simpleeval を評価するループ（100 epoch ×数パーツで数ms。ベクトル化不要）。
5. **custom パーツ**: `ctx.data_dir` 前提の設計のため、バッチでは
   epoch ごとに関数を呼ぶループにフォールバック（正しさは保たれる。
   遅くなるのは custom パーツのみ、とドキュメントに明記）。
6. **`compute_score_batch(epochs, run_config, ...)`**:
   上記を束ね、1バッチ分の結果 DataFrame（§7）を返す。

### 5.2 相対化・dVtBudget が epoch 混線しないこと（確認済みの根拠)

- `relative.apply_relative` は「残っている全軸が一致する行同士」を join で
  ペアにする → Epoch 列があるので**同一 epoch 内でのみ**ペアになる。
- `denominator_pre_aggregation` の group_keys にも Epoch が含まれるため、
  分母集計も epoch 内に閉じる。分母が「スカラーまで潰れて cross join」する
  分岐は Epoch が残る限り発生しない（=誤った epoch 横断ブロードキャスト
  は構造的に起きない）。
- diff / expr op の group_keys・join keys にも Epoch が自動的に入る。

## 6. パイプライン実行フロー（BatchRunner）

### 6.1 データ供給 — ローカル・ダウンロード・ハイブリッド

**入力の各 result_history は「ローカルパス」または「リモート参照」の
どちらでもよく、混在（ハイブリッド）も通常運用として扱う。**

- ソースは epoch 単位ではなく result_history 単位で指定し、Runner が
  epoch ごとの取得タスクに分解する。
- **ローカル/共有マウント済みパス** → pass-through（取得・コピーなし。
  アーカイブ展開が必要な場合のみステージングが発生）。
  データサーバとの連携仕様が固まるまでは、この形が主となる。
- **リモート参照** → fetcher（下記）がローカルのステージング領域へ取得。
- 取得手段は差し替え可能な callable として注入する:

  ```python
  # (epoch参照, ステージング領域) → ローカルの epoch ディレクトリ。
  # リモート実装は staging_root 配下に取得して返す（計算後に削除される）。
  # ブロッキングでよい（Runner側で並行化）
  Fetcher = Callable[[EpochRef, Path], Path]
  ```

- **scp か共有マウントかで実装は大きく変わるか？** → 変わらない。
  - 共有マウント = OSからは普通のパスなので pass-through そのもの
    （マウントが遅い場合に備え「ローカルへ先読みコピーする」fetcher も
    同じインターフェースで足せる）。
  - scp/rsync = subprocess 呼び出し + リトライの薄い fetcher 実装
    （数十行）。認証（ssh鍵等）は運用側の設定であり、コードの構造には
    影響しない。
  - どちらもアーキテクチャは不変で、fetcher の実装が1つ増えるだけ。
    **初期実装では pass-through fetcher のみを提供**し、リモート用は
    転送手段確定後に追加する（インターフェースは本設計で固定）。

### 6.2 パイプラインの動き

```
epochリスト → batch_size 個ずつのバッチ列に分割
                                    （取得単位 = 計算単位）

  [prefetch worker(s)]                     [main]
  バッチk+1..k+P を fetch+展開 ──queue──→ バッチk を計算
  （P = max_prefetch, 既定 2）              ↓ 計算完了
  queue が P 件埋まっていたら待機          バッチkのステージング領域を削除
                                           （バックグラウンドで実施可）
                                           次のバッチを queue から取得
```

- **先行取得は最大 `max_prefetch` バッチ**（既定 2、設定可能）。
  ディスク使用量の上限は「(1 + max_prefetch) × 1バッチ分」で押さえられる。
- fetch + 展開は I/O バウンドなので `ThreadPoolExecutor` + 上限付きキューで
  実装（polars 計算中も GIL を長く握らないため並行が効く）。
- **削除するのはステージング領域（fetch 先・展開先）のみ**。
  pass-through されたローカル既存データは削除しない。
- `keep_staging=True`（デバッグ用）で削除を止められる。
- 逐次モード（prefetch なし: fetch→計算→削除→fetch...）は
  `max_prefetch=0` として同じコードパスで表現する。

### 6.3 バッチサイズとメモリ — 既定値・自動推奨・警告

- `batch_size` 既定 50（epoch 数）。1バッチ ≒ 50 × 130万行 = 6,500万行だが:
  - 全パイプラインを lazy に構成し、polars の projection/predicate pushdown
    を効かせる（filter 系の絞り込みは concat を越えて各 CSV スキャンまで
    押し下がる）。collect 点（prefix_cache・最終 collapse）は
    streaming エンジンで実行し、ピークメモリを抑える。
  - epoch は互いに独立なので、`batch_size` を変えても結果は不変。
- **自動推奨・警告（advisory。必須機能ではないが実装する）**:
  - 実行環境は Ubuntu が一般的（古いバージョンあり）→ 追加依存なしで
    `/proc/meminfo` の `MemAvailable` を読む（どの Linux でも可。
    読めない環境 = Windows 開発機等では psutil があれば使用、
    なければ推奨をスキップ）。
  - 1 epoch あたりのメモリ足跡は、最初の epoch を実測して見積もる
    （必要軸に射影した行数 × 列数 × dtype幅 + 相対化中間の係数）。
  - `--batch-size auto` で「利用可能メモリの ~1/3 に収まる最大サイズ」を
    自動選択。数値指定時も、見積もりが利用可能メモリに対して
    過大（警告: 減らす提案）/ 極端に小さい（情報: 増やせる提案）の
    メッセージを stderr に出す。
  - あくまで目安であり、実行をブロックしない（見積もりが外れても
    streaming 実行により即 OOM にはなりにくい）。
- 数千 epoch 規模でも「バッチ列を順に流す」だけで対応できる。

## 7. 出力仕様

Python API・CLI とも、1 epoch = 1 行の表を返す:

| 列 | 型 | 意味 | 例 |
|---|---|---|---|
| `Epoch` | str | epoch の一意ID（`{ラベル}#{番号}`）。計算内部で識別軸として使った値そのもの | `expA/Step1/Loop01#0001` |
| `History` | str | その epoch がどの result_history 由来か（=ラベル）。**BO側が「どの実験の何epoch目か」からパラメータを突き合わせるための分解済みキー** | `expA/Step1/Loop01` |
| `EpochNo` | int | result_history 内の `result.NNNN` の番号 | `1` |
| `Score` | float | expression の評価値（現行 CLI の `Score` と同じ定義） | `160.4` |
| `<パーツ名>`... | float | 定義された全スコアパーツの値（現行 CLI と同じ定義、パーツ定義順） | |

- `Epoch` = `History` + `#` + `EpochNo` であり情報としては冗長だが、
  「一意キー1本で扱いたい場合」と「実験・epoch番号で結合したい場合」の
  両方に対応するため分解済みの2列も付けている。不要なら列を無視すればよい。
- Python API: 全バッチ縦結合の DataFrame を返す。バッチ完了ごとの
  callback / generator も提供（BOモデル構築側が incremental に受け取れる）。
- 除外 epoch は結果に行を持たず、別リスト（§8）で報告される。

## 8. エラー処理方針

「使えると判断されたデータ」に判断漏れがあり得る前提で設計する。
**既定は skip-and-report**（確定）: 不良 epoch を除外して続行し、
最後に除外一覧と理由を報告する。`strict=True` で最初の不良で即時失敗。

1. **ステージング時の事前検証（epoch 単位・安価）**:
   config が参照する type のファイル・列の存在、dVtBudget 使用時の
   `initial_temperature.csv` の存在を epoch ごとに検査。
   不合格 epoch はこの時点で除外リストへ。
2. **計算時のバッチ全体エラーの帰属**: グループ範囲外の値・filter が
   全 epoch で空、などはバッチ計算中に発生する。発生したら
   **そのバッチだけを epoch 単位の逐次計算に落として再実行**し、
   原因 epoch を特定・除外し、正常 epoch の値は救う
   （バッチ計算と逐次計算は数値等価なので結果の一貫性は保たれる）。
3. **epoch 欠落の検出**: パーツごとに「結果の行数 = バッチの epoch 数」を
   検証。ある epoch だけ filter が空振りすると行ごと消えるため
   （null にならない）、この検証で必ず捕まえ、欠落 epoch を特定する。
4. **報告**: 戻り値に `failed: {Epoch: 理由}` を必ず添える。
   全 epoch 不良の場合は結果空でも正常終了とせず、エラーにする。

## 9. CLI

現行 CLI（`scorelib_param.cli`）の入出力契約は変更しない。新設:

```bash
python -m scorelib_param.batch \
    --config <config.jsonc> \
    --history <result_historyパス> [--history <パス2> ...] \  # label=path 形式でラベル明示も可
    --dvtbudget-coef <coef.jsonc> \      # dVtBudgetパーツがある場合のみ
    --out <scores.csv> \                 # §7 の表（Epoch,History,EpochNo,Score,<パーツ>...）
    [--batch-size 50 | --batch-size auto] [--max-prefetch 2] \
    [--staging-dir <作業領域>] [--strict] [--keep-staging]
```

- `--initial-temperature` は不要（epoch ごとに各 result.NNNN 内のものを読む）。
- 結果 CSV とは別に、除外 epoch と理由の一覧を stderr（および
  `--out` と並ぶ `*.failed.csv`）へ出力する。
- 現行最適化スクリプト（python3.7）からは現行 CLI と同様に
  subprocess 起動 → CSV 読み込みで受け取れる。

## 10. 互換性とテスト計画

- 既存の全テスト（26件）は無変更で通ること。
- 追加テスト:
  1. **等価性（最重要）**: 同じ epoch ディレクトリ群をバッチ計算した結果
     が、`compute_score_file` を epoch ごとに呼んだ結果と全パーツ完全一致
     （`tests/data/result_tmp_mini` を複製して複数 epoch を構成。
     FBC 以外の type = tR も含めて検証）。
  2. 相対化・dVtBudget（epoch で温度が異なるケース）・グループ派生軸・
     複合軸が epoch 混線しないことの単体テスト。
  3. 圧縮: tar.gz 展開ビューdir / csv.gz 直読み / 混在 epoch。
     展開物の削除と、pass-through 入力が削除されないこと。
  4. エラー系: ファイル欠落 epoch の除外と報告、filter 空振り epoch の
     検出、バッチ→逐次フォールバックによる原因特定、strict モード、
     全滅時のエラー。
  5. パイプライン: max_prefetch 上限が守られること、Step/Loop 構造からの
     ラベル導出、ラベル重複エラー。
  6. バッチサイズ推奨: /proc/meminfo が無い環境でスキップされること
     （機能自体は Linux 実機での動作確認が主）。

## 11. 決定済み事項

- エラー既定: skip-and-report（`strict` はオプション）
- 識別軸名: `Epoch`
- fetcher: 初期実装は pass-through のみ。インターフェースは本設計で固定し、
  scp / マウント先読みコピー等は転送手段確定後に追加
