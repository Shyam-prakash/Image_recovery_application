"""Streamlit UI for photo-restore.  Start with:  photo-restore ui
(or: streamlit run photo_restore/ui_app.py -- --root ~/photo-restore)
"""
import argparse
import re
import sys
import time
from collections import Counter
from pathlib import Path

import streamlit as st
from PIL import Image, ImageFile, ImageOps

from photo_restore import jobs
from photo_restore.batch import list_inputs
from photo_restore.config import Params, Paths, default_root
from photo_restore.pipeline import process, rel_key
from photo_restore.report import ORDER, load_results
from photo_restore.sysutil import display_path, is_wsl, open_in_file_manager, to_local_path

ImageFile.LOAD_TRUNCATED_IMAGES = True

STATUS_LABEL = {
    "needs_review": ":material/warning: needs review", "failed": ":material/error: failed",
    "repaired_cropped": ":material/content_cut: repaired (cropped)", "repaired": ":material/check_circle: repaired",
    "unsupported": ":material/block: unsupported", "clean": ":material/verified: clean",
    "other": ":material/description: other",
}
STATUS_HELP = {
    "clean": "No damage found; the original was copied unchanged.",
    "repaired": "Restored at full size; small areas were filled in.",
    "repaired_cropped": "The end of the file was destroyed; the image was cropped above it.",
    "needs_review": "Restored with low confidence: check it.",
    "unsupported": "Not a baseline JPEG (e.g. progressive); copied unchanged.",
    "failed": "An error occurred; see the message.",
    "other": "Not a JPEG; copied to _other/.",
}
PAGE_SIZE = 24


# --------------------------------------------------------------------------- setup

def _root_from_argv() -> Path:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None)
    a, _ = ap.parse_known_args(sys.argv[1:])
    return Path(a.root).expanduser() if a.root else default_root()


st.set_page_config(page_title="Photo restore", page_icon=":material/photo_library:", layout="wide")
paths = Paths(_root_from_argv())
paths.ensure()


def results_signature():
    """Cheap fingerprint of the results folder so cached data refreshes after a run."""
    fs = list(paths.results.rglob("*.json"))
    return len(fs), max((f.stat().st_mtime for f in fs), default=0)


@st.cache_data(show_spinner=False)
def cached_results(_sig):
    return load_results(paths)


def results():
    return cached_results(results_signature())


@st.cache_data(show_spinner=False, max_entries=64)
def load_display(path: str, max_side: int, orientation: int = 0, mtime: float = 0.0):
    """Image for display: decoded leniently, EXIF orientation applied, downscaled."""
    try:
        im = Image.open(path)
        im.load()
    except Exception:
        return None
    if orientation:
        im = _apply_orientation(im, orientation)
    else:
        im = ImageOps.exif_transpose(im)
    im = im.convert("RGB")
    im.thumbnail((max_side, max_side))
    return im


@st.cache_data(show_spinner=False, max_entries=512)
def load_tile(path: str, mtime: float = 0.0, w: int = 480, h: int = 360):
    """Thumbnail fitted into a fixed 4:3 tile so the grid lines up."""
    try:
        im = Image.open(path).convert("RGB")
    except Exception:
        return None
    im.thumbnail((w, h))
    tile = Image.new("RGB", (w, h), (38, 39, 48))
    tile.paste(im, ((w - im.width) // 2, (h - im.height) // 2))
    return tile


def _apply_orientation(im, o):
    from photo_restore.pipeline import _TRANSPOSE
    return im.transpose(_TRANSPOSE[o]) if o in _TRANSPOSE else im


def mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def win_path(p: Path) -> str:
    return display_path(p)


# --------------------------------------------------------------------------- header

st.title(":material/photo_library: Photo restore")
st.caption(f"Project folder: `{paths.root}` · everything runs locally, nothing is uploaded")

recs = results()
counts = Counter(r.get("status") for r in recs)

cols = st.columns(len(ORDER))
for c, s in zip(cols, ORDER, strict=True):
    c.metric(STATUS_LABEL[s], counts.get(s, 0), help=STATUS_HELP[s])

tab_import, tab_restore, tab_results, tab_single = st.tabs(
    ["1 · Import ZIPs", "2 · Restore", "3 · Results", "Single photo"])


# --------------------------------------------------------------------------- job panel

def job_running():
    j = jobs.status(paths)
    return bool(j and j["running"])


@st.fragment(run_every=2)
def job_panel():
    j = jobs.status(paths)
    if not j:
        st.info("No job has been run from the UI yet.")
        return
    steps = " → ".join(s[0] for s in j["steps"])
    tail = jobs.log_tail(j, 60)
    m = re.findall(r"(\d+)/(\d+) \[", tail)
    if j["running"]:
        st.markdown(f"**Running:** {steps} · started {time.strftime('%H:%M:%S', time.localtime(j['started']))}")
        if m:
            done, total = map(int, m[-1])
            st.progress(done / max(total, 1), text=f"{done} / {total} photos")
        else:
            st.progress(0.0, text="starting…")
        if st.button("Stop job", key="stop", icon=":material/stop_circle:"):
            jobs.stop(paths)
            st.rerun()
    else:
        st.success(f"Last job finished: {steps}")
        if getattr(st.session_state, "_was_running", False):
            st.session_state._was_running = False
            cached_results.clear()
            st.rerun(scope="app")
    if j["running"]:
        st.session_state._was_running = True
    with st.expander("Log", expanded=j["running"]):
        st.code(tail or "(empty)", language=None)


# --------------------------------------------------------------------------- 1 import

with tab_import:
    st.subheader("Import ZIP files")
    where = ("Windows paths (`C:\\Users\\...\\file.zip`) and WSL paths (`/mnt/c/...`) both work. "
             if is_wsl() else "")
    st.write(f"Paste one path per line. {where}A folder adds every ZIP inside it. The ZIPs are "
             f"copied into the project folder and extracted there; your original files are never "
             f"modified. For loose photos, copy them into `{paths.extracted}` (any sub-folder).")
    default = st.session_state.get("zip_text", "")
    zip_text = st.text_area("ZIP files or folders", value=default, height=120,
                            placeholder="C:\\Users\\me\\Downloads\\drive-download-001.zip")
    st.session_state.zip_text = zip_text
    lines = [ln.strip() for ln in zip_text.splitlines() if ln.strip()]
    ok_args, problems = [], []
    for ln in lines:
        p = to_local_path(ln)
        if p.is_dir():
            ok_args += ["--zip-dir", ln]
        elif p.is_file() and p.suffix.lower() == ".zip":
            ok_args += ["--zip", ln]
        else:
            problems.append(f"not found or not a ZIP: `{ln}` → `{p}`")
    for pr in problems:
        st.warning(pr)
    c1, c2 = st.columns(2)
    run_all = c2.checkbox("Also restore and build the report afterwards", value=True)
    if c1.button("Import", icon=":material/unarchive:", type="primary", disabled=not ok_args or bool(problems) or job_running()):
        steps = [["ingest"] + ok_args]
        if run_all:
            steps += [["restore"], ["report"]]
        jobs.start(paths, steps)
        st.toast("Import started: see progress on the Restore tab")
        st.rerun()
    zips = sorted(paths.zips.glob("*.zip"))
    if zips:
        st.markdown("**Already imported:**")
        for z in zips:
            n = sum(1 for f in (paths.extracted / z.stem).rglob("*") if f.is_file()) if (paths.extracted / z.stem).exists() else 0
            st.markdown(f"- `{z.name}` ({z.stat().st_size / 1e6:.0f} MB, {n} files extracted)")


# --------------------------------------------------------------------------- 2 restore

with tab_restore:
    st.subheader("Restore all photos")
    inputs = list_inputs(paths)
    st.write(f"{len(inputs)} files in `input/extracted`. Photos that already have a result are "
             "skipped unless you tick **Redo everything**.")
    c1, c2, c3 = st.columns(3)
    force = c1.checkbox("Redo everything (--force)", value=False)
    keep_full = c2.checkbox("Keep full size (grey instead of crop)", value=False,
                            help="Unrecoverable bottom parts are filled with neutral grey instead of cropped.")
    jobs_n = c3.number_input("Parallel workers (0 = automatic)", min_value=0, max_value=32, value=0,
                             help="Automatic uses CPU count − 1, limited by free memory.")
    only = st.text_input("Only these files (optional, glob on name or path, comma separated)",
                         placeholder="IMG_*, *_damaged.jpg")
    args = ["restore"]
    if force:
        args.append("--force")
    if keep_full:
        args.append("--keep-full-size")
    if jobs_n:
        args += ["--jobs", str(int(jobs_n))]
    for pat in [x.strip() for x in only.split(",") if x.strip()]:
        args += ["--only", pat]
    b1, b2 = st.columns([1, 3])
    if b1.button("Start restore", icon=":material/play_arrow:", type="primary", disabled=job_running() or not inputs):
        jobs.start(paths, [args, ["report"]])
        st.rerun()
    flagged = [r for r in recs if r.get("status") in ("needs_review", "failed")]
    if b2.button(f"Redo only the {len(flagged)} flagged photos", icon=":material/replay:", disabled=job_running() or not flagged):
        a = ["restore", "--force"]
        for r in flagged:
            a += ["--only", r["path"]]
        jobs.start(paths, [a, ["report"]])
        st.rerun()
    st.divider()
    job_panel()
    st.divider()
    idx = paths.report / "index.html"
    o1, o2 = st.columns(2)
    if o1.button("Open restored photos folder", icon=":material/folder_open:") and \
            not open_in_file_manager(paths.restored):
        st.warning("Could not open a file manager here.")
    if idx.exists() and o2.button("Open HTML report", icon=":material/description:") and \
            not open_in_file_manager(idx):
        st.warning("Could not open the report here.")
    st.caption(f"Restored photos: `{win_path(paths.restored)}`  \nHTML report: `{win_path(idx)}`")


# --------------------------------------------------------------------------- 3 results

def detail_view(r, ctx):
    rel = r["path"]
    src = paths.extracted / rel
    out = paths.root / r["out_path"] if r.get("out_path") else None
    dbg = paths.root / r["debug_path"] if r.get("debug_path") else None
    orient = int(r.get("orientation") or 1)
    st.markdown(f"### {Path(rel).name}")
    st.caption(str(Path(rel).parent))
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Status", r["status"].replace("_", " "), help=STATUS_HELP.get(r["status"]))
    m2.metric("Confidence", f"{r['confidence']:.2f}" if r.get("confidence") is not None else "–")
    m3.metric("Filled", f"{r.get('filled_pct', 0):.1f} %")
    m4.metric("Damage points", r.get("damage_points", 0))
    size = "×".join(map(str, r["out_size"])) if r.get("out_size") else "–"
    m5.metric("Output size", size)
    if r.get("message"):
        st.info(r["message"])
    size_px = st.select_slider("Display size", options=[600, 900, 1300, 1800], value=900, key=f"dsize_{ctx}")
    v1, v2, v3 = st.columns(3)
    with v1:
        st.markdown("**Before** (as a normal viewer shows it)")
        im = load_display(str(src), size_px, 0, mtime(src))
        if im is not None:
            st.image(im)
        else:
            st.write("cannot decode")
    with v2:
        st.markdown("**After**")
        if out and out.exists():
            st.image(load_display(str(out), size_px, 0, mtime(out)))
        else:
            st.write("–")
    with v3:
        st.markdown("**Filled areas** (magenta = invented, dark red = unrecoverable)")
        if dbg and dbg.exists():
            st.image(load_display(str(dbg), size_px, orient, mtime(dbg)))
        else:
            st.write("no debug image (clean/unsupported)")
    d1, d2, d3 = st.columns(3)
    if out and out.exists():
        d1.download_button("Download restored photo", out.read_bytes(), file_name=out.name,
                           mime="image/jpeg", key=f"dl_{ctx}_{rel}", icon=":material/download:")
        d2.code(win_path(out), language=None)
    if r.get("segments"):
        with st.expander(f"How it was pieced together ({len(r['segments'])} segments)"):
            st.dataframe([dict(decoded=f"{s['decoded_start']}–{s['decoded_end']}", shift=s["shift"],
                               offsets=", ".join(str(x) for x in s["offsets"]), method=s["method"],
                               score=s["score"]) for s in r["segments"]], hide_index=True)
    if ctx == "results" and d3.button("Tune in Single photo tab", icon=":material/tune:", key=f"tune_{ctx}_{rel}"):
        st.session_state.single_path = rel
        st.toast("Selected — switch to the 'Single photo' tab")


@st.dialog("Before / after", width="large")
def details_dialog(r):
    detail_view(r, "results")


with tab_results:
    if not recs:
        st.info("No results yet. Import ZIPs and run a restore first.")
    else:
        options = [s for s in ORDER if counts.get(s)]
        chosen = st.pills("Show", options, selection_mode="multi", default=["needs_review"] if counts.get("needs_review") else options,
                          format_func=lambda s: f"{STATUS_LABEL[s]} ({counts.get(s, 0)})")
        f2, f3 = st.columns([2, 1])
        name_q = f2.text_input("Search name", "")
        view = f3.radio("Thumbnail", ["after", "filled areas", "before"], horizontal=True)
        sel = [r for r in recs if r.get("status") in (chosen or options) and name_q.lower() in r["path"].lower()]
        if not sel:
            st.write("Nothing matches.")
        else:
            pages = (len(sel) - 1) // PAGE_SIZE + 1
            page = st.number_input(f"Page (of {pages}) · {len(sel)} photos", 1, pages, 1) if pages > 1 else 1
            chunk = sel[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
            kind = {"after": "after", "filled areas": "debug", "before": "before"}[view]
            grid = st.columns(4)
            for i, r in enumerate(chunk):
                with grid[i % 4]:
                    t = paths.thumbs / (r["path"] + f".{kind}.jpg")
                    if not t.exists():
                        t = paths.thumbs / (r["path"] + ".after.jpg")
                    if t.exists():
                        st.image(load_tile(str(t), mtime(t)))
                    conf = f" · conf {r['confidence']:.2f}" if r.get("confidence") is not None else ""
                    st.caption(f"{STATUS_LABEL.get(r['status'], r['status'])}{conf}  \n`{Path(r['path']).name}`")
                    if st.button("Details", key=f"open_{r['path']}", icon=":material/zoom_in:"):
                        details_dialog(r)


# --------------------------------------------------------------------------- single photo

with tab_single:
    st.subheader("Restore one photo, with adjustable settings")
    st.write("Pick a photo from the imported set, or add a new JPEG. The result replaces that "
             "photo's output; the original is never touched.")
    up = st.file_uploader("Add a JPEG", type=["jpg", "jpeg"])
    if up is not None:
        dest = paths.extracted / "_uploads" / Path(up.name).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists() or dest.stat().st_size != up.size:
            dest.write_bytes(up.getvalue())
        st.session_state.single_path = str(rel_key(paths, dest))
    all_paths = [r["path"] for r in recs] or [str(rel_key(paths, p)) for p in list_inputs(paths)]
    if st.session_state.get("single_path") and st.session_state.single_path not in all_paths:
        all_paths = [st.session_state.single_path] + all_paths
    default_i = all_paths.index(st.session_state.single_path) if st.session_state.get("single_path") in all_paths else 0
    choice = st.selectbox("Photo", all_paths, index=default_i if all_paths else None,
                          format_func=lambda p: f"{Path(p).name}   ({Path(p).parent})")
    with st.expander("Settings (defaults are tuned on this photo set)", expanded=False):
        p0 = Params()
        c1, c2, c3 = st.columns(3)
        dmg = c1.slider("Damage threshold", 10.0, 60.0, p0.damage_threshold, 1.0,
                        help="Boundary mismatch (grey levels) that counts as damage. Raise it for very detailed photos.")
        acc = c2.slider("Resync accept", 4.0, 20.0, p0.resync_accept, 0.5,
                        help="How well a re-aligned stretch must fit the rows above. Lower = stricter.")
        trim = c3.slider("Trim threshold", 8.0, 40.0, p0.trim_threshold, 1.0,
                         help="How strongly an MCU at a damage edge must disagree before it is blanked and filled.")
        keep = st.checkbox("Keep full size (grey instead of crop)", value=False, key="single_keep")
    if choice and st.button("Restore this photo", icon=":material/play_arrow:", type="primary", disabled=job_running()):
        params = Params(damage_threshold=dmg, resync_accept=acc, trim_threshold=trim, keep_full_size=keep)
        with st.spinner("Restoring… (large photos can take up to a minute)"):
            rec = process(paths.extracted / choice, paths, params, force=True)
        cached_results.clear()
        st.session_state.single_rec = rec
    if job_running():
        st.caption("A batch job is running; wait for it to finish before restoring a single photo.")
    rec = st.session_state.get("single_rec")
    if rec and rec.get("path") == choice:
        detail_view(rec, "single")
    elif choice:
        rmap = {r["path"]: r for r in recs}
        if choice in rmap:
            detail_view(rmap[choice], "single")
