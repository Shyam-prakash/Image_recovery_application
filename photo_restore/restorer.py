"""Find damage points, resynchronise shift/DC offset after each one, clean up segment edges.

Conventions (same as the prototype notes):
  * MCU indices are linear, row-major. A segment maps decoded MCUs [a, b) to true MCUs
    [a+s, b+s); s is the shift.
  * DC offsets are in quantised DC units per component; corrected DC = raw predictor - off.
"""
import logging
import warnings
from dataclasses import dataclass, field

import numpy as np

from .config import Params

log = logging.getLogger(__name__)

CHROMA_W = 1.5      # chroma boundary errors are weighted up: chroma is smooth, casts are obvious


@dataclass
class Segment:
    a: int                  # decoded start
    b: int                  # decoded end (exclusive)
    s: int                  # shift
    off: tuple              # DC offset per component
    method: str = "start"   # start | resync | gap
    score: float = 0.0      # resync score (mean boundary error) or seam score

    @property
    def t0(self): return self.a + self.s
    @property
    def t1(self): return self.b + self.s

    def as_dict(self):
        return dict(decoded_start=int(self.a), decoded_end=int(self.b), shift=int(self.s),
                    true_start=int(self.t0), true_end=int(self.t1),
                    offsets=[round(float(x), 1) for x in self.off], method=self.method,
                    score=round(float(self.score), 2))


@dataclass
class Result:
    segments: list = field(default_factory=list)
    damage_points: list = field(default_factory=list)   # decoded indices of accepted damage
    false_positives: int = 0
    blanked: list = field(default_factory=list)         # (true_start, true_end) ranges
    tail_true: int = None                               # first true MCU not covered at the end
    foreign_tail: bool = False
    gap_segments: int = 0
    no_resync_true: int = None                          # true MCU where the restorer gave up
    garbage_dropped: int = 0                            # segments removed as non-image data
    notes: list = field(default_factory=list)


class Restorer:
    """Places the decoded MCUs of one damaged scan back where they belong.

    Usage: ``res = Restorer(scan, params).run(); restorer.trim()``. After that ``final`` holds
    the YCbCr planes (NaN = unknown), ``res`` the segments, damage points and notes.
    """

    def __init__(self, scan, params: Params = None):
        self.sc = sc = scan
        self.p = params or Params()
        # MCU N ends exactly at EOI and no decode errors: no MCUs were gained or lost, so the
        # only possible damage is a DC offset change (shift stays 0)
        self.aligned = sc.end_aligned()
        self.NX, self.N = sc.NX, sc.N
        self.nc = len(sc.comps)
        self.Dl = min(sc.Dlim, sc.D)
        self.tr = sc.mcu_dc
        self.final = sc.blank()
        self.truth = np.full((self.N, self.nc), np.nan)
        self.res = Result(foreign_tail=sc.foreign_offset is not None)
        # top pixel row of every decoded MCU: AC part, and DC (quant units) per pixel column
        self.top_ac, self.top_dc = [], []
        for ci in range(self.nc):
            self.top_ac.append(sc.acpix[ci][:, 0, :])
            dc0 = sc.dcval[ci][:, 0, :]
            self.top_dc.append(np.repeat(dc0, 8, -1))
        self.scale = [q / 8.0 for q in sc.q0]

    # ------------------------------------------------------------------ canvas helpers
    def draw(self, a, b, s, off):
        """Render decoded [a, b) into the final canvas and record corrected DC values."""
        a = max(a, -s, 0)
        b = min(b, self.N - s, self.Dl)
        if b > a:
            self.sc.render(self.final, a, b, s, off)
            self.truth[a + s:b + s] = self.tr[a:b] - np.asarray(off)
        return a, b

    def place(self, seg: Segment):
        a, b = self.draw(seg.a, seg.b, seg.s, seg.off)
        if b <= a:
            return
        seg.a, seg.b = a, b
        self.res.segments.append(seg)

    def boundary_err(self, canvas):
        """(nc, N): mean abs diff between each MCU's top pixel row and the row above."""
        out = np.full((self.nc, self.N), np.nan)
        for ci in range(self.nc):
            bh, bw = self.sc.mcu_shape(ci)
            P = canvas[ci]
            top = P[bh::bh]
            up = P[bh - 1:-1:bh]
            e = np.abs(top - up).reshape(self.sc.NY - 1, self.NX, bw).mean(-1)
            out[ci, self.NX:] = e.ravel()
        return out

    @staticmethod
    def combine(e):
        """Per-MCU combined error from per-component errors (Y, Cb, Cr)."""
        if e.shape[0] == 1:
            return e[0]
        return np.fmax(e[0], CHROMA_W * np.fmax(e[1], e[2]))

    def bottom_rows(self):
        """(nc) arrays (N, bw): bottom pixel row of every true MCU in final."""
        out = []
        for ci in range(self.nc):
            bh, bw = self.sc.mcu_shape(ci)
            out.append(self.final[ci][bh - 1::bh].reshape(self.sc.NY, self.NX, bw).reshape(self.N, bw))
        return out

    # ------------------------------------------------------------------ damage detection
    def find_damage(self, seg: Segment, start: int):
        """First decoded index >= start where the segment stops fitting, or None."""
        sc, p = self.sc, self.p
        end = min(self.Dl, self.N - seg.s)
        if start >= end:
            return None
        tmp = [f.copy() for f in self.final]
        sc.render(tmp, seg.a, end, seg.s, seg.off)
        E = self.combine(self.boundary_err(tmp))
        # row 0 has no row above; left-neighbour differences there are too noisy to call
        # damage on their own (no way to confirm a false positive) -> only decode errors and
        # illegal DC values count in the first row
        idx = np.arange(start, end)
        t = idx + seg.s
        e = np.nan_to_num(E[t], nan=0.0)
        bad = e > self.thr_damage
        cands = []
        if len(bad) >= p.damage_run:
            runs = np.convolve(bad.astype(int), np.ones(p.damage_run, int), "valid") == p.damage_run
            if runs.any():
                cands.append(start + int(np.argmax(runs)))
        # moderate but sustained jump (a colour cast below a damage point): high mean over
        # half a row of consecutive MCUs
        Lr = max(4, self.NX // 2)
        if len(e) >= Lr:
            m = np.convolve(e, np.ones(Lr) / Lr, "valid")
            hit = np.nonzero(m > self.thr_sustained)[0]
            if len(hit):
                k = int(hit[0])
                k += int(np.argmax(e[k:k + Lr] > self.thr_sustained))   # first elevated MCU
                cands.append(start + k)
        er = np.nonzero(sc.err[start:end])[0]
        if len(er):
            cands.append(start + int(er[0]))
        dcb = np.nonzero(sc.dc_out_of_range(idx, seg.off))[0]
        if len(dcb):
            cands.append(start + int(dcb[0]))
        return min(cands) if cands else None

    # ------------------------------------------------------------------ resync
    def window_fit(self, j, L, S, off=None, bottoms=None):
        """Fit decoded MCUs [j, j+L) at shifts S against the pixel rows directly above.

        off: None to estimate the DC offset per shift (median pixel step across the
        boundary), or a fixed (nc,) offset. Returns (score (nS,), offsets (nS, nc), n_valid).
        """
        NX, N = self.NX, self.N
        bottoms = bottoms if bottoms is not None else self.bottom_rows()
        have = ~np.isnan(self.truth[:, 0])
        S = np.atleast_1d(S)
        k = np.arange(j, min(j + L, self.Dl))
        T = k[None, :] + S[:, None]
        A = T - NX
        Ac = np.clip(A, 0, N - 1)
        valid = (T >= 0) & (T < N) & (A >= 0) & have[Ac]
        nvalid = valid.sum(1)
        offs = np.zeros((len(S), self.nc))
        errs = []
        for ci in range(self.nc):
            raw = self.top_ac[ci][k][None] + self.top_dc[ci][k][None] * self.scale[ci] + 128   # (1, L, bw)
            bot = bottoms[ci][Ac]                                                               # (nS, L, bw)
            if off is None:
                delta = (raw - bot) / self.scale[ci]
                delta[~valid] = np.nan
                with np.errstate(all="ignore"), warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    o = np.nanmedian(delta.reshape(len(S), -1), axis=1)
                offs[:, ci] = np.nan_to_num(o)
            else:
                offs[:, ci] = off[ci]
            e = np.abs(raw - offs[:, ci, None, None] * self.scale[ci] - bot).mean(-1)
            e[~valid] = np.nan
            with np.errstate(all="ignore"), warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                errs.append(np.nanmean(e, axis=1))
        score = self._score(np.array(errs))
        score[nvalid < max(4, min(L, self.p.resync_window) // 2)] = np.inf
        return np.nan_to_num(score, nan=np.inf), offs, nvalid

    def resync_scan(self, d, s_old, off_old, j_from=None):
        """Search decoded start j >= d and shift s whose window fits the rows above.

        Returns ("same", j) when the old shift/offset still fit (false positive), or
        ("new", j, s, off, score), or None.
        """
        p = self.p
        W = p.resync_window
        if self.aligned:
            S0 = np.array([s_old])
        else:
            S0 = np.arange(s_old + p.shift_min, s_old + p.shift_max + 1)
        rate = self.byte_rate(d)
        bottoms = self.bottom_rows()
        jmax = min(d + int(p.resync_scan_rows * self.NX), self.Dl - W)
        # after a long damaged stretch the scan may need to start well past d
        jc = self.first_clean(d, need=W)
        if jc is not None and jc > jmax - W:
            jmax = min(jc + int(p.resync_scan_rows * self.NX), self.Dl - W)
        # true position a new segment may not start before (allow a little overlap with
        # garbage placed just before the damage was detected)
        floor = max(d + s_old, self.frontier()) - W // 2
        # a real resync decodes cleanly afterwards: the whole window must be free of errors
        cerr = np.r_[0, np.cumsum(self.sc.err[:self.Dl])]
        for j in range(j_from if j_from is not None else d, jmax):
            if cerr[min(j + W, self.Dl)] - cerr[j] > 0:
                continue
            S = S0
            if not self.aligned:
                # also search around the shift implied by the byte length of the damage
                s_est = self.shift_estimate(d, j, s_old, rate)
                if abs(s_est - s_old) > p.shift_max // 2:
                    S = np.unique(np.r_[S0, np.arange(s_est + p.shift_min, s_est + p.shift_max + 1)])
            so, _, _ = self.window_fit(j, W, [s_old], off_old, bottoms)
            if so[0] < self.thr_accept:
                return ("same", j)
            score, offs, _ = self.window_fit(j, W, S, None, bottoms)
            # a resync must move forward: landing back inside what was just placed means a
            # lucky fit of garbage (the true damage cannot end before it started)
            score[j + S < floor] = np.inf
            bi = int(np.argmin(score))
            if score[bi] >= self.thr_accept:
                continue
            # confirm over a longer window (up to the next decode error)
            nxt = np.nonzero(self.sc.err[j:min(self.Dl, j + 4 * W)])[0]
            L = int(nxt[0]) if len(nxt) else 4 * W
            L = max(L, W)
            cand = S[score < min(self.thr_accept, score[bi] + 2.0)]
            sc2, offs2, _ = self.window_fit(j, L, cand, None, bottoms)
            bi2 = int(np.argmin(sc2))
            if s_old in cand:
                io = int(np.nonzero(cand == s_old)[0][0])
                if sc2[io] <= sc2[bi2] + 0.3:
                    bi2 = io
            s_new, off_new, sc_new = int(cand[bi2]), offs2[bi2], float(sc2[bi2])
            if not sc_new < self.thr_accept:
                # The long window does not fit. Either the segment really is short (it runs
                # into the next damage point) or this was a lucky fit inside garbage. Real
                # short segments move forward through the image; lucky fits in garbage keep
                # landing on top of what was just placed. Reject the latter and keep scanning.
                bi = int(np.argmin(score))
                if j + int(S[bi]) < floor:
                    continue
                io = int(np.nonzero(s_old == S)[0][0])
                if score[io] <= score[bi] + 0.3:
                    bi = io
                s_new, off_new, sc_new = int(S[bi]), offs[bi], float(score[bi])
            # if the old shift/offset fit (nearly) as well over the same stretch, this was a
            # false positive on strong content (e.g. a vertical edge), not a damage point
            so_l, _, _ = self.window_fit(j, max(L, W), [s_old], off_old, bottoms)
            sn_l, _, _ = self.window_fit(j, max(L, W), [s_new], off_new, bottoms)
            if so_l[0] < 1.5 * self.thr_accept and so_l[0] <= sn_l[0] + 1.0:
                return ("same", j)
            j = self.extend_back(j, max(d, floor - s_new), s_new, off_new, bottoms)
            return ("new", j, s_new, off_new, sc_new)
        return None

    def frontier(self):
        """Furthest true MCU index covered by a placed segment so far."""
        return max((sg.t1 for sg in self.res.segments), default=0)

    def extend_back(self, j, d, s, off, bottoms):
        """Move the segment start back over MCUs that still fit (the decoder resynced earlier)."""
        T = self.p.trim_threshold
        while j - 1 >= d and not self.sc.err[j - 1]:
            k = j - 1
            t = k + s
            if t - self.NX < 0 or t >= self.N or np.isnan(self.truth[t - self.NX, 0]):
                break
            e = []
            for ci in range(self.nc):
                raw = self.top_ac[ci][k] + (self.top_dc[ci][k] - off[ci]) * self.scale[ci] + 128
                e.append(np.abs(raw - bottoms[ci][t - self.NX]).mean())
            if self.combine(np.array(e)[:, None])[0] > T:
                break
            j -= 1
        return j

    @staticmethod
    def _score(errs):
        if errs.shape[0] == 1:
            return errs[0]
        return (2 * errs[0] + CHROMA_W * (errs[1] + errs[2])) / 4

    # ------------------------------------------------------------------ gap handling
    def first_clean(self, d, need=None):
        """First decoded j >= d followed by `need` MCUs without decode errors."""
        need = need or self.NX
        err = self.sc.err[:self.Dl]
        j = d
        while j < self.Dl - need:
            w = np.nonzero(err[j:j + need])[0]
            if not len(w):
                return j
            j += int(w[-1]) + 1                # no window containing that error can be clean
        return None

    def full_row_above(self, row):
        r = row - 1
        have = ~np.isnan(self.truth[:, 0])
        while r >= 0:
            if have[r * self.NX:(r + 1) * self.NX].all():
                return r
            r -= 1
        return None

    def gap_offset(self, j, s, residual=False):
        """Offset for a segment starting at decoded j (shift s) from the nearest full row above.

        With residual=True returns (offset, mismatch) where mismatch is the median absolute
        DC difference left after removing the offset (grey levels): small when the rows line up.
        """
        NX = self.NX
        t0 = j + s
        r = (t0 + NX - 1) // NX               # first full true row of the segment
        ra = self.full_row_above(r)
        if ra is None:
            return (None, None) if residual else (None, None, r)
        rows = []
        for rr in (r, r + 1):
            k = np.arange(rr * NX, (rr + 1) * NX) - s
            if k[-1] >= self.Dl or k[0] < 0:
                break
            rows.append(self.tr[k] - self.truth[ra * NX:(ra + 1) * NX])
        if not rows:
            return (None, None) if residual else (None, ra, r)
        d = np.concatenate(rows)
        off = np.median(d, 0)
        if residual:
            mis = np.median(np.abs(d - off), 0) * np.array(self.scale)
            return off, float(mis[0] + 0.5 * mis[1:].sum()) if self.nc > 1 else float(mis[0])
        return off, ra, r

    def seam_profile(self, a, b, s, off):
        """Normalised vertical-seam strength at every MCU column boundary for segment [a,b)."""
        sc, NX = self.sc, self.NX
        cv = sc.blank()
        sc.render(cv, a, b, s, off)
        r0 = (a + s + NX - 1) // NX
        r1 = (b + s) // NX
        if r1 - r0 < 2:
            return None
        prof = np.zeros(NX)
        for ci in range(self.nc):
            bh, bw = sc.mcu_shape(ci)
            P = cv[ci][r0 * bh:r1 * bh]
            dcol = np.abs(np.diff(P, axis=1)).mean(0)              # (NX*bw - 1)
            Hb = np.zeros(NX)
            Hb[1:] = dcol[bw - 1::bw][:NX - 1]
            ref = np.median(Hb[1:]) + 1e-3
            prof += Hb / ref
        return prof / self.nc

    def seam_shift(self, a, b, s, off, maxd=7, thresh=1.8, far_thresh=4.0):
        """Wrap-seam test: return (corrected shift or s, seam strength).

        A shift error of d MCUs leaves a vertical seam at column d (or NX+d) down the whole
        segment. Columns within maxd of an edge use the normal threshold; further columns need
        a stronger seam. A correction is accepted only if it removes the strongest seam in the
        whole profile: shifting a natural vertical edge just moves it, a wrap seam vanishes.
        """
        prof = self.seam_profile(a, b, s, off)
        if prof is None:
            return s, None
        NX = self.NX
        cols = np.arange(1, NX)
        near = np.minimum(cols, NX - cols) <= maxd
        need = np.where(near, thresh, far_thresh)
        ok = prof[1:] >= need
        if not ok.any():
            return s, float(prof[1:].max())
        c = int(cols[ok][np.argmax(prof[1:][ok])])
        delta = c if c <= NX // 2 else c - NX
        s2 = s - delta
        prof2 = self.seam_profile(a, b, s2, off)
        if prof2 is None:
            return s, float(prof[c])
        if prof2[1:].max() < 0.75 * prof[1:].max() and prof2[c] < 0.5 * prof[c]:
            return s2, float(prof2[1:].max())
        return s, float(prof[c])

    def byte_rate(self, d):
        """Average scan bytes per MCU over the rows before d."""
        sc = self.sc
        a_ref = max(0, d - 4 * self.NX)
        return max((sc.byteoff[d] - sc.byteoff[a_ref]) / max(1, d - a_ref), 1e-6)

    def shift_estimate(self, d, j, s_old, rate):
        """Shift for decoded j if the bytes between d and j stood in for true MCUs at the usual
        rate (true for overwritten or wiped sectors)."""
        lost = (self.sc.byteoff[j] - self.sc.byteoff[d]) / rate
        return int(round(d + s_old + lost - j))

    def absolute_offset(self, a, b):
        """DC offset with no reference above (damage in the first rows): keep every block mean
        inside the legal 0..255 range, centre luminance in the feasible range, grey-world chroma."""
        off = np.zeros(self.nc)
        for ci in range(self.nc):
            v = self.sc.dcval[ci][a:b].reshape(-1)
            if not len(v):
                continue
            q = self.sc.q0[ci]
            lo = np.percentile(v, 99.5) - 1023 / q
            hi = np.percentile(v, 0.5) + 1024 / q
            want = (lo + hi) / 2 if ci == 0 else np.median(v)
            off[ci] = np.clip(want, lo, hi) if lo <= hi else want
        return off

    def gap_resync(self, d, s_old):
        """Resync when the rows above are unavailable: clean stream start + seam test + gap offset."""
        j = self.first_clean(d)
        if j is None:
            return None
        NX = self.NX
        # estimate the true position from the byte length of the damaged stretch
        s_est = self.shift_estimate(d, j, s_old, self.byte_rate(d))
        e = min(self.Dl, self.N - s_est, j + 40 * NX)
        if e - j < 2 * NX:
            return None
        off, _, _ = self.gap_offset(j, s_est)
        if off is None:
            off = self.absolute_offset(j, e)
        # seam test over several rounds (each fixes up to 7 MCUs)
        s = s_est
        for _ in range(4):
            s2, _ = self.seam_shift(j, e, s, off)
            if s2 == s:
                break
            s = s2
        # the seam test fixes the column; choose the row (s +- k*NX) whose first full row
        # matches the nearest complete row above best, without overlapping placed content
        best = None
        for k in (-2, -1, 0, 1, 2):
            sk = s + k * NX
            if j + sk < d + s_old - self.p.resync_window // 2 or j + sk >= self.N:
                continue
            o, res_k = self.gap_offset(j, sk, residual=True)
            if o is not None and (best is None or res_k < best[0]):
                best = (res_k, sk, o)
        if best is not None:
            s, off = best[1], best[2]
        else:
            off2, _, _ = self.gap_offset(j, s)
            off = off2 if off2 is not None else self.absolute_offset(j, e)
        prof = self.seam_profile(j, e, s, off)
        sc_ = float(max(prof[1:8].max(), prof[-7:].max())) if prof is not None else 0.0
        return j, s, off, sc_

    # ------------------------------------------------------------------ main loop
    def _same_params(self, off_a, off_b):
        """Offsets equal within estimation noise (grey levels; looser for end-aligned scans)."""
        tol = 6.0 if self.aligned else 4.0
        return bool(np.all(np.abs(np.asarray(off_a) - np.asarray(off_b)) * np.array(self.scale) < tol))

    def _false_positive(self, d, j):
        """Record a rejected damage candidate; return where detection continues."""
        self.res.false_positives += 1
        if self.sc.err[d:j + 1].any():
            self.res.damage_points.append(("glitch", int(d), int(j)))
        return max(j + self.p.resync_window // 2, d + 1)

    def calibrate(self):
        """Adapt damage/accept thresholds to the image's own boundary statistics, measured on
        the stretch before the first decode error (detail-rich images have larger natural
        MCU-boundary differences)."""
        p = self.p
        er = np.nonzero(self.sc.err[:self.Dl])[0]
        end = int(er[0]) if len(er) and er[0] >= 3 * self.NX else self.Dl
        cv = self.sc.blank()
        self.sc.render(cv, 0, min(end, self.N), 0, np.zeros(self.nc))
        E = self.combine(self.boundary_err(cv))
        E = E[~np.isnan(E)]
        self.thr_sustained = 10.0
        if len(E) < 2 * self.NX:
            self.thr_damage, self.thr_accept = p.damage_threshold, p.resync_accept
            return
        self.base_median = float(np.median(E))
        self.base_p95 = float(np.percentile(E, 95))
        self.thr_damage = max(p.damage_threshold, 1.6 * self.base_p95)
        self.thr_accept = max(p.resync_accept, 1.5 * self.base_median)
        self.thr_sustained = max(10.0, 2.5 * self.base_median)

    def run(self):
        """Detect -> resync -> place loop over the whole scan. Returns the Result."""
        p, NX = self.p, self.NX
        self.calibrate()
        seg = Segment(0, self.Dl, 0, tuple(np.zeros(self.nc)), "start")
        start = 0
        guard = 0
        while True:
            guard += 1
            if guard > max(400, self.N // 40):
                self.res.notes.append("too many damage candidates; stopped early")
                break
            d = self.find_damage(seg, start)
            if d is None:
                break
            self.draw(seg.a, d, seg.s, seg.off)      # rows above must be visible to the search
            r = self.resync_scan(d, seg.s, np.asarray(seg.off))
            # a real resync is followed by a real stretch of data: reject candidates whose
            # segment breaks again within half a window and keep scanning
            for _ in range(50):
                if r is None or r[0] != "new":
                    break
                _, jn, sn, on, _sc = r
                probe = Segment(jn, self.Dl, sn, tuple(on))
                d2 = self.find_damage(probe, jn + 1)
                if d2 is None or d2 - jn >= p.resync_window // 2:
                    break
                r = self.resync_scan(d, seg.s, np.asarray(seg.off), j_from=jn + 1)
            method = "resync"
            near_err = self.sc.err[max(0, d - 2 * NX):min(self.Dl, d + 3 * NX)].any()
            if r is None and (self.aligned or not near_err):
                # no decoder evidence of damage here: a content false positive
                self.res.false_positives += 1
                start = d + max(p.damage_run, self.NX // 2)
                continue
            if r is None:
                # rows above missing (big hole) or no fit within the scan range
                r = self.gap_resync(d, seg.s)
                method = "gap"
                if r is None:
                    seg.b = d
                    self.res.notes.append(f"no resync after decoded MCU {d}")
                    self.res.no_resync_true = int(d + seg.s)
                    break
            if r[0] == "same":
                # false positive (detail-rich content) or harmless glitch: keep the segment
                start = self._false_positive(d, r[1])
                continue
            if r[0] == "new":
                r = r[1:]
            j, s_new, off_new, score = r
            off_new = np.asarray(off_new, float)
            if method == "resync" and s_new == seg.s and self._same_params(off_new, seg.off):
                # same shift, offset within estimation noise: not a real damage point
                start = self._false_positive(d, j)
                continue
            if method == "gap":
                off_new = self.legal_clamp(j, off_new)
            new = Segment(j, self.Dl, s_new, tuple(off_new), method, score)
            # validate long segments with the wrap-seam test
            new = self.check_seam(new)
            if new.s == seg.s and self._same_params(new.off, seg.off):
                # the seam test brought it back to the current parameters: not a damage point
                start = self._false_positive(d, j)
                continue
            seg.b = d
            self.place(seg)
            self.res.damage_points.append(("damage", int(d), int(j)))
            if method == "gap":
                self.res.gap_segments += 1
            seg = new
            start = j + 1
        seg.b = min(seg.b, self.Dl)
        self.place(seg)
        self.res.segments.sort(key=lambda x: x.t0)
        self.drop_garbage()
        last = max(sg.t1 for sg in self.res.segments) if self.res.segments else 0
        self.res.tail_true = int(last) if last < self.N else None
        return self.res

    def legal_clamp(self, j, off, rows=40):
        """Clamp a DC offset so the segment's block means stay inside 0..255 (a hard limit for
        real image data); fixes offsets estimated across a big hole."""
        b = min(self.Dl, j + rows * self.NX)
        er = np.nonzero(self.sc.err[j:b])[0]
        if len(er):
            b = j + int(er[0])                 # stop before the next damage point
        out = np.array(off, float)
        for ci in range(self.nc):
            v = self.sc.dcval[ci][j:b].reshape(-1)
            if len(v) < 4 * self.NX:
                continue
            q = self.sc.q0[ci]
            lo = np.percentile(v, 99.9) - 1023 / q
            hi = np.percentile(v, 0.1) + 1024 / q
            margin = 10 * 8 / q                # only correct clear violations (> 10 grey levels)
            if lo <= hi and (out[ci] < lo - margin or out[ci] > hi + margin):
                out[ci] = np.clip(out[ci], lo, hi)
        return out

    def drop_garbage(self):
        """Remove placed segments that are not image data: rows inside the segment do not fit
        each other (random garbage), or the segment is flat (decoded from filler bytes)."""
        if len(self.res.segments) < 2:
            return
        E = self.combine(self.boundary_err(self.final))
        base = getattr(self, "base_median", 4.0)
        ref_energy = None
        keep = []
        for i, sg in enumerate(self.res.segments):
            t0, t1 = max(sg.t0, 0), min(sg.t1, self.N)
            ts = np.arange(t0 + self.NX, t1)
            internal = E[ts] if len(ts) else np.array([])
            internal = internal[~np.isnan(internal)]
            a, b = sg.a, min(sg.b, self.sc.D)
            energy = float(np.abs(self.sc.acpix[0][a:b]).mean()) if b > a else 0.0
            if i == 0:
                ref_energy = energy
                keep.append(sg)
                continue
            garbage = False
            if len(internal) >= self.NX and np.median(internal) > max(12.0, 3.0 * base):
                garbage = True
            if (b - a) >= self.NX and ref_energy and energy < 0.08 * ref_energy:
                garbage = True
            # real image data keeps (almost) every block mean inside 0..255. A segment that
            # breaks this may be real content with a wrong offset (re-centre it into the legal
            # range) or garbage whose DC drifts over more than the legal span (drop it).
            if b > a and not garbage:
                idx = np.arange(a, b)
                illegal = self.sc.dc_out_of_range(idx, sg.off).mean()
                if illegal > 0.2:
                    off2 = self.absolute_offset(a, b)
                    if self.sc.dc_out_of_range(idx, off2).mean() < 0.03:
                        sg.off = tuple(off2)
                        sg.method += "+legal"
                        self.draw(sg.a, sg.b, sg.s, sg.off)
                        self.res.notes.append(f"re-centred offset of segment (true {t0}-{t1})")
                    else:
                        garbage = True
            if garbage:
                self.sc.blank_mcus(self.final, np.arange(t0, t1))
                self.truth[t0:t1] = np.nan
                self.res.notes.append(f"dropped garbage segment (true {t0}-{t1})")
                self.res.garbage_dropped += 1
            else:
                keep.append(sg)
        # a trailing chain of short pieces (no long, consistent stretch ever follows) is the
        # decoder wandering through unrecoverable data: drop it so the tail gets cropped
        while len(keep) > 1 and (keep[-1].t1 - keep[-1].t0) < self.NX:
            sg = keep.pop()
            t0, t1 = max(sg.t0, 0), min(sg.t1, self.N)
            if t1 > t0:
                self.sc.blank_mcus(self.final, np.arange(t0, t1))
                self.truth[t0:t1] = np.nan
            self.res.notes.append(f"dropped trailing short segment (true {t0}-{t1})")
        self.res.segments = keep
        last = max(sg.t1 for sg in keep)
        self.res.tail_true = int(last) if last < self.N else None

    def check_seam(self, seg: Segment):
        """Correct the shift of a long new segment with the wrap-seam test, if needed."""
        d2 = self.find_damage(seg, seg.a + 2 * self.NX)
        e = d2 if d2 is not None else min(self.Dl, self.N - seg.s)
        if e - seg.a < 3 * self.NX:
            return seg
        s2, v = self.seam_shift(seg.a, e, seg.s, seg.off)
        if s2 == seg.s:
            return seg
        off = None
        sc_, offs, _ = self.window_fit(seg.a, 4 * self.p.resync_window, [s2])
        if np.isfinite(sc_[0]):
            off = offs[0]
        if off is None:
            off, _, _ = self.gap_offset(seg.a, s2)
        if off is None:
            return seg
        log.debug("seam test moved shift %d -> %d at decoded %d", seg.s, s2, seg.a)
        return Segment(seg.a, seg.b, s2, tuple(off), seg.method + "+seam", v or 0.0)

    # ------------------------------------------------------------------ trimming
    def mcu_badness(self):
        """Per true MCU: boundary error vs the MCU above and vs the one below (NaN = unknown)."""
        E = self.combine(self.boundary_err(self.final))
        above = E
        below = np.full(self.N, np.nan)
        below[:-self.NX] = E[self.NX:]
        return above, below

    def trim(self):
        """Blank garbage MCUs at segment ends and around glitches; returns blanked ranges."""
        p = self.p
        T = p.trim_threshold
        look = max(p.trim_look, 8)
        blanked = []

        def is_bad(above, below, t):
            ea, eb = above[t], below[t]
            ka, kb = not np.isnan(ea), not np.isnan(eb)
            if not ka and not kb:
                return False
            return (not ka or ea > T) and (not kb or eb > T)

        segs = self.res.segments
        for i, sg in enumerate(segs):
            above, below = self.mcu_badness()
            # tail of the segment (only if something follows: damage point or tail)
            t_end = sg.t1
            if t_end <= self.N:
                lo = max(sg.t0, t_end - look)
                flags = np.array([is_bad(above, below, t) for t in range(lo, min(t_end, self.N))], bool)
                cut = None
                for k in range(len(flags)):
                    if flags[k] and flags[k:].mean() >= 0.5:
                        cut = lo + k
                        break
                followed = i < len(segs) - 1 or self.res.tail_true is not None
                if cut is not None and followed:
                    self.sc.blank_mcus(self.final, np.arange(cut, min(t_end, self.N)))
                    self.truth[cut:min(t_end, self.N)] = np.nan
                    blanked.append((int(cut), int(min(t_end, self.N))))
            # head of the segment (not the first)
            if i > 0:
                above, below = self.mcu_badness()
                hi = min(sg.t1, sg.t0 + look, self.N)
                flags = np.array([is_bad(above, below, t) for t in range(sg.t0, hi)], bool)
                cut = None
                for k in range(len(flags) - 1, -1, -1):
                    if flags[k] and flags[:k + 1].mean() >= 0.5:
                        cut = sg.t0 + k
                        break
                if cut is not None:
                    self.sc.blank_mcus(self.final, np.arange(sg.t0, cut + 1))
                    self.truth[sg.t0:cut + 1] = np.nan
                    blanked.append((int(sg.t0), int(cut + 1)))
        # glitches inside segments: isolated garbage MCUs
        for kind, d, j in self.res.damage_points:
            if kind != "glitch":
                continue
            sg = next((x for x in segs if x.a <= d < x.b), None)
            if sg is None:
                continue
            above, below = self.mcu_badness()
            ts = [t for t in range(d + sg.s - 4, j + sg.s + 2) if 0 <= t < self.N and is_bad(above, below, t)]
            if ts:
                self.sc.blank_mcus(self.final, np.array(ts))
                self.truth[ts] = np.nan
                blanked.append((int(min(ts)), int(max(ts) + 1)))
        blanked += self.grow_holes(0.6 * T)
        self.res.blanked = blanked
        return blanked

    def grow_holes(self, T, rounds=4):
        """Blank MCUs left/right of a hole that disagree with both the MCU above and below
        (mild garbage just outside the trimmed range)."""
        out = []
        for _ in range(rounds):
            above, below = self.mcu_badness()
            empty = np.isnan(self.truth[:, 0])
            left_of = np.zeros(self.N, bool)
            right_of = np.zeros(self.N, bool)
            left_of[:-1] = empty[1:]
            right_of[1:] = empty[:-1]
            cand = np.nonzero(~empty & (left_of | right_of))[0]
            bad = []
            for t in cand:
                ea, eb = above[t], below[t]
                if np.isnan(ea) and np.isnan(eb):
                    continue
                if (np.isnan(ea) or ea > T) and (np.isnan(eb) or eb > T):
                    bad.append(t)
            if not bad:
                break
            bad = np.array(bad)
            self.sc.blank_mcus(self.final, bad)
            self.truth[bad] = np.nan
            out += [(int(t), int(t) + 1) for t in bad]
        return out

    # ------------------------------------------------------------------ quality
    def residual_seams(self):
        """Ratio of boundary error on the first full row of each later segment to the median."""
        E = self.combine(self.boundary_err(self.final))
        med = np.nanmedian(E) + 1e-6
        out = []
        for sg in self.res.segments[1:]:
            r = (sg.t0 + self.NX - 1) // self.NX
            row = E[r * self.NX:(r + 1) * self.NX] if r * self.NX < self.N else np.array([np.nan])
            if np.isnan(row).all():
                continue
            out.append(float(np.nanmean(row) / med))
        return out
