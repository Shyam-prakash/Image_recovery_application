"""Process one file: classify, restore, fill, save, write debug images and a result record."""
import json
import shutil
import struct
import time
import traceback
from pathlib import Path

import numpy as np
from PIL import Image, ImageFile

from .config import Params, Paths
from .fill import fill_holes, tint
from .jpegscan import Scan, Unsupported, to_rgb_float
from .restorer import Restorer

JPEG_EXT = {".jpg", ".jpeg", ".jpe", ".jfif"}
THUMB_W = 480


def rel_key(paths: Paths, src: Path) -> Path:
    try:
        return src.relative_to(paths.extracted)
    except ValueError:
        return Path(src.name)


def output_path(paths: Paths, src: Path, params: Params) -> Path:
    rel = rel_key(paths, src)
    if params.suffix:
        rel = rel.with_name(rel.stem + params.suffix + rel.suffix)
    return paths.restored / rel


def result_path(paths: Paths, src: Path) -> Path:
    return paths.results / (str(rel_key(paths, src)) + ".json")


def is_jpeg(src: Path) -> bool:
    with open(src, "rb") as f:
        return f.read(3) == b"\xff\xd8\xff"


# --------------------------------------------------------------------------- EXIF patching

def patch_exif_height(exif: bytes, new_h: int) -> bytes:
    """Rewrite PixelYDimension (0xA003) in a raw 'Exif\\0\\0' payload, keeping everything else."""
    try:
        b = bytearray(exif)
        t = 6
        bo = "<" if b[t:t + 2] == b"II" else ">"

        def u16(o):
            return struct.unpack(bo + "H", b[o:o + 2])[0]

        def u32(o):
            return struct.unpack(bo + "I", b[o:o + 4])[0]

        def find(ifd, tag):
            n = u16(t + ifd)
            for i in range(n):
                e = t + ifd + 2 + 12 * i
                if u16(e) == tag:
                    return e
            return None

        e = find(u32(t + 4), 0x8769)
        if e is None:
            return exif
        sub = u32(e + 8)
        e = find(sub, 0xA003)
        if e is None:
            return exif
        typ = u16(e + 2)
        if typ == 3:
            b[e + 8:e + 10] = struct.pack(bo + "H", new_h)
        elif typ == 4:
            b[e + 8:e + 12] = struct.pack(bo + "I", new_h)
        return bytes(b)
    except Exception:
        return exif


# --------------------------------------------------------------------------- thumbnails

def lenient_decode(src: Path):
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        im = Image.open(src)
        im.load()
        return im.convert("RGB")
    except Exception:
        return None


_TRANSPOSE = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180,
              4: Image.Transpose.FLIP_TOP_BOTTOM, 5: Image.Transpose.TRANSPOSE,
              6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE, 8: Image.Transpose.ROTATE_90}


def exif_orientation(src: Path) -> int:
    try:
        return int(Image.open(src).getexif().get(0x0112, 1) or 1)
    except Exception:
        return 1


def save_thumb(img, dest: Path, orientation=1):
    """Thumbnail shown upright (EXIF orientation applied, as a viewer would)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if img is None:
        img = Image.new("RGB", (THUMB_W, THUMB_W * 3 // 4), (60, 60, 60))
    im = img.copy()
    im.thumbnail((THUMB_W, THUMB_W))
    if orientation in _TRANSPOSE:
        im = im.transpose(_TRANSPOSE[orientation])
    im.save(dest, "JPEG", quality=80)


# --------------------------------------------------------------------------- main

def process(src, paths: Paths, params: Params, force=False, debug=False):
    src = Path(src)
    t0 = time.time()
    rel = rel_key(paths, src)
    rec = dict(path=str(rel), status=None, message="", orig_bytes=src.stat().st_size,
               orig_size=None, out_size=None, out_path=None, damage_points=0, segments=[],
               blanked=[], filled_pct=0.0, rows_cropped=0, confidence=None, false_positives=0,
               sampling=None, time_s=None)
    rp = result_path(paths, src)
    out = output_path(paths, src, params)
    if not force and rp.exists():
        old = json.loads(rp.read_text())
        if old.get("out_path") and Path(paths.root / old["out_path"]).exists():
            old["skipped"] = True
            return old
    try:
        if src.suffix.lower() not in JPEG_EXT and not is_jpeg(src):
            dest = paths.other / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            dest.chmod(0o644)
            rec.update(status="other", message="not a JPEG; copied to _other",
                       out_path=str(dest.relative_to(paths.root)))
            return _finish(rec, rp, t0)
        _process_jpeg(src, out, rel, rec, paths, params, debug)
    except Exception as e:      # never crash the batch on one file
        rec.update(status="failed", message=f"{type(e).__name__}: {e}",
                   traceback=traceback.format_exc(limit=6))
    return _finish(rec, rp, t0)


def _finish(rec, rp, t0):
    rec["time_s"] = round(time.time() - t0, 2)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps(rec, indent=1))
    return rec


def _copy_unchanged(src, out, rec, paths, status, message):
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, out)
    out.chmod(0o644)
    rec.update(status=status, message=message, out_path=str(out.relative_to(paths.root)),
               out_size=rec["orig_size"])


def _process_jpeg(src, out, rel, rec, paths, params, debug):
    data = src.read_bytes()
    before = lenient_decode(src)
    orient = exif_orientation(src)
    rec["orientation"] = orient
    thumbs = paths.thumbs / rel
    save_thumb(before, thumbs.with_name(thumbs.name + ".before.jpg"), orient)
    if before is not None:
        rec["orig_size"] = list(before.size)
    try:
        sc = Scan(data=data, tail_window=params.tail_window, tail_bad=params.tail_bad_markers)
    except Unsupported as e:
        _copy_unchanged(src, out, rec, paths, "unsupported", f"{e}; copied unchanged")
        save_thumb(before, thumbs.with_name(thumbs.name + ".after.jpg"), orient)
        return
    rec["orig_size"] = [sc.W, sc.H]
    rec["sampling"] = sc.hdr.sampling
    R = Restorer(sc, params)
    res = R.run()
    real = [x for x in res.damage_points if x[0] == "damage"]
    glitches = [x for x in res.damage_points if x[0] == "glitch"]
    clean = (not real and not glitches and res.tail_true is None and not res.foreign_tail
             and not sc.err[:sc.N].any() and sc.D >= sc.N and len(res.segments) == 1)
    rec["end_aligned"] = bool(R.aligned)
    rec["false_positives"] = res.false_positives
    if clean:
        _copy_unchanged(src, out, rec, paths, "clean", "no damage found; copied byte-for-byte")
        rec["confidence"] = 1.0
        save_thumb(before, thumbs.with_name(thumbs.name + ".after.jpg"), orient)
        return
    R.trim()
    rec["damage_points"] = len(real) + len(glitches)
    rec["segments"] = [s.as_dict() for s in res.segments]
    rec["blanked"] = res.blanked
    rec["notes"] = res.notes

    # ---- geometry: crop above the unrecoverable tail
    bh = 8 * sc.hdr.vmax
    H_out = sc.H
    rows_cropped = 0
    if res.tail_true is not None:
        full = res.tail_true // sc.NX
        part = res.tail_true % sc.NX
        keep_rows = full + (1 if part >= 0.75 * sc.NX else 0)
        H_cut = min(sc.H, keep_rows * bh)
        if H_cut < bh:
            # nothing usable before the tail: keep the frame, mark it grey, never write 0 rows
            res.notes.append("no usable rows before the unrecoverable data")
            H_cut = bh
        rows_cropped = sc.H - H_cut
        if not params.keep_full_size:
            H_out = H_cut
    rec["rows_cropped"] = int(rows_cropped)

    rgb_full = to_rgb_float(sc, R.final)
    rgbf = rgb_full
    hole = np.isnan(rgbf).any(-1)
    tail_mask = np.zeros_like(hole)
    if rows_cropped:
        tail_mask[sc.H - rows_cropped:] = True
    rgbf = rgbf[:H_out]
    hole = hole[:H_out]
    tail_mask = tail_mask[:H_out]
    fill_mask = hole & ~tail_mask
    rgb = np.clip(np.nan_to_num(rgbf, nan=128.0) + 0.5, 0, 255).astype(np.uint8)
    if tail_mask.any():
        rgb[tail_mask] = 128               # --keep-full-size: neutral grey, never invented content
    rgb = fill_holes(rgb, fill_mask, params.fill_maxgap)
    n_px = rgb.shape[0] * rgb.shape[1]
    rec["filled_pct"] = round(100.0 * fill_mask.sum() / n_px, 3)

    # ---- quality / status
    seams = R.residual_seams()
    rec["residual_seams"] = [round(x, 2) for x in seams]
    big_hole = _largest_hole_rows(fill_mask, bh)
    rec["largest_hole_rows"] = big_hole
    max_seam = max(seams) if seams else 0.0
    # giving up within the last two MCU rows only costs a thin strip (cropped): not a concern
    late = res.no_resync_true is not None and res.no_resync_true >= sc.N - 2 * sc.NX
    stuck = any(n.startswith("too many") or (n.startswith("no resync") and not late) for n in res.notes)
    # confidence: what can go visibly wrong, not how many damage points there were
    conf = 1.0
    conf -= 4.0 * rec["filled_pct"] / 100.0
    conf -= 0.05 * min(res.gap_segments, 4)
    conf -= 0.05 * min(max(0.0, max_seam - 4.0), 6.0)
    conf -= 0.03 * min(max(0, big_hole - 4), 10)
    conf -= 0.3 * stuck
    conf = float(np.clip(conf, 0, 1))
    rec["confidence"] = round(conf, 3)
    review = []
    if rec["filled_pct"] > params.review_fill_pct:
        review.append(f"filled {rec['filled_pct']:.1f}% > {params.review_fill_pct}%")
    if res.gap_segments >= 3:
        review.append(f"{res.gap_segments} segments placed without a row above")
    if max_seam > params.review_seam:
        review.append(f"residual seam after a damage point ({max_seam:.1f}x)")
    if stuck:
        review.append("; ".join(n for n in res.notes if n.startswith("too many") or (n.startswith("no resync") and not late)))
    total = sum(sg.t1 - sg.t0 for sg in res.segments) or 1
    weak = [sg for sg in res.segments[1:]
            if sg.score > params.review_weak_fit and (sg.t1 - sg.t0) > 0.03 * total]
    if weak:
        conf = min(conf, 0.6)
        rec["confidence"] = round(conf, 3)
        review.append(f"{len(weak)} large segment(s) placed with a weak fit (score > {params.review_weak_fit:g})")
    if big_hole > params.review_hole_rows:
        review.append(f"hole {big_hole} MCU rows tall")
    if conf < 0.5:
        review.append(f"low confidence {conf:.2f}")
    if review:
        status = "needs_review"
    elif rows_cropped:
        status = "repaired_cropped"
    else:
        status = "repaired"
    msg = []
    if rows_cropped:
        why = {"wiped": "data wiped", "foreign": "overwritten by other data"}.get(sc.tail_kind, "data missing")
        how = "grey" if params.keep_full_size else "cropped"
        msg.append(f"bottom {rows_cropped}px unrecoverable ({why}; {how})")
    msg += review
    rec["status"] = status
    rec["message"] = "; ".join(msg)

    # ---- save
    out.parent.mkdir(parents=True, exist_ok=True)
    im = Image.fromarray(rgb)
    kw = dict(quality=params.jpeg_quality, subsampling=0, optimize=True)
    if sc.hdr.exif:
        exif = sc.hdr.exif
        if H_out != sc.H:
            exif = patch_exif_height(exif, H_out)
        kw["exif"] = exif
    if sc.hdr.icc:
        kw["icc_profile"] = sc.hdr.icc
    tmp = out.with_name(out.name + ".part")
    im.save(tmp, "JPEG", **kw)
    tmp.replace(out)
    rec["out_path"] = str(out.relative_to(paths.root))
    rec["out_size"] = [rgb.shape[1], rgb.shape[0]]
    after = Image.fromarray(rgb)
    save_thumb(after, thumbs.with_name(thumbs.name + ".after.jpg"), orient)

    # ---- debug image: invented pixels tinted magenta, unrecoverable tail red/grey
    dbg = np.clip(np.nan_to_num(rgb_full, nan=0) + 0.5, 0, 255).astype(np.uint8)
    del rgb_full, rgbf
    full_fill = np.zeros(dbg.shape[:2], bool)
    full_fill[:H_out] = fill_mask
    dbg[:H_out] = rgb
    dbg = tint(dbg, full_fill, (255, 0, 255), 0.5)
    if rows_cropped:
        tm = np.zeros(dbg.shape[:2], bool)
        tm[sc.H - rows_cropped:] = True
        dbg[tm] = 40
        dbg = tint(dbg, tm, (255, 0, 0), 0.3)
    dbg_path = paths.debug / (str(rel) + ".png")
    dbg_path.parent.mkdir(parents=True, exist_ok=True)
    dimg = Image.fromarray(dbg)
    if not debug and max(dimg.size) > 1600:
        dimg.thumbnail((1600, 1600))
    dimg.save(dbg_path, optimize=False, compress_level=3)
    rec["debug_path"] = str(dbg_path.relative_to(paths.root))
    save_thumb(Image.fromarray(dbg), thumbs.with_name(thumbs.name + ".debug.jpg"), orient)


def _largest_hole_rows(mask, bh):
    if not mask.any():
        return 0
    rows = mask.any(1)
    best = cur = 0
    for r in rows:
        cur = cur + 1 if r else 0
        best = max(best, cur)
    return int(np.ceil(best / bh))
