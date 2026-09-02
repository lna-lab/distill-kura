"""Lna Journal Source: fail-closed evidence classes and a byte watermark that
never drinks a partial line.

The J1 Journal is `**/*.lna.jsonl`, one JSON run per line:

    {"v":1,"type":"run","runId":...,"sessionId":...,"nodeId":...,"runtimeId":...,
     "startedAt":...,"completedAt":...,"outcome":"completed",
     "origin":{"rootKind":"human","currentKind":"human","delegated":false},
     "entries":[{"type":"message","role":"user","text":"..."} ...]}

The rules under test:

* only direct human user text — root human, current human, no delegation — is [USER]
* agent / service / unknown / delegated user text is [SELF], never [USER]
* assistant text is [SELF]
* tool-call is [ACT], tool-result is [TOOL]
* a partial final line is never read, and its bytes never enter the watermark
* claim_bound() and sip() stop on the SAME byte — no journal marked drunk unread
"""
from __future__ import annotations

import itertools
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from distill_kura.distill.sources import (                          # noqa: E402
    MAX_SEG, MAX_TOOL, LnaJournalSource, Segment, source_for,
)
from distill_kura.distill.watermark import Watermarks               # noqa: E402


# ── corpora ─────────────────────────────────────────────────────────────────

def _run(origin, entries, run_id="run-1", outcome="completed"):
    return {
        "v": 1, "type": "run", "runId": run_id,
        "sessionId": "sess-1", "nodeId": "asama", "runtimeId": "pi",
        "startedAt": "2026-09-02T09:00:00.000Z",
        "completedAt": "2026-09-02T09:00:05.000Z",
        "outcome": outcome, "origin": origin, "entries": entries,
    }


def _human_origin():
    return {"rootKind": "human", "currentKind": "human", "delegated": False}


def _agent_origin():
    return {"rootKind": "human", "currentKind": "agent", "delegated": True}


def _msg(role, text, message_id="m1"):
    return {"type": "message", "messageId": message_id, "role": role, "text": text}


def _tool_call(name="read_file", arguments=None, call_id="c1", message_id="m2"):
    return {"type": "tool-call", "messageId": message_id, "callId": call_id,
            "name": name, "arguments": arguments}


def _tool_result(content, call_id="c1", message_id="m2", ok=True):
    return {"type": "tool-result", "messageId": message_id, "callId": call_id,
            "ok": ok, "content": content}


def write_journal(path, records) -> str:
    """Write one run per line, exactly as JsonlJournalWriter does (UTF-8)."""
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False))
            f.write("\n")
    return str(path)


def lna_journal(tmp_path, name="session.lna.jsonl") -> str:
    return str(tmp_path / name)


# ── discover & dispatch ─────────────────────────────────────────────────────

def test_discovers_only_lna_jsonl(tmp_path):
    """Only `*.lna.jsonl`, and RECURSIVELY: the J1-C layout puts a journal under a
    per-day or per-session directory, so a pattern without `**` would find nothing
    there while still passing a flat fixture."""
    p = tmp_path / "j" / "v1"
    (p / "2026-09-02").mkdir(parents=True)
    write_journal(p / "a.lna.jsonl", [_run(_human_origin(), [_msg("user", "hi")])])
    write_journal(p / "2026-09-02" / "deep.lna.jsonl",
                  [_run(_human_origin(), [_msg("user", "nested")])])
    write_journal(p / "b.jsonl", [{"type": "user", "message": {"content": "x"}}])  # claude
    (p / "c.txt").write_text("plain note\n", encoding="utf-8")                     # text
    (p / "d.lna.jsonl.tmp").write_text("half-written\n", encoding="utf-8")         # not ours
    src = LnaJournalSource()
    found = sorted(os.path.basename(x) for x in src.discover(str(p)))
    assert found == ["a.lna.jsonl", "deep.lna.jsonl"]


def test_source_for_prefers_lna_over_claude(tmp_path):
    # *.lna.jsonl also ends in .jsonl — lna must win.
    p = lna_journal(tmp_path)
    write_journal(p, [_run(_human_origin(), [_msg("user", "hi")])])
    assert source_for(p) is not None
    assert source_for(p).name == "lna"          # type: ignore[union-attr]


# ── evidence mapping, fail-closed ───────────────────────────────────────────

def _sip_one(record, tmp_path=None):
    """One run, read the way the distiller reads it: written to a real journal and
    drunk through `sip()`.

    Deliberately NOT `_classify()` directly. While these tests called the private
    classifier, nothing exercised tool-call or tool-result through `_walk` at all —
    a read path that dropped every ACT and TOOL segment on the floor still passed
    the whole file.
    """
    d = tmp_path or tempfile.mkdtemp()
    path = os.path.join(str(d), "one.lna.jsonl")
    write_journal(path, [record])
    segs, stop = LnaJournalSource().sip(path, 0, 1 << 30)
    assert stop == os.path.getsize(path)        # a whole run, or nothing
    return segs


def test_direct_human_user_is_user(tmp_path):
    segs = _sip_one(_run(_human_origin(), [_msg("user", "the garage door sensor is quiet")]),
                    tmp_path)
    assert [(s.cls, s.text) for s in segs] == [
        ("USER", "the garage door sensor is quiet")]


def test_delegated_human_to_agent_user_is_self(tmp_path):
    segs = _sip_one(_run(_agent_origin(), [_msg("user", "agent-origin utterance")]), tmp_path)
    assert [(s.cls, s.text) for s in segs] == [("SELF", "agent-origin utterance")]


KINDS = ("human", "agent", "service", "unknown")


@pytest.mark.parametrize("root,current,delegated",
                         list(itertools.product(KINDS, KINDS, (False, True))))
def test_only_the_direct_human_row_is_user(root, current, delegated, tmp_path):
    """The whole provenance truth table. Exactly ONE row is [USER] — root human,
    current human, no delegation — and every other row is [SELF].

    A table rather than named cases because the named ones all moved two fields at
    once: every non-USER fixture had rootKind == currentKind, so deleting EITHER kind
    clause from the three-way AND left the suite green. An agent-rooted chain whose
    current speaker is labelled human is the row that laundered agent text into the
    human's own words, and nothing failed.
    """
    origin = {"rootKind": root, "currentKind": current, "delegated": delegated}
    want = "USER" if (root, current, delegated) == ("human", "human", False) else "SELF"
    segs = _sip_one(_run(origin, [_msg("user", "who said this")]), tmp_path)
    assert segs == [Segment(want, "who said this")]


ABSENT = object()


@pytest.mark.parametrize("delegated", [ABSENT, None, "false", 0, "", "no"])
def test_delegation_must_be_stated_false(delegated, tmp_path):
    """Absence is not a denial. The kind fields demand the exact string "human", so a
    truthiness test on delegation would have accepted the field's own absence — and a
    backfilled projection, which cannot always recover delegation for an old run,
    writes null there. Then that record's agent-composed "approve the retirement of X"
    would reach the gate as the human's own sentence."""
    origin = {"rootKind": "human", "currentKind": "human"}
    if delegated is not ABSENT:
        origin["delegated"] = delegated
    segs = _sip_one(_run(origin, [_msg("user", "approve the retirement of X")]), tmp_path)
    assert segs == [Segment("SELF", "approve the retirement of X")]


def test_assistant_is_self(tmp_path):
    segs = _sip_one(_run(_human_origin(), [_msg("assistant", "let me check")]), tmp_path)
    assert segs == [Segment("SELF", "let me check")]


def test_tool_call_is_act(tmp_path):
    """Exact text, not a substring: `arguments` is a JsonValue, and a repr would read
    the same to a human while being unstable across runs and unparseable later."""
    segs = _sip_one(_run(_human_origin(), [
        _tool_call("read_file", {"path": "/tmp/a.txt"})]), tmp_path)
    assert segs == [Segment("ACT", 'read_file {"path": "/tmp/a.txt"}')]


def test_tool_call_arguments_may_be_absent(tmp_path):
    segs = _sip_one(_run(_human_origin(), [_tool_call("list_dir", None)]), tmp_path)
    assert segs == [Segment("ACT", "list_dir")]


def test_tool_result_is_tool(tmp_path):
    segs = _sip_one(_run(_human_origin(), [
        _tool_result({"lines": 42, "text": "measured"})]), tmp_path)
    assert len(segs) == 1 and segs[0].cls == "TOOL"
    assert "measured" in segs[0].text and "42" in segs[0].text


def test_non_string_tool_content_json_dumps_stably(tmp_path):
    segs = _sip_one(_run(_human_origin(), [_tool_result({"value": 42})]), tmp_path)
    assert segs == [Segment("TOOL", '{"value": 42}')]


def test_japanese_and_emoji_survive_untouched(tmp_path):
    text = "こんにちは、Lna Harness です 🫶✨"
    segs = _sip_one(_run(_human_origin(), [_msg("user", text)]), tmp_path)
    assert segs == [Segment("USER", text)]


def test_empty_message_dropped(tmp_path):
    segs = _sip_one(_run(_human_origin(), [_msg("user", "   ")]), tmp_path)
    assert segs == []


def test_unrecognised_entry_type_yields_nothing(tmp_path):
    """An inner monologue is not evidence. The Journal is not supposed to carry one,
    and if a later version does, it must arrive as a new entry type that this adapter
    ignores — never fall through to [SELF]."""
    segs = _sip_one(_run(_human_origin(), [
        {"type": "reasoning", "text": "maybe the sensor is broken"},
        {"type": "message-delta", "text": "partial"},
        _msg("assistant", "the sensor is quiet"),
    ]), tmp_path)
    assert segs == [Segment("SELF", "the sensor is quiet")]


def test_mixed_run_keeps_order(tmp_path):
    segs = _sip_one(_run(_human_origin(), [
        _msg("user", "read the file"),
        _tool_call("read_file", {"path": "/x"}),
        _tool_result("42 rows"),
        _msg("assistant", "done"),
    ]), tmp_path)
    assert [(s.cls, s.text.split()[0]) for s in segs] == [
        ("USER", "read"), ("ACT", "read_file"), ("TOOL", "42"), ("SELF", "done")]


def test_failed_run_still_reads_user_evidence(tmp_path):
    """A run the machine could not finish still holds what the person said. The
    assistant's error prose is [SELF]; the provider's failure is not a measurement and
    never becomes [TOOL]."""
    segs = _sip_one(_run(_human_origin(),
                         [_msg("user", "please measure it"),
                          _msg("assistant", "the provider errored out")],
                         outcome="failed"), tmp_path)
    assert [(s.cls, s.text) for s in segs] == [
        ("USER", "please measure it"), ("SELF", "the provider errored out")]


def test_delegation_alone_demotes_a_human_pair(tmp_path):
    """rootKind and currentKind both human, but the turn was DELEGATED. The text was
    composed by a delegating agent, so it is not the human's own sentence."""
    origin = {"rootKind": "human", "currentKind": "human", "delegated": True}
    segs = _sip_one(_run(origin, [_msg("user", "do what I would do")]), tmp_path)
    assert segs == [Segment("SELF", "do what I would do")]


def test_missing_origin_is_self_not_user(tmp_path):
    """No provenance at all is the weakest possible claim, not the strongest."""
    run = _run(_human_origin(), [_msg("user", "unattributed")])
    del run["origin"]
    assert _sip_one(run, tmp_path) == [Segment("SELF", "unattributed")]


def test_segments_pass_through_the_house_caps(tmp_path):
    """MAX_SEG for prose, MAX_TOOL for machine output — the same ceilings the other
    adapters use, so one journal cannot spend a whole batch on one line."""
    segs = _sip_one(_run(_human_origin(), [
        _msg("user", "あ" * (MAX_SEG + 500)),
        _tool_result("x" * (MAX_TOOL + 500)),
    ]), tmp_path)
    assert [(s.cls, len(s.text)) for s in segs] == [("USER", MAX_SEG), ("TOOL", MAX_TOOL)]


# ── corruption handling ─────────────────────────────────────────────────────

def test_complete_invalid_json_line_raises(tmp_path):
    path = lna_journal(tmp_path)
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"v":1,"type":"run","runId":"ok","entries":[]}\n')
        f.write("{this is not json}\n")
    src = LnaJournalSource()
    with pytest.raises(RuntimeError, match="invalid JSON"):
        src.sip(path, 0, 1 << 30)


def test_unsupported_v_raises(tmp_path):
    path = lna_journal(tmp_path)
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"v":2,"type":"run","runId":"x","entries":[]}\n')
    with pytest.raises(RuntimeError, match="unsupported record"):
        LnaJournalSource().sip(path, 0, 1 << 30)


def test_non_run_type_raises(tmp_path):
    path = lna_journal(tmp_path)
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"v":1,"type":"memory","runId":"x","entries":[]}\n')
    with pytest.raises(RuntimeError, match="unsupported record"):
        LnaJournalSource().sip(path, 0, 1 << 30)


def test_corruption_raises_in_claim_bound_too(tmp_path):
    """The reserve must fail as loudly as the read.

    `Watermarks.claim()` writes the reserve BEFORE `sip()` runs, and `advance()` only
    ever moves forward — so a `claim_bound` that returned a bound without parsing
    would put the mark past the corruption, sip would raise, and the next pass would
    find the file drunk. The loud failure would have swallowed the good runs behind
    the bad line. Measured on exactly this three-line journal before the shortcut in
    `claim_bound` was removed: reserved 379 of 379 bytes, read nothing, lost both
    healthy runs.
    """
    path = lna_journal(tmp_path)
    good = json.dumps(_run(_human_origin(), [_msg("user", "real human evidence")]),
                      ensure_ascii=False)
    with open(path, "w", encoding="utf-8") as f:
        f.write(good + "\n")
        f.write("{broken json}\n")            # a COMPLETE line that will not parse
        f.write(good.replace("real human", "second real") + "\n")
    src = LnaJournalSource()
    with pytest.raises(RuntimeError, match="invalid JSON"):
        src.claim_bound(path, 0, 1 << 30)     # would have returned the file size

    # …and through the real reserve-then-read path, the mark never moves.
    marks = str(tmp_path / "m" / "marks.json")
    wm = Watermarks(marks)
    with pytest.raises(RuntimeError, match="invalid JSON"):
        wm.claim([path], 1 << 30, 1)
    assert wm.read().get(src.key(path), 0) == 0


# ── watermark: never drink a partial line ───────────────────────────────────

def test_empty_journal_reads_nothing_and_moves_nothing(tmp_path):
    """The first-run state on a fresh node: the writer has made the file and not yet
    written a run."""
    path = lna_journal(tmp_path)
    open(path, "wb").close()
    src = LnaJournalSource()
    assert src.sip(path, 0, 1 << 30) == ([], 0)
    assert src.claim_bound(path, 0, 1 << 30) == (0, 0)


def test_journal_of_only_a_partial_line_reads_nothing(tmp_path):
    """The very first run, caught mid-write. There is no complete line behind it to
    fall back to, so the mark must stay at zero rather than at the file's size."""
    path = lna_journal(tmp_path)
    with open(path, "wb") as f:
        f.write('{"v":1,"type":"run","runId":"first"'.encode("utf-8"))
    src = LnaJournalSource()
    assert src.sip(path, 0, 1 << 30) == ([], 0)
    assert src.claim_bound(path, 0, 1 << 30) == (0, 0)


def _good_line() -> str:
    return json.dumps(_run(_human_origin(), [_msg("user", "a complete run")]),
                      ensure_ascii=False)


def test_partial_final_line_is_not_read(tmp_path):
    path = lna_journal(tmp_path)
    with open(path, "wb") as f:
        f.write((_good_line() + "\n").encode("utf-8"))
        f.write('{"v":1,"type":"run","runId":"half"'.encode("utf-8"))  # no newline
    src = LnaJournalSource()
    segs, stop = src.sip(path, 0, 1 << 30)
    assert [s.cls for s in segs] == ["USER"]
    # stop is at the END of the complete line — not past the partial one
    complete = _good_line() + "\n"
    assert stop == len(complete.encode("utf-8"))


def test_partial_line_bytes_not_in_claim_bound(tmp_path):
    path = lna_journal(tmp_path)
    with open(path, "wb") as f:
        f.write((_good_line() + "\n").encode("utf-8"))
        f.write('{"v":1,"type":"run","runId":"half"'.encode("utf-8"))
    src = LnaJournalSource()
    end, _ = src.claim_bound(path, 0, 1 << 30)
    complete = _good_line() + "\n"
    assert end == len(complete.encode("utf-8"))


def test_completed_partial_is_read_next_round(tmp_path):
    path = lna_journal(tmp_path)
    head = ('{"v":1,"type":"run","runId":"half","origin":'
            '{"rootKind":"human","currentKind":"human","delegated":false},"entries":[')
    with open(path, "wb") as f:
        f.write((_good_line() + "\n").encode("utf-8"))
        f.write(head.encode("utf-8"))            # cut mid-record: no newline yet
    src = LnaJournalSource()
    segs1, stop = src.sip(path, 0, 1 << 30)
    assert [s.cls for s in segs1] == ["USER"]
    # the writer finishes the line it was in the middle of
    with open(path, "ab") as f:
        f.write('{"type":"message","role":"user","text":"late"}]}\n'.encode("utf-8"))
    segs2, stop2 = src.sip(path, stop, 1 << 30)
    assert segs2 == [Segment("USER", "late")]    # the once-partial run, read whole
    assert stop2 == os.path.getsize(path)        # and the mark is now at EOF


def _line_offsets(path: str) -> list[int]:
    """Byte offset of the start of each line — the only starts a mark can hold."""
    offs, at = [], 0
    with open(path, "rb") as h:
        for line in h:
            offs.append(at)
            at += len(line)
    return offs


def test_claim_end_equals_sip_stop(tmp_path):
    path = lna_journal(tmp_path)
    records = [_run(_human_origin(), [_msg("user", f"line {i}")], run_id=f"r{i}")
               for i in range(50)]
    write_journal(path, records)
    src = LnaJournalSource()
    offs = _line_offsets(path)
    # Every start a watermark can actually hold is a line start: each stop this
    # adapter ever returns is one. Budgets on both sides of a single run, so the
    # early-return branch and the run-to-EOF branch are both exercised.
    for start in (offs[0], offs[1], offs[17], offs[-1]):
        for budget in (1, 30, 2_000, 1 << 30):
            end, approx = src.claim_bound(path, start, budget)
            _, stop = src.sip(path, start, budget)
            assert end == stop, (f"start {start} budget {budget}: "
                                 f"reserved {end}, read stopped {stop}")
            assert approx == max(0, end - start)


# ── drain: small budget == unlimited sip, exact ─────────────────────────────

def _drain(src, path, marks_path, budget, rounds=4000):
    wm = Watermarks(marks_path)
    got, stretches = [], []
    for _ in range(rounds):
        c = wm.claim([path], budget, 1)
        if not c:
            return got, stretches
        _, start, s = c
        segs, stop = s.sip(path, start, budget)
        wm.advance(s.key(path), stop)
        got += segs
        stretches.append((start, stop))
    raise AssertionError("the drain never finished")


def test_drain_small_budget_matches_unlimited_sip(tmp_path):
    path = lna_journal(tmp_path)
    # CJK-heavy journal: 3+ bytes per char; small budgets land mid-line often.
    records = []
    for i in range(300):
        records.append(_run(_human_origin(), [
            _msg("user", f"{i}番目の記録です。ガレージの扉のセンサーは今日も静かでした。"),
            _msg("assistant", f"承知しました。{i}件目として控えます。"),
        ], run_id=f"r{i}"))
    write_journal(path, records)
    src = LnaJournalSource()
    whole, _ = src.sip(path, 0, 1 << 40)
    drunk, stretches = _drain(src, path, str(tmp_path / "marks" / "marks.json"), 200)
    assert [(s.cls, s.text) for s in drunk] == [(s.cls, s.text) for s in whole]
    # stretches tile the file exactly: no gap, no overlap
    assert stretches[0][0] == 0
    assert all(b[0] == a[1] for a, b in zip(stretches, stretches[1:]))
    assert stretches[-1][1] == os.path.getsize(path)


def test_partial_line_during_drain_stops_watermark_then_recovers(tmp_path):
    """The J1-C shape: writer appends a run, then the drainer runs while the
    writer has written only half the next line. The mark must not pass the
    partial line; once the line completes, the next drain reads it."""
    path = lna_journal(tmp_path)
    records = [_run(_human_origin(), [_msg("user", f"run {i}")], run_id=f"r{i}")
               for i in range(20)]
    write_journal(path, records)
    # writer begins a run, stops halfway (crash / slow write)
    with open(path, "ab") as f:
        f.write('{"v":1,"type":"run","runId":"r20","entries":[{"type":"message"'.encode("utf-8"))

    src = LnaJournalSource()
    marks = str(tmp_path / "m2" / "marks.json")
    wm = Watermarks(marks)
    # drain once: reads the 20 complete runs, stops before the partial line
    c = wm.claim([path], 1 << 30, 1)
    _, start, s = c
    segs, stop = s.sip(path, start, 1 << 30)
    wm.advance(s.key(path), stop)
    assert len(segs) == 20
    complete_20 = "".join(
        json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode("utf-8")
    assert stop == len(complete_20)          # mark at end of run 20, not past r20
    # writer finishes r20
    with open(path, "ab") as f:
        f.write(',"role":"user","text":"late"}]}\n'.encode("utf-8"))
    # next drain reads exactly r20
    c = wm.claim([path], 1 << 30, 1)
    _, start2, s2 = c
    segs2, stop2 = s2.sip(path, start2, 1 << 30)
    assert [sg.text for sg in segs2] == ["late"]
    wm.advance(s2.key(path), stop2)
    assert stop2 == os.path.getsize(path)
