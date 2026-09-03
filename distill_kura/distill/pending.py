"""Batches whose model call failed, kept until they are drunk for real.

The watermark reserves a stretch of journal BEFORE the batch is read, and it is never
rolled back — that reservation is what keeps two distillers off the same water
(`watermark.py`). The cost of that design is this file: once the mark has moved, the
only thing standing between a failed brain call and journal that no one will ever read
again is a durable copy of the segments themselves.

So a batch the brain never answered is not "0 candidates". It is written here, exactly
as it was sipped, and the next pass works it off before it sips new water. Nothing is
dropped silently: a batch that has exhausted its attempts stays on the shelf with
`retryable: false` and a reason a person can read.

The same no-loss rule applies one step further down the line, with a different unit:
when the SCRIBE is unreachable the candidate and its evidence packet are kept (in the
`pending-compose` shelf), because the journal it came from is behind the mark and
cannot be read for it again.

`retryable: false` is the end of automatic work, never the end of the record.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone

PENDING_VERSION = 1
MAX_ATTEMPTS = 5             # transient/bad_reply: past this it waits for a person

# A prompt that does not fit is not a transport hiccup: retrying it unchanged is the one
# failure mode guaranteed to fail again. The wordings are the ones our own mouths use —
# vLLM, llama.cpp, and the assertion our DSH mouth raises as an HTTP 400.
_OVERFLOW = re.compile(
    r"maximum context length|exceeds the available context|context length|context window|"
    r"context_length_exceeded|prompt is too long|too many tokens|reduce the length",
    re.I)
_ASSERT = re.compile(r"assertion", re.I)
_ASSERT_WORDS = re.compile(r"\bpages?\b|\bcache\b|\btokens?\b", re.I)


def failure_kind(detail: str) -> str:
    """`context_overflow` or `transient`, from what the endpoint left in `last_error`.

    Only overflow changes what we DO (split instead of retry), so anything not
    recognisably an overflow is treated as transient — a wrong key retried five times
    is noise, a batch split on a wrong guess is work done for nothing, but a batch
    dropped because we guessed wrong is loss."""
    d = detail or ""
    if _OVERFLOW.search(d):
        return "context_overflow"
    if _ASSERT.search(d) and _ASSERT_WORDS.search(d):
        return "context_overflow"
    return "transient"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()[:19]


class PendingShelf:
    """One directory of JSON records, oldest first. No index and no lock: a record is
    written whole (tmp + rename) and read by whoever gets to it — two distillers
    working the same shelf at worst repeat one batch, which is the recoverable
    direction."""

    def __init__(self, directory: str):
        self.dir = directory

    def save(self, rec: dict, path: str | None = None) -> str:
        """Write (or rewrite, when `path` is given) one record. Returns its path."""
        rec = {**rec, "v": PENDING_VERSION, "at": _now()}
        rec.setdefault("first_at", rec["at"])
        rec.setdefault("attempt", 1)
        rec.setdefault("retryable", True)
        os.makedirs(self.dir, exist_ok=True)
        if path is None:
            stamp = f"{time.time():.6f}"
            seed = json.dumps([rec.get("key"), rec.get("start"), stamp], ensure_ascii=False)
            base = f"{stamp.replace('.', '')}-{hashlib.sha1(seed.encode()).hexdigest()[:8]}"
            path = os.path.join(self.dir, base + ".json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return path

    def load(self) -> list[tuple[str, dict]]:
        """Every readable record, oldest first. A file that will not parse is left
        where it is — it is evidence of a bug, and deleting it would be the very loss
        this shelf exists to prevent."""
        out = []
        for p in sorted(glob.glob(os.path.join(self.dir, "*.json"))):
            try:
                with open(p, encoding="utf-8") as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict):
                out.append((p, rec))
        return out

    def drop(self, path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass
