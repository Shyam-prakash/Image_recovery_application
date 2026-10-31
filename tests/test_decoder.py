import io

import numpy as np
import pytest
from PIL import Image

from conftest import encode, photo_like
from photo_restore.jpegscan import Scan, Unsupported, find_foreign_tail, to_rgb


@pytest.mark.parametrize("sampling", ["H2V1", "H1V2", "H2V2", "H1V1"])
@pytest.mark.parametrize("shape", [(480, 640), (640, 480), (301, 437)])
def test_matches_pillow(sampling, shape):
    img = photo_like(*shape, seed=1)
    data = encode(img, sampling)
    sc = Scan(data=data)
    assert sc.err[:sc.N].sum() == 0
    assert sc.D >= sc.N
    cv = sc.blank()
    sc.render(cv, 0, sc.D, 0, [0, 0, 0])
    ours = to_rgb(sc, cv).astype(int)
    ref = np.asarray(Image.open(io.BytesIO(data)).convert("RGB")).astype(int)
    assert ours.shape == ref.shape
    diff = np.abs(ours - ref)
    assert diff.mean() <= 2.0, diff.mean()
    assert diff.reshape(-1, 3).mean(0).max() <= 2.0


def test_sampling_detected():
    img = photo_like(64, 64)
    assert Scan(data=encode(img, "H2V1")).hdr.sampling == "2x11x11x1"
    assert Scan(data=encode(img, "H1V2")).hdr.sampling == "1x21x11x1"


def test_progressive_unsupported():
    b = io.BytesIO()
    Image.fromarray(photo_like(64, 64)).save(b, "JPEG", progressive=True)
    with pytest.raises(Unsupported, match="progressive"):
        Scan(data=b.getvalue())


def test_never_aborts_on_garbage():
    data = bytearray(encode(photo_like(256, 256), "H2V1"))
    rng = np.random.default_rng(3)
    start = data.index(b"\xff\xda") + 20
    data[start + 100:start + 400] = rng.integers(0, 256, 300, dtype=np.uint8).tobytes()
    sc = Scan(data=bytes(data))
    assert sc.D > 0
    assert sc.D >= sc.N // 2      # kept decoding past the garbage


def test_foreign_tail_detection():
    good = encode(photo_like(1024, 1024, seed=4), "H2V1", quality=95)
    rng = np.random.default_rng(5)
    sos = good.index(b"\xff\xda")
    cut = sos + (len(good) - sos) * 2 // 3
    junk = rng.integers(0, 256, len(good) - cut, dtype=np.uint8)
    # unrelated data: no FF00 stuffing
    junk[(junk[:-1] == 0xFF).nonzero()[0] + 1] |= 1
    data = good[:cut] + junk.tobytes()
    raw = data[sos + 14:]
    off = find_foreign_tail(raw, base=sos + 14)
    assert off is not None
    assert abs((off + sos + 14) - cut) < 4096
    assert find_foreign_tail(good[sos + 14:]) is None
