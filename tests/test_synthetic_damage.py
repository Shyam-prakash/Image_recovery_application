"""Damage a clean JPEG's scan data and check the restorer gets the image back."""
import numpy as np
import pytest

from conftest import encode, photo_like
from photo_restore.config import Params, Paths
from photo_restore.jpegscan import Scan, to_rgb_float
from photo_restore.pipeline import process
from photo_restore.restorer import Restorer


def _safe_pos(data, p):
    """A position whose deletion really changes the bitstream (not part of an FF00 pair)."""
    while 0xFF in data[p - 1:p + 2]:
        p += 1
    return p


def _scan_bounds(data):
    sos = data.index(b"\xff\xda")
    L = int.from_bytes(data[sos + 2:sos + 4], "big")
    return sos + 2 + L, len(data) - 2


def _restore(data):
    sc = Scan(data=data)
    R = Restorer(sc, Params())
    res = R.run()
    R.trim()
    return sc, R, res


def _reference(img_bytes):
    sc = Scan(data=img_bytes)
    cv = sc.blank()
    sc.render(cv, 0, sc.D, 0, [0, 0, 0])
    return to_rgb_float(sc, cv)


def _check(clean, damaged, max_hole_pct=4.0):
    ref = _reference(clean)
    sc, R, res = _restore(damaged)
    out = to_rgb_float(sc, R.final)
    hole = np.isnan(out).any(-1)
    assert 100 * hole.mean() < max_hole_pct, f"holes {100 * hole.mean():.1f}%"
    # error outside the holes must be small: placement and DC offsets recovered
    err = np.abs(out - ref)[~hole].mean()
    assert err < 2.5, f"mean error {err:.2f}, segments {[s.as_dict() for s in res.segments]}"
    return res


@pytest.fixture(scope="module")
def clean():
    img = photo_like(768, 1024, seed=7)
    return encode(img, "H2V1")


@pytest.mark.parametrize("frac", [0.3, 0.55, 0.8])
def test_bit_flips(clean, frac):
    a, b = _scan_bounds(clean)
    d = bytearray(clean)
    p = a + int((b - a) * frac)
    rng = np.random.default_rng(int(frac * 100))
    for _ in range(4):
        i = p + int(rng.integers(0, 40))
        d[i] ^= 1 << int(rng.integers(0, 8))
    _check(clean, bytes(d))


@pytest.mark.parametrize("frac", [0.35, 0.7])
def test_overwrite_bytes(clean, frac):
    a, b = _scan_bounds(clean)
    d = bytearray(clean)
    p = a + int((b - a) * frac)
    d[p:p + 12] = bytes([0x5A, 0xC3, 0xFF, 0x12, 0x00, 0x99, 0xFF, 0x77, 0x31, 0x08, 0xEE, 0x42])
    res = _check(clean, bytes(d))
    assert len([x for x in res.damage_points if x[0] == "damage"]) >= 1


@pytest.mark.parametrize("frac", [0.4, 0.6])
def test_deleted_byte(clean, frac):
    a, b = _scan_bounds(clean)
    p = _safe_pos(clean, a + int((b - a) * frac))
    damaged = clean[:p] + clean[p + 1:]
    _check(clean, damaged)


def test_two_damage_points(clean):
    a, b = _scan_bounds(clean)
    p1, p2 = a + int((b - a) * 0.3), _safe_pos(clean, a + int((b - a) * 0.65))
    d = clean[:p2] + clean[p2 + 1:]
    d = bytearray(d)
    d[p1:p1 + 6] = b"\x13\x37\xc0\xde\x55\xaa"
    res = _check(clean, bytes(d))
    assert len(res.segments) >= 3


def test_clean_file_copied_unchanged(tmp_path, clean):
    root = tmp_path / "root"
    paths = Paths(root)
    paths.ensure()
    src = paths.extracted / "z" / "clean photo ü.jpg"
    src.parent.mkdir(parents=True)
    src.write_bytes(clean)
    rec = process(src, paths, Params())
    assert rec["status"] == "clean"
    assert (root / rec["out_path"]).read_bytes() == clean


def test_damaged_file_pipeline(tmp_path, clean):
    a, b = _scan_bounds(clean)
    p = _safe_pos(clean, a + int((b - a) * 0.4))
    damaged = clean[:p] + clean[p + 1:]
    paths = Paths(tmp_path)
    paths.ensure()
    src = paths.extracted / "z" / "d.jpg"
    src.parent.mkdir(parents=True)
    src.write_bytes(damaged)
    rec = process(src, paths, Params())
    assert rec["status"] in ("repaired", "needs_review"), rec
    assert rec["out_size"] == [1024, 768]
    assert (tmp_path / rec["debug_path"]).exists()
    # resumable: second call skips
    rec2 = process(src, paths, Params())
    assert rec2.get("skipped")


def test_non_jpeg_goes_to_other(tmp_path):
    paths = Paths(tmp_path)
    paths.ensure()
    src = paths.extracted / "z" / ".nomedia"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"")
    rec = process(src, paths, Params())
    assert rec["status"] == "other"
    assert (paths.other / "z" / ".nomedia").exists()


def test_failed_file_is_recorded(tmp_path):
    paths = Paths(tmp_path)
    paths.ensure()
    src = paths.extracted / "broken.jpg"
    src.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 10)
    rec = process(src, paths, Params())
    assert rec["status"] in ("failed", "unsupported")
