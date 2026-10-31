"""Regression tests on real damaged photos kept OUTSIDE the repository.

Point the environment variable PHOTO_RESTORE_SAMPLES at a folder containing the damaged JPEGs
and an ``expected.json`` describing what the restorer must find for each of them:

    {
      "some_photo.jpg": {
        "shifts":  [0, 2, 1, 1],                        # shift of every segment, in order
        "offsets": [[267, 7, -20], [39.5, -107.5, -138]], # DC offsets of segments 2.. (optional)
        "size":    [2048, 1384]                         # output width, height
      }
    }

Without the variable (or the folder) these tests are skipped, so forks run the rest of the suite.
Never commit the photos or the manifest: they belong to whoever owns the photos.
"""
import json

import numpy as np
import pytest

from photo_restore.config import Params, Paths
from photo_restore.jpegscan import Scan
from photo_restore.pipeline import process
from photo_restore.restorer import Restorer

TOL = np.array([12, 8, 8])     # quantised DC units (x Q/8 = grey levels)


def _manifest(samples_dir):
    f = samples_dir / "expected.json"
    if not f.exists():
        pytest.skip(f"no expected.json in {samples_dir}")
    return json.loads(f.read_text(encoding="utf-8"))


def test_segments(samples_dir):
    for name, exp in _manifest(samples_dir).items():
        res = Restorer(Scan(samples_dir / name), Params()).run()
        got = res.segments
        assert [s.s for s in got] == exp["shifts"], (name, [s.as_dict() for s in got])
        for s, off in zip(got[1:], exp.get("offsets", []), strict=False):
            assert (np.abs(np.array(s.off) - np.array(off)) <= TOL).all(), (name, s.as_dict(), off)


def test_output_geometry(samples_dir, tmp_path):
    from PIL import Image
    for name, exp in _manifest(samples_dir).items():
        paths = Paths(tmp_path / name)
        paths.ensure()
        rec = process(samples_dir / name, paths, Params())
        assert tuple(rec["out_size"]) == tuple(exp["size"]), name
        assert rec["status"] in ("repaired", "repaired_cropped"), (name, rec["status"])
        assert rec["filled_pct"] < 3, name
        src_exif = Image.open(samples_dir / name).getexif()
        out_exif = Image.open(tmp_path / name / rec["out_path"]).getexif()
        assert out_exif.get(0x10F) == src_exif.get(0x10F), "camera make must be preserved"
        assert out_exif.get(0x0112, 1) == src_exif.get(0x0112, 1), "orientation must be preserved"
