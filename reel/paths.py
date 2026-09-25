"""Resolving paths so nothing outside an allowed folder is ever touched.

Scanning, playback and pictures all go through resolve_inside(): it follows
symlinks and ".." and then insists the result is still inside the root. Checked
each time a file is used, not just once, since files can change after a scan.
"""
import os
from pathlib import Path


class OutsideRoot(Exception):
    """The path resolves to somewhere outside the allowed folder."""


def resolve_inside(root: Path, rel: str | os.PathLike, *, must_exist: bool = True) -> Path:
    """The real path of `rel` under `root`, or OutsideRoot if it escapes.

    Symlinks that stay inside the root are fine; ones that lead out are not.
    """
    real_root = Path(root).resolve(strict=must_exist)
    target = (real_root / rel).resolve(strict=must_exist)
    if not target.is_relative_to(real_root):
        raise OutsideRoot(f"{rel} is outside {root}")
    return target


def is_inside(root: Path, rel: str | os.PathLike) -> bool:
    try:
        resolve_inside(root, rel)
        return True
    except (OutsideRoot, OSError):
        return False
