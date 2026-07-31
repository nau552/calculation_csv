# Copyright (c) 2026
"""scorelib_param: GUIで設計したスコア定義を計算するエンジン。

__version__ はエンジンのリリース版数。scorelib_param/ を SVN のスクリプト領域へ
同期登録するたびに上げる(開発の正は git、SVN には実行用スナップショットのみ
置く — docs/score_gui_ui_design.md 2.1節「配置・起動形態」参照)。
設計UIのサイドバーと CLI(stderr / --version)に表示され、UIに同梱された
エンジンと SVN 側エンジンの版ズレに気づくための目印になる。
"""

# 版上げは【この1行だけ】変えればよい。pyproject.toml は dynamic 設定で
# ここを参照しており、UIサイドバー・CLI(--version / stderr)もここを表示する
__version__ = "0.7.1"
