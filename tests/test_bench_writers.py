"""Writer A/B tests use local endpoint doubles, so no model or socket is needed."""
from __future__ import annotations

import json
import os

import pytest

from distill_kura import bench_writers
from distill_kura.distill import Distiller
from distill_kura.registry import Registry
from distill_kura.store import Store
from distill_kura.thinker import Models


EVIDENCE = [{"class": "USER", "text": "the human selected the blue plan"}]
CANDIDATE = {"topic": "blue-plan", "kind": "project",
             "why": "the human selected the blue plan", "evidence": EVIDENCE,
             "classes": ["USER"], "quotes": ["[USER] the human selected the blue plan"]}


class FakeScribe:
    """A deterministic writer whose first answer can model one unsafe floor."""

    def __init__(self, name: str, mode: str):
        self.name = name
        self.url = f"fake://{name}"
        self.model = name
        self.effort = "low"
        self.thinking = False
        self.extra = {}
        self.last_error = ""
        self.last_usage = None
        self.mode = mode
        self.calls = 0

    def ask(self, system, user, **kwargs):
        self.calls += 1
        self.last_usage = {"prompt_tokens": 10 + self.calls,
                           "completion_tokens": 5}
        bad = self.mode in ("number", "link", "quote", "attribution")
        if self.mode == "fix" and self.calls == 1:
            bad = True
        body = "The human selected the blue plan."
        if bad and self.mode in ("number", "fix"):
            body += " It was chosen in 99 sessions."
        if bad and self.mode == "link":
            body += " See [[missing-memory]]."
        if bad and self.mode == "quote":
            body += ' Someone said "a fabricated sentence".'
        if bad and self.mode == "attribution":
            body += " Ken decided it."
        return ("SLUG: blue-plan\nTITLE: Blue plan\n"
                "DESC: the human selected the blue plan\nBODY:\n" + body + "\n")


def packet() -> bench_writers.Packet:
    return bench_writers.Packet(kind="NEW", target=None, evidence=EVIDENCE,
                                candidate=CANDIDATE, known_slugs=())


def test_packet_ids_are_stable_and_json_round_trips(tmp_path):
    original = packet()
    restored = bench_writers.Packet.from_json(original.to_json())
    assert restored.to_dict() == original.to_dict()
    assert restored.id == bench_writers.Packet(
        kind="NEW", target=None, evidence=EVIDENCE, candidate=CANDIDATE,
        known_slugs=()).id
    assert restored.id in original.to_json()


def test_freezing_uses_the_brain_gate_path_without_advancing_watermarks(tmp_path, monkeypatch):
    store = Store(name="m", path=str(tmp_path / "store"))
    store.init_files()
    journal = tmp_path / "journal.md"
    journal.write_text("the human selected the blue plan\n\n" + ("padding " * 1000),
                       encoding="utf-8")
    distiller = Distiller(
        Registry(stores={"m": store}, modes={}, models=Models.from_config({}), default="m",
                 raw={"distill": {"journals": {"text": str(tmp_path)}}}), store)
    monkeypatch.setattr(distiller, "spot", lambda segs: [dict(CANDIDATE)])
    monkeypatch.setattr(distiller, "novelty", lambda c, near: ("NEW", "new", None))
    monkeypatch.setattr(bench_writers, "kura_recall", lambda *args, **kwargs: {"walked": []})

    packets = bench_writers.freeze_packets(store, distiller, chunks=1)
    assert len(packets) == 1 and packets[0].kind == "NEW"
    assert packets[0].source_key.startswith("text:")
    assert distiller.marks.read() == {}


def test_writer_report_counts_each_floor_and_records_the_retry(tmp_path):
    store = Store(name="m", path=str(tmp_path / "store"))
    store.init_files()
    writers = [FakeScribe("clean", "clean"), FakeScribe("number", "number"),
               FakeScribe("link", "link"), FakeScribe("fix", "fix")]
    report = bench_writers.run_writers([packet()], writers, store, str(tmp_path / "report"))

    assert len(report.rows) == 4
    by_writer = {row["writer"]: row for row in report.rows}
    assert by_writer["clean"]["clean_first_try"] is True
    assert by_writer["number"]["status"] == "rejected"
    assert by_writer["number"]["floor_counts"]["invented_number"] == 1
    assert by_writer["link"]["floor_counts"]["dead_link"] == 1
    assert by_writer["fix"]["clean_after_rewrite"] is True
    assert by_writer["fix"]["retry_fixed"] is True
    assert by_writer["fix"]["attempts"][0]["violations"] == ["invented number: 99"]
    assert report.totals["number"]["rejected"] == 1
    assert report.totals["number"]["invented_number"] == 1
    assert report.totals["link"]["dead_link"] == 1
    assert report.totals["fix"]["clean_after_rewrite"] == 1
    assert report.totals["fix"]["attempt_floor_counts"]["invented_number"] == 1
    assert report.totals["fix"]["invented_number"] == 0
    assert by_writer["clean"]["prompt_tokens"] == 11
    assert by_writer["clean"]["completion_tokens"] == 5

    out = tmp_path / "report"
    assert (out / "report.json").exists()
    assert (out / "report.md").exists()
    for name in ("clean", "number", "link", "fix"):
        surface = out / name / f"{packet().id}.md"
        assert surface.exists() and surface.read_text(encoding="utf-8")
    markdown = (out / "report.md").read_text(encoding="utf-8")
    assert markdown.count("| " + packet().id + " |") == 4
    assert "| clean | — | 1 |" in markdown


def test_writer_report_separates_quotation_and_attribution_floors(tmp_path):
    store = Store(name="m", path=str(tmp_path / "store"))
    store.init_files()
    quotation_packet = packet()
    attribution_packet = bench_writers.Packet(
        kind="NEW", target=None,
        evidence=[{"class": "SELF", "text": "I think the blue plan is useful"}],
        candidate={"topic": "blue-plan", "kind": "project", "why": "my judgement",
                   "quotes": ["[SELF] I think the blue plan is useful"],
                   "classes": ["SELF"]}, known_slugs=())
    report = bench_writers.run_writers(
        [quotation_packet, attribution_packet],
        [("quote", FakeScribe("quote", "quote")),
         ("attribution", FakeScribe("attribution", "attribution"))],
        store, str(tmp_path / "report"))
    quote_row = next(r for r in report.rows
                     if r["writer"] == "quote" and r["packet_id"] == quotation_packet.id)
    attr_row = next(r for r in report.rows
                    if r["writer"] == "attribution" and r["packet_id"] == attribution_packet.id)
    assert quote_row["floor_counts"]["invented_quotation"] == 1
    assert attr_row["floor_counts"]["attribution"] == 1


def test_bench_writer_config_refuses_unknown_keys_and_preserves_rep(tmp_path):
    store_path = tmp_path / "store"
    good = tmp_path / "good.toml"
    good.write_text(
        f"[stores.main]\npath = {json.dumps(str(store_path))}\n"
        "[[bench.writers]]\nname = 'a'\nurl = 'http://writer/v1'\nmodel = 'm'\n"
        "rep = 1.05\n", encoding="utf-8")
    reg = Registry.load(str(good))
    assert reg.bench_writers[0]["rep"] == 1.05
    assert reg.bench_writers[0]["extra"]["repeat_penalty"] == 1.05

    bad = tmp_path / "bad.toml"
    bad.write_text(
        f"[stores.main]\npath = {json.dumps(str(tmp_path / 'bad-store'))}\n"
        "[[bench.writers]]\nname = 'a'\nurl = 'http://writer/v1'\nmodel = 'm'\n"
        "not_a_writer_key = true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not_a_writer_key"):
        Registry.load(str(bad))


def test_markdown_has_one_linked_surface_row_for_each_writer_and_packet(tmp_path):
    store = Store(name="m", path=str(tmp_path / "store"))
    store.init_files()
    store.remember("old-memory", "the old blue plan", "old body")
    packets = [packet(), bench_writers.Packet(
        kind="EXTENDS", target="old-memory", evidence=EVIDENCE,
        candidate={**CANDIDATE, "extends": "old-memory"}, known_slugs=("old-memory",))]
    writer = FakeScribe("clean", "clean")
    report = bench_writers.run_writers(packets, [("clean", writer)], store,
                                       str(tmp_path / "report"))
    assert len(report.rows) == 2
    markdown = (tmp_path / "report" / "report.md").read_text(encoding="utf-8")
    assert markdown.count("[clean/" + packet().id + ".md]") == 1
    assert markdown.count("[clean/" + packets[1].id + ".md]") == 1
