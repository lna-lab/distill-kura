"""A model that did not answer must not cost us the water.

The watermark reserves a stretch of journal before the batch is read and never goes
backwards — that is what keeps two distillers apart. So the only thing between a failed
brain call and journal no one will ever read again is the shelf: the segments, kept as
sipped, worked off before any new water. These tests hold that line, and hold the two
answers apart — an explicit `[]` is a verdict, everything else is a failure.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from distill_kura import cli                                   # noqa: E402
from distill_kura.distill import Distiller                     # noqa: E402
from distill_kura.distill.pending import MAX_ATTEMPTS, failure_kind   # noqa: E402
from distill_kura.registry import Registry                     # noqa: E402
from distill_kura.store import Store                           # noqa: E402
from distill_kura.thinker import Endpoint, Models              # noqa: E402

SPOT_PHRASE = "deserves to become a permanent memory"
OVERFLOW = ("HTTP 400 (vllm body): This model's maximum context length is 32768 tokens, "
            "however you requested 41000")
DOWN = "unreachable: URLError: <urlopen error [Errno 111] Connection refused>"

CANDIDATE = json.dumps([{"topic": "archive-on-slow-disk", "kind": "project",
                         "why": "where the archive lives",
                         "quotes": ["[USER] put the archive on the slow disk",
                                    "[TOOL] /data 3.2T used 1.1T avail"]}])

SCRIBE = ("SLUG: archive-on-slow-disk\nTITLE: Archive on the slow disk\n"
          "DESC: the archive lives on the slow disk\n"
          "BODY:\nThe archive goes on the slow disk.\n\n**Why:** endurance.\n")

LINES = [("user", "put the archive on the slow disk"),
         ("tool", "/data 3.2T used 1.1T avail"),
         ("self", "I think we should also mirror it")]


@dataclass
class FakeMouth(Endpoint):
    """An endpoint that answers a script instead of a server.

    `spot` decides every batch-reading call and is handed the evidence, so a test can
    fail on size (the way a context overflow does) rather than on a call count. An
    answer is a string; `("fail", detail)` is the endpoint's own way of failing (None,
    with the reason in `last_error`); an Exception is raised, which is what a client
    library does when the socket dies mid-read."""
    spot: object = None
    other: dict = field(default_factory=dict)
    seen: list = field(default_factory=list)

    def ask(self, system, user, max_tokens=400, timeout=None, temperature=None):
        if self.spot is not None and SPOT_PHRASE in system:
            self.seen.append(user)
            answer = self.spot(user, len(self.seen))
        else:
            answer = next((v for k, v in self.other.items() if k in system), "")
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, tuple):
            self.last_error = answer[1]
            return None
        self.last_error = ""
        return answer


def journal(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for who, text in lines:
            if who == "user":
                f.write(json.dumps({"type": "user", "message": {"content": [
                    {"type": "text", "text": text}]}}) + "\n")
            elif who == "tool":
                f.write(json.dumps({"type": "user", "message": {"content": [
                    {"type": "tool_result", "content": [{"type": "text", "text": text}]}]}}) + "\n")
            else:
                f.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "text", "text": text}]}}) + "\n")
        f.write(json.dumps({"type": "user", "message": {"content": [
            {"type": "text", "text": "padding " * 2000}]}}) + "\n")   # past the min-drink bar


def build(tmp_path):
    store = Store(name="main", path=str(tmp_path / "kura"), label="k")
    store.init_files()
    models = Models.from_config({"thinker": {"url": "http://127.0.0.1:9/v1", "model": "none"}})
    reg = Registry(stores={"main": store}, modes={}, models=models, default="main",
                   raw={"distill": {"journals": {"claude": str(tmp_path / "journals")},
                                    "language": "English"}})
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    return reg, store


def distiller(tmp_path, spot, scribe=SCRIBE):
    """A distiller whose brain answers `spot` and whose scribe answers one thing."""
    reg, store = build(tmp_path)
    d = Distiller(reg, store)
    d.models.brain = FakeMouth(url="http://fake", model="fake", name="brain", spot=spot,
                               other={"actually NEW": "NEW\nnothing close"})
    d.models.scribe = FakeMouth(url="http://fake", model="fake", name="scribe",
                                other={"": scribe} if isinstance(scribe, str) else scribe)
    return d


def shelf(d):
    return [rec for _, rec in d.pending.load()]


def segments_of(user: str) -> int:
    return len([l for l in user.splitlines() if l.startswith("[")])


# ── the batch the brain never read ───────────────────────────────────────

def test_a_brain_that_did_not_answer_keeps_the_batch_instead_of_losing_it(tmp_path):
    d = distiller(tmp_path, lambda user, n: ("fail", DOWN))
    r = d.run(chunks=1)
    assert r["ok"] is False and r["why"] == "brain error" and r["reason"] == "transient"
    assert os.path.exists(r["pending_file"])
    rec = shelf(d)[0]
    assert rec["segments"] and rec["retryable"] is True
    # the water itself is on the shelf, not just a note that something went wrong
    assert any("slow disk" in s["text"] for s in rec["segments"])


def test_the_shelved_batch_is_drunk_on_the_next_pass_and_not_re_sipped(tmp_path):
    d = distiller(tmp_path, lambda user, n: ("fail", DOWN) if n == 1 else CANDIDATE)
    assert d.run(chunks=1)["ok"] is False
    mark = d.marks.read()
    r = d.run(chunks=1)                       # the mouth is back
    assert r["ok"] and r["drafts"] == ["archive-on-slow-disk"]
    assert shelf(d) == [] and r["pending"] == 0
    assert d.marks.read() == mark             # no journal was read for it a second time
    assert os.path.exists(os.path.join(d.drafts_dir, "archive-on-slow-disk.md"))


def test_an_explicit_empty_list_is_nothing_worth_drinking_not_a_failure(tmp_path):
    d = distiller(tmp_path, lambda user, n: "[]")
    assert d.run(chunks=1) == {"ok": True, "why": "nothing worth drinking"}
    assert shelf(d) == []


def test_a_reply_that_is_neither_a_list_nor_an_error_is_kept_as_a_bad_reply(tmp_path):
    d = distiller(tmp_path, lambda user, n: "I am sorry, I cannot help with that.")
    r = d.run(chunks=1)
    assert r["ok"] is False and r["reason"] == "bad_reply"
    assert shelf(d)[0]["segments"]


def test_a_mouth_that_raises_is_a_failure_not_a_verdict(tmp_path):
    d = distiller(tmp_path, lambda user, n: OSError("socket died"))
    r = d.run(chunks=1)
    assert r["ok"] is False and shelf(d)[0]["retryable"] is True


# ── the batch that does not fit ──────────────────────────────────────────

def test_a_batch_too_big_for_the_brain_is_split_on_segment_boundaries(tmp_path):
    """Retrying an overflow unchanged is the one retry guaranteed to fail again."""
    def spot(user, n):
        if segments_of(user) > 2:
            return ("fail", OVERFLOW)
        return CANDIDATE if "slow disk" in user else "[]"

    d = distiller(tmp_path, spot)
    assert d.run(chunks=1)["ok"] is False
    sipped = segments_of(d.models.brain.seen[0])
    r = d.run(chunks=1)
    assert r["ok"] and r["drafts"] == ["archive-on-slow-disk"]
    assert shelf(d) == []                       # both halves were drunk, nothing owed
    assert segments_of(d.models.brain.seen[1]) == sipped   # one more try at full size
    halves = [segments_of(u) for u in d.models.brain.seen[2:]]
    assert halves == [sipped // 2, sipped - sipped // 2]   # then the two halves, whole


def test_one_segment_that_still_overflows_stays_pending_and_says_reduce_chunk(tmp_path):
    d = distiller(tmp_path, lambda user, n: ("fail", OVERFLOW))
    assert d.run(chunks=1)["ok"] is False
    r = d.run(chunks=1)
    left = shelf(d)
    assert r["ok"] is False or r["pending"] == len(left)
    assert left and all(len(rec["segments"]) == 1 for rec in left)
    assert all(rec["retryable"] is False and rec["reason"] == "reduce chunk" for rec in left)
    # refused by hand, never dropped: the text is still there to be read
    assert any("slow disk" in rec["segments"][0]["text"] for rec in left)


def test_the_wording_of_an_overflow_is_told_from_a_mouth_that_is_down():
    assert failure_kind(OVERFLOW) == "context_overflow"
    assert failure_kind("HTTP 400 (vllm body): AssertionError: pages > cache") == "context_overflow"
    assert failure_kind("llama.cpp: the prompt exceeds the available context") == "context_overflow"
    assert failure_kind(DOWN) == "transient"
    assert failure_kind("HTTP 503 (vllm body): upstream busy") == "transient"


# ── the batch that is retried, but not forever ───────────────────────────

def test_a_transient_failure_is_retried_a_bounded_number_of_times(tmp_path):
    d = distiller(tmp_path, lambda user, n: ("fail", DOWN))
    d.run(chunks=1)
    for _ in range(MAX_ATTEMPTS):
        d.run(chunks=1)
    rec = shelf(d)[0]
    assert rec["attempt"] > MAX_ATTEMPTS and rec["retryable"] is False
    assert rec["segments"]                       # kept, never dropped
    tries = len(d.models.brain.seen)
    d.run(chunks=1)
    assert len(d.models.brain.seen) == tries     # a shelf that waits for a person is quiet


# ── the scribe: same rule, one candidate at a time ───────────────────────

def test_a_scribe_that_did_not_answer_keeps_the_candidate_for_the_next_pass(tmp_path):
    d = distiller(tmp_path, lambda user, n: CANDIDATE,
                  scribe={"You write the final memory": ("fail", DOWN)})
    r = d.run(chunks=1)
    assert r["drafts"] == [] and r["pending"] == 1
    rec = d.pending_compose.load()[0][1]
    assert rec["candidate"]["topic"] == "archive-on-slow-disk"
    assert rec["candidate"]["evidence"]           # the evidence packet travels with it
    spots, mark = len(d.models.brain.seen), d.marks.read()
    d.models.scribe.other = {"": SCRIBE}
    r = d.run(chunks=1)
    assert r["drafts"] == ["archive-on-slow-disk"] and d.pending_compose.load() == []
    assert len(d.models.brain.seen) == spots      # composed again, never read again
    assert d.marks.read() == mark


def test_a_scribe_that_answered_badly_is_a_verdict_and_not_kept_forever(tmp_path):
    """"Did not keep the shape" is quality, not transport: shelving it would be a loop."""
    d = distiller(tmp_path, lambda user, n: CANDIDATE, scribe={"": "not a draft at all"})
    assert d.run(chunks=1) == {"ok": True, "why": "nothing worth drinking"}
    assert d.pending_compose.load() == []


# ── what a scheduler is told ─────────────────────────────────────────────

def test_a_pass_that_owes_water_exits_one_not_nothing_to_do(tmp_path):
    """rc 2 rests the track as "nothing to do"; rc 1 is a failure a scheduler retries.
    A dead brain used to look like an empty queue, and the shelf would never be read."""
    store = Store(name="m", path=str(tmp_path / "m"), label="m")
    store.init_files()
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    cfg = tmp_path / "kura.toml"
    cfg.write_text(f"""
default = "m"
[stores.m]
path = "{store.path}"
[distill]
journals = {{ claude = "{tmp_path / 'journals'}" }}
[models.thinker]
url = "http://127.0.0.1:9/v1"
model = "none"
""", encoding="utf-8")
    assert cli.main(["-c", str(cfg), "-s", "m", "distill", "run"]) == 1
    assert os.listdir(os.path.join(store.still, "pending"))
