"""自作スコアパーツ関数（type="custom"）の登録ファイル。

ここに書いた関数は、config の score_parts で

    {"name": "my_score", "type": "custom"}                       // name と同名の関数を呼ぶ
    {"name": "s2", "type": "custom", "function": "my_score",
     "params": {"threshold": 3}}                                 // 関数名を明示・params も渡せる

のように参照でき、通常の集計パイプラインの代わりに関数の戻り値（1つの有限な数値）が
そのパーツの値になる。エンジンは常に**リポジトリ直下のこのファイル**を読む
（設定ファイルからパスを与える形にはしない: 実験入力から任意コードが実行できて
しまうため。関数の追加・変更は SVN コミット=レビューを通す）。

関数の書き方:

    import polars as pl

    def my_score(ctx):
        # ctx.data_dir   : 測定結果ディレクトリ (Path)。csv を自由に読める
        # ctx.generation : Generation 文字列 (無ければ None)
        # ctx.group_defs : グループ定義 name -> GroupDef
        # ctx.params     : config の params 辞書
        df = pl.read_csv(ctx.data_dir / "FBC.csv")
        return float(df["FBC"].mean())

アンダースコア始まりの関数は UI の一覧に出ない（ヘルパー用）。
"""
