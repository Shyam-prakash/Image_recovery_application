"""Batch pool, background jobs and path handling: the parts that differ between Windows and Linux/WSL."""
import json
import time
from pathlib import Path

import pytest

from conftest import encode, photo_like
from photo_restore import jobs
from photo_restore.batch import restore
from photo_restore.config import Params, Paths
from photo_restore.report import build
from photo_restore.sysutil import IS_WINDOWS, mp_context, to_local_path


def _damaged(seed):
    """A JPEG with 16 overwritten bytes in the middle of its scan data."""
    clean = bytearray(encode(photo_like(256, 384, seed=seed), "H2V1"))
    sos = clean.index(b"\xff\xda")
    p = sos + (len(clean) - sos) // 2
    clean[p:p + 16] = bytes([0x5A, 0xC3, 0x12, 0x99, 0x77, 0x31, 0x08, 0xEE] * 2)
    return bytes(clean)


@pytest.fixture
def project(tmp_path):
    paths = Paths(tmp_path / "root")
    paths.ensure()
    d = paths.extracted / "set one" / "sub ü"          # spaces and non-ASCII on purpose
    d.mkdir(parents=True)
    for i in range(3):
        (d / f"photo {i}.jpg").write_bytes(_damaged(i))
    (d / "clean.jpg").write_bytes(encode(photo_like(128, 128, seed=9), "H2V1"))
    (d / "notes.txt").write_text("not a photo")
    return paths


def test_parallel_batch_and_report(project):
    res = restore(project, Params(), jobs=2)
    statuses = sorted(r["status"] for r in res)
    assert len(res) == 5
    assert statuses.count("other") == 1 and statuses.count("clean") == 1
    assert all(s in ("repaired", "repaired_cropped", "needs_review") for s in statuses
               if s not in ("other", "clean"))
    recs, counts = build(project)
    assert (project.report / "index.html").exists()
    assert sum(counts.values()) == 5
    # resumable: a second run skips everything
    again = restore(project, Params(), jobs=2)
    assert all(r.get("skipped") for r in again if r["status"] != "other")


def test_background_job_runs_steps(project):
    info = jobs.start(project, [["restore", "--jobs", "1"], ["report"]])
    deadline = time.time() + 240
    while time.time() < deadline:
        st = jobs.status(project)
        if not st["running"]:
            break
        time.sleep(1)
    st = jobs.status(project)
    assert not st["running"], jobs.log_tail(st, 30)
    assert (project.report / "report.json").exists(), jobs.log_tail(info, 30)
    recs = json.loads((project.report / "report.json").read_text(encoding="utf-8"))
    assert len(recs) == 5


def test_stop_without_job_is_harmless(project):
    assert jobs.stop(project) is False


def test_mp_context_is_usable():
    assert mp_context().get_start_method() in ("forkserver", "spawn")


def test_local_paths():
    assert to_local_path('  "~/x.zip" ') == Path("~/x.zip").expanduser()
    if IS_WINDOWS:
        assert to_local_path(r"C:\Users\me\a.zip") == Path(r"C:\Users\me\a.zip")
    else:
        p = to_local_path(r"C:\Users\me\a b.zip")
        assert str(p).endswith("Users/me/a b.zip") and str(p).startswith("/mnt/c")
