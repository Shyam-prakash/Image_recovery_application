import io
import os
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def photo_like(h, w, seed=0):
    """Smooth, photo-like test image: blurred noise fields, gradients and a few hard edges."""
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w, 3), np.float32)
    for scale, amp in ((16, 25), (48, 45), (160, 30)):
        small = rng.normal(0, 1, (max(2, h // scale), max(2, w // scale), 3)).astype(np.float32)
        img += amp * cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)
    yy, xx = np.mgrid[0:h, 0:w]
    img += (yy / h * 60)[..., None]
    for _ in range(6):
        x0, y0 = rng.integers(0, w), rng.integers(0, h)
        r = rng.integers(h // 12, h // 5)
        m = (xx - x0) ** 2 + (yy - y0) ** 2 < r * r
        img[m] += rng.normal(0, 40, 3)
    img = cv2.GaussianBlur(img, (0, 0), 1.2)
    return np.clip(img + 128 - img.mean(), 0, 255).astype(np.uint8)


def encode(img, sampling="H2V1", quality=85):
    if sampling == "H1V2":
        ok, enc = cv2.imencode(".jpg", img[..., ::-1], [cv2.IMWRITE_JPEG_QUALITY, quality,
                                                       cv2.IMWRITE_JPEG_SAMPLING_FACTOR,
                                                       cv2.IMWRITE_JPEG_SAMPLING_FACTOR_440])
        return enc.tobytes()
    b = io.BytesIO()
    sub = {"H2V1": "4:2:2", "H2V2": "4:2:0", "H1V1": "4:4:4"}[sampling]
    Image.fromarray(img).save(b, "JPEG", quality=quality, subsampling=sub)
    return b.getvalue()


# The regression tests in test_samples.py need four specific damaged photos that are not part
# of the repository. Point PHOTO_RESTORE_SAMPLES at the folder holding them; otherwise those
# tests are skipped.
SAMPLES = Path(os.environ.get("PHOTO_RESTORE_SAMPLES", "~/photo-restore/samples")).expanduser()


@pytest.fixture
def samples_dir():
    if not SAMPLES.is_dir():
        pytest.skip("sample photos not available (set PHOTO_RESTORE_SAMPLES)")
    return SAMPLES
