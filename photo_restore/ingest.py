"""Copy ZIPs from Windows into WSL and extract them safely."""
import contextlib
import logging
import os
import re
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

from .sysutil import to_local_path

log = logging.getLogger(__name__)


to_wsl_path = to_local_path      # backwards-compatible name


def collect_zips(zips, zip_dirs):
    found = []
    for z in zips or []:
        found.append(to_local_path(z))
    for d in zip_dirs or []:
        dp = to_local_path(d)
        found.extend(sorted(x for x in dp.iterdir() if x.suffix.lower() == ".zip"))
    for f in found:
        if not f.is_file():
            raise FileNotFoundError(f"ZIP not found: {f}")
    return found


def copy_zip(src: Path, dest_dir: Path) -> Path:
    dest = dest_dir / src.name
    if dest.exists() and dest.stat().st_size == src.stat().st_size:
        log.info("already copied: %s", dest.name)
        return dest
    tmp = dest.with_suffix(dest.suffix + ".part")
    log.info("copying %s -> %s", src, dest)
    shutil.copyfile(src, tmp)
    tmp.rename(dest)
    return dest


def _decode_name(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if not (info.flag_bits & 0x800):
        # legacy encoding: zipfile decoded as cp437; try utf-8 first
        with contextlib.suppress(UnicodeEncodeError, UnicodeDecodeError):
            name = name.encode("cp437").decode("utf-8")
    return name


def safe_member_path(base: Path, name: str):
    """Return the target path for a member, or None if it would escape base (zip-slip)."""
    name = name.replace("\\", "/")
    parts = [x for x in PurePosixPath(name).parts if x not in ("", ".")]
    if not parts or any(x == ".." for x in parts) or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    target = (base / Path(*parts)).resolve()
    if not str(target).startswith(str(base.resolve()) + os.sep):
        return None
    return target


def extract_zip(zpath: Path, extracted_root: Path) -> list:
    """Extract into extracted_root/<zip stem>/ and make files read-only."""
    stem = zpath.stem
    base = extracted_root / stem
    base.mkdir(parents=True, exist_ok=True)
    out = []
    with zipfile.ZipFile(zpath) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = _decode_name(info)
            target = safe_member_path(base, name)
            if target is None:
                log.warning("skipping unsafe member %r in %s", name, zpath.name)
                continue
            if target.exists() and target.stat().st_size == info.file_size:
                out.append(target)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.chmod(stat.S_IWUSR | stat.S_IRUSR)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            out.append(target)
    log.info("extracted %d files from %s", len(out), zpath.name)
    return out


def ingest(paths, zips=None, zip_dirs=None):
    paths.ensure()
    srcs = collect_zips(zips, zip_dirs)
    if not srcs:
        raise SystemExit("no ZIPs given (use --zip or --zip-dir)")
    files = []
    for s in srcs:
        local = copy_zip(s, paths.zips)
        files += extract_zip(local, paths.extracted)
    return files
