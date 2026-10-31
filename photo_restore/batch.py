"""Parallel, resumable batch restore."""
import fnmatch
import logging
import os
import time
from collections import Counter

from .config import Params, Paths
from .pipeline import process, rel_key
from .sysutil import available_gb, mp_context

log = logging.getLogger(__name__)


def list_inputs(paths: Paths, only=None):
    files = sorted(p for p in paths.extracted.rglob("*") if p.is_file() and not p.name.endswith(".part"))
    if only:
        pats = only if isinstance(only, (list, tuple)) else [only]
        files = [f for f in files if any(fnmatch.fnmatch(str(rel_key(paths, f)), pat)
                                          or fnmatch.fnmatch(f.name, pat) for pat in pats)]
    return files


def _warmup():
    """Compile the numba kernels once in the parent so the workers load them from numba's
    on-disk cache instead of all compiling them at the same time."""
    import io

    import numpy as np
    from PIL import Image

    from .jpegscan import Scan
    b = io.BytesIO()
    Image.fromarray(np.zeros((16, 16, 3), np.uint8)).save(b, "JPEG")
    Scan(data=b.getvalue())


GB_PER_WORKER = 1.6     # peak for a 13 MP image incl. debug output


_G = {}


def _init(paths, params, force):
    import cv2
    cv2.setNumThreads(1)
    _G.update(paths=paths, params=params, force=force)


def _work(src):
    return process(src, _G["paths"], _G["params"], _G["force"])


def restore(paths: Paths, params: Params, jobs=None, force=False, only=None):
    paths.ensure()
    files = list_inputs(paths, only)
    if not files:
        log.warning("no input files under %s (run ingest first)", paths.extracted)
        return []
    if not jobs:
        jobs = max(1, (os.cpu_count() or 2) - 1)
        mem = available_gb()
        if mem:
            cap = max(1, int(mem / GB_PER_WORKER))
            if cap < jobs:
                log.info("limiting workers to %d (%.1f GB RAM available)", cap, mem)
                jobs = cap
    log.info("restoring %d files with %d workers", len(files), jobs)
    _warmup()
    t0 = time.time()
    results = []
    try:
        from tqdm import tqdm
    except ImportError:          # pragma: no cover
        tqdm = None
    ctx = mp_context()
    # biggest files first: better load balancing
    files.sort(key=lambda f: -f.stat().st_size)
    with ctx.Pool(jobs, initializer=_init, initargs=(paths, params, force), maxtasksperchild=50) as pool:
        it = pool.imap_unordered(_work, files, chunksize=1)
        if tqdm:
            it = tqdm(it, total=len(files), unit="img")
        for r in it:
            results.append(r)
            if r.get("status") == "failed":
                log.error("FAILED %s: %s", r["path"], r.get("message"))
            else:
                log.info("%-16s %s %s", r.get("status"), r["path"], r.get("message", ""))
    c = Counter(r.get("status") for r in results)
    skipped = sum(1 for r in results if r.get("skipped"))
    log.info("done in %.0fs: %s (skipped %d already done)", time.time() - t0, dict(c), skipped)
    return results
