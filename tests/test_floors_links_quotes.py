"""Two more floors on a composed surface: dead links, and quotations nobody said.

Rina's ruling (2026-09-03): existence, quotation, numbers and attribution are floors;
whether the advice is any good is the writer's competence — do not mix them. So a
`[[slug]]` must name a memory the store holds (topology is a fact), and a run of text
inside quote marks must stand verbatim in the surviving evidence (a future reply the
scribe imagined — 「はい、反復バグは解決しました」 — is a forged record). Neither floor
reads the prose for quality; nothing here judges whether a "How to apply" line earns
its place, and its ABSENCE is not a broken shape.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from distill_kura.distill import Distiller                                    # noqa: E402
from distill_kura.distill.gate import (MIN_QUOTED, final_surface_violations,  # noqa: E402
                                       invented_quotations, unknown_links)
from distill_kura.registry import Registry                                    # noqa: E402
from distill_kura.store import Store                                          # noqa: E402
from distill_kura.thinker import Models                                       # noqa: E402

EV = [{"class": "USER", "text": "put the archive on the slow disk, the fast one is scratch"},
      {"class": "TOOL", "text": "/data 3.2T used 1.1T avail"}]
CAND = {"topic": "archive-on-slow-disk", "kind": "project", "why": "where the archive lives",
        "evidence": EV, "classes": ["TOOL", "USER"]}


def build(tmp_path) -> tuple[Distiller, Store]:
    store = Store(name="main", path=str(tmp_path / "kura"), label="k")
    store.init_files()
    models = Models.from_config({"thinker": {"url": "http://127.0.0.1:9/v1", "model": "none"}})
    reg = Registry(stores={"main": store}, modes={}, models=models, default="main",
                   raw={"distill": {"journals": {"claude": str(tmp_path / "journals")},
                                    "language": "English"}})
    return Distiller(reg, store), store


def scribe_says(d: Distiller, *answers: str) -> list[str]:
    """Hand the scribe a queue of answers and record the prompts it was given, so a
    test can read the rejection message the scribe actually got back."""
    seen: list[str] = []
    queue = list(answers)

    def pick(task, user, max_tokens=0):
        seen.append(user)
        return queue.pop(0) if queue else ""
    d.scribe = pick               # type: ignore[method-assign]
    return seen


def draft(body: str, slug: str = "archive-on-slow-disk") -> str:
    return (f"SLUG: {slug}\nTITLE: Archive on the slow disk\n"
            "DESC: the archive lives on the slow disk\nBODY:\n" + body + "\n")


# ── the two checks, on their own ─────────────────────────────────────────

def test_a_link_to_a_memory_the_store_does_not_hold_is_named():
    assert unknown_links("see [[alive]] and [[upload-prep]] and [[bug-list]]",
                         frozenset({"alive"})) == ["upload-prep", "bug-list"]


def test_a_caller_that_cannot_say_which_slugs_exist_checks_nothing():
    """None is "I do not know the topology", not "nothing exists" — a floor that
    guessed would refuse honest links."""
    assert unknown_links("see [[anything]]", None) == []


def test_a_quotation_the_evidence_does_not_contain_is_caught():
    assert invented_quotations("そして「はい、反復バグは解決しました」と返答し", EV) == \
        ["はい、反復バグは解決しました"]


def test_double_corner_brackets_are_quotation_marks_too():
    """The reply that motivated this floor was written in 『』, not 「」 — the house
    uses both, and a floor that knew only one bracket would have missed the very
    sentence it exists for."""
    assert invented_quotations("そして『はい、反復バグは解決しました』と返答し", EV) == \
        ["はい、反復バグは解決しました"]
    assert invented_quotations("『put the archive on the slow disk』", EV) == []


def test_a_quotation_that_stands_in_the_evidence_passes():
    assert invented_quotations('the human said "put the archive on the slow disk"', EV) == []
    assert invented_quotations("“put the archive on the slow disk”", EV) == []


def test_full_width_quote_marks_and_spacing_are_not_a_disguise():
    """NFKC and collapsed whitespace on both sides: the same sentence in full-width
    punctuation, or broken across a line, is the same quotation."""
    assert invented_quotations('＂put the archive on\n  the slow disk＂', EV) == []


def test_a_short_quoted_run_is_emphasis_not_testimony():
    assert len("scratch") < MIN_QUOTED
    assert invented_quotations('the fast one is "scratch"', EV) == []


def test_a_bare_token_in_quotes_is_a_name_not_a_quotation():
    """TAGS: ["emotion-carried"] is JSON in a header line, not something anyone said."""
    assert invented_quotations('TAGS: ["emotion-carried", "decision"]', EV) == []


def test_the_final_surface_reports_both_in_the_words_the_scribe_gets_back():
    v = final_surface_violations("[[upload-prep]] [[bug-list]] 「はい、直りました」",
                                 EV, ["USER"], known_slugs=frozenset())
    assert "unknown links: upload-prep, bug-list" in v
    assert any(x.startswith("invented quotation: はい、直りました") for x in v)


# ── the same floors where the scribe meets them ──────────────────────────

def test_a_dead_link_is_rejected_once_and_the_rewrite_is_kept(tmp_path):
    d, _ = build(tmp_path)
    seen = scribe_says(d,
                       draft("The archive goes on the slow disk. See [[upload-prep]]."),
                       draft("The archive goes on the slow disk."))
    rec = d.compose(CAND, near={"walked": []})
    assert rec and rec["slug"] == "archive-on-slow-disk"
    assert "unknown links: upload-prep" in seen[1]      # the scribe was told which ones


def test_a_dead_link_twice_is_a_compose_failure(tmp_path):
    d, _ = build(tmp_path)
    scribe_says(d, draft("See [[upload-prep]]."), draft("Still see [[upload-prep]]."))
    assert d.compose(CAND, near={"walked": []}) is None


def test_a_link_to_an_existing_memory_or_to_the_drafts_own_slug_passes(tmp_path):
    d, store = build(tmp_path)
    store.remember("older-note", "an older note", "body")
    scribe_says(d, draft("The archive goes on the slow disk. See [[older-note]] and "
                         "[[archive-on-slow-disk]]."))
    assert d.compose(CAND, near={"walked": []}) is not None


def test_an_invented_quotation_is_rejected_and_a_verbatim_one_is_not(tmp_path):
    d, _ = build(tmp_path)
    seen = scribe_says(d,
                       draft("そして「はい、反復バグは解決しました」と返答した。"),
                       draft('The human said "put the archive on the slow disk".'))
    assert d.compose(CAND, near={"walked": []}) is not None
    assert "invented quotation: はい、反復バグは解決しました" in seen[1]


def test_a_body_without_a_how_to_apply_line_still_keeps_the_shape(tmp_path):
    """The prompt no longer demands fact → Why → How to apply; a writer with no
    reusable rule in the evidence must be free to omit it rather than invent one."""
    d, _ = build(tmp_path)
    scribe_says(d, draft("The archive goes on the slow disk.\n\n**Why:** endurance."))
    rec = d.compose(CAND, near={"walked": []})
    assert rec is not None and "How to apply" not in rec["body"]


def test_the_scribe_prompt_asks_for_how_to_apply_only_when_it_is_earned():
    from distill_kura.distill import prompts
    assert "ONLY" in prompts.SCRIBE_SYS.split("How to apply")[1][:60]
