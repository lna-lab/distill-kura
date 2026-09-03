"""Fixes from the 2026-09 full-repo bug review.

Each test here fails on the code as it was before the fix it names. The failures
were found by review and (where a number could lie) confirmed by running the real
code first — the comments say what actually happened, not what might have.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from distill_kura import fastpath                                   # noqa: E402
from distill_kura.distill.gate import composed_number_violations    # noqa: E402
from distill_kura.distill.pipeline import Distiller                 # noqa: E402
from distill_kura.distill.sources import DshSource                  # noqa: E402
from distill_kura.fastpath import _cited                            # noqa: E402
from distill_kura.recall import recall                              # noqa: E402
from distill_kura.registry import Registry                          # noqa: E402
from distill_kura.server import _make_handler                       # noqa: E402
from distill_kura.store import Store                                # noqa: E402
from distill_kura.thinker import Models                             # noqa: E402
from distill_kura.weave import Cloth, Loom                          # noqa: E402


def _store(tmp_path, name="m", **kw) -> Store:
    s = Store(name=name, path=str(tmp_path / name), **kw)
    s.init_files()
    return s


def _registry(store: Store, raw=None) -> Registry:
    models = Models.from_config({"thinker": {"url": "http://127.0.0.1:9/v1", "model": "none"}})
    return Registry(stores={store.name: store}, modes={}, models=models,
                    default=store.name, raw=raw or {})


# ── the gate's numeric floor ────────────────────────────────────────────────

def test_a_comma_decimal_is_not_its_digit_concatenation():
    """Erasing every comma made "1,5" canonicalise to "15": a claimed 1,5 GB passed
    on evidence that said 15 GB. Two different numbers, one vouching for the other."""
    ev = [{"class": "TOOL", "text": "df showed 15 GB free"}]
    assert composed_number_violations("the disk had 1,5 GB free", ev) == ["1,5"]
    assert composed_number_violations("cost 12,34 yen",
                                      [{"class": "TOOL", "text": "total 1234 yen"}]) == ["12,34"]


def test_a_thousands_separator_is_still_forgiven():
    ev = [{"class": "TOOL", "text": "indexed 1,234,567 lines"}]
    assert composed_number_violations("1,234,567 lines indexed", ev) == []
    assert composed_number_violations("1234567 lines indexed", ev) == []


def test_exponent_case_is_one_magnitude_not_two_claims():
    ev = [{"class": "TOOL", "text": "rate 1.23E-4 measured"}]
    assert composed_number_violations("the rate was 1.23e-4", ev) == []


# ── the watermark ───────────────────────────────────────────────────────────

def test_dsh_claim_reserves_exactly_what_sip_reads(tmp_path, monkeypatch):
    """claim() reserved 2.2× the budget while sip() read 1×: every chunk's unread
    tail was marked drunk and skipped forever (two thirds of a simulated journal,
    measured). The reserve and the read must agree event for event."""
    events, seq = [], 0
    for i in range(400):
        seq += 1
        if i % 10 == 0:
            events.append({"seq": seq, "type": "system/ping"})   # unclassified noise
        else:
            events.append({"seq": seq, "type": "user/message",
                           "data": {"source": {"kind": "user"},
                                    "content": [{"type": "text",
                                                 "text": f"note {i} about the garage door"}]}})
    src = DshSource()
    monkeypatch.setattr(DshSource, "_lines", staticmethod(lambda path: iter(events)))
    path = str(tmp_path / "session.jsonl.zstd")

    from distill_kura.distill.watermark import Watermarks
    wm = Watermarks(str(tmp_path / "marks" / "marks.json"))
    consumed, start, budget, rounds = set(), 0, 2000, 0
    while rounds < 100:
        c = wm.claim([path], budget, 1)
        if not c:
            break
        claimed_start, s = c.start, c.source
        assert claimed_start == start
        segs, nxt = s.sip(path, start, budget)
        rounds += 1
        # which classified seqs did this sip actually see?
        total = 0
        for d in events:
            q = d.get("seq")
            if q is None or q <= start:
                continue
            seg = s._classify(d)
            if seg:
                consumed.add(q)
                total += len(seg.text[:2000 if seg.cls == "TOOL" else 4000])
                if total >= budget:
                    break
        wm.advance(s.key(path), nxt)
        start = wm.read()[s.key(path)]
    classified = [d["seq"] for d in events if src._classify(d)]
    missing = [q for q in classified if q not in consumed]
    assert not missing, f"{len(missing)} of {len(classified)} events never distilled (e.g. {missing[:6]})"


# ── drain: the judge is not an oracle about infrastructure ─────────────────

def _stage_one_draft(tmp_path):
    s = _store(tmp_path, write_policy="distiller-only")
    reg = _registry(s)
    d = Distiller(reg, s)
    j = tmp_path / "j.jsonl"
    j.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": [{"type": "text",
               "text": "put the archive on the slow disk"}]}}),
        json.dumps({"type": "user", "message": {"content": [{"type": "text",
               "text": "padding " * 2000}]}})]) + "\n", encoding="utf-8")
    d._current_source = str(j)
    d.scribe = lambda task, u, max_tokens=0: (                    # type: ignore[method-assign]
        "SLUG: archive\nTITLE: Archive\nDESC: the archive lives on the slow disk\n"
        "BODY:\nThe archive goes on the slow disk.\n")
    composed = d.compose({"topic": "archive", "kind": "project", "why": "disks",
                          "evidence": [{"class": "USER", "text": "put the archive on the slow disk"}],
                          "classes": ["USER"]})
    assert composed, "fixture: the draft must stage for the drain tests to mean anything"
    d.stage(composed, str(j))
    return s, d


def test_an_unreachable_scribe_leaves_the_draft_staged(tmp_path):
    """ask() returns None for unreachable/timeout/empty; scribe() used to collapse
    that to "", the judge read "" as "did not keep the shape", and drain DELETED a
    gate-passed draft. A quiet outage emptied the whole queue."""
    s, d = _stage_one_draft(tmp_path)
    d.scribe = lambda task, u, max_tokens=0: None                 # type: ignore[method-assign]
    r = d.drain()
    assert r["poured"] == 0 and r["tossed"] == 0 and r["skipped"] == 1
    assert os.path.exists(os.path.join(d.drafts_dir, "archive.md")), "the draft was destroyed"
    assert not os.path.exists(os.path.join(s.still, "tossed.jsonl"))
    assert not s.slugs(), "nothing may enter the store while the judge is down"


def test_a_fix_without_a_body_section_is_not_a_pour(tmp_path):
    """A FIX verdict means "part of this goes beyond the evidence". When the BODY:
    section failed to parse, drain fell through to pour() and filed exactly the
    text the judge had just condemned."""
    s, d = _stage_one_draft(tmp_path)
    d.scribe = lambda task, u, max_tokens=0: "FIX\nreason: cut the overreach"   # type: ignore[method-assign]
    r = d.drain()
    assert r["poured"] == 0 and r["fix_unparsed"] == 1
    assert os.path.exists(os.path.join(d.drafts_dir, "archive.md")), "left staged, not poured"
    assert not s.slugs()


# ── the extension path stands on the same floor ─────────────────────────────

def test_an_extensions_candidate_sentences_wear_the_number_floor(tmp_path):
    """The new-memory path floors the candidate's belongs_because/keep/may_fade;
    the extension path floored only the scribe's, so an unbacked number in the
    CANDIDATE's sentence reached the memory under the curation mark."""
    import time
    s = _store(tmp_path, write_policy="distiller-only")
    s.pour_verified("x", "the slow disk", "The archive goes on the slow disk.")
    reg = _registry(s)
    d = Distiller(reg, s)
    src = tmp_path / "j.jsonl"; src.write_text("{}\n")
    t = time.mktime((2026, 8, 20, 12, 0, 0, 0, 0, -1)); os.utime(src, (t, t))
    d._current_source = str(src)
    d.scribe = lambda task, u, max_tokens=0: "SECTION: ## new\nBODY:\nnew fact\n"  # type: ignore[method-assign]
    base = {"extends": "x", "extends_why": "adds",
            "evidence": [{"class": "USER", "text": "move it tonight"}],
            "classes": ["USER"], "kind": "project"}
    bad = {**base, "belongs_because": "the 99-rack note lives here"}
    good = {**base, "belongs_because": "this store keeps storage decisions"}
    assert d._compose_extension(bad) is None, "an unbacked number walked in through the candidate"
    out = d._compose_extension(good)
    assert out is not None and "99" not in json.dumps(out)


# ── store: the write policy an operator set is the one they get ─────────────

def test_readonly_false_cannot_silently_thaw_a_frozen_store(tmp_path):
    """`readonly = false` used to overwrite a validated write_policy outright:
    frozen became direct-allowed, signalled by nothing. Tightening (readonly=True)
    keeps its documented meaning."""
    with pytest.raises(ValueError, match="frozen"):
        Store(name="m", path=str(tmp_path / "m"), write_policy="frozen", readonly=False)
    st = Store(name="m2", path=str(tmp_path / "m2"), write_policy="frozen", readonly=True)
    assert st.write_policy == "distiller-only"        # the documented tightening


def test_an_empty_description_is_refused_not_a_broken_index_line(tmp_path):
    """remember_direct with description='' wrote `- [](x.md) — ` into MEMORY.md —
    a line no later write can match, so the rot persisted until hand-edited."""
    s = _store(tmp_path)
    r = s.remember_direct("x", "", "the body")
    assert r["ok"] is False and "description" in r["error"]
    assert "[](x.md)" not in s.index_text()
    assert not s.slugs()


def test_a_heartbeat_that_parses_but_lies_does_not_take_down_doctor(tmp_path):
    """tend.json valid-as-JSON but wrong-shaped (a list; "at": "recently") used to
    raise out of tend_state() and take doctor() with it."""
    s = _store(tmp_path)
    os.makedirs(s.still, exist_ok=True)
    hp = os.path.join(s.still, "tend.json")
    for junk in ('[1, 2, 3]', '{"at": "recently", "pid": "me"}', '"a string"'):
        with open(hp, "w", encoding="utf-8") as f:
            f.write(junk)
        st = s.tend_state()
        assert st["alive"] is False and st["why"] == "heartbeat unreadable", junk
        s.doctor()                                   # must not raise


def test_annotating_a_memory_with_unreadable_tags_refuses_and_keeps_them(tmp_path):
    """tags() deliberately hides an unreadable tags line; writing that () back
    through _annotate ERASED the tags. An unreadable line is a refusal now."""
    s = _store(tmp_path)
    s.remember_direct("x", "a trigger line worth keeping", "body")
    f = s.file_of("x")
    text = open(f, encoding="utf-8").read()
    with open(f, "w", encoding="utf-8") as fh:        # a hand-broken tags line
        fh.write(text.replace("tags:", "tags: Not Kebab").replace(
            "---\n", "---\ntags: Not Kebab\n", 1) if "tags:" not in text else
            text.replace("tags: []", "tags: Not Kebab"))
    before = open(f, encoding="utf-8").read()
    r = s.annotate_direct("x", tags=["decision"])
    assert r["ok"] is False and "unreadable" in r["error"]
    assert open(f, encoding="utf-8").read() == before, "the broken line must survive verbatim"


# ── registry: [models] gets the same loud load as every other table ─────────

def _cfg(tmp_path, body: str) -> str:
    p = tmp_path / "kura.toml"
    p.write_text(body, encoding="utf-8")
    return str(p)


BASE_STORE = '[stores.m]\npath = "{p}"\n'


def test_models_table_unknown_role_key_and_type_are_refused_at_load(tmp_path):
    p = str(tmp_path / "m").replace("\\", "\\\\")
    good = _cfg(tmp_path, f'[server]\ndefault = "m"\n{BASE_STORE.format(p=p)}\n'
                          f'[models.thinker]\nurl = "http://127.0.0.1:9/v1"\n')
    assert Registry.load(good).port == 8085
    bad_cfgs = [
        f'[models.thinkre]\nurl = "http://x/v1"\n',                 # typo'd role: was silent
        f'[models.thinker]\napi_key_ev = "KEY"\n',                  # typo'd key: was silent
        f'[models.thinker]\ntimeout = "120"\n',                     # wrong type: crashed at call
        f'[models.thinker]\ndialect = "openai-compatible"\n',       # unknown dialect
        '[server]\nport = 8085.9\n',                                # was silently truncated
        '[server]\nport = true\n',                                  # was silently 1
    ]
    for extra in bad_cfgs:
        with pytest.raises(ValueError):
            Registry.load(_cfg(tmp_path, f'[server]\ndefault = "m"\n'
                                         f'{BASE_STORE.format(p=p)}\n' + extra)), extra


def test_a_string_port_that_is_a_clean_integer_is_accepted(tmp_path):
    p = str(tmp_path / "m").replace("\\", "\\\\")
    reg = Registry.load(_cfg(tmp_path, f'[server]\nport = "8090"\ndefault = "m"\n'
                                       f'{BASE_STORE.format(p=p)}\n'))
    assert reg.port == 8090


# ── the server answers 400, not a dropped connection ────────────────────────

def _serve(tmp_path):
    s = _store(tmp_path)
    reg = _registry(s)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(reg))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_malformed_numbers_get_a_400_the_client_can_read(tmp_path):
    """?window=big, {"hops": "one"} and {"question": null} used to escape as
    ValueError/TypeError: the connection dropped with no HTTP reply at all."""
    srv, base = _serve(tmp_path)
    try:
        for method, url, body in [
            ("GET", f"{base}/prefill?window=big", None),
            ("GET", f"{base}/prefill?fraction=lots", None),
            ("POST", f"{base}/recall", json.dumps({"question": "q", "hops": "one"}).encode()),
            ("POST", f"{base}/recall", json.dumps({"question": None}).encode()),
        ]:
            req = urllib.request.Request(url, data=body, method=method,
                                         headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=10)
                raise AssertionError(f"{url} {body}: expected 400")
            except urllib.error.HTTPError as e:
                assert e.code == 400, (url, body, e.code)
                assert "invalid argument" in json.load(e).get("error", "")
    finally:
        srv.shutdown()


def test_a_garbage_content_length_gets_a_400_not_a_dropped_socket(tmp_path):
    srv, base = _serve(tmp_path)
    try:
        raw = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=10)
        raw.sendall(b"POST /recall HTTP/1.1\r\nHost: x\r\nContent-Length: 12abc\r\n\r\n")
        data = raw.recv(4096)
        raw.close()
        assert b" 400 " in data.split(b"\r\n")[0], data[:60]     # a reply, not a reset
    finally:
        srv.shutdown()


# ── tier zero: a short slug is not a citation ────────────────────────────────

def test_a_two_char_slug_does_not_hit_from_inside_a_longer_word(tmp_path):
    """Slug "ai" scored a full name-head hit from "tr**ai**ning"; with no runner-up
    the margin gate was skipped and tier zero answered an unrelated memory with
    full confidence, thinker never consulted."""
    s = _store(tmp_path)
    s.remember_direct("ai", "a trigger about something else entirely", "body text")
    r = fastpath.lookup(s, "how does the training loop branch?")
    assert r["hits"] == [] and r["verdict"] == "no-confident-hit"


def test_a_slug_is_cited_only_as_a_whole_name():
    assert _cited("ssd-tier", "the ssd-tier setup")
    assert not _cited("ssd-tier", "the ssd-tier-mission plan"), \
        "the question named a longer, different slug"
    assert not _cited("ai", "training")
    assert not _cited("mac", "machine")
    assert not _cited("gpt", "what about gpt-4?"), \
        "a hyphen continues the name: gpt-4 is not the slug gpt"
    assert _cited("gpt", "is gpt running?")


def test_a_long_slug_still_fastpaths_when_the_question_names_it(tmp_path):
    s = _store(tmp_path)
    s.remember_direct("ssd-tier-mission", "run the 2.6T model from the ssd tier",
                      "body text about the mission")
    r = fastpath.lookup(s, "what about the ssd-tier-mission plan?")
    assert r["verdict"] == "ok" and r["hits"][0]["slug"] == "ssd-tier-mission"


# ── recall's fallback answers inside the ceiling it was given ────────────────

def test_the_truncated_fallback_respects_total_chars_with_a_long_label(tmp_path):
    """The fallback reserved a fixed 60 chars for a header it never measured:
    label + slug > 37 walked past the documented hard ceiling."""
    s = Store(name="m", path=str(tmp_path / "m"),
              label="YUKI's very long EQ dialogue room name")
    s.init_files()
    long_slug = "ssd-inference-chip-performance-notes"
    s.remember_direct(long_slug, "the trigger for a rather long memory name",
                      "word " * 2000)
    d = recall(s, None, "chips on the ssd", chars=6000, total_chars=200)
    assert d["context"], "the fallback must answer, not go silent"
    assert len(d["context"]) <= 200, len(d["context"])


# ── the loom does not write a cloth it cannot check ──────────────────────────

def test_a_cloth_without_source_provenance_is_refused(tmp_path):
    """persist() skipped the compare-and-swap entirely when a Cloth carried no
    source hash/revision — no in-repo caller did that, but the method promised
    the check unconditionally."""
    s = _store(tmp_path)
    s.remember_direct("x", "trigger", "body")
    loom = Loom(s, scribe=None)
    stats = loom.persist(Cloth("a hand-built cloth\n"))
    assert stats["written"] is False and "provenance" in stats["refused"]
    assert loom.cloth_on_disk() is None
