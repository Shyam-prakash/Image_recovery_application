"""Platform helpers: native Windows, WSL and plain Linux/macOS."""
import multiprocessing as mp
import os
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

IS_WINDOWS = os.name == "nt"


@lru_cache(maxsize=1)
def is_wsl() -> bool:
    if IS_WINDOWS or not sys.platform.startswith("linux"):
        return False
    try:
        with open("/proc/version", encoding="utf-8", errors="ignore") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


_WIN_PATH = re.compile(r"^[A-Za-z]:[\\/]|^\\\\")


def to_local_path(p: str) -> Path:
    """Turn a user-supplied path into a local Path.

    On Windows, paths are used as they are. On WSL, Windows paths (C:\\..., \\\\server\\...)
    are converted with `wslpath` (falling back to /mnt/<drive>/...). Quotes are stripped.
    """
    p = p.strip().strip('"').strip("'")
    if not IS_WINDOWS and _WIN_PATH.match(p):
        try:
            out = subprocess.run(["wslpath", "-u", p], capture_output=True, text=True, check=True)
            return Path(out.stdout.strip())
        except (OSError, subprocess.CalledProcessError):
            if p[1:2] == ":":
                return Path(f"/mnt/{p[0].lower()}{p[2:].replace(chr(92), '/')}")
    return Path(p).expanduser()


def display_path(p: Path) -> str:
    """Path as the user would type it in Windows Explorer (converted on WSL)."""
    if is_wsl():
        try:
            out = subprocess.run(["wslpath", "-w", str(p)], capture_output=True, text=True, check=True)
            return out.stdout.strip() or str(p)
        except (OSError, subprocess.CalledProcessError):
            pass
    return str(p)


def available_gb():
    """Free memory in GB, or None if unknown."""
    try:
        import psutil
        return psutil.virtual_memory().available / 1024 ** 3
    except Exception:
        return None


def mp_context(preload=("photo_restore.pipeline",)):
    """Process start method for the worker pool.

    Plain 'fork' can deadlock when the parent already runs library thread pools (OpenCV,
    OpenBLAS). 'forkserver' (Linux/WSL) forks workers from a clean, single-threaded helper
    that has the modules (and numba's compiled kernels) preloaded. Windows and macOS use 'spawn'.
    """
    if "forkserver" in mp.get_all_start_methods() and sys.platform != "darwin":
        ctx = mp.get_context("forkserver")
        ctx.set_forkserver_preload(list(preload))
        return ctx
    return mp.get_context("spawn")


def open_in_file_manager(p: Path) -> bool:
    """Open a file or folder with the desktop's default application. Returns success."""
    try:
        if IS_WINDOWS:
            os.startfile(str(p))  # noqa: S606  (Windows only)
        elif is_wsl():
            subprocess.Popen(["explorer.exe", display_path(p)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return True
    except OSError:
        return False
