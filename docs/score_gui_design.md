# score_gui Phase1 設計書 (v2)

v1からのフィードバックを反映した改訂版。修正箇所には要点のみ記載し、
まだ確定していない項目は末尾「11. 確認事項」にまとめる。

参照した実物ファイル: `result_tmp/*`, `reference_scripts/expand_FBC_measure.py`（旧 gomi/）,
`sample.jsonc`(WLgroup・Generation・constraintThresholdを含む実際のoptimization設定),
`sample.py`(dVtBudget係数), `result_tmp/initial_temperature.csv`(Board別実測温度)

---

## 1. スコープ（Phase1）

- ユーザのローカルで動かす Streamlit スコア設計UI
- スコア／スコアパーツの jsonc ファイルへの保存・読込
- **実際にスコアを計算するバックエンド本体**（python3.13 venv + polars）
  → これは後回しではなく Phase1 の必須成果物。現行の最適化スクリプトは
  python3.7 で動作するため、スコア計算部分だけ別venv(python3.13)で
  subprocessとして起動される必要がある。
- 現行GUI・現行最適化スクリプト自体の改修（アップロード先の紐付け等）は対象外。
  ただしスコア計算エンジンをsubprocessから呼べる形（CLI）にしておくのは必須。

---

## 2. 全体アーキテクチャ（訂正）

v1では「Streamlit UIがengineを直接importし、jsoncへの変換もengine側」という
書き方をしていたが誤り。**UIの入力をjsoncに変換する処理自体はただの構造化データの
シリアライズであり、polarsは不要**。polars(engine)を使うのは以下の2箇所のみ。

1. Streamlit UI内の「ダミーデータでテスト」ボタン（同venv内で直接呼び出し）
2. 本番の最適化ループ内で、各epochの実データに対しスコアを計算する部分
   （python3.7側からsubprocessで python3.13 venv のCLIを呼ぶ）

```
[現行GUI(サーバ)]
  定義ファイル一式をダウンロード提供:
    - map_dataName.csv, map_Label.csv, map_State.csv, map_Override.csv（typeで共通）
    - parameterLabel_{type}.csv, dataName_{type}.csv（typeごと）
    - optimization設定(Generation, WLgroup, constraintThreshold等。sample.jsonc相当)
        ※ 実測前は存在しない{type}.csv自体は含まれない
        ※ dVtBudget係数(sample.py相当)も定義ファイルとして提供
     │
     │ ユーザが手動ダウンロード
     ▼
[ユーザのローカルPC : Streamlit UI (python3.13 venv)]
    - 上記定義ファイルをアップロードし、type別の軸候補を画面に反映
    - スコアパーツ／スコアをGUI操作で組み立てる → 内部はただのdict構築（polars不要）
    - [テスト]ボタン: 同venv内で scorelib_param engine (polars) を直接呼び出し、
      あれば実データ(result_tmp相当)、なければダミーデータで動作確認
    - [完了]ボタン: score.jsonc としてローカル保存
     │
     │ ユーザが手動アップロード（現行GUI側の受け口に依存。詳細は現行GUI側の実装次第）
     ▼
[現行GUI(サーバ)] → 最適化実行時の適切なディレクトリへ配置
     │
     ▼
[最適化スクリプト(python3.7): turbo.py の get_score()] 各epoch実行時
    - 現状 get_score() は result_tmp 配下のcsvを読み、config(jsonc由来の辞書)の
      `optimization.score_function` で指定された score.py 内の関数名を実行して
      DataFrameを返す構造になっている（テスト運用実績はあるが本番未組込み）。
    - 今回のengineは `score_function` に例えば `"gui_score"` のような予約名が
      指定されたときの分岐先として get_score() 内に追加する形で差し込む
      （既存のscore.py関数群とは独立に、こちらのCLIをsubprocess起動する薄い
      ブリッジをget_score()内に数行足すだけで良い、という認識で一致）。
    - subprocess呼び出し:
      python(3.13 venv) -m scorelib_param.cli --config config.jsonc --data-dir <epoch出力ディレクトリ>
      （score.jsonc相当の内容は config の `optimization{}` 内にマージ済みの想定。6節参照）
    → engineがpolarsで {type}.csv 等を読み、指示通り計算し、
      「Score列 + 全ScorePart名の列」を1行のテーブルとして標準出力
    ← get_score()側が標準出力を pandas DataFrame にパースして返す
```

---

## 3. データモデル

### 3.1 map系ファイルの共有範囲（確認事項1への回答反映）

map系ファイル（`map_DataName.csv`, `map_Label.csv`, `map_State.csv`, `map_Override.csv`）は
**全type共通**と確認済み。type別に探索する分岐は不要、単純に共有ファイルとして1セットのみ読む。

（2026-07-29 追記: DataName の map の実出力の綴りは `map_DataName.csv`（大文字D）と
実環境で判明。旧サンプル・本書の旧記述は `map_dataName.csv` だったが、Linux は
大文字小文字を区別するため実環境で候補が出ない不具合になった。エンジンは
両方の綴りを読む — axis_resolve._map_file_for_axis）

### 3.2 軸の解決方式（設計を簡略化）

v1では「FBC_expanded.csv相当に一度展開してから使う」想定だったが、
ユーザ提案の通り、**わざわざ全列を展開しない**。理由と方針:

- スコアパーツの `order`/`aggregations` に登場する軸だけが必要
- polarsのlazy frameで `{type}.csv` に対し、必要なラベル軸（Erase/Program/Read Label,
  Override）や DataName のみを都度 `join` し、早い段階で `filter` することで
  無駄な展開・メモリ使用を避けられる（predicate pushdown が効く）
- 実装上は「スコアパーツが要求する軸の集合」を先に集めてから、
  必要なmapファイル・parameterLabelファイルだけをjoinする resolver を作る

→ v1の「3.2 軸の分類」の表形式の説明は撤回し、
  「実測軸（{type}.csv直接列）」「解決が必要な軸（parameterLabel/dataName経由）」の
  2分類のみとし、後者は要求されたときだけ遅延解決する。

**InBatchEpochについて**: 通常運用では常に0で実質未使用の列であることを確認。
BO側が1epochで複数候補をバッチ提案するケース（過去データ流用時など）は
将来的にありうるとのことだが、現状は他の軸と同じ扱いでよく、
「行識別子として集計対象から外して保持する」ような特別扱いは不要と判断した。
他の軸同様、集計指示(`order`/`aggregations`)の対象に含めてよい
（実質1値しかないため`filter`や`mean`等どれを選んでも結果は変わらない）。

### 3.3 相対値（reference / evaluation）判定軸（訂正・確認中）

> **再訂正（2026-07-28 実装済み）**: Override の True/False は相対化の判定に
> **使えない**ことが担当者ヒアリングで判明し、**Measure 番号（測定順序番号）を
> 第一級の識別軸に昇格**させて分子/分母を指定する仕様に変更した。dataName は
> 表示・検証層（labels 注記）。経緯・合意・実装状況は
> `docs/spec_change_dataname_measure.md` を参照（本ノート整理時に本節へ統合予定）。
> `split_axis` をハードコードしない下記の設計判断は的中し、エンジンは無変更で
> 新仕様に対応した。以下は当時の記録。

v1では `DataName` の文字列prefix(`reference_`/`evaluation_`)で判定するとしていたが、
**Overrideの True/False で判定する**という理解に訂正。読み込み系スコアでは
`Read_Override`、書き込み系スコアでは `Program_Override` になるのではとのことで、
現在担当者に確認中とのこと。

これを踏まえ、どちらの軸を分子/分母判定に使うかをハードコードせず、
スコアパーツ側で明示指定できるようにしておく（確定後は該当typeのデフォルト値を
決めるだけで済む）。

```jsonc
"relative": {
  "split_axis": "Read_Override",       // 確定待ち: Program_Override等になる場合もある
  "denominator_when": false,           // Override=False → 分母(reference)
  "numerator_when": true,              // Override=True  → 分子(evaluation)
  "denominator_offset": 20,            // 分母が0になるのを避けるためのoffset
  "denominator_pre_aggregation": [
    {"axis": "WL", "op": "mean"},
    {"axis": "STR", "op": "mean"}
  ]
}
```

`denominator_offset` は「相対化するときは常に分母に一定値を加算する」という
確認内容を反映したもので、dVtBudgetに限らず全ての相対値計算に共通して使う。

### 3.4 WLgroup

`sample.jsonc` の実物により、WLgroup定義は単独ファイルではなく
**既存のoptimization設定ファイルの一部**として提供されることを確認。

```jsonc
{
    "Generation": "B9LS",
    "optimization": {
        "score_function": "score_function_1",
        "constraintThreshold": { ... },
        "WLgroup": {
            "WLgroup01": [0, 3],
            "WLgroup02": [4, 8],
            ...
        }
    }
}
```

Streamlit UIはこの設定ファイル全体をアップロードしてもらい、`Generation` と
`WLgroup` をそこから読み取る（`WLgroup`単体を別ファイルとして扱う設計は撤回）。

追記（グループ定義の一般化）: 設定jsonc の `WLgroup` は読み込み時に score file 側の
`groupDefs`（4.2節）へ**編集可能なテンプレートとして取り込む**位置づけになった。
スコア計算が実際に使うのは score file の `groupDefs`（自己完結・エクスポートにも同梱）。
WL 以外の軸（STR 等）のグループ定義も UI から追加できる。実験のパラメータ割り当てと
スコア集計の分割が食い違う懸念はユーザーに確認済み（実験スクリプトも同じ合成後 config を
読むため齟齬は生じない、編集自由で良い）。

**訂正（2026-07-29、0.7.0）**: 上の「齟齬は生じない」というユーザー回答は
「**合成後の config には定義が1つだけ入り、実験スクリプトもそれを読む**」という
前提の発言だった。当時の実装はエクスポートが groupDefs に書くだけで
`optimization.WLgroup`（実験スクリプトが読む場所）へ書き戻さず、合成後 config に
新旧2定義が並ぶ形になっており、前提と食い違っていた（手編集時の変え忘れ・
「どちらが使われるか」の混乱もユーザー指摘）。0.7.0 で**エクスポートは WL 軸の
WLgroup 定義を旧形式キーだけに書き、定義の在り処を1つにする**形へ修正
（ScoreFile は旧形式キーを読める。設計判断の詳細は CHANGELOG ver.0.7.0）。

### 3.5 dVtBudget（大幅訂正）

**世代(Generation)は計算時にoptimization設定ファイルから取得**するため、
スコアパーツ側で世代を指定する必要はない（v1の誤り）。

**温度は測定結果と共に返る**（`result_tmp/initial_temperature.csv`参照）。

```
0,-28.236     # Board=0 の実測温度
1,82.934      # Board=1 の実測温度
```

一般に高温Board・低温Boardの2枚体制で測定するとのこと。
dVtBudget計算時は、この実測温度に**最も近い**キーを `dVtBudget_coef[generation]`
の温度キー（例: `-30`, `85`）から選び、Board単位で使用する係数を決める。

**変換式（訂正）**:

```
dVtBudget = -log10(relative_FBC) / b * 1000
```

`a` は使用しない。`relative_FBC` は3.3節の相対値計算結果（offset適用済み）。

これによりdVtBudgetタイプのScorePartは以下のように単純化される
（v1にあった `generation`/`temperature` フィールドは削除）:

```jsonc
{
  "name": "dVtBudget_R2A",
  "type": "dVtBudget",           // FBC.csvを読み、自動で相対値化→dVtBudget変換
  "relative": { ... },            // 3.3節と同じ形式（offset含む）
  "order": ["State", "Board", "Chip", "Block", "WL", "STR"],
  "aggregations": {
    "State": {"op": "filter", "value": "R2A"},
    "WL": {"op": "mean"},
    "STR": {"op": "mean"},
    "Board": {"op": "mean"},
    "Chip": {"op": "mean"},
    "Block": {"op": "mean"}
  }
}
```

世代・Board別温度・係数テーブルの参照は全てengine内部で
「optimization設定 + initial_temperature.csv + dVtBudget係数ファイル」から
自動解決し、ユーザがGUI上で世代や温度を選ぶ操作は不要とする。

dVtBudget係数ファイル（`sample.py`）は**正式にjsonc化してよい**と確認済み。
そのままの構造でjsonc化する:

```jsonc
{
  "B9LS": {
    "-30": { "R2A": {"a": 0.13, "b": -0.22}, ... },
    "85":  { "R2A": {"a": 0.23, "b": -0.12}, ... }
  }
}
```
（Pythonの負数・数値キーはjsonc化の際に文字列キーへ変換する）

---

## 4. スコアパーツ（ScorePart）仕様（訂正）

### 4.1 全体構造

v1のサンプルで type="FBC" なのに `dvtbudget` フィールドが存在していたのは
単純な記載ミス（コピペミス）で、指摘の通りおかしい。訂正版:

```jsonc
{
  "name": "FBC_A2B_upper1_rel",
  "type": "FBC",
  "relative": {
    "split_axis": "Read_Override",
    "denominator_when": false,
    "numerator_when": true,
    "denominator_offset": 20,
    "denominator_pre_aggregation": [
      {"axis": "WL", "op": "mean"},
      {"axis": "STR", "op": "mean"}
    ]
  },
  "order": ["Read_Label", "State", "WL", "STR", "Board", "Chip", "Block"],
  "aggregations": {
    "Read_Label": {"op": "filter", "value": "read_level_upper1"},
    "State":      {"op": "filter", "value": "A2B"},
    "WL":         {"op": "mean"},
    "STR":        {"op": "mean", "value": [0, 1, 2]},
    "Board":      {"op": "mean"},
    "Chip":       {"op": "mean"},
    "Block":      {"op": "mean"}
  }
}
```

`type: "dVtBudget"` の場合のみ、集計後に3.5節の変換が自動適用される。
（v1にあった `dvtbudget: {generation, temperature}` ブロックは完全に削除）

### 4.2 集計op（`worst`廃止）

確認の通り「小さい方が常に良い」という前提で全体が設計されているため、
ユーザが `min`/`max` を明示的に選べば済み、`worst` という抽象化は不要と判断し廃止。

| op | 意味 |
|---|---|
| `filter` | 指定した値のみ残す |
| `mean` / `sum` / `min` / `max` | 単純集計 |
| `mean` / `sum` / `min` / `max` + `values` | `values` 指定で値集合に限定してから集計（旧 `*_subset` は読み込み時に自動変換） |
| `mean` / `sum` / `min` / `max` + `weight` | **集計時重み**: その軸を潰す直前に、軸の値ごとの重みを値へ**乗じてから**集計する（例: `{"op": "max", "weight": {"WLgroup00": 10.0, ...}}`）。正規化された加重平均**ではない**（mean なら mean(重み×値)）。`weight` は辞書か数値1つ、`weight_ref` で重みセット（WLgroupWeight / weightSets）参照。重みを掛けるタイミングを明示的に制御したい場合（dVtBudget変換の前後で変えたい等）は従来どおり変換ステップ（`__xxx__` + `by`）を使う — 両方が適用可能な場面では結果は同一 |
| `expr` | 自由記述式 |

**2026-07-29 追記（0.6.0）**: 変換ステップ（`__xxx__`。従来は定数演算 add/sub/mul/div）に
定数を取らない**単項op**を追加した: `abs` = \|x\|、`log` = ln(max(\|x\|, floor))
（`floor` 必須 — 0 や負値で発散しない安全な対数。KLD の標準計算
`np.log(np.maximum(np.fabs(x), 1e-6))` がこの1ステップで書ける）。
KLD / dVthSGWLD の UI 雛形はこの op 入りの標準計算で生成される（12節）。

**グループ集計は派生軸（groupDefs）で表現する**（`group_reduce` op は廃止。読み込み時に
移行案内つきエラーになる）。グループ定義は「名前 + 対象軸 + 範囲一覧」で、
score file の `groupDefs`（設定jsonc の `optimization.WLgroup` は WL に対する定義として
互換読み込みされ、`groupDefs` 側が優先）:

```jsonc
"groupDefs": {
    "WLgroup":  { "axis": "WL",  "groups": { "WLgroup01": [0, 3], "WLgroup02": [4, 8] } },
    "STRgroup": { "axis": "STR", "groups": { "even": [0, 1], "odd": [2, 3] } }
}
```

パーツが定義名を order/aggregations/relative で参照すると、データ読み込み直後に
グループ列が生成され、以降は**普通の軸**として任意の位置で集計できる:

```jsonc
// WLgroupごとにWLをmin、他の軸を畳んだ後、最後にグループ間でmax
"order": ["WL", "STR", "Board", "WLgroup"],
"aggregations": { "WL": {"op": "min"}, ..., "WLgroup": {"op": "max"} }
```

旧 `group_reduce`(inner/outer) は「WLの直後にグループ軸を置く」ことで等価に書ける。
集計タイミングをずらせる（グループ間集計を Board 集約の後に回す等）のが廃止の動機。
注意: グループ列は読み込み時から存在するため、そのパーツ内では relative の分母事前集計等でも
グループをまたいで混ざらない（グループ横断で集計したい場合はそのステップにグループ軸自体を足す）。
定義名が order に無いのに対象軸だけ参照した場合は従来どおりの暗黙集約。
定義名と対象軸名の同名は禁止（エンジンがエラーにする）。

### 4.2b 自作Python関数パーツ（type="custom"）

集計パイプラインで書けない計算（複数csvの突き合わせ等）向けに、Pythonが書ける
ユーザが関数を1つ書いてスコアパーツとして呼べる:

```jsonc
{"name": "my_score", "type": "custom"}   // name と同名関数。function/params で明示・引数渡しも可
```

- 関数は **リポジトリ直下の `custom_parts.py`（SVN管理）** に置く。エンジンは常に
  そこを読み、**configにパスは持たせない**（実験入力から任意コード実行が可能に
  なってしまうため。関数の追加・変更は SVN コミット=レビューを経由させるのが意図）
- 関数契約: `def f(ctx) -> float`。ctx は data_dir / generation / group_defs / params。
  戻り値は有限な1スカラー（エンジンが検証）。custom パーツは order/aggregations/relative
  を持たない（混在はエラー）。expression・constraintThreshold からは通常パーツと同一に参照
- 設計UIは、GUIからダウンロードする**一式zipに同梱された custom_parts.py**（または
  パス指定/データディレクトリ内自動検出）から関数一覧を読み込む。実行側はリポジトリ内の
  ファイルを読むため、リビジョン一致なら設計時と同じ関数が走る。不一致で関数が無い場合は
  関数名つきの明確なエラー
- 実装: `scorelib_param/custom.py`（ロード・一覧・戻り値検証）、cli の type=custom 分岐、
  `--custom-parts`（テスト用上書き）。type単位の共有キャッシュは custom には適用しない

### 4.3 自由記述式（expr）— 実装方針決定

`simpleeval` ライブラリを採用する（サンドボックスされた式評価器で、四則演算・比較演算子・
べき乗等の一般的な演算を標準サポートし、`log`/`min`/`max`/`mean` 等の関数を
安全に追加登録できるため）。独自DSLをゼロから実装するよりメンテナンスコストが低い。
スコアパーツ内の `expr` op、および5節のスコア合成式・制約式の評価すべてで
同じ評価器を使い回す。

---

## 5. スコア仕様（大幅訂正・簡略化）

- Scoreに名前は不要、**スコアパーツと同じファイルに合成式・制約をまとめる**。
- 制約は「指標の値がしきい値より大きい(=悪い)ならNG」という判定を
  **最適化側の内部動作が行う**ため、ここでは比較演算子(`op`)を持たない。
- `sample.jsonc` に実在した `constraintThreshold` の形式をそのまま踏襲する
  （動的制約: `type: "percentile"` の場合、その時点までの実測値から計算した
  `coef`パーセンタイル値と `value` の大きい方を実際のしきい値として使う、
  という現行の動的制約ロジックは最適化側で完結しており、スコア側は
  「どの指標に対して何を設定するか」を記述するだけでよい）。

```jsonc
{
  "score_parts": [
    { "name": "FBC_A2B_upper1_rel", "type": "FBC", ... },
    { "name": "FBC_C2D_upper1_rel", "type": "FBC", ... },
    { "name": "dVtBudget_R2A", "type": "dVtBudget", ... }
  ],
  "expression": "0.5 * FBC_A2B_upper1_rel + 0.3 * log(FBC_C2D_upper1_rel) - dVtBudget_R2A",
  "constraintThreshold": {
    "FBC_A2B_upper1_rel": {"value": 25},
    "dVtBudget_R2A": {"value": 10, "active": "True", "type": "percentile", "coef": 20}
  }
}
```

`constraintThreshold` のキーはスコアパーツ名（または将来的に最終スコア自体を
指す予約名）を参照する。`active`/`type`/`coef` は既存フォーマットに合わせ
そのまま踏襲（`active`が文字列"True"/"False"である点も含め既存互換を優先）。

engineの責務は「`Score`（合成式の評価値）と、定義された**全ScorePartの計算値**を
返す」ところまでとする。`constraintThreshold`で指定されていないScorePartも
解析用に全て出力する（constraintThresholdに載っているものだけに絞らない）。
percentile計算や動的しきい値との比較・reject判定などの制約評価ロジック自体は
最適化側（呼び出し元）の責務のまま変更しない、という理解で設計する。

**出力契約（確定）**: InBatchEpochを含まない1行のテーブルとして
`Score, <ScorePart名1>, <ScorePart名2>, ...` を返す
（CSV1行 または JSONのフラットな辞書、実装はcli.py側で決定）。

---

## 6. ファイルフォーマット（jsonc）

- 1ファイルにスコアパーツ一式＋合成式＋制約をまとめて `score.jsonc` として保存
- スコアパーツ単体の使い回し用に、`score_parts`配列の要素だけを個別に
  export/importできる機能もUIに用意する（ファイル自体は同じスキーマの部分集合）
- 命名・スタイルは更新版 `sample.jsonc` に準拠（インデント4、キーはPascal/snake混在の
  現行スタイルをそのまま踏襲）

---

## 7. 計算エンジン

- Python 3.13（miniforge venv）、polars使用
- **subprocessエンジンはPhase1で必須実装**（v1で「Phase2」としていたのは誤り）
- モジュール構成（v1から変更なし、dVtBudgetまわりのシグネチャのみ簡略化）:

```
scorelib_param/
  models.py        # ScorePart / Relative / Aggregation / ScoreFile(score_parts+expression+constraintThreshold)
  io_jsonc.py       # jsonc <-> モデルの読み書き
  axis_resolve.py   # {type}.csv に対し、要求された軸だけを遅延join/filterするresolver
  aggregate.py      # order/aggregationsの逐次実行（polars, グループ派生列の式含む）
  relative.py       # split_axisベースの相対値計算 + denominator_offset
  dvtbudget.py      # Generation(config) + Board別温度(initial_temperature.csv)
                     # + 係数ファイルから -log10(rel)/b*1000 を計算
  expression.py     # simpleevalベースの式評価（score_parts合成式で共用）
  cli.py            # `python -m scorelib_param.cli --config config.jsonc --data-dir <result_tmp相当>`
                     # config.jsonc の optimization{} 内に score_parts/expression/constraintThreshold が
                     # マージされている前提。標準出力に Score + 全ScorePart値を1行のテーブルとして返す
                     # (get_score()側でDataFrame化しやすいようCSV or JSON records形式)
tests/
  test_aggregate.py
  test_relative.py
  test_dvtbudget.py
  test_expression.py
  # ダミーデータ生成を使うテストは後回し（8節）。実データ(result_tmp)ベースの
  # 小規模フィクスチャで先に検証する。
```

---

## 8. テストについて（優先度を下げる）

`parameterLabel_{type}.csv` 等は実測前には存在しないため、当初想定していた
「定義ファイルから軸候補・スキーマを予測してダミーデータを生成する」機能は、
必要な定義ファイル一式がまだ整備されていないため**後回し**とする。

Phase1では、`result_tmp` にある実データ相当の小規模フィクスチャ・
`sample.jsonc`・dVtBudget係数jsonc・`initial_temperature.csv` を使った
pytestでの動作確認を優先し、GUI上の「ダミーデータでテスト」ボタンや
本格的な自動ダミー生成は後続タスクとする。

---

## 9. Streamlit UI（変更なし・概要のみ）

- スコアパーツ一覧／編集画面（type選択、相対値化設定、Order、軸別集計指示）
- スコア編集画面（合成式のテキスト入力、`constraintThreshold`の追加編集）
- テスト実行画面（実データ or 簡易ダミーデータでの動作確認。8節の通り後回し可）
- 完了ボタンで `score.jsonc` をダウンロード

---

## 10. 実装順序（改訂）

1. **データモデル + jsonc I/O**（`models.py`, `io_jsonc.py`） — 更新版sample.jsonc・
   dVtBudget係数jsoncとの相互変換をテスト
2. **軸解決・集計エンジン**（`axis_resolve.py`, `aggregate.py`） — result_tmpの実データで
   pytest検証。展開なしの遅延join方式で実装
3. **相対値・dVtBudget変換**（`relative.py`, `dvtbudget.py`）
4. **式評価**（`expression.py`, simpleeval採用）
5. **CLI**（`cli.py`） — subprocess呼び出し口として、この時点で一通り動く状態にする
   （後回しにしないこと自体が今回の重要な訂正点）
6. **Streamlit UI**
7. （後回し）ダミーデータ自動生成によるテスト機能

---

## 11. 確認事項（残）

以下の2点は担当者確認待ち・細部調整のため設計上は差し替え可能にしておき、
実装はブロックしない。

1. ~~相対値の分子/分母判定に使う軸（`Read_Override` か `Program_Override` か）は
   type・スコアパーツごとに異なりうるとのことで担当者確認中。engine側は
   `split_axis` をスコアパーツ内で明示指定できる設計にしておくので、
   確定後にtype別デフォルト値を決めるだけで対応可能という理解でよいか。~~
   → **解消（2026-07-28）**: Override 判定自体が使えないと判明し、Measure 番号
   基準へ仕様変更・実装済み（3.3節の注記と docs/spec_change_dataname_measure.md）。
   「split_axis を明示指定できる設計」はそのまま活き、エンジン無変更で対応できた。
2. `denominator_offset`（1 or 20）はスコアパーツ・typeによって使い分けるのか、
   固定のデフォルト値を1つ決めて基本的に使い回すのか。
3. ~~offsetの適用範囲~~ → **確認済み**: offsetは分子・分母の両方に加算する
   `(num+offset)/(den+offset)` で正しい（分子のFBCが0のときlog10が-infに
   発散するのを防ぐ意味でも必須）。現実装の通り。

以下は解決済み（v2で反映）:

- ~~subprocessブリッジの要否~~ → get_score()内で`score_function`が予約名
  （例:`"gui_score"`）のときに今回のCLIを呼ぶ数行の分岐を追加するだけでよい、
  という方針で一致。ブリッジ実装自体は本プロジェクトのスコープに含める。
- ~~score.jsoncと現行configの紐付け~~ → score_parts/expression/constraintThresholdは
  config(jsonc)の`optimization{}`内にマージされる想定。
- ~~InBatchEpochの扱い~~ → 実質未使用の定数列であり、特別な行識別子として
  保持する必要はない（3.2節に追記）。
- ~~engine出力の形式~~ → `Score` + 定義された全ScorePartの値を1行で返す
  （constraintThreshold記載の有無に関わらず全パーツを出力）。

## 12. 2026-07-29: 新計算対象の標準計算と vthSkip ダミー計算（0.6.0）

実フォーマット確定（spec_change ノート10節）を受けた設計判断。

### 12.1 KLD / dVthSGWLD の「一般的な計算」は type 雛形として提供

ユーザー確認済みの標準形（強制ではなく初期値。生成後は普通に編集できる）:

- **KLD**: Board/Chip mean → `__log__`（log(max(|x|, 1e-6))）→ SGWLD sum
  （集計時重み 0.1。SGWLD 別の重みに変えるのも同じ欄でできる）
- **dVthSGWLD**: Board/Chip/Block mean → `__abs__` → SGWLD sum
  （SGSB/SGS/SGD/SGDT を除く8要素の選択つき集計。除外は「これまで変更した
  ことがない」慣習であり、map にその名前がある場合に限り雛形へ入れる）

エンジンに暗黙の既定は埋め込まない: 標準計算はすべて設定 jsonc に明示され、
担当者が設定を見れば何が起きるか分かる（雛形=初期値、という従来方針の適用）。

### 12.2 vthSkip: ファイル不在 epoch のダミー計算

実験 config（`optimization.vthSkip` — フロー側の既存項目）に vthSkip がある
場合、フローは指定 epoch 数まで KLD / dVthSGWLD を測定せず**ファイル自体が
出力されない**。エンジンの対応:

- **トリガーはファイル不在のみ**（epochs はエンジンでは使わない）。epoch 数の
  管理はフロー側の仕事のままにし、batch で過去実験を流用するときも「無いもの
  はダミー」で自動的に正しくなる（今の実験の epochs 値は過去データと無関係）
- **ダミー値の出所は config の `dummyKLDValue` / `dummyDVthValue`**（フローの
  既存キーをそのまま読む。スコア設定側に重複して書かせない）。キー名 → type
  の対応（KLD / dVthSGWLD）はエンジン固定。他 type に必要になったらパーツ単位の
  汎用フィールドを検討する
- **ダミー値の意味論は「変換後の値」**: SGWLD 等の軸の全組み合わせ（要素は
  map → 他 csv の実在値 → 選択リストの順で決定）にダミー値を敷き詰め、
  変換ステップ（__log__/__abs__ 等）は**スキップ**、集計（選択リスト・集計時
  重み・sum/mean）は通常どおり適用する。フローの慣習（KLD のダミー 0 は
  log 適用後の量に対する値）をそのまま受け入れるため。
  典型: KLD ダミー 0 → 0.0、dVthSGWLD ダミー 1 → 残す8要素の総和 = 8.0
- **報告**: 単一 epoch 計算は stderr の note、batch は BatchResult.dummy_used
  （epoch → パーツ名）+ CLI の stderr 報告。「静かに全部ダミーだった」に
  気づけるようにする（そもそも KLD を測っていない古い実験を batch に食わせた
  場合も全 epoch ダミーになる — エラーではなく報告で扱う）
- 制限: relative / dVtBudget パーツのダミー計算は非対応（明示エラー）。
  UI は変更なし（設計時はファイルがある前提。vthSkip は実行時の機構）
