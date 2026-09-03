"""Tags through the distiller: a model proposes, the evidence decides, the pour keeps.

The shape of the lie, for each claiming tag: `entrusted` on a memory the human never
asked to keep; `emotion-carried` with no word of the human's; `landmine` resting on
the agent's own prose; one of the forgetting words assigned by a model that is not
the forgetting pass. And `recurred` — proposed freely it would be a popularity
counter, so it is decided against a prior memory and written exactly once.
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from distill_kura.distill import Distiller, pipeline          # noqa: E402
from distill_kura.distill.gate import verify_tags             # noqa: E402
from distill_kura.registry import Registry                    # noqa: E402
from distill_kura.store import Store                          # noqa: E402
from distill_kura.thinker import Models                       # noqa: E402

U = {"class": "USER", "text": "put the archive on the slow disk, and remember that"}
U_PLAIN = {"class": "USER", "text": "put the archive on the slow disk"}
T = {"class": "TOOL", "text": "/data 3.2T used 1.1T avail"}
S = {"class": "SELF", "text": "I think we should also mirror it"}


# ── the deterministic check ──────────────────────────────────────────────

def test_entrusted_needs_the_human_to_have_asked():
    kept, basis, refused = verify_tags(["entrusted"], [U_PLAIN])
    assert kept == () and "entrusted" in refused
    kept, basis, refused = verify_tags(["entrusted"], [U])
    assert kept == ("entrusted",) and basis["entrusted"]["quote"] == U["text"]
    kept, _, refused = verify_tags(["entrusted"], [{"class": "USER", "text": "これは覚えておいてね"}])
    assert kept == ("entrusted",)


def test_emotion_carried_needs_a_user_quote():
    assert verify_tags(["emotion-carried"], [T, S])[0] == ()
    kept, basis, _ = verify_tags(["emotion-carried"], [U_PLAIN, S])
    assert kept == ("emotion-carried",) and basis["emotion-carried"]["class"] == "USER"


def test_landmine_and_formative_do_not_rest_on_the_agents_prose_alone():
    assert verify_tags(["landmine", "formative"], [S])[0] == ()
    assert verify_tags(["landmine", "formative"], [S, T])[0] == ("formative",)   # a quiet df line
    T_FAIL = {"class": "TOOL", "text": "docker: Error response: OOM killed"}
    assert verify_tags(["landmine", "formative"], [S, T_FAIL])[0] == ("formative", "landmine")


def test_the_forgetting_words_are_refused_from_a_model():
    kept, _, refused = verify_tags(["expired", "released", "decision"], [U, T])
    assert kept == ("decision",)
    assert set(refused) == {"expired", "released"}


def test_recurred_is_not_a_models_to_propose():
    kept, _, refused = verify_tags(["recurred"], [U])
    assert kept == () and "recurred" in refused
    assert verify_tags(["recurred"], [U], recurred_ok=True)[0] == ("recurred",)


def test_an_unreadable_tag_list_refuses_the_whole_list_and_says_so():
    kept, _, refused = verify_tags(["decision", "Not Kebab"], [U])
    assert kept == () and "Not Kebab" in refused["*"]


def test_plain_curation_words_pass_as_proposed():
    assert verify_tags(["hypothesis", "my-own-word"], [S])[0] == ("hypothesis", "my-own-word")


# ── through the pipeline ─────────────────────────────────────────────────

def journal(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for who, text in lines:
            if who == "user":
                f.write(json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": text}]}}) + "\n")
            elif who == "tool":
                f.write(json.dumps({"type": "user", "message": {"content": [
                    {"type": "tool_result", "content": [{"type": "text", "text": text}]}]}}) + "\n")
            else:
                f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n")
        f.write(json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "padding " * 2000}]}}) + "\n")


def build(tmp_path, policy="distiller-only"):
    store = Store(name="main", path=str(tmp_path / "kura"), label="k", write_policy=policy)
    store.init_files()
    models = Models.from_config({"thinker": {"url": "http://127.0.0.1:9/v1", "model": "none"}})
    reg = Registry(stores={"main": store}, modes={}, models=models, default="main",
                   raw={"distill": {"journals": {"claude": str(tmp_path / "journals")},
                                    "language": "English"}})
    return reg, store


def script(dis: Distiller, answers: dict) -> None:
    """Answer each role by a phrase in its task prompt; no HTTP, no model."""
    def pick(task, user, max_tokens=0):
        return next((v for k, v in answers.items() if k in task), "")
    dis.brain = pick          # type: ignore[method-assign]
    dis.scribe = pick         # type: ignore[method-assign]
    # novelty/recall use the thinker over HTTP; a dead endpoint degrades to words
    # and that is fine here — what is under test is the tag plumbing.


SPOT = json.dumps([{"topic": "archive-on-slow-disk", "kind": "project",
                    "why": "where the archive lives",
                    "quotes": ["[USER] put the archive on the slow disk, and remember that",
                               "[TOOL] /data 3.2T used 1.1T avail"],
                    "tags": ["decision", "entrusted", "expired", "emotion-carried"],
                    "belongs_because": "this store holds storage decisions",
                    "keep": "which disk", "may_fade": "the df numbers"}])

SCRIBE = ("SLUG: archive-on-slow-disk\nTITLE: Archive on the slow disk\n"
          "DESC: the archive lives on the slow disk\n"
          "TAGS: [\"landmine\", \"decision\"]\n"
          "KEEP: the disk, not the numbers\n"
          "BODY:\nThe archive goes on the slow disk.\n\n**Why:** endurance.\n")

LINES = [("user", "put the archive on the slow disk, and remember that"),
         ("tool", "/data 3.2T used 1.1T avail"),
         ("self", "I think we should also mirror it")]


def test_tags_and_sentences_travel_from_candidate_to_store(tmp_path):
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    reg, store = build(tmp_path)
    d = Distiller(reg, store)
    script(d, {"deserves to become a permanent memory": SPOT, "actually NEW": "NEW\nnothing",
               "You write the final memory": SCRIBE, "draw the last line": "POUR\nreason: fine"})
    r = d.run(chunks=1)
    assert r["drafts"] == ["archive-on-slow-disk"]
    draft = open(os.path.join(d.drafts_dir, "archive-on-slow-disk.md"), encoding="utf-8").read()
    # the kept tags are in the SIGNED text; the refused ones are named in the header
    assert 'TAGS: ["decision", "emotion-carried", "entrusted"]' in draft
    assert "tags refused: expired" in draft and "landmine (needs an actual failure" in draft
    assert "KEEP: the disk, not the numbers" in draft             # the scribe's sentence wins
    assert "BELONGS_BECAUSE: this store holds storage decisions" in draft
    # the manifest says why each claiming tag exists, and why one does not
    ref = [l for l in draft.splitlines() if "evidence_manifest:" in l][0].split("sha256:")[1].strip()
    man = json.load(open(os.path.join(store.path, "_evidence", ref + ".json")))
    assert man["gate_version"] == 7
    assert man["tags"] == ["decision", "emotion-carried", "entrusted"]
    assert man["tag_evidence"]["entrusted"]["quote"].endswith("remember that")
    assert man["tags_refused"]["expired"] == "reserved for the forgetting pass; a model may not assign it"
    assert "landmine" in man["tags_refused"]
    assert man["annotations"]["keep"] == "the disk, not the numbers"
    # and the pour keeps all of it, with the body free of the header lines
    out = d.drain()
    assert out["poured"] == 1
    assert store.tags("archive-on-slow-disk") == ("decision", "emotion-carried", "entrusted")
    assert store.annotations("archive-on-slow-disk") == {
        "belongs_because": "this store holds storage decisions",
        "keep": "the disk, not the numbers", "may_fade": "the df numbers"}
    body = store.read_exact("archive-on-slow-disk")
    assert "TAGS:" not in body and "KEEP:" not in body.split("---")[2]
    assert store.doctor()["invalid_tags"] == {}


def test_a_scribe_that_kept_the_old_shape_still_yields_a_memory(tmp_path):
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    reg, store = build(tmp_path)
    d = Distiller(reg, store)
    old_spot = json.dumps([{"topic": "archive-on-slow-disk", "kind": "project", "why": "disks",
                            "quotes": ["[USER] put the archive on the slow disk"]}])
    old_scribe = ("SLUG: archive-on-slow-disk\nTITLE: Archive\nDESC: the slow disk\n"
                  "BODY:\nThe archive goes on the slow disk.\n")
    script(d, {"deserves to become a permanent memory": old_spot, "actually NEW": "NEW\n",
               "You write the final memory": old_scribe, "draw the last line": "POUR\nreason: ok"})
    d.run(chunks=1)
    assert d.drain()["poured"] == 1
    assert store.tags("archive-on-slow-disk") == ()
    assert store.annotations("archive-on-slow-disk") == {}
    assert "tags:" not in store.read_exact("archive-on-slow-disk")


def test_an_unreadable_tags_line_writes_the_memory_untagged_not_broken(tmp_path, capsys):
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    reg, store = build(tmp_path)
    d = Distiller(reg, store)
    bad_scribe = SCRIBE.replace('TAGS: ["landmine", "decision"]', "TAGS: landmine, Not A Tag")
    spot_plain = json.dumps([{"topic": "archive-on-slow-disk", "kind": "project", "why": "disks",
                              "quotes": ["[USER] put the archive on the slow disk"]}])
    script(d, {"deserves to become a permanent memory": spot_plain, "actually NEW": "NEW\n",
               "You write the final memory": bad_scribe, "draw the last line": "POUR\nreason: ok"})
    d.run(chunks=1)
    assert "TAGS line unreadable" in capsys.readouterr().out
    assert d.drain()["poured"] == 1
    assert store.tags("archive-on-slow-disk") == ()
    assert store.doctor()["invalid_tags"] == {}
    assert store.annotations("archive-on-slow-disk")["keep"] == "the disk, not the numbers"


def test_a_tag_edited_into_a_staged_draft_breaks_the_gate_mark(tmp_path):
    """Tags sit inside the signed text on purpose: a hand-added `entrusted` is an
    edit, and an edited draft does not pour."""
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    reg, store = build(tmp_path)
    d = Distiller(reg, store)
    script(d, {"deserves to become a permanent memory": SPOT, "actually NEW": "NEW\n",
               "You write the final memory": SCRIBE, "draw the last line": "POUR\nreason: ok"})
    d.run(chunks=1)
    p = os.path.join(d.drafts_dir, "archive-on-slow-disk.md")
    t = open(p, encoding="utf-8").read().replace('"entrusted"]', '"entrusted", "fulfilled"]')
    open(p, "w", encoding="utf-8").write(t)
    r = d.pour("archive-on-slow-disk")
    assert not r["ok"] and "gate mark" in r["why"]
    assert "archive-on-slow-disk" not in store.slugs()


def test_fix_keeps_every_header_line_not_just_the_first(tmp_path):
    """Regression: a FIX used to keep only the first header line, so DESC was lost and
    the memory poured with its slug as the index trigger."""
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    reg, store = build(tmp_path)
    d = Distiller(reg, store)
    script(d, {"deserves to become a permanent memory": SPOT, "actually NEW": "NEW\n",
               "You write the final memory": SCRIBE,
               "draw the last line": "FIX\nreason: drop the number\nBODY:\nThe archive goes on the slow disk.\n"})
    d.run(chunks=1)
    out = d.drain()
    assert out["fixed"] == 1 and out["poured"] == 1
    assert "- [Archive on the slow disk](archive-on-slow-disk.md) — the archive lives on the slow disk" \
        in store.index_text()
    assert store.tags("archive-on-slow-disk") == ("decision", "emotion-carried", "entrusted")
    assert store.read("archive-on-slow-disk").rstrip().endswith("The archive goes on the slow disk.")


# ── recurred: once, against a prior memory, from another occasion ────────

def _pour_first(tmp_path):
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    reg, store = build(tmp_path)
    d = Distiller(reg, store)
    spot = json.dumps([{"topic": "archive-on-slow-disk", "kind": "project", "why": "disks",
                        "quotes": ["[USER] put the archive on the slow disk"]}])
    script(d, {"deserves to become a permanent memory": spot, "actually NEW": "NEW\n",
               "You write the final memory": SCRIBE.replace("TAGS", "XTAGS"),
               "draw the last line": "POUR\nreason: ok"})
    d.run(chunks=1)
    assert d.drain()["poured"] == 1
    return reg, store


def _covered_pass(reg, store, journal_name, lines, tmp_path):
    journal(str(tmp_path / "journals" / journal_name), lines)
    d = Distiller(reg, store)
    spot = json.dumps([{"topic": "archive again", "kind": "project",
                        "why": "where the archive lives on the slow disk",
                        "quotes": [f"[{c.upper() if c != 'self' else 'SELF'}] {t}" for c, t in lines
                                   if c in ("user", "self")]}])
    script(d, {"deserves to become a permanent memory": spot,
               "actually NEW": "COVERED archive-on-slow-disk\nalready there"})
    return d.run(chunks=1)


def test_recurred_is_written_once_from_another_occasion_and_never_counted(tmp_path):
    reg, store = _pour_first(tmp_path)
    assert store.tags("archive-on-slow-disk") == ()
    # another session, the human's own words → one tag, with its own manifest
    r = _covered_pass(reg, store, "b.jsonl", [("user", "so the archive stays on the slow disk, right")], tmp_path)
    assert r["covered"] == 1 and r["recurred"] == 1
    assert store.tags("archive-on-slow-disk") == ("recurred",)
    ref = store.frontmatter("archive-on-slow-disk")["recurred_manifest"]
    man = json.load(open(os.path.join(store.path, "_evidence", ref[7:] + ".json")))
    assert man["recurrence_of"] == "archive-on-slow-disk" and man["tags"] == ["recurred"]
    assert man["tag_evidence"]["recurred"]["quote"].startswith("so the archive")
    before = open(store.file_of("archive-on-slow-disk"), "rb").read()
    # a third session: still one word, file untouched, no number anywhere
    r = _covered_pass(reg, store, "c.jsonl", [("user", "the archive on the slow disk again")], tmp_path)
    assert r["covered"] == 1 and r["recurred"] == 0
    assert open(store.file_of("archive-on-slow-disk"), "rb").read() == before
    assert store.tags("archive-on-slow-disk") == ("recurred",)
    fm = store.frontmatter("archive-on-slow-disk")
    assert not any("count" in k for k in fm)


def test_recurred_needs_the_humans_words_and_a_different_journal(tmp_path):
    reg, store = _pour_first(tmp_path)
    # the agent repeating itself is not the human returning to a topic
    journal(str(tmp_path / "journals" / "b.jsonl"), [("self", "as I said, the archive is on the slow disk")])
    d = Distiller(reg, store)
    spot = json.dumps([{"topic": "archive again", "kind": "feedback",
                        "why": "my judgement: where the archive lives on the slow disk",
                        "quotes": ["[SELF] as I said, the archive is on the slow disk"]}])
    script(d, {"deserves to become a permanent memory": spot,
               "actually NEW": "COVERED archive-on-slow-disk\nalready there"})
    r = d.run(chunks=1)
    assert r["covered"] == 1 and r["recurred"] == 0
    assert store.tags("archive-on-slow-disk") == ()
    log = open(os.path.join(store.still, "dropped.jsonl"), encoding="utf-8").read()
    assert "no [USER] quote" in log
    # the same journal the memory came from is not another occasion
    origin = d._origin_key("archive-on-slow-disk")
    assert origin and origin.endswith("a.jsonl")
    c = {"classes": ["USER"], "evidence": [{"class": "USER", "text": "the archive on the slow disk"}],
         "kind": "project"}
    why = d.recur(c, "archive-on-slow-disk", origin, str(tmp_path / "journals" / "a.jsonl"))
    assert "same journal" in why
    assert store.tags("archive-on-slow-disk") == ()


def test_recurred_leaves_a_memory_of_unknown_origin_alone_and_says_so(tmp_path):
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    reg, store = build(tmp_path, policy="direct-allowed")
    store.remember_direct("archive-on-slow-disk", "the slow disk", "hand-written, no manifest")
    r = _covered_pass(reg, store, "b.jsonl", [("user", "put the archive on the slow disk, and remember that")], tmp_path)
    assert r["covered"] == 1 and r["recurred"] == 0
    assert store.tags("archive-on-slow-disk") == ()
    log = open(os.path.join(store.still, "dropped.jsonl"), encoding="utf-8").read()
    assert "origin unknown" in log


def test_recurred_respects_a_frozen_store(tmp_path):
    reg, store = _pour_first(tmp_path)
    store.write_policy = "frozen"
    r = _covered_pass(reg, store, "b.jsonl", [("user", "the archive on the slow disk, again")], tmp_path)
    assert r["recurred"] == 0
    assert store.tags("archive-on-slow-disk") == ()


# ── ownership does not move ──────────────────────────────────────────────

def test_tags_never_move_a_memory_and_no_move_exists(tmp_path):
    """A Develop memory wearing an EQ-ish tag is still a Develop memory. And there is
    no door through which it could go anywhere else."""
    dev = Store(name="develop", path=str(tmp_path / "develop")); dev.init_files()
    eq = Store(name="eq", path=str(tmp_path / "eq")); eq.init_files()
    dev.remember_direct("black-screen", "d", "b", tags=["emotion-carried", "landmine"])
    assert dev.slugs() == ["black-screen"] and eq.slugs() == []
    assert dev.tags("black-screen") == ("emotion-carried", "landmine")
    for cls in (Store, Distiller, pipeline):
        for name in dir(cls):
            assert not any(w in name.lower() for w in ("move", "migrate", "copy_to", "transfer")), name


def test_a_fix_may_supply_the_missing_belongs_because(tmp_path):
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    reg, store = build(tmp_path)
    d = Distiller(reg, store)
    spot_plain = json.dumps([{"topic": "archive-on-slow-disk", "kind": "project", "why": "disks",
                              "quotes": ["[USER] put the archive on the slow disk"]}])
    script(d, {"deserves to become a permanent memory": spot_plain, "actually NEW": "NEW\n",
               "You write the final memory": SCRIBE,
               "draw the last line": ("FIX\nreason: it never said why it is here\n"
                                      "BELONGS_BECAUSE: this store keeps storage decisions\n"
                                      "BODY:\nThe archive goes on the slow disk.\n")})
    d.run(chunks=1)
    assert d.drain()["poured"] == 1
    assert store.annotations("archive-on-slow-disk")["belongs_because"] == "this store keeps storage decisions"
    assert store.tags("archive-on-slow-disk") == ("decision",)          # landmine: no failure in the evidence


def test_the_prompts_rank_by_charter_not_by_a_universal_list():
    """Emotion and recurrence are things not to miss, not reasons that outrank the
    store's purpose. The words that made them a universal ranking are gone."""
    from distill_kura.distill import prompts
    assert "WHAT IS WORTH KEEPING (in order)" not in prompts.SPOT_SYS
    assert "Emotion is what makes a fact stick" not in prompts.SPOT_SYS
    assert "the charter above" in prompts.SPOT_SYS and "belongs_because" in prompts.SPOT_SYS
    assert "charter" in prompts.COVERAGE_SYS and "charter" in prompts.POUR_SYS
    for line in ("TAGS:", "BELONGS_BECAUSE:", "KEEP:", "MAY_FADE:"):
        assert line in prompts.SCRIBE_SYS
    # no score vocabulary anywhere a model could pick it up
    for name in ("SPOT_SYS", "COVERAGE_SYS", "SCRIBE_SYS", "POUR_SYS", "EXTEND_SYS"):
        t = getattr(prompts, name).lower()
        for w in ("score", "salience", "priority_", "rank by", "more important than"):
            assert w not in t, (name, w)


# ── the door behind the last writer (round-three review) ────────────────────
#
# A judge's FIX rewrites the body last of all — and used to be re-signed without
# re-verification. Now the mark is a proof of having passed the floor.

def test_a_fix_that_invents_a_number_is_refused(tmp_path):
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    reg, store = build(tmp_path)
    d = Distiller(reg, store)
    script(d, {"deserves to become a permanent memory": SPOT, "actually NEW": "NEW\nnothing",
               "You write the final memory": SCRIBE,
               "draw the last line": "FIX\nreason: sharpen it\nBODY:\nreached 99 TPS on the slow disk"})
    r = d.run(chunks=1)
    assert r["drafts"] == ["archive-on-slow-disk"]
    before = open(os.path.join(d.drafts_dir, "archive-on-slow-disk.md"), encoding="utf-8").read()
    out = d.drain()
    after = open(os.path.join(d.drafts_dir, "archive-on-slow-disk.md"), encoding="utf-8").read()
    assert out["poured"] == 0 and out["fixed"] == 0 and out["left"] == 1
    assert after == before                       # not rewritten, not re-signed
    assert not store.read("archive-on-slow-disk")


def test_a_fix_that_stays_inside_the_evidence_pours(tmp_path):
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    reg, store = build(tmp_path)
    d = Distiller(reg, store)
    script(d, {"deserves to become a permanent memory": SPOT, "actually NEW": "NEW\nnothing",
               "You write the final memory": SCRIBE,
               "draw the last line": "FIX\nreason: tighten\nBODY:\nthe archive lives on the slow disk"})
    r = d.run(chunks=1)
    assert r["drafts"] == ["archive-on-slow-disk"]
    out = d.drain()
    assert out["fixed"] == 1 and out["poured"] == 1
    assert "slow disk" in store.read_exact("archive-on-slow-disk")


def _corrupt_manifest(store, slug):
    import glob as _g
    fm = store.frontmatter(slug) if store.read(slug) else {}
    ref = fm.get("evidence_manifest", "")
    if ref.startswith("sha256:"):
        p = os.path.join(store.path, "_evidence", ref[7:] + ".json")
    else:
        p = sorted(_g.glob(os.path.join(store.path, "_evidence", "*.json")))[0]
    with open(p, "a", encoding="utf-8") as f:
        f.write(" ")          # still valid JSON; the bytes no longer hash to the name
    return p


def test_a_fix_over_a_tampered_manifest_fails_closed(tmp_path):
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    reg, store = build(tmp_path)
    d = Distiller(reg, store)
    script(d, {"deserves to become a permanent memory": SPOT, "actually NEW": "NEW\nnothing",
               "You write the final memory": SCRIBE,
               "draw the last line": "FIX\nreason: tighten\nBODY:\nthe archive lives on the slow disk"})
    r = d.run(chunks=1)
    assert r["drafts"] == ["archive-on-slow-disk"]
    import glob as _g
    with open(sorted(_g.glob(os.path.join(store.path, "_evidence", "*.json")))[0], "a") as f:
        f.write(" ")
    out = d.drain()
    assert out["fixed"] == 0 and out["poured"] == 0 and out["left"] == 1


def test_doctor_reports_a_tampered_manifest(tmp_path):
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    reg, store = build(tmp_path)
    d = Distiller(reg, store)
    script(d, {"deserves to become a permanent memory": SPOT, "actually NEW": "NEW\nnothing",
               "You write the final memory": SCRIBE, "draw the last line": "POUR\nreason: fine"})
    d.run(chunks=1)
    assert d.drain()["poured"] == 1
    assert store.doctor()["tampered_manifest"] == []
    _corrupt_manifest(store, "archive-on-slow-disk")
    assert store.doctor()["tampered_manifest"] == ["archive-on-slow-disk"]


# ── round five: identity is signed too ──────────────────────────────────────

def test_a_renamed_draft_loses_its_mark(tmp_path):
    # A signed 12-gpu draft renamed to another slug used to pour under the new
    # identity — the mark signed the text but never the name.
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    reg, store = build(tmp_path)
    d = Distiller(reg, store)
    script(d, {"deserves to become a permanent memory": SPOT, "actually NEW": "NEW\nnothing",
               "You write the final memory": SCRIBE, "draw the last line": "POUR\nreason: fine"})
    r = d.run(chunks=1)
    assert r["drafts"] == ["archive-on-slow-disk"]
    os.rename(os.path.join(d.drafts_dir, "archive-on-slow-disk.md"),
              os.path.join(d.drafts_dir, "stolen-name.md"))
    out = d.pour("stolen-name")
    assert not out["ok"] and "gate mark" in out["why"]
    assert not store.read("stolen-name")


def test_a_slug_with_an_invented_number_is_refused(tmp_path):
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    reg, store = build(tmp_path)
    d = Distiller(reg, store)
    bad_scribe = ("SLUG: 99-gpu-archive\nTITLE: Archive disk\nDESC: the archive on the slow disk\n"
                  "BODY:\nthe archive lives on the slow disk\n")
    script(d, {"deserves to become a permanent memory": SPOT, "actually NEW": "NEW\nnothing",
               "You write the final memory": bad_scribe, "draw the last line": "POUR\nreason: fine"})
    r = d.run(chunks=1)
    assert not r.get("drafts")        # the slug is surface; the floor refused it


# ── round six: the mark signs the envelope, and the judge is not a mint ─────

def test_a_renamed_draft_cannot_be_laundered_through_fix(tmp_path):
    # v5 closed rename→POUR; the bypass was rename→FIX→re-sign→POUR.
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    reg, store = build(tmp_path)
    d = Distiller(reg, store)
    script(d, {"deserves to become a permanent memory": SPOT, "actually NEW": "NEW\nnothing",
               "You write the final memory": SCRIBE,
               "draw the last line": "FIX\nreason: tighten\nBODY:\nthe archive lives on the slow disk"})
    assert d.run(chunks=1)["drafts"] == ["archive-on-slow-disk"]
    os.rename(os.path.join(d.drafts_dir, "archive-on-slow-disk.md"),
              os.path.join(d.drafts_dir, "stolen-name.md"))
    out = d.drain()
    assert out["fixed"] == 0 and out["poured"] == 0 and out["quarantined"] == 1
    assert not store.read("stolen-name")
    import glob as _g
    assert _g.glob(os.path.join(store.path, "_still", "quarantine", "stolen-name*.md"))


def test_a_kind_flip_breaks_the_mark(tmp_path):
    # kind decides pinned status in the resident map — it is signed now.
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    reg, store = build(tmp_path)
    d = Distiller(reg, store)
    script(d, {"deserves to become a permanent memory": SPOT, "actually NEW": "NEW\nnothing",
               "You write the final memory": SCRIBE, "draw the last line": "POUR\nreason: fine"})
    assert d.run(chunks=1)["drafts"] == ["archive-on-slow-disk"]
    p = os.path.join(d.drafts_dir, "archive-on-slow-disk.md")
    raw = open(p, encoding="utf-8").read()
    open(p, "w", encoding="utf-8").write(raw.replace("kind: project", "kind: user", 1))
    out = d.pour("archive-on-slow-disk")
    assert not out["ok"] and "gate mark" in out["why"]


def test_a_manifest_pointer_swap_breaks_the_mark(tmp_path):
    # Swapping to a DIFFERENT validly-hashed manifest forged provenance; signed now.
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    reg, store = build(tmp_path)
    d = Distiller(reg, store)
    script(d, {"deserves to become a permanent memory": SPOT, "actually NEW": "NEW\nnothing",
               "You write the final memory": SCRIBE, "draw the last line": "POUR\nreason: fine"})
    assert d.run(chunks=1)["drafts"] == ["archive-on-slow-disk"]
    import hashlib as _hl
    blob = json.dumps({"quotes": [], "source_key": "somewhere:else"})
    other = _hl.sha256(blob.encode()).hexdigest()
    open(os.path.join(store.path, "_evidence", other + ".json"), "w").write(blob)
    p = os.path.join(d.drafts_dir, "archive-on-slow-disk.md")
    raw = open(p, encoding="utf-8").read()
    swapped = re.sub(r"evidence_manifest: sha256:[0-9a-f]{64}",
                     "evidence_manifest: sha256:" + other, raw, count=1)
    open(p, "w", encoding="utf-8").write(swapped)
    out = d.pour("archive-on-slow-disk")
    assert not out["ok"] and "gate mark" in out["why"]


def test_a_straight_pour_verifies_the_manifests_bytes(tmp_path):
    # The mark stays valid when the FILE is corrupted (the pointer is unchanged) —
    # so pour itself must re-hash the manifest before the memory exists.
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    reg, store = build(tmp_path)
    d = Distiller(reg, store)
    script(d, {"deserves to become a permanent memory": SPOT, "actually NEW": "NEW\nnothing",
               "You write the final memory": SCRIBE, "draw the last line": "POUR\nreason: fine"})
    assert d.run(chunks=1)["drafts"] == ["archive-on-slow-disk"]
    import glob as _g
    with open(sorted(_g.glob(os.path.join(store.path, "_evidence", "*.json")))[0], "a") as f:
        f.write(" ")
    out = d.pour("archive-on-slow-disk")
    assert not out["ok"] and "manifest" in out["why"]


def test_a_second_coverage_pass_drops_a_topic_the_first_already_took(tmp_path):
    """The audit pass is told what the first pass took. If the dedup or the cap broke,
    the same candidate would be composed twice — two drafts, two memories, one fact."""
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    reg, store = build(tmp_path)
    d = Distiller(reg, store)
    d.coverage_passes, d.max_items = 2, 4
    seen = {}
    more = json.dumps([{"topic": "archive-on-slow-disk", "kind": "project", "why": "again",
                        "quotes": ["[USER] put the archive on the slow disk"]},
                       {"topic": "mirror-hunch", "kind": "project", "why": "a mirror",
                        "quotes": ["[SELF] I think we should also mirror it"]}])

    def pick(task, user, max_tokens=0):
        if "already took the candidates" in task:
            seen["user"] = user
            return more
        return SPOT if "deserves to become a permanent memory" in task else ""
    d.brain = pick          # type: ignore[method-assign]
    segs, _, _ = d.sip_one()
    assert [c["topic"] for c in d.spot(segs)] == ["archive-on-slow-disk", "mirror-hunch"]
    assert "=== ALREADY TAKEN ===" in seen["user"] and "archive-on-slow-disk" in seen["user"]


def test_a_draft_edited_while_judged_gets_no_verdict(tmp_path):
    """The verdict binds the bytes the model read. A draft that changed under the
    judge must not be poured on the old verdict — and one that vanished must not
    raise, or a single removed file would end the drain for every other draft."""
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    reg, store = build(tmp_path)
    d = Distiller(reg, store)
    script(d, {"deserves to become a permanent memory": SPOT, "actually NEW": "NEW\nnothing",
               "You write the final memory": SCRIBE, "draw the last line": "POUR\nreason: fine"})
    assert d.run(chunks=1)["drafts"] == ["archive-on-slow-disk"]
    orig = d.judge_draft

    def racing(path):
        j = orig(path)
        open(path, "a", encoding="utf-8").write("\n")
        return j
    d.judge_draft = racing          # type: ignore[method-assign]
    out = d.drain()
    assert out["poured"] == 0 and out["tossed"] == 0 and out["left"] == 1
    assert store.read("archive-on-slow-disk") == ""

    def vanishing(path):
        j = orig(path)
        os.remove(path)
        return j
    d.judge_draft = vanishing       # type: ignore[method-assign]
    out = d.drain()
    assert out["poured"] == 0 and out["left"] == 0
    assert store.read("archive-on-slow-disk") == ""


def test_the_drafts_listing_says_slug_classes_and_trigger(tmp_path):
    """`kura drafts` reads this. It is a listing, not a gate: a hand-dropped draft is
    listed too — nothing about being listed says a draft may be poured."""
    from distill_kura.distill import drafts_of
    journal(str(tmp_path / "journals" / "a.jsonl"), LINES)
    reg, store = build(tmp_path)
    d = Distiller(reg, store)
    script(d, {"deserves to become a permanent memory": SPOT, "actually NEW": "NEW\nnothing",
               "You write the final memory": SCRIBE, "draw the last line": "POUR\nreason: fine"})
    assert d.run(chunks=1)["drafts"] == ["archive-on-slow-disk"]
    os.makedirs(d.drafts_dir, exist_ok=True)
    with open(os.path.join(d.drafts_dir, "by-hand.md"), "w", encoding="utf-8") as f:
        f.write("<!-- kind: project   evidence classes: SELF\n-->\n"
                "TITLE: Bad\nDESC: something the human never said\n\nbody\n")
    assert drafts_of(store) == [
        ("archive-on-slow-disk", "TOOL,USER", "the archive lives on the slow disk"),
        ("by-hand", "SELF", "something the human never said")]
