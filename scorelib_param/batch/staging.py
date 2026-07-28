"""epoch データのステージング: アーカイブ展開・事前検証・削除。

方針（docs/batch_design.md 3.2節）:

- 非圧縮 `.csv` と gzip 単体の `.csv.gz` は polars がそのまま読めるため
  何もしない（axis_resolve.data_file が両方を解決する）。
- `.tar.gz` / `.tgz` / `.tar` / `.zip`（複数ファイルのアーカイブ）だけは
  polars では読めないため、ステージング領域に**ビューディレクトリ**を作り、
  「アーカイブの展開結果 + 非アーカイブファイルへのシンボリックリンク
  （symlink 不可の環境ではコピー）」で result_tmp 相当の形にしてから
  エンジンに渡す。エンジンは圧縮の存在を知らない。
- 展開が不要な epoch はビューを作らず元ディレクトリをそのまま使う
  （ステージングも削除も発生しない）。
- **入力元のファイルは一切変更・削除しない**。削除できるのは
  このモジュールが作ったビューディレクトリだけ（cleanup_epoch）。
"""
from __future__ import annotations

import shutil
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from .. import axis_resolve
from .history import EpochRef

TAR_SUFFIXES = (".tar.gz", ".tgz", ".tar")
ZIP_SUFFIXES = (".zip",)
ARCHIVE_SUFFIXES = TAR_SUFFIXES + ZIP_SUFFIXES


@dataclass
class StagedEpoch:
    """計算に渡せる状態になった 1 epoch。error が入っている場合は
    計算せずスキップ対象（skip-and-report）。"""

    ref: EpochRef
    data_dir: Path  # エンジンに渡すディレクトリ（元 or ビュー）
    created_dir: Optional[Path] = None  # ステージングとして作った場合のみ（削除対象）
    error: Optional[str] = None


def _is_archive(name: str) -> bool:
    return name.lower().endswith(ARCHIVE_SUFFIXES)


def _check_member_name(name: str, archive: Path) -> None:
    """展開先の外に書き出すエントリ（絶対パス・..）を拒否する。"""
    p = Path(name)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"unsafe path '{name}' in archive {archive}")


def _extract_archive(archive: Path, dest: Path) -> None:
    if archive.name.lower().endswith(TAR_SUFFIXES):
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                _check_member_name(member.name, archive)
            # filter="data": 展開先脱出・特殊ファイル等を tarfile 側でも拒否する
            # （上の自前チェックの多重防御 + Python 3.14 で必須になる引数の先回り）
            tf.extractall(dest, filter="data")
    else:
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                _check_member_name(name, archive)
            zf.extractall(dest)


def _flatten_single_dir(view: Path) -> None:
    """`tar czf x.tar.gz result.0001/` のようにディレクトリごと固められて
    いた場合の救済: ビュー直下が「1ディレクトリのみ」なら中身を持ち上げる。"""
    entries = list(view.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for item in inner.iterdir():
            item.rename(view / item.name)
        inner.rmdir()


def _link_or_copy(src: Path, dst: Path) -> None:
    try:
        dst.symlink_to(src)
    except OSError:
        # Windows で権限が無い場合など。コピーでも正しさは変わらない
        shutil.copy2(src, dst)


def _view_dir_for(ref: EpochRef, staging_root: Path) -> Path:
    safe_label = ref.label.replace("/", "_").replace("\\", "_").replace(":", "_")
    return staging_root / safe_label / f"result.{ref.epoch_no:04d}"


def stage_epoch(ref: EpochRef, staging_root: Path) -> StagedEpoch:
    """1 epoch をエンジンが読める形にする。例外は投げず error に落とす
    （skip-and-report の入り口。呼び出し側が strict を判断する）。"""
    try:
        source = ref.source_dir
        if not source.is_dir():
            return StagedEpoch(ref, source, error=f"epoch directory not found: {source}")
        files = [p for p in source.iterdir() if p.is_file()]
        archives = [p for p in files if _is_archive(p.name)]
        if not archives:
            return StagedEpoch(ref, source)

        view = _view_dir_for(ref, staging_root)
        if view.exists():
            shutil.rmtree(view)
        view.mkdir(parents=True)
        try:
            for archive in archives:
                _extract_archive(archive, view)
            _flatten_single_dir(view)
            for f in files:
                if not _is_archive(f.name) and not (view / f.name).exists():
                    _link_or_copy(f, view / f.name)
        except Exception:
            shutil.rmtree(view, ignore_errors=True)
            raise
        return StagedEpoch(ref, view, created_dir=view)
    except Exception as err:  # noqa: BLE001 — 理由ごと報告してスキップさせる
        return StagedEpoch(ref, ref.source_dir, error=f"staging failed: {err}")


def validate_epoch(
    staged: StagedEpoch, required_types: Sequence[str], needs_dvtbudget: bool
) -> Optional[str]:
    """計算前の安価な検証。エラー文字列（skip理由）か None を返す。
    固定リストではなく「config が参照する type」駆動（docs/batch_design.md 8節）。"""
    if staged.error:
        return staged.error
    missing: List[str] = []
    for type_ in required_types:
        if not axis_resolve.data_file(staged.data_dir, f"{type_}.csv").exists():
            missing.append(f"{type_}.csv")
    if needs_dvtbudget and not axis_resolve.data_file(
        staged.data_dir, "initial_temperature.csv"
    ).exists():
        missing.append("initial_temperature.csv")
    if missing:
        return f"missing files: {', '.join(missing)} (in {staged.data_dir})"
    return None


def cleanup_epoch(staged: StagedEpoch) -> None:
    """このモジュールが作ったビューディレクトリだけを削除する。"""
    if staged.created_dir is not None:
        shutil.rmtree(staged.created_dir, ignore_errors=True)
