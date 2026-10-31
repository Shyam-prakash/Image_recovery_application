"""Fill blanked MCUs: vertical linear interpolation, slight horizontal blur, Telea fallback."""
import cv2
import numpy as np


def vfill(img, mask, maxgap=64):
    """Band-wise wrapper around _vfill: only rows near holes are processed (saves memory)."""
    out = img.astype(np.float32)
    left = np.zeros_like(mask)
    rows = np.nonzero(mask.any(1))[0]
    if not len(rows):
        return out, left
    # split into runs of hole rows separated by more than 2*maxgap clean rows
    brk = np.nonzero(np.diff(rows) > 2 * maxgap + 2)[0]
    starts = np.r_[rows[0], rows[brk + 1]]
    ends = np.r_[rows[brk], rows[-1]]
    H = mask.shape[0]
    for r0, r1 in zip(starts, ends, strict=True):
        a, b = max(0, r0 - maxgap - 1), min(H, r1 + maxgap + 2)
        filled, unfilled = _vfill(img[a:b], mask[a:b], maxgap)
        out[a:b] = np.where(mask[a:b, :, None], filled, out[a:b])
        left[a:b] |= unfilled
    return out, left


def _vfill(img, mask, maxgap=64):
    """Fill masked pixels by linear interpolation between the nearest valid pixels above and
    below in the same column. Returns (filled image, mask of pixels still unfilled)."""
    H, W = mask.shape
    rows = np.broadcast_to(np.arange(H, dtype=np.int32)[:, None], (H, W))
    valid = ~mask
    above = np.where(valid, rows, np.int32(-1))
    above = np.maximum.accumulate(above, axis=0)
    below = np.where(valid, rows, np.int32(H))
    below = np.minimum.accumulate(below[::-1], axis=0)[::-1]
    # for masked pixels: above index is last valid row above (or -1), below is next valid (or H)
    can = mask & (above >= 0) & (below < H) & ((below - above - 1) <= maxgap)
    out = img.astype(np.float32).copy()
    if can.any():
        ya, yb = above[can], below[can]
        xs = np.nonzero(can)[1]
        w = ((np.nonzero(can)[0] - ya) / (yb - ya)).astype(np.float32)[:, None]
        out[can] = img[ya, xs].astype(np.float32) * (1 - w) + img[yb, xs].astype(np.float32) * w
    return out, mask & ~can


def fill_holes(rgb, mask, maxgap=64):
    """rgb: uint8 (H, W, 3); mask: bool, True = invented. Returns uint8 image."""
    if not mask.any():
        return rgb
    out, left = vfill(rgb, mask, maxgap)
    blur = cv2.GaussianBlur(out, (9, 1), 0)
    out = np.where(mask[..., None], blur, out)
    out = np.clip(out + 0.5, 0, 255).astype(np.uint8)
    if left.any():
        bgr = cv2.cvtColor(out, cv2.COLOR_RGB2BGR)
        bgr = cv2.inpaint(bgr, left.astype(np.uint8) * 255, 6, cv2.INPAINT_TELEA)
        out = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    return out


def tint(rgb, mask, color=(255, 0, 255), alpha=0.45):
    out = rgb.astype(np.float32)
    c = np.array(color, np.float32)
    out[mask] = out[mask] * (1 - alpha) + c * alpha
    return out.astype(np.uint8)
