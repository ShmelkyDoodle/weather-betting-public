"""Log rotation: copytruncate + gzip generations, threshold, oldest-drop (offline)."""
import asyncio, gzip, sys, tempfile
from pathlib import Path
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb.logrotate import rotate_file, rotate_dir

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)


def _write_gz(path: Path, data: bytes):
    with gzip.open(path, "wb") as f:
        f.write(data)


def _read_gz(path: Path) -> bytes:
    with gzip.open(path, "rb") as f:
        return f.read()


def test_rotate_and_shift():
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "tracker.out.log"
        log.write_bytes(b"X" * 2000)
        _write_gz(Path(str(log) + ".1.gz"), b"old1")
        _write_gz(Path(str(log) + ".2.gz"), b"old2")
        check("rotated (over threshold)", rotate_file(log, max_bytes=1000, keep=3) is True)
        check("original truncated in place", log.exists() and log.stat().st_size == 0)
        check(".1.gz holds the rotated contents", _read_gz(Path(str(log) + ".1.gz")) == b"X" * 2000)
        check("old .1 shifted to .2", _read_gz(Path(str(log) + ".2.gz")) == b"old1")
        check("old .2 shifted to .3", _read_gz(Path(str(log) + ".3.gz")) == b"old2")


def test_oldest_dropped():
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "b.log"
        log.write_bytes(b"Y" * 2000)
        _write_gz(Path(str(log) + ".1.gz"), b"g1")
        _write_gz(Path(str(log) + ".2.gz"), b"g2")   # keep=2 -> this is dropped
        rotate_file(log, max_bytes=1000, keep=2)
        check("new .1.gz current", _read_gz(Path(str(log) + ".1.gz")) == b"Y" * 2000)
        check(".2.gz is old g1", _read_gz(Path(str(log) + ".2.gz")) == b"g1")
        check("no .3.gz beyond keep", not Path(str(log) + ".3.gz").exists())


def test_below_threshold_untouched():
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "a.log"
        log.write_bytes(b"tiny")
        check("small file not rotated", rotate_file(log, max_bytes=1000, keep=3) is False)
        check("small file untouched", log.read_bytes() == b"tiny")
        check("no .1.gz created", not Path(str(log) + ".1.gz").exists())


def test_rotate_dir_selective():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "big.log").write_bytes(b"Z" * 2000)
        (Path(d) / "small.log").write_bytes(b"z")
        rotated = rotate_dir(Path(d), max_bytes=1000, keep=3)
        check("only the oversized log rotated", [p.name for p in rotated] == ["big.log"])


async def main():
    test_rotate_and_shift(); test_oldest_dropped()
    test_below_threshold_untouched(); test_rotate_dir_selective()
    print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)

asyncio.run(main())
