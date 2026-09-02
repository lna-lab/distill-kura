"""Classified `.evidence.jsonl` intake: schema, safety, watermarks, discovery."""
from __future__ import annotations

import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from distill_kura.distill.pipeline import Distiller, MIN_DRINK, SipPending   # noqa: E402
from distill_kura.distill.sources import (                  # noqa: E402
    MAX_ID,
    MAX_LINE,
    MAX_SEG,
    MAX_TOOL,
    SCAN_LIMIT,
    SOURCES,
    ClaudeCodeSource,
    EvidenceJsonlSource,
    IntakeReport,
    Segment,
    Source,
    call_claim_bound,
    call_sip,
    discover_all,
    source_for,
)
from distill_kura.distill.watermark import Watermarks          # noqa: E402
from distill_kura.registry import Registry                    # noqa: E402
from distill_kura.store import Store                          # noqa: E402
from distill_kura.thinker import Models                       # noqa: E402


def _event(cls: str, text: str, **extra) -> dict:
    base = {
        "schema_version": 1,
        "event_id": "evt-1",
        "session_id": "sess-1",
        "turn_id": "turn-1",
        "class": cls,
        "text": text,
        "timestamp": "2026-08-27T00:00:00Z",
    }
    base.update(extra)
    return base


def _write(path, *events, trailing_partial: str | None = None):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
        if trailing_partial is not None:
            f.write(trailing_partial)


# One irreversibly-oversized line: MAX_LINE+1 detection read + at most SCAN_LIMIT scan.
_PER_ATTEMPT_BYTE_CAP = SCAN_LIMIT + MAX_LINE + 1
# Resumed scan continues from saved cursor — no second detection read.
_PER_RESUME_BYTE_CAP = SCAN_LIMIT


def _track_readline_bytes(path, monkeypatch) -> list[int]:
    """Bytes consumed via readline() per open/close, for one target file."""
    consumed: list[int] = []
    real_open = open
    abspath = os.path.abspath(str(path))

    def open_wrapper(open_path, mode="r", *args, **kwargs):
        fh = real_open(open_path, mode, *args, **kwargs)
        if os.path.abspath(str(open_path)) == abspath and "b" in mode:
            total = 0
            base_readline = fh.readline

            def readline(size=-1):
                nonlocal total
                chunk = base_readline(size)
                if chunk:
                    total += len(chunk)
                return chunk

            fh.readline = readline
            base_close = fh.close

            def close():
                consumed.append(total)
                return base_close()

            fh.close = close
        return fh

    monkeypatch.setattr("builtins.open", open_wrapper)
    return consumed


def _sip_past_huge_prefix(src, path, start, limit, *, bound_end=None, max_rounds=200):
    """Repeat sip until segments appear (progressive oversized-line discard)."""
    pos = start
    for _ in range(max_rounds):
        kwargs: dict = {}
        if bound_end is not None:
            kwargs["bound_end"] = bound_end
        segs, pos = src.sip(path, pos, limit, **kwargs)
        if segs:
            return segs, pos
    raise AssertionError("sip stuck scanning an oversized prefix")


def _clear_scan_state(*sources: EvidenceJsonlSource) -> None:
    seen: set[int] = set()
    for src in sources:
        sid = id(src)
        if sid in seen:
            continue
        seen.add(sid)
        with src._scan_lock:
            src._scan_cursors.clear()


@pytest.fixture(autouse=True)
def _evidence_scan_state_isolation():
    singleton = SOURCES.get("evidence")
    _clear_scan_state(EvidenceJsonlSource(), singleton if isinstance(singleton, EvidenceJsonlSource) else EvidenceJsonlSource())
    yield
    _clear_scan_state(EvidenceJsonlSource(), singleton if isinstance(singleton, EvidenceJsonlSource) else EvidenceJsonlSource())

# ── classification ──────────────────────────────────────────────────────────

def test_all_four_evidence_classes_are_preserved(tmp_path):
    p = tmp_path / "j.evidence.jsonl"
    _write(p,
           _event("USER", "human words"),
           _event("SELF", "assistant prose", event_id="e2"),
           _event("ACT", "tool_call read_file", event_id="e3"),
           _event("TOOL", "file contents here", event_id="e4"))
    segs, end = EvidenceJsonlSource().sip(str(p), 0, 10_000)
    assert [(s.cls, s.text) for s in segs] == [
        ("USER", "human words"),
        ("SELF", "assistant prose"),
        ("ACT", "tool_call read_file"),
        ("TOOL", "file contents here"),
    ]
    assert end == p.stat().st_size


def test_source_for_prefers_evidence_over_claude(tmp_path):
    p = tmp_path / "x.evidence.jsonl"
    p.write_text("{}\n", encoding="utf-8")
    assert source_for(str(p)).name == "evidence"
    assert not ClaudeCodeSource().matches(str(p))


def test_claude_discover_omits_evidence_jsonl(tmp_path):
    root = tmp_path / "logs"
    root.mkdir()
    (root / "plain.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "tagged.evidence.jsonl").write_text("{}\n", encoding="utf-8")
    found = ClaudeCodeSource().discover(str(root))
    assert str(root / "plain.jsonl") in found
    assert str(root / "tagged.evidence.jsonl") not in found


# ── malformed / invalid lines ─────────────────────────────────────────────

def test_malformed_json_is_skipped_not_reclassified(tmp_path):
    p = tmp_path / "bad.evidence.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        f.write("not json at all\n")
        f.write(json.dumps(_event("USER", "good line", event_id="e2")) + "\n")
    segs, _ = EvidenceJsonlSource().sip(str(p), 0, 10_000)
    assert len(segs) == 1 and segs[0].cls == "USER" and segs[0].text == "good line"


def test_unknown_schema_version_is_skipped(tmp_path):
    p = tmp_path / "v2.evidence.jsonl"
    _write(p, {**_event("USER", "future"), "schema_version": 2})
    assert EvidenceJsonlSource().sip(str(p), 0, 10_000)[0] == []


def test_unknown_class_is_skipped_not_mapped_to_user(tmp_path):
    p = tmp_path / "cls.evidence.jsonl"
    _write(p, {**_event("USER", "ok"), "class": "SYSTEM"})
    segs, _ = EvidenceJsonlSource().sip(str(p), 0, 10_000)
    assert segs == []


def test_missing_or_blank_ids_are_skipped(tmp_path):
    p = tmp_path / "ids.evidence.jsonl"
    _write(p,
           {**_event("USER", "no event_id"), "event_id": ""},
           {**_event("USER", "no session"), "session_id": "  "},
           {**_event("USER", "missing turn"), "turn_id": None})
    assert EvidenceJsonlSource().sip(str(p), 0, 10_000)[0] == []


def test_blank_or_non_string_text_is_skipped(tmp_path):
    p = tmp_path / "text.evidence.jsonl"
    _write(p,
           {**_event("USER", ""), "text": ""},
           {**_event("USER", "x"), "text": 42},
           {**_event("USER", "x"), "text": "   "})
    assert EvidenceJsonlSource().sip(str(p), 0, 10_000)[0] == []


def test_oversized_text_is_truncated_not_skipped(tmp_path):
    p = tmp_path / "big.evidence.jsonl"
    _write(p,
           _event("USER", "x" * (MAX_SEG + 1), event_id="big-user"),
           _event("TOOL", "y" * (MAX_TOOL + 1), event_id="big-tool"),
           _event("USER", "fits", event_id="ok"))
    segs, _ = EvidenceJsonlSource().sip(str(p), 0, 10_000)
    assert len(segs) == 3
    assert segs[0].cls == "USER" and len(segs[0].text) == MAX_SEG and segs[0].text == "x" * MAX_SEG
    assert segs[1].cls == "TOOL" and len(segs[1].text) == MAX_TOOL and segs[1].text == "y" * MAX_TOOL
    assert segs[2].text == "fits"


def test_oversized_text_is_stripped_before_cap(tmp_path):
    pad = " " * 10
    p = tmp_path / "strip-cap.evidence.jsonl"
    _write(p, _event("USER", pad + ("x" * MAX_SEG) + pad, event_id="strip-user"))
    segs, _ = EvidenceJsonlSource().sip(str(p), 0, 10_000)
    assert len(segs) == 1
    assert segs[0].text == "x" * MAX_SEG


# ── incomplete final line / watermark ─────────────────────────────────────

def test_incomplete_final_line_does_not_advance_watermark(tmp_path):
    p = tmp_path / "tail.evidence.jsonl"
    good = json.dumps(_event("USER", "first"))
    with open(p, "wb") as f:
        f.write((good + "\n").encode())
        f.write(b'{"schema_version": 1, "event_id": "e2"')
    src = EvidenceJsonlSource()
    segs1, pos1 = src.sip(str(p), 0, 10_000)
    assert len(segs1) == 1 and segs1[0].text == "first"
    assert pos1 == len(good) + 1
    end, _, _ = src.claim_bound(str(p), 0, 10_000)
    assert end == pos1 < p.stat().st_size

    with open(p, "ab") as f:
        f.write(b', "session_id": "s", "turn_id": "t", "class": "USER", '
                b'"text": "second", "timestamp": "2026-08-27T00:00:01Z"}\n')
    segs2, pos2 = src.sip(str(p), pos1, 10_000)
    assert len(segs2) == 1 and segs2[0].text == "second"
    assert pos2 == p.stat().st_size


def test_watermark_resume_skips_already_drunk_lines(tmp_path):
    p = tmp_path / "resume.evidence.jsonl"
    _write(p,
           _event("USER", "one", event_id="e1"),
           _event("USER", "two", event_id="e2"))
    src = EvidenceJsonlSource()
    first_line = (json.dumps(_event("USER", "one", event_id="e1")) + "\n").encode()
    segs, pos = src.sip(str(p), len(first_line), 10_000)
    assert len(segs) == 1 and segs[0].text == "two"
    assert pos == p.stat().st_size


def test_duplicate_basenames_get_distinct_watermark_keys(tmp_path):
    src = EvidenceJsonlSource()
    a = tmp_path / "a" / "j.evidence.jsonl"
    b = tmp_path / "b" / "j.evidence.jsonl"
    _write(a, _event("USER", "from a"))
    _write(b, _event("USER", "from b"))
    assert src.key(str(a)) != src.key(str(b))
    assert src.key(str(a)).startswith("evidence:")


# ── discovery: include/exclude and store isolation ──────────────────────────

def test_include_and_exclude_globs_narrow_evidence_root(tmp_path):
    _write(tmp_path / "logs" / "keep" / "a.evidence.jsonl", _event("USER", "keep me"))
    _write(tmp_path / "logs" / "skip" / "b.evidence.jsonl", _event("USER", "drop me"))
    root = str(tmp_path / "logs")
    kept = discover_all({"evidence": {"root": root, "exclude_glob": ["skip/**"]}})
    assert len(kept) == 1 and "keep" in kept[0]
    only = discover_all({"evidence": {"root": root, "include_glob": ["skip/**"]}})
    assert len(only) == 1 and "skip" in only[0]


def test_a_hardlinked_memory_in_an_evidence_root_is_not_discovered(tmp_path):
    st = Store(name="s", path=str(tmp_path / "s"))
    st.init_files()
    st.remember("mem", "d", "MODEL-WRITTEN MEMORY BODY")
    jr = tmp_path / "jr"
    jr.mkdir()
    _write(jr / "real.evidence.jsonl", _event("USER", "human note"))
    os.link(st.file_of("mem"), jr / "hardlinked.evidence.jsonl")
    found = discover_all({"evidence": str(jr)}, exclude_roots=[st.path])
    names = [os.path.basename(f) for f in found]
    assert names == ["real.evidence.jsonl"]


# ── timestamp contract ──────────────────────────────────────────────────────

def test_missing_or_non_string_timestamp_is_skipped(tmp_path):
    p = tmp_path / "ts.evidence.jsonl"
    _write(p,
           {k: v for k, v in _event("USER", "no ts").items() if k != "timestamp"},
           {**_event("USER", "numeric", event_id="e2"), "timestamp": 1724716800},
           {**_event("USER", "null", event_id="e3"), "timestamp": None})
    segs, _ = EvidenceJsonlSource().sip(str(p), 0, 10_000)
    assert segs == []


def test_malformed_and_naive_timestamps_are_skipped(tmp_path):
    p = tmp_path / "naive.evidence.jsonl"
    _write(p,
           {**_event("USER", "date-only"), "timestamp": "2026-08-27"},
           {**_event("USER", "naive", event_id="e2"), "timestamp": "2026-08-27T00:00:00"},
           {**_event("USER", "space", event_id="e3"), "timestamp": "2026-08-27 00:00:00Z"},
           {**_event("USER", "leap", event_id="e4"), "timestamp": "2026-06-30T23:59:60Z"})
    assert EvidenceJsonlSource().sip(str(p), 0, 10_000)[0] == []


def test_rfc3339_z_offset_and_fractional_timestamps_are_accepted(tmp_path):
    p = tmp_path / "ok-ts.evidence.jsonl"
    _write(p,
           _event("USER", "z"),
           _event("USER", "frac", event_id="e2", timestamp="2026-08-27T00:00:00.123Z"),
           _event("USER", "offset", event_id="e3", timestamp="2026-08-27T09:00:00+09:00"))
    segs, _ = EvidenceJsonlSource().sip(str(p), 0, 10_000)
    assert [s.text for s in segs] == ["z", "frac", "offset"]


def test_timestamp_is_a_gate_not_a_stored_or_filled_field(tmp_path):
    """A missing timestamp is skipped. The clock is not a fallback, and the
    segment never gains a timestamp field — evidence is not rewritten."""
    p = tmp_path / "clock.evidence.jsonl"
    _write(p, {k: v for k, v in _event("USER", "no ts").items() if k != "timestamp"})
    segs, _ = EvidenceJsonlSource().sip(str(p), 0, 10_000)
    assert segs == []
    assert set(Segment.__dataclass_fields__) == {"cls", "text"}


def test_json_true_is_not_schema_version_one(tmp_path):
    p = tmp_path / "bool.evidence.jsonl"
    _write(p, {**_event("USER", "bool ver"), "schema_version": True})
    assert EvidenceJsonlSource().sip(str(p), 0, 10_000)[0] == []


# ── bounded parsing ─────────────────────────────────────────────────────────

def test_oversized_ids_are_skipped_not_truncated(tmp_path):
    p = tmp_path / "ids-size.evidence.jsonl"
    _write(p,
           _event("USER", "too long", event_id="e" * (MAX_ID + 1)),
           _event("USER", "ordinary-uuid", event_id="550e8400-e29b-41d4-a716-446655440000"))
    segs, _ = EvidenceJsonlSource().sip(str(p), 0, 10_000)
    assert len(segs) == 1 and segs[0].text == "ordinary-uuid"


def test_oversized_line_is_skipped_without_json_loads(tmp_path, monkeypatch):
    p = tmp_path / "huge.evidence.jsonl"
    huge = b'{"schema_version": 1, "event_id": "e", "text": "' + (b"A" * (MAX_LINE + 50)) + b'"}\n'
    good = (json.dumps(_event("USER", "after the dump", event_id="ok")) + "\n").encode()
    with open(p, "wb") as f:
        f.write(huge)
        f.write(good)
    called = {"n": 0}
    real = json.loads

    def spy(s, *a, **k):
        called["n"] += 1
        if isinstance(s, (bytes, bytearray)) and len(s) > MAX_LINE:
            raise AssertionError("json.loads on an oversized line")
        if isinstance(s, str) and len(s.encode()) > MAX_LINE:
            raise AssertionError("json.loads on an oversized line")
        return real(s, *a, **k)
    monkeypatch.setattr("distill_kura.distill.sources.json.loads", spy)
    segs, end = EvidenceJsonlSource().sip(str(p), 0, 10_000)
    assert len(segs) == 1 and segs[0].text == "after the dump"
    assert end == p.stat().st_size
    assert called["n"] == 1


# ── runtime reporting ───────────────────────────────────────────────────────

def test_skips_are_reported_without_payloads_or_paths(tmp_path):
    p = tmp_path / "secret-dir" / "j.evidence.jsonl"
    secret = "credential-hunter2-not-for-logs"
    os.makedirs(p.parent, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write("this is not json and contains " + secret + "\n")
        f.write(json.dumps({**_event("USER", "future " + secret), "schema_version": 2}) + "\n")
        f.write(json.dumps({**_event("USER", "sys " + secret), "class": "SYSTEM"}) + "\n")
        f.write(json.dumps({**_event("USER", "blank " + secret), "event_id": ""}) + "\n")
        f.write(json.dumps({**_event("USER", ""), "text": ""}) + "\n")
        f.write(json.dumps({**_event("USER", "oversized id " + secret),
                            "event_id": "e" * (MAX_ID + 1)}) + "\n")
        f.write(json.dumps({k: v for k, v in _event("USER", "no ts " + secret).items()
                            if k != "timestamp"}) + "\n")
        f.write('{"schema_version": 1, "event_id": "partial-' + secret + '"')
    report = IntakeReport()
    segs, pos = EvidenceJsonlSource().sip(str(p), 0, 10_000, report=report)
    assert segs == []
    assert pos < p.stat().st_size
    assert report.skipped.get("malformed")
    assert report.skipped.get("unknown_version")
    assert report.skipped.get("unknown_class")
    assert report.skipped.get("blank")
    assert report.skipped.get("oversized")
    assert report.skipped.get("missing")
    assert report.skipped.get("partial")
    blob = json.dumps(report.as_dict())
    assert secret not in blob
    assert "secret-dir" not in blob
    assert str(p) not in blob
    assert len(report.samples) <= IntakeReport.MAX_SAMPLES


def test_reporting_is_bounded_on_a_flood_of_junk(tmp_path):
    p = tmp_path / "flood.evidence.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for _ in range(200):
            f.write("not-json\n")
    report = IntakeReport()
    EvidenceJsonlSource().sip(str(p), 0, 10_000, report=report)
    assert report.skipped["malformed"] == 200
    assert len(report.samples) == IntakeReport.MAX_SAMPLES


def _blob(tag: str, n: int = 3500) -> str:
    return (tag + " " + ("x" * n))[:n]


def _distiller(tmp_path, journal_dir, chunk_chars=4000):
    st = Store(name="s", path=str(tmp_path / "s"))
    st.init_files()
    models = Models.from_config({})
    reg = Registry(stores={"s": st}, modes={}, models=models, default="s",
                   raw={"distill": {"journals": {"evidence": str(journal_dir)}}})
    return Distiller(reg, st, chunk_chars=chunk_chars), st


# ── real claim + sip_one durability ─────────────────────────────────────────

def test_sip_one_partial_tail_does_not_skip_unread_bytes(tmp_path):
    """Luna: claim reserved byte 192, sip returned 156, max-forward kept 192."""
    jr = tmp_path / "jr"
    jr.mkdir()
    p = jr / "j.evidence.jsonl"
    events = [_event("USER", _blob(f"keep-{i}"), event_id=f"e{i}") for i in range(2)]
    _write(p, *events, trailing_partial='{"schema_version": 1, "event_id": "tail')
    complete = sum(len((json.dumps(e) + "\n").encode()) for e in events)
    assert complete < p.stat().st_size
    d, st = _distiller(tmp_path, jr, chunk_chars=20_000)
    got = d.sip_one()
    assert got is not None
    segs, path, key = got
    assert [s.text[:6] for s in segs] == ["keep-0", "keep-1"]
    mark = d.marks.read()[key]
    assert mark == complete
    assert mark < p.stat().st_size
    # completing the tail must be drinkable, not skipped
    with open(p, "ab") as f:
        f.write(b"\n")
        f.write((json.dumps(_event("USER", _blob("keep-2"), event_id="e2")) + "\n").encode())
        f.write((json.dumps(_event("USER", _blob("keep-3"), event_id="e3")) + "\n").encode())
    got2 = d.sip_one()
    assert got2 is not None
    assert {s.text[:6] for s in got2[0]} == {"keep-2", "keep-3"}


def test_sip_one_large_complete_events_are_not_skipped_by_overclaim(tmp_path):
    """Luna: claim reserved 22000 while sip consumed only through 12309.

    A 2.2× byte/char fudge in claim() reserved past sip's char-budget stop;
    max-forward then skipped the unread complete record forever.
    """
    jr = tmp_path / "jr"
    jr.mkdir()
    p = jr / "j.evidence.jsonl"
    events = [_event("USER", _blob(f"evt-{i}"), event_id=f"e{i}") for i in range(4)]
    _write(p, *events)
    d, _ = _distiller(tmp_path, jr, chunk_chars=4000)
    first = d.sip_one()
    second = d.sip_one()
    assert first is not None and second is not None
    assert [s.text[:5] for s in first[0] + second[0]] == ["evt-0", "evt-1", "evt-2", "evt-3"]
    assert d.sip_one() is None
    assert d.marks.read()[first[2]] == p.stat().st_size


def test_sip_one_resume_does_not_redrink_or_skip(tmp_path):
    jr = tmp_path / "jr"
    jr.mkdir()
    p = jr / "j.evidence.jsonl"
    events = [_event("USER", _blob(f"n{i}"), event_id=f"e{i}") for i in range(4)]
    _write(p, *events)
    d, _ = _distiller(tmp_path, jr, chunk_chars=4000)
    first = d.sip_one()
    second = d.sip_one()
    third = d.sip_one()
    assert first and second
    assert third is None
    seen = [s.text[:2] for s in first[0] + second[0]]
    assert seen == ["n0", "n1", "n2", "n3"]
    assert d.marks.read()[first[2]] == p.stat().st_size


def test_evidence_claim_bound_is_where_the_read_stops(tmp_path):
    p = tmp_path / "bound.evidence.jsonl"
    events = [_event("USER", _blob(f"b{i}"), event_id=f"e{i}") for i in range(6)]
    _write(p, *events)
    src = EvidenceJsonlSource()
    for budget in (500, 4000, 20_000):
        end, _, _ = src.claim_bound(str(p), 0, budget)
        _, stop = src.sip(str(p), 0, budget)
        assert end == stop


def test_parallel_claims_reserve_disjoint_complete_records(tmp_path):
    jr = tmp_path / "jr"
    jr.mkdir()
    p = jr / "j.evidence.jsonl"
    events = [_event("USER", _blob(f"p{i}"), event_id=f"e{i}") for i in range(8)]
    _write(p, *events)
    d, st = _distiller(tmp_path, jr, chunk_chars=4000)
    first = d.marks.claim([str(p)], 4000, MIN_DRINK)
    assert first is not None
    path_a, start_a, end_a, src_a = first.path, first.start, first.end, first.source
    key = src_a.key(str(p))
    reserved_a = d.marks.read()[key]
    second = d.marks.claim([str(p)], 4000, MIN_DRINK)
    assert second is not None
    path_b, start_b, end_b, src_b = second.path, second.start, second.end, second.source
    reserved_b = d.marks.read()[key]
    assert start_a < start_b
    assert reserved_a <= start_b
    assert reserved_b > start_b
    # sip of each reservation drinks only that stretch
    src = EvidenceJsonlSource()
    a, a_end = call_sip(src, path_a, start_a, 4000, bound_end=reserved_a)
    b, b_end = call_sip(src, path_b, start_b, 4000, bound_end=reserved_b)
    assert a_end == reserved_a and b_end == reserved_b
    assert {s.text[:2] for s in a}.isdisjoint({s.text[:2] for s in b})
    d.marks.advance(src.key(str(p)), a_end)
    d.marks.advance(src.key(str(p)), b_end)
    assert d.marks.read()[src.key(str(p))] == max(a_end, b_end)


def test_parallel_sip_one_does_not_overlap_or_skip(tmp_path):
    jr = tmp_path / "jr"
    jr.mkdir()
    p = jr / "j.evidence.jsonl"
    events = [_event("USER", _blob(f"t{i}"), event_id=f"e{i}") for i in range(8)]
    _write(p, *events)
    d1, st = _distiller(tmp_path, jr, chunk_chars=4000)
    d2, _ = _distiller(tmp_path, jr, chunk_chars=4000)
    d2.marks = d1.marks
    bag = []

    def worker(d):
        got = d.sip_one()
        if got:
            bag.append([s.text[:2] for s in got[0]])

    threads = [threading.Thread(target=worker, args=(d,)) for d in (d1, d2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    flat = [x for row in bag for x in row]
    assert len(flat) == len(set(flat))
    assert set(flat) <= {f"t{i}" for i in range(8)}
    # leftover complete records are still drinkable
    rest = d1.sip_one()
    if rest:
        flat += [s.text[:2] for s in rest[0]]
    rest2 = d1.sip_one()
    if rest2:
        flat += [s.text[:2] for s in rest2[0]]
    assert sorted(flat) == [f"t{i}" for i in range(8)]


def test_sip_one_writes_bounded_intake_without_payloads(tmp_path):
    jr = tmp_path / "secret-root"
    jr.mkdir()
    p = jr / "j.evidence.jsonl"
    secret = "token-abc-not-for-logs"
    events = [_event("USER", _blob(f"ok{i}"), event_id=f"e{i}") for i in range(2)]
    with open(p, "w", encoding="utf-8") as f:
        f.write("not json " + secret + "\n")
        for e in events:
            f.write(json.dumps(e) + "\n")
        f.write(json.dumps({**_event("USER", "sys " + secret), "class": "SYSTEM"}) + "\n")
    d, st = _distiller(tmp_path, jr, chunk_chars=20_000)
    got = d.sip_one()
    assert got is not None
    intake = open(os.path.join(st.still, "intake.jsonl"), encoding="utf-8").read()
    assert "malformed" in intake and "unknown_class" in intake
    assert secret not in intake
    assert "secret-root" not in intake
    assert str(p) not in intake
    assert "j.evidence.jsonl" in intake


def test_intake_write_failure_does_not_break_sip_one(tmp_path):
    jr = tmp_path / "jr"
    jr.mkdir()
    p = jr / "j.evidence.jsonl"
    _write(p,
           _event("USER", _blob("a"), event_id="e1"),
           _event("USER", _blob("b"), event_id="e2"))
    with open(p, "a", encoding="utf-8") as f:
        f.write("not-json\n")
    d, st = _distiller(tmp_path, jr, chunk_chars=20_000)
    os.makedirs(os.path.join(st.still, "intake.jsonl"))
    got = d.sip_one()
    assert got is not None
    assert [s.text[:1] for s in got[0]] == ["a", "b"]


# ── Luna rework: reserved end, legacy sip, bounded tail ─────────────────────

class _LegacySource(Source):
    """Pre-existing custom adapter: sip(path, start, limit_chars) only."""
    name = "legacy"

    def matches(self, path: str) -> bool:
        return path.endswith(".legacy.txt")

    def key(self, path: str) -> str:
        return "legacy:" + os.path.abspath(path)

    def discover(self, root: str) -> list[str]:
        return []

    def sip(self, path: str, start: int, limit_chars: int) -> tuple[list[Segment], int]:
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(limit_chars)
        return [Segment("USER", data.decode(errors="replace"))], start + len(data)

    def claim_bound(self, path: str, start: int, budget_chars: int) -> tuple[int, int]:
        end = min(os.path.getsize(path), start + budget_chars)
        return end, max(0, end - start)


def test_call_claim_bound_normalizes_legacy_two_tuple(tmp_path):
    p = tmp_path / "x.legacy.txt"
    p.write_bytes(b"hello world")
    src = _LegacySource()
    assert call_claim_bound(src, str(p), 0, 5) == (5, 5, 0)


def test_watermark_claim_with_legacy_two_tuple_claim_bound(tmp_path, monkeypatch):
    legacy = _LegacySource()
    p = tmp_path / "note.legacy.txt"
    p.write_bytes(b"abcdefghij")
    marks = Watermarks(str(tmp_path / "marks.json"))
    monkeypatch.setattr("distill_kura.distill.watermark.source_for",
                        lambda path: legacy if legacy.matches(path) else None)
    c = marks.claim([str(p)], 4, 1)
    assert c is not None
    assert c.end == 4 and c.scan_pending == 0


def test_catch_up_advances_past_completed_oversized_line(tmp_path):
    """One catch_up must finish progressive discard through a huge completed line."""
    jr = tmp_path / "jr"
    jr.mkdir()
    p = jr / "j.evidence.jsonl"
    huge = b"x" * (2 * SCAN_LIMIT + 50_000) + b"\n"
    good = (json.dumps(_event("USER", "after", event_id="ok")) + "\n").encode()
    with open(p, "wb") as f:
        f.write(huge)
        f.write(good)
    d, _ = _distiller(tmp_path, jr, chunk_chars=4000)
    key = EvidenceJsonlSource().key(str(p))
    r = d.catch_up()
    assert r["ok"] and r["moved"] == 1
    assert d.marks.read()[key] == p.stat().st_size
    assert d.sip_one() is None


def test_catch_up_stops_before_unterminated_oversized_tail(tmp_path):
    """Completed prefix and valid event advance; open oversized tail stays unread."""
    jr = tmp_path / "jr"
    jr.mkdir()
    p = jr / "luna.evidence.jsonl"
    huge = b"x" * (MAX_LINE + 100) + b"\n"
    good = (json.dumps(_event("USER", "middle", event_id="mid")) + "\n").encode()
    tail = b"x" * (SCAN_LIMIT + 50_000)
    p.write_bytes(huge + good + tail)
    d, _ = _distiller(tmp_path, jr, chunk_chars=4000)
    key = EvidenceJsonlSource().key(str(p))
    r = d.catch_up()
    assert r["ok"] and r["moved"] == 1
    assert d.marks.read()[key] == len(huge) + len(good)
    assert d.marks.read()[key] < p.stat().st_size


def test_catch_up_with_legacy_two_tuple_claim_bound(tmp_path, monkeypatch):
    legacy = _LegacySource()
    jr = tmp_path / "jr"
    jr.mkdir()
    p = jr / "note.legacy.txt"
    p.write_bytes(b"abcdefghij")
    d, _ = _distiller(tmp_path, jr, chunk_chars=4)
    monkeypatch.setattr(d, "files", lambda session=None: [str(p)])
    real_source_for = source_for

    def fake_source_for(path):
        return legacy if legacy.matches(path) else real_source_for(path)

    monkeypatch.setattr("distill_kura.distill.sources.source_for", fake_source_for)
    monkeypatch.setattr("distill_kura.distill.watermark.source_for", fake_source_for)
    monkeypatch.setattr("distill_kura.distill.pipeline.source_for", fake_source_for)
    r = d.catch_up()
    assert r["ok"] and r["moved"] == 1
    assert d.marks.read()[legacy.key(str(p))] == len(b"abcdefghij")


def test_call_sip_legacy_three_arg_source_ignores_report(tmp_path):
    p = tmp_path / "x.legacy.txt"
    p.write_bytes(b"hello world")
    src = _LegacySource()
    segs, stop = call_sip(src, str(p), 0, 5, report=IntakeReport())
    assert len(segs) == 1 and segs[0].text == "hello"
    assert stop == 5


def test_sip_one_with_legacy_three_arg_source(tmp_path, monkeypatch):
    legacy = _LegacySource()
    jr = tmp_path / "jr"
    jr.mkdir()
    p = jr / "note.legacy.txt"
    p.write_bytes(b"abcdefghij")
    d, st = _distiller(tmp_path, jr, chunk_chars=4)
    monkeypatch.setattr("distill_kura.distill.pipeline.MIN_DRINK", 1)
    monkeypatch.setattr(d, "files", lambda session=None: [str(p)])
    real_source_for = source_for

    def fake_source_for(path):
        return legacy if legacy.matches(path) else real_source_for(path)

    monkeypatch.setattr("distill_kura.distill.sources.source_for", fake_source_for)
    monkeypatch.setattr("distill_kura.distill.watermark.source_for", fake_source_for)
    got = d.sip_one()
    assert got is not None
    segs, _, key = got
    assert segs[0].text == "abcd"
    assert d.marks.read()[key] == 4


def test_first_sip_respects_reserved_end_after_second_claim(tmp_path):
    """Adversarial: claim, append, second claim, first sip — no overlap or skip."""
    jr = tmp_path / "jr"
    jr.mkdir()
    p = jr / "j.evidence.jsonl"
    events = [_event("USER", _blob(f"c{i}"), event_id=f"e{i}") for i in range(4)]
    _write(p, *events)
    d, _ = _distiller(tmp_path, jr, chunk_chars=4000)
    first = d.marks.claim([str(p)], 4000, MIN_DRINK)
    assert first is not None
    path_a, start_a, end_a, src_a = first.path, first.start, first.end, first.source
    key = src_a.key(str(p))
    with open(p, "ab") as f:
        for i in range(4):
            f.write((json.dumps(_event("USER", _blob(f"late{i}"), event_id=f"late{i}"))
                     + "\n").encode())
    second = d.marks.claim([str(p)], 4000, MIN_DRINK)
    assert second is not None
    path_b, start_b, end_b, src_b = second.path, second.start, second.end, second.source
    assert start_b == end_a
    src = EvidenceJsonlSource()
    a, a_end = call_sip(src, path_a, start_a, 4000, bound_end=end_a)
    b, b_end = call_sip(src, path_b, start_b, 4000, bound_end=end_b)
    assert a_end == end_a <= start_b
    assert b_end == end_b
    assert {s.text[:4] for s in a}.isdisjoint({s.text[:4] for s in b})
    d.marks.advance(key, a_end)
    d.marks.advance(key, b_end)
    assert d.marks.read()[key] == max(a_end, b_end)


def test_unterminated_oversized_tail_stays_bounded_per_attempt(tmp_path, monkeypatch):
    p = tmp_path / "tail.evidence.jsonl"
    p.write_bytes(b"x" * (MAX_LINE * 10))
    consumed = _track_readline_bytes(p, monkeypatch)
    src = EvidenceJsonlSource()
    segs, pos = src.sip(str(p), 0, 10_000)
    assert segs == [] and pos == 0
    assert consumed and consumed[0] <= _PER_ATTEMPT_BYTE_CAP
    segs2, pos2 = src.sip(str(p), 0, 10_000)
    assert segs2 == [] and pos2 == 0
    assert len(consumed) == 2 and consumed[1] <= _PER_RESUME_BYTE_CAP


def test_completed_oversized_line_then_valid_event(tmp_path):
    p = tmp_path / "mix.evidence.jsonl"
    huge = (b'{"schema_version": 1, "event_id": "e", "session_id": "s", "turn_id": "t", '
            b'"class": "USER", "text": "' + (b"A" * (MAX_LINE + 50))
            + b'", "timestamp": "2026-08-27T00:00:00Z"}\n')
    good = (json.dumps(_event("USER", "after", event_id="ok")) + "\n").encode()
    with open(p, "wb") as f:
        f.write(huge)
        f.write(good)
    segs, end = EvidenceJsonlSource().sip(str(p), 0, 10_000)
    assert len(segs) == 1 and segs[0].text == "after"
    assert end == p.stat().st_size


def test_unterminated_tail_larger_than_scan_limit_stays_bounded(tmp_path, monkeypatch):
    """Garbage tail with no newline: capped per attempt, watermark unchanged."""
    p = tmp_path / "tail.evidence.jsonl"
    p.write_bytes(b"x" * (SCAN_LIMIT + 50_000))
    consumed = _track_readline_bytes(p, monkeypatch)
    src = EvidenceJsonlSource()
    segs, pos = src.sip(str(p), 0, 10_000)
    assert segs == [] and pos == 0
    assert consumed and consumed[0] <= _PER_ATTEMPT_BYTE_CAP
    segs2, pos2 = src.sip(str(p), 0, 10_000)
    assert segs2 == [] and pos2 == 0
    assert len(consumed) == 2 and consumed[1] <= _PER_RESUME_BYTE_CAP


def test_unterminated_json_tail_larger_than_scan_limit_stays_bounded(tmp_path, monkeypatch):
    """Unterminated JSON-shaped tail: same bounded cap, no prefix escape hatch."""
    p = tmp_path / "json-tail.evidence.jsonl"
    p.write_bytes(b'{"schema_version": 1, "event_id": "e"' + b"x" * (SCAN_LIMIT + 50_000))
    consumed = _track_readline_bytes(p, monkeypatch)
    src = EvidenceJsonlSource()
    segs, pos = src.sip(str(p), 0, 10_000)
    assert segs == [] and pos == 0
    assert consumed and consumed[0] <= _PER_ATTEMPT_BYTE_CAP
    segs2, pos2 = src.sip(str(p), 0, 10_000)
    assert segs2 == [] and pos2 == 0
    assert len(consumed) == 2 and consumed[1] <= _PER_RESUME_BYTE_CAP


def test_oversized_discard_resumes_from_cursor_not_byte_zero(tmp_path, monkeypatch):
    """Second sip continues the saved scan cursor instead of rereading from pos 0."""
    p = tmp_path / "resume-scan.evidence.jsonl"
    p.write_bytes(b"x" * (SCAN_LIMIT + 50_000))
    consumed = _track_readline_bytes(p, monkeypatch)
    src = EvidenceJsonlSource()
    src.sip(str(p), 0, 10_000)
    src.sip(str(p), 0, 10_000)
    assert len(consumed) == 2
    assert consumed[0] <= _PER_ATTEMPT_BYTE_CAP
    assert consumed[1] <= _PER_RESUME_BYTE_CAP
    assert consumed[0] > consumed[1]


def test_completed_nonjson_oversized_line_past_scan_limit_then_valid_event(tmp_path):
    """Completed non-JSON line longer than 2×SCAN_LIMIT must not block later evidence."""
    p = tmp_path / "xline.evidence.jsonl"
    huge = b"x" * (2 * SCAN_LIMIT + 50_000) + b"\n"
    good = (json.dumps(_event("USER", "after", event_id="ok")) + "\n").encode()
    with open(p, "wb") as f:
        f.write(huge)
        f.write(good)
    src = EvidenceJsonlSource()
    segs, end = _sip_past_huge_prefix(src, str(p), 0, 10_000)
    assert len(segs) == 1 and segs[0].text == "after"
    assert end == p.stat().st_size


def test_completed_oversized_line_past_scan_limit_then_valid_event(tmp_path):
    """Completed invalid line longer than 2×SCAN_LIMIT must not block later evidence."""
    p = tmp_path / "past-scan.evidence.jsonl"
    prefix = (b'{"schema_version": 1, "event_id": "e", "session_id": "s", "turn_id": "t", '
              b'"class": "USER", "text": "')
    suffix = b'", "timestamp": "2026-08-27T00:00:00Z"}\n'
    text_len = 2 * SCAN_LIMIT - len(prefix) - len(suffix) + 50_000
    huge = prefix + (b"A" * text_len) + suffix
    assert len(huge) > 2 * SCAN_LIMIT and huge.endswith(b"\n")
    good = (json.dumps(_event("USER", "after", event_id="ok")) + "\n").encode()
    with open(p, "wb") as f:
        f.write(huge)
        f.write(good)
    src = EvidenceJsonlSource()
    segs, end = _sip_past_huge_prefix(src, str(p), 0, 10_000)
    assert len(segs) == 1 and segs[0].text == "after"
    assert end == p.stat().st_size


def test_claim_bound_eventually_passes_min_drink_past_huge_prefix(tmp_path):
    """Skipped-byte span from progressive discard must satisfy MIN_DRINK."""
    p = tmp_path / "claim-past.evidence.jsonl"
    huge = b"x" * (2 * SCAN_LIMIT + 50_000) + b"\n"
    good = (json.dumps(_event("USER", _blob("enough"), event_id="ok")) + "\n").encode()
    with open(p, "wb") as f:
        f.write(huge)
        f.write(good)
    src = EvidenceJsonlSource()
    claimed = None
    for _ in range(30):
        end, approx, scan_pending = src.claim_bound(str(p), 0, 4000)
        if end > 0 and approx >= MIN_DRINK:
            claimed = (end, approx)
            break
        assert scan_pending > 0
    assert claimed is not None
    end, approx = claimed
    segs, sip_end = src.sip(str(p), 0, 4000, bound_end=end)
    assert len(segs) == 1 and segs[0].text.startswith("enough")
    assert sip_end == end == p.stat().st_size


def test_completed_oversized_valid_event_then_unterminated_tail(tmp_path):
    """Luna composite: skip huge line, drink valid event, stop before open tail."""
    p = tmp_path / "luna.evidence.jsonl"
    huge = b"x" * (MAX_LINE + 100) + b"\n"
    good = (json.dumps(_event("USER", "middle", event_id="mid")) + "\n").encode()
    tail = b"x" * (SCAN_LIMIT + 50_000)
    p.write_bytes(huge + good + tail)
    src = EvidenceJsonlSource()
    segs, pos = _sip_past_huge_prefix(src, str(p), 0, 10_000)
    assert len(segs) == 1 and segs[0].text == "middle"
    assert pos == len(huge) + len(good)
    segs2, pos2 = src.sip(str(p), pos, 10_000)
    assert segs2 == [] and pos2 == pos


def test_completed_oversized_past_scan_limit_respects_bound_end(tmp_path):
    """Skip a completed oversized line inside the reservation; do not drink past it."""
    p = tmp_path / "bound-scan.evidence.jsonl"
    prefix = (b'{"schema_version": 1, "event_id": "e", "session_id": "s", "turn_id": "t", '
              b'"class": "USER", "text": "')
    suffix = b'", "timestamp": "2026-08-27T00:00:00Z"}\n'
    text_len = 2 * SCAN_LIMIT - len(prefix) - len(suffix) + 50_000
    huge = prefix + (b"A" * text_len) + suffix
    in_bound = (json.dumps(_event("USER", "inside", event_id="in")) + "\n").encode()
    outside = (json.dumps(_event("USER", "outside", event_id="out")) + "\n").encode()
    with open(p, "wb") as f:
        f.write(huge)
        f.write(in_bound)
        f.write(outside)
    bound_end = len(huge) + len(in_bound)
    src = EvidenceJsonlSource()
    segs, end = _sip_past_huge_prefix(src, str(p), 0, 10_000, bound_end=bound_end)
    assert len(segs) == 1 and segs[0].text == "inside"
    assert end == bound_end
    segs2, end2 = src.sip(str(p), bound_end, 10_000)
    assert len(segs2) == 1 and segs2[0].text == "outside"
    assert end2 == p.stat().st_size


def test_discard_scan_state_resets_on_truncation(tmp_path):
    """Truncation/replacement clears cached scan cursors for that inode."""
    p = tmp_path / "trunc.evidence.jsonl"
    old_size = SCAN_LIMIT + 5000
    p.write_bytes(b"x" * old_size)
    src = EvidenceJsonlSource()
    _, pos1 = src.sip(str(p), 0, 10_000)
    assert pos1 == 0
    st = p.stat()
    stale_key = EvidenceJsonlSource._scan_key(str(p), 0, st.st_dev, st.st_ino)
    with src._scan_lock:
        assert stale_key in src._scan_cursors
    good = (json.dumps(_event("USER", "fresh", event_id="f")) + "\n").encode()
    p.write_bytes(good)
    segs, end = src.sip(str(p), 0, 10_000)
    with src._scan_lock:
        assert stale_key not in src._scan_cursors
    assert len(segs) == 1 and segs[0].text == "fresh"
    assert end == len(good)


def test_progressive_cursor_survives_append_growth(tmp_path, monkeypatch):
    """Append growth must not invalidate the unreserved progressive scan cursor."""
    p = tmp_path / "grow.evidence.jsonl"
    p.write_bytes(b"x" * (SCAN_LIMIT + 50_000))
    consumed = _track_readline_bytes(p, monkeypatch)
    src = EvidenceJsonlSource()
    _, pos1 = src.sip(str(p), 0, 10_000)
    assert pos1 == 0
    first_pass = consumed[-1]
    with open(p, "ab") as f:
        f.write(b"y" * 1000)
    _, pos2 = src.sip(str(p), 0, 10_000)
    assert pos2 == 0
    second_pass = consumed[-1]
    assert second_pass <= _PER_RESUME_BYTE_CAP
    assert first_pass > second_pass


def test_claim_append_one_byte_reserved_sip_returns_valid_evidence(tmp_path):
    """claim_bound past a huge prefix, append one byte, sip(bound_end) must not skip."""
    p = tmp_path / "claim-append.evidence.jsonl"
    huge = b"x" * (2 * SCAN_LIMIT + 50_000) + b"\n"
    good = (json.dumps(_event("USER", _blob("reserved"), event_id="ok")) + "\n").encode()
    with open(p, "wb") as f:
        f.write(huge)
        f.write(good)
    src = EvidenceJsonlSource()
    marks = Watermarks(str(tmp_path / "marks.json"))
    claimed = None
    for _ in range(30):
        result = marks.claim([str(p)], 4000, MIN_DRINK)
        if result is not None and not result.scan_pending:
            claimed = result
            break
        assert result is not None and result.scan_pending > 0
    assert claimed is not None
    path, start, reserved = claimed.path, claimed.start, claimed.end
    assert start == 0
    with open(p, "ab") as f:
        f.write(b"z")
    segs, sip_end = src.sip(path, start, 4000, bound_end=reserved)
    assert len(segs) == 1 and segs[0].text.startswith("reserved")
    assert sip_end == reserved == p.stat().st_size - 1
    assert marks.read()[src.key(str(p))] == reserved


def test_path_replacement_resets_scan_cursor(tmp_path):
    """Unlink/recreate at the same path must not inherit a stale progressive cursor."""
    p = tmp_path / "replace.evidence.jsonl"
    p.write_bytes(b"x" * (SCAN_LIMIT + 5000))
    src = EvidenceJsonlSource()
    src.sip(str(p), 0, 10_000)
    st_old = p.stat()
    key_old = EvidenceJsonlSource._scan_key(str(p), 0, st_old.st_dev, st_old.st_ino)
    with src._scan_lock:
        assert key_old in src._scan_cursors
    good = (json.dumps(_event("USER", "replaced", event_id="r")) + "\n").encode()
    p.unlink()
    p.write_bytes(good)
    segs, end = src.sip(str(p), 0, 10_000)
    assert len(segs) == 1 and segs[0].text == "replaced"
    assert end == len(good)
    with src._scan_lock:
        assert key_old not in src._scan_cursors


def test_same_inode_rewrite_resets_scan_cursor(tmp_path):
    """Same-inode content rewrite at line_start clears a saved progressive cursor."""
    p = tmp_path / "rewrite.evidence.jsonl"
    p.write_bytes(b"x" * (SCAN_LIMIT + 5000))
    src = EvidenceJsonlSource()
    src.sip(str(p), 0, 10_000)
    st = p.stat()
    key = EvidenceJsonlSource._scan_key(str(p), 0, st.st_dev, st.st_ino)
    with src._scan_lock:
        assert key in src._scan_cursors
    good = (json.dumps(_event("USER", "rewritten", event_id="w")) + "\n").encode()
    p.write_bytes(good)
    segs, end = src.sip(str(p), 0, 10_000)
    with src._scan_lock:
        assert key not in src._scan_cursors
    assert len(segs) == 1 and segs[0].text == "rewritten"
    assert end == len(good)


def test_same_size_same_head_rewrite_resets_scan_cursor(tmp_path):
    """Same inode, size, and head anchor: generation change must not reuse cursor."""
    p = tmp_path / "same-size.evidence.jsonl"
    old_size = SCAN_LIMIT + 5000
    p.write_bytes(b"x" * old_size)
    src = EvidenceJsonlSource()
    segs1, pos1 = src.sip(str(p), 0, 10_000)
    assert segs1 == [] and pos1 == 0
    st = p.stat()
    key = EvidenceJsonlSource._scan_key(str(p), 0, st.st_dev, st.st_ino)
    with src._scan_lock:
        assert key in src._scan_cursors
    good = (json.dumps(_event("USER", "found", event_id="f")) + "\n").encode()
    huge = b"x" * (old_size - len(good) - 1) + b"\n"
    assert len(huge) + len(good) == old_size
    assert huge[:16] == b"x" * 16
    p.write_bytes(huge + good)
    segs2, end = src.sip(str(p), 0, 10_000)
    with src._scan_lock:
        assert key not in src._scan_cursors
    assert len(segs2) == 1 and segs2[0].text == "found"
    assert end == old_size


def test_same_size_rewrite_resets_even_when_mtime_restored(tmp_path):
    """Restoring mtime cannot hide a same-size rewrite; ctime generation still resets."""
    p = tmp_path / "mtime-restore.evidence.jsonl"
    old_size = SCAN_LIMIT + 5000
    p.write_bytes(b"x" * old_size)
    src = EvidenceJsonlSource()
    src.sip(str(p), 0, 10_000)
    old_st = p.stat()
    good = (json.dumps(_event("USER", "ctime", event_id="c")) + "\n").encode()
    huge = b"x" * (old_size - len(good) - 1) + b"\n"
    p.write_bytes(huge + good)
    os.utime(p, ns=(old_st.st_atime_ns, old_st.st_mtime_ns))
    assert p.stat().st_mtime_ns == old_st.st_mtime_ns
    assert p.stat().st_ctime_ns != old_st.st_ctime_ns
    segs, end = src.sip(str(p), 0, 10_000)
    assert len(segs) == 1 and segs[0].text == "ctime"
    assert end == old_size


def test_append_growth_near_cursor_mutation_resets_scan_cursor(tmp_path, monkeypatch):
    """Growth that mutates bytes near the saved cursor must not reuse it."""
    p = tmp_path / "mutate.evidence.jsonl"
    p.write_bytes(b"x" * (SCAN_LIMIT + 50_000))
    consumed = _track_readline_bytes(p, monkeypatch)
    src = EvidenceJsonlSource()
    src.sip(str(p), 0, 10_000)
    first_pass = consumed[-1]
    with src._scan_lock:
        entry = next(iter(src._scan_cursors.values()))
        cursor_pos = entry.pos
    with open(p, "r+b") as f:
        f.seek(cursor_pos - 1)
        f.write(b"y")
        f.seek(0, 2)
        f.write(b"z" * 1000)
    _, pos = src.sip(str(p), 0, 10_000)
    assert pos == 0
    second_pass = consumed[-1]
    # Reset restarts detection + progressive scan; resume would read far less.
    assert second_pass >= first_pass - (MAX_LINE + 2)


def test_scan_state_is_instance_local(tmp_path):
    """Two instances do not share progressive scan cursors."""
    p = tmp_path / "local.evidence.jsonl"
    p.write_bytes(b"x" * (SCAN_LIMIT + 5000))
    a = EvidenceJsonlSource()
    b = EvidenceJsonlSource()
    a.sip(str(p), 0, 10_000)
    with a._scan_lock:
        assert a._scan_cursors
    with b._scan_lock:
        assert not b._scan_cursors


def test_scan_state_cap_evicts_without_breaking_reserved_sip(tmp_path):
    """Eviction restarts unreserved scans but reserved sips stay correct."""
    src = EvidenceJsonlSource()
    src._SCAN_STATE_CAP = 2
    paths = []
    for i in range(3):
        p = tmp_path / f"cap{i}.evidence.jsonl"
        p.write_bytes(b"x" * (SCAN_LIMIT + 1000))
        paths.append(p)
        src.sip(str(p), 0, 10_000)
    with src._scan_lock:
        assert len(src._scan_cursors) <= 2
    huge = b"x" * (2 * SCAN_LIMIT + 10_000) + b"\n"
    good = (json.dumps(_event("USER", "after-cap", event_id="c")) + "\n").encode()
    p = tmp_path / "reserved.evidence.jsonl"
    p.write_bytes(huge + good)
    bound_end = len(huge) + len(good)
    segs, end = src.sip(str(p), 0, 10_000, bound_end=bound_end)
    assert len(segs) == 1 and segs[0].text == "after-cap"
    assert end == bound_end


def _night_quiet_stamp_after_pass(last: int | None, stamp: int, result: dict) -> int | None:
    """The quiet-mtime stamp ``night()`` remembers after one bounded pass."""
    if result.get("scan_pending_bytes"):
        return last
    return stamp


def test_night_retries_same_mtime_after_scan_pending():
    """Scan-only progress must not rest the watcher for this quiet period."""
    stamp = 1_700_000_000
    last = None
    last = _night_quiet_stamp_after_pass(last, stamp, {"ok": True, "scan_pending_bytes": 50_000})
    assert last is None
    last = _night_quiet_stamp_after_pass(last, stamp, {"ok": True, "why": "nothing worth drinking"})
    assert last == stamp


class _NightLoopDone(Exception):
    pass


def test_distiller_night_pending_skips_drain(tmp_path, monkeypatch):
    """Pending pass must not drain or mark this quiet period done (no 600s rest)."""
    jr = tmp_path / "jr"
    jr.mkdir()
    p = jr / "j.evidence.jsonl"
    p.write_bytes(b"x\n")
    d, _ = _distiller(tmp_path, jr)
    stamp = 1_700_000_000
    monkeypatch.setattr(d, "files", lambda session=None: [str(p)])
    monkeypatch.setattr("os.path.getmtime", lambda _path: stamp)
    monkeypatch.setattr("time.time", lambda: stamp + 9999)
    run_results = [
        {"ok": True, "scan_pending_bytes": 50_000},
        {"ok": True, "why": "nothing worth drinking"},
    ]
    drain_calls: list[bool] = []
    sleep_calls: list[float] = []

    def fake_run(chunks=1):
        return run_results.pop(0)

    def fake_drain(n=None):
        drain_calls.append(True)
        return {"ok": True}

    loops = [0]

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        loops[0] += 1
        if loops[0] >= 3:
            raise _NightLoopDone()

    monkeypatch.setattr(d, "run", fake_run)
    monkeypatch.setattr(d, "drain", fake_drain)
    monkeypatch.setattr("time.sleep", fake_sleep)
    with pytest.raises(_NightLoopDone):
        d.night(idle_min=0.001, poll_s=1)
    assert drain_calls == [True]
    assert 600 not in sleep_calls


def test_invalid_only_run_does_not_call_spot_or_metrics(tmp_path, monkeypatch):
    """Invalid-only intake advances the watermark but must not wake the model."""
    jr = tmp_path / "jr"
    jr.mkdir()
    p = jr / "j.evidence.jsonl"
    lines = [json.dumps({"not": "schema", "n": i}) + "\n" for i in range(700)]
    p.write_text("".join(lines), encoding="utf-8")
    d, st = _distiller(tmp_path, jr, chunk_chars=4000)
    monkeypatch.setattr(d, "files", lambda session=None: [str(p)])
    monkeypatch.setattr("distill_kura.distill.pipeline.MIN_DRINK", 1)
    spot_calls: list[int] = []
    metric_calls: list[dict] = []
    monkeypatch.setattr(d, "spot", lambda segs: spot_calls.append(len(segs)) or [])
    monkeypatch.setattr(d, "_metric", lambda row: metric_calls.append(row))
    result = d.run(chunks=1)
    assert result == {"ok": True, "why": "nothing worth drinking"}
    assert spot_calls == []
    assert metric_calls == []
    assert d.marks.read()[EvidenceJsonlSource().key(str(p))] == p.stat().st_size


def test_invalid_only_chunk_returns_nothing_not_pending(tmp_path, monkeypatch):
    """700 invalid lines: watermark and intake move; run is not scan-pending."""
    jr = tmp_path / "jr"
    jr.mkdir()
    p = jr / "j.evidence.jsonl"
    lines = [json.dumps({"not": "schema", "n": i}) + "\n" for i in range(700)]
    p.write_text("".join(lines), encoding="utf-8")
    d, st = _distiller(tmp_path, jr, chunk_chars=4000)
    monkeypatch.setattr(d, "files", lambda session=None: [str(p)])
    monkeypatch.setattr("distill_kura.distill.pipeline.MIN_DRINK", 1)
    key = EvidenceJsonlSource().key(str(p))
    result = d.run(chunks=1)
    assert result == {"ok": True, "why": "nothing worth drinking"}
    assert d.marks.read()[key] == p.stat().st_size
    intake = os.path.join(st.still, "intake.jsonl")
    assert os.path.exists(intake)
    got = d.sip_one()
    assert got is None


def test_pipeline_run_reaches_valid_event_past_huge_oversized_line(tmp_path):
    """Distiller run(chunks=1) must scan, reserve, and drink without manual sip."""
    jr = tmp_path / "jr"
    jr.mkdir()
    p = jr / "j.evidence.jsonl"
    huge = b"x" * (2 * SCAN_LIMIT + 50_000) + b"\n"
    good = (json.dumps(_event("USER", "after", event_id="ok")) + "\n").encode()
    with open(p, "wb") as f:
        f.write(huge)
        f.write(good)
    singleton = SOURCES.get("evidence")
    if isinstance(singleton, EvidenceJsonlSource):
        with singleton._scan_lock:
            singleton._scan_cursors.clear()
    d, _ = _distiller(tmp_path, jr, chunk_chars=4000)
    key = EvidenceJsonlSource().key(str(p))
    file_size = p.stat().st_size
    saw_pending = False
    for _ in range(10):
        mark = d.marks.read().get(key, 0)
        result = d.run(chunks=1)
        if result.get("scan_pending_bytes"):
            saw_pending = True
            assert mark == 0
            assert d.marks.read().get(key, 0) == 0
            assert result["scan_pending_bytes"] > 0
            continue
        assert d.marks.read().get(key, 0) == file_size
        break
    else:
        pytest.fail("pipeline never finished the huge prefix")
    assert saw_pending
    mark = d.marks.read().get(key, 0)
    segs, end = EvidenceJsonlSource().sip(str(p), mark, 10_000)
    assert segs == [] and end == file_size
    assert _night_quiet_stamp_after_pass(None, int(p.stat().st_mtime),
                                         {"ok": True, "scan_pending_bytes": 50_000}) is None
