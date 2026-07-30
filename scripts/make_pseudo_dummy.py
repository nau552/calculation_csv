# Copyright (c) 2026
"""正データから疑似ダミー一式を作る(Board/Chip を1つに削る)。

担当者のダミー一式が納品される前に、ダミー展開フロー
(docs/spec_change_dataname_measure.md 9節・プラン4)を試すための開発用。

    python scripts/make_pseudo_dummy.py <正データdir> <出力dir>
    例: python scripts/make_pseudo_dummy.py result_tmp dummy_bundle
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scorelib_param.dummy import make_pseudo_dummy

if __name__ == "__main__":
    EXPECTED_ARGC = 3  # スクリプト名 + 正データ dir + 出力 dir
    if len(sys.argv) != EXPECTED_ARGC:
        print(__doc__)
        sys.exit(1)
    dest = make_pseudo_dummy(sys.argv[1], sys.argv[2])
    print(f"疑似ダミー一式を作成しました: {dest}")
