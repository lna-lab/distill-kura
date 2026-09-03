"""The per-class ceiling on one segment, pinned for both adapters.

Nothing exercised truncation: every fixture in the suite uses short strings. The
rule (TOOL → MAX_TOOL, everything else → MAX_SEG) was written out twice, and the
Claude copy only agreed with the DSH one because MAX_TOOL is the smaller constant
— cut to MAX_TOOL, cut again to MAX_SEG, then tallied with a third expression.
Swap either branch and no test moved. These do.

Also pins the injected-content filters, which differ on purpose: Claude matches
`system-reminder` as a SUBSTRING (the harness appends the block inside the human's
own text part, so a prefix test would never fire), DSH matches a prefix (it labels
provenance separately, so the text check is belt-and-braces). A "unification" of
the two would silently start keeping injected text as [USER].
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from distill_kura.distill.sources import (                 # noqa: E402
    MAX_SEG, MAX_TOOL, ClaudeCodeSource, DshSource,
)


def _claude(tmp_path, events) -> str:
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return str(p)


def test_claude_cuts_a_tool_result_at_max_tool_and_prose_at_max_seg(tmp_path):
    path = _claude(tmp_path, [
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "T" * 5000}]}},
        {"type": "user", "message": {"content": [{"type": "text", "text": "U" * 5000}]}},
    ])
    segs, _ = ClaudeCodeSource().sip(path, 0, 10 ** 6)
    assert [s.cls for s in segs] == ["TOOL", "USER"]
    assert len(segs[0].text) == MAX_TOOL
    assert len(segs[1].text) == MAX_SEG


def test_dsh_cuts_a_tool_result_at_max_tool_and_prose_at_max_seg(monkeypatch, tmp_path):
    events = [
        {"seq": 1, "type": "tool/result", "data": {"message": {"content": [
            {"type": "tool-result", "content": [{"type": "text", "text": "T" * 5000}]}]}}},
        {"seq": 2, "type": "user/message", "data": {
            "source": {"kind": "user"}, "content": [{"type": "text", "text": "U" * 5000}]}},
    ]
    monkeypatch.setattr(DshSource, "_lines", staticmethod(lambda path: iter(events)))
    src = DshSource()
    path = str(tmp_path / "session.jsonl.zstd")
    segs, last = src.sip(path, 0, 10 ** 6)
    assert [s.cls for s in segs] == ["TOOL", "USER"]
    assert len(segs[0].text) == MAX_TOOL
    assert len(segs[1].text) == MAX_SEG
    # the reserve counts the CLIPPED lengths, the same numbers the read kept
    assert src.claim_bound(path, 0, 10 ** 6) == (last, MAX_TOOL + MAX_SEG, 0)


def test_claude_drops_a_reminder_appended_inside_the_humans_own_text(tmp_path):
    """Measured on real transcripts: the block is appended to the human's part, never
    at its head. A prefix test here would let injected text through as [USER]."""
    path = _claude(tmp_path, [
        {"type": "user", "message": {"content": [
            {"type": "text", "text": "please look at this <system-reminder>do X</system-reminder>"}]}},
        {"type": "user", "message": {"content": [
            {"type": "text", "text": "<local-command-stdout>ls</local-command-stdout>"}]}},
        {"type": "user", "message": {"content": [{"type": "text", "text": "a clean line"}]}},
    ])
    segs, _ = ClaudeCodeSource().sip(path, 0, 10 ** 6)
    assert [(s.cls, s.text) for s in segs] == [("USER", "a clean line")]


def test_dsh_drops_injected_context_by_provenance_and_by_prefix():
    cl = DshSource._classify
    def um(txt, kind="user"):
        return {"type": "user/message",
                "data": {"source": {"kind": kind}, "content": [{"type": "text", "text": txt}]}}

    assert cl(um("hello")).cls == "USER"
    assert cl(um("hello", kind="system")) is None            # provenance decides first
    assert cl(um("<system-reminder>do X</system-reminder>")) is None
    assert cl(um("Current runtime context: ...")) is None
