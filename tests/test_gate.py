"""The gate — the floor under every model in the system.

If these tests pass, a model cannot get the covered classes of unsupported claims —
fabricated quotes, unbacked numbers, unearned attribution to the human — into the
store, no matter how
confidently it words it. That is the whole claim of this project, so it is tested
adversarially: each case is a way a real model actually tried to get something through.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from distill_kura.distill.gate import attributes_to_human, gate, salvage   # noqa: E402
from distill_kura.distill.sources import Segment                           # noqa: E402

SEGS = [
    Segment("USER", "let's move the index to a separate file, the current one is too big"),
    Segment("TOOL", "elapsed 4.21s, 118 memories, 402 links resolved"),
    Segment("ACT", "Bash grep -c '' MEMORY.md"),
    Segment("SELF", "I think the index will need pruning before long"),
]


def test_verbatim_quote_survives():
    kept, dropped, ideas = gate([{
        "topic": "index-split", "kind": "project", "why": "a decision about the index",
        "quotes": ["[USER] let's move the index to a separate file"],
    }], SEGS)
    assert len(kept) == 1 and not dropped
    assert kept[0]["classes"] == ["USER"]


def test_a_legacy_ken_or_me_tag_still_names_the_class_it_meant():
    """The house once wrote [KEN] and [ME]. If those stopped mapping to USER and SELF,
    an old quote would silently take the class of whatever haystack it was found in —
    and the class is what decides what may be claimed."""
    kept, _, _ = gate([{
        "topic": "index-split", "kind": "project", "why": "a decision about the index",
        "quotes": ["[KEN] let's move the index to a separate file"],
    }], SEGS)
    assert kept[0]["evidence"][0]["class"] == "USER"
    kept, _, _ = gate([{
        "topic": "pruning-needed", "kind": "project", "why": "my own judgement, a read",
        "quotes": ["[ME] I think the index will need pruning before long"],
    }], SEGS)
    assert kept[0]["evidence"][0]["class"] == "SELF"


def test_fabricated_quote_is_dropped():
    kept, dropped, _ = gate([{
        "topic": "invented", "kind": "project", "why": "sounds plausible",
        "quotes": ["[USER] we agreed to rewrite the whole thing in Rust"],
    }], SEGS)
    assert not kept
    assert dropped[0]["why_dropped"] == "quotes not found in the raw material"


def test_paraphrase_is_dropped_even_when_faithful():
    """A paraphrase may be perfectly accurate — it still cannot be checked, so it is
    treated exactly like an invention."""
    kept, dropped, _ = gate([{
        "topic": "paraphrase", "kind": "project", "why": "same meaning, different words",
        "quotes": ["[USER] the user wants the index moved out into its own file"],
    }], SEGS)
    assert not kept and dropped


def test_agent_prose_alone_cannot_become_a_fact():
    kept, dropped, _ = gate([{
        "topic": "pruning-needed", "kind": "project", "why": "the index needs pruning",
        "quotes": ["[SELF] I think the index will need pruning before long"],
    }], SEGS)
    assert not kept
    assert dropped[0]["why_dropped"] == "turning the agent's own words into a fact"


def test_agent_prose_survives_when_it_names_itself_a_judgement():
    kept, _, _ = gate([{
        "topic": "pruning-needed", "kind": "feedback",
        "why": "my judgement: the index will need pruning",
        "quotes": ["[SELF] I think the index will need pruning before long"],
    }], SEGS)
    assert len(kept) == 1 and kept[0]["judgement"] is True


def test_number_without_tool_backing_is_flagged():
    kept, _, _ = gate([{
        "topic": "402-links", "kind": "project", "why": "there are 402 links now",
        "quotes": ["[USER] let's move the index to a separate file"],
    }], SEGS)
    assert kept[0]["unverified_numbers"] is True


def test_number_with_tool_backing_is_grounded():
    kept, _, _ = gate([{
        "topic": "402-links", "kind": "project", "why": "there are 402 links now",
        "quotes": ["[TOOL] elapsed 4.21s, 118 memories, 402 links resolved"],
    }], SEGS)
    assert kept[0]["unverified_numbers"] is False
    assert "TOOL" in kept[0]["classes"]


def test_quote_already_in_the_store_is_an_echo_not_new_material():
    """A tool result that read the store back is not a discovery. Without this, a store
    re-finds and re-records its own contents forever."""
    store_text = "elapsed 4.21s, 118 memories, 402 links resolved"
    kept, dropped, _ = gate([{
        "topic": "echo", "kind": "project", "why": "counts",
        "quotes": ["[TOOL] elapsed 4.21s, 118 memories, 402 links resolved"],
    }], SEGS, store_text)
    assert not kept
    assert dropped[0]["why_dropped"] == "echo of text already in the store"


def test_ideas_need_no_quotes():
    _, _, ideas = gate([{
        "topic": "try-a-bloom-filter", "kind": "idea",
        "why": "a bloom filter might shortcut the resolve step", "quotes": [],
    }], SEGS)
    assert len(ideas) == 1


def test_a_factual_report_cannot_hide_inside_an_idea():
    """The one hole in the idea hatch, found in the wild: a factual claim with no quotes
    relabelled `kind: idea` to skip verification."""
    _, dropped, ideas = gate([{
        "topic": "approval", "kind": "idea",
        "why": "the user approved moving the index into its own file", "quotes": [],
    }], SEGS)
    assert not ideas
    assert dropped[0]["why_dropped"] == "a factual report dressed as an idea"


def test_class_tag_is_corrected_to_where_the_text_really_is():
    """A quote labelled [USER] that actually lives in tool output is re-filed, not
    trusted — otherwise mislabelling would launder a machine line into a human decision."""
    kept, _, _ = gate([{
        "topic": "mislabelled", "kind": "project", "why": "counts",
        "quotes": ["[USER] elapsed 4.21s, 118 memories, 402 links resolved"],
    }], SEGS)
    assert kept[0]["classes"] == ["TOOL"]


def test_too_short_quotes_do_not_count():
    kept, dropped, _ = gate([{
        "topic": "tiny", "kind": "project", "why": "x", "quotes": ["[USER] the"],
    }], SEGS)
    assert not kept and dropped


def test_salvage_recovers_objects_from_a_truncated_array():
    raw = ('[{"topic":"a","kind":"project","why":"one","quotes":["[USER] x"]},'
           '{"topic":"b","kind":"project","why":"two","quotes":["[USER] y')
    got = salvage(raw)
    assert len(got) == 1 and got[0]["topic"] == "a"


def test_salvage_ignores_braces_inside_strings():
    raw = '[{"topic":"a","why":"contains a } brace","quotes":["[USER] x"]}]'
    assert len(salvage(raw)) == 1


def test_attribution_check_is_mechanical():
    assert attributes_to_human("the user decided to drop it", [])
    assert attributes_to_human("ケンが決めた", [])
    assert not attributes_to_human("the user decided to drop it", ["USER"])
    assert not attributes_to_human("the index was moved", [])


# ── the composed text's numbers (gate_version 3) ────────────────────────────
#
# Every case is a way a scribe actually invents: a measurement from nowhere, a
# ratio it computed itself, a rounded "improvement" of a real figure.

def test_composed_invented_number_is_caught():
    ev = [{"class": "TOOL", "text": "decode 51.43 t/s held while streaming"}]
    from distill_kura.distill.gate import composed_number_violations
    assert composed_number_violations("the run reached 49 TPS on 12 GPUs", ev) == ["49", "12"]


def test_composed_number_from_evidence_passes():
    from distill_kura.distill.gate import composed_number_violations
    ev = [{"class": "TOOL", "text": "decode 51.43 t/s held; port :8085 answered"}]
    assert composed_number_violations("held 51.43 t/s, served on :8085", ev) == []


def test_composed_number_survives_formatting_drift():
    from distill_kura.distill.gate import composed_number_violations
    ev = [{"class": "TOOL", "text": "table is 51,200,245,760 params"}]
    assert composed_number_violations("a 51200245760-param table", ev) == []


def test_composed_derived_ratio_is_refused_on_purpose():
    from distill_kura.distill.gate import composed_number_violations
    ev = [{"class": "TOOL", "text": "before 899 ms, after 2.3 ms"}]
    assert composed_number_violations("that is roughly 390x faster", ev) == ["390"]


def test_composed_allowed_text_and_list_markers():
    from distill_kura.distill.gate import composed_number_violations
    ev = [{"class": "USER", "text": "please keep the archive on the slow disk"}]
    assert composed_number_violations("## 2026-09-01 decided", ev, allowed="2026-09-01") == []
    assert composed_number_violations("1. check the fans\n2. check the disk", ev) == []


def test_composed_single_digits_are_claims_now():
    # "8 GPUs" and "4-bit" are exactly what a local-model house invents.
    from distill_kura.distill.gate import composed_number_violations
    ev = [{"class": "TOOL", "text": "ran on 4 GPUs"}]
    assert composed_number_violations("ran on 8 GPUs", ev) == ["8"]
    assert composed_number_violations("ran on 4 GPUs", ev) == []


# Three exploits from the second outside review — each passed the first gate v3 draft.

def test_composed_numbers_never_borrow_neighbouring_digits():
    from distill_kura.distill.gate import composed_number_violations
    ev = [{"class": "TOOL", "text": "before 899 ms; after 2.3 ms"}]
    assert composed_number_violations("it took 923 ms", ev) == ["923"]


def test_composed_sign_is_meaning():
    from distill_kura.distill.gate import composed_number_violations
    ev = [{"class": "TOOL", "text": "profit was +12.5%"}]
    assert composed_number_violations("loss was -12.5%", ev) == ["-12.5"]
    assert composed_number_violations("the figure 12.5% moved", ev) == []


def test_composed_range_is_one_claim():
    from distill_kura.distill.gate import composed_number_violations
    ev = [{"class": "TOOL", "text": "12 GPUs and 16 GB"}]
    assert composed_number_violations("needs 12-16 GPUs", ev) == ["12-16"]


def test_composed_markdown_bullet_is_not_a_sign():
    from distill_kura.distill.gate import composed_number_violations
    ev = [{"class": "TOOL", "text": "counted 12 entries"}]
    assert composed_number_violations("- 12 entries were counted", ev) == []


# Round three: the Unicode disguises, and the door behind the last writer.

def test_composed_unicode_dashes_are_not_a_disguise():
    from distill_kura.distill.gate import composed_number_violations
    ev = [{"class": "TOOL", "text": "12 GPUs and 16 GB; profit was +12.5%"}]
    assert composed_number_violations("needs 12\u201316 GPUs", ev) == ["12-16"]      # en dash
    assert composed_number_violations("loss was \u221212.5%", ev) == ["-12.5"]       # true minus


def test_composed_scientific_notation_is_one_token():
    from distill_kura.distill.gate import composed_number_violations
    ev = [{"class": "TOOL", "text": "about 1e9 parameters"}]
    assert composed_number_violations("about 1e9 parameters", ev) == []
    assert composed_number_violations("about 2e9 parameters", ev) == ["2e9"]


def test_final_surface_covers_attribution_too():
    from distill_kura.distill.gate import final_surface_violations
    ev = [{"class": "SELF", "text": "I think the slow disk is right"}]
    out = final_surface_violations("the user decided on the slow disk", ev, ["SELF"])
    assert out == ["credits the human with no [USER] quote"]
    assert final_surface_violations("the slow disk seems right", ev, ["SELF"]) == []


def test_composed_signed_scientific_is_one_token():
    # Round four: "-1e9" must not decompose into an evidenced "-1" and "9".
    from distill_kura.distill.gate import composed_number_violations
    ev = [{"class": "TOOL", "text": "temperature -1 C; repeated 9 times"}]
    assert composed_number_violations("-1e9 parameters", ev) == ["-1e9"]


def test_attribution_knows_the_houses_own_shorthand():
    # The house writes "ケン確定 / ケン裁定 / ケン: …" more often than "ケンが決めた"; a
    # floor that knew only the verb forms let a cue rewrite who decided.
    for line in ("ケン確定: SSD層はアーカイブ用途", "ケン裁定 09-01", "ケン: こっちで行こう",
                 "Ken decided to keep the slow disk", "the owner ruled it out"):
        assert attributes_to_human(line, []), line
    for line in ("ケンにとってのユキ", "SSD層はアーカイブ用途", "a decision was reached"):
        assert not attributes_to_human(line, []), line


def test_the_manifests_gate_version_and_the_signed_format_string_agree():
    """The gate's format version used to be written twice — `"gate_version": 6` in the
    manifest and the literal `"gate-format-v6"` inside the signed blob — with nothing
    tying them together. A bump that moved one and not the other would ship manifests
    announcing a version whose marks are still signed under the old string, and every
    existing test would still pass. One constant now, the other derived; this pins it."""
    from distill_kura.distill import pipeline

    assert pipeline.GATE_FORMAT == f"gate-format-v{pipeline.GATE_VERSION}"
    # The version number itself does not move without a deliberate edit here.
    assert pipeline.GATE_VERSION == 7
    assert pipeline.GATE_FORMAT == "gate-format-v7"
    src = open(pipeline.__file__, encoding="utf-8").read()
    # No second literal may creep back in: the string is built, never typed.
    assert '"gate-format-v7' not in src and "'gate-format-v7" not in src


def test_the_index_craft_tells_writers_that_retired_things_wear_it():
    """A retired memory must not be dressed as current (Rina, 2026-09-02): the rule
    every writer reads names the retirement face and ties it to a VERIFIED
    transition, never to prose alone."""
    from distill_kura.distill.prompts import INDEX_CRAFT, SCRIBE_SYS, TIDY_SYS
    assert "Retired things wear it" in INDEX_CRAFT and "VERIFIED" in INDEX_CRAFT
    assert "never hidden" in INDEX_CRAFT
    for sysmsg in (SCRIBE_SYS, TIDY_SYS):
        assert "Retired things wear it" in sysmsg
