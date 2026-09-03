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

Provenance is decided differently in each, and that is the point: Claude Code has no
label and must drop a whole part that merely CONTAINS an injection marker, DSH has
`source.kind`, and the Lna Journal carries `origin` — a structured claim about who
was speaking, which is the only one of the three that can be trusted positively.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
from dataclasses import dataclass

from ..store import contained

MAX_TOOL = 1500
MAX_SEG = 4000


def _cap(cls: str) -> int:
    """Per-class ceiling on one segment's text. Tools are verbose; the head is enough
    to ground a number. One rule, because the adapters wrote it out separately: the
    Claude path cut to MAX_TOOL and then again to MAX_SEG and only agreed with the DSH
    path because MAX_TOOL happens to be the smaller constant."""
    return MAX_TOOL if cls == "TOOL" else MAX_SEG

CLASSES = ("USER", "TOOL", "ACT", "SELF")


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

    def sip(self, path: str, start: int, limit_chars: int) -> tuple[list[Segment], int]:
        """Read past the watermark. Returns (segments, new watermark)."""
        raise NotImplementedError

    def claim_bound(self, path: str, start: int, budget_chars: int) -> tuple[int, int]:
        """Reserve a stretch before drinking it, so parallel runs never overlap.
        Returns (end watermark, approximate chars in the stretch)."""
        raise NotImplementedError


# ── Claude Code / plain JSONL transcripts (append-only → byte watermark) ─────

class ClaudeCodeSource(Source):
    """`~/.claude/projects/<project>/<session>.jsonl`, one JSON event per line."""
    name = "claude"

    def matches(self, path: str) -> bool:
        return path.endswith(".jsonl")

    def key(self, path: str) -> str:
        return "claude:" + os.path.basename(path)

    def discover(self, root: str) -> list[str]:
        return sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True),
                      key=os.path.getmtime, reverse=True)

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

    def _walk(self, path: str, start: int, limit_chars: int) -> tuple[list[Segment], int, int]:
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

    def sip(self, path: str, start: int, limit_chars: int) -> tuple[list[Segment], int]:
        segs, end, _ = self._walk(path, start, limit_chars)
        return segs, end

    def claim_bound(self, path: str, start: int, budget_chars: int) -> tuple[int, int]:
        size = os.path.getsize(path)
        # A kept character never costs less than one byte of the line it came from
        # (UTF-8 never shrinks, and the JSON scaffolding around the text is pure
        # surplus), so a stretch shorter than the budget can never fill it: the walk
        # would run to EOF. That makes this shortcut exact rather than estimated —
        # which matters because catch_up() asks for the whole file with a budget of
        # 2**40 and must not JSON-parse a year of journals to be told where it ends.
        if size - start <= budget_chars:
            return size, max(0, size - start)
        # Otherwise walk the lines a second time: reserving costs a re-read of a few
        # tens of KB, guessing costs journal. `approx` is the raw stretch, not the
        # kept chars — it only feeds the "worth waking the model" filter, and erring
        # HIGH there at worst spends a pass that finds nothing and moves on, while
        # erring low would park the mark forever on a journal of nothing but
        # sidechain and system-reminder lines.
        _, end, _ = self._walk(path, start, budget_chars)
        return end, max(0, end - start)


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

    def _walk(self, path: str, start: int, limit_chars: int) -> tuple[list[Segment], int, int]:
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

    def sip(self, path: str, start: int, limit_chars: int) -> tuple[list[Segment], int]:
        segs, last, _ = self._walk(path, start, limit_chars)
        return segs, last

    def claim_bound(self, path: str, start: int, budget_chars: int) -> tuple[int, int]:
        # Exact in both numbers: the same walk, so the reserve lands on the same event
        # the read stops at. The archive is decompressed twice (once to reserve, once
        # to drink) — a few hundred ms against a stretch of journal lost in silence.
        _, end, total = self._walk(path, start, budget_chars)
        return end, total


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

    def sip(self, path: str, start: int, limit_chars: int) -> tuple[list[Segment], int]:
        stop = self._stop(path, start, limit_chars)
        with open(path, "rb") as h:
            h.seek(start)
            raw = h.read(max(0, stop - start)).decode("utf-8", errors="ignore")
        segs = [Segment("USER", p.strip()[:MAX_SEG]) for p in raw.split("\n\n") if p.strip()]
        return segs, stop

    def claim_bound(self, path: str, start: int, budget_chars: int) -> tuple[int, int]:
        # Exact by construction: the same rule the read obeys. If the file GREW between
        # the two, the reserve is the older, shorter stop — short is the recoverable
        # direction (advance() carries the mark to wherever the read truly ended);
        # long is the one that loses journal.
        end = self._stop(path, start, budget_chars)
        # `approx` is raw bytes, not kept chars: see ClaudeCodeSource.claim_bound for
        # why this filter errs high.
        return end, max(0, end - start)


# ── Lna Harness journal v1 (append-only JSONL → byte watermark) ─────────────

class LnaJournalSource(Source):
    """Lna Harness's own journal (`**/*.lna.jsonl`), one JSON *run* per line.

    The record is the J1 projection of one run — caller user message, assistant
    replies, tool calls and results — with provenance limited to
    `origin.{rootKind,currentKind,delegated}`. No PrincipalId, no displayName,
    no reasoning, no deltas: Kura only ever sees what the Journal chose to keep.

    `outcome` is deliberately never read. A run that failed still contains what the
    human actually said, and their sentence is the primary evidence in it: dropping
    the run would mean the machine having a bad day erased the person's words. The
    error prose the assistant wrote about it stays [SELF], and a provider error is
    not promoted to [TOOL] — a failure is not a measurement.

    Evidence classes, fail-closed on provenance:

        user message, root human, current human, delegated stated false → [USER]
        any other user message (agent/service/unknown/delegated/unsaid) → [SELF]
        assistant message                                              → [SELF]
        tool-call                                                      → [ACT]
        tool-result                                                    → [TOOL]

    Watermark is a byte offset over an append-only file. A partial final line
    (no trailing newline) is never treated as read: the walk stops at its start,
    so both sip() and claim_bound() leave the mark exactly at the last complete
    line. A completed line that is not valid JSON — or a record with an
    unsupported v / type — fails loud rather than silently skipping: mistaking a
    broken journal for drunk is how evidence goes missing forever.
    """
    name = "lna"

    def matches(self, path: str) -> bool:
        return path.endswith(".lna.jsonl")

    def key(self, path: str) -> str:
        return "lna:" + os.path.abspath(path)

    def discover(self, root: str) -> list[str]:
        return sorted(glob.glob(os.path.join(root, "**", "*.lna.jsonl"), recursive=True),
                      key=os.path.getmtime, reverse=True)

    @staticmethod
    def _class_for_user(origin: dict) -> str:
        """Fail-closed: only a direct human utterance is [USER]. Everything else —
        agent, service, unknown, or any delegation — falls to [SELF], never USER.

        `is False`, not `not delegated`. The kind tests demand the exact string
        "human", so a missing or null field there already falls to SELF; a truthiness
        test on delegation would have accepted the field's own ABSENCE as proof that
        no delegation happened. A backfilled projection, which cannot always recover
        delegation for an old run, writes `"delegated": null` — and that record's
        agent-composed "approve the retirement of X" would have entered the gate as
        the human's own words. Provenance is only ever believed when it is stated.
        """
        if (origin.get("rootKind") == "human"
                and origin.get("currentKind") == "human"
                and origin.get("delegated") is False):
            return "USER"
        return "SELF"

    @staticmethod
    def _json_text(value) -> str:
        """Stable text for a JSON value (tool arguments / results are not always str)."""
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False) if value is not None else ""

    def _classify(self, d: dict, path: str, line_start: int) -> list[Segment]:
        """One journal run (one line) → its segments. Invalid records fail loud."""
        if not isinstance(d, dict):
            raise RuntimeError(f"lna journal {path}: non-object line at byte {line_start}")
        if d.get("v") != 1 or d.get("type") != "run":
            raise RuntimeError(
                f"lna journal {path}: unsupported record v={d.get('v')!r} "
                f"type={d.get('type')!r} at byte {line_start}")
        origin = d.get("origin") or {}
        if not isinstance(origin, dict):
            raise RuntimeError(f"lna journal {path}: origin is not an object at byte {line_start}")
        user_cls = self._class_for_user(origin)
        segs: list[Segment] = []
        entries = d.get("entries") or []
        if not isinstance(entries, list):
            raise RuntimeError(f"lna journal {path}: entries is not a list at byte {line_start}")
        for e in entries:
            if not isinstance(e, dict):
                continue                      # schema says every entry is an object
            t = e.get("type")
            if t == "message":
                role = e.get("role")
                if role == "user":
                    cls = user_cls
                elif role == "assistant":
                    cls = "SELF"
                else:
                    continue                  # toolResult etc: not in Journal v1 messages
                txt = self._json_text(e.get("text"))
            elif t == "tool-call":
                cls = "ACT"
                name = self._json_text(e.get("name"))
                args = self._json_text(e.get("arguments"))
                txt = f"{name} {args}".strip()
            elif t == "tool-result":
                cls = "TOOL"
                txt = self._json_text(e.get("content"))
            else:
                continue                      # unknown entry type is not a Journal v1 entry
            txt = txt.strip()
            if not txt:
                continue                      # empty text yields no segment
            txt = txt[:_cap(cls)]
            segs.append(Segment(cls, txt))
        return segs

    def _walk(self, path: str, start: int, limit_chars: int,
              collect: bool = True) -> tuple[list[Segment], int, int]:
        """The one line-walk. sip() and claim_bound() both come through here so the
        reserved end is exactly where the read stops (the lesson of the earlier
        sources). A partial final line stops the walk at its own start — the mark
        never advances past an incomplete run.

        A run is one line; a run is never split across a budget. We fully classify
        a line, then check the budget.

        `collect=False` keeps the segments out of memory for a caller that only wants
        the stop offset. Every line is still parsed and classified, so the tally, the
        stopping rule and the loud failures are identical — the only difference is
        what is kept. It matters because `catch_up()` bounds the whole file with a
        budget of 2**40: accumulating a year of segments to be told where the last
        line ends is the cost the claude adapter dodges with its size shortcut, which
        this walk cannot have (see `claim_bound`).

        Returns (segments, stop offset, kept chars).
        """
        segs: list[Segment] = []
        total = 0
        with open(path, "rb") as h:
            h.seek(start)
            while True:
                line_start = h.tell()
                line = h.readline()
                if not line:
                    return segs, line_start, total      # EOF; mark at EOF
                if not line.endswith(b"\n"):
                    # Partial final line — the writer may be mid-append. Do NOT
                    # treat it as read. The mark stays at the last complete line.
                    return segs, line_start, total
                try:
                    d = json.loads(line)
                except ValueError:
                    raise RuntimeError(
                        f"lna journal {path}: invalid JSON in a completed line at byte {line_start}")
                run_segs = self._classify(d, path, line_start)
                if collect:
                    segs.extend(run_segs)
                total += sum(len(s.text) for s in run_segs)
                if total >= limit_chars:
                    return segs, h.tell(), total

    def sip(self, path: str, start: int, limit_chars: int) -> tuple[list[Segment], int]:
        segs, end, _ = self._walk(path, start, limit_chars)
        return segs, end

    def claim_bound(self, path: str, start: int, budget_chars: int) -> tuple[int, int]:
        """Always the same walk the read takes — no size shortcut.

        The claude adapter may reserve `size` for a stretch shorter than the budget
        without parsing, because its walk SKIPS a bad line and therefore always
        reaches EOF. This walk RAISES on a completed line that is not valid JSON, and
        `Watermarks.claim()` writes the reserve BEFORE `sip()` ever runs, so the
        shortcut would strand the mark past the corruption: measured on a
        three-line journal (good / broken / good), claim reserved all 379 bytes,
        sip raised, and the next pass found the file fully drunk — the loud failure
        having eaten the unread human evidence behind it. Parsing twice is the price
        of a mark that can only move over lines somebody actually read.
        """
        _, end, _ = self._walk(path, start, budget_chars, collect=False)
        return end, max(0, end - start)


SOURCES: dict[str, Source] = {s.name: s for s in (ClaudeCodeSource(), DshSource(), TextSource(), LnaJournalSource())}


def source_for(path: str) -> Source | None:
    # `lna` must be checked before `claude`: *.lna.jsonl also ends in .jsonl.
    for s in (SOURCES["dsh"], SOURCES["lna"], SOURCES["claude"], SOURCES["text"]):
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
