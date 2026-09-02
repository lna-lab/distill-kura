"""The reserve and the read must be the same stretch — for every source.

`Watermarks.claim()` writes the CLAIMED end into the mark before the read happens,
and `advance()` merges with max(). So a bound that reserves MORE than the read
consumes is not a rounding error: the bytes between the true stop and the claimed
end are marked drunk and never read again, silently, forever.

It had already happened once (DSH, two thirds of a journal). It came back in the
claude adapter in a different guise: the bound reserved `budget * 4` bytes — the
UTF-8 worst case — while the read stops when the KEPT characters reach the budget.
On a 4000-line ASCII journal that is 80 KB reserved against a true stop of 30 KB,
and 62% of every stretch went down unread. English and code journals are the common
case, so the loss was happening in production.

Every test here fails on the code as it stood before that fix.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from distill_kura.distill.sources import (                          # noqa: E402
    ClaudeCodeSource, DshSource, Segment, TextSource,
)
from distill_kura.distill.watermark import Watermarks               # noqa: E402


# ── corpora ─────────────────────────────────────────────────────────────────

def _write_jsonl(path, events) -> str:
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(e if isinstance(e, str) else json.dumps(e, ensure_ascii=False))
            f.write("\n")
    return str(path)


def _user(text, side=False):
    d = {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}
    if side:
        d["isSidechain"] = True
    return d


def _assistant(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _tool_use(name, inp):
    return {"type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


def _tool_result(text):
    return {"type": "user",
            "message": {"content": [{"type": "tool_result", "content": text}]}}


def ascii_journal(tmp_path, lines: int = 4000) -> str:
    """The shape that was losing journal: dense ASCII, one byte per character."""
    ev = []
    for i in range(lines):
        ev.append(_user(f"line {i}: the garage door sensor reads {i} millimetres today"))
        if i % 3 == 0:
            ev.append(_assistant(f"noted, {i}"))
        if i % 5 == 0:
            ev.append(_tool_use("Bash", {"command": f"echo {i}"}))
        if i % 7 == 0:
            ev.append(_tool_result(f"exit 0, {i} rows"))
    return _write_jsonl(tmp_path / "ascii.jsonl", ev)


def cjk_journal(tmp_path, lines: int = 600) -> str:
    """Three bytes per character: the case the 4× rule was built for."""
    ev = []
    for i in range(lines):
        ev.append(_user(f"{i}番目の記録です。ガレージの扉のセンサーは今日も静かでした。"))
        ev.append(_assistant(f"承知しました。{i}件目として控えます。"))
    return _write_jsonl(tmp_path / "cjk.jsonl", ev)


def skipped_journal(tmp_path, lines: int = 800) -> str:
    """Mostly dross: system reminders, sidechain, non-dict lines, broken JSON."""
    ev = []
    for i in range(lines):
        ev.append(_user(f"<system-reminder>context {i}</system-reminder>"))
        ev.append(_user(f"a subagent brief number {i}", side=True))
        ev.append('"a stray string, not an object"')
        ev.append("{not json at all")
        ev.append({"type": "system", "message": {"content": "housekeeping"}})
        if i % 40 == 0:
            ev.append(_user(f"a real human line, the {i}th"))
    return _write_jsonl(tmp_path / "skipped.jsonl", ev)


def tiny_journal(tmp_path) -> str:
    """Smaller than any budget — the whole file is one stretch."""
    return _write_jsonl(tmp_path / "tiny.jsonl", [_user("just the one line"), _assistant("ok")])


CLAUDE_CORPUS = {"ascii": ascii_journal, "cjk": cjk_journal,
                 "skipped": skipped_journal, "tiny": tiny_journal}


def dsh_events(kind: str) -> list[dict]:
    def um(txt):
        return {"type": "user/message",
                "data": {"source": {"kind": "user"}, "content": [{"type": "text", "text": txt}]}}

    def am(txt):
        return {"type": "assistant/chunk",
                "data": {"chunk": {"type": "block-end", "block": {"type": "text", "text": txt}}}}

    out, seq = [], 0
    n = {"ascii": 400, "cjk": 300, "skipped": 400, "tiny": 2}[kind]
    for i in range(n):
        seq += 1
        if kind == "ascii":
            out.append({**um(f"note {i} about the garage door sensor"), "seq": seq})
        elif kind == "cjk":
            out.append({**um(f"{i}番目の記録です。ガレージの扉は静かでした。"), "seq": seq})
        elif kind == "tiny":
            out.append({**am(f"short {i}"), "seq": seq})
        else:
            if i % 20 == 0:
                out.append({**um(f"a real human line, the {i}th"), "seq": seq})
            else:
                out.append({"seq": seq, "type": "system/ping"})        # unclassified
                seq += 1
                out.append({"seq": seq, "type": "user/message",
                            "data": {"source": {"kind": "system"},
                                     "content": [{"type": "text", "text": "injected"}]}})
    return out


@pytest.fixture
def dsh(monkeypatch, tmp_path):
    """A DSH source whose archive is a list of events; zstd stays out of the test."""
    def make(kind: str):
        events = dsh_events(kind)
        monkeypatch.setattr(DshSource, "_lines", staticmethod(lambda path: iter(events)))
        return DshSource(), str(tmp_path / kind / "session.jsonl.zstd"), events
    return make


def text_file(tmp_path, kind: str) -> str:
    if kind == "ascii":
        body = "\n\n".join(f"paragraph {i}: the sensor was quiet again today." for i in range(2000))
    elif kind == "cjk":
        body = "\n\n".join(f"{i}段落目。ガレージの扉のセンサーは今日も静かでした。" for i in range(800))
    else:
        body = "one short note.\n\nand another."
    p = tmp_path / f"{kind}.md"
    p.write_text(body, encoding="utf-8")
    return str(p)


# ── 1. the reserve is the true stop, for every source ───────────────────────

@pytest.mark.parametrize("kind", list(CLAUDE_CORPUS))
@pytest.mark.parametrize("budget", [500, 4000, 20_000, 10_000_000])
def test_claude_claim_bound_is_where_the_read_stops(tmp_path, kind, budget):
    src, path = ClaudeCodeSource(), CLAUDE_CORPUS[kind](tmp_path)
    for start in (0, 1_000 if os.path.getsize(path) > 1_000 else 0):
        end, _, _ = src.claim_bound(path, start, budget)
        _, stop = src.sip(path, start, budget)
        assert end == stop, f"{kind}: reserved {end}, read stopped at {stop}"


def test_claude_reserve_no_longer_multiplies_the_budget_by_four(tmp_path):
    """The measurement that opened the bug: 4000 ASCII lines, a 20 000-char budget.
    The old rule reserved 80 000 B; the read stopped at ~30 000 B. Everything in
    between was marked drunk without being read."""
    src, path = ClaudeCodeSource(), ascii_journal(tmp_path)
    end, _, _ = src.claim_bound(path, 0, 20_000)
    _, stop = src.sip(path, 0, 20_000)
    assert end == stop
    assert end < 20_000 * 4                      # the old arithmetic, still failing here


@pytest.mark.parametrize("kind", ["ascii", "cjk", "skipped", "tiny"])
@pytest.mark.parametrize("budget", [200, 5_000, 1 << 40])
def test_dsh_claim_bound_is_where_the_read_stops(dsh, kind, budget):
    src, path, _ = dsh(kind)
    for start in (0, 5):
        end, _, _ = src.claim_bound(path, start, budget)
        _, stop = src.sip(path, start, budget)
        assert end == stop


@pytest.mark.parametrize("kind", ["ascii", "cjk", "tiny"])
@pytest.mark.parametrize("budget", [64, 1_000, 5_000, 1 << 20])
def test_text_claim_bound_is_where_the_read_stops(tmp_path, kind, budget):
    src, path = TextSource(), text_file(tmp_path, kind)
    for start in (0, 7):
        end, _, _ = src.claim_bound(path, start, budget)
        _, stop = src.sip(path, start, budget)
        assert end == stop


def test_text_sip_never_splits_a_character_in_half(tmp_path):
    """A window cut mid-character lost that character twice over: `errors="ignore"`
    dropped its head bytes here and its tail bytes on the next sip."""
    src, path = TextSource(), text_file(tmp_path, "cjk")
    size = os.path.getsize(path)
    seen = []
    start = 0
    while start < size:
        segs, stop = src.sip(path, start, 25)          # 100 B: lands mid-character often
        assert stop > start
        seen.append("".join(s.text for s in segs))
        start = stop
    whole = open(path, encoding="utf-8").read()
    assert "".join(seen).replace("\n", "") == whole.replace("\n", "").replace("\n\n", "")


# ── 2. drain the whole file: every kept segment, exactly once ───────────────

def _drain(src, path, marks_path, budget, rounds=4000):
    """Claim + sip until the file is exhausted, the way the pipeline does it."""
    wm = Watermarks(marks_path)
    got, stretches = [], []
    for _ in range(rounds):
        c = wm.claim([path], budget, 1)
        if not c:
            return got, stretches
        if c.scan_pending:
            continue
        _, start, bound_end, s, _ = c
        segs, stop = s.sip(path, start, budget, bound_end=bound_end)
        wm.advance(s.key(path), stop)
        got += segs
        stretches.append((start, stop))
    raise AssertionError("the drain never finished")


@pytest.mark.parametrize("kind", list(CLAUDE_CORPUS))
def test_claude_drain_sees_every_segment_exactly_once(tmp_path, kind):
    """The regression that would have caught the bug. Draining a journal chunk by
    chunk must yield exactly what one unlimited read yields — no segment skipped
    because the mark had already been moved past it."""
    src, path = ClaudeCodeSource(), CLAUDE_CORPUS[kind](tmp_path)
    whole, _ = src.sip(path, 0, 1 << 40)
    drunk, stretches = _drain(src, path, str(tmp_path / "m1" / "marks.json"), 3_000)
    assert [(s.cls, s.text) for s in drunk] == [(s.cls, s.text) for s in whole]
    # and the stretches tile the file: contiguous, no gap, no overlap
    assert stretches[0][0] == 0
    assert all(b[0] == a[1] for a, b in zip(stretches, stretches[1:]))
    assert stretches[-1][1] == os.path.getsize(path)


@pytest.mark.parametrize("kind", ["ascii", "cjk", "skipped", "tiny"])
def test_dsh_drain_sees_every_segment_exactly_once(dsh, tmp_path, kind):
    src, path, _ = dsh(kind)
    whole, _ = src.sip(path, 0, 1 << 40)
    drunk, _ = _drain(src, path, str(tmp_path / f"m2-{kind}" / "marks.json"), 300)
    assert [(s.cls, s.text) for s in drunk] == [(s.cls, s.text) for s in whole]


def test_text_drain_loses_no_character(tmp_path):
    src, path = TextSource(), text_file(tmp_path, "cjk")
    drunk, stretches = _drain(src, path, str(tmp_path / "m3" / "marks.json"), 40)
    whole = open(path, encoding="utf-8").read()
    assert "".join(s.text for s in drunk).replace("\n", "") == whole.replace("\n", "")
    assert stretches[0][0] == 0 and stretches[-1][1] == os.path.getsize(path)
    assert all(b[0] == a[1] for a, b in zip(stretches, stretches[1:]))


# ── 3. two runners, never the same water ───────────────────────────────────

@pytest.mark.parametrize("kind", ["ascii", "cjk"])
def test_two_interleaved_claimers_never_overlap(tmp_path, kind):
    """Reserve-before-read is what keeps two distillers apart. Interleave them and no
    byte may be handed out twice — nor may any be handed out to nobody."""
    src, path = ClaudeCodeSource(), CLAUDE_CORPUS[kind](tmp_path)
    marks = str(tmp_path / "m4" / "marks.json")
    a, b = Watermarks(marks), Watermarks(marks)
    taken, whose = [], []
    for i in range(400):
        wm = a if i % 2 == 0 else b
        c = wm.claim([path], 2_000, 1)
        if not c:
            break
        _, start, _, s, _ = c
        segs, stop = s.sip(path, start, 2_000)
        wm.advance(s.key(path), stop)
        taken.append((start, stop))
        whose.append(wm)
    assert len(taken) > 4 and len(set(whose)) == 2          # both really got stretches
    assert taken[0][0] == 0
    assert all(b_[0] == a_[1] for a_, b_ in zip(taken, taken[1:]))   # disjoint, no gap
    assert taken[-1][1] == os.path.getsize(path)


def test_two_interleaved_dsh_claimers_never_overlap(dsh, tmp_path):
    src, path, events = dsh("ascii")
    marks = str(tmp_path / "m5" / "marks.json")
    a, b = Watermarks(marks), Watermarks(marks)
    seen, taken = [], []
    for i in range(400):
        wm = a if i % 2 == 0 else b
        c = wm.claim([path], 400, 1)
        if not c:
            break
        _, start, _, s, _ = c
        segs, stop = s.sip(path, start, 400)
        wm.advance(s.key(path), stop)
        taken.append((start, stop))
        seen += [sg.text for sg in segs]
    assert len(taken) > 4
    assert all(b_[0] == a_[1] for a_, b_ in zip(taken, taken[1:]))
    assert len(seen) == len(set(seen)) == len(events)       # every event, once


# ── 4. the mark's own contract ─────────────────────────────────────────────

def test_the_mark_only_ever_moves_forward(tmp_path):
    wm = Watermarks(str(tmp_path / "m6" / "marks.json"))
    wm.advance("claude:x.jsonl", 500)
    wm.advance("claude:x.jsonl", 200)                       # a stale runner reporting in
    assert wm.read()["claude:x.jsonl"] == 500


def test_claim_reserves_before_the_read(tmp_path):
    """The reserve must land in the file the instant it is handed out, or a second
    runner starting in the same breath gets the same water."""
    src, path = ClaudeCodeSource(), ascii_journal(tmp_path, lines=500)
    wm = Watermarks(str(tmp_path / "m7" / "marks.json"))
    c = wm.claim([path], 4_000, 1)
    start, s = c.start, c.source
    assert wm.read()[s.key(path)] > start                   # written before any sip()
    end, _, _ = s.claim_bound(path, start, 4_000)
    assert wm.read()[s.key(path)] == end
