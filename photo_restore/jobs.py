"""Background jobs (ingest / restore / report) started from the UI.

A job is a separate `photo-restore steps ...` process, so it keeps running if the browser tab is
closed or the UI is restarted. Its state lives in <root>/logs/ui_job.json. Works on Windows,
WSL, Linux and macOS.
"""
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

from .config import Paths
from .sysutil import IS_WINDOWS


def _state_file(paths: Paths) -> Path:
    return paths.logs / "ui_job.json"


def start(paths: Paths, steps: list) -> dict:
    """Run CLI steps in sequence, e.g. [["ingest", "--zip", p], ["restore"], ["report"]]."""
    cur = status(paths)
    if cur and cur["running"]:
        raise RuntimeError("a job is already running")
    paths.ensure()
    log = paths.logs / f"ui-job-{time.strftime('%Y%m%d-%H%M%S')}.log"
    cmd = [sys.executable, "-m", "photo_restore.cli", "--root", str(paths.root), "steps", json.dumps(steps)]
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    kw = {}
    if IS_WINDOWS:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    with open(log, "w", encoding="utf-8") as fh:
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=str(paths.root), env=env, **kw)
    info = dict(pid=proc.pid, create_time=psutil.Process(proc.pid).create_time(), steps=steps,
                started=time.time(), log=str(log))
    _state_file(paths).write_text(json.dumps(info), encoding="utf-8")
    return info


def _process(info):
    """The job's process if it is still alive (guards against PID reuse)."""
    try:
        p = psutil.Process(info["pid"])
        if abs(p.create_time() - info.get("create_time", p.create_time())) > 1:
            return None
        if p.status() == psutil.STATUS_ZOMBIE:
            return None
        return p
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def status(paths: Paths):
    f = _state_file(paths)
    if not f.exists():
        return None
    try:
        info = json.loads(f.read_text(encoding="utf-8"))
    except ValueError:
        return None
    info["running"] = _process(info) is not None
    return info


def stop(paths: Paths) -> bool:
    """Terminate the running job and its worker processes."""
    info = status(paths)
    p = _process(info) if info else None
    if p is None:
        return False
    procs = p.children(recursive=True) + [p]
    for q in procs:
        with contextlib.suppress(psutil.NoSuchProcess):
            q.terminate()
    _, alive = psutil.wait_procs(procs, timeout=5)
    for q in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            q.kill()
    return True


def log_tail(info, n=40) -> str:
    try:
        text = Path(info["log"]).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # tqdm writes progress with carriage returns: keep the last state of each line
    lines = [ln.split("\r")[-1] for ln in text.splitlines()]
    return "\n".join(lines[-n:])
