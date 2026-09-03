"""Writer A/B measurement on frozen, gate-passed candidate packets.

The reader and the store are deliberately held still here. A packet is made once from
the normal brain → gate → novelty path, then every writer sees that same candidate,
evidence and known-slug snapshot. The only judgement in this module is aggregation:
the final surface is accepted or rejected by ``Distiller.compose`` and its existing
mechanical floors.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

from .distill.gate import gate
from .distill.pipeline import MIN_DRINK, Distiller
from .distill.prompts import DEFAULT_CHARTER
from .distill.sources import call_claim_bound, call_sip, source_for
from .recall import recall as kura_recall
from .thinker import Endpoint, Models

FLOORS = ("invented_number", "invented_quotation", "dead_link", "attribution", "shape")


def _evidence_text(evidence: list[dict]) -> str:
    return "\n".join(f"[{e.get('class', '')}] {e.get('text', '')}" for e in evidence)


def packet_id(evidence: list[dict], kind: str, target: str | None) -> str:
    """A packet identity is its evidence and novelty destination, not its topic.

    Topics are model wording and may change while the evidence stays the same. Hashing
    that wording made two equivalent writer inputs look different and made a paired
    comparison impossible to reproduce.
    """
    raw = f"{_evidence_text(evidence)}\n{kind}\n{target or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class Packet:
    """The frozen input one writer must receive unchanged."""

    kind: str
    target: str | None
    evidence: list[dict]
    candidate: dict
    known_slugs: tuple[str, ...] | None = None
    source_key: str = ""
    source_file: str = ""
    id: str = ""

    def __post_init__(self) -> None:
        self.kind = str(self.kind).upper()
        self.target = str(self.target) if self.target is not None else None
        self.evidence = [{"class": str(e["class"]), "text": str(e["text"])}
                         for e in self.evidence]
        self.candidate = dict(self.candidate)
        if self.known_slugs is not None:
            self.known_slugs = tuple(sorted(str(s) for s in self.known_slugs))
        expected = packet_id(self.evidence, self.kind, self.target)
        if self.id and self.id != expected:
            raise ValueError(f"packet id does not match its evidence/kind/target: {self.id!r}")
        self.id = expected

    @classmethod
    def from_candidate(cls, candidate: dict, kind: str, target: str | None,
                       known_slugs=None, source_key: str = "",
                       source_file: str = "") -> "Packet":
        """Copy the gate result so later model or store changes cannot rewrite a packet."""
        evidence = list(candidate.get("evidence") or [])
        return cls(kind=kind, target=target, evidence=evidence, candidate=dict(candidate),
                   known_slugs=tuple(known_slugs) if known_slugs is not None else None,
                   source_key=source_key, source_file=source_file)

    def compose_candidate(self) -> dict:
        """Restore the pipeline's candidate shape, adding novelty's target mechanically."""
        c = {**self.candidate, "evidence": [dict(e) for e in self.evidence]}
        if self.target is not None and self.kind == "EXTENDS":
            c["extends"] = self.target
        return c

    @property
    def target_slug(self) -> str | None:
        """The explicit name used by callers that do not use the shorter report key."""
        return self.target

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "target": self.target,
                "evidence": [dict(e) for e in self.evidence],
                "candidate": self.candidate,
                "known_slugs": list(self.known_slugs) if self.known_slugs is not None else None,
                "source_key": self.source_key, "source_file": self.source_file}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=1)

    @classmethod
    def from_dict(cls, data: dict) -> "Packet":
        if not isinstance(data, dict):
            raise ValueError(f"a packet must be an object, got {type(data).__name__}")
        return cls(kind=data["kind"], target=data.get("target"),
                   evidence=list(data.get("evidence") or []),
                   candidate=dict(data.get("candidate") or {}),
                   known_slugs=(tuple(data["known_slugs"])
                                if data.get("known_slugs") is not None else None),
                   source_key=str(data.get("source_key") or ""),
                   source_file=str(data.get("source_file") or ""),
                   id=str(data.get("id") or ""))

    @classmethod
    def from_json(cls, text: str) -> "Packet":
        return cls.from_dict(json.loads(text))


def _selection_options(journal_selection, chunks: int | None, session: str | None):
    """Accept the public keyword forms without making callers know a private selector type."""
    if isinstance(journal_selection, Mapping):
        chunks = journal_selection.get("chunks", chunks)
        session = journal_selection.get("session", session)
        paths = journal_selection.get("paths") or journal_selection.get("files")
    elif isinstance(journal_selection, str):
        session = journal_selection
        paths = None
    elif isinstance(journal_selection, (list, tuple)):
        paths = list(journal_selection)
    else:
        paths = None
    return max(0, int(chunks if chunks is not None else 1)), session, paths


def _selected_chunk(distiller: Distiller, positions: dict[str, int], session: str | None,
                    paths: list[str] | None):
    """Read the next worth-drinking chunk without claiming its production watermark.

    Freezing is an observation, not a distill run. Advancing ``watermark.json`` here
    would make merely preparing a comparison consume the journal even if no writer
    was ever run; the local positions preserve the same source boundaries in memory.
    """
    files = list(paths) if paths is not None else distiller.files(session)
    for path in files:
        src = source_for(path)
        if not src:
            continue
        key = src.key(path)
        start = positions.get(key, distiller.marks.read().get(key, 0))
        # 3-value claim (end, approx, scan_pending) since the evidence source landed (PR #5);
        # call_* normalise legacy 2-value adapters. A bounded discard in progress is skipped
        # here — freezing must not drink through an oversized line.
        end, approx, scan_pending = call_claim_bound(src, path, start, distiller.chunk_chars)
        if scan_pending or approx < MIN_DRINK or end <= start:
            continue
        segs, nxt = call_sip(src, path, start, distiller.chunk_chars, bound_end=end)
        positions[key] = nxt
        return segs, path, key
    return None


def freeze_packets(store, distiller: Distiller, journal_selection=None, *,
                   chunks: int | None = None, session: str | None = None) -> list[Packet]:
    """Freeze gate-passed NEW/EXTENDS candidates from selected journal water.

    ``Distiller.spot`` and ``gate`` remain the source of candidate and evidence
    semantics. Novelty is also the production method, because an EXTENDS packet needs
    the target slug that the writer's extension prompt will read. COVERED candidates
    are not writer inputs: production does not compose a second memory for them.
    """
    count, session, paths = _selection_options(journal_selection, chunks, session)
    positions: dict[str, int] = {}
    known = tuple(sorted(store.slug_set()))
    out: list[Packet] = []
    seen: set[str] = set()
    for _ in range(count):
        got = _selected_chunk(distiller, positions, session, paths)
        if not got:
            break
        segs, path, key = got
        if not segs:
            continue
        candidates = distiller.spot(segs)
        kept, _, _ = gate(candidates, segs, distiller.store_text())
        for candidate in kept:
            if candidate.get("extends"):
                verdict, why, target = "EXTENDS", candidate.get("extends_why", ""), candidate["extends"]
            else:
                near = kura_recall(store, distiller.models.thinker,
                                   candidate.get("why") or candidate.get("topic", ""),
                                   hops=0, top=3, chars=1200)
                verdict, why, target = distiller.novelty(candidate, near)
            if verdict == "COVERED":
                continue
            c = {**candidate}
            if verdict == "EXTENDS" and target:
                c.update({"extends": target, "extends_why": why})
            p = Packet.from_candidate(c, verdict, target if verdict == "EXTENDS" else None,
                                      known_slugs=known, source_key=key,
                                      source_file=path)
            if p.id not in seen:
                seen.add(p.id)
                out.append(p)
    return out


def save_packets(packets: list[Packet], path: str) -> None:
    """Write one self-contained packet file; the wrapper leaves room for schema metadata."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"packets": [p.to_dict() for p in packets]}, f,
                  ensure_ascii=False, indent=1)
        f.write("\n")


def load_packets(path: str) -> list[Packet]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    raw = data.get("packets") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raise ValueError(f"packet file must contain an array or a `packets` array: {path}")
    return [Packet.from_dict(p) for p in raw]


class _TraceEndpoint:
    """Endpoint proxy that measures calls while leaving its request/response unchanged."""

    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.calls: list[dict] = []
        self.last_error = ""

    @property
    def model(self):
        return getattr(self.endpoint, "model", "")

    def ask(self, system: str, user: str, **kwargs):
        started = time.perf_counter()
        error = None
        answer = None
        try:
            answer = self.endpoint.ask(system, user, **kwargs)
            return answer
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            raise
        finally:
            usage = getattr(self.endpoint, "last_usage", None)
            if not isinstance(usage, dict):
                usage = getattr(self.endpoint, "usage", None)
            self.last_error = getattr(self.endpoint, "last_error", "") or (error or "")
            self.calls.append({"latency_s": round(time.perf_counter() - started, 6),
                               "prompt_tokens": _usage_number(usage, "prompt"),
                               "completion_tokens": _usage_number(usage, "completion"),
                               "answered": answer is not None,
                               "error": error})


def _usage_number(usage: dict | None, stem: str):
    if not usage:
        return None
    for key in (f"{stem}_tokens", f"{stem}_token_count", f"{stem}_n"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


def _sum_known(calls: list[dict], key: str):
    values = [c[key] for c in calls if c.get(key) is not None]
    return sum(values) if values else None


def _floor_of(violations: list[str]) -> dict[str, int]:
    counts = {f: 0 for f in FLOORS}
    for v in violations:
        if v.startswith("invented number:"):
            counts["invented_number"] += 1
        elif v.startswith("invented quotation:"):
            counts["invented_quotation"] += 1
        elif v.startswith("unknown links:"):
            counts["dead_link"] += max(1, len(v.split(":", 1)[1].split(",")))
        elif v.startswith("credits the human"):
            counts["attribution"] += 1
    return counts


def _safe_name(name: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name)).strip(".-")
    return result or "writer-" + hashlib.sha256(str(name).encode()).hexdigest()[:8]


def _rep_of(endpoint):
    extra = getattr(endpoint, "extra", {}) or {}
    for key in ("repeat_penalty", "repetition_penalty"):
        value = extra.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


def _writer_items(writers):
    if isinstance(writers, Mapping):
        values = list(writers.items())
    else:
        values = list(writers or [])
    out = []
    for item in values:
        if isinstance(item, tuple) and len(item) == 2:
            name, endpoint = item
            if isinstance(endpoint, dict):
                config = {**endpoint, "name": name}
                extra = dict(config.get("extra") or {})
                if config.get("rep") is not None and "repeat_penalty" not in extra and "repetition_penalty" not in extra:
                    extra["repeat_penalty"] = config["rep"]
                endpoint = Endpoint.from_dict({**config, "extra": extra}, str(name))
        elif isinstance(item, Endpoint) or hasattr(item, "ask"):
            endpoint = item
            name = getattr(item, "name", "writer")
        elif isinstance(item, dict):
            name = item["name"]
            extra = dict(item.get("extra") or {})
            if item.get("rep") is not None and "repeat_penalty" not in extra and "repetition_penalty" not in extra:
                extra["repeat_penalty"] = item["rep"]
            endpoint = Endpoint.from_dict({**item, "extra": extra}, str(name))
        else:
            raise TypeError(f"writer must be an endpoint, pair, or config object, got {item!r}")
        if not hasattr(endpoint, "ask"):
            raise TypeError(f"writer {name!r} has no ask() method")
        out.append((str(name), endpoint))
    names = [name for name, _ in out]
    if len(names) != len(set(names)):
        raise ValueError(f"writer names must be unique: {names}")
    return out


def _composition_distiller(store, endpoint):
    """Make the compose object without running its workshop constructor.

    ``Distiller.__init__`` creates ``_still/drafts`` as part of a production run. A
    frozen writer bench must not touch the store, so only the read-only attributes
    compose needs are assembled here; the floor implementation remains Distiller's.
    """
    d = Distiller.__new__(Distiller)
    d.store = store
    d.models = Models(thinker=Endpoint(), brain=Endpoint(), scribe=endpoint)
    d.language = "English"
    d.charter = DEFAULT_CHARTER
    if store.charter and os.path.exists(store.charter):
        d.charter = open(store.charter, encoding="utf-8").read()
    try:
        if store.profile_state()["state"] == "present":
            d.charter = d.charter.rstrip("\n") + "\n\n" + store.profile_text().strip() + "\n"
    except (OSError, ValueError):
        pass
    d.still = store.still
    d.drafts_dir = os.path.join(store.still, "drafts")
    d._store_text = None
    d._current_source = ""
    d._current_key = ""
    return d


def _writer_meta(name: str, endpoint) -> dict:
    return {"name": name, "url": getattr(endpoint, "url", ""),
            "model": getattr(endpoint, "model", ""),
            "api_key_env": getattr(endpoint, "api_key_env", None),
            "effort": getattr(endpoint, "effort", None),
            "thinking": getattr(endpoint, "thinking", None),
            "rep": _rep_of(endpoint)}


@dataclass
class Report:
    """Raw paired rows and per-writer counts; deliberately no composite score."""

    rows: list[dict] = field(default_factory=list)
    totals: dict[str, dict] = field(default_factory=dict)
    writers: list[dict] = field(default_factory=list)
    packets: int = 0
    store: str = ""

    def to_dict(self) -> dict:
        return {"store": self.store, "packets": self.packets, "writers": self.writers,
                "totals": self.totals, "rows": self.rows}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=1)

    @classmethod
    def from_dict(cls, data: dict) -> "Report":
        return cls(rows=list(data.get("rows") or []), totals=dict(data.get("totals") or {}),
                   writers=list(data.get("writers") or []), packets=int(data.get("packets", 0)),
                   store=str(data.get("store") or ""))

    def markdown(self) -> str:
        cols = ("packet_id", "writer", "rep", "status", "clean_first_try", "clean_after_rewrite",
                "did_retry", "latency_s", "prompt_tokens", "completion_tokens",
                "surface_length", "surface")
        lines = [f"# Writer benchmark{f' (store={self.store})' if self.store else ''}", "",
                 "Raw paired rows; no composite score.", "",
                 "| " + " | ".join(cols) + " |",
                 "|" + "|".join("---" for _ in cols) + "|"]
        for row in self.rows:
            cells = []
            for c in cols:
                value = row.get(c, "")
                if c == "rep" and isinstance(value, (int, float)):
                    value = f"{value:.2f}"
                if c == "surface":
                    path = row.get("surface_file", "")
                    value = f"[{path}]({path})"
                cells.append(str(value).replace("|", "\\|"))
            lines.append("| " + " | ".join(cells) + " |")
        lines += ["", "## Totals", "",
                  "| writer | rep | packets | clean first try | clean after rewrite | rejected | "
                  "invented number | invented quotation | dead link | attribution | shape |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for name, total in self.totals.items():
            lines.append("| " + " | ".join(str(total.get(k, 0) if k != "rep"
                                                     else (f"{total[k]:.2f}" if isinstance(total.get(k), (int, float))
                                                           else (total.get(k) or "—"))) for k in (
                "writer", "rep", "packets", "clean_first_try", "clean_after_rewrite", "rejected",
                "invented_number", "invented_quotation", "dead_link", "attribution", "shape")) + " |")
        return "\n".join(lines) + "\n"


def run_writers(packets: list[Packet], writers, store, out_dir: str) -> Report:
    """Run each writer against every packet and write ``report.json``/``report.md``.

    A retry is a property of the row, not a reason to hide the first unsafe answer:
    attempts retain their separate violations, while totals count the final packet
    outcome and ``attempt_floor_counts`` preserves the raw first-pass failures.
    """
    os.makedirs(out_dir, exist_ok=True)
    items = _writer_items(writers)
    rows: list[dict] = []
    totals: dict[str, dict] = {}
    used_dirs: dict[str, str] = {}
    for name, endpoint in items:
        dirname = _safe_name(name)
        if dirname in used_dirs and used_dirs[dirname] != name:
            dirname += "-" + hashlib.sha256(name.encode()).hexdigest()[:8]
        used_dirs[dirname] = name
        writer_dir = os.path.join(out_dir, dirname)
        os.makedirs(writer_dir, exist_ok=True)
        total = {"writer": name, "packets": 0, "clean_first_try": 0,
                 "clean_after_rewrite": 0, "rejected": 0,
                 "rep": _rep_of(endpoint),
                 **{floor: 0 for floor in FLOORS},
                 "attempt_floor_counts": {floor: 0 for floor in FLOORS}}
        for packet in packets:
            total["packets"] += 1
            endpoint_trace = _TraceEndpoint(endpoint)
            d = _composition_distiller(store, endpoint_trace)
            observations: list[dict] = []
            failures: list[str] = []
            candidate = packet.compose_candidate()
            error = None
            try:
                record = d.compose_with(candidate, endpoint_trace, near={"walked": []},
                                        failures=failures,
                                        known_slugs=(frozenset(packet.known_slugs)
                                                     if packet.known_slugs is not None else None),
                                        observations=observations)
            except Exception as e:
                record = None
                error = f"{type(e).__name__}: {e}"
            for observation in observations:
                for floor, count in _floor_of(observation.get("violations") or []).items():
                    total["attempt_floor_counts"][floor] += count
            kept = record is not None
            final = observations[-1] if observations else {"shape": True, "surface": "",
                                                             "violations": []}
            final_floors = _floor_of(final.get("violations") or [])
            if final.get("shape"):
                final_floors["shape"] = 1
            for floor, count in final_floors.items():
                total[floor] += count
            did_retry = len(endpoint_trace.calls) > 1
            clean_first = kept and not did_retry
            clean_after = kept and did_retry
            if clean_first:
                total["clean_first_try"] += 1
            if clean_after:
                total["clean_after_rewrite"] += 1
            if not kept:
                total["rejected"] += 1
            surface = final.get("surface") or ""
            if kept and not surface:
                surface = (f"SLUG: {record.get('slug', '')}\nTITLE: {record.get('title', '')}\n"
                           f"DESC: {record.get('description', '')}\nBODY:\n{record.get('body', '')}")
            surface_path = os.path.join(writer_dir, packet.id + ".md")
            with open(surface_path, "w", encoding="utf-8") as f:
                f.write(surface)
                if surface and not surface.endswith("\n"):
                    f.write("\n")
            reasons = list(final.get("violations") or [])
            if final.get("shape"):
                reasons.append("shape not kept")
            row = {"packet_id": packet.id, "writer": name,
                   "rep": _rep_of(endpoint),
                   "status": "kept" if kept else "rejected", "kept": kept,
                   "clean_first_try": clean_first, "clean_after_rewrite": clean_after,
                   "did_retry": did_retry, "retry_fixed": clean_after,
                   "reject_reasons": reasons,
                   "floor_counts": final_floors,
                   "attempts": [{"attempt": i + 1, "shape": bool(o.get("shape")),
                                 "violations": o.get("violations") or [],
                                 **({k: endpoint_trace.calls[i][k]
                                     for k in ("latency_s", "prompt_tokens", "completion_tokens")}
                                    if i < len(endpoint_trace.calls) else {})}
                                for i, o in enumerate(observations)],
                   "latency_s": round(sum(c.get("latency_s", 0) for c in endpoint_trace.calls), 6),
                   "prompt_tokens": _sum_known(endpoint_trace.calls, "prompt_tokens"),
                   "completion_tokens": _sum_known(endpoint_trace.calls, "completion_tokens"),
                   "surface_length": len(surface),
                   "surface_file": os.path.relpath(surface_path, out_dir).replace(os.sep, "/")}
            if failures:
                row["failures"] = failures
            if error:
                row["error"] = error
            rows.append(row)
        total["floor_counts"] = {floor: total[floor] for floor in FLOORS}
        totals[name] = total
    report = Report(rows=rows, totals=totals,
                    writers=[_writer_meta(name, endpoint) for name, endpoint in items],
                    packets=len(packets), store=getattr(store, "name", ""))
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
        f.write(report.to_json() + "\n")
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write(report.markdown())
    return report


def endpoints_from_specs(specs: list[str]) -> list[tuple[str, Endpoint]]:
    """Parse the intentionally small CLI form ``name=url/model``."""
    out = []
    for spec in specs:
        name, sep, value = spec.partition("=")
        url, slash, model = value.rpartition("/")
        if not name or not sep or not url or not slash or not model:
            raise ValueError(f"--writer wants name=url/model, got {spec!r}")
        out.append((name, Endpoint(url=url, model=model, name=name)))
    return out
