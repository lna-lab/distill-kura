"""Where the raw material comes from, and how it is CLASSED.

Everything the distiller reads is turned into segments carrying an evidence class:

    [USER]  the human's own words      — primary evidence
    [TOOL]  machine output             — the ONLY place a measured number may come from
    [ACT]   a tool the agent invoked   — evidence that an action was taken
    [SELF]  the agent's own prose      — a judgement worth keeping, never a bare fact

Reasoning / thinking blocks are dropped: an inner monologue is not evidence.
Injected content (system reminders, runtime context) is not the human speaking. How
that is detected is adapter-specific, because the harnesses inject differently: see
the two filters below.

Four adapters ship here; add your own by subclassing `Source` and registering it
in `SOURCES`. `watermark` semantics differ per adapter, so each one owns them:
byte offset for append-only files, sequence number for rewritten archives.
"""
from __future__ import annotations

import glob
import inspect
import json
import os
import re
import subprocess
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime

from ..store import contained

MAX_TOOL = 1500      # tools are verbose; the head is enough to ground a number
MAX_SEG = 4000
MAX_LINE = 32 * 1024  # raw JSONL line, including the newline; bound before json.loads
SCAN_LIMIT = MAX_LINE * 10  # unterminated tail: bounded per read, never scan to EOF
MAX_ID = 256          # event_id / session_id / turn_id; oversized is skipped, never sliced
MAX_TIMESTAMP = 40    # RFC3339 date-time with timezone; ordinary values fit with room
MAX_CLASS = 32

# RFC3339 date-time with a timezone. The clock is not consulted; a miss is a skip.
# Accepted: 2026-08-27T00:00:00Z | .123Z | +09:00 | -00:00
# Rejected: missing/non-string, date-only, naive, space-separator, leap seconds,
# offsets with seconds. Parsed by datetime.fromisoformat after Z → +00:00.
_RFC3339 = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})\Z"
)


def _rfc3339(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > MAX_TIMESTAMP:
        return False
    if _RFC3339.match(value) is None:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _cap(cls: str) -> int:
    """Per-class ceiling on one segment's text. Tools are verbose; the head is enough
    to ground a number. One rule, because the adapters wrote it out separately: the
    Claude path cut to MAX_TOOL and then again to MAX_SEG and only agreed with the DSH
    path because MAX_TOOL happens to be the smaller constant."""
    return MAX_TOOL if cls == "TOOL" else MAX_SEG

CLASSES = ("USER", "TOOL", "ACT", "SELF")


@dataclass
class IntakeSkip:
    """One skipped record. Offset and size only — never the payload."""
    reason: str
    at: int
    size: int


@dataclass
class IntakeReport:
    """Bounded skip accounting for one sip. A diagnostic must not flood or throw.

    Reasons are a closed set: malformed, unknown_version, unknown_class, missing,
    blank, oversized, partial, invalid. Samples cap at MAX_SAMPLES; counts do not.
    Nothing here is a path, an id, a credential, or evidence text.
    """
    skipped: dict[str, int] = field(default_factory=dict)
    samples: list[IntakeSkip] = field(default_factory=list)
    MAX_SAMPLES = 16
    MAX_SIZE_REPORTED = MAX_LINE

    def note(self, reason: str, at: int, size: int) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1
        if len(self.samples) < self.MAX_SAMPLES:
            self.samples.append(IntakeSkip(
                reason, max(0, int(at)),
                min(max(0, int(size)), self.MAX_SIZE_REPORTED)))

    @property
    def total(self) -> int:
        return sum(self.skipped.values())

    def as_dict(self) -> dict:
        return {
            "skipped": dict(self.skipped),
            "samples": [{"reason": s.reason, "at": s.at, "size": s.size}
                        for s in self.samples],
        }


@dataclass
class Segment:
    cls: str
    text: str

    def as_line(self) -> str:
        return f"[{self.cls}] {self.text}"


def as_evidence(segs: list[Segment]) -> str:
    """What the model sees. The class tags stay on — they are the judgement material."""
    return "\n".join(s.as_line() for s in segs)


class Source:
    """One kind of journal. Watermarks are opaque ints owned by the adapter."""
    name = "base"

    def matches(self, path: str) -> bool:
        raise NotImplementedError

    def key(self, path: str) -> str:
        """Watermark key. Must be unique across files (basenames often collide)."""
        return f"{self.name}:{os.path.abspath(path)}"

    def discover(self, root: str) -> list[str]:
        raise NotImplementedError

    def sip(self, path: str, start: int, limit_chars: int,
            report: IntakeReport | None = None) -> tuple[list[Segment], int]:
        """Read past the watermark. Returns (segments, new watermark).

        `report` collects bounded skip reasons; it must never carry payloads.
        """
        raise NotImplementedError

    def claim_bound(self, path: str, start: int, budget_chars: int) -> tuple[int, int, int]:
        """Reserve a stretch before drinking it, so parallel runs never overlap.

        Returns ``(end watermark, approximate chars in the stretch, scan_pending)``.
        ``scan_pending`` is a bounded byte count of progressive discard work done
        this call while the watermark stayed at ``start`` — only adapters that
        scan past irreversibly-oversized lines may return a non-zero value.
        """
        raise NotImplementedError


def call_sip(src: Source, path: str, start: int, limit_chars: int, *,
             report: IntakeReport | None = None,
             bound_end: int | None = None) -> tuple[list[Segment], int]:
    """Invoke sip with only kwargs the adapter accepts.

    Pre-existing custom sources may implement only ``sip(path, start, limit_chars)``.
    Those legacy adapters ignore ``report`` and ``bound_end``; ``call_sip`` never
    passes ``bound_end`` unless the adapter's signature accepts it, so concurrent
    reservation isolation is only promised for built-in adapters (and custom ones
    that honour ``bound_end``). A legacy adapter on a growing append-only file can
    read past a peer's reservation — keep custom sources on single-runner paths
    or upgrade them to accept ``bound_end``.
    """
    params = inspect.signature(src.sip).parameters
    kwargs: dict = {}
    if "report" in params:
        kwargs["report"] = report
    if "bound_end" in params:
        kwargs["bound_end"] = bound_end
    return src.sip(path, start, limit_chars, **kwargs)


def call_claim_bound(src: Source, path: str, start: int, budget_chars: int) -> tuple[int, int, int]:
    """Invoke claim_bound; normalize legacy ``(end, approx)`` to ``(end, approx, 0)``.

    Pre-existing custom sources may return only two values. The optional third is
    ``scan_pending``: bounded discard bytes done this call while the watermark stayed
    at ``start`` (only irreversibly-oversized-line adapters use it). Any other arity
    or non-tuple return fails loudly — nothing is guessed.
    """
    raw = src.claim_bound(path, start, budget_chars)
    if not isinstance(raw, tuple):
        raise TypeError(f"{type(src).__name__}.claim_bound returned {type(raw).__name__}, "
                        "expected tuple")
    if len(raw) == 2:
        end, approx = raw
        return int(end), int(approx), 0
    if len(raw) == 3:
        end, approx, pending = raw
        return int(end), int(approx), int(pending)
    raise ValueError(f"{type(src).__name__}.claim_bound returned {len(raw)} values, "
                     "expected 2 or 3")


# ── Claude Code / plain JSONL transcripts (append-only → byte watermark) ─────

class ClaudeCodeSource(Source):
    """`~/.claude/projects/<project>/<session>.jsonl`, one JSON event per line."""
    name = "claude"

    def matches(self, path: str) -> bool:
        return path.endswith(".jsonl") and not path.endswith(".evidence.jsonl")

    def key(self, path: str) -> str:
        return "claude:" + os.path.basename(path)

    def discover(self, root: str) -> list[str]:
        found = [f for f in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
                 if not f.endswith(".evidence.jsonl")]
        return sorted(found, key=os.path.getmtime, reverse=True)

    @staticmethod
    def _text_of(part) -> str:
        if isinstance(part, str):
            return part
        if isinstance(part, dict):
            if part.get("type") == "text":
                return part.get("text") or ""
            if part.get("type") == "tool_result":
                c = part.get("content")
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    return " ".join(x.get("text", "") for x in c if isinstance(x, dict))
        return ""

    def _walk(self, path: str, start: int, limit_chars: int,
              bound_end: int | None = None) -> tuple[list[Segment], int, int]:
        """The one line-walk: sip() and claim_bound() both come through here.

        A reserve computed by a second, cheaper rule drifts from the read. This bound
        used to assume 4 bytes per character and reserve `budget * 4` bytes, while the
        read stops when the KEPT characters reach the budget: on a 4000-line ASCII
        journal it reserved 80 KB against a true stop of 30 KB, and the 50 KB between
        was marked drunk without ever being read — 62% of every stretch, silently,
        forever. English and code journals are the common case, so it was happening in
        production. Same rules, same tally, same stopping condition, by construction.

        Returns (segments, stop offset, kept chars).
        """
        segs: list[Segment] = []
        total = 0
        with open(path, "rb") as h:
            h.seek(start)
            while True:
                pos = h.tell()
                if bound_end is not None and pos >= bound_end:
                    return segs, pos, total
                line = h.readline()
                if not line:
                    break
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue          # a stray non-object line is not a message
                t = d.get("type")
                # A subagent's transcript records the PARENT MODEL's instructions as
                # `type: user` with `isSidechain: true`. That text is model-written:
                # classed [USER] it would be the human's own words, and "the owner
                # approved X" in a delegation prompt would pass the gate as a decision.
                # Tool results stay [TOOL]; everything else in a sidechain is [SELF].
                side = bool(d.get("isSidechain"))
                msg = d.get("message")
                c = msg.get("content") if isinstance(msg, dict) else None
                parts = c if isinstance(c, list) else ([c] if isinstance(c, str) else [])
                for p in parts:
                    cls = None
                    if t == "user":
                        if isinstance(p, dict) and p.get("type") == "tool_result":
                            cls = "TOOL"
                        else:
                            cls = "SELF" if side else "USER"
                    elif t == "assistant":
                        if isinstance(p, dict) and p.get("type") == "text":
                            cls = "SELF"
                        elif isinstance(p, dict) and p.get("type") == "tool_use":
                            txt = f"{p.get('name')} {json.dumps(p.get('input', {}), ensure_ascii=False)[:600]}"
                            segs.append(Segment("ACT", txt)); total += len(txt)
                            continue
                    if not cls:
                        continue
                    txt = self._text_of(p).strip()
                    # SUBSTRING, deliberately: Claude Code appends <system-reminder>
                    # blocks INSIDE the human's own text part, and the transcript
                    # carries no provenance label, so a prefix test would never fire.
                    # The whole part is dropped rather than risk laundering injected
                    # text into [USER]. DshSource can afford a prefix test because it
                    # has `source.kind` to decide provenance first.
                    if not txt or "system-reminder" in txt or txt.startswith("<local-command"):
                        continue
                    txt = txt[:_cap(cls)]
                    segs.append(Segment(cls, txt))
                    total += len(txt)
                if total >= limit_chars:
                    return segs, h.tell(), total
            return segs, h.tell(), total

    def sip(self, path: str, start: int, limit_chars: int,
            report: IntakeReport | None = None,
            bound_end: int | None = None) -> tuple[list[Segment], int]:
        segs, end, _ = self._walk(path, start, limit_chars, bound_end=bound_end)
        return segs, end

    def claim_bound(self, path: str, start: int, budget_chars: int) -> tuple[int, int, int]:
        size = os.path.getsize(path)
        # A kept character never costs less than one byte of the line it came from
        # (UTF-8 never shrinks, and the JSON scaffolding around the text is pure
        # surplus), so a stretch shorter than the budget can never fill it: the walk
        # would run to EOF. That makes this shortcut exact rather than estimated —
        # which matters because catch_up() asks for the whole file with a budget of
        # 2**40 and must not JSON-parse a year of journals to be told where it ends.
        if size - start <= budget_chars:
            return size, max(0, size - start), 0
        # Otherwise walk the lines a second time: reserving costs a re-read of a few
        # tens of KB, guessing costs journal. `approx` is the raw stretch, not the
        # kept chars — it only feeds the "worth waking the model" filter, and erring
        # HIGH there at worst spends a pass that finds nothing and moves on, while
        # erring low would park the mark forever on a journal of nothing but
        # sidechain and system-reminder lines.
        _, end, _ = self._walk(path, start, budget_chars)
        return end, max(0, end - start), 0


# ── DeepSeek Harness sessions (zstd, rewritten → sequence watermark) ─────────

class DshSource(Source):
    """`<DSH_HOME>/sessions/<dir>/session.jsonl.zstd`.

    The archive is REWRITTEN, not appended, so a byte offset lies. The event
    `seq` counter is the honest watermark. The key must include the session
    directory: every file is literally named `session.jsonl.zstd`.
    """
    name = "dsh"

    def matches(self, path: str) -> bool:
        return path.endswith(".jsonl.zstd")

    def key(self, path: str) -> str:
        return "dsh:" + os.path.basename(os.path.dirname(path))

    def discover(self, root: str) -> list[str]:
        return sorted(glob.glob(os.path.join(root, "**", "session.jsonl.zstd"), recursive=True),
                      key=os.path.getmtime, reverse=True)

    @staticmethod
    def _lines(path: str):
        try:
            p = subprocess.run(["zstd", "-dc", path], capture_output=True, timeout=300)
        except FileNotFoundError:
            raise RuntimeError("zstd is not installed; DSH session archives cannot be read")
        if p.returncode != 0:
            # A corrupt archive used to yield no lines at all: every event was
            # "skipped", the watermark never moved, and the session read as drunk
            # on every pass — a silent hole in the journal.
            raise RuntimeError(f"zstd failed on {path} (rc={p.returncode}): "
                               f"{p.stderr.decode(errors='replace')[:200]}")
        for line in p.stdout.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                yield d

    @staticmethod
    def _classify(d: dict) -> Segment | None:
        t = d.get("type")
        data = d.get("data")
        if not isinstance(data, dict):
            return None
        if t == "user/message":
            if (data.get("source") or {}).get("kind") != "user":
                return None                       # injected context is not the human
            txt = " ".join(c.get("text", "") for c in (data.get("content") or [])
                           if isinstance(c, dict) and c.get("type") == "text").strip()
            # Prefix, not substring: `source.kind` above has already decided
            # provenance, so this is belt-and-braces. (ClaudeCodeSource has no such
            # label and must match anywhere in the part — see its walk.)
            if not txt or txt.startswith("<system-reminder") or txt.startswith("Current runtime context"):
                return None
            return Segment("USER", txt)
        if t == "assistant/chunk":
            c = data.get("chunk") or {}
            if c.get("type") == "block-end":
                b = c.get("block") or {}
                if b.get("type") == "text" and (b.get("text") or "").strip():
                    return Segment("SELF", b["text"].strip())
            return None                            # reasoning blocks are dropped
        if t == "tool/call":
            return Segment("ACT", f"{data.get('name')} {(data.get('arguments') or '')[:600]}")
        if t == "tool/result":
            parts = []
            for c in (data.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool-result":
                    for cc in c.get("content") or []:
                        if isinstance(cc, dict) and cc.get("type") == "text":
                            parts.append(cc.get("text", ""))
            txt = "\n".join(parts).strip()
            return Segment("TOOL", txt) if txt else None
        return None

    def _walk(self, path: str, start: int, limit_chars: int,
              bound_end: int | None = None) -> tuple[list[Segment], int, int]:
        """The one event-walk, breaking only after a segment is counted — so the
        reserved end is exactly where the read with this budget stops. Written twice,
        these two loops drifted: breaking on unclassified events too let the marks run
        ahead of the reads, and every chunk's unread tail was skipped forever (two
        thirds of a DSH journal, measured). One walk cannot disagree with itself.

        Returns (segments, sequence watermark, kept chars).
        """
        segs: list[Segment] = []
        total, last = 0, start
        for d in self._lines(path):
            seq = d.get("seq")
            if seq is None or seq <= start:
                continue
            if bound_end is not None and seq > bound_end:
                break
            last = max(last, seq)
            s = self._classify(d)
            if not s:
                continue
            s.text = s.text[:_cap(s.cls)]
            segs.append(s)
            total += len(s.text)
            if total >= limit_chars:
                break
        return segs, last, total

    def sip(self, path: str, start: int, limit_chars: int,
            report: IntakeReport | None = None,
            bound_end: int | None = None) -> tuple[list[Segment], int]:
        segs, last, _ = self._walk(path, start, limit_chars, bound_end=bound_end)
        return segs, last

    def claim_bound(self, path: str, start: int, budget_chars: int) -> tuple[int, int, int]:
        # Exact in both numbers: the same walk, so the reserve lands on the same event
        # the read stops at. The archive is decompressed twice (once to reserve, once
        # to drink) — a few hundred ms against a stretch of journal lost in silence.
        _, end, total = self._walk(path, start, budget_chars)
        return end, total, 0


# ── Classified evidence JSONL (append-only → byte watermark) ───────────────

@dataclass
class _ScanCursor:
    pos: int
    anchor: bytes
    cursor_anchor: bytes
    file_size: int
    mtime_ns: int
    ctime_ns: int


class EvidenceJsonlSource(Source):
    """`*.evidence.jsonl` — one versioned, class-tagged event per line.

    Writers append complete JSON objects; a crash may leave a partial final line.
    The watermark stops before that line so the next append can finish it. Invalid
    lines are dropped, never reclassified, and counted on an IntakeReport.

    Progressive oversized-line discard assumes an append-only source contract.
    Cached scan cursors are fail-closed defence against observable truncation,
    inode/path replacement, same-size rewrites (via ``st_mtime_ns`` /
    ``st_ctime_ns`` generation), and in-place mutation near a saved cursor
    (via head and cursor anchors). A malicious rewrite that grows the file and
    preserves every checked anchor is not claimed detectable here.

    Minimum shape (schema_version 1)::

        {"schema_version": 1, "event_id": "…", "session_id": "…", "turn_id": "…",
         "class": "USER"|"SELF"|"ACT"|"TOOL", "text": "…",
         "timestamp": "RFC3339 date-time with timezone"}

    `timestamp` is a JSON string matching `YYYY-MM-DDTHH:MM:SS[.frac](Z|±HH:MM)`,
    parsed by `datetime.fromisoformat` after a trailing `Z` is rewritten to
    `+00:00`. Date-only, naive, non-string, space-separator, leap-second, and
    `±HH:MM:SS` values are rejected. The clock is not consulted and the record
    is not rewritten; the timestamp is a gate, not a stored field.

    Identity fields (`event_id`, `session_id`, `turn_id`) are at most 256
    characters; a raw line is at most 32 KiB. Oversized values are skipped,
    never truncated into a valid identity. Ordinary UUIDs, ULIDs, and hex
    digests fit.
    """
    name = "evidence"
    _ANCHOR_LEN = 16
    # ponytail: raise if many concurrent unterminated huge tails exceed this cap;
    # eviction only restarts an unreserved progressive scan, never a reserved sip.
    _SCAN_STATE_CAP = 256

    def __init__(self) -> None:
        self._scan_lock = threading.Lock()
        self._scan_cursors: OrderedDict[
            tuple[str, int, int, int], _ScanCursor
        ] = OrderedDict()

    def matches(self, path: str) -> bool:
        return path.endswith(".evidence.jsonl")

    def key(self, path: str) -> str:
        return "evidence:" + os.path.abspath(path)

    def discover(self, root: str) -> list[str]:
        return sorted(glob.glob(os.path.join(root, "**", "*.evidence.jsonl"), recursive=True),
                      key=os.path.getmtime, reverse=True)

    @staticmethod
    def _note(report: IntakeReport | None, reason: str, at: int, size: int) -> None:
        if report is not None:
            report.note(reason, at, size)

    @staticmethod
    def _scan_key(path: str, line_start: int, st_dev: int, st_ino: int) -> tuple[str, int, int, int]:
        return (os.path.abspath(path), line_start, st_dev, st_ino)

    def _anchor_ok(self, h, line_start: int, anchor: bytes) -> bool:
        if not anchor:
            return True
        here = h.tell()
        h.seek(line_start)
        ok = h.read(len(anchor)) == anchor
        h.seek(here)
        return ok

    def _read_cursor_anchor(self, h, line_start: int, pos: int) -> bytes:
        if pos <= line_start:
            return b""
        start = max(line_start, pos - self._ANCHOR_LEN)
        here = h.tell()
        h.seek(start)
        anchor = h.read(pos - start)
        h.seek(here)
        return anchor

    def _cursor_anchor_ok(
        self, h, line_start: int, pos: int, cursor_anchor: bytes,
    ) -> bool:
        if not cursor_anchor:
            return pos <= line_start
        start = max(line_start, pos - len(cursor_anchor))
        here = h.tell()
        h.seek(start)
        ok = h.read(len(cursor_anchor)) == cursor_anchor
        h.seek(here)
        return ok

    def _make_scan_cursor(
        self, h, path: str, line_start: int, pos: int, head_anchor: bytes,
    ) -> _ScanCursor:
        st = os.stat(path)
        return _ScanCursor(
            pos=pos,
            anchor=head_anchor,
            cursor_anchor=self._read_cursor_anchor(h, line_start, pos),
            file_size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            ctime_ns=st.st_ctime_ns,
        )

    def _scan_put(self, key: tuple[str, int, int, int], entry: _ScanCursor) -> None:
        abspath, line_start, st_dev, st_ino = key
        stale = [k for k in self._scan_cursors
                 if k[0] == abspath and k[1] == line_start and k[2:] != (st_dev, st_ino)]
        for k in stale:
            self._scan_cursors.pop(k, None)
        if key in self._scan_cursors:
            self._scan_cursors.move_to_end(key)
        self._scan_cursors[key] = entry
        while len(self._scan_cursors) > self._SCAN_STATE_CAP:
            self._scan_cursors.popitem(last=False)

    def _scan_get(self, path: str, line_start: int) -> _ScanCursor | None:
        st = os.stat(path)
        key = self._scan_key(path, line_start, st.st_dev, st.st_ino)
        abspath = os.path.abspath(path)
        with self._scan_lock:
            stale = [k for k in self._scan_cursors
                     if k[0] == abspath and k[1] == line_start
                     and k[2:] != (st.st_dev, st.st_ino)]
            for k in stale:
                self._scan_cursors.pop(k, None)
            entry = self._scan_cursors.get(key)
            if entry is None:
                return None
            if st.st_size < entry.file_size or st.st_size < entry.pos:
                self._scan_cursors.pop(key, None)
                return None
            if (st.st_size == entry.file_size
                    and (st.st_mtime_ns != entry.mtime_ns
                         or st.st_ctime_ns != entry.ctime_ns)):
                self._scan_cursors.pop(key, None)
                return None
            self._scan_cursors.move_to_end(key)
            return entry

    def _scan_clear(self, path: str, line_start: int) -> None:
        st = os.stat(path)
        key = self._scan_key(path, line_start, st.st_dev, st.st_ino)
        with self._scan_lock:
            self._scan_cursors.pop(key, None)

    def _scan_bounded_line(self, h, line_start: int, visible_end: int) -> tuple[str, int]:
        """Within a reservation: scan the oversized line through its newline once."""
        scanned = 0
        while h.tell() < visible_end:
            pos = h.tell()
            limit = min(MAX_LINE, visible_end - pos)
            if limit <= 0:
                break
            chunk = h.readline(limit)
            if not chunk:
                h.seek(line_start)
                return "partial", scanned
            scanned += len(chunk)
            if chunk.endswith(b"\n"):
                return "oversized", scanned
        h.seek(line_start)
        return "partial", scanned

    def _scan_progressive(
        self,
        h,
        path: str,
        line_start: int,
        visible_end: int,
        anchor: bytes,
    ) -> tuple[str, int]:
        """Unreserved: scan at most SCAN_LIMIT bytes; watermark stays at line_start."""
        st = os.stat(path)
        key = self._scan_key(path, line_start, st.st_dev, st.st_ino)
        scanned = 0
        while scanned < SCAN_LIMIT:
            pos = h.tell()
            if pos >= visible_end:
                with self._scan_lock:
                    self._scan_put(
                        key, self._make_scan_cursor(h, path, line_start, pos, anchor))
                h.seek(line_start)
                return "partial", scanned
            limit = min(MAX_LINE, visible_end - pos)
            if limit <= 0:
                h.seek(line_start)
                return "partial", scanned
            chunk = h.readline(limit)
            if not chunk:
                with self._scan_lock:
                    self._scan_put(
                        key, self._make_scan_cursor(
                            h, path, line_start, h.tell(), anchor))
                h.seek(line_start)
                return "partial", scanned
            scanned += len(chunk)
            if chunk.endswith(b"\n"):
                with self._scan_lock:
                    self._scan_cursors.pop(key, None)
                return "oversized", scanned
        with self._scan_lock:
            self._scan_put(
                key, self._make_scan_cursor(h, path, line_start, h.tell(), anchor))
        h.seek(line_start)
        return "partial", scanned

    def _read_record(
        self,
        h,
        path: str,
        bound_end: int | None = None,
    ) -> tuple[bytes | None, str, int]:
        """Bounded read of one JSONL record. Never readline()s the rest of the file.

        Status: 'eof' | 'ok' | 'partial' | 'oversized'.
        'ok' payload includes the newline and is at most MAX_LINE bytes.
        The third value is bounded scan work this call when status is 'partial'.
        """
        line_start = h.tell()
        if bound_end is not None and line_start >= bound_end:
            return None, "eof", 0
        file_size = os.path.getsize(path)
        visible_end = bound_end if bound_end is not None else file_size

        if bound_end is None:
            entry = self._scan_get(path, line_start)
            if entry is not None:
                st = os.stat(path)
                head_ok = self._anchor_ok(h, line_start, entry.anchor)
                if head_ok and (
                        st.st_size == entry.file_size
                        or self._cursor_anchor_ok(
                            h, line_start, entry.pos, entry.cursor_anchor)):
                    h.seek(entry.pos)
                    status, scanned = self._scan_progressive(
                        h, path, line_start, visible_end, entry.anchor)
                    return None, status, scanned
                self._scan_clear(path, line_start)
                h.seek(line_start)

        first_limit = MAX_LINE + 1
        if bound_end is not None:
            first_limit = min(first_limit, max(0, bound_end - line_start))
            if first_limit <= 0:
                return None, "eof", 0
        chunk = h.readline(first_limit)
        if not chunk:
            return None, "eof", 0
        if chunk.endswith(b"\n"):
            if len(chunk) > MAX_LINE:
                return None, "oversized", 0
            return chunk, "ok", 0
        if len(chunk) <= MAX_LINE:
            return chunk, "partial", 0
        here = h.tell()
        h.seek(line_start)
        anchor = h.read(self._ANCHOR_LEN)
        h.seek(here)
        if bound_end is not None:
            status, scanned = self._scan_bounded_line(h, line_start, visible_end)
            return None, status, scanned
        status, scanned = self._scan_progressive(
            h, path, line_start, visible_end, anchor)
        return None, status, scanned

    @staticmethod
    def _parse(raw: bytes) -> tuple[str | None, Segment | None]:
        try:
            d = json.loads(raw)
        except ValueError:
            return "malformed", None
        if not isinstance(d, dict):
            return "malformed", None
        if "schema_version" not in d:
            return "missing", None
        ver = d.get("schema_version")
        # `True == 1` in Python; a JSON true must not pass as version 1.
        if type(ver) is not int or ver != 1:
            return "unknown_version", None
        if "class" not in d:
            return "missing", None
        cls = d.get("class")
        if not isinstance(cls, str):
            return "unknown_class", None
        if len(cls) > MAX_CLASS:
            return "oversized", None
        if cls not in CLASSES:
            return "unknown_class", None
        for name in ("event_id", "session_id", "turn_id"):
            if name not in d:
                return "missing", None
            val = d[name]
            if not isinstance(val, str):
                return "invalid", None
            if not val.strip():
                return "blank", None
            if len(val) > MAX_ID:
                return "oversized", None
        if "text" not in d:
            return "missing", None
        text = d.get("text")
        if not isinstance(text, str):
            return "invalid", None
        if not text.strip():
            return "blank", None
        cap = _cap(cls)
        if "timestamp" not in d:
            return "missing", None
        ts = d.get("timestamp")
        if not isinstance(ts, str):
            return "invalid", None
        if not ts.strip():
            return "blank", None
        if len(ts) > MAX_TIMESTAMP:
            return "oversized", None
        if not _rfc3339(ts):
            return "invalid", None
        return None, Segment(cls, text.strip()[:cap])

    def _drink(self, path: str, start: int, limit_chars: int,
               report: IntakeReport | None = None,
               bound_end: int | None = None) -> tuple[list[Segment], int, int]:
        segs: list[Segment] = []
        total = 0
        with open(path, "rb") as h:
            h.seek(start)
            while True:
                line_start = h.tell()
                if bound_end is not None and line_start >= bound_end:
                    return segs, line_start, 0
                raw, status, scan_pending = self._read_record(h, path, bound_end=bound_end)
                if status == "eof":
                    return segs, line_start, 0
                if status == "partial":
                    self._note(report, "partial", line_start, len(raw or b""))
                    return segs, line_start, scan_pending
                if status == "oversized":
                    self._note(report, "oversized", line_start, MAX_LINE + 1)
                    continue
                reason, seg = self._parse(raw or b"")
                if reason:
                    self._note(report, reason, line_start, len(raw or b""))
                    continue
                assert seg is not None
                segs.append(seg)
                total += len(seg.text)
                if total >= limit_chars:
                    return segs, h.tell(), 0
            return segs, h.tell(), 0

    def sip(self, path: str, start: int, limit_chars: int,
            report: IntakeReport | None = None,
            bound_end: int | None = None) -> tuple[list[Segment], int]:
        segs, end, _ = self._drink(path, start, limit_chars, report=report,
                                    bound_end=bound_end)
        return segs, end

    def claim_bound(self, path: str, start: int, budget_chars: int) -> tuple[int, int, int]:
        # Same walk sip uses, same budget Distiller will pass to sip.
        segs, end, scan_pending = self._drink(path, start, budget_chars)
        text = sum(len(s.text) for s in segs)
        walked = max(0, end - start)
        # Junk-only stretches still have to move: approx=0 would refuse the claim
        # and never report the skips. A short valid event after a huge discarded
        # prefix must count the walked span, not just kept characters.
        approx = max(text, walked)
        if end <= start and scan_pending > 0:
            return start, 0, scan_pending
        return end, approx, 0


# ── Plain text / markdown notes (append-only → byte watermark) ──────────────

class TextSource(Source):
    """A directory of notes or logs. Everything is [USER] — a human wrote it.
    Useful for distilling meeting notes, diaries, or exported chat logs."""
    name = "text"

    def matches(self, path: str) -> bool:
        return path.endswith((".md", ".txt", ".log"))

    def key(self, path: str) -> str:
        return "text:" + os.path.abspath(path)

    def discover(self, root: str) -> list[str]:
        out = []
        for ext in ("*.md", "*.txt", "*.log"):
            out += glob.glob(os.path.join(root, "**", ext), recursive=True)
        return sorted(out, key=os.path.getmtime, reverse=True)

    @staticmethod
    def _stop(path: str, start: int, limit_chars: int) -> int:
        """Where a sip of this budget ends — the one rule, so the reserve and the read
        can never be computed differently. (Computing them separately is what marked
        62% of a claude stretch drunk without reading it.)

        A fixed byte window — 4 per char, the UTF-8 worst case — pulled BACK off a
        half-written character: cutting mid-character lost that character twice over,
        `errors="ignore"` dropping its head here and its tail on the next sip, with
        nothing in the log to say a character had gone.
        """
        size = os.path.getsize(path)
        end = min(start + limit_chars * 4, size)
        if end >= size or end <= start:
            return max(start, min(end, size))
        n = min(4, end - start)             # a UTF-8 character is at most 4 bytes
        with open(path, "rb") as h:
            h.seek(end - n)
            tail = h.read(n)
        i = len(tail) - 1
        while i >= 0 and (tail[i] & 0xC0) == 0x80:      # 10xxxxxx: a continuation byte
            i -= 1
        if i < 0:
            return end                     # no lead byte in reach; leave the bytes be
        b = tail[i]
        need = 1 if b < 0x80 else 2 if b < 0xE0 else 3 if b < 0xF0 else 4
        have = len(tail) - i
        return end if have >= need else max(start + 1, end - have)

    def sip(self, path: str, start: int, limit_chars: int,
            report: IntakeReport | None = None,
            bound_end: int | None = None) -> tuple[list[Segment], int]:
        stop = self._stop(path, start, limit_chars)
        if bound_end is not None:
            stop = min(stop, bound_end)
        with open(path, "rb") as h:
            h.seek(start)
            raw = h.read(max(0, stop - start)).decode("utf-8", errors="ignore")
        segs = [Segment("USER", p.strip()[:MAX_SEG]) for p in raw.split("\n\n") if p.strip()]
        return segs, stop

    def claim_bound(self, path: str, start: int, budget_chars: int) -> tuple[int, int, int]:
        # Exact by construction: the same rule the read obeys. If the file GREW between
        # the two, the reserve is the older, shorter stop — short is the recoverable
        # direction (advance() carries the mark to wherever the read truly ended);
        # long is the one that loses journal.
        end = self._stop(path, start, budget_chars)
        # `approx` is raw bytes, not kept chars: see ClaudeCodeSource.claim_bound for
        # why this filter errs high.
        return end, max(0, end - start), 0


SOURCES: dict[str, Source] = {
    s.name: s for s in (ClaudeCodeSource(), DshSource(), EvidenceJsonlSource(), TextSource())
}


def source_for(path: str) -> Source | None:
    for s in (SOURCES["dsh"], SOURCES["evidence"], SOURCES["claude"], SOURCES["text"]):
        if s.matches(path):
            return s
    return None


def discover_all(roots: dict, exclude_roots: list[str] | None = None) -> list[str]:
    """Journals to drink from, newest first — today's decisions are worth the most.

    `roots` maps a source kind to either a path or a table::

        {"dsh": "~/dsh/sessions"}
        {"dsh": {"root": "~/dsh/sessions-maker",
                 "include_glob": ["**/session.jsonl.zstd"],
                 "exclude_glob": ["**/scratch/**"]}}

    Per-source globs exist because one sessions directory usually holds every mode's
    conversations. Pointing two stores at it distils all of them into both: the memory
    directories are separate but the INTAKE is shared, and contamination happens there.

    `exclude_roots` (the store directories) is defence in depth: a journal root that
    contains a store would re-ingest memories as if a human had written them, which
    launders model-written text into [USER] evidence. The registry refuses that overlap
    at load; this catches it again at discovery.
    """
    files: list[str] = []
    for kind, spec in (roots or {}).items():
        src = SOURCES.get(kind)
        if not src:
            continue
        if isinstance(spec, dict):
            root = os.path.expanduser(str(spec.get("root", "")))
            include = spec.get("include_glob") or []
            exclude = spec.get("exclude_glob") or []
        else:
            root, include, exclude = os.path.expanduser(str(spec)), [], []
        if not root or not os.path.isdir(root):
            continue
        found = src.discover(root)
        if include:
            keep: list[str] = []
            for pat in include:
                keep += glob.glob(os.path.join(root, pat), recursive=True)
            found = [f for f in found if f in set(keep)]
        for pat in exclude:
            dropped = set(glob.glob(os.path.join(root, pat), recursive=True))
            found = [f for f in found if f not in dropped]
        files += found
    for root in (exclude_roots or []):
        files = [f for f in files if not contained(root, f)]
    # Path exclusion is not enough: a HARDLINK to a memory, sitting in an otherwise clean
    # journal root, is a different path to the same inode. It walked through and was
    # sipped as [USER] evidence — model-written memory laundered into the human's words,
    # which is the one thing the evidence gate exists to prevent. Compare identities.
    ids = set()
    for root in (exclude_roots or []):
        for p in glob.glob(os.path.join(root, "**", "*.md"), recursive=True):
            try:
                st = os.stat(p)
                ids.add((st.st_dev, st.st_ino))
            except OSError:
                continue
    if ids:
        keep = []
        for f in files:
            try:
                st = os.stat(f)
            except OSError:
                continue
            if (st.st_dev, st.st_ino) not in ids:
                keep.append(f)
        files = keep
    return sorted(set(files), key=os.path.getmtime, reverse=True)
