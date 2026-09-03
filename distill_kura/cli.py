"""`kura` — one command for every store in the registry.

    kura serve                        open the mouth (all stores, one port)
    kura stores                       what exists, which mode maps where
    kura recall "question" [-s eq]    recall by hand
    kura glance <slug> [-s eq]        confirm one exact memory for ~150 tokens
    kura remember slug "desc" [-]     write one fact (body on stdin with `-`)
    kura annotate slug --tag landmine add tags / the three sentences to one memory
    kura profile show|draft|apply     the wide room's learned profile (draft → read → apply by hand)
    kura tend [-s eq]                 the watcher: quiet hours — drain → distil → weave → trail → pay-forward → tidy
    kura doctor [-s eq]               health of a store (--all for every one)
    kura weave [-s eq] [--status]     re-weave the resident index (three-layer cloth)
    kura prefill [-s eq]            print the standing block a host should inject
    kura trail [-s eq]              rebuild the Hot Trail appended after the map
    kura constellation [-s eq]      the sector map: which `## ` heading holds what
    kura edges [-s eq] [--slug S]   typed worldline edges — derived routing state
    kura pay-forward [-s eq]          bake the map into each mouth's KV slot, save it to disk
    kura bench compress|retention     what the store cost / is what mattered still findable
    kura bench payforward --mouth N   what the pay-forward spine buys, priced by the mouth
    kura bench packets --out FILE     freeze gate-passed candidates for a writer A/B run
    kura bench writers --packets FILE --out DIR
                                      compare configured or explicitly named writers
    kura metrics richness [-s eq]     did it stop remembering LIES or stop remembering (§15)
    kura init <name> --path DIR       create a store and print the TOML to paste
    kura distill catchup [-s eq]      start from today: mark every journal drunk up to now
    kura distill run [-s eq]          one pass: drink → spot → gate → write drafts
    kura distill drafts|pour|drain|tidy|sip   inspect / pour / repair the index, or sip one draft
    kura distill night                stay resident, distil in the quiet

Exit code 2 means "there was nothing to do". A scheduler needs that distinct from 0,
or a watchdog spins on an empty queue and starves the steps that need idle time.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .glance import glance as do_glance
from .recall import recall as do_recall
from .registry import Registry
from .server import serve
from .store import ANNOTATION_KEYS, Store



def _annotation_args(p) -> None:
    p.add_argument("--tag", action="append", default=[],
                   help="a word describing the memory (repeatable). Describes; never ranks")
    p.add_argument("--belongs-because", help="one sentence: why it belongs in THIS kura")
    p.add_argument("--keep", help="one sentence: the meaning that must survive")
    p.add_argument("--may-fade", help="one sentence: the detail that may thin out later")


def _annotations_of(a) -> dict:
    # The three names live in store.ANNOTATION_KEYS; argparse's dests already match
    # them, so a fourth sentence never has to be added here as well.
    return {k: getattr(a, k) for k in ANNOTATION_KEYS if getattr(a, k, None)}


def _reg(a) -> Registry:
    return Registry.load(a.config)


def _store(reg: Registry, sel: str | None) -> Store:
    try:
        return reg.store(sel)
    except KeyError:
        sys.exit(f"unknown store or mode: {sel!r}. known: {sorted(reg.stores)} "
                 f"modes: {reg.modes}")


def _distiller(reg: Registry, store: Store):
    from .distill import Distiller
    return Distiller(reg, store)


_WL_COLS = ("runnable", "target_reached", "wrong_branch", "obsolete_branch",
            "honest_unknown", "remembered_but_unreachable", "unnecessary_opens",
            # Why the format errors happened, beside how many: a run cut by the
            # token cap and a run that thought instead of answering need opposite
            # repairs, and one count cannot tell them apart.
            "truncated", "reasoning_only",
            "thinker_calls_total", "opened_mean")


def _worldline_table(r: dict) -> str:
    """Per resident variant, the map's size and the raw counts side by side — the
    guide's §9 comparison in one glance. Counts, not a score: a column that went
    up and a column that went down are meant to be read together."""
    head = ["variant", "resident_tokens", *_WL_COLS]
    rows = [head]
    for name, v in r.get("variants", {}).items():
        sm = v["summary"]
        rows.append([name, str(v["resident_tokens"]), *[str(sm.get(c, "")) for c in _WL_COLS]])
    widths = [max(len(row[i]) for row in rows) for i in range(len(head))]
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(row)) for row in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    # The case-set digest rides the header, not a footnote: a table read without
    # it cannot say whether it may be compared with the table beside it.
    sha = r.get("case_set_sha") or ""
    return (f"worldline  store={r['store']}  routing={r['routing']}  cases={r['cases']}"
            + (f"  case_set_sha={sha[:12]}" if sha else "") + "\n"
            + "\n".join(lines))


_WL_PAIRED_COLS = ("cases", "target_reached", "wrong_branch", "obsolete_branch",
                   "remembered_but_unreachable")


def _worldline_paired_table(r: dict) -> str:
    """The same counts over the cases every variant answered in a readable format.
    A variant that garbles what it finds hard scores those rows as failures for
    itself and never for its rival, so the all-cases table can move for a reason
    that has nothing to do with the map. This is the table to promote on."""
    paired = r.get("paired_valid") or {}
    if not paired:
        return ""
    head = ["variant", *_WL_PAIRED_COLS]
    rows = [head] + [[name, *[str(v.get(c, "")) for c in _WL_PAIRED_COLS]]
                     for name, v in paired.items()]
    widths = [max(len(row[i]) for row in rows) for i in range(len(head))]
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(row)) for row in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "paired-format-valid  (cases readable in EVERY variant)\n" + "\n".join(lines)


def _worldline_compare_text(c: dict) -> str:
    """Two runs side by side. Recovery twice, the safety counts with their signs,
    and no composite — the reading is 'recovery rose and none of these did'."""
    def pct(v) -> str:
        return "—" if v is None else f"{v:.3f}"

    def signed(n: int) -> str:
        return f"{n:+d}"

    out = [f"worldline-compare  case_set_sha={(c['case_set_sha'] or '')[:12]}  "
           f"A={c['a']}  B={c['b']}",
           f"paired-format-valid cases (valid in every variant of BOTH runs): "
           f"{c['paired_valid_cases']}"]
    for name, v in c["variants"].items():
        al, pv = v["all_cases"], v["paired_valid"]
        out.append(f"\n[{name}]")
        out.append(f"  all cases          recovery A {pct(al['recovery_a'])} "
                   f"({al['runnable_a']} runnable)  B {pct(al['recovery_b'])} "
                   f"({al['runnable_b']} runnable)")
        out.append(f"  paired valid       recovery A {pct(pv['recovery_a'])}  "
                   f"B {pct(pv['recovery_b'])}   over {pv['cases']} cases")
        fe = v["format_error"]
        out.append(f"  format_error       A {fe['a']}  B {fe['b']}  "
                   f"delta {signed(fe['delta'])}")
        out.append("  safety (lower is better):")
        for k, d in v["safety"].items():
            out.append(f"    {k:<28} A {d['a']:>4}  B {d['b']:>4}  "
                       f"delta {signed(d['delta'])}")
    return "\n".join(out)


def _payforward_table(r: dict) -> str:
    """One line per condition: what the mouth said it reprocessed (prompt_n), how long
    the call took, and what the row varied. prompt_n IS the finding — a spine is warm
    when the number is the trail's size, cold when it is the map's."""
    def cell(v) -> str:
        return "—" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))
    head = ["condition", "prompt_n", "prompt_ms", "wall_s", "note"]
    rows = [head] + [[x["condition"], cell(x["prompt_n"]), cell(x["prompt_ms"]),
                      cell(x["wall_s"]), x["note"]] for x in r["rows"]]
    widths = [max(len(row[i]) for row in rows) for i in range(len(head))]
    lines = ["  ".join(c.ljust(widths[i]) if i < 4 else c
                       for i, c in enumerate(row)) for row in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return (f"bench payforward  mouth={r['mouth']}  store={r['store']}  etag={r['etag']}"
            f"  map={r['map_tokens_est']}t  trail={r['trail_tokens_est']}t ({r['trail']})\n"
            + "\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kura", description="distilled long-term memory for agents")
    ap.add_argument("-c", "--config", help="path to kura.toml (default: $KURA_CONFIG, "
                    "then ./kura.toml, then ~/.config/distill-kura/kura.toml)")
    ap.add_argument("-s", "--store", help="store or mode name (default: the configured default)")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("serve", help="run the HTTP service")
    p.add_argument("--port", type=int)
    p.add_argument("--host")

    sub.add_parser("stores", help="list stores, modes and model roles")

    p = sub.add_parser("recall", help="recall by meaning")
    p.add_argument("question")
    p.add_argument("--hops", type=int, default=1)
    p.add_argument("--top", type=int, default=3)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("glance", help="confirm one exact memory for ~150 tokens, before a full read")
    p.add_argument("slug")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("remember", help="write one fact")
    p.add_argument("slug")
    p.add_argument("description")
    p.add_argument("body", nargs="?", default="-", help="body text, or - to read stdin")
    p.add_argument("--title")
    p.add_argument("--type", default="project")
    _annotation_args(p)

    p = sub.add_parser("annotate", help="add tags / the three sentences to one memory")
    p.add_argument("slug")
    _annotation_args(p)

    p = sub.add_parser("retire", help="mark OLD as superseded by NEW — a person's act, "
                                      "with the evidence manifest that proves it")
    p.add_argument("old", help="the memory being retired (it is never deleted or hidden)")
    p.add_argument("new", help="the memory that replaces it")
    p.add_argument("--manifest", required=True,
                   help="the evidence manifest, sha256:<hex> or the bare hex; it must "
                        "carry a [USER] quote naming the old memory")

    p = sub.add_parser("profile", help="the learned profile of a store (the wide room)")
    psub = p.add_subparsers(dest="pcmd", required=True)
    psub.add_parser("show", help="state and text of profile.md, and whether a draft waits")
    psub.add_parser("draft", help="write _still/profile.draft.md from this store's memories")
    psub.add_parser("apply", help="copy the draft over profile.md — a person's act, never automatic")

    p = sub.add_parser("tend", help="the watcher: distil, pour, weave and tidy in the quiet hours")
    p.add_argument("--idle-min", type=float, default=None, help="minutes of journal silence before working (config: distill.idle_min, default 10)")
    p.add_argument("--backoff-min", type=float, default=None, help="rest after a track had nothing to do (config: distill.backoff_min, default 20)")
    p.add_argument("--poll", type=float, default=15.0, help="seconds between looks at the journal")
    p.add_argument("--no-yield", action="store_true", help="do not stop a running track when the human returns (editor on a separate seat)")
    p.add_argument("--once", action="store_true", help="one tick, then exit (for schedulers and tests)")
    p.add_argument("--timeout", type=float, default=3600.0,
                   help="--once only: seconds to wait for the track it started; when the "
                        "deadline passes the track is stopped and the run exits 1 (timeout)")

    p = sub.add_parser("doctor", help="health check")
    p.add_argument("--all", action="store_true", help="every store, not just one")

    p = sub.add_parser("weave", help="re-weave the resident index")
    p.add_argument("--status", action="store_true", help="report layers and size, weave nothing")
    p.add_argument("--adaptive", action="store_true",
                   help="also run the M4 shadow (shortest safe cue per memory); implied by "
                        "[prefill] adaptive_triggers = true")
    p.add_argument("--adaptive-out", metavar="PATH",
                   help="write the shadow RENDERED as a resident map to PATH (for "
                        "`kura bench worldline --resident-file adaptive=PATH`); never the cloth")
    p.add_argument("--fresh-days", type=float)
    p.add_argument("--trigger-tokens", type=int)
    p.add_argument("--no-model", action="store_true", help="trim mechanically, call no model")

    p = sub.add_parser("prefill", help="print the standing index block")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("trail", help="rebuild the Hot Trail — the recent-path block appended after the map")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("constellation",
                       help="the sector map: which `## ` heading holds what, and the invariant")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("edges",
                       help="typed worldline edges — derived routing state, read-only")
    p.add_argument("--json", action="store_true")
    p.add_argument("--slug", help="only this memory's outgoing and incoming edges")

    p = sub.add_parser("pay-forward", help="pay the map's cold prefill forward: bake it "
                                           "into each mouth's KV slot and save the slot to disk")
    p.add_argument("--mouth", help="only this mouth (by [[payforward.mouths]] name)")
    p.add_argument("--force", action="store_true", help="re-bake even when the etag says fresh")

    p = sub.add_parser("bench", help="measure, rather than claim")
    bsub = p.add_subparsers(dest="bcmd")
    b = bsub.add_parser("compress", help="store_ratio and map_ratio for a store")
    b.add_argument("--tokenizer-command",
                   help="a command that reads text on stdin and prints a token count. "
                        "Without one, figures are labelled `estimated`.")
    b.add_argument("--session", help="only batches whose source key contains this")
    b = bsub.add_parser("packets", help="freeze gate-passed candidates for writer A/B")
    b.add_argument("--store", dest="bench_store", help="store or mode to read")
    b.add_argument("--out", required=True, metavar="FILE", help="packet JSON output file")
    group = b.add_mutually_exclusive_group()
    group.add_argument("--chunks", type=int, default=None,
                       help="number of unprocessed journal chunks to freeze (default 1)")
    group.add_argument("--session", help="only journal paths containing this session text")
    b = bsub.add_parser("writers", help="compare writers on frozen candidate packets")
    b.add_argument("--store", dest="bench_store", help="store snapshot used for composition")
    b.add_argument("--packets", required=True, metavar="FILE")
    b.add_argument("--out", required=True, metavar="DIR", help="report and surface directory")
    b.add_argument("--writer", action="append", default=[], metavar="NAME=URL/MODEL",
                   help="writer endpoint; repeatable")
    b.add_argument("--from-config", action="store_true",
                   help="also use [[bench.writers]] from kura.toml")
    b = bsub.add_parser("payforward", help="what the pay-forward spine buys, one mouth: "
                                           "cold, restored spine + trail, changed trail, "
                                           "changed map, warm repeat")
    b.add_argument("--mouth", required=True, help="the [[payforward.mouths]] name to measure")
    b.add_argument("--json", action="store_true")
    b.add_argument("--skip-cold", action="store_true",
                   help="skip cold-full — the whole prefill with no cache, minutes on a CPU mouth")
    b = bsub.add_parser("retention", help="is what mattered still findable?")
    b.add_argument("--questions", default="bench/fixtures/questions.json")
    b.add_argument("--hops", type=int, default=1)
    b.add_argument("--verbose", action="store_true")
    b = bsub.add_parser("worldline", help="breadcrumb → shared world recovery (raw traces)")
    b.add_argument("--cases", default="bench/worldline/cases.json")
    b.add_argument("--routing", default="full", choices=["agent-only", "fastpath", "full"],
                   help="agent-only: the model reads the map alone; fastpath: tier zero "
                        "only, silence is silence; full: the production path")
    b.add_argument("--hops", type=int, default=1)
    b.add_argument("--trace", help="append JSONL traces here (default: <store>/_still/worldline-traces.jsonl)")
    b.add_argument("--agent-url", help="agent-only measures THIS model reading the map "
                   "alone; without it the configured thinker plays the agent")
    b.add_argument("--agent-model", help="model name for --agent-url (default: 'agent')")
    b.add_argument("--agent-key-env", help="NAME of the environment variable holding the "
                   "bearer key for --agent-url (the key itself never goes on a command line)")
    b.add_argument("--no-cues", action="store_true",
                   help="run the fastpath tier without the callsign pre-head — the "
                        "comparison that isolates what the shared vocabulary buys")
    b.add_argument("--resident", default="canonical",
                   help="resident-map variants to put the SAME cases in front of, "
                        "comma-separated: canonical (the full index), woven (the "
                        "production cloth, no model calls), or a name given by "
                        "--resident-file. Default: canonical")
    b.add_argument("--resident-file", action="append", default=[], metavar="NAME=PATH",
                   help="a text file to wear as the resident map under NAME (repeatable) "
                        "— how a map from a module that does not exist yet gets measured")
    b.add_argument("--json", action="store_true",
                   help="dump the full result with traces (default: a per-variant table)")
    b = bsub.add_parser("worldline-compare",
                        help="two `bench worldline --json` result files, side by side: "
                             "recovery over all cases and over the cases both runs "
                             "answered readably, with the safety counts. No score.")
    b.add_argument("a", help="the baseline result file (bench worldline --json > A.json)")
    b.add_argument("b", help="the candidate result file")
    b.add_argument("--json", action="store_true")

    p = sub.add_parser("metrics", help="read-only gauges over a store's _still logs")
    msub = p.add_subparsers(dest="mcmd")
    m = msub.add_parser("richness",
                        help="did the store stop remembering LIES, or stop "
                             "remembering? (plan §15) — candidate rate, rejection "
                             "reasons, evidence survival, unreachable, fallback; "
                             "pure aggregation, never writes")
    m.add_argument("--json", action="store_true")
    m.add_argument("--since", type=float, default=None, metavar="DAYS",
                   help="only rows from the last DAYS days")
    m.add_argument("--window", type=float, default=7.0, metavar="DAYS",
                   help="rolling window size, so a trend is visible (default 7)")

    p = sub.add_parser("init", help="create a new store")
    p.add_argument("name")
    p.add_argument("--path", required=True)
    p.add_argument("--label", default="")

    p = sub.add_parser("distill", help="the distiller")
    dsub = p.add_subparsers(dest="dcmd")
    d = dsub.add_parser("run"); d.add_argument("--session"); d.add_argument("--chunks", type=int, default=1)
    dsub.add_parser("drafts")
    d = dsub.add_parser("pour"); d.add_argument("slug", nargs="?"); d.add_argument("--all", action="store_true")
    d = dsub.add_parser("drain"); d.add_argument("-n", type=int, default=0)
    d = dsub.add_parser("tidy"); d.add_argument("-n", type=int, default=6)
    d = dsub.add_parser("night"); d.add_argument("--idle-min", type=float, default=20)
    dsub.add_parser("sip")
    dsub.add_parser("catchup", help="mark every journal as drunk up to now — start from today")

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 0

    if a.cmd == "init":
        st = Store(name=a.name, path=a.path, label=a.label or a.name)
        st.init_files()
        print(f"created {st.path}")
        print("\nadd this to kura.toml:\n")
        print(f'[stores.{a.name}]\npath = "{st.path}"\nlabel = "{st.label}"\n')
        print(f'[modes]\n{a.name} = "{a.name}"   # map an agent mode to this store')
        return 0

    reg = _reg(a)

    if a.cmd == "serve":
        serve(reg, a.host, a.port)
        return 0

    if a.cmd == "stores":
        print(json.dumps(reg.describe(), ensure_ascii=False, indent=1))
        return 0

    if a.cmd == "doctor":
        if a.all:
            print(json.dumps({n: s.doctor() for n, s in reg.stores.items()},
                             ensure_ascii=False, indent=1))
        else:
            print(json.dumps(_store(reg, a.store).doctor(), ensure_ascii=False, indent=1))
        return 0

    if a.cmd == "constellation":
        from . import constellation
        st = _store(reg, a.store)
        r = constellation.check(st)
        if a.json:
            print(json.dumps({**r, "sectors_detail": [
                {"name": sec.name, "count": len(sec.slugs), "titles": sec.titles}
                for sec in constellation.sectors(st)]}, ensure_ascii=False, indent=1))
            return 0
        for sec in constellation.sectors(st):
            line = f"- {sec.name} — {len(sec.slugs)} memories"
            if sec.titles:
                line += f" (e.g. {' / '.join(sec.titles)})"
            print(line)
        print(f"invariant: sum(sector counts) = {r['covered']} memories, "
              f"store holds {r['memories']} — "
              f"{'ok' if r['invariant_ok'] else 'BROKEN'}")
        return 0

    if a.cmd == "edges":
        from . import edges as edges_mod
        st = _store(reg, a.store)
        if a.slug:
            rows = edges_mod.edges_of(st, a.slug)
            if a.json:
                print(json.dumps(rows, ensure_ascii=False, indent=1))
                return 0
            for r in rows:
                arrow = "→" if r["direction"] == "out" else "←"
                print(f"{arrow} {r['type']} {r['other']}")
            return 0
        payload = edges_mod.current(st)        # read-only: the CLI never writes the cache
        if a.json:
            print(json.dumps(payload, ensure_ascii=False, indent=1))
            return 0
        for e in payload.get("edges", []):
            ev = f"   evidence: {e['evidence']}" if e.get("evidence") else ""
            print(f"{e['source']} -[{e['type']}]-> {e['target']}   cue: {e['cue']}{ev}")
        print(f"counts: {json.dumps(payload.get('counts', {}), ensure_ascii=False)}   "
              f"unevidenced: {payload.get('unevidenced', 0)}")
        if payload.get("dropped"):
            print(f"dropped: {json.dumps(payload['dropped'], ensure_ascii=False)}")
        return 0

    if a.cmd == "pay-forward":
        # Handled before the store default resolves: no `-s` means EVERY mouth, not
        # the default store's.
        from . import payforward
        if a.store:
            _store(reg, a.store)                # the shared loud unknown-store error
        try:
            r = payforward.run(reg, store=a.store, mouth=a.mouth, force=a.force)
        except KeyError as e:
            sys.exit(e.args[0])                 # a typo'd --mouth must not read as "all warm"
        for x in r["results"]:
            if x.get("error"):
                print(f"⚠ mouth '{x['mouth']}': {x['did']} — {x['error']}", file=sys.stderr)
        print(json.dumps(r, ensure_ascii=False))
        if r["failed"] or r["locked"]:
            # Checked FIRST: {A: baked, B: locked} must exit 1, or a scheduler that
            # saw "worked" would never come back for B. failed is broken, locked is
            # busy (another runner held the slot — maybe finishing an OLDER map);
            # either way part of the fleet is not covered, and retry outranks done.
            return 1
        if r["worked"]:
            return 0                            # the whole fleet is covered
        return 2                                # every mouth VERIFIED fresh — the scheduler may rest

    # Two finished result files, compared. Deliberately before the store is
    # resolved: the reading is entirely in the files, and demanding a live store
    # to read two JSON files would stop the comparison happening anywhere but the
    # machine that produced it.
    if a.cmd == "bench" and a.bcmd == "worldline-compare":
        from . import worldline as wl
        try:
            with open(a.a, encoding="utf-8") as f:
                ra = json.load(f)
            with open(a.b, encoding="utf-8") as f:
                rb = json.load(f)
            c = wl.compare(ra, rb, os.path.basename(a.a), os.path.basename(a.b))
        except (ValueError, OSError) as e:
            sys.exit(str(e))         # a mismatched case set is a refusal, not a warning
        print(json.dumps(c, ensure_ascii=False, indent=1) if a.json
              else _worldline_compare_text(c))
        return 0

    store = _store(reg, getattr(a, "bench_store", None) or a.store)

    if a.cmd == "metrics":
        from . import richness
        if a.mcmd != "richness":
            sys.exit("kura metrics {richness}")
        r = richness.gauge(store, since_days=a.since, window_days=a.window)
        if a.json:
            print(json.dumps(r, ensure_ascii=False, indent=1))
        else:
            print(richness.table(r))
        # §15's warning rides in the output and in r["warnings"], but it is a gauge,
        # not a gate: the exit code stays 0 so a scheduler never mistakes "the store
        # looks suspicious" for "the command failed".
        return 0

    if a.cmd == "bench":
        from . import bench
        if a.bcmd == "compress":
            print(json.dumps(bench.compress(reg, store, a.tokenizer_command, a.session),
                             ensure_ascii=False, indent=1))
            return 0
        if a.bcmd == "packets":
            from . import bench_writers
            try:
                chunks = a.chunks if a.chunks is not None else 1
                if chunks < 1:
                    raise ValueError(f"--chunks must be >= 1, got {chunks}")
                packets = bench_writers.freeze_packets(
                    store, _distiller(reg, store), chunks=chunks, session=a.session)
                bench_writers.save_packets(packets, a.out)
            except (OSError, ValueError, RuntimeError) as e:
                sys.exit(str(e))
            print(json.dumps({"packets": len(packets), "out": a.out}, ensure_ascii=False))
            return 0 if packets else 2
        if a.bcmd == "writers":
            from . import bench_writers
            try:
                packets = bench_writers.load_packets(a.packets)
                writers = []
                if a.from_config:
                    from .thinker import Endpoint
                    writers.extend((w["name"], Endpoint.from_dict(w, w["name"]))
                                   for w in reg.bench_writers)
                writers.extend(bench_writers.endpoints_from_specs(a.writer))
                if not writers:
                    raise ValueError("writers: provide --writer name=url/model or --from-config")
                report = bench_writers.run_writers(packets, writers, store, a.out)
            except (OSError, ValueError, TypeError, KeyError) as e:
                sys.exit(str(e))
            print(report.to_json())
            return 0
        if a.bcmd == "retention":
            r = bench.retention(reg, store, a.questions, hops=a.hops)
            if not a.verbose:
                r.pop("rows", None)
            print(json.dumps(r, ensure_ascii=False, indent=1))
            # A retention run that scores badly should not look like a passing command.
            return 0 if r["score"] >= 0.9 else 1
        if a.bcmd == "worldline":
            from . import worldline as wl
            from .thinker import Endpoint
            try:
                files = {}
                for spec in a.resident_file:
                    name, sep, path = spec.partition("=")
                    if not sep or not name or not path:
                        raise ValueError(f"--resident-file wants NAME=PATH, got {spec!r}")
                    files[name] = path
                names = [n.strip() for n in a.resident.split(",") if n.strip()]
                variants = wl.resident_variants(store, names, files,
                                                prefill_cfg=reg.prefill_cfg_for(store))
                thinker = reg.models_for(store).thinker
                identity = None
                if a.routing == "agent-only":
                    if a.agent_url:
                        thinker = Endpoint(url=a.agent_url, model=a.agent_model or "agent",
                                           api_key_env=a.agent_key_env or None)
                    identity = {"url": thinker.url, "model": thinker.model}
                elif a.agent_url or a.agent_model:
                    # The same refusal bench.worldline() makes: full ALWAYS runs the
                    # configured thinker, or the routing modes stop being comparable.
                    raise ValueError("--agent-url/--agent-model measure agent-only routing; "
                                     f"--routing {a.routing!r} always uses the configured thinker")
                cases, cases_sha = wl.load_case_set(a.cases)
                r = wl.run(store, cases, routing=a.routing, case_set_sha=cases_sha,
                           thinker=thinker, fastpath_cfg=reg.fastpath_cfg_for(store),
                           hops=a.hops,
                           trace_path=a.trace
                           or os.path.join(store.still, "worldline-traces.jsonl"),
                           agent=identity, use_cues=not a.no_cues,
                           resident_variants=variants)
            except (ValueError, OSError) as e:
                sys.exit(str(e))            # a mode conflict or a bad file, named, not a traceback
            if a.json:
                print(json.dumps(r, ensure_ascii=False, indent=1))
            else:
                print(_worldline_table(r))
                paired = _worldline_paired_table(r)
                if paired:
                    print()
                    print(paired)
            # A wrong branch (an abandoned plan anchoring a case) is the one result
            # that must never read as a passing run — and a resurrected obsolete
            # plan is the worse form of it; nothing runnable is the other.
            s = r["summary"]
            return 0 if s["runnable"] and not s["wrong_branch"] and not s["obsolete_branch"] else 1
        if a.bcmd == "payforward":
            from . import bench_payforward as bpf
            try:
                r = bpf.run(reg, mouth=a.mouth, skip_cold=a.skip_cold)
            except KeyError as e:
                sys.exit(e.args[0])             # a typo'd --mouth must not read as a measurement
            except OSError as e:
                sys.exit(f"mouth {a.mouth!r} unreachable: {e}")   # exit 1, with the reason
            if r.get("warning"):
                print(f"⚠ {r['warning']}", file=sys.stderr)
            if r.get("final_restore_error"):
                print(f"⚠ {r['final_restore_error']}", file=sys.stderr)
            if a.json:
                print(json.dumps(r, ensure_ascii=False, indent=1))
            else:
                print(_payforward_table(r))
            return 0
        sys.exit("kura bench {" + "|".join(bsub.choices) + "}")

    if a.cmd in ("weave", "prefill", "trail"):
        from . import prefill as prefill_mod
        cfg = dict(reg.prefill_cfg_for(store))
        if getattr(a, "fresh_days", None) is not None:
            cfg["fresh_days"] = a.fresh_days
        if getattr(a, "trigger_tokens", None) is not None:
            cfg["trigger_tokens"] = a.trigger_tokens
        scribe = (None if (a.cmd in ("prefill", "trail") or getattr(a, "no_model", False))
                  else reg.models_for(store).scribe)
        loom = prefill_mod.loom_for(store, cfg, scribe=scribe)

        if a.cmd == "trail":
            t = prefill_mod.trail_for(store, cfg, loom=loom)
            r = t.write()
            print(json.dumps(r, ensure_ascii=False))
            # 2 = nothing fresh to say (the trail was removed or never existed)
            return 0 if r.get("written") else 2

        if a.cmd == "prefill":
            pf = prefill_mod.build_from_cfg(
                store, loom, cfg, trail=prefill_mod.trail_for(store, cfg, loom=loom))
            if a.json:
                print(json.dumps(pf.as_dict(), ensure_ascii=False))
            else:
                print(pf.text, end="")
            # 2 = the block is not what it should be (no cloth, or stale): the caller
            # still gets usable text, but a hook can notice and re-weave.
            return 2 if pf.stats.get("stale") or pf.stats.get("note") else 0

        if a.status:
            st = loom.weave(generate=False).stats
            print(json.dumps(st, ensure_ascii=False, indent=1))
            return 0
        # M4: the adaptive shadow runs AFTER the production cloth is settled and never
        # decides what it says — unless adaptive_apply has been earned by a benchmark,
        # in which case the shortest-safe cues are worn through the loom's own override
        # (the postcondition still applies). Old configs never reach this block.
        adaptive_on = bool(cfg.get("adaptive_triggers")) or getattr(a, "adaptive", False)
        if adaptive_on and a.cmd == "weave":
            from .adaptive import DEFAULT_STEPS, Adaptive
            ad = Adaptive(store, loom, steps=cfg.get("trigger_steps") or DEFAULT_STEPS,
                          scribe=scribe)
            if cfg.get("adaptive_apply"):
                shadow = ad.shadow()
                cloth = loom.weave(triggers=ad.triggers(shadow))
                r = loom.persist(cloth) if hasattr(loom, "persist") else {}
                print(json.dumps({"adaptive": shadow["summary"], "applied": True,
                                  **({"persist": r} if r else {})}, ensure_ascii=False))
                return 0
            cloth = loom.weave()
            r = loom.persist(cloth) if hasattr(loom, "persist") else {}
            shadow = ad.shadow()
            out_path = getattr(a, "adaptive_out", None)
            if out_path:
                # A rendered VARIANT for the benchmark — written where asked, never
                # where the cloth lives, so nothing can mistake it for production.
                if os.path.abspath(out_path) == os.path.abspath(loom.out_path):
                    sys.exit("--adaptive-out must not be the cloth path")
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(ad.render(shadow))
            print(json.dumps({**({"persist": r} if r else {}), "adaptive": shadow["summary"],
                              "applied": False, **({"rendered": out_path} if out_path else {})},
                             ensure_ascii=False))
            return 0
        from .weave import WeaveError
        try:
            w, f, _ = prefill_mod.budget_of(cfg)
            cloth = loom.fit(window_tokens=w, fraction=f)
        except WeaveError as e:
            sys.exit(f"weave refused to write: {e}")
        # `fit` already wove with the model; persist exactly that text.
        stats = loom.persist(cloth)
        print(json.dumps(stats, ensure_ascii=False))
        if stats.get("refused"):
            # A memory was poured while the loom was working; the old cloth stands.
            # Exit 2 (the same "re-weave" signal `prefill` uses) so a scheduler sees
            # this as "run me again", not as "worked" — one retry is the caller's
            # job, looping is nobody's.
            print("⚠ the canonical index moved while weaving; nothing was written. "
                  "Run `kura weave` again.", file=sys.stderr)
            return 2
        if stats.get("over_budget"):
            w = stats.get("weight", {})
            print(f"⚠ the index is {stats['tokens_est']} tokens, over the "
                  f"{stats['budget_tokens']}-token budget "
                  f"({100 * stats['fraction_used']:.2f}% of the window). Nothing was "
                  f"dropped, and the vivid layer was kept because no setting reaches the "
                  f"budget anyway (fresh_days={stats['fresh_days_used']}).\n"
                  f"  weight: {w.get('grouped_lines', 0)} grouped lines (never trimmed — "
                  f"they name several memories each), {w.get('pinned_lines', 0)} pinned, "
                  f"{w.get('trigger_lines', 0)} trimmed, {w.get('header_lines', 0)} headers.\n"
                  f"  dials: lower trigger_tokens, shrink pinned_types, split the store, "
                  f"or raise budget_fraction if the window can afford it.",
                  file=sys.stderr)
        return 0

    if a.cmd == "recall":
        d = do_recall(store, reg.models_for(store).thinker, a.question, a.hops, a.top,
                      fastpath_cfg=reg.fastpath_cfg_for(store))
        if a.json:
            print(json.dumps(d, ensure_ascii=False))
        else:
            print(f"[{d['elapsed_s']}s / {d['how']}] picked: {d['picked']}")
            print(f"          walked: {d['walked']}  ({d['chars']} chars)\n")
            print(d["context"])
        return 0 if d["walked"] else 2

    if a.cmd == "glance":
        g = do_glance(store, a.slug)
        if not g.get("ok"):
            # Exact: a misspelling is a refusal in BOTH modes — JSON either way,
            # so a script never has to parse the human format to see the error.
            print(json.dumps(g, ensure_ascii=False))
            return 1
        if a.json:
            print(json.dumps(g, ensure_ascii=False))
        else:
            print(g["text"])
        return 0

    if a.cmd == "remember":
        body = sys.stdin.read() if a.body == "-" else a.body
        r = store.remember_direct(a.slug, a.description, body, a.type, title=a.title,
                                  tags=a.tag, annotations=_annotations_of(a))
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r.get("ok") else 1

    if a.cmd == "retire":
        r = store.retire(a.old, a.new, a.manifest)
        if not r.get("ok"):
            print(r["error"], file=sys.stderr)
            return 1
        if r.get("already"):
            print(f"{r['old']} already reads as superseded by {r['new']}; nothing written")
        else:
            print(f"{r['old']} now reads as superseded by {r['new']} "
                  f"({r['manifest']}); the memory itself is untouched")
        return 0

    if a.cmd == "annotate":
        r = store.annotate_direct(a.slug, tags=a.tag, annotations=_annotations_of(a))
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r.get("ok") else 1

    if a.cmd == "tend":
        from .tend import Tender
        # reg.config_path, not a.config: the resolved file the registry actually
        # loaded (--config, then $KURA_CONFIG, then the candidates). Children are
        # pinned to it instead of re-resolving under whatever exists at their launch.
        t = Tender(reg, store, reg.config_path, idle_min=a.idle_min, poll_s=a.poll,
                   backoff_min=a.backoff_min, yield_on_return=(False if a.no_yield else None))
        if a.once:
            # 0 = the requested work completed, 1 = it was attempted or required and
            # did not complete (failed, yielded, child error, timed out), 2 = there was
            # honestly nothing to do. The timeout used to fall through as 0, so a
            # scheduler read "started, deadline passed" as done and never retried.
            r = t.run_once(timeout_s=a.timeout)
            t.beat(0.0, getattr(t, "_once_stamp", 0.0))
            print(json.dumps({"store": store.name, "done": t.done,
                              "next_ok": {k: int(v) for k, v in t.next_ok.items()},
                              **r}, ensure_ascii=False))
            return int(r["code"])
        t.watch()
        return 0

    if a.cmd == "profile":
        import shutil
        draft = os.path.join(store.still, "profile.draft.md")
        if a.pcmd == "show":
            st = store.profile_state()
            print(json.dumps({"store": store.name, **st,
                              "draft_waiting": os.path.exists(draft),
                              "text": store.profile_text()}, ensure_ascii=False, indent=1))
            return 0
        if a.pcmd == "draft":
            r = _distiller(reg, store).profile_draft()
            print(json.dumps(r, ensure_ascii=False))
            return 0 if r.get("ok") else 1
        if a.pcmd == "apply":
            # A person copying a file they have read. Refused on a frozen store, because
            # frozen means nothing is written there by anyone through this tool.
            if store.write_policy == "frozen":
                print(json.dumps({"ok": False, "error": f"store '{store.name}' is frozen"}))
                return 1
            if not os.path.exists(draft):
                print(json.dumps({"ok": False, "error": "no draft: run `kura profile draft` first"}))
                return 1
            shutil.copyfile(draft, store.profile_path)
            os.remove(draft)
            print(json.dumps({"ok": True, "applied": store.profile_path,
                              **store.profile_state()}, ensure_ascii=False))
            return 0

    if a.cmd == "distill":
        from .distill import drafts_of
        dis = _distiller(reg, store)
        if a.dcmd == "run":
            r = dis.run(a.session, a.chunks)
            print(json.dumps(r, ensure_ascii=False))
            if not r.get("ok"):
                return 1        # water is owed, not "nothing to do": a scheduler retries
            return 2 if r.get("why") == "nothing worth drinking" else 0
        if a.dcmd == "sip":
            got = dis.sip_one()
            if not got:
                print("nothing worth drinking")
                return 2
            segs, path, key = got
            by: dict[str, int] = {}
            for s in segs:
                by[s.cls] = by.get(s.cls, 0) + 1
            print(f"{key}: {len(segs)} segments {by}")
            from .distill.sources import as_evidence
            print(as_evidence(segs)[:2000])
            return 0
        if a.dcmd == "catchup":
            r = dis.catch_up()
            print(json.dumps(r, ensure_ascii=False))
            return 0 if r.get("moved") else 2
        if a.dcmd == "drafts":
            rows = drafts_of(store)
            for slug, cls, desc in rows:
                print(f"  {slug:36} [{cls}]\n      {desc}")
            return 0 if rows else 2
        if a.dcmd == "pour":
            if a.all:
                drafts = drafts_of(store)
                for slug, _, _ in drafts:
                    print(json.dumps(dis.pour(slug), ensure_ascii=False))
                # No drafts is "nothing to do" — the docstring's exit 2 — or a
                # scheduler sees success and never comes back.
                return 2 if not drafts else 0
            if not a.slug:
                sys.exit("give a slug or --all")
            r = dis.pour(a.slug)
            print(json.dumps(r, ensure_ascii=False))
            return 0 if r.get("ok") else 1
        if a.dcmd == "drain":
            r = dis.drain(a.n)
            print(json.dumps(r, ensure_ascii=False))
            return 0 if (r.get("poured") or r.get("tossed")) else 2
        if a.dcmd == "tidy":
            r = dis.tidy(a.n)
            print(json.dumps(r, ensure_ascii=False))
            return 0 if r.get("fixed") else 2
        if a.dcmd == "night":
            dis.night(a.idle_min)
            return 0
        sys.exit("kura distill {" + "|".join(dsub.choices) + "}")   # derived: a hand-kept
        # list drifted (it never mentioned `catchup`). Not `required=True` on the
        # subparser: argparse would exit 2, which this CLI reserves for "nothing to do".

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
