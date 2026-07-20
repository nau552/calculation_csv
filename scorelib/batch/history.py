"""過去実験 result_history の列挙と Epoch 識別子の生成。

過去実験は次の構造を持つ（docs/batch_design.md 3.1節）:

    <実験ログディレクトリ>/Step{N}/Loop{NN}/result_history/result.{NNNN}/

result.NNNN 1つが 1 epoch 分の測定結果（result_tmp 相当）。
複数の result_history を同時に扱うため、各 history に一意な**ラベル**を
付け、epoch は「ラベル#番号」（例: "expA/Step1/Loop01#0001"）で識別する。
このラベル#番号の文字列が、バッチ計算でパイプラインを流れる識別軸
`Epoch` の値になる（compute.EPOCH_COL）。
"""
from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Union

# result_history 内の epoch ディレクトリ名（例: result.0001）
RESULT_DIR_RE = re.compile(r"^result\.(\d+)$")


@dataclass(frozen=True)
class EpochRef:
    """1 epoch 分のデータへの参照（まだステージングされていない状態）。"""

    label: str  # result_history のラベル（例: "expA/Step1/Loop01"）
    epoch_no: int  # result.NNNN の番号
    source_dir: Path  # result.NNNN ディレクトリの実体

    @property
    def epoch_id(self) -> str:
        """識別軸 Epoch に入る一意な値。"""
        return f"{self.label}#{self.epoch_no:04d}"


def derive_label(history_path: Union[str, Path]) -> str:
    """result_history のパスからデフォルトのラベルを導出する:
    `{実験ログ名}/Step{N}/Loop{NN}`（親を3段さかのぼる）。
    Step/Loop 構造が想定と違う場合は警告した上で同じ規則の名前を使う
    （呼び出し側でラベルを明示指定すれば警告は出ない）。"""
    p = Path(history_path).resolve()
    loop, step, exp = p.parent, p.parent.parent, p.parent.parent.parent
    names = [n for n in (exp.name, step.name, loop.name) if n]
    if not (loop.name.startswith("Loop") and step.name.startswith("Step")):
        warnings.warn(
            f"result_history path does not follow the <exp>/Step*/Loop*/ layout: {p} "
            f"(using label '{'/'.join(names)}'; pass an explicit label to silence this)"
        )
    return "/".join(names) if names else p.name


def enumerate_epochs(
    histories: Union[Sequence[Union[str, Path]], Mapping[str, Union[str, Path]]],
) -> List[EpochRef]:
    """result_history のリスト（または {ラベル: パス} 辞書）から全 epoch を
    列挙する。リストの場合ラベルは `derive_label` で導出する。

    - ラベルの重複はエラー（黙って連番を振らない）
    - `result.NNNN` 以外のエントリは無視（警告のみ）
    - epoch を1つも含まない history はエラー（パス間違いの可能性が高い）
    """
    if isinstance(histories, Mapping):
        labeled = {str(label): Path(path) for label, path in histories.items()}
    else:
        labeled = {}
        for path in histories:
            label = derive_label(path)
            if label in labeled:
                raise ValueError(
                    f"duplicate history label '{label}' (from {labeled[label]} and {path}); "
                    "pass explicit labels as a {label: path} mapping"
                )
            labeled[label] = Path(path)

    refs: List[EpochRef] = []
    for label, path in labeled.items():
        if not path.is_dir():
            raise ValueError(f"result_history not found (label '{label}'): {path}")
        found: Dict[int, Path] = {}
        ignored: List[str] = []
        for entry in sorted(path.iterdir()):
            m = RESULT_DIR_RE.match(entry.name)
            if m and entry.is_dir():
                found[int(m.group(1))] = entry
            else:
                ignored.append(entry.name)
        if ignored:
            warnings.warn(
                f"ignoring non-epoch entries in {path}: {ignored[:10]}"
                + (" ..." if len(ignored) > 10 else "")
            )
        if not found:
            raise ValueError(f"no result.NNNN epoch directories in {path} (label '{label}')")
        refs.extend(
            EpochRef(label=label, epoch_no=no, source_dir=dir_)
            for no, dir_ in sorted(found.items())
        )
    return refs
