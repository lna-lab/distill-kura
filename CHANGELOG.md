# Changelog

## 0.3.0 — unreleased

### Two more floors on a composed surface — dead links and invented quotations

Rina's ruling (2026-09-03): "existence, quotation, numbers, attribution are floors;
whether the advice is good is the writer's competence — do not mix them."

* **Dead-link floor.** Every `[[slug]]` a scribe writes must name a memory the store
  holds, one already staged as a draft, or the draft's own slug. Unknown ones come
  back to the scribe once, named exactly (`unknown links: upload-prep, bug-list`); a
  rewrite that still points nowhere is a compose failure, like any other broken shape.
  A caller that cannot say which slugs exist passes `known_slugs=None` and nothing is
  checked — the floor never guesses at topology.
* **Invented-quotation floor.** A run of at least `MIN_QUOTED` (8) characters inside
  「」/『』/“”/"" that reads as speech (a space or a CJK character in it) must stand
  verbatim — NFKC, whitespace collapsed — in the surviving evidence. This catches a
  reply nobody made, embedded as a quotation. Short scare-quotes and bare JSON tokens
  in a TAGS line are not testimony and are ignored.
* **`How to apply` is now optional.** The scribe prompt asked for fact → Why → How to
  apply as a required shape, which pressured a weak writer to invent a next-time
  instruction. It is now asked for only when the evidence itself carries a reusable
  procedure or rule. Whether an instruction follows from the evidence stays the
  writer's competence — no regex judges it.

`GATE_VERSION` 6 → 7.

### Richness regression gauge (plan §15)

`kura metrics richness` tells "the store stopped remembering lies" apart from "the
store stopped remembering". Rejections going up is ambiguous — the gate may be
learning honesty, or the writer may have gone quiet and the gate is all that is
left — so the gauge reads the `_still/*.jsonl` logs their writers already record
(metrics, dropped, tossed, seeds, reads, worldline traces) and never writes. Pure
aggregation, no model, `--since DAYS` to bound the range, `--window DAYS` (default
7) so every metric also appears per rolling window and a trend is visible. Each
number carries its denominator; a malformed log line is counted (`bad_lines` per
file) and skipped, never fatal. What it reports: candidate rate per raw journal
(and per source key), the gate's rejection-reason distribution with
`unverified_numbers` counted, USER evidence candidates against `gated_kept` (the
writer records no per-class keeps, so that cell says "not recorded by the writer"
rather than inventing a proxy), the seed-retreat rate with toss verdicts,
callsign receipts against refusals, `remembered_but_unreachable` per resident
variant, target-reached per `resident_sha` (the sha is the map's identity; the
variant name only labels it), and the fallback-thinker rate — where it also names
the honest hole that recall's "how" (fastpath vs meaning) reaches no log, and
which writer would have to carry it. When the latest window shows gate rejections
up AND `gated_kept` down against the previous window, one line prints:
`WARN richness: rejections up (a→b), kept down (c→d) — is the gate learning
honesty or silence?` — a gauge, not a gate: exit code 0 either way.

### M4 — the shortest cue that still recognises (shadow)

The fixed 24-token trigger asked "how much can I cut?". M4 asks the plan's
question: *what is the shortest cue that makes the reader recognise THIS memory and
not its neighbour?* — and answers it in the shadow, where being wrong costs nothing.
`kura weave` with `[prefill] adaptive_triggers = true` (or `--adaptive`) writes
`_still/adaptive.json`: per trigger-layer memory, the current trigger, a candidate per
rung of `trigger_steps` (one scribe call per memory, cached), the shortest one that
is SAFE, and — the measurement — `why_not_shorter` for every rung it refused
("ambiguous: <slug>", "negation dropped", "number re-bound: 43.7 ms", "not
offered"). The production cloth does not change: `adaptive_apply` stays `false`
until a benchmark earns it, and an untouched old config never reaches this code.

Safe means two things, checked in that order. The floor: every production floor the
trigger already passes (2-gram grounding, invented numbers, invented attribution,
dead words, title restated) plus what a SHORTER cue newly risks — a number re-bound
to another unit or referent (43.7 t/s → 43.7 ms, TP=4 → TP=8), a before→after arrow
reversed, a retirement word dropped (⚠️退役 shrinking into a line that reads as
current), an identifier cut mid-token (ds4-tp8-engine-canonical → ds4-tp8), a
negation gained or lost (工房ではない → 工房), a marker or [[link]] the line never
had. Then the distinguishability test: the five-head recognizer is asked the cue as a
question with the callsign pre-head OFF and the body head OFF — a new
`fastpath.lookup(..., cues=False, body=False)` over a resident-only index — so a cue
counts as recognised only by what the resident map itself shows. A verified callsign
is the human's own word and shares no 2-grams with the line, so it is not graded by
grounding at all: its floor is a live, unambiguous receipt and its test is the receipt
route, recorded as `via: receipt` and counted separately.

Two lessons the red team taught before the code was written: candidates are
memory-local and cached (`_still/adaptive.hooks.json`), but VERDICTS are not — whether
a cue is ambiguous depends on every neighbour, so the tests run again on every
shadow, without a model, and a memory poured this morning can revoke a cue judged
safe last night; and `trigger_steps` must END at `trigger_tokens`, or the ladder
could never offer the size production wears today. Falling back to 24 — or to the
canonical line itself — is not a failure; it is the finding that this memory needs
that many tokens.

Also in this change: `fastpath.RECOGNIZER_VERSION` (the shadow keys on it);
`gate.attributes_to_human` learns the house's own shorthand (ケン確定 / 裁定 / 方針 /
決定 / 号令, "ケン:", "Ken decided") — a cue could rewrite who decided while every
2-gram stayed in place; `Loom.weave(triggers=...)` — an explicit trigger map the
cloth may wear instead of the ledger, the one seam production exposes to M4 (the
postcondition still applies to it).

**Worldline bench for M4.** `kura bench worldline --resident canonical,woven[,NAME]`
runs the same cases across resident-map variants and prints them side by side;
`--resident-file NAME=PATH` benchmarks any rendered map (the adaptive shadow's
`Adaptive.render()`) without the benchmark importing it. New raw metrics, no
composite: `remembered_but_unreachable` (the memory exists, the door was too narrow —
only meaningful under agent-only/fastpath, where no thinker rescues a thin map),
`unnecessary_opens`, `obsolete_branch` (disjoint from `wrong_branch`; a resurrection
alone now fails the run), `honest_unknown`. A 27-memory synthetic fixture store
(`bench/worldline/memories.json`, superseded plans included) makes the shipped 26
cases runnable anywhere; `explanation_burden` waits for M8 and a human definition.

*Found on the way:* the shadow's candidate cache (`adaptive.hooks.json`) is a file,
not a witness — a reused entry is now shape-checked and re-cleaned, and every reused
cue still meets the floors and the recognizer in `judge` (reuse saves the model call,
nothing else; `cues_reused` in the summary). The production `hooks.json` now carries
the same mark as the cue ledger — HMAC over the canonical payload with the store's
gate key — and a file whose mark does not verify is treated as empty: every hook is
regenerated, never partially trusted. Two more measurement fixes: the recognizer
tells `untestable` (a cue made only of stop-grams — the store could not be asked)
apart from `no-confident-hit` (the store said no), and every
Worldline trace row and variant carries `resident_sha`, the identity of the map it
actually wore, because "adaptive" names a label, not a trigger set.

**FAILURES FOUND: the production hook wore what the scribe answered, unchecked.**
Measured on the house store (2026-09-02, 67 trigger-layer memories): the adaptive
floors refuse the CURRENT production hook for 19 of the 67 — a git hash `6d62189`
worn as `d62189`, ★/⚠️ on lines that never had them, among others. The production
Loom checked only the older floors, so a scribe answer that passed the numeric floor
went onto the resident map as-is. Now every hook — scribe answer and mechanical trim
alike — faces `floors.first_violation` before it is worn; on a violation the
mechanical trim gets its chance, and if that lies too, the canonical line is worn
unchanged. The reason is stored on the hook entry as `floor` (None when clean), and
`LEDGER_VERSION` is bumped to 10 so the old unchecked hooks are regenerated, not
reused. A cut that lies is never worn.

*Measured, not promoted (2026-09-02):* on a 320-memory house store, 42 private
cases, one format-compliant reader run twice, the adaptive map recovered 24/42 and
22/42 against the production cloth's 20/42 and 22/42 — inside the run-to-run noise
(±2) — with more unnecessary opens and a 2% smaller map. `adaptive_apply` stays off;
the shadow keeps measuring. The measurable gain of M4 so far is the one it forced
upstream: the production trigger now faces the same floors (34/67 house hooks were
wearing a re-bound number, an invented marker, a dropped negation or a reversed arrow).

### Pay-forward measured (M5)

*Measured on the house mouth (2026-09-02, a 320B CPU model, 17.6k-token map, 217-token
trail): cold 950 s; restored spine + trail 16.4 s (prompt_n 190); trail changed 2.1 s
(35); last line of the map changed 22.5 s (242); first line changed 956 s (17,629) —
the volatile-header proof in one row; warm repeat 16.0 s. The spine/trail split stands;
no partial-KV work is warranted by these numbers.*

`kura bench payforward --mouth NAME` makes a `[[payforward.mouths]]` entry price its
own warmth, condition by condition, reading `timings.prompt_n` back from each reply —
the mouth's own count of what it reprocessed, the only witness that counts. Six
conditions, in order: `cold-full` (the whole resident block, cache off — skippable
with `--skip-cold`, minutes on a CPU mouth); `restore-spine+trail` (the current-etag
slot file restored, then map+trail sent — should reprocess ≈ the trail plus the probe);
`trail-changed` (one synthetic line appended to the trail in memory only — ≈ the
changed tail); `map-changed` (spine restored, one character changed in the LAST index
line — from the change to the end); `map-changed-first-line` (the change in the FIRST
line — nearly the whole map, the proof that a volatile header re-prices everything);
`warm-repeat` (the row-2 request again, re-armed from the spine file, no bake — the
in-process prefix cache). When no slot file exists for the current etag the spine is
baked first, exactly as `kura pay-forward` bakes it (probe, save, ledger advance), and
shown as its own `bake-spine` row. Nothing is written to the store, no modified map or
trail reaches disk, the mouth is left restored on the current-etag spine, and the run
exits 0 unless the mouth is unreachable. The test fake is a llama.cpp in miniature: a
per-slot KV string, common-prefix reprocessing, and save/restore by filename.

### Constellation (M6)

For a store whose full map alone is over the hard ceiling, the choice until now was
an honest stub that said only "too large" — and an agent told nothing about what
exists fills the gap with invention. The constellation is the honest middle, with no
new structure and no model: the sectors ARE the canonical index's existing `## `
headings, in order, and every memory belongs to exactly one sector — the heading
above its FIRST index line. Lines before the first heading, and memories with no
index line at all, are `UNSECTIONED`; a grouped line (`- topic — [A](a.md)/[B](b.md)`)
puts each of its slugs in that line's sector once. The invariant is structural,
checked in code, and raised loudly if broken: sum(sector counts) == len(slug_set()).

Absence means something different here and the block says so out loud — right after
the frame header, verbatim: "This is a map of sectors, not individual memories. /
A memory not named here may still exist inside a sector." The full map's "not on the
list = not remembered" wording never rides on a constellation. A sector line carries
the name, its count, and up to three example titles verbatim from the index marked
"e.g." — a hint, never a listing, and partial maps are forbidden in both directions.

`prefill.build` gains `resident_mode` (`full | auto | constellation`): `full` is
today's behaviour byte for byte; `constellation` always wears the sector map (the
trail still rides after it when it fits); `auto` wears the map while it fits under
the ceiling and falls back to the constellation only when the map alone is over it —
and when the constellation itself is over, the existing TOO_BIG stub comes back.
Stats name the choice: `resident_mode`, `source: "constellation"` when worn,
`map_shown` stays false (it is not the map), and `constellation_shown` says what
went out. Configure with `[prefill] resident_mode = "..."` (global or per store);
anything outside the three names fails config loading. `kura constellation [--json]`
prints the sector table and the invariant; `kura doctor` carries the sector and
UNSECTIONED counts — a large unsectioned count is the cue to add headings.

### Typed worldline edges (M7)

Typed edges between memories — exactly five: `continues`, `next`, `supersedes`,
`rejected`, `blocked-by` — exist as DERIVED routing state and nothing else. They are
never written into frontmatter or bodies; `distill_kura/edges.py` recomputes them from
the canonical files on demand and keeps them in one marked cache, `_still/edges.json`
(the cue ledger's `{"payload", "mark"}` shape, signed with the store's gate key). A
mis-marked file reads as empty and is rebuilt; on a frozen store the derivation still
answers, in memory, writing nothing.

The floors are structural: both ends must be existing exact slugs of THIS store, source
≠ target, and the target must be among the source's own `[[links]]` — prose alone
invents nothing. The cue words that fire are lexical and recorded on the edge (`cue`),
with a specificity order (supersedes → rejected → blocked-by → next → continues) for a
line that says several things. Evidence floors where it matters: `supersedes` and
`rejected` need USER evidence in the source's verified manifest, `blocked-by` needs
USER, TOOL or ACT, and a memory whose manifest is missing or unverifiable yields none
of the three — counted in the report as `unevidenced`, never dropped in silence.
Deterministic and byte-stable at a revision; no model calls.

Consumers: `kura glance` fills its `relations` field and renders up to three as a
`RELATIONS:` block under the same token contract as LINKS; the Hot Trail grows one
optional `↳ source continues → target` tail (max 3 lines, only when a fresh breadcrumb
has an onward `continues`/`next` edge, and the edges payload hash rides in the trail's
spec so a changed edge set re-prices the trail); `kura bench worldline` reports
`edge_says_obsolete` — whether a `supersedes` edge independently marks what a case
calls obsolete — as a raw metric with no scoring change. `kura edges [--json] [--slug
S]` prints the derived table; `kura doctor` carries the edge counts. Recall's BFS is
untouched: edges are routing hints for trail/glance/bench, not a new way into recall.

*Trail header, measured and moved (2026-09-02):* the prose line under the trail marker
("CURRENT PATH — these are recent breadcrumbs, not the whole memory:") made the reading
model start narrating — on the house store, 42 cases, one reader: format errors 13→19
and recovery 22→17 with the trail appended, back to 22 with the sentence removed, 25
with the same words folded into the marker. The note now rides inside `TRAIL_BEGIN`;
the block is index lines and markers only. Trail state version 3 (a trail on disk rebuilds).

*Full A/B measured (M8, 2026-09-02):* seven resident shapes (canonical, woven, adaptive,
each with and without the trail, constellation) in front of the same 42 private cases,
two readers, two runs each. The three maps that can name memories sit within ±2
recoveries of each other for the format-compliant reader; the second reader's rows are
dominated by reasoning cut at the output cap and do not rank them. The constellation
alone recovers only the unknowns — as designed, it is the exit for stores over the
ceiling, not a map. Defaults are unchanged; the one change the run bought is the trail
header above.

### Review follow-ups landed (2026-09-02 afternoon)
Of sixteen bug-class findings from the morning review, twelve were already fixed at HEAD
by the day's work and were left alone; four were real and are fixed with regression
tests: a tended watcher's child tracks now receive the config path the registry
actually resolved (not the bare flag); `bench compress` attributes memories through the
verified manifest loader, never a raw read of a file named by its hash; a second pour of
one slug no longer renames onto the earlier `.md.poured` and destroys it; and the seed
ledger's sow/confirm run under a lock with per-pid temp files, so parallel runners no
longer lose seeds or truncate each other.

### Structure pass, wave 2 (2026-09-02 afternoon)
Ninety-three non-bug findings from the morning review (duplication, drifted docs, test
gaps, error handling, naming) were re-verified at HEAD and worked in four isolated
worktrees, behaviour-preserving and under the full suite: one containment predicate,
one per-store config merge, one bearer-header builder, one prefill build-from-config,
one `_send` for every HTTP reply, one draft-head/evidence-lines/draft-record helper in
the pipeline, hoisted gate vocabularies, `Store.profile_check` as the single profile
judgement, a stale "degraded" label and a bad `hops` no longer read as outages, and
the README/OPERATING/DESIGN passages brought back to what the code does. New tests
pin CLI exit codes, server routes (ETag/304/text), and the branches that had none.
The degraded word tier (`pick_by_words`, used only when the thinker is unreachable)
now matches link TARGETS with the `](` anchor, as `known_slugs` and `doctor` already
did — a prose parenthetical such as `(AGENTS.md)` no longer scores as a memory while
the line's real memory goes unscored.
Wave 3 closed four structural edges: HTTP routes match exactly (a `/healthz` probe is a
404, not `/health` wearing another name); `[prefill]`/`[fastpath]` refuse unknown keys like
the store tables do (a typo in an every-turn switch is now loud, not silent); the gate
format version is one constant with its signed string derived; `gate_key()` reads the
key file through one path on every outcome.
Left for a decision or a later wave: a single `_parse_scribe`, an `_Envelope` type,
fsync-unified atomic writes, and the twenty-one structure/split findings.

### The instruments stop lying (2026-09-02 evening, the plan author's rulings)
Agent-only rows are judged on the model's user-visible `content` alone (`Endpoint.ask_full`
takes the reply apart; `ask()` keeps its thinker-side reasoning fallback); `truncated`
(finish_reason=length) and `reasoning_only` (blank content beside reasoning) are recorded
apart and never excuse a `format_error`. Every row and result carries `case_set_sha`, the
sha256 of the case file it was measured on. A `paired-format-valid` table judges only the
cases valid in every variant of the run, and `kura bench worldline-compare A B` sets two
runs of the same case set side by side (all cases, paired-valid, format_error delta, the
four safety metrics) with no composite score; the promotion rule is written in OPERATING
with the observed noise envelope (about ±2 / 42 on the house set v1). `kura tend --once`
exits 0 = completed, 1 = attempted or required but not completed (a timeout is 1, with a
`retryable` record), 2 = honestly nothing to do; five outcomes stay distinguishable and no
child process survives the return. The index craft tells writers that a retired thing
wears it — on its own trigger, only after a verified transition, never from a derived edge.

### The retirement face (2026-09-02 evening)
A retired plan, method or ruling is never hidden and never dressed as current: the OLD
memory's own trigger says so (`退役: …／現在は [[new]]`, `superseded: … — now [[new]]`).
The only door is `Store.retire(old, new, manifest_hex)`, which verifies through the
content-addressed loader that the manifest carries a USER-class quote naming the old
memory; a TOOL/SELF/ACT-only manifest, a tampered manifest, a missing or identical slug,
a grouped line, a frozen store and a second, different successor are all refused; a
repeat is `already`. A derived edge cannot retire anything (tested). Only the index line
changes, through the index writer with WAL and a revision bump, plus one appended body
line naming the manifest. The distiller fires it at the end of a pour when the human's
gated words named what the new memory replaces; the loom compresses inside the face,
never the face (the trimmer had been dropping the trailing `[[new]]`, measured and
fixed); doctor, the richness gauge and worldline rows count and show faced memories;
`kura retire OLD NEW --manifest HEX` is the human-driven door. Proposed is not proven (a P1 found the same
evening): the face is written only when ONE of the human's quotes names the old memory,
carries an explicit replacement construction (やめて…で行く／代わりに／今後は／→ …,
instead of／replaced with／switch to …) and names the new memory in that same sentence —
never stitched across quotes, and nothing after a topic-shift clause (ところで, by the
way) counts. `Store.retire` proves it again on the same manifest; the model's proposed
tag is no signal at all. A quote that only retires writes no face: many false negatives
are acceptable, a wrong successor in canonical is not.

### Worldline / Breadcrumb (M0–M2 of the plan, in progress)

The next purpose, in the plan's words: from the smallest breadcrumb, restore the
large shared world correctly — on a 50 tok/s prefill model, opening a session
already sharing the projects, people, decisions, failures and turns of phrase.

- **`kura bench worldline`** (M0): twenty opening-utterance cases across ten
  categories (a shared callsign, an ellipsis, a superseded plan, a question the
  store knows nothing about) with a raw-metrics JSONL trace and three routing
  modes that keep the credit separate — `agent-only` (the conversation model
  reads the resident map alone; an unreachable model is an outage, never an
  honest empty answer), `fastpath` (tier zero only; its silence IS the
  measurement), `full` (the production path). No composite score exists, on
  purpose. A case written against another house is skipped with its reason.
- **`kura_glance`** (M1): the "ああ、それね" tier — an exact ~150-token
  confirmation of one recognised slug (canonical index line, the KEEP sentence
  only when curation is verified, in-store [[links]]) before a full read costs
  its tokens. `GET /glance/<slug>`, `kura glance`, MCP and DSH tools, and the
  tool ladder turned around: glance what you recognise, read for detail, recall
  only when the NAME is unclear — the thinker leaves the everyday critical path.
- **The Hot Trail** (M2, `_still/trailhead.md`): the fresh layer, newest
  internal date first, ~200 tokens, every line an existing recognition line
  reused verbatim — current position, not importance; the read log is never
  consulted. Appended AFTER the byte-stable map so a trail-only change (the
  fresh window slides with time) leaves the map's prefix identical — pinned by
  test — with the cloth's two-ended freshness proof extended by a config-spec
  hash and a `valid_until` horizon (the moment the first included breadcrumb
  ages out; time alone retires the trail). A stale trail is not appended, an
  empty fresh layer removes it, the map outranks it at the hard ceiling, and
  `kura tend` rebuilds it after every weave and whenever its horizon passes —
  the watcher's trail track is model-free and counted as work. Hardened by an
  outside review: the trail carries the loom's containment guards (never the
  canonical index, never a memory slot, nothing inside a frozen store), and
  revision 0 proves a trail as well as any other number.

- **Glance, reviewed twice**: grouped index lines (`- topic — [A](a.md)/[B](b.md)`,
  a measured 26% of one store) return their whole shared line verbatim; the
  ~150-token contract is a TARGET (the recognition line and the verified KEEP are
  never cut — links are budgeted with an out-loud `+N more links` tail, and the
  reply carries tokens_est / over_target / links_shown / links_omitted); and both
  tool registries register glance before read before recall, the order the
  guidance teaches.

- **The worldline benchmark, hardened**: agent-only reads the model's reply as a
  WHOLE JSON array (prose is a format error — following the format is part of the
  ability), counts only exact slug_set membership as `opened`, separates
  `proposed_slugs` / `invalid_slugs` / `format_error`, scores an unknown case as
  correctly refused ONLY on a valid empty array (a hallucinated slug no longer
  vanishes into a false pass), and stamps the url+model of whoever was actually
  measured — `--agent-url`/`--agent-model` name a conversation model for
  agent-only and are refused with any other routing, so one flag can never swap
  the production path's thinker.

- **USER callsigns (M3)**: the shared vernacular ("全員野球") that routes back to
  a memory — gated like a quote, because a routing word is worth exactly its
  provenance. Only an exact substring of a SURVIVING [USER] quote passes
  verify_callsigns (the agent coining a nickname, a tool printing the string, a
  paraphrase of what the human "meant" — all the same refusal); 3–40 codepoints
  after NFKC+casefold with the display keeping the human's spelling; at most two.
  Provenance lives in the manifest (`memory_slug` is code-chosen — compose,
  EXTENDS or COVERED target, never a model proposal; `routing_cues_version`
  stays separate from `gate_version`). A COVERED candidate keeps a late-born
  cue: memory novelty and routing novelty are different questions, and the new
  word is provenanced against the existing slug without moving one canonical
  byte. `_still/cues.json` is a derived index rebuilt from hash-verified
  manifests (delete it and the same ledger returns); a cue naming a slug outside
  slug_set() is not routing material, and the same cue on two memories is
  AMBIGUOUS — silence, never a guess. Tier zero answers through a pre-head:
  a unique verified cue in the question routes directly (`how=fastpath-cue`,
  the cue named in the reply), not mixed into the five heads; ambiguous or
  absent is silence and the heads run exactly as before. `kura bench worldline
  --no-cues` runs the comparison that isolates what the shared vocabulary buys.

- **Callsigns hardened (receipts are the authority)**: a cue becomes a ROUTE only
  through an immutable receipt — content-addressed, HMAC-signed with the store's
  own gate key — issued the moment the association becomes real (a draft that
  actually POURED, an extension that POURED, or a COVERED verdict against a slug
  that exists). A staged or TOSSed draft's manifest carries provenance, never a
  route. The reader trusts nothing it is handed: every receipt is re-verified on
  build (file hash, HMAC, schema, slug membership, the manifest's hash, cue
  class/quote/substring/length — "it was gated when issued" is not an argument),
  and the pointed-to receipt is verified AGAIN on every direct hit. `_still/
  cues.json` is a marked cache over a two-ended stamp (store revision + a hash of
  the receipt set, so a COVERED cue — which moves no canonical byte — is visible
  immediately); a rewritten slug, a forged revision, an inserted fake cue or a
  corrupt file each mean rebuild-from-receipts, and a cache the disk refuses to
  hold is answered from memory — a cache failure is never a recall failure.
  Several cues naming the SAME memory route together (a world has several
  names); cues naming different memories remain silence.

### The full-repo review (2026-09): every finding verified, then fixed

A review pass over every module, with each finding verified against the code (and
run, where a number could lie) before anything was changed.

**The drink lost most of a DSH journal.** `claim()` reserved 2.2× the chunk budget
while `sip()` read 1×, and the marks only move forward: every chunk's unread tail
was "drunk" on paper and skipped forever. Measured on a simulated journal before
the fix — 239 of 360 classified events never distilled; after, 0 of 360. The
reserve and the read now agree event for event: the DSH adapter's `claim_bound`
walks exactly the path `sip` takes, and the byte adapters reserve the full
`4 × budget` window their `sip` reads (reserving less is recoverable — `advance()`
takes the true stop; reserving more is silent loss).

**An outage was read as a verdict.** `ask()` returns None for unreachable,
timeout and empty replies, and `scribe()` collapsed that to `""` — which
`judge_draft` read as "the scribe did not keep the shape": a TOSS, and TOSS
deletes. A quiet hour with the editor down emptied the drafts queue. `scribe()`
now returns None intact and drain answers SKIP — the draft stays staged, counted
as `skipped`, judged again next silence. The same loop no longer pours a FIX
whose `BODY:` failed to parse: falling through to `pour()` filed exactly the text
the judge had condemned (now `fix_unparsed`, left staged).

**Two gate flaws, one per direction.** `_canon_num` erased every comma, so "1,5"
canonicalised to "15" and a claimed 1,5 GB passed on evidence that said 15 GB —
a comma is now forgiven only as a thousands separator (`1,234,567` still passes
against `1234567`; `12,34` fails closed). And the exponent's case was meaning:
"1.23e-4" against evidence "1.23E-4" was a false violation; "E" folds to "e".

**The extension path skipped half the floor.** The new-memory path floors the
candidate's own three sentences; `_compose_extension` floored only the scribe's,
so an unbacked number in the CANDIDATE's `belongs_because` reached the memory
under the curation mark. Both sentences sets stand on the floor now.

**`readonly = false` silently thawed a frozen store.** The deprecated key was
applied after validation and always won, so `frozen` + `readonly = false` became
`direct-allowed`, signalled by nothing. Weakening now refuses at construction
(the registry already refused the pair at load); tightening (`readonly = true` →
distiller-only) keeps its documented meaning.

**`[models]` was the one table that loaded silently.** A typo'd key
(`api_key_ev`) was dropped without a word — requests went out unauthenticated and
failed at call time — and a typo'd role meant no thinker at all. `[models]` and
`[model_profiles.*]` are checked at load like every other table: unknown roles
and keys throw naming the offender, value types are checked, `dialect` must be
one of the three, and `[server] port` refuses `8085.9` and `true` instead of
silently truncating them.

**Tier zero cited two-letter slugs.** The name head matched slugs as raw
substrings with no length floor (the title check had one), so a memory `ai.md`
scored a full name hit from "tr**ai**ning" — and with no runner-up the margin
gate is skipped by design, making it a confident answer. A slug is a citation
only as a whole name: three characters minimum, boundaries at anything that is
not slug alphabet (a question naming `ssd-tier-mission` no longer cites
`ssd-tier` — it named a different memory).

**The server dropped the socket instead of answering 400.** `?window=big`,
`{"hops": "one"}`, `{"question": null}` and a garbage `Content-Length` all
escaped as uncaught ValueError/TypeError: the client got no HTTP reply at all,
where the bad-json branch two lines down promises a readable 400. Both verb
handlers wrap their dispatch; malformed values are a 400 naming the error.

**And the smaller ones, each verified before fixing:** recall's truncated-memory
fallback now counts its own header against `total_chars` (a long label + slug
walked past the ceiling by up to tens of chars); `remember_direct` with an empty
description is refused instead of writing `- [](slug.md) — ` — a line no later
write can match; `tend_state()` survives a heartbeat that parses as JSON but
lies about its shape (it used to take `doctor` down with it); annotating a
memory whose tags line is unreadable refuses rather than erasing the tags;
`Loom.persist()` refuses a Cloth with no source provenance instead of writing it
unconditionally; the MCP bridge no longer dies on a valid-JSON non-object line
and reads a 404 as "no memory called …" rather than "cannot reach"; a bare
`KURA_WRITE_LOG` filename actually writes; `kura distill pour --all` with no
drafts and `kura tend --once` with no work exit 2 per the exit-code contract,
and a refused single pour exits 1; `bench` closes its files, names the offender
when a questions fixture is malformed, and skips (counting) malformed metric
lines; the DSH plugin's background refresh cannot clobber a newer store's
resident map with a late failure, `kura_map` applies the same served-store check
the cache does, and `url: "http://"` is refused at load; `fake_llm.py`'s
coverage key actually matches now; the rooms example finds its charters through
the default `<store>/charter.md` lookup instead of a CWD-relative path the
README's copy step never creates.

### Pay it forward (new)

The resident map is byte-stable so a prefix cache can hold it — but the first turn
after a re-weave still pays the whole cold prefill, and on a slow mouth that is
minutes. `kura pay-forward` pays it once, in the quiet hours: right after a weave
changes the map, it pushes the new map through each `[[payforward.mouths]]` entry (a
llama.cpp server started with `--slot-save-path`) and saves the slot's KV to disk, so
even a mouth restart wakes up warm. Measured on one machine — 320B pure-CPU llama.cpp,
16,444-token map: bake 796 s, save 283 ms (1.5 GB on NVMe), restore after killing and
rebooting the server 655 ms, first turn after it reprocessed 18 prompt tokens. Named
after the film: the cold turn is paid forward so the next one receives it warm.

Slot files are content-addressed on the map's etag (`kura-<store>-<etag…>.bin`), so a
changed etag tries a restore before it bakes — a file left by a lost state file or a
parallel runner is still the right bytes — and a fresh etag is proven, never assumed:
a restore shows the file still exists, a one-token probe with `cache_prompt: true`
reads `timings.prompt_n`, and small means warm. A restore whose 200 was a lie is
caught by the same probe, and the probe's own prefill is saved rather than paid twice;
a reply with no timings at all proves nothing and is refused — fail closed — because
llama.cpp always sends them. An unreachable mouth is a loud, labeled skip that never
advances the per-store state (`_still/payforward.json`, written only after a confirmed
success, read-modify-written under the slot's lock). One runner per physical slot: the
whole sequence holds a machine-local flock keyed on (normalized base url, slot) — the
runners that can collide, `kura tend` and the systemd restart hook, are machine-local —
and a second runner skips cleanly instead of racing. Two config entries naming one
physical slot are refused at load. The ledger itself takes a second lock — the slot
lock cannot cover it: two mouths of one store hold two different slot locks and share
one `payforward.json`, so `--mouth A` / `--mouth B` running together was a lost update.
The read-modify-write now holds a millisecond flock beside the file. And busy is no
longer fresh: a held slot lock proves another runner exists, not that it is warming
your etag, so `skipped-locked` exits 1 (transient — retry) and only a verified
all-fresh run exits 2. Exit precedence puts not-covered first: {A baked, B busy}
exits 1 so the scheduler comes back for B — 0 means the whole fleet is covered, never
"something, at least, worked". Old slot files are
not pruned, because the slots API can save and restore a filename but cannot list the
directory. Exit 2 when every mouth is fresh, so a scheduler can tell "nothing to do"
from work; a failed mouth is exit 1, because a failure is neither. `kura tend` runs it
as a track after each weave, counting bakes and restores — work, never launches.

### The gate reaches the composed text (gate_version 3)

The candidate's quotes were verified; the scribe's finished text was not — and the
scribe is a model too: told "write no numbers", it could still write one with nothing
behind it. Now every numeric token of two or more digits in the final DESC+BODY must
already exist in a verified quote or in the evidence's own date, checked by the same
deterministic substring floor as the quotes themselves. One retry with the violations
named, then the draft is dropped. A derived number — a ratio the scribe computed — is
refused on purpose: arithmetic the evidence never did is a claim the evidence never
made. (Found in an outside review; confirmed against the code before fixing.)

The second review then bent the floor, and it hardened: numbers are matched token
by token, canonicalised (commas forgiven, nothing else) — never against the
evidence's concatenated digits, which had let "899 ms … 2.3 ms" vouch for an
invented "923". A sign is meaning ("-12.5" is not "+12.5"); a range is one claim
("12-16" is not licensed by "12" and "16"); the evidence's date is no longer
auto-allowed in the body — an extension heading gets its date from code, after
verification. The gate's test-file contract now states exactly which classes the
deterministic floor covers, no more.

The third review found the door missing where it mattered most: the judge's FIX
is the LAST model to touch the text, and used to be re-signed without
re-verification. Now nothing earns a mark without passing the floor — a FIX is
re-checked against the draft's full evidence manifest (fail closed if the
manifest is unreadable) and refused if it cannot pass, leaving the draft as
staged. The floor also widened to the whole model-written surface: title,
recognition trigger, section heading and the curation sentences, not just
DESC+BODY — a title lands in the resident map, and "99-GPU構成" must not enter
through it. Tokens are Unicode-normalised (en/em dashes between digits, the
true minus, full-width digits), scientific notation is one token, and single
digits are verified too — "8 GPUs", "4-bit" and "2x" are exactly the claims a
local-model house invents — with ordered-list markers mechanically excluded.
Manifests written from here on say `gate_version: 4`, so provenance can tell
which floor a memory passed.

The fourth review followed the truth downstream: the memory can be right while
the memory the agent WEARS is wrong. Two more writers of resident truth now
stand behind the same floor — `tidy`, the only path that puts model prose into
the canonical index (an invented "99-GPU" title used to walk straight into
recall and the resident map; now the memory itself is the evidence and the
rewrite is refused), and the weave's trigger scribe, whose one-digit swap kept
enough 2-grams to clear the grounding overlap (numbers now need the title or
description to contain them; LEDGER_VERSION 6, so every cached hook re-earns
its place under the new floor). The numeric tokenizer closes its last known
seam — a signed mantissa ("-1e9" no longer decomposes into an evidenced "-1"
and "9") — and content-addressed provenance now means it: manifests are
re-hashed on every read (a FIX over a tampered manifest fails closed) and
`doctor` reports `tampered_manifest` alongside `missing_manifest`.

### An explicit nothing is an answer

`pick_by_meaning` distinguished "the thinker is unreachable" (None) from "the thinker
read the whole index and named nothing" ([]) — and recall then overrode the second
with word overlap anyway, handing back look-alikes for questions the store knows
nothing about. The explicit empty pick is now respected (`how: "meaning→none"`);
word overlap remains the fallback only when the thinker is unreachable.

### Docs

`allowSwitch` in the README now matches the code: it follows `store` (naming a store
binds the preset, fail-closed) rather than defaulting open.

### Identity is signed too (gate_version 5)

The slug was the one model-written surface outside the floor, and the one thing
the mark never signed: a draft staged as `12-gpu-rig.md` could be renamed to
`99-gpu-rig.md` and poured under the new identity with its mark still valid.
Now the slug is part of the gated surface, and the mark signs `slug + body` —
what a memory SAYS and what it IS CALLED are one signed claim. Drafts staged
before this release fail their mark check; re-stage them (drafts are transient
by design). Manifests say `gate_version: 5`.

### tidy merges under the lock, weave compare-and-swaps its source

Both rewriters of resident truth had a TOCTOU: they read a snapshot, spent
model time, and wrote the snapshot back — a memory poured meanwhile lost its
index line (tidy) or was missing from a cloth whose mtime called itself fresh
(weave). tidy now re-reads the index inside the store lock and merges line by
line, skipping any line that moved (`skipped_stale`, loudly). The weave fix is
below, from its own hands.

### Provenance readers are one door now

`_origin_key` read manifests with a bare `json.load`; now everything —
recurrence, FIX, doctor — goes through `load_manifest_verified`, which also
defines what a digest is (64 hex chars, refused otherwise, so the loader can
never be steered by a path-shaped reference). doctor audits EVERY
`*_manifest` pointer in frontmatter — `origin_manifest` and
`recurred_manifest` too, not just the newest — reporting `missing_manifest`
and `tampered_manifest` per memory.

### The server names its build

- `GET /health` names the build actually serving: `build_id` (from `KURA_BUILD_ID`
  stamped at launch, else `"unknown"`), package `version`, `pid`, `started_at`, the
  `module_path` actually imported, and the `config_path` actually loaded. Motive: a
  restart "succeeded" while an old 0.0.0.0-bound process kept the port and served
  three deploys' worth of stale code, and `/health` had no way to show it. The deploy
  postcondition (compare `build_id`) and the kill-by-port-not-by-interface caveat are
  in docs/OPERATING.md, "Deploying means proving it". `/health` is never part of a
  prefix-cached surface, so its volatile fields are safe.

### The weave, in its own hands

- The woven cloth is now compare-and-swapped on its SOURCE: `weave()` records the
  sha256 of the canonical index text it read, and `persist()` re-hashes the index
  under the store's write lock, refusing distinctly (`refused: "source moved while
  weaving"`, `kura weave` exits 2) when a memory was poured mid-weave — the old cloth
  stands and the caller re-weaves. Motive: the poured memory was missing from the
  cloth, yet the cloth's mtime was NEWER than the index, so the mtime staleness test
  called it fresh and pay-forward baked the stale map into KV.
- Staleness (`Loom.is_stale()`, and through it the serving-side check in
  `prefill.build`) is now judged by hash, never mtime: stale ⇔ current index hash ≠
  the hash `persist()` verified. The hash lives in a sidecar (`<cloth>.state.json`),
  never in the injected map — the cloth text stays byte-stable. A cloth with no
  record (pre-upgrade) is served as stale; one re-weave heals it.
- Triggers get the same deterministic floor for attribution as for numbers: a trigger
  that credits the human with a decision its source line never credited is rejected
  in `_acceptable` (via `gate.attributes_to_human`) and the mechanical trimmer takes
  over. A source that already credits the human may keep a crediting trigger.
  `LEDGER_VERSION` → 7 so cached hooks re-earn their place.

### The mark signs the envelope (gate_version 6)

Signing the name exposed the rest of the envelope. Two doors closed at once:
the judge is no longer a mint — a draft whose mark is invalid for its CURRENT
name is mechanically tossed before any model sees it (a rename used to be
laundered through FIX, which re-signed the stolen identity), and the mark now
signs `slug + kind + evidence-manifest digest + body`. `kind` decides pinned
status in the resident map, and a header edit used to promote a memory without
touching a signed byte; the manifest pointer could be swapped to a DIFFERENT
validly-hashed manifest, forging provenance that no tamper check would ever
see. Both attacks are regression tests now. `pour` also re-hashes the
manifest's bytes before the memory exists — a mark can be valid while the file
behind it rots, and provenance must exist before the memory does.

tidy's CAS gains its second end (the memory body the model read is re-read
under the lock, not just the index line), doctor reports
`invalid_manifest_pointer` for pointers that are not even digests, the
verified loader requires a JSON object, and the freshness stamp on the woven
cloth is a two-ended proof — from the weave's own hands:

- The cloth's freshness stamp now proves the PRODUCT as well as the source:
  `persist()` records `cloth_sha256` (the exact cloth bytes written) beside
  `source_sha256` in `<cloth>.state.json`, and `is_stale()` — and through it
  prefill's serving check — requires BOTH to match; a cloth corrupted or
  hand-edited while the index sat unchanged is served as stale (canonical
  fallback, one re-weave heals). Crash ordering unchanged: cloth first, record
  second, so a crash between the two yields "unprovable → stale", never a fresh
  stamp on old text.

### Three chores before the WAL

The gate key's first-boot race is closed (`O_CREAT|O_EXCL`: the first writer
mints, the loser reads the winner's key) and a short or corrupt `gate.key` is
now a loud RuntimeError, never a silent regeneration — a fresh key orphans
every existing mark in one stroke, the one repair that must never be
automatic. A draft whose mark cannot be verified is QUARANTINED
(`_still/quarantine/`, atomic rename, logged with its destination), not
deleted: an invalid mark means "origin unprovable", not "content unwanted".
And a judge's verdict now binds the exact bytes it judged (`judged_sha`,
re-checked at apply time) — a draft fixed by a parallel drain while the model
thought is "moved", and the stale verdict is discarded.

### The store survives power loss (WAL + revision)

`_write` changed two canonical files — the memory, then the index — with two atomic
replaces. Each was atomic; the pair was not, and the comment in `store.py` said so: a
crash between them left a memory nothing pointed at, which `doctor` could only report
as `not_in_index` after the fact. That was the store's last known crash hole, and it
is closed.

Now, under the store lock, the final bytes of both files are written and fsynced to
`_still/wal/<txid>/` (payloads plus an `intent.json` carrying their hashes and the
next revision) BEFORE any canonical file moves. Only then: memory replace → index
replace → revision replace, each write-fsync-rename, then the WAL entry is cleared.
A crash anywhere replays to the same final state, because the payloads ARE the final
state — not diffs. Replay runs on entry to every locked mutation (so it cannot be
forgotten, and a leftover promise cannot clobber a newer write) and in `doctor`,
which reports the txids it finished as `wal_replayed`. A transaction that fails its
own hashes — missing payload, mismatched sha256, unreadable intent — promised
nothing: it is moved to `_still/wal-quarantine/` with its payloads kept, named by
`doctor` as `broken_wal`, and never applied, rolled back, or guessed at.

`_still/revision` is new: one integer, bumped once per committed canonical mutation —
a poured or rewritten memory, an annotation, a tidied index — exposed as
`Store.revision()` (0 when absent) and in `doctor`. Single-file changes (`tidy`'s
index replace, `_annotate`) skip the WAL ceremony — there is no second file to fall
out of step with — and bump the revision before their atomic replace, so a crash in
between over-announces (a wasted re-read) rather than under-announces (a stale map
served until an unrelated write). The counter exists for the weave to consume next:
"did anything change?" without hashing the store.

- The cloth's freshness stamp now sees past the index text: `weave()` captures
  `Store.revision()` (before reading the index), the sidecar records
  `source_revision` beside the two hashes, `persist()` re-reads the revision inside
  the store lock and refuses ("source moved while weaving") when it moved, and
  `is_stale()` requires the revision to match as well as both hashes. Motive: the
  weave's real input includes memory types and body dates (`layer_of`) — a body-only
  change leaves the index byte-identical and slips any hash, but bumps the counter.
  A pre-upgrade sidecar without `source_revision` is unprovable → stale, healed by
  one re-weave; a store with no counter file honestly weaves at revision 0 ("no
  counted mutation yet") and starts counting from its first mutation.

## 0.2.0

The first release shaped by outside review: a security/isolation review, an adversarial
pass over the fixes it produced, and a third-party reproducibility review.

### Tier zero of recall: the fast path (new)

A deterministic five-head recognizer (slug/title containment, word IDF, character
3-grams with stop-grams, character 2-grams, the opening of the body) answers a DIRECT
question — one that names a memory — in well under a millisecond, skipping the
thinker's ~17k-token index prefill entirely. An honesty gate (top1 >= `gate`,
top1/top2 >= 1.15) keeps every paraphrase with the thinker: blind-tested against it
before porting — 14/14 agreement on direct questions, zero wrong answers, silent on
everything semantic. Its index is built lazily from the store's own data and cached
in-process, keyed on the canonical index's mtime and the memory count.

`[fastpath]` in the config (`enabled`, `gate`; per-store overridable), a
`fastpath_verdict` / `fastpath_ms` pair in every recall reply beside `how = "fastpath"`,
and a `fastpath` block in `doctor`. A side effect worth naming: a direct question now
still finds its memory when the thinker is down, instead of degrading straight to
word overlap.

### The resident map (new)

The index is now *worn*, not merely queried: a standing block in the system prompt so an
agent can see what is known — and, more importantly, what is not. Three layers (pinned /
fresh / trigger) from a blind A/B test showing that detail earns its place only for
recent things. Byte-stable by contract, degrades to an honest note rather than to silence
or to half a map, and never truncates the list to fit.

`kura weave`, `kura prefill`, `GET /prefill`, a `systemPrompt.section` in the DSH plugin,
and a `kura_map` tool for hosts that cannot inject.

### Rooms, tags, and the sentences that go with a memory (new)

The room is chosen before the conversation and a memory never leaves it; a memory
may carry several **tags** — words about its character, never weights — and three
curation sentences: `belongs_because`, `keep`, `may_fade`. The distiller proposes
them against the store's charter; a tag that claims something about the human
(`entrusted`, `emotion-carried`, `recurred`) is checked deterministically against
the quotes, and both the basis and every refusal go into the evidence manifest
(`gate_version` 2, additive). `recurred` is written once, by the distiller, when
the human raises a covered topic again from another journal — decided, never
proposed, never counted.

The prompts no longer rank every store alike (decisions, then emotion, then topics
returned to …): the charter ranks, and emotion and recurrence are things not to
walk past. `examples/rooms/` carries a five-room layout — Research / Develop /
Manage / EQ / USER — with charters and a config where each room drinks from its
own journal. The core still serves any stores and any selectors.

A wide room may keep a learned `profile.md` beside its charter, in sentences, read
after the charter by its own distiller and never entering the resident map.
`kura profile draft` writes one from that store's memories; `kura profile apply` is
a person copying a file they have read. A profile carrying numbers about how much
things matter is reported as broken and not read.

A claiming tag needs its evidence, and `landmine` needs an *actual* failure — an
error in `[TOOL]` output, or a warning or correction in the human's words; a quiet
`df` line is tool output and nothing else. The verified door signs the curation it
writes (`curation_mark`, same per-store key as the draft gate mark), and `doctor`
names a hand-edited tag on a `distiller-only` store as `tampered` or `unsigned`.

`POST /annotate`, `kura annotate`, `tags`/`belongs_because`/`keep`/`may_fade` on
`/remember` and the MCP `kura_remember`; `GET /memory` returns them; `doctor` reports
`invalid_tags`, `missing_manifest`, `learned_profile` and **capacity in four units
with `limit` and `pressure` left `None`**.

**Not in this release:** forgetting. Nothing is garaged, settled, absorbed,
released or deleted, and no unit or limit is chosen. `docs/DESIGN.md` §8 says what
is undecided and why the first pass will be a dry run.

### The editor, and the watcher (new)

The model you talk to is also the **editor** that writes and judges memories in its
idle minutes — that is the default, and a GPU model does it acceptably (measured on
the house's Qwen: six drafts in 33 s, judged in 5 s, with reasons in the evidence
vocabulary). The upgrade path is an editor on its own seat, including a CPU model
that never competes with the conversation. `kura tend` is the watcher: quiet is the
newest journal's mtime; after `idle_min` it drains, distils, re-weaves once per
silence and tidies once; a track with nothing to do exits 2 and rests
`backoff_min`; work is counted, launches are not; every track's output is kept;
a heartbeat that `doctor` reads says whether anyone is tending the store; the human's
return stops a running track unless `yield_on_return = false`. Rebuilt from the
five-day record of the house's first watcher and the four ways it went wrong.

`kura distill catchup` marks every journal as drunk up to now, so pointing a
distiller at an existing history does not start by re-reading all of it. Forward
only — it cannot lose progress.

An extension's heading now carries the evidence's date (the journal file's mtime)
and a heading that says otherwise is corrected mechanically — 30 of 39 extension
headings in the house had been dated before the distiller existed.

Not shipped, on purpose: an autonomous research loop. It stays on the house side.

### Boundaries

- **Containment.** Every lookup resolves into the set of memories a store actually holds.
  `GET /memory/..%2Fother%2Fsecret` used to return another store's memory; a `[[../x]]`
  link used to walk there. Explicit reads are exact; only a model's pick is fuzzy.
- **Write authority.** `write_policy = direct-allowed | distiller-only | frozen`, with
  `remember_direct()` and `pour_verified()` as separate doors. The deprecated
  `readonly = true` now means `distiller-only`, which is what it always claimed — it
  used to refuse the distiller's pour as well. The gate signs what it stages and the
  pour verifies it, so a hand-written draft does not pour.
- **Isolation.** Per-store journal roots (no implicit inheritance above one store),
  per-store `model_profile`, and load-time refusal of aliased, nested or
  journal-overlapping stores, mode/store name collisions, unknown or mistyped keys, and
  partial model profiles.
- **`docs/TRUST.md`**, which states plainly that several kura behind one server are
  independent as *routing*, not as confidentiality: one trust level per process.

### Measurement (new)

`kura bench compress` and `kura bench retention`, `_still/metrics.jsonl`, and
content-addressed evidence manifests under `_evidence/` referenced from each memory's
frontmatter, so "why does this memory exist?" stays answerable after the draft is gone.

Measured with the shipped fixtures: `store_ratio` 0.18 on ordinary chat and 1.14 on dense
material. The ratio is a property of the corpus, not of the tool.

### Fixed

- a FIX verdict kept only the first draft header line, so DESC was lost and the
  memory poured with its slug as the index trigger
- a Claude Code subagent transcript records the PARENT MODEL's prompt as `type: user`
  with `isSidechain: true`; it was classed [USER], so a model's "the owner approved X"
  could pass the gate as the human's decision. Sidechain text is [SELF] now (tool
  results stay [TOOL]). 360 of the house's 391 journal files were sidechains
- `tidy()` wrote the index with a bare `open()`, outside the store lock and the atomic
  replace every other index write uses
- an EXTENDS pour overwrote `evidence_manifest`, erasing where the memory came from;
  the first manifest is now pinned as `origin_manifest` and `recur()` reads that
- `profile_draft()` read `_study/` notes first (underscore sorts before letters) and
  a few long notes spent the whole budget before it saw a memory
- `known_slugs()` matched `(AGENTS.md)` inside an index line's prose and reported an
  orphan that was never a memory; only link targets count now
- `commitment` passed `verify_tags()` unconditionally; it is a claim about the human
  and now needs a [USER] quote like `emotion-carried`
- the resident map had no store identity: a failed switch left the previous kura's index
  in the prompt while recall went to the new one
- the trigger quality gate tested the alphabet, rejecting good Japanese triggers (and ★)
- a memory and its index line were two writes; concurrent writers lost index lines
- `chars` in recall was per memory and read as a total; `total_chars` is a hard ceiling
- `doctor` and `/index` still used `len//2`, biased low against every real tokenizer
- the loom could write into a memory slot, destroying it one weave at a time
- `tidy()` and `init_files()` wrote into frozen stores
- `pour '../../../x'` read a file from anywhere on the filesystem into the store
- hardlinks: reported on the read side, filtered by inode on the intake side
- the DSH plugin pinned `@deepseek-ai/dsh-tools` as a direct dependency, so a profile
  could load a second physical copy and split its module-local Symbol identity — the
  first tool call died on `undefined.prepare`. Now a `"*"` peer: the host supplies the
  one copy it already has. First outside contribution, by @kisaragi-mochi (#1)

### Compatibility

Endpoints have a `dialect` (`vllm` | `openai` | `generic`) and record why a call failed.
"OpenAI-compatible" means it answers `POST <url>/chat/completions` in that shape — a
vendor's native API needs a gateway in front of it.

## 0.1.0

First public release: recall by recognition, the evidence gate, several kura behind one
server, the distiller, the DSH plugin and the MCP bridge.
