# scorelib — スコア計算エンジン (score_gui Phase1 バックエンド)

`score_gui.md` の仕様・`score_gui_design.md` の設計に基づく、スコア／スコアパーツ計算エンジンの実装。
Streamlit UI（未実装・次ステップ）が出力する設定を受け取り、実測データからスコアを計算する。

## ディレクトリ構成

```
scorelib/                   # 本体パッケージ
  models.py                 # 設定ファイルのデータモデル（pydantic）
  jsonc.py                  # jsonc（コメント・末尾カンマ付きJSON）の低レベル読み書き
  io_jsonc.py               # jsonc <-> pydanticモデルの変換
  axis_resolve.py           # {type}.csv + parameterLabel/dataName/map系の遅延join
  aggregate.py              # 軸ごとの逐次集計エンジン
  relative.py               # 相対値（分子/分母）計算
  dvtbudget.py              # dVtBudget変換
  expression.py             # 自由記述式の評価（simpleeval）
  cli.py                    # サブプロセス起動用エントリポイント
scripts/
  convert_dvtbudget_coef.py # dVtBudget係数のPythonファイル → jsonc 変換
tests/
  conftest.py               # 共通fixture（result_tmp等へのパス）
  fixtures/
    config.jsonc            # テスト用のconfig実例（後述）
    dvtbudget_coef.jsonc    # sample.py から変換した係数ファイル
  test_axis_resolve.py      # 軸解決の正しさ（FBC_expanded.csvとの全行一致）
  test_aggregate.py         # 各集計opの単体テスト
  test_relative.py          # 相対値計算の単体テスト
  test_dvtbudget.py         # 温度最近傍選択と変換式のテスト
  test_expression.py        # 式評価のテスト（サンドボックス性含む）
  test_jsonc.py             # jsonc読み書き・ラウンドトリップ
  test_cli.py               # 実データを使ったエンドツーエンドテスト
pyproject.toml              # パッケージ定義（pip install -e . 用）
.venv/                      # ローカルvenv（Python 3.11 + polars/pydantic/simpleeval/pytest）
```

## セットアップ

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
```

※ 設計上はPython 3.13を想定。この開発機には3.11しかなかったため3.11で構築したが、
コードは3.13でそのまま動作する（3.10+対応）。

## 使い方

### CLI（本番の最適化ループから呼ばれる形）

```bash
python -m scorelib.cli \
    --config <config.jsonc> \
    --data-dir <そのepochの測定結果ディレクトリ（result_tmp相当）> \
    --dvtbudget-coef <dVtBudget係数.jsonc> \        # dVtBudgetパーツがある場合のみ必須
    --initial-temperature <initial_temperature.csv>  # 同上
```

標準出力に1つのJSONオブジェクトを返す:

```json
{"Score": 160.408..., "FBC_A2B_upper1_rel": 1.344..., "dVtBudget_R2A": 159.736...}
```

- `Score`: `expression` の評価値
- それ以外: **定義された全スコアパーツ**の値（constraintThresholdに載っていないものも出力）

現行最適化スクリプト(python3.7)の `get_score()` からは、`score_function` に
`"gui_score"` 等の予約名が指定された場合の分岐としてこのCLIをsubprocess起動し、
標準出力をパースしてDataFrame化する想定（ブリッジは現行スクリプト側整備後に実装）。

### Pythonから直接呼ぶ（Streamlit UIのテストボタン等）

```python
from scorelib import io_jsonc
from scorelib.cli import compute_score_file
from scorelib.dvtbudget import load_board_temperatures

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

## config.jsonc の書き方

現行のoptimization設定（更新版 `sample.jsonc` の形式）に `score_parts` と `expression` を
追加した形。テストで実際に使用している `tests/fixtures/config.jsonc` が完全な実例。

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
        "split_axis": "Read_Override",   // この軸の値で分子/分母を分ける
        "numerator_when": true,          // Override=True の行が分子（提案パラ）
        "denominator_when": false,       // Override=False の行が分母（基準パラ）
        "denominator_offset": 1,         // (分子+offset)/(分母+offset)。両方に加算【確認済み】
        "denominator_pre_aggregation": [ // 比を取る前に分母だけ先に集計（省略可）
            {"axis": "WL", "op": "mean"},
            {"axis": "STR", "op": "mean"}
        ]
    },
    "order": ["Read_Label", "State", "WL", "STR", "Board", "Chip", "Block"],
    "aggregations": {
        "Read_Label": {"op": "filter", "value": "read_level_upper1"},
        "State":      {"op": "filter", "value": "A2B"},
        "WL":         {"op": "group_reduce", "group_def": "WLgroup",
                       "inner_op": "mean", "outer_op": "max"},
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

#### relative の各フィールド

毎epochの測定には基準パラの測定と提案パラの測定が混在しており、それを見分けて比を取る。

`relative` ブロックが**書いてあれば相対化する**。絶対値のまま使いたい場合は
ブロックごと省略（またはコメントアウト）する。`enabled` フラグは無い
（旧ファイルの `enabled: true` は無視され、`enabled: false` は明確なエラーになる）。

| フィールド | 意味 |
|---|---|
| `split_axis` | 分子/分母を見分ける軸（読み込み系: `Read_Override`、書き込み系: `Program_Override` 想定） |
| `numerator_when` | split_axisがこの値の行が分子（提案パラ）。例: `true` |
| `denominator_when` | split_axisがこの値の行が分母（基準パラ）。例: `false` |
| `mode` | `"ratio"`（デフォルト）: 比 `(分子+o)/(分母+o)` / `"diff"`: **delta値** `分子 - 分母` |
| `denominator_offset` | ratio時に**分子分母両方**に加算。ゼロ割・log発散防止。diff時は差で相殺されるため無視される |
| `denominator_pre_aggregation` | 比/差を取る前に**分母だけ**先に集計する指示のリスト（例: WL,STRを平均した値を分母にする） |

OverrideのTrueからFalseを引いたdelta値を取りたい場合は `"mode": "diff"` を指定する
（ペア照合の仕組みはratioと同一で、演算だけが引き算になる）。

分子行と分母行は、その時点で残っている全軸の値が一致するもの同士でペアになる。
`denominator_pre_aggregation` で分母側の軸を潰した場合は、残った軸で照合され
分母値が分子側にブロードキャストされる。

#### パイプラインステップ（orderへの処理の組み込み）

`order` には軸名のほかに `__xxx__` 形式の**仮想ステップ**を置け、
相対化・dVtBudget変換・オフセット加算などの処理を任意の位置に挿入できる:

| ステップ | 意味 | 省略時のデフォルト位置 |
|---|---|---|
| `"__relative__"` | この位置で相対化 | 全集計より前（先頭） |
| `"__dvtbudget__"` | この位置でdVtBudget変換（type=dVtBudgetのみ） | `__relative__` の直後 |
| その他の `"__名前__"` | 値列への行単位変換。aggregationsに同名のエントリで内容を指定 | （明示したときのみ実行） |

現在使える変換op: `add`（値列に定数を加算）。

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
| `filter` | 指定値の行だけ残す | 選択1個 |
| `mean` / `sum` / `min` / `max` | 集計。`value` を付けると対象をその選択集合に限定 | なし or 選択のリスト |
| `diff` | 2つの選択の差で潰す: a − b | 選択ちょうど2個のリスト |
| `group_reduce` | グループ定義で分割→グループ内集計→グループ間集計 | なし（`group_def`, `inner_op`, `outer_op`） |
| `expr` | 自由記述式。全値のリスト `values` と、軸の値ごとの辞書 `by` が使える | なし（`expr`） |

軸の値同士を組み合わせる例（Stateの集計指示として）:

```jsonc
"State": {"op": "filter", "value": "R2A"}                        // 通常: 1つのStateを選ぶ
"State": {"op": "diff", "value": ["R2A", "B2A"]}                 // R2A - B2A の差
"State": {"op": "sum", "value": ["R2A", "B2A"]}                  // R2A + B2A の和
"State": {"op": "expr", "expr": "0.5*by['R2A'] + 0.5*by['A2B']"} // 任意の重み付き合成
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
コピペせず **`optimization.selectionSets` に名前付きで定義して `ref` で参照**する
（WLgroupを `group_def` で参照するのと同じパターン）:

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
.venv/Scripts/python -m pytest tests/ -q     # 26件、全パス
```

### 何をどう検証しているか

- **軸解決の正しさ** (`test_axis_resolve.py`):
  `result_tmp` の実データ（FBC.csv 80,640行 + parameterLabel/dataName/map系）を
  本エンジンで解決した結果が、現行ロジック（`gomi/expand_FBC_measure.py`）の出力である
  `gomi/FBC_expanded.csv` と**全行一致**することを確認。展開せず遅延joinする新方式が
  現行の展開方式と同じ結果を返すことの保証。
- **各集計opの単体テスト** (`test_aggregate.py`): 手計算で答えの分かる小さなデータで
  filter/mean/subset/group_reduce/exprを検証。orderが全軸を潰し切らない場合の
  エラーも確認。
- **相対値** (`test_relative.py`): 分母の事前集計（WL→STRの順のmean）とoffsetが
  設計通りに効くことを手計算値と照合。
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

将来の過去データ活用（複数epochバッチ計算）に備え、集計の最終収束は
「識別軸（例: Epoch）を残して潰す」形に一般化済み（`aggregate.collapse`）。
現在は識別軸なし＝1スカラーで従来と同じ動作。

## 現行スクリプトとの数値比較手順（result_tmp_mini）

現行スクリプトの計算結果と数値一致を確認するための最小データが `result_tmp_mini/` にある
（FBC.csv 1152行 + tR.csv 432行。tRはState軸の代わりに**Page軸**を持ち、`map_Page.csv` で
L/M/U に解決される。map系ファイルは `map_{軸名}.csv` の命名規則により自動発見されるため、
FBCに無い軸でもエンジン側の変更なしで扱える）。

そのまま実行できるスコア設計例として `config_mini.jsonc` をリポジトリ直下に用意した:

```bash
.venv/Scripts/python -m scorelib.cli \
    --config config_mini.jsonc \
    --data-dir result_tmp_mini \
    --dvtbudget-coef dvtbudget_coef.jsonc \
    --initial-temperature result_tmp_mini/initial_temperature.csv
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
- **温度の最近傍選択**: `result_tmp_mini/initial_temperature.csv` は 25℃ / 30.83℃ だが、
  係数キーが -30 / 85 の場合、25℃→**-30**（|25-(-30)|=55 < |25-85|=60）、
  30.83℃→**85** に解決される。室温近辺のBoard同士でも異なる係数が選ばれるため、
  現行スクリプトの温度→係数の選択ルールが「最近傍」でない場合は数値がズレる。
- **dVtBudget係数**: `dvtbudget_coef.jsonc`（リポジトリ直下）は
  `tests/fixtures/dvtbudget_coef.jsonc`（実係数に更新済みのもの）と同内容にしてある。
  係数を変えたら両方更新するか、`--dvtbudget-coef` でどちらか一方を指すこと。

## 未確定事項（実装は差し替え可能な形にしてある）

`score_gui_design.md` の11節を参照。残っているのは:

1. 相対値の分子/分母判定軸（Read_Override / Program_Override）のtype別デフォルト
   （担当者確認中。現状はスコアパーツ側の `split_axis` で明示指定）
2. `denominator_offset` の値の運用（パーツごと指定 or 全体デフォルト）

## 次のステップ

- Streamlit UI（スコアパーツ編集・order指定・テスト実行・jsoncダウンロード）
- 現行GUIからダウンロードする定義ファイル一式が整備され次第、
  ダミーデータ自動生成によるテスト機能
- 現行最適化スクリプト側 `get_score()` への分岐（ブリッジ）追加
