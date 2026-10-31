"""Paths and tunable parameters."""
import os
from dataclasses import dataclass
from pathlib import Path


def default_root() -> Path:
    return Path(os.environ.get("PHOTO_RESTORE_ROOT", "~/photo-restore")).expanduser()


@dataclass
class Paths:
    root: Path

    @property
    def zips(self): return self.root / "input" / "zips"
    @property
    def extracted(self): return self.root / "input" / "extracted"
    @property
    def restored(self): return self.root / "output" / "restored"
    @property
    def other(self): return self.restored / "_other"
    @property
    def report(self): return self.root / "output" / "report"
    @property
    def debug(self): return self.report / "debug"
    @property
    def thumbs(self): return self.report / "thumbs"
    @property
    def results(self): return self.report / "results"   # per-image JSON results
    @property
    def logs(self): return self.root / "logs"

    def ensure(self):
        for p in (self.zips, self.extracted, self.restored, self.report, self.debug,
                  self.thumbs, self.results, self.logs):
            p.mkdir(parents=True, exist_ok=True)


@dataclass
class Params:
    # damage detection
    damage_threshold: float = 25.0     # mean abs diff at MCU top boundary (Y)
    damage_run: int = 3                # consecutive bad MCUs to call a damage point
    # resync
    resync_window: int = 24            # MCUs in the resync window
    resync_accept: float = 8.0         # max mean boundary error to accept a (j, s)
    shift_min: int = -24
    shift_max: int = 24
    resync_scan_rows: float = 4.0      # how far (in MCU rows) to scan forward for resync
    # trim
    trim_threshold: float = 18.0
    trim_look: int = 48
    # fill
    fill_maxgap: int = 64
    # tail detection
    tail_window: int = 4096
    tail_bad_markers: int = 6          # invalid FFxx per window to call it foreign data
    # output
    jpeg_quality: int = 95
    suffix: str = ""
    keep_full_size: bool = False
    # status
    review_fill_pct: float = 3.0
    review_seam: float = 7.5           # boundary error on a segment's first row vs image median
    review_hole_rows: int = 8
    review_weak_fit: float = 10.0      # resync score above this on a big segment -> review
