"""Tolerant baseline JPEG decoder.

The decoder never aborts. It records, for every decoded MCU, its start bit position, an
error flag and the raw DC differences; AC coefficients are turned into "AC-only" pixel
blocks straight away, so any range of MCUs can later be rendered at any MCU position with
any DC offset just by adding a constant per block.
"""
import struct

import numpy as np
from numba import njit

ZIGZAG = np.array([
    0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5, 12, 19, 26, 33, 40, 48, 41, 34, 27,
    20, 13, 6, 7, 14, 21, 28, 35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51, 58,
    59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63], np.int64)


class Unsupported(Exception):
    pass


def _idct_matrix():
    C = np.zeros((8, 8))
    for k in range(8):
        for n in range(8):
            C[k, n] = (np.sqrt(1 / 8) if k == 0 else np.sqrt(2 / 8)) * np.cos(np.pi * (2 * n + 1) * k / 16)
    return C


IDCT = _idct_matrix()


# --------------------------------------------------------------------------- header parsing

class Header:
    """Parsed JPEG header: tables, frame, scan, APP segments."""

    def __init__(self, data: bytes):
        self.data = data
        self.qt = {}
        self.ht = {}           # (class, id) -> (counts[16], symbols)
        self.comps = []        # dicts: id, h, v, tq, td, ta
        self.W = self.H = 0
        self.sof = None
        self.dri = 0
        self.exif = None       # raw APP1 payload incl. "Exif\0\0"
        self.icc = b""
        self.app_markers = []
        self.scan_start = None
        self._parse()

    def _parse(self):
        d = self.data
        if d[:2] != b"\xff\xd8":
            raise Unsupported("not a JPEG (no SOI)")
        i = 2
        icc_chunks = []
        while i < len(d) - 3:
            if d[i] != 0xFF:
                raise Unsupported(f"bad marker structure at {i}")
            m = d[i + 1]
            if m == 0xFF:
                i += 1
                continue
            if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                i += 2
                continue
            if m == 0xD9:
                raise Unsupported("EOI before SOS")
            L = struct.unpack(">H", d[i + 2:i + 4])[0]
            seg = d[i + 4:i + 2 + L]
            if 0xE0 <= m <= 0xEF:
                self.app_markers.append(m - 0xE0)
                if m == 0xE1 and seg[:6] == b"Exif\x00\x00" and self.exif is None:
                    self.exif = bytes(seg)
                if m == 0xE2 and seg[:12] == b"ICC_PROFILE\x00":
                    icc_chunks.append((seg[12], bytes(seg[14:])))
            elif m == 0xDB:
                j = 0
                while j < len(seg):
                    pq, tq = seg[j] >> 4, seg[j] & 15
                    j += 1
                    if pq:
                        q = np.frombuffer(seg[j:j + 128], ">u2").astype(np.int32)
                        j += 128
                    else:
                        q = np.frombuffer(seg[j:j + 64], np.uint8).astype(np.int32)
                        j += 64
                    nat = np.zeros(64, np.int32)
                    nat[ZIGZAG] = q
                    self.qt[tq] = nat
            elif m == 0xC4:
                j = 0
                while j < len(seg):
                    tc, th = seg[j] >> 4, seg[j] & 15
                    j += 1
                    counts = list(seg[j:j + 16])
                    j += 16
                    n = sum(counts)
                    self.ht[(tc, th)] = (counts, bytes(seg[j:j + n]))
                    j += n
            elif m in (0xC0, 0xC1):
                self.sof = m
                P = seg[0]
                if P != 8:
                    raise Unsupported(f"{P}-bit precision")
                self.H, self.W = struct.unpack(">HH", seg[1:5])
                n = seg[5]
                for c in range(n):
                    cid, s, tq = seg[6 + 3 * c], seg[7 + 3 * c], seg[8 + 3 * c]
                    self.comps.append(dict(id=cid, h=s >> 4, v=s & 15, tq=tq))
            elif 0xC2 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                kind = {0xC2: "progressive", 0xC3: "lossless", 0xC9: "arithmetic",
                        0xCA: "progressive arithmetic"}.get(m, f"SOF{m - 0xC0}")
                raise Unsupported(f"{kind} JPEG")
            elif m == 0xCC:
                raise Unsupported("arithmetic coding")
            elif m == 0xDD:
                self.dri = struct.unpack(">H", seg[:2])[0]
            elif m == 0xDA:
                if not self.comps:
                    raise Unsupported("SOS before SOF")
                n = seg[0]
                if n != len(self.comps):
                    raise Unsupported("non-interleaved scan")
                for c in range(n):
                    cid = seg[1 + 2 * c]
                    comp = next((x for x in self.comps if x["id"] == cid), None)
                    if comp is None:
                        raise Unsupported("scan references unknown component")
                    comp["td"] = seg[2 + 2 * c] >> 4
                    comp["ta"] = seg[2 + 2 * c] & 15
                self.scan_start = i + 2 + L
                break
            i += 2 + L
        if self.scan_start is None or self.sof is None:
            raise Unsupported("no baseline frame/scan found")
        if len(self.comps) not in (1, 3):
            raise Unsupported(f"{len(self.comps)} components")
        if self.dri:
            raise Unsupported("restart markers (DRI) not handled")
        if len(self.comps) == 1:
            self.comps[0]["h"] = self.comps[0]["v"] = 1
        for c in self.comps:
            if c["tq"] not in self.qt or (0, c["td"]) not in self.ht or (1, c["ta"]) not in self.ht:
                raise Unsupported("missing quant/Huffman table")
        if icc_chunks:
            self.icc = b"".join(x[1] for x in sorted(icc_chunks))
        self.hmax = max(c["h"] for c in self.comps)
        self.vmax = max(c["v"] for c in self.comps)
        self.NX = (self.W + 8 * self.hmax - 1) // (8 * self.hmax)
        self.NY = (self.H + 8 * self.vmax - 1) // (8 * self.vmax)
        self.N = self.NX * self.NY

    @property
    def sampling(self):
        return "".join(f"{c['h']}x{c['v']}" for c in self.comps)


# --------------------------------------------------------------------------- bitstream

@njit(cache=True)
def _unstuff(raw):
    """FF00 -> FF. Any other FFxx is kept as ordinary data. Returns bytes + raw offset map."""
    n = raw.shape[0]
    out = np.empty(n + 8, np.uint8)
    src = np.empty(n + 1, np.int64)
    o = 0
    i = 0
    while i < n:
        b = raw[i]
        out[o] = b
        src[o] = i
        o += 1
        if b == 0xFF and i + 1 < n and raw[i + 1] == 0:
            i += 2
        else:
            i += 1
    src[o] = n
    for k in range(8):
        out[o + k] = 0
    return out, src[:o + 1], o


def _build_lut(counts, syms):
    sym = np.full(65536, -1, np.int16)
    ln = np.zeros(65536, np.uint8)
    code = 0
    k = 0
    for L in range(1, 17):
        for _ in range(counts[L - 1]):
            if k >= len(syms) or code >= (1 << L):
                break
            base = code << (16 - L)
            sym[base:base + (1 << (16 - L))] = syms[k]
            ln[base:base + (1 << (16 - L))] = L
            k += 1
            code += 1
        code <<= 1
    return sym, ln


@njit(cache=True)
def _decode(data, nbits, blk_dc, blk_ac, lut_sym, lut_len, max_mcu):
    nb = blk_dc.shape[0]
    bitpos = np.zeros(max_mcu + 1, np.int64)
    err = np.zeros(max_mcu, np.uint8)
    dcdiff = np.zeros((max_mcu, nb), np.int32)
    coef = np.zeros((max_mcu, nb, 64), np.int16)
    pos = 0
    m = 0
    while m < max_mcu:
        if pos >= nbits:
            break
        bitpos[m] = pos
        e = 0
        for b in range(nb):
            # ---- DC
            t = blk_dc[b]
            q = pos >> 3
            w = (np.int64(data[q]) << 16) | (np.int64(data[q + 1]) << 8) | np.int64(data[q + 2])
            p = (w >> (8 - (pos & 7))) & 0xFFFF
            s = lut_sym[t, p]
            if s < 0 or s > 11:
                e = 1
                s = 0
                pos += 1
            else:
                pos += lut_len[t, p]
            diff = 0
            if s:
                q = pos >> 3
                w = (np.int64(data[q]) << 24) | (np.int64(data[q + 1]) << 16) | (np.int64(data[q + 2]) << 8) | np.int64(data[q + 3])
                v = (w >> (32 - (pos & 7) - s)) & ((1 << s) - 1)
                pos += s
                diff = v if v >= (1 << (s - 1)) else v - (1 << s) + 1
            dcdiff[m, b] = diff
            # ---- AC
            t = blk_ac[b]
            k = 1
            while k < 64:
                q = pos >> 3
                w = (np.int64(data[q]) << 16) | (np.int64(data[q + 1]) << 8) | np.int64(data[q + 2])
                p = (w >> (8 - (pos & 7))) & 0xFFFF
                rs = lut_sym[t, p]
                if rs < 0:
                    e = 1
                    pos += 1
                    break
                pos += lut_len[t, p]
                r = rs >> 4
                sz = rs & 15
                if sz == 0:
                    if r == 15:
                        k += 16
                        continue
                    break
                k += r
                if k > 63 or sz > 11:
                    e = 1
                    break
                q = pos >> 3
                w = (np.int64(data[q]) << 24) | (np.int64(data[q + 1]) << 16) | (np.int64(data[q + 2]) << 8) | np.int64(data[q + 3])
                v = (w >> (32 - (pos & 7) - sz)) & ((1 << sz) - 1)
                pos += sz
                coef[m, b, k] = v if v >= (1 << (sz - 1)) else v - (1 << sz) + 1
                k += 1
        if pos > nbits:
            pos = bitpos[m]     # ran out of data inside this MCU: drop it
            break
        err[m] = e
        m += 1
    bitpos[m] = pos
    return bitpos[:m + 1], err[:m], dcdiff[:m], coef[:m], m


def marker_stats(raw: bytes, window: int):
    """Per window of the raw scan: number of FF00 stuffings and of invalid FFxx pairs."""
    a = np.frombuffer(raw, np.uint8)
    if len(a) < 2:
        return np.zeros(0, int), np.zeros(0, int)
    ff = np.nonzero(a[:-1] == 0xFF)[0]
    nxt = a[ff + 1]
    stuff = ff[nxt == 0]
    bad = ff[(nxt != 0) & (nxt != 0xFF) & ~((nxt >= 0xD0) & (nxt <= 0xD7))]
    nw = (len(a) + window - 1) // window
    return (np.bincount(stuff // window, minlength=nw)[:nw],
            np.bincount(bad // window, minlength=nw)[:nw])


def find_foreign_tail(raw: bytes, window=4096, bad_min=6, base=0):
    """Byte offset (relative to scan start) where unrelated non-JPEG data starts, or None.

    Signature of foreign data: (almost) no FF00 stuffing and many invalid FFxx pairs, running
    to the end of the file. Windows are classified; the tail is the final run of foreign
    windows (one stray window allowed), and its start is refined to just after the last FF00
    stuffing before the run and, when possible, to the most strongly aligned 512-byte file
    offset between that FF00 and the first invalid marker (overwrites follow flash clusters).
    `base` is the file offset of raw[0].
    """
    stuff, bad = marker_stats(raw, window)
    nw = len(stuff)
    if nw == 0:
        return None
    lens = np.full(nw, window, float)
    lens[-1] = len(raw) - (nw - 1) * window
    dens = bad * window / np.maximum(lens, 1)
    foreign = (dens >= bad_min) & (stuff * 6 <= bad)
    if lens[-1] < 512 and nw > 1:
        foreign[-1] = foreign[-2]
    if not foreign[-1]:
        return None
    k = nw - 1
    while k > 0:
        if foreign[k - 1]:
            k -= 1
        elif k > 1 and foreign[k - 2] and stuff[k - 1] <= 2:
            k -= 2
        else:
            break
    if (len(raw) - k * window) < 2 * window and nw - k < 2:
        return None
    a = np.frombuffer(raw, np.uint8)
    head = a[:k * window + 1]
    ffs = np.nonzero((head[:-1] == 0xFF) & (head[1:] == 0))[0]
    lo = int(ffs[-1]) + 2 if len(ffs) else 0
    lo = max(lo, (k - 1) * window, 0)
    ff = np.nonzero(a[lo:-1] == 0xFF)[0] + lo
    nx = a[ff + 1]
    inv = ff[(nx != 0) & (nx != 0xFF) & ~((nx >= 0xD0) & (nx <= 0xD7))]
    hi = int(inv[0]) if len(inv) else len(raw)
    best, best_al = lo, -1
    first = ((base + lo + 511) // 512) * 512
    for off in range(first, base + hi + 1, 512):
        al = (off & -off).bit_length()
        if al > best_al:
            best, best_al = off - base, al
    return int(best)


def find_wiped_runs(raw: bytes, minlen=64):
    """Byte ranges [start, end) of long runs of 0x00 or 0xFF (wiped/erased flash sectors).

    Real entropy-coded data never contains 64 identical 0x00 or 0xFF bytes in a row.
    """
    a = np.frombuffer(raw, np.uint8)
    out = []
    for val in (0x00, 0xFF):
        m = np.r_[0, (a == val).astype(np.int8), 0]
        d = np.diff(m)
        st, en = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
        keep = (en - st) >= minlen
        out += list(zip(st[keep].tolist(), en[keep].tolist(), strict=True))
    return sorted(out)


# --------------------------------------------------------------------------- scan object

class Scan:
    """Decoded scan: DC trace, AC-only pixel blocks per MCU and helpers to render."""

    def __init__(self, path=None, data=None, tail_window=4096, tail_bad=6):
        if data is None:
            with open(path, "rb") as f:
                data = f.read()
        self.path = path
        self.hdr = h = Header(data)
        self.comps = h.comps
        self.NX, self.NY, self.N = h.NX, h.NY, h.N
        self.W, self.H = h.W, h.H
        raw = data[h.scan_start:]
        self.first_eoi = data.find(b"\xff\xd9", h.scan_start)   # first EOI (file offset) or -1
        # Where does the scan end? At the first EOI if only padding (00/FF) or another image
        # follows it (some phones pad files or append a second JPEG); otherwise at a final EOI
        # near the end of the file; otherwise the scan runs to the end of the file.
        tailpos = -1
        if self.first_eoi >= 0:
            rest = data[self.first_eoi + 2:]
            pad = rest.count(0) + rest.count(255)
            if not rest or pad >= 0.9 * len(rest) or rest[:2] == b"\xff\xd8":
                tailpos = self.first_eoi - h.scan_start
        if tailpos < 0:
            tailpos = raw.rfind(b"\xff\xd9", max(0, len(raw) - 1024))
        if tailpos >= 0:
            raw = raw[:tailpos]
        self.raw = raw
        self.foreign_offset = find_foreign_tail(raw, tail_window, tail_bad, base=h.scan_start)
        wiped_all = find_wiped_runs(raw)
        u, src, n = _unstuff(np.frombuffer(raw, np.uint8))
        self.src = src
        # block layout of one MCU
        blk_comp, blk_by, blk_bx = [], [], []
        for ci, c in enumerate(self.comps):
            for by in range(c["v"]):
                for bx in range(c["h"]):
                    blk_comp.append(ci)
                    blk_by.append(by)
                    blk_bx.append(bx)
        self.blk_comp = np.array(blk_comp)
        keys = sorted(h.ht)
        kidx = {k: i for i, k in enumerate(keys)}
        lut_sym = np.zeros((len(keys), 65536), np.int16)
        lut_len = np.zeros((len(keys), 65536), np.uint8)
        for k, (counts, syms) in h.ht.items():
            lut_sym[kidx[k]], lut_len[kidx[k]] = _build_lut(counts, syms)
        blk_dc = np.array([kidx[(0, self.comps[c]["td"])] for c in blk_comp], np.int64)
        blk_ac = np.array([kidx[(1, self.comps[c]["ta"])] for c in blk_comp], np.int64)
        max_mcu = int(self.N * 1.5) + 4 * self.NX + 64
        bitpos, err, dcdiff, coef, m = _decode(u, n * 8, blk_dc, blk_ac, lut_sym, lut_len, max_mcu)
        self.D = m
        self.bitpos = bitpos
        self.err = err.astype(bool)
        # raw byte offset (in scan) of each MCU start
        self.byteoff = src[np.minimum(bitpos >> 3, len(src) - 1)]
        # usable decoded MCUs: stop at the foreign tail
        # wiped sectors (runs of 00/FF) inside the scan
        self.wiped = list(wiped_all)
        self.tail_kind = "foreign" if self.foreign_offset is not None else None
        # a run reaching the end of the scan data is an unrecoverable tail
        if self.wiped and len(raw) - self.wiped[-1][1] < 2048:
            ws = self.wiped.pop()[0]
            if self.foreign_offset is None or ws < self.foreign_offset:
                self.foreign_offset, self.tail_kind = ws, "wiped"
        self.Dlim = m
        if self.foreign_offset is not None:
            self.Dlim = int(np.searchsorted(self.byteoff[:m], self.foreign_offset, side="left"))
        # MCUs decoded from wiped runs (and the one straddling each end) are garbage
        self.wiped_mcus = []
        for ws, we in self.wiped:
            a0 = max(0, int(np.searchsorted(self.byteoff[:m], ws, side="right")) - 1)
            a1 = min(m, int(np.searchsorted(self.byteoff[:m], we, side="left")) + 1)
            self.err[a0:a1] = True
            self.wiped_mcus.append((a0, a1))
        # ----- DC values (cumulative predictors) and AC-only pixels per component
        self.q0 = []
        self.dcval = []       # per comp (D, v, h) float: cumulative predictor of each block
        self.acpix = []       # per comp (D, v*8, h*8) float32
        self.mcu_dc = np.zeros((m, len(self.comps)))
        for ci, c in enumerate(self.comps):
            sel = np.nonzero(self.blk_comp == ci)[0]
            q = h.qt[c["tq"]].astype(np.float32)
            self.q0.append(float(q[0]))
            diffs = dcdiff[:, sel].astype(np.int64)
            cum = np.cumsum(diffs.reshape(-1)).reshape(m, len(sel))
            self.dcval.append(cum.reshape(m, c["v"], c["h"]).astype(np.float64))
            self.mcu_dc[:, ci] = cum.mean(1) if m else 0
            # coef is stored by zigzag index k; natural position is ZIGZAG[k]
            nat = np.zeros((m, len(sel), 64), np.float32)
            nat[:, :, ZIGZAG] = coef[:, sel, :]
            nat *= q[None, None, :]
            nat[:, :, 0] = 0
            blocks = np.einsum("ki,mbkl,lj->mbij", IDCT.astype(np.float32),
                               nat.reshape(m, len(sel), 8, 8), IDCT.astype(np.float32), optimize=True)
            pix = blocks.reshape(m, c["v"], c["h"], 8, 8).transpose(0, 1, 3, 2, 4).reshape(m, c["v"] * 8, c["h"] * 8)
            self.acpix.append(np.ascontiguousarray(pix, dtype=np.float32))
        del coef

    def end_aligned(self, slack=2):
        """True if MCU N ends exactly at the first EOI: no Huffman errors and no MCUs gained or
        lost anywhere. Damage that changes the MCU count cannot pass this test."""
        if self.D < self.N or self.err[:self.N].any() or self.first_eoi < 0:
            return False
        endb = (int(self.bitpos[self.N]) + 7) // 8
        end_raw = self.hdr.scan_start + int(self.src[min(endb, len(self.src) - 1)])
        return 0 <= self.first_eoi - end_raw <= slack

    # ------------------------------------------------------------------ geometry
    def mcu_shape(self, ci):
        c = self.comps[ci]
        return c["v"] * 8, c["h"] * 8

    def blank(self):
        return [np.full((self.NY * c["v"] * 8, self.NX * c["h"] * 8), np.nan, np.float32) for c in self.comps]

    def _view(self, plane, ci):
        bh, bw = self.mcu_shape(ci)
        return plane.reshape(self.NY, bh, self.NX, bw).transpose(0, 2, 1, 3)

    def mcu_pixels(self, ci, idx, off):
        """Pixels of decoded MCUs idx for component ci with DC offset off (quant units)."""
        q0 = self.q0[ci]
        dc = (self.dcval[ci][idx] - off) * (q0 / 8.0) + 128.0
        dc = np.repeat(np.repeat(dc, 8, -2), 8, -1)
        return self.acpix[ci][idx] + dc.astype(np.float32)

    def render(self, canvas, a, b, s, off):
        """Render decoded MCUs [a, b) at true positions a+s.. with DC offset off (per comp)."""
        a = max(a, -s, 0)
        b = min(b, self.N - s, self.D)
        if b <= a:
            return
        idx = np.arange(a, b)
        t = idx + s
        my, mx = np.divmod(t, self.NX)
        for ci in range(len(self.comps)):
            v = self._view(canvas[ci], ci)
            v[my, mx] = self.mcu_pixels(ci, idx, off[ci])

    def blank_mcus(self, canvas, ts):
        ts = np.asarray(ts)
        ts = ts[(ts >= 0) & (ts < self.N)]
        my, mx = np.divmod(ts, self.NX)
        for ci in range(len(self.comps)):
            self._view(canvas[ci], ci)[my, mx] = np.nan

    def dc_out_of_range(self, idx, off):
        """True where any component's absolute DC coefficient exceeds the legal range."""
        bad = np.zeros(len(idx), bool)
        for ci in range(len(self.comps)):
            v = (self.dcval[ci][idx] - off[ci]) * self.q0[ci]
            # legal |DC| <= 1024; quantisation rounding can add up to one step
            bad |= (np.abs(v) > 1024 + self.q0[ci]).reshape(len(idx), -1).any(1)
        return bad


# --------------------------------------------------------------------------- colour

def upsample(plane, fy, fx, H, W):
    """Chroma upsampling: bilinear (like libjpeg's fancy upsampling), nearest next to holes."""
    import cv2
    if fy == 1 and fx == 1:
        return plane[:H, :W]
    near = np.repeat(np.repeat(plane, fy, 0), fx, 1)
    lin = cv2.resize(np.nan_to_num(plane, nan=128.0).astype(np.float32), (plane.shape[1] * fx, plane.shape[0] * fy),
                     interpolation=cv2.INTER_LINEAR)
    holes = np.isnan(plane)
    if holes.any():
        m = cv2.dilate(holes.astype(np.uint8), np.ones((3, 3), np.uint8))
        m = np.repeat(np.repeat(m, fy, 0), fx, 1).astype(bool)
        lin = np.where(m, near, lin)
    return lin[:H, :W]


def to_rgb_float(scan, canvas, H=None):
    """Canvas (YCbCr planes, NaN = empty) -> float RGB (H, W, 3) with NaN where empty."""
    H = scan.H if H is None else H
    W = scan.W
    hm, vm = scan.hdr.hmax, scan.hdr.vmax
    ch = []
    for ci, c in enumerate(scan.comps):
        ch.append(upsample(canvas[ci], vm // c["v"], hm // c["h"], H, W))
    if len(ch) == 1:
        return np.repeat(ch[0][..., None], 3, -1)
    Y, Cb, Cr = ch
    R = Y + 1.402 * (Cr - 128)
    G = Y - 0.344136 * (Cb - 128) - 0.714136 * (Cr - 128)
    B = Y + 1.772 * (Cb - 128)
    return np.stack([R, G, B], -1).astype(np.float32, copy=False)


def to_rgb(scan, canvas, H=None):
    rgb = to_rgb_float(scan, canvas, H)
    return np.clip(np.nan_to_num(rgb, nan=0) + 0.5, 0, 255).astype(np.uint8)
