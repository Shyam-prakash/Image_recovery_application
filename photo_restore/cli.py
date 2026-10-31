"""photo-restore command line."""
import argparse
import contextlib
import json
import logging
import os
import sys
import time
from pathlib import Path

# keep BLAS/OpenMP single-threaded: parallelism comes from worker processes
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

from .config import Params, Paths, default_root  # noqa: E402


def _setup_logging(paths: Paths, verbose=False, name="photo-restore"):
    paths.logs.mkdir(parents=True, exist_ok=True)
    logfile = paths.logs / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.DEBUG if verbose else logging.WARNING)
    sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.addHandler(sh)
    for noisy in ("numba", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logfile


def _params(a) -> Params:
    p = Params()
    for k in ("suffix", "damage_threshold", "resync_accept", "trim_threshold"):
        v = getattr(a, k, None)
        if v is not None:
            setattr(p, k, v)
    if getattr(a, "keep_full_size", False):
        p.keep_full_size = True
    return p


def cmd_ingest(a, paths):
    from .ingest import ingest
    files = ingest(paths, a.zip, a.zip_dir)
    print(f"ingested {len(files)} files into {paths.extracted}")


def cmd_restore(a, paths):
    from .batch import restore
    res = restore(paths, _params(a), jobs=a.jobs, force=a.force, only=a.only)
    from collections import Counter
    c = Counter(r.get("status") for r in res)
    print("status counts:", ", ".join(f"{k}={v}" for k, v in sorted(c.items())))


def cmd_report(a, paths):
    from .report import build
    recs, c = build(paths)
    print(f"report: {paths.report / 'index.html'}")
    print("status counts:", ", ".join(f"{k}={v}" for k, v in sorted(c.items())))


def cmd_all(a, paths):
    cmd_ingest(a, paths)
    cmd_restore(a, paths)
    cmd_report(a, paths)


def cmd_inspect(a, paths):
    from .pipeline import process
    src = Path(a.file).expanduser().resolve()
    if a.debug:
        logging.getLogger("photo_restore").setLevel(logging.DEBUG)
    rec = process(src, paths, _params(a), force=True, debug=a.debug)
    print(f"{rec['path']}: {rec['status']}  {rec.get('message', '')}")
    print(f"  size {rec.get('orig_size')} -> {rec.get('out_size')}  filled {rec.get('filled_pct')}%  "
          f"cropped {rec.get('rows_cropped')}px  confidence {rec.get('confidence')}  {rec.get('time_s')}s")
    segs = rec.get("segments") or []
    if segs:
        print(f"  {'decoded':>15} {'true':>15} {'shift':>6}  {'DC offsets (Y,Cb,Cr)':<26} method / score")
        for s in segs:
            print(f"  {s['decoded_start']:>7}-{s['decoded_end']:<7} {s['true_start']:>7}-{s['true_end']:<7} "
                  f"{s['shift']:>+6}  {str(s['offsets']):<26} {s['method']} / {s['score']}")
    if rec.get("blanked"):
        print("  blanked true MCUs:", ", ".join(f"{x[0]}-{x[1] - 1}" for x in rec["blanked"]))
    for k in ("out_path", "debug_path"):
        if rec.get(k):
            print(f"  {k}: {paths.root / rec[k]}")
    if a.json:
        print(json.dumps(rec, indent=1))


def cmd_ui(a, paths):
    import subprocess
    app = Path(__file__).with_name("ui_app.py")
    cmd = [sys.executable, "-m", "streamlit", "run", str(app),
           "--server.address", a.address, "--server.port", str(a.port),
           "--server.headless", "true", "--browser.gatherUsageStats", "false",
           "--server.maxUploadSize", "100", "--client.toolbarMode", "viewer",
           "--", "--root", str(paths.root)]
    print(f"Photo restore UI: http://localhost:{a.port}  (Ctrl+C to stop)")
    with contextlib.suppress(KeyboardInterrupt):
        subprocess.run(cmd, check=False)


def cmd_steps(a, paths):
    """Run several sub-commands in sequence in this process (used by the UI's background jobs)."""
    parser = build_parser()
    for step in json.loads(a.steps_json):
        sa = parser.parse_args(["--root", str(paths.root)] + list(step))
        _validate(parser, sa)
        sa.fn(sa, paths)


def build_parser():
    ap = argparse.ArgumentParser(prog="photo-restore",
                                 description="Restore phone-camera JPEGs with corrupted scan data.")
    ap.add_argument("--root", default=None, help="project folder (default ~/photo-restore or $PHOTO_RESTORE_ROOT)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def zip_args(p):
        p.add_argument("--zip", action="append", default=[], help="ZIP file (Windows or WSL path); repeatable")
        p.add_argument("--zip-dir", action="append", default=[], help="folder containing ZIPs; repeatable")

    def restore_args(p):
        p.add_argument("--jobs", "-j", type=int, default=None, help="worker processes (default CPU-1, limited by RAM)")
        p.add_argument("--force", action="store_true", help="redo images that already have output")
        p.add_argument("--only", action="append", default=None, help="glob on relative path or file name; repeatable")
        p.add_argument("--keep-full-size", action="store_true",
                       help="fill an unrecoverable tail with neutral grey instead of cropping")
        p.add_argument("--suffix", default=None, help="suffix added to output file names")
        p.add_argument("--damage-threshold", type=float, default=None, help="minimum damage threshold (default 25)")
        p.add_argument("--resync-accept", type=float, default=None, help="minimum resync accept score (default 8)")
        p.add_argument("--trim-threshold", type=float, default=None, help="trim threshold (default 18)")

    p = sub.add_parser("ingest", help="copy ZIPs into the project folder and extract them")
    zip_args(p)
    p.set_defaults(fn=cmd_ingest)
    p = sub.add_parser("restore", help="restore all extracted images")
    restore_args(p)
    p.set_defaults(fn=cmd_restore)
    p = sub.add_parser("report", help="build report.csv/json and index.html")
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser("all", help="ingest + restore + report")
    zip_args(p)
    restore_args(p)
    p.set_defaults(fn=cmd_all)
    p = sub.add_parser("inspect", help="restore one file verbosely")
    p.add_argument("file")
    p.add_argument("--debug", action="store_true", help="full-resolution debug image, debug logging")
    p.add_argument("--json", action="store_true", help="print the full result record")
    restore_args(p)
    p.set_defaults(fn=cmd_inspect)
    p = sub.add_parser("ui", help="start the browser UI (Streamlit)")
    p.add_argument("--port", type=int, default=8501)
    p.add_argument("--address", default="localhost", help="bind address (default localhost only)")
    p.set_defaults(fn=cmd_ui)
    p = sub.add_parser("steps", help=argparse.SUPPRESS)
    p.add_argument("steps_json")
    p.set_defaults(fn=cmd_steps)
    return ap


def _validate(ap, a):
    if a.cmd in ("all", "ingest") and not (a.zip or a.zip_dir):
        ap.error(f"{a.cmd}: give --zip or --zip-dir")


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    paths = Paths(Path(a.root).expanduser() if a.root else default_root())
    paths.ensure()
    _setup_logging(paths, a.verbose, a.cmd)
    _validate(ap, a)
    a.fn(a, paths)


if __name__ == "__main__":
    main()
