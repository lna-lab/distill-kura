"""The distiller: raw journal in, one drop of memory out.

    sip      read past the watermark, tagging every segment with its evidence class
    spot     the BRAIN reads the batch and names what deserves to be remembered
    gate     deterministic Python verifies every quote (see gate.py) — no model here
    novelty  is the store already saying this? COVERED / EXTENDS / NEW
    compose  the SCRIBE writes the memory in the store's language
    stage    it lands in _still/drafts/ — a draft is NOT yet a memory
    pour     the scribe re-reads each draft cold and decides POUR / FIX / TOSS
    tidy     index hygiene: mechanically detectable rot in the index is re-written

Section banners ②…⑧ below follow this order. ① sip is `sip_one`, under "the pass";
③ gate is gate.py, where no model runs.

Why the last step exists at all: if a human has to read the drafts, the system has
quietly made the human its bottleneck, and drafts pile up forever. Nothing in the
loop may depend on someone who is not always present.

Exit codes matter for schedulers: "nothing to do" must be distinguishable from
"did work", or a watchdog spins on an empty queue and starves the steps that need
the idle time.
"""
from __future__ import annotations

import glob
import hashlib
import hmac
import json
import os
import re
import time
from typing import NamedTuple
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from ..recall import recall as kura_recall
from ..tokens import estimate
from ..registry import Registry
from ..store import ANNOTATION_KEYS, FROZEN, Store, normalize_tags
from . import prompts, transition
from .gate import (attributes_to_human, composed_number_violations,
                   final_surface_violations, gate, norm, salvage, verify_tags)
from .seeds import Seeds
from .sources import Segment, as_evidence, call_claim_bound, call_sip, discover_all, source_for, IntakeReport, SCAN_LIMIT
from .watermark import Watermarks


class SipPending(NamedTuple):
    """Bounded discard progress: watermark unchanged, retry without draining."""
    path: str
    key: str
    scan_pending_bytes: int

CHUNK_CHARS = 200_000        # one batch ≈ what a long-context reader swallows at once
MIN_DRINK = 6_000            # less raw material than this is not worth a pass

# ── the evidence gate's format version ───────────────────────────────────────
#
# ONE number. It was written twice — `"gate_version": 6` in the manifest and the
# literal `gate-format-v6` inside the signed blob — with nothing tying them together,
# so a bump could move one and leave the other: manifests announcing a version whose
# marks are still signed under the old string, and no test to say so.
#
# What each version added (additive — a v1 manifest is still read by everything that
# reads manifests):
#   2  tags, the evidence each claiming tag rests on, the ones refused and why,
#      and the three curation sentences.
#   3  the composed text's numbers are re-verified against the evidence before staging.
#   4  the floor covers the whole model-written surface (title, trigger, section,
#      curation sentences, and a judge's FIX before it is re-signed), with
#      Unicode-normalised tokens and single digits verified.
#   5  the slug is part of the gated surface.
#   6  the mark signs the whole ENVELOPE — slug, kind, evidence-manifest digest and
#      body — the judge never judges an unsigned draft, and pour verifies the
#      manifest's bytes.
#
# Routing cues carry their OWN schema version and are never folded into this one.
GATE_VERSION = 6
GATE_FORMAT = f"gate-format-v{GATE_VERSION}"     # derived: the two can no longer drift


def _drafts_dir(still: str) -> str:
    """Where staged drafts live. One spelling: a second one would be a directory
    nothing drains."""
    return os.path.join(still, "drafts")


def _evidence_lines(ev: list[dict], limit: int | None = None, indent: str = "") -> str:
    """The `[CLASS] text` shape a prompt shows evidence in — and the shape the gate
    matches quotes against, so it is rendered in one place. `limit` truncates each
    quote for human eyes (the draft header); the prompts pass the whole thing."""
    return "\n".join(f"{indent}[{e['class']}] {e['text'][:limit]}" for e in ev)


def _log(s: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} {s}", flush=True)


# The lines a draft carries above its body. TITLE/DESC name the memory, EXTENDS points
# at one, and the four curation lines are the scribe's judgement about the store, not
# new facts. All of them sit INSIDE the signed text: an edited tag would break the gate
# mark exactly like an edited sentence.
_HEAD_KEYS = ("EXTENDS", "TITLE", "DESC", "TAGS", "BELONGS_BECAUSE", "KEEP", "MAY_FADE")


_HEAD_LINE = re.compile(r"^(" + "|".join(_HEAD_KEYS) + r"):[ \t]*(.*)$")


def _safe_slug(raw: str) -> str:
    """A draft's file name, from the scribe's SLUG line.

    The sanitiser keeps ASCII only, so a store written in Japanese hands back slugs
    that reduce to nothing (or to a bare "1" out of "メモ-1") — and every such memory
    was then the same file, each new one overwriting the last. A digest of the
    original text gives the nameless one a name of its own: same slug, same name on
    every run, and two different slugs cannot land on one file."""
    s = re.sub(r"[^a-z0-9-]+", "-", raw.strip().lower()).strip("-")[:48].strip("-")
    if len(s) >= 3:
        return s
    h = hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()[:10]
    return f"{s}-{h}" if s else f"memory-{h}"


def _free_path(directory: str, base: str, suffix: str = ".md") -> str:
    """`<base>.md` in `directory`, or `<base>.2.md`, `<base>.3.md`… if that exists.

    Writing straight to a name that is already taken destroys the earlier file with
    no trace — the loser of a name collision is simply gone. Numbering keeps both
    where a person can still read them."""
    p = os.path.join(directory, base + suffix)
    n = 1
    while os.path.exists(p):
        n += 1
        p = os.path.join(directory, f"{base}.{n}{suffix}")
    return p


def _split_draft(body: str) -> tuple[dict[str, str], str]:
    """(header lines as a dict, the rest). Only the block at the TOP counts: a memory
    whose body happens to contain a line starting `KEEP:` keeps it. The block ends at
    the first line that is neither a header nor blank."""
    head: dict[str, str] = {}
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        m = _HEAD_LINE.match(lines[i])
        if m:
            head[m.group(1)] = m.group(2).strip()
        elif lines[i].strip():
            break
        i += 1
    return head, "\n".join(lines[i:]).strip()


def _curation_of(out: str) -> tuple[list[str], dict[str, str], str | None]:
    """Read the optional curation lines from a scribe's output: (tags, annotations,
    problem). A missing line is simply absent — a model that kept the old shape still
    produces a memory. A TAGS line that is not a JSON array is a *problem*, named, and
    the memory is written without tags rather than with a broken frontmatter."""
    # The scribe's output starts with SLUG:/TITLE:/DESC: lines; the curation lines sit
    # among them, above BODY:. Everything after BODY: is the memory and is not scanned.
    head: dict[str, str] = {}
    for line in out.split("BODY:", 1)[0].splitlines():
        m = _HEAD_LINE.match(line)
        if m:
            head[m.group(1)] = m.group(2).strip()
    ann = {k.lower(): head[k] for k in ("BELONGS_BECAUSE", "KEEP", "MAY_FADE") if head.get(k)}
    raw = head.get("TAGS", "")
    if not raw:
        return [], ann, None
    try:
        return list(normalize_tags(raw)), ann, None
    except ValueError as e:
        return [], ann, f"TAGS line unreadable, written untagged: {e}"


class Distiller:
    def __init__(self, reg: Registry, store: Store, journals: dict[str, str] | None = None,
                 language: str | None = None, scribe_slots: int = 4,
                 chunk_chars: int = CHUNK_CHARS):
        self.reg = reg
        self.store = store
        self.models = reg.models_for(store)      # never the shared set behind a profile
        cfg = (reg.raw.get("distill") or {})
        scfg = store.extra.get("distill") if isinstance(store.extra.get("distill"), dict) else {}
        # Key presence, not truthiness. `journals = {}` under a store used to fall
        # through to the global roots because an empty dict is falsey — so "this store
        # inherits nothing" silently meant "this store inherits everything".
        inherit = scfg.get("inherit_global_journals",
                           cfg.get("inherit_global_journals", len(reg.stores) < 2))
        if journals is not None:
            self.journals = journals
        elif "journals" in scfg:
            self.journals = dict(scfg["journals"])
            if inherit:
                self.journals = {**(cfg.get("journals") or {}), **self.journals}
        elif inherit:
            self.journals = dict(cfg.get("journals") or {})
        else:
            # More than one store and no per-store journals: refuse to guess. Feeding
            # every store from the same root is how one mode's conversations end up
            # distilled into another mode's memory.
            self.journals = {}
        self.exclude_roots = [st.path for st in reg.stores.values()]
        self.language = language or scfg.get("language") or cfg.get("language") or "English"
        self.slots = int(scfg.get("scribe_slots") or cfg.get("scribe_slots") or scribe_slots)
        self.chunk_chars = int(scfg.get("chunk_chars") or cfg.get("chunk_chars") or chunk_chars)
        # How many candidates one batch may yield. Four was hardcoded, which is generous
        # for idle chat and thin for a batch full of decisions: past that point a low
        # "compression ratio" is an OMISSION rate, not a quality.
        self.max_items = int(scfg.get("max_items") or cfg.get("max_items") or 4)
        self.coverage_passes = int(scfg.get("coverage_passes")
                                   or cfg.get("coverage_passes") or 1)
        self.charter = (open(store.charter, encoding="utf-8").read()
                        if store.charter and os.path.exists(store.charter)
                        else prompts.DEFAULT_CHARTER)
        # The learned profile, when the store has a sound one, is read AFTER the
        # charter by every role — still one byte-identical head per store, so still
        # one cached prefix. A broken profile is named at construction and NOT read:
        # the fixed charter carries on alone, visibly rather than as a quiet fallback.
        self.profile = store.profile_state()
        if self.profile["state"] == "present":
            self.charter = self.charter.rstrip("\n") + "\n\n" + store.profile_text().strip() + "\n"
        elif self.profile["state"] == "broken":
            _log(f"⚠ learned profile not read — {self.profile['why']}")
        self.still = store.still
        self.drafts_dir = _drafts_dir(self.still)
        self.marks = Watermarks(os.path.join(self.still, "watermark.json"))
        self.seeds = Seeds(os.path.join(self.still, "seeds.jsonl"))
        os.makedirs(self.drafts_dir, exist_ok=True)
        self._store_text: str | None = None

    # ── model roles (charter first, byte-identical, for the shared prefix) ──
    def _sys(self, task: str) -> str:
        return self.charter + "\n──────────\n" + task

    def brain(self, task: str, user: str, max_tokens: int = 2000) -> str:
        return self.models.brain.ask(self._sys(task), user, max_tokens=max_tokens,
                                     timeout=3600) or ""

    def scribe(self, task: str, user: str, max_tokens: int = 1400) -> str | None:
        # None survives on purpose: "the scribe was unreachable" and "the scribe
        # answered" are different facts, and a caller that collapses them (as `or
        # ""` once did) reads an outage as a verdict — a TOSS that deletes the draft.
        return self.models.scribe.ask(self._sys(task.format(language=self.language)),
                                      user, max_tokens=max_tokens, timeout=3600)

    # ── store text, for echo suppression ─────────────────────────────────
    def store_text(self) -> str:
        """Everything the store says, for echo suppression.

        Built from the store's own memories rather than by globbing the directory: a
        file the store excludes (a symlink out of it) is not part of what this store
        says, and letting its text in here would let outside content suppress a
        legitimate candidate."""
        if self._store_text is None:
            self._store_text = norm("\n".join(self.store.read_exact(sl)
                                               for sl in self.store.slugs()))
        return self._store_text

    # ── ② spot ───────────────────────────────────────────────────────────
    def spot(self, segs: list[Segment], max_items: int | None = None) -> list[dict]:
        limit = max_items or self.max_items
        raw = self.brain(prompts.SPOT_SYS.format(max_items=limit), as_evidence(segs), 5000)
        found = salvage(raw)[:limit]
        # A second look, told what the first one already took. One pass optimises for the
        # most striking thing in the batch; the audit asks what it walked past.
        for _ in range(max(0, self.coverage_passes - 1)):
            if len(found) >= limit:
                break
            already = "\n".join(f"- {c.get('topic')}: {c.get('why')}" for c in found)
            more = salvage(self.brain(
                prompts.COVERAGE_SYS.format(max_items=limit - len(found)),
                f"=== ALREADY TAKEN ===\n{already or '(nothing yet)'}\n\n"
                f"=== THE MATERIAL ===\n{as_evidence(segs)}", 5000))
            if not more:
                break
            seen = {c.get("topic") for c in found}
            found += [c for c in more if c.get("topic") not in seen][:limit - len(found)]
        return found

    # ── ④ novelty ────────────────────────────────────────────────────────
    def novelty(self, c: dict, near: dict) -> tuple[str, str, str | None]:
        """Looking at only the top hit picks the wrong neighbour: recall walks by
        meaning, so the real match is often second or third. Compare against three."""
        walked = (near.get("walked") or [])[:3]
        if not walked:
            return "NEW", "nothing close in the store", None
        texts, names = [], []
        for t in walked:
            body = self.store.read(t)
            if body:
                texts.append(f"=== EXISTING MEMORY: {t} ===\n{body[:7000]}")
                names.append(t)
        if not texts:
            return "NEW", "could not read the neighbours", None
        ev = _evidence_lines(c["evidence"])
        out = self.brain(prompts.NOVEL_SYS,
                         f"CANDIDATE: {c.get('topic')}\n{c.get('why')}\n\nEVIDENCE:\n{ev}\n\n"
                         + "\n\n".join(texts)
                         + "\n\nIf the verdict is COVERED or EXTENDS, put the memory's name on "
                           "the verdict line, e.g. `EXTENDS some-slug`.", 300)
        first = out.splitlines()[0].split() if out.strip() else []
        verdict = (first[0].upper() if first else "NEW")
        named = next((w for w in first[1:] if w.strip("`,.") in names), None)
        return ((verdict if verdict in ("COVERED", "EXTENDS", "NEW") else "NEW"),
                " ".join(out.splitlines()[1:])[:200], (named.strip("`,.") if named else names[0]))

    # ── recurrence: one word, once ───────────────────────────────────────
    #
    # "The human brought this up again" is a property worth recording and a number not
    # worth keeping. So a COVERED candidate may put ONE `recurred` on the memory that
    # covers it, under three conditions the model does not get to judge: the candidate
    # carries the human's own words; the memory was distilled from a DIFFERENT journal
    # (a second mention in the same session is not another occasion); and the memory
    # does not already carry the tag. The evidence goes into a manifest of its own,
    # referenced from the memory, so "why does this say recurred?" stays answerable.
    #
    # A memory with no manifest — one written before manifests existed, or by hand —
    # has no known origin, and "different occasion" cannot be decided. It is left alone
    # and the fact is logged; widening this is a decision for a person, not a default.
    def _origin_key(self, slug: str) -> str | None:
        fm = self.store.frontmatter(slug)
        # The ORIGIN, not the latest extension: `evidence_manifest` moves with every
        # EXTENDS pour; `origin_manifest` is pinned to the first and never overwritten.
        ref = fm.get("origin_manifest") or fm.get("evidence_manifest", "")
        if not ref.startswith("sha256:"):
            return None
        man = self.store.load_manifest_verified(ref[7:])
        return str(man.get("source_key") or "") if man is not None else None

    def recur(self, c: dict, target: str, key: str, source: str) -> str:
        """→ 'tagged' | 'already' | a reason it was not.

        Every judgement comes back as a string, never as an exception, and nothing is
        counted. A disk failure in the manifest write or the annotate propagates, like
        every other write in a pass — night() is the designated catch."""
        if "USER" not in c["classes"]:
            return "no [USER] quote: the agent repeating itself is not a recurrence"
        if "recurred" in self.store.tags(target):
            return "already"
        origin = self._origin_key(target)
        if origin is None:
            return "origin unknown (no manifest): left untagged"
        if origin == key:
            return "same journal as the memory's origin: not another occasion"
        kept, basis, _ = verify_tags(["recurred"], c["evidence"], recurred_ok=True)
        if "recurred" not in kept:
            return "not verified"
        digest = self._write_manifest({"kind": c.get("kind"), "classes": c["classes"],
                                       "evidence": c["evidence"], "tags": ["recurred"],
                                       "tag_basis": basis, "recurrence_of": target},
                                      source, key)
        r = self.store.annotate_verified(target, tags=["recurred"],
                                         meta={"recurred_manifest": f"sha256:{digest}"})
        if not r.get("ok"):
            return f"refused: {r.get('error')}"
        return "tagged" if r.get("changed") else "already"

    # ── the learned profile: drafted from this store, applied by a person ──
    def profile_draft(self) -> dict:
        """Write `_still/profile.draft.md` from THIS store's memories. Observable, never
        applied: the file a person reads before deciding is the whole output. A store
        with no memories yields no draft — there is nothing to have learned from."""
        # Memories first, the study shelf last: `slugs()` sorts `_study/` before the
        # letters, and a few long notes used to spend the whole budget before the
        # draft had seen a single memory.
        slugs = sorted(self.store.slugs(), key=lambda x: (x.startswith("_study/"), x))
        if not slugs:
            return {"ok": False, "why": "no memories: nothing to draft a profile from"}
        bodies = []
        room = 60_000
        for sl in slugs:
            t = self.store.read_exact(sl)
            if room - len(t) < 0:
                _log(f"  profile draft: stopped reading at {sl} ({len(slugs)} memories, budget spent)")
                break
            bodies.append(f"=== {sl} ===\n{t}")
            room -= len(t)
        out = self.brain(prompts.PROFILE_SYS.format(language=self.language),
                         f"=== INDEX ===\n{self.store.index_text()}\n\n"
                         f"=== MEMORIES ===\n" + "\n\n".join(bodies), 1600)
        if not out.strip().startswith("##"):
            return {"ok": False, "why": "the model did not keep the shape (no heading first)"}
        # The same check the store applies on read: a draft that would be refused as
        # a profile is refused as a draft, and the reason is in the answer.
        probe_state = self.store.profile_check(out)
        if probe_state["state"] != "present":
            return {"ok": False, "why": f"draft refused: {probe_state['why']}"}
        tmp = os.path.join(self.still, "profile.draft.md")
        os.makedirs(self.still, exist_ok=True)
        with open(tmp + ".tmp", "w", encoding="utf-8") as f:
            f.write(out.strip() + "\n")
        os.replace(tmp + ".tmp", tmp)
        return {"ok": True, "draft": tmp, "chars": len(out),
                "note": "a draft, not a profile. Read it; `kura profile apply` copies it by hand."}

    def sprout(self, c: dict) -> None:
        open_seeds = self.seeds.open_seeds(30)
        if not open_seeds:
            return
        ev = _evidence_lines(c["evidence"])
        listing = "\n".join(f"{i+1}. {s['text']}" for i, s in enumerate(open_seeds))
        out = self.brain(prompts.SPROUT_SYS,
                         f"=== NEW EVIDENCE ===\n{c.get('topic')}: {c.get('why')}\n{ev}\n\n"
                         f"=== OPEN SEEDS ===\n{listing}", 200)
        m = re.match(r"\s*(\d+)\s*\|\s*(.+)", (out or "").strip())
        if not m:
            return
        i = int(m.group(1)) - 1
        if 0 <= i < len(open_seeds) and self.seeds.confirm(open_seeds[i]["text"],
                                                           m.group(2).strip(), c.get("topic", "")):
            _log(f"      🌾 a seed came true: {open_seeds[i]['text'][:60]}")

    # ── ⑤ compose ────────────────────────────────────────────────────────
    def compose(self, c: dict, near: dict | None = None) -> dict | None:
        """`near` is the recall the caller already paid for. run() asks the thinker
        once per candidate, for novelty; composing asked again with the same question
        and got the same answer — two model calls for one fact. A caller that has it
        hands it over; one that does not (a test, the CLI) still gets its own."""
        if c.get("extends"):
            return self._compose_extension(c)
        ev = _evidence_lines(c["evidence"])
        if near is None:
            near = kura_recall(self.store, self.models.thinker,
                               c.get("why") or c.get("topic", ""), hops=0, top=3, chars=1200)
        hints = "\n".join(f"- {n}" for n in (near.get("walked") or [])[:6])
        warn = ""
        if c.get("unverified_numbers"):
            warn += ("\n⚠️ This candidate claims a number with no tool output behind it. "
                     "**Write no numbers.**\n")
        if c.get("judgement"):
            warn += ("★ This is the AGENT'S OWN JUDGEMENT, not an outside fact. Write it in the "
                     "first person as a judgement. Do not launder it into the form of a fact — "
                     "the next agent will read it back as ground truth.\n")
        if "USER" not in c["classes"]:
            warn += ("⚠️ There is NOT ONE word of the human's in this candidate. Do not write "
                     "that they decided, chose, or instructed anything.\n")
        user = (f"CANDIDATE: {c.get('topic')}\nKIND: {c.get('kind')}\n"
                f"The distiller's reading (**not evidence — never cite it**): "
                f"{c.get('why')}\n{warn}\n"
                f"=== EVIDENCE (this is everything) ===\n{ev}\n\n"
                f"=== NEARBY MEMORIES (candidates for [[links]]) ===\n"
                f"{hints or '(nothing close)'}\n")
        # The scribe is a model: its finished text gets the same deterministic floor
        # as the candidate's quotes did. One retry with the violations named, then drop.
        for attempt in (1, 2):
            out = self.scribe(prompts.SCRIBE_SYS, user)
            if not out:
                return None
            slug = re.search(r"^SLUG:\s*(.+)$", out, re.M)
            title = re.search(r"^TITLE:\s*(.+)$", out, re.M)
            desc = re.search(r"^DESC:\s*(.+)$", out, re.M)
            body = re.search(r"^BODY:\s*\n(.*)$", out, re.S | re.M)
            if not (slug and desc and body):
                return None
            text = desc.group(1) + "\n" + body.group(1)
            # The floor sees everything that will be stored or indexed: the title
            # lands in MEMORY.md and the resident map, the curation sentences are
            # saved under the curation mark — all of it is model-written surface.
            _, s_ann, _ = _curation_of(out)
            cand_ann = " ".join(str(c.get(k) or "") for k in ANNOTATION_KEYS)
            surface = "\n".join([slug.group(1), (title.group(1) if title else ""), text,
                                 " ".join(s_ann.values()), cand_ann])
            bad = final_surface_violations(surface, c["evidence"], c["classes"])
            if not bad:
                break
            if attempt == 2:
                _log(f"      ✗ final surface fails the floor: {bad}")
                return None
            user += ("\n⚠️ REJECTED — fix these and answer again: "
                     f"{'; '.join(bad)}. A number must come from the evidence itself "
                     "(do not compute new ones); never credit the human without their "
                     "own quoted words.\n")
        _, plain = _split_draft(body.group(1))
        return self._draft_record(c, out, slug=_safe_slug(slug.group(1)),
                                  title=(title.group(1).strip()[:40] if title else ""),
                                  description=desc.group(1).strip()[:200], body=plain)

    def _draft_record(self, c: dict, out: str, *, slug: str, title: str, description: str,
                      body: str, extends: str | None = None) -> dict:
        """The record stage() writes, from a candidate and the scribe's answer.

        A new memory and an extension differ only in their first four keys; everything
        after them — what kind it is, the evidence it stands on, the gate's flags, the
        routing cues, the curation — must be identical, or a draft would carry one set
        of provenance down one path and another set down the other."""
        rec = {"slug": slug, "title": title, "description": description}
        if extends is not None:
            rec["extends"] = extends
        rec.update({
            "body": body,
            "kind": c.get("kind", "project"),
            "evidence": c["evidence"], "classes": c["classes"],
            "unverified_numbers": c.get("unverified_numbers", False),
            "judgement": c.get("judgement", False),
            # routing cues ride to the manifest untouched; they never enter the
            # body or the index — a callsign is a way BACK, not content
            "routing_cues": c.get("routing_cues") or [],
            "routing_cues_refused": c.get("routing_cues_refused") or {},
            **self._curate(c, out)})
        return rec

    def _curate(self, c: dict, out: str) -> dict:
        """Tags and the three sentences, from the brain's candidate and the scribe's
        output, verified against the evidence. The scribe's sentences win (it wrote the
        final text); tags are the union, and every claiming tag must earn its place."""
        s_tags, s_ann, problem = _curation_of(out)
        proposed = list(c.get("tags") or []) + s_tags
        try:
            normalize_tags(proposed)
        except ValueError as e:
            problem = (problem + "; " if problem else "") + f"candidate tags unreadable: {e}"
            proposed = s_tags
        kept, basis, refused = verify_tags(proposed, c["evidence"])
        ann = {k: c[k] for k in ANNOTATION_KEYS if isinstance(c.get(k), str) and c[k].strip()}
        ann.update(s_ann)
        if problem:
            _log(f"      ⚠ {problem}")
        return {"tags": list(kept), "tag_basis": basis, "tags_refused": refused,
                "annotations": ann, "curation_problem": problem}

    def _evidence_date(self) -> str:
        """The date the evidence carries: the journal file's last change, local time.
        Segments have no timestamps of their own, and the file's mtime is the one date
        nobody wrote from memory."""
        src = getattr(self, "_current_source", "")
        try:
            return datetime.fromtimestamp(os.path.getmtime(src)).strftime("%Y-%m-%d")
        except (OSError, ValueError, TypeError):
            return datetime.now().strftime("%Y-%m-%d")

    _DATE_IN_TEXT = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")

    def _compose_extension(self, c: dict) -> dict | None:
        target = c["extends"]
        existing = self.store.read(target)
        if not existing:
            return None
        ev = _evidence_lines(c["evidence"])
        date = self._evidence_date()
        out = self.scribe(prompts.EXTEND_SYS,
                          f"MEMORY TO EXTEND: {target}\nDATE: {date}\n"
                          f"The distiller's reading (not evidence): {c.get('extends_why')}\n\n"
                          f"=== ALREADY WRITTEN THERE (do not repeat) ===\n{existing[:9000]}\n\n"
                          f"=== NEW EVIDENCE (this is everything) ===\n{ev}\n")
        body = re.search(r"^BODY:\s*\n(.*)$", out or "", re.S | re.M)
        section = re.search(r"^SECTION:\s*(.+)$", out or "", re.M)
        if not body:
            return None
        _, plain = _split_draft(body.group(1))
        # The heading's date is the evidence's date, mechanically. 30 of 39 extension
        # headings in the house were dated before the distiller existed; one said 2025.
        head = section.group(1).strip() if section else f"## {date}"
        head = self._DATE_IN_TEXT.sub(date, head)
        if date not in head:
            head = f"## {date} " + head.lstrip("#").strip()
        # The floor runs AFTER the mechanical date stamp: the heading's date is
        # code's claim, so it rides in `allowed`; every other number — a "99x" in
        # the section, the body, the curation sentences — must be the evidence's.
        _, s_ann, _ = _curation_of(out or "")
        # The candidate's own sentences ride along too (_curate merges them below),
        # so they stand on the same floor as in the new-memory path: the extension
        # branch used to floor only the scribe's sentences, and an unbacked number
        # in the CANDIDATE's belongs_because slipped into the memory under the mark.
        cand_ann = " ".join(str(c.get(k) or "") for k in ANNOTATION_KEYS)
        bad = final_surface_violations("\n".join([head, plain, " ".join(s_ann.values()), cand_ann]),
                                       c["evidence"], c["classes"], allowed=date)
        if bad:
            _log(f"      ✗ extension surface fails the floor: {bad}")
            return None
        text = head + "\n" + plain
        return self._draft_record(c, out or "", slug=target, title="", description="",
                                  body=text.strip(), extends=target)

    # ── provenance that outlives the draft ───────────────────────────────
    #
    # The draft carries its evidence in a comment, and the draft is renamed `.poured`
    # and eventually swept. After that, a canonical memory has no way back to what it
    # was made from: which journal, which stretch of it, which quotes, which models.
    # "Why does this memory exist?" stops being answerable, which is the question the
    # whole evidence gate exists to keep answerable.
    #
    # So the manifest is content-addressed and written where memories live, not in the
    # workshop: `_evidence/<sha256>.json`, referenced from the memory's frontmatter.
    def _evidence_dir(self) -> str:
        return os.path.join(self.store.path, "_evidence")

    def _write_manifest(self, d: dict, source: str, key: str) -> str:
        manifest = {
            # The one number, and what each version added: GATE_VERSION, at the top
            # of this module. `_mark` signs the string derived from it.
            "gate_version": GATE_VERSION,
            "source_key": key,
            "source_file": os.path.basename(source),
            "source_sha256": self._source_digest(source),
            "kind": d.get("kind"),
            "evidence_classes": d.get("classes"),
            "quotes": d.get("evidence"),
            "unverified_numbers": d.get("unverified_numbers"),
            "judgement": d.get("judgement"),
            "tags": list(d.get("tags") or []),
            "recurrence_of": d.get("recurrence_of"),
            "tag_evidence": d.get("tag_basis") or {},
            "tags_refused": d.get("tags_refused") or {},
            "annotations": d.get("annotations") or {},
            "brain_model": self.models.brain.model,
            "scribe_model": self.models.scribe.model,
            "language": self.language,
            "created_at": datetime.now(timezone.utc).isoformat()[:19] + "Z",
        }
        # Routing cues are their OWN schema version, never folded into gate_version:
        # the envelope mark already binds the manifest's digest, and cue plumbing can
        # evolve without re-touching the gate's number. memory_slug is the slug this
        # provenance routes TO — set by code (compose/extends/covered), never by the
        # model's proposal.
        if d.get("routing_cues"):
            manifest["memory_slug"] = d.get("slug") or d.get("extends")
            manifest["routing_cues_version"] = 1
            manifest["routing_cues"] = d["routing_cues"]
            if d.get("routing_cues_refused"):
                manifest["routing_cues_refused"] = d["routing_cues_refused"]
        blob = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=1)
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        path = os.path.join(self._evidence_dir(), f"{digest}.json")
        if not os.path.exists(path):
            os.makedirs(self._evidence_dir(), exist_ok=True)
            tmp = path + f".tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(blob)
            os.replace(tmp, path)
        return digest

    @staticmethod
    def _source_digest(source: str) -> str:
        """Identify the journal itself, not just its basename — names collide."""
        try:
            h = hashlib.sha256()
            with open(source, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return ""

    # ── ⑥ stage ──────────────────────────────────────────────────────────
    def stage(self, d: dict, source: str) -> str:
        # Two candidates can compose to one slug (two Japanese titles reach the same
        # fallback name, or two English ones sanitise alike), and staging straight to
        # `<slug>.md` overwrote the draft already standing there — gate-passed work
        # gone with nothing logged. The second draft takes a numbered name instead.
        p = _free_path(self.drafts_dir, d["slug"])
        staged = os.path.basename(p)[:-3]
        ev = _evidence_lines(d["evidence"], limit=300, indent="  ")
        flags = ""
        if d.get("unverified_numbers"):
            flags += "   ⚠️unbacked number"
        if d.get("judgement"):
            flags += "   🧠the agent's judgement (not an outside fact)"
        if d.get("extends"):
            flags += f"   ↑extends {d['extends']}"
        # An EXTENDS draft is named after the memory it appends to, so a numbered file
        # name says nothing about the destination: provenance still routes to the
        # memory the pour will extend, not to the file the draft happens to sit in.
        manifest = self._write_manifest({**d, "slug": d.get("extends") or staged},
                                        source, getattr(self, "_current_key", ""))
        cur = ""
        if d.get("tags"):
            cur += f"TAGS: {json.dumps(list(d['tags']), ensure_ascii=False)}\n"
        for k in ANNOTATION_KEYS:
            if (d.get("annotations") or {}).get(k):
                cur += f"{k.upper()}: {d['annotations'][k]}\n"
        if d.get("tags_refused"):
            flags += "   ⊘tags refused: " + ", ".join(f"{t} ({w})" for t, w in d["tags_refused"].items())
        body = ((f"EXTENDS: {d['extends']}\n" if d.get("extends")
                 else f"TITLE: {d.get('title') or d['slug']}\nDESC: {d['description']}\n")
                + cur + f"\n{d['body']}\n")
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"<!-- distilled {datetime.now(timezone.utc).isoformat()[:19]}Z\n"
                    f"     source: {os.path.basename(source)}\n"
                    f"     kind: {d['kind']}   evidence classes: {','.join(d['classes'])}{flags}\n"
                    f"     gate: {self._mark(staged, d['kind'], manifest, body)}\n"
                    f"     evidence_manifest: sha256:{manifest}\n"
                    f"     evidence:\n{ev}\n-->\n" + body)
        # The mark signs the NAME, so the caller must hear the name that exists: a
        # draft reported under the slug it wanted would be looked up, and poured,
        # under a file that is not this one.
        d["slug"] = staged
        return p

    # ── the gate's mark ──────────────────────────────────────────────────
    #
    # `distiller-only` has to mean "this text passed the evidence gate", and file
    # existence in `_still/drafts/` is not that: a hand-written draft dropped in the
    # directory poured straight into a store whose direct door refuses everything.
    # So the gate signs what it staged, and the pour re-checks the signature.
    #
    # Honest about its limit: the key sits next to the drafts, so a principal who can
    # write the directory can usually read the key too. This stops an agent with a file
    # tool and an accident, not someone with the filesystem. The boundary there is still
    # permissions — docs/TRUST.md says so.
    def _gate_key(self) -> bytes:
        return self.store.gate_key()          # one key per store, shared with the curation mark

    @staticmethod
    def _draft_head(raw: str) -> str:
        """The comment header a draft carries — everything above `-->`.

        The envelope (kind, evidence manifest, gate mark) lives here and nowhere
        else, so every reader of it splits the same way, in one place."""
        return raw.split("-->")[0]

    @staticmethod
    def _draft_body(raw: str) -> str:
        return re.sub(r"<!--.*?-->\s*", "", raw, flags=re.S).strip()

    def _mark(self, slug: str, kind: str, manifest: str, body: str) -> str:
        # The signed surface is the whole ENVELOPE, not just the text — see
        # GATE_VERSION's changelog for what each version bound and why.
        blob = f"{GATE_FORMAT}\n{slug}\n{kind}\n{manifest}\n{body.strip()}"
        return hmac.new(self._gate_key(), blob.encode("utf-8"),
                        hashlib.sha256).hexdigest()[:32]

    _ENV_KIND = re.compile(r"kind:\s*(\w+)")
    _ENV_MAN = re.compile(r"evidence_manifest:\s*sha256:([0-9a-f]{64})")
    _ENV_MARK = re.compile(r"gate:\s*([0-9a-f]{32})")

    def _envelope_of(self, raw: str) -> tuple[str, str, str] | None:
        """(kind, manifest_hex, mark) from a draft's header — None if any is absent."""
        head = self._draft_head(raw)
        k = self._ENV_KIND.search(head)
        m = self._ENV_MAN.search(head)
        g = self._ENV_MARK.search(head)
        return (k.group(1), m.group(1), g.group(1)) if (k and m and g) else None

    def _draft_mark_valid(self, slug: str, raw: str) -> bool:
        env = self._envelope_of(raw)
        if env is None:
            return False
        kind, man, mark = env
        return hmac.compare_digest(mark, self._mark(slug, kind, man, self._draft_body(raw)))

    # ── ⑦ pour / drain ────────────────────────────────────────────────────
    def pour(self, slug: str) -> dict:
        # A draft is named by a bare slug. Joined as a path, `../../../out/o` read a file
        # from anywhere on the filesystem into the store and renamed the original.
        if slug != os.path.basename(slug) or slug in ("", ".", ".."):
            return {"ok": False, "why": "a draft is named by a bare slug, not a path"}
        p = os.path.join(self.drafts_dir, f"{slug}.md")
        if not os.path.exists(p):
            return {"ok": False, "why": "no such draft"}
        raw = open(p, encoding="utf-8").read()
        # The distiller no longer writes this flag: the final-surface floor refuses a
        # composed text that credits the human before a draft can ever be staged. It
        # stays here for the drafts the floor never saw — one written by hand into the
        # directory, or one carried over from an older distiller — because the answer
        # to "should this be a memory?" must not depend on which version staged it.
        if "🚫" in self._draft_head(raw):
            return {"ok": False, "why": "credits the human with no [USER] evidence; not poured"}
        if not self._draft_mark_valid(slug, raw):
            return {"ok": False,
                    "why": "this draft carries no valid gate mark — it was not staged by "
                           "the distiller, or its name, kind, manifest or body was "
                           "edited afterwards"}
        env = self._envelope_of(raw)
        if env and self.store.load_manifest_verified(env[1]) is None:
            return {"ok": False,
                    "why": "the evidence manifest this draft claims is missing or "
                           "tampered — provenance must exist before the memory does"}
        body = re.sub(r"<!--.*?-->\s*", "", raw, flags=re.S)
        kind = re.search(r"kind:\s*(\w+)", raw)
        head, add = _split_draft(body)
        title = ""
        # Curation lines were inside the signed text, so they are the distiller's own.
        # A TAGS line that does not parse here was not written by stage() — refuse
        # rather than pour a memory with a frontmatter nobody can read.
        try:
            tags = normalize_tags(head.get("TAGS") or [])
        except ValueError as e:
            return {"ok": False, "why": f"draft TAGS line unreadable: {e}"}
        ann = {k: head[k.upper()] for k in ANNOTATION_KEYS if head.get(k.upper())}
        if head.get("EXTENDS"):
            target = head["EXTENDS"]
            cur = self.store.read(target)
            m = re.match(r"^---\n.*?\n---\n(.*)$", cur, re.S)
            if not m:
                return {"ok": False, "why": f"cannot parse {target}"}
            dm = re.search(r"^description:\s*(.+)$", cur, re.M)
            slug_out = target
            desc = dm.group(1).strip().strip('"') if dm else target
            new_body = m.group(1).rstrip() + "\n\n" + add
        else:
            slug_out = slug
            desc = head.get("DESC") or slug
            title = head.get("TITLE", "")
            new_body = add
        # The pour has been through the gate, so it uses the verified door: a store set
        # to `distiller-only` accepts this and refuses a bare tool call.
        man = re.search(r"evidence_manifest:\s*(sha256:[0-9a-f]{64})", self._draft_head(raw))
        r = self.store.pour_verified(slug_out, desc, new_body,
                                     type_=kind.group(1) if kind else "project",
                                     title=title,
                                     meta={"evidence_manifest": man.group(1)} if man else None,
                                     tags=tags, annotations=ann)
        if r.get("ok"):
            # Numbered, like the staging side: a later draft of the same slug is
            # staged under the plain name again (the poured one no longer occupies
            # `<slug>.md`), and renaming straight onto `<slug>.md.poured` destroyed
            # the earlier draft's text, evidence header and mark with no trace.
            os.rename(p, _free_path(os.path.dirname(p), os.path.basename(p)[:-3],
                                    ".md.poured"))
            self._store_text = None
            # The route becomes real only now that the memory does. A staged draft
            # carried its cues as provenance; a TOSSed or quarantined one never
            # reaches this line, so no unpoured draft can grow a route. A receipt
            # that cannot be minted does NOT fail the pour — but it is never
            # silent: the result says so.
            cue_receipt = "none"
            if man:
                hexd = man.group(1).split("sha256:", 1)[-1]
                vm = self.store.load_manifest_verified(hexd)
                if vm and vm.get("routing_cues"):
                    from ..cues import CueLedger
                    res = CueLedger(self.store).issue(
                        memory_slug=slug_out,
                        evidence_manifest=f"sha256:{hexd}",
                        routing_cues=vm["routing_cues"],
                        accepted_via="extends" if head.get("EXTENDS") else "new")
                    cue_receipt = "issued" if res["ok"] else f"failed: {res['why']}"
                    if not res["ok"]:
                        _log(f"  ⚠ cue receipt refused for {slug_out} — {res['why']}")
            if cue_receipt != "none":
                r["cue_receipt"] = cue_receipt
            # The memory that replaces one exists only now, so this is the earliest
            # moment a retirement can point anywhere. An EXTENDS pours INTO the old
            # memory — there is no successor to point at — so it never retires.
            if man and not head.get("EXTENDS"):
                ret = self._retire_if_superseded(slug_out, man.group(1).split("sha256:", 1)[-1])
                if ret is not None:
                    r["retired"] = ret.get("old") if ret.get("ok") else False
        # `created` already means "the file did not exist"; naming the slug here too
        # overwrote that answer with a string.
        return {**r, "poured_into": slug_out, "extended": bool(head.get("EXTENDS"))}

    # ── the retirement face: a poured memory can retire the one it replaces ──
    #
    # The gate refuses `superseded` as a TAG — it is reserved for a forgetting pass
    # nobody has designed, and a model may not assign it. That refusal is NOT a signal
    # and is not read here: "the model proposed it" is not "the human said it", and
    # treating the gate's correct refusal as evidence resurrected exactly what the
    # gate threw away. The only trigger is `find_transition`: ONE surviving [USER]
    # quote that retires a memory this store holds AND names this new one as its
    # successor. `Store.retire` runs the same relation again; nothing here is trusted.
    def _retirement_target(self, man: dict, new_slug: str) -> dict | None:
        """The proof that a [USER] quote in this manifest retires an existing memory
        in favour of `new_slug`, or None.

        Old is resolved by exact slug or exact index title only, longest name first,
        so `k3-plan` is never retired because a quote happened to mention `k3`. No
        proof is no retirement — the distiller stays silent rather than guessing."""
        quotes = [q for q in (man.get("quotes") or [])
                  if isinstance(q, dict) and q.get("class") == "USER"]
        if not quotes:
            return None
        titles = {sl: t for t, sl in self.store.titles().items()}
        new = {"slug": new_slug, "title": titles.get(new_slug, "")}
        cands = sorted(((len(titles.get(sl, "") or sl), sl) for sl in self.store.slugs()
                        if sl != new_slug), reverse=True)
        for _, sl in cands:
            r = transition.find_transition(quotes, {"slug": sl, "title": titles.get(sl, "")},
                                           new)
            if r and r["kind"] == "superseded":
                return r
        return None

    def _retire_if_superseded(self, new_slug: str, hexd: str) -> dict | None:
        """→ the retirement result, or None when nothing was proven."""
        man = self.store.load_manifest_verified(hexd)
        if not man:
            return None
        proof = self._retirement_target(man, new_slug)
        if not proof:
            return None
        old = proof["old"]
        r = self.store.retire(old, new_slug, hexd)
        if r.get("ok") and r.get("changed"):
            _log(f"  ⌛ retired {old} — superseded by {new_slug}")
        elif not r.get("ok"):
            _log(f"  ⚠ not retired {old} — {r.get('error')}")
        return r

    def judge_draft(self, path: str) -> dict:
        raw = open(path, encoding="utf-8").read()
        head = self._draft_head(raw)
        body = self._draft_body(raw)
        slug = os.path.basename(path)[:-3]
        judged_sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        # ask() collapses every infrastructure failure to None (unreachable, timeout,
        # HTTP error). Judging on "" would read "the model was down" as "the scribe
        # did not keep the shape" — a TOSS that DELETES a gate-passed draft. No
        # answer is not a verdict.
        out = self.scribe(prompts.POUR_SYS,
                          f"=== DRAFT: {slug} ===\n{body}\n\n"
                          f"=== EVIDENCE AND FLAGS (set by the distiller) ===\n{head}", 1600)
        if not (out or "").strip():
            return {"slug": slug, "verdict": "SKIP", "judged_sha": judged_sha,
                    "why": "the scribe was unreachable or answered nothing — not a verdict"}
        first = (out.splitlines() or [""])[0].upper()
        v = next((x for x in ("POUR", "FIX", "TOSS") if x in first), None)
        rm = re.search(r"^reason[:：]\s*(.+)$", out, re.M | re.I) if v else None
        why = rm.group(1) if rm else ""
        m = re.search(r"^BODY:\s*\n(.*)$", out, re.S | re.M)
        bb = re.search(r"^BELONGS_BECAUSE:\s*(.+)$", out.split("BODY:", 1)[0], re.M)
        return {"slug": slug, "verdict": v or "TOSS", "judged_sha": judged_sha,
                "why": (why or "the scribe did not keep the shape")[:160],
                "new_body": m.group(1).strip() if m else None,
                "belongs_because": bb.group(1).strip() if bb else None}

    def _manifest_evidence(self, draft_raw: str) -> tuple[list[dict] | None, list[str]]:
        """The draft's FULL evidence, from its content-addressed manifest.

        The header's own evidence lines are truncated for human eyes (300 chars);
        re-verification needs the real quotes, and the manifest has them. No
        manifest, no re-signing — fail closed."""
        m = re.search(r"evidence_manifest:\s*sha256:([0-9a-f]{64})", draft_raw)
        if not m:
            return None, []
        man = self.store.load_manifest_verified(m.group(1))
        if man is None:
            return None, []
        return list(man.get("quotes") or []), list(man.get("evidence_classes") or [])

    def drain(self, limit: int = 0) -> dict:
        ds = sorted(glob.glob(os.path.join(self.drafts_dir, "*.md")))
        if limit:
            ds = ds[:limit]
        if not ds:
            return {"ok": True, "why": "no drafts"}
        # The judge must never be a mint: a draft whose mark is invalid for its
        # CURRENT name (renamed, or header-edited) is not judged at all — a FIX
        # re-signs, and re-signing laundered a stolen identity into a valid one.
        signed, pre_quarantined = [], []
        for pth in ds:
            s0 = os.path.basename(pth)[:-3]
            raw0 = open(pth, encoding="utf-8").read()
            if self._draft_mark_valid(s0, raw0):
                signed.append(pth)
                continue
            # Mechanically, without a model: judging an unsigned draft would let a
            # FIX mint a fresh mark for a stolen name — the judge must never be a
            # mint. And an invalid mark means "origin unprovable", not "content
            # unwanted" — so the draft is QUARANTINED, never destroyed: moved
            # aside atomically where nothing judges, fixes or pours it, but a
            # person can still look.
            qdir = os.path.join(self.still, "quarantine")
            os.makedirs(qdir, exist_ok=True)
            qp = _free_path(qdir, s0)
            os.replace(pth, qp)
            with open(os.path.join(self.still, "tossed.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"slug": s0, "verdict": "QUARANTINE",
                                    "why": "no valid mark for its current name/kind/manifest",
                                    "moved_to": qp,
                                    "at": datetime.now(timezone.utc).isoformat()[:19]},
                                   ensure_ascii=False) + "\n")
            pre_quarantined.append(s0)
            _log(f"  ⊘ quarantine {s0} — no valid mark for its current name; never judged")
        ds = signed
        if not ds:
            return {"ok": True, "poured": 0, "fixed": 0, "tossed": 0, "retired": 0,
                    "quarantined": len(pre_quarantined),
                    "left": len(glob.glob(os.path.join(self.drafts_dir, "*.md")))}
        _log(f"drain: {len(ds)} drafts, {self.slots} at a time")
        poured, fixed, tossed, retired = [], [], [], []
        skipped, unparsed = [], []          # not verdicts: left staged, loudly
        quarantined = pre_quarantined
        with ThreadPoolExecutor(max_workers=self.slots) as pool:
            for j in pool.map(self.judge_draft, ds):
                p = os.path.join(self.drafts_dir, j["slug"] + ".md")
                # The verdict binds the BYTES that were judged: another drain may
                # have fixed this draft while the model thought. Moved → next drain.
                try:
                    now_sha = hashlib.sha256(open(p, encoding="utf-8").read()
                                             .encode("utf-8")).hexdigest()
                except OSError:
                    _log(f"  ⚠ {j['slug']} vanished while judged; skipping")
                    continue
                if j.get("judged_sha") and now_sha != j["judged_sha"]:
                    _log(f"  ⚠ {j['slug']} moved while judged; verdict discarded")
                    continue
                if j["verdict"] == "SKIP":
                    # The judge could not judge (unreachable or empty): the draft
                    # stays exactly as staged for the next drain. Deleting on an
                    # infrastructure failure is how a quiet outage empties the queue.
                    _log(f"  ⏸ skip  {j['slug']} — {j['why'][:70]}")
                    skipped.append(j["slug"])
                    continue
                if j["verdict"] == "TOSS":
                    with open(os.path.join(self.still, "tossed.jsonl"), "a", encoding="utf-8") as f:
                        f.write(json.dumps({**j, "at": datetime.now(timezone.utc).isoformat()[:19],
                                            "body": open(p, encoding="utf-8").read()[:4000]},
                                           ensure_ascii=False) + "\n")
                    os.remove(p)
                    tossed.append(j["slug"])
                    _log(f"  ✗ toss {j['slug']} — {j['why'][:70]}")
                    continue
                if j["verdict"] == "FIX":
                    if not j.get("new_body"):
                        # A FIX whose BODY section did not parse is NOT a POUR: the
                        # judge said part of the text goes beyond the evidence, and
                        # pouring the draft as staged would file exactly that part.
                        # It stays staged for the next drain to judge cold.
                        _log(f"  ⚠ fix unparsed {j['slug']} — no BODY: section; left staged")
                        unparsed.append(j["slug"])
                        continue
                    raw = open(p, encoding="utf-8").read()
                    # Every header line survives a FIX, not just the first one: keeping
                    # only TITLE dropped DESC, and the memory poured with its slug as
                    # the index trigger. The scribe rewrote the BODY, nothing else.
                    hd, _ = _split_draft(self._draft_body(raw))
                    if j.get("belongs_because"):
                        hd["BELONGS_BECAUSE"] = j["belongs_because"]
                    keep_head = [f"{k}: {v}" for k, v in hd.items()]
                    body = ("\n".join(keep_head) + "\n\n" if keep_head else "") + j["new_body"] + "\n"
                    # The judge is the LAST model to touch this text, so the door
                    # stands behind it too: the mark is a proof of having passed the
                    # floor, and a FIX that cannot pass does not get re-signed — the
                    # draft stays as staged, for the next drain to judge cold.
                    ev, classes = self._manifest_evidence(raw)
                    if ev is None:
                        _log(f"  ⚠ fix refused {j['slug']} — no readable evidence manifest")
                        continue
                    bad = final_surface_violations(f"{j['slug']}\n{body}", ev, classes)
                    if bad:
                        _log(f"  ⚠ fix refused {j['slug']} — {'; '.join(bad)[:90]}")
                        continue
                    env = self._envelope_of(raw)
                    keep = re.sub(r"gate:\s*[0-9a-f]{32}",
                                  f"gate: {self._mark(j['slug'], env[0], env[1], body)}",
                                  self._draft_head(raw)) + "-->\n"
                    open(p, "w", encoding="utf-8").write(keep + body)
                    fixed.append(j["slug"])
                    _log(f"  ✎ fix  {j['slug']} — {j['why'][:70]}")
                r = self.pour(j["slug"])
                if r.get("retired"):
                    retired.append(r["retired"])
                if r.get("ok"):
                    poured.append(j["slug"])
                    _log(f"  ○ pour {j['slug']}")
                else:
                    _log(f"  ⚠ not poured {j['slug']} — {r.get('why') or r.get('error')}")
        out = {"ok": True, "poured": len(poured), "fixed": len(fixed), "tossed": len(tossed),
               "quarantined": len(quarantined), "skipped": len(skipped),
               "fix_unparsed": len(unparsed), "retired": len(retired),
               "left": len(glob.glob(os.path.join(self.drafts_dir, "*.md")))}
        # A transition is a change to canonical the run's numbers must carry: a face
        # appearing on the map with nothing in the metrics is a change nobody can add up.
        self._metric({"op": "drain", **{k: v for k, v in out.items() if k != "ok"},
                      "retired_slugs": retired})
        return out

    # ── ⑧ index hygiene ──────────────────────────────────────────────────
    @staticmethod
    def _bad_index_line(title: str, desc: str, slug: str) -> str | None:
        """Only mechanically detectable rot. Whether a line is GOOD is the scribe's call."""
        if len(title) > 40:
            return "title too long"
        if desc.startswith(title.rstrip()) and len(title) >= 20:
            return "title is just the head of the description"
        if title == slug and len(slug) > 24:
            return "title is still the raw slug"
        if re.search(r"(について|の話|に関する|重要な知見)$", desc.strip()) or \
           re.search(r"\b(notes on|about|important findings)\b\s*$", desc.strip(), re.I):
            return "trigger would fit any memory"
        if len(desc) < 12:
            return "trigger too short to recognise"
        return None

    def tidy(self, limit: int = 6) -> dict:
        # The index is read on every recall and every prefill, and this is the only path
        # that puts MODEL-authored prose into it. On a frozen store it must not run.
        if self.store.write_policy == FROZEN:
            return {"ok": False, "why": f"store '{self.store.name}' is frozen: "
                                        f"the index is not repaired"}
        lines = self.store.index_text().splitlines()
        targets = []
        for i, l in enumerate(lines):
            m = re.match(r"- \[([^\]]+)\]\(([A-Za-z0-9_/-]+)\.md\) — (.+)", l)
            if not m:
                continue
            why = self._bad_index_line(m.group(1), m.group(3), m.group(2))
            if why:
                targets.append((i, m.group(2), why))
        if not targets:
            return {"ok": True, "why": "no ragged index lines"}
        _log(f"tidy: {len(targets)} ragged lines (fixing up to {limit})")
        repl: list[tuple[str, str, str, str]] = []
        for i, slug, why in targets[:limit]:
            body = self.store.read(slug)[:6000]
            if not body:
                continue
            out = self.scribe(prompts.TIDY_SYS,
                              f"slug: {slug}\nwhat is wrong with the current line: {why}\n\n"
                              f"=== THE MEMORY ===\n{body}", 300) or ""
            mt = re.search(r"^TITLE:\s*(.+)$", out, re.M)
            md = re.search(r"^DESC:\s*(.+)$", out, re.M)
            if not (mt and md):
                continue
            # This is the only path that puts model prose into the canonical index,
            # and the index feeds recall AND the resident map — so it wears the same
            # numeric floor as every other model-written surface. The memory itself
            # (plus the line being replaced) is the evidence.
            derived = mt.group(1) + "\n" + md.group(1)
            bad = composed_number_violations(derived, [{"text": body}, {"text": lines[i]}])
            if attributes_to_human(derived, []) and not attributes_to_human(
                    body + " " + lines[i], []):
                bad = bad + ["credits the human where the memory does not"]
            if bad:
                _log(f"  ⚠ tidy refused {slug} — {bad}")
                continue
            repl.append((lines[i],
                         f"- [{mt.group(1).strip()[:40]}]({slug}.md) — {md.group(1).strip()[:200]}",
                         slug, why, body))
        fixed = skipped_stale = 0
        if repl:
            # The model calls above ran on a SNAPSHOT, and a pour may have landed
            # meanwhile — writing the snapshot back would erase its line (the exact
            # `not_in_index` wound doctor keeps finding). So: re-read under the
            # lock and merge line by line; a target line that no longer exists as
            # read is stale and is skipped, never guessed at.
            with self.store._locked():
                cur = self.store.index_text().splitlines()
                for old_line, new_line, slug, why, body_snap in repl:
                    if self.store.read(slug)[:6000] != body_snap:
                        skipped_stale += 1
                        _log(f"  ⚠ tidy skipped {slug} — the memory changed while the model wrote")
                        continue
                    try:
                        cur[cur.index(old_line)] = new_line
                    except ValueError:
                        skipped_stale += 1
                        _log(f"  ⚠ tidy skipped {slug} — the line moved while the model wrote")
                        continue
                    fixed += 1
                    _log(f"  ✎ {slug} — {why}")
                if fixed:
                    self.store._write_index("\n".join(cur) + "\n")
        return {"ok": True, "fixed": fixed, "skipped_stale": skipped_stale,
                "still_ragged": len(targets) - fixed}

    # ── the pass ─────────────────────────────────────────────────────────
    def files(self, session: str | None = None) -> list[str]:
        fs = discover_all(self.journals, exclude_roots=self.exclude_roots)
        return [f for f in fs if session in f] if session else fs

    def catch_up(self) -> dict:
        """Mark every journal as already drunk, up to where it stands now.

        Pointing a distiller at a history it has never seen means drinking all of it —
        for a year-old journal that is days of model time to re-learn what the store
        may already know. This says "start from today" without deleting anything: the
        marks move forward only (`advance` takes a max), so a journal that was already
        further along is untouched.
        """
        moved, seen = {}, 0
        for path in self.files():
            src = source_for(path)
            if not src:
                continue
            k = src.key(path)
            pos = 0
            end = 0
            pending_passes = 0
            size = os.path.getsize(path)
            while True:
                end, _, scan_pending = call_claim_bound(src, path, pos, 1 << 40)
                if scan_pending <= 0:
                    break
                if end > pos:
                    pos = end
                    pending_passes = 0
                    continue
                pending_passes += 1
                if pending_passes > size // SCAN_LIMIT + 16:
                    end = pos
                    break
            before = self.marks.read().get(k, 0)
            seen += 1
            if end > before:
                self.marks.advance(k, end)
                moved[k] = end
        return {"ok": True, "journals": seen, "moved": len(moved), "at": moved}

    def sip_one(self, session: str | None = None
                ) -> tuple[list[Segment], str, str] | SipPending | None:
        c = self.marks.claim(self.files(session), self.chunk_chars, MIN_DRINK)
        if not c:
            return None
        path, start, bound_end, src = c.path, c.start, c.end, c.source
        if c.scan_pending:
            return SipPending(path, src.key(path), c.scan_pending)
        report = IntakeReport()
        segs, nxt = call_sip(src, path, start, self.chunk_chars,
                             report=report, bound_end=bound_end)
        self.marks.advance(src.key(path), nxt)
        self._emit_intake(path, report)
        return segs, path, src.key(path)

    def _emit_intake(self, path: str, report: IntakeReport) -> None:
        """One bounded summary per sip. A diagnostic must never break a pass,
        print evidence text, or grow without a cap — samples already are."""
        if not report.skipped:
            return
        try:
            _log(f"intake: {os.path.basename(path)} skipped {report.total} "
                 f"({', '.join(f'{k}={v}' for k, v in report.skipped.items())})")
            row = {"source": os.path.basename(path), **report.as_dict()}
            os.makedirs(self.still, exist_ok=True)
            with open(os.path.join(self.still, "intake.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass                        # a diagnostic must never break a pass

    def _metric(self, row: dict) -> None:
        """One line per batch, so the pipeline's behaviour is a measurement rather than
        an impression. Nothing here is a claim: it is what happened, in numbers someone
        else can add up."""
        row = {**row, "at": datetime.now(timezone.utc).isoformat()[:19],
               "store": self.store.name,
               "brain_model": self.models.brain.model,
               "scribe_model": self.models.scribe.model,
               "max_items": self.max_items, "chunk_chars": self.chunk_chars}
        try:
            os.makedirs(self.still, exist_ok=True)
            with open(os.path.join(self.still, "metrics.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass                        # a metric must never break a pass

    def run(self, session: str | None = None, chunks: int = 1) -> dict:
        made, killed, covered, sown, recurred = [], 0, 0, 0, 0
        cue_receipts = cue_receipt_failures = 0
        scan_pending_bytes = 0
        for _ in range(chunks):
            got = self.sip_one(session)
            if not got:
                break
            if isinstance(got, SipPending):
                scan_pending_bytes += got.scan_pending_bytes
                continue
            segs, path, key = got
            self._current_key = key
            self._current_source = path
            if not segs:
                continue
            by: dict[str, int] = {}
            for s in segs:
                by[s.cls] = by.get(s.cls, 0) + 1
            raw_chars = sum(len(s.text) for s in segs)
            _log(f"drink: {key[:40]} → {len(segs)} segments {by}")

            t0 = time.time()
            cands = self.spot(segs)
            _log(f"  brain found {len(cands)} candidates ({time.time()-t0:.1f}s)")
            kept, dropped, ideas = gate(cands, segs, self.store_text())
            for i in ideas:
                self.seeds.sow(f"{i.get('topic')} — {i.get('why')}", "brain/spot")
                _log(f"    🌱 seed: {i.get('topic')} — {str(i.get('why'))[:70]}")
            sown += len(ideas)
            killed += len(dropped)
            for d in dropped:
                _log(f"    ✗ {d.get('topic')} — {d['why_dropped']}")
                with open(os.path.join(self.still, "dropped.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps({**d, "at": datetime.now(timezone.utc).isoformat()},
                                       ensure_ascii=False) + "\n")

            to_write = []
            for c in kept:
                near = kura_recall(self.store, self.models.thinker,
                                   c.get("why") or c["topic"], hops=0, top=3, chars=1200)
                verdict, why, target = self.novelty(c, near)
                _log(f"    ○ {c['topic']} [{','.join(c['classes'])}] → {verdict} {target or ''}")
                if verdict == "COVERED":
                    covered += 1
                    rec = self.recur(c, target, key, path) if target else "no target named"
                    if rec == "tagged":
                        recurred += 1
                        _log(f"      ↺ recurred: {target}")
                    elif rec != "already":
                        _log(f"      · not marked recurred — {rec}")
                    if c.get("routing_cues") and target in self.store.slug_set():
                        # Memory novelty is COVERED; ROUTING novelty may still be NEW:
                        # the store already says this, but the human just used a word
                        # for it the store had never heard. The cue and its provenance
                        # are recorded against the EXISTING slug (code-chosen, never
                        # the model's) — and nothing else moves: no memory body, no
                        # index line, not one canonical byte. The RECEIPT is what makes
                        # it a route; the manifest alone is provenance, not authority.
                        mdigest = self._write_manifest(
                            {"slug": target, "kind": c.get("kind"),
                             "evidence": c["evidence"], "classes": c["classes"],
                             "routing_cues": c["routing_cues"],
                             "routing_cues_refused": c.get("routing_cues_refused") or {}},
                            path, key)
                        from ..cues import CueLedger
                        res = CueLedger(self.store).issue(
                            memory_slug=target, evidence_manifest=f"sha256:{mdigest}",
                            routing_cues=c["routing_cues"], accepted_via="covered")
                        if res["ok"]:
                            cue_receipts += 1
                            _log(f"      ⇢ cue kept for COVERED {target}: "
                                 f"{[x['text'] for x in c['routing_cues']]}")
                        else:
                            # A route that cannot be minted is silence — but never
                            # silent ABOUT it: the run's numbers and log both say so.
                            cue_receipt_failures += 1
                            _log(f"      ⚠ cue receipt refused for COVERED {target} — "
                                 f"{res['why']}")
                    with open(os.path.join(self.still, "dropped.jsonl"), "a", encoding="utf-8") as f:
                        f.write(json.dumps({**{k: v for k, v in c.items() if k != "evidence"},
                                            "why_dropped": f"COVERED by {target}", "reason": why,
                                            "recurred": rec,
                                            "at": datetime.now(timezone.utc).isoformat()},
                                           ensure_ascii=False) + "\n")
                    continue
                if verdict == "EXTENDS":
                    c = {**c, "extends": target, "extends_why": why}
                self.sprout(c)
                to_write.append((c, near))

            drafted, draft_chars, draft_text = [], 0, []
            if to_write:
                t1 = time.time()
                with ThreadPoolExecutor(max_workers=self.slots) as pool:
                    for d in pool.map(lambda cn: self.compose(*cn), to_write):
                        if not d:
                            _log("      the scribe did not keep the shape")
                            continue
                        _log(f"      wrote {d['slug']} → {os.path.basename(self.stage(d, path))}")
                        made.append(d["slug"])
                        drafted.append(d["slug"])
                        draft_chars += len(d.get("body", ""))
                        draft_text.append(d.get("body", ""))
                _log(f"      {len(to_write)} composed in {time.time()-t1:.0f}s")
            self._metric({
                "source_key": key, "segments": len(segs), "by_class": by,
                "raw_chars": raw_chars, "raw_tokens_est": estimate(as_evidence(segs)),
                "candidates": len(cands), "gated_kept": len(kept),
                "gated_dropped": len(dropped), "ideas": len(ideas),
                "covered": covered, "recurred": recurred, "drafts": drafted,
                "cue_receipts": cue_receipts, "cue_receipt_failures": cue_receipt_failures,
                "draft_chars": draft_chars,
                "draft_tokens_est": estimate("\n".join(draft_text)),
                "index_tokens_est": estimate(self.store.index_text()),
            })
        if not made and not killed and not covered and not sown:
            if scan_pending_bytes:
                return {"ok": True, "scan_pending_bytes": scan_pending_bytes}
            return {"ok": True, "why": "nothing worth drinking"}
        return {"ok": True, "drafts": made, "dropped": killed, "covered": covered,
                "recurred": recurred, "seeds": sown,
                "cue_receipts": cue_receipts, "cue_receipt_failures": cue_receipt_failures}

    def night(self, idle_min: float = 20.0, poll_s: float = 30.0) -> None:
        """Run a pass whenever the journals have been quiet long enough. Never gets in
        the way of the foreground."""
        _log(f"distiller watching (a pass after {idle_min} min of quiet)")
        last = None
        while True:
            time.sleep(poll_s)
            fs = self.files()
            if not fs:
                continue
            newest = max(fs, key=os.path.getmtime)
            if time.time() - os.path.getmtime(newest) < idle_min * 60:
                last = None
                continue
            stamp = int(os.path.getmtime(newest))
            if last == stamp:
                time.sleep(600)          # already did a pass in this silence
                continue
            try:
                result = self.run(chunks=1)
                _log(f"  {result}")
            except Exception as e:       # a bad pass must not end the watch
                _log(f"  pass failed: {type(e).__name__}: {e}")
                result = {}
            if result.get("scan_pending_bytes"):
                continue                 # bounded discard progress — retry same quiet
            try:
                _log(f"  {self.drain()}")
            except Exception as e:       # a bad drain must not end the watch
                _log(f"  drain failed: {type(e).__name__}: {e}")
            last = stamp


def drafts_of(store: Store) -> list[tuple[str, str, str]]:
    """(slug, evidence classes, trigger) for every staged draft — a listing for a
    person, not a gate: it reads the file as it stands and checks no mark."""
    out = []
    for p in sorted(glob.glob(os.path.join(_drafts_dir(store.still), "*.md"))):
        t = open(p, encoding="utf-8").read()
        d = re.search(r"^DESC:\s*(.+)$", t, re.M)
        cls = re.search(r"evidence classes:\s*(\S+)", t)
        out.append((os.path.basename(p)[:-3], cls.group(1) if cls else "?",
                    (d.group(1) if d else "")[:100]))
    return out
