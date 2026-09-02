"""How far we have drunk from each journal — and how two distillers avoid the same water.

Two mistakes were made here and both are fixed in this file:

**Rewind.** Two distillers running in parallel each read the marks dict, each wrote
their own back, and the later write erased the other's progress — the same stretch was
re-processed a dozen times. The fix is BOTH halves: `flock` to serialise, and `max()`
to merge. A lock alone still lets a stale snapshot win.

**Drinking before reserving.** Reserving the stretch *before* reading it is what keeps
two runners apart. Advance-after-read leaves a window where the other runner starts on
the same offset.

Watermark units are the source adapter's business (byte offset for append-only files,
sequence number for rewritten archives) — this module only stores integers.
"""
from __future__ import annotations

import fcntl
import json
import os
from typing import NamedTuple

from .sources import Source, call_claim_bound, source_for


class Claim(NamedTuple):
    path: str
    start: int
    end: int
    source: Source
    scan_pending: int = 0


class Watermarks:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def read(self) -> dict[str, int]:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except ValueError:
            return {}

    def _write(self, cur: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def advance(self, key: str, pos: int) -> None:
        """Move forward only. A stale value must never pull the mark backwards."""
        with open(self.path + ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                cur = self.read()
                cur[key] = max(cur.get(key, 0), int(pos))
                self._write(cur)
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)

    def claim(self, files: list[str], budget_chars: int,
              min_chars: int) -> Claim | None:
        """Reserve the next stretch worth drinking.

        Returns a Claim. ``end`` is the reserved watermark unit sip must not read
        past — a second runner may already own bytes/events after it. When
        ``scan_pending`` is non-zero the watermark did not move: a source made
        bounded discard progress through an irreversibly-oversized line and the
        caller must retry without treating silence as completion.
        """
        with open(self.path + ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                cur = self.read()
                for path in files:
                    src = source_for(path)
                    if not src:
                        continue
                    k = src.key(path)
                    start = cur.get(k, 0)
                    # The reserve must be the window sip() will ACTUALLY consume with
                    # the same budget — twice now it has not been. In DSH it was 2.2×
                    # larger; in the claude adapter it was budget*4 BYTES against a
                    # read that stops on kept CHARACTERS (80 KB reserved, 30 KB read,
                    # on an ASCII journal). Both times the mark outran the read and
                    # every chunk's unread tail was skipped forever. Claiming less than
                    # sip reads is recoverable (advance() moves the mark to the true
                    # stop); claiming more is silent loss, the unforgivable direction.
                    # So no adapter may compute this by a second rule: claim_bound()
                    # takes the same walk sip() takes, and pays the second read.
                    end, approx, scan_pending = call_claim_bound(
                        src, path, start, budget_chars)
                    if approx >= min_chars and end > start:
                        cur[k] = end
                        self._write(cur)
                        return Claim(path, start, end, src)
                    if scan_pending > 0 and end <= start:
                        return Claim(path, start, start, src, scan_pending=scan_pending)
                return None
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)
