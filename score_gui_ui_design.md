# score_gui Phase1 Streamlit UI 設計書 (v1)

バックエンド（scorelib engine）は実装済み。本書はその上に載せる
ローカル実行のスコア設計UI（Streamlit）の設計を記す。
エンジン側仕様は `score_gui_design.md` / `README.md` を参照。

---

## 1. 目的とスコープ

- 一般ユーザがPython/jsoncを直接書かずに、スコアパーツ・スコア・制約を設計できるUI
- ユーザのローカルPCで `streamlit run` により起動（サーバには置かない: Phase1の独立性要件）
- 設計結果を `score.jsonc`（ScoreFile形式: score_parts + expression + constraintThreshold +
  selectionSets）としてダウンロードし、ユーザが現行GUIにアップロードする
- 実測データがあればその場でテスト計算（エンジン直呼び）
- ダミーデータ自動生成によるテストは対象外（エンジン設計書8節の通り後回し）

## 2. 構成と起動

```
ui/
  app.py            # エントリポイント。サイドバーで画面切替する単一アプリ
  state.py          # session_state の初期化・ScoreFile編集状態の管理（純粋ロジック）
  widgets.py        # 集計指示エディタ等、複数画面で使う部品
scorelib/
  introspect.py     # 【追加】定義ファイル群から type一覧・軸一覧・軸の値候補を導出
                    # （UIから使うが純粋関数としてscorelib側に置き、pytest可能にする）
```

起動: `.venv/Scripts/streamlit run ui/app.py`（依存に `streamlit` を追加）

設計原則: **UIはエンジンの薄いラッパー**とする。判断ロジック（軸候補の導出、
検証、jsonc入出力、計算）はすべて scorelib 側の関数として実装し、
app.py/widgets.py はウィジェット配置とsession_stateの受け渡しだけを行う。
これによりUIロジックの大部分をpytestで検証できる。

## 3. 画面構成（サイドバーで切替する5画面）

### 画面1: データ読み込み

- **定義ファイルディレクトリ**（=**同系統の過去実験の出力一式**、result_tmp相当。
  5.1節参照）のパスをテキスト入力。画面上にもこの前提を説明文として表示する
- 取り込みは **FileCatalog 抽象**（名前→内容の辞書的インターフェース）を介して行い、
  Phase1のバックエンドは「ディレクトリ走査」1種のみ実装する。
  将来サーバ実行に移行した場合もサーバ側のパス（実験IDから決まるディレクトリ）を
  読む形でそのまま動作し、もし「ローカルファイルの持ち込み」が必要になった場合は
  アップロード・バックエンドを追加するだけで画面側は変更不要（確認事項1への回答反映）
- 「読み込み」ボタンで以下を走査し、認識結果を一覧表示:
  - `optimization` 設定jsonc（Generation / WLgroup / constraintThreshold /
    selectionSets / 既存score_parts。継続編集の起点になる）
  - dVtBudget係数jsonc
  - `parameterLabel_{type}.csv` / `dataName_{type}.csv` / `map_*.csv` → **type一覧**の検出
  - `{type}.csv` / `initial_temperature.csv`（あれば。テスト計算と値候補の精度向上に使用）
- 認識できたファイル・不足ファイル・検出されたtypeと軸の一覧を表示
- 軸の値候補の導出ルール（`scorelib/introspect.py`）:
  - Label系軸: `map_Label.csv` の値一覧
  - Override系軸: true / false
  - State等 `map_{軸}.csv` がある軸: そのmapの値一覧
  - WL/STR/Board/Chip/Block等の数値軸: `{type}.csv` があれば実データのユニーク値、
    なければ「候補なし（自由入力のみ）」
- dVtBudgetタイプは FBC系ファイルと係数jsoncが揃っている場合のみ選択肢に出す

### 画面2: スコアパーツ編集

- パーツ一覧（名前・type・相対化有無・使用軸の要約を表形式）＋
  「追加」「複製」「削除」「単体エクスポート/インポート」

**新規作成（「追加」）の動作**: エンジンは「orderが全軸を潰し切らないとエラー」
という仕様のため、空のパーツから始めるとユーザが全軸を手で追加するまで
計算可能にならない。これを避けるため、新規作成時は
**最初から計算可能な雛形を自動生成**する:

1. 名前（`part_1` 等の重複しないデフォルト）と type を選ぶ
2. type決定時に雛形を生成:
   - `order`: そのtypeの全軸をデフォルト順
     （Label系 → map系軸(State等) → WL, STR → Board, Chip, Block）で全て並べる
   - `aggregations`: 各軸にデフォルトop（`mean`）
   - `relative`: デフォルトON（仕様上、基準/提案の相対値化が一般的なため）。
     split_axis=Read_Override, numerator_when=true, denominator_when=false,
     denominator_offset=1 をプリセット。不要ならチェックを外す
3. 以降は「雛形からの差分編集」（Read_Label/Stateをfilterに変える等）となり、
   どの時点でもテスト実行が通る状態を保てる（軸の潰し忘れが構造的に起きない）

新規作成の入口は「追加」（雛形生成）「複製」（既存のコピー）
「単体インポート」（過去のエクスポート品）の3つ。

- パーツを選ぶと編集フォームを表示:
  - **name**: テキスト入力（重複チェック）
  - **type**: 検出済みtypeのプルダウン（FBC / tR / dVtBudget / ...）
  - **relative**: 「相対化する」チェックボックス（ON=ブロックあり、OFF=省略。
    enabledフラグは無いというエンジン仕様に対応）
    - split_axis（Override系軸のプルダウン）、numerator_when / denominator_when、
      mode（ratio/diff）、denominator_offset（数値）
    - denominator_pre_aggregation: 軸+opの行を追加・削除できる小テーブル
  - **order**: 現在のエントリを上下ボタンで並べ替えるリスト。エントリ追加は:
    - 軸を1つ選んで追加
    - 複合軸: 軸を複数選択して「束ねて追加」（"State&Read_Label" が生成される）
    - 仮想ステップ: `__relative__` / `__dvtbudget__` の位置指定、`__offset__` 等の追加
  - **aggregations**: orderの各エントリごとに集計指示エディタ:
    - opプルダウン（filter / mean / sum / min / max / diff / group_reduce / expr）
    - opに応じた入力欄だけを表示（value/values混同がUI上は構造的に起きない）:
      - filter: 値候補のプルダウン＋自由入力。複合軸なら軸ごとの値選択1行
      - mean等: 「全値」or「選択リスト」or「選択セット(ref)」の3択
      - diff: 選択2つ（複合軸なら軸ごとの値選択2行）、または ref
      - group_reduce: group_def（WLgroup）+ inner_op / outer_op
      - expr: テキスト入力（`values` / `by[...]` の説明を添える）
  - フォーム変更のたびにpydantic検証を実行し、エラーは該当パーツにインライン表示
    （エンジンの読み込み時検証メッセージをそのまま出す）

### 画面3: 選択セット管理

- セット一覧（名前・件数・**どのパーツから参照されているか**）
- 新規作成 / 編集 / **別名で保存**（複製してから編集。参照は元の名前のまま変わらない）/
  削除（参照中のセットは削除不可、参照パーツ名を提示）
- セットの中身エディタ: 通常軸の値リスト、または複合軸用の軸名つき辞書リスト
  （軸ごとのプルダウン行を追加していく形）

### 画面4: スコア合成・制約

- **expression**: テキスト入力。定義済みパーツ名の一覧をクリックで挿入。
  使える関数（log, ln, min, max, mean等）のヘルプ表示。
  入力のたびに simpleeval でパースし、未定義のパーツ名参照はエラー表示
- **constraintThreshold**: パーツ名プルダウン + value + 動的制約
  （active / type=percentile / coef）の行エディタ。
  パーツ名と一致しないキー（パーツ改名時等）は警告表示

### 画面5: テスト実行・エクスポート

- テスト計算: 測定データのあるディレクトリ（画面1と同じでも別でも可）を指定し、
  `compute_score_file` を直接呼んで **Score + 全パーツ値の表**を表示。
  エラー時はエンジンのエラーメッセージ（「orderが全軸を潰していない」等）を表示
- エクスポート:
  - `score.jsonc`（ScoreFile一式。selectionSets同梱）を `st.download_button` で保存
  - パーツ単体のエクスポート（参照している選択セットを同梱）
- インポート: 既存の `score.jsonc` / optimization設定jsonc を読み込んで編集を継続

## 4. 状態管理

- `st.session_state` に以下を保持:
  - `context`: 画面1で読み込んだ定義情報（type一覧、軸と値候補、WLgroup、Generation、
    係数の有無、データディレクトリのパス）
  - `score_file`: 編集中のScoreFile相当のdict（pydanticモデルはdictとの相互変換で使用。
    dictで持つのはStreamlitのウィジェット双方向バインドと相性が良いため）
  - `selected_part`: 編集中パーツのindex
- 検証は「dict → pydantic model_validate」を編集のたびに試み、例外メッセージを
  そのままエラー表示に使う（エンジンと同一の検証を二重実装しない）
- ブラウザリロードでsession_stateは消えるため、**編集内容の自動バックアップ**として
  変更のたびに `~/.scorelib_draft.jsonc`（またはカレント）へ自動保存し、
  起動時に「前回の編集を復元しますか?」を出す

## 5. エンジン側への追加実装（scorelib/introspect.py）

UIのために以下の純粋関数を追加する（pytest対象）:

- `detect_types(data_dir) -> list[str]`:
  `parameterLabel_{type}.csv` / `dataName_{type}.csv` / `{type}.csv` の命名から
  type一覧を検出（dVtBudgetはFBC系+係数があるときに追加）
- `axis_catalog(data_dir, type) -> dict[axis, list[候補値] | None]`:
  実測軸は `{type}.csv` のヘッダ（tRのPage等、typeごとの軸差はここで自然に出る）、
  ラベル軸は `parameterLabel_{type}.csv` の列名から得る。値候補は
  `map_{軸}.csv` と実データのユニーク値から（候補なし=None は自由入力）
- `validate_score_file(dict) -> list[エラーメッセージ]`:
  UIから毎回呼ぶ検証入口（pydantic例外を人が読める形に整形）

### 5.1 type・軸情報の情報源（重要な前提）

`parameterLabel_{type}.csv` 等は**測定前には存在しない**ため、
「その実験がどのtypeを出力し、各typeがどの軸を持つか」の情報源は二段構えとする:

- **Phase1（本設計の前提）**: **同系統の過去実験の出力一式（result_tmp相当）**を
  定義ファイルディレクトリとして指定してもらう。スコア設計は同系統実験を
  繰り返す文脈で行われるため、前回出力からtype・軸・値候補が正確に得られる。
  画面1にこの前提を明記する
- **Phase2（現行GUI側のファイル整備後）**: 実験の測定設定から「出力されるtypeと
  軸・値域」を宣言するマニフェストファイルを現行GUIが提供できるようになった
  時点で、それを読むバックエンドを追加する。`detect_types` / `axis_catalog` の
  インターフェースは変えず情報源だけ差し替える（FileCatalogと同じ発想）。
  マニフェストのフォーマットはファイル整備時に別途決定する

## 6. テスト方針

- `scorelib/introspect.py`: result_tmp_mini を使ったpytest（type検出、軸候補、
  tRのPage軸など汎用性の確認）
- `ui/state.py` の編集操作（パーツ追加・複製・orderの並べ替え・セット別名保存等）:
  純粋関数としてpytest
- 画面の結線: Streamlit公式の `st.testing.v1.AppTest` によるスモークテスト
  （起動してエラーが出ない、データ読み込み→パーツ追加→エクスポートの一連が通る）

## 7. 実装順序

1. `scorelib/introspect.py` + pytest（UIの土台になる導出ロジック）
2. `ui/state.py`（編集状態の純粋ロジック）+ pytest
3. 画面1（データ読み込み）→ 画面2（パーツ編集）→ 画面3（選択セット）→
   画面4（合成・制約）→ 画面5（テスト・エクスポート）の順に実装
   （画面2が最大なので、まずfilter/mean等の基本opで通し、diff/複合軸/仮想ステップを追加）
4. AppTestスモークテスト
5. README更新（起動方法・使い方）

## 8. 確認事項（回答済み）

1. **ファイル取り込み方式**: ディレクトリパス指定 + FileCatalog抽象（3節画面1参照）。
   サーバ移行時もサーバ側パスを読む形で成立。アップロードは必要になったら
   バックエンド追加で対応
2. **UI表示言語**: 日本語
3. **orderの並べ替えUI**: 初期は**上下ボタン**。並べ替え部品は差し替え可能にしておき、
   運用後に不満があれば streamlit-sortables 等のD&Dをオプショナル導入
   （D&D部品はコミュニティ製カスタムコンポーネントで、Streamlit本体更新で
   壊れるリスクがあるため必須依存にはしない）
4. **編集内容の自動バックアップ**: あり（4節の通り）
5. **パッケージ制約**: proxyはあるがpipで入るものは基本OKとのこと。
   必須依存は streamlit のみ追加
