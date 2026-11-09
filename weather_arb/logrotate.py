"""Size-based log rotation for the launchd job logs (copytruncate).

The tracker/watchdog/divergence services write to ``logs/*.log`` via launchd's
``StandardOutPath``, which keeps the file open for the life of the process. A
plain ``mv`` would therefore not free space -- launchd would keep writing to the
moved inode. So this uses **copytruncate**: copy the log to a rotated, gzip'd
generation, then truncate the original *in place*, leaving launchd's open file
descriptor valid (it appends to the now-empty file). A tiny window of writes
between copy and truncate can be lost -- fine for logs.

Generations are ``<log>.1.gz`` (newest) .. ``<log>.<keep>.gz`` (oldest); each
rotation shifts them up and drops the oldest. Only files at/over ``max_bytes``
rotate. Dependency-free; run from launchd on a daily schedule.

    python -m weather_arb.logrotate [--dir logs] [--max-mb 50] [--keep 5]
"""

from __future__ import annotations

import argparse
import gzip
import os
import shutil
from pathlib import Path
from typing import Optional


def _gen(path: Path, i: int) -> Path:
    return path.with_name(f"{path.name}.{i}.gz")


def rotate_file(path: Path, max_bytes: int, keep: int) -> bool:
    """Rotate ``path`` if it is at least ``max_bytes``. Returns True if rotated.

    Shifts ``.i.gz`` generations up (dropping ``.keep.gz``), writes the current
    contents to ``.1.gz``, then truncates the original to zero in place."""
    if keep < 1 or not path.is_file() or path.stat().st_size < max_bytes:
        return False

    oldest = _gen(path, keep)
    if oldest.exists():
        oldest.unlink()
    for i in range(keep - 1, 0, -1):
        src = _gen(path, i)
        if src.exists():
            src.rename(_gen(path, i + 1))

    # copytruncate: gzip current contents into .1.gz, then truncate the original.
    with path.open("rb") as src, gzip.open(_gen(path, 1), "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.truncate(path, 0)
    return True


def rotate_dir(log_dir: Path, max_bytes: int, keep: int,
               patterns: tuple[str, ...] = ("*.log",)) -> list[Path]:
    """Rotate every oversized log in ``log_dir`` matching ``patterns``."""
    rotated: list[Path] = []
    for pattern in patterns:
        for path in sorted(log_dir.glob(pattern)):
            if rotate_file(path, max_bytes, keep):
                rotated.append(path)
    return rotated


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Rotate oversized logs (copytruncate + gzip).")
    parser.add_argument("--dir", default="logs", help="log directory (default: logs)")
    parser.add_argument("--max-mb", type=float, default=50.0,
                        help="rotate files at/over this size in MB (default 50)")
    parser.add_argument("--keep", type=int, default=5, help="compressed generations to keep")
    args = parser.parse_args(argv)

    log_dir = Path(args.dir)
    if not log_dir.is_dir():
        print(f"logrotate: no such directory: {log_dir}")
        return 0
    rotated = rotate_dir(log_dir, int(args.max_mb * 1024 * 1024), max(1, args.keep))
    if rotated:
        print("rotated: " + ", ".join(str(p) for p in rotated))
    else:
        print("logrotate: nothing over threshold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
