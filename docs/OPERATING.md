# Operating a kura

## Run it resident

The server is a single Python process, stdlib only. systemd (user unit):

```ini
# ~/.config/systemd/user/kura.service
[Unit]
Description=distill-kura
After=network.target

[Service]
Environment=KURA_CONFIG=%h/kura/kura.toml
ExecStart=/usr/bin/python3 -m distill_kura.cli serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now kura
curl -s localhost:8085/health
```

The distiller is separate on purpose — the mouth must stay up even when the distiller
is wedged, and the distiller must be killable without taking recall down with it.

```ini
# ~/.config/systemd/user/kura-tend@.service      (one instance per store)
[Unit]
Description=distill-kura watcher for %i
After=kura.service

[Service]
Environment=KURA_CONFIG=%h/kura/kura.toml
ExecStart=/usr/bin/python3 -m distill_kura.cli -s %i tend
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now kura-tend@maker kura-tend@eq
curl -s 'localhost:8085/doctor?store=maker' | python3 -c 'import json,sys; print(json.load(sys.stdin)["tending"])'
```

`Restart=always`, not `on-failure`: the first watcher this replaced was started by a
boot script, died with the machine, and was not missed for twelve days. `tend` works
only once the journals have been quiet for `idle_min` (10) minutes, rests a track that
had nothing to do for `backoff_min` (20), stops a running track when the journal
changes (unless `yield_on_return = false` — an editor on its own seat), and keeps every
track's output in `_still/tend.log`. `distill night` still exists for a bare
one-pass-when-quiet loop, but `tend` is the one to run.

## Deploying means proving it

**Issuing a restart command is not evidence the new build is serving. (`build_id` is a launch stamp — what the launcher claimed — not a code attestation; it is exactly enough to catch a stale survivor holding the port).** The night this
section was written, a restart "succeeded" — and an old process, bound to `0.0.0.0`,
kept the port and served three deploys' worth of stale code while `/health` answered
`ok: true` the whole time. Two habits close that hole:

**Stamp the build at launch, and make `/health` the postcondition.**

```ini
# in kura.service, [Service]
Environment=KURA_BUILD_ID=<commit>     # else /health says "unknown", which is honest
```

```bash
systemctl --user restart kura
curl -s localhost:8085/health | python3 -c \
  'import json,sys; h=json.load(sys.stdin); print(h["build_id"], h["pid"], h["started_at"])'
```

The deploy is done when `build_id` matches the commit you just shipped — not before.
`/health` also names the package `version`, the `pid`, `started_at`, the `module_path`
actually imported and the `config_path` actually loaded: enough to tell "the process I
just started" from "a survivor of three deploys ago". These fields are volatile on
purpose, and safe where they are — `/health` is never part of a prefix-cached surface.

**Kill by port, not by interface.** The survivor lived because it was bound to
`0.0.0.0` while the kill was filtered on `127.0.0.1` — the filter matched nothing, the
restart started a second process, and the old one kept winning the port:

```bash
ss -ltnp 'sport = :8085'    # see WHO holds the port, however it bound
fuser -k 8085/tcp           # reap the holder by port, not by address match
```

## Keeping the resident map current

The map the agent wears is only as good as its last weave.

```ini
# ~/.config/systemd/user/kura-weave.service   (+ a .timer, or a cron line)
[Service]
Type=oneshot
Environment=KURA_CONFIG=%h/kura/kura.toml
ExecStart=/usr/bin/python3 -m distill_kura.cli weave
```

Weave after the distiller pours, and once more in the morning:

```bash
kura distill drain && kura weave
```

A weave in the steady state costs nothing: trigger lines are cached in a ledger keyed on
the description *and* the budget, so only genuinely changed lines reach a model, and an
unchanged cloth is not rewritten at all (`written: false`). Re-weaving on a tight timer
is still pointless — the index changes a few times a day, not a few times a minute.

**What to watch:**

| signal | meaning |
|---|---|
| `kura prefill` exits 2 | no cloth yet, or the cloth is stale — the full index is being served instead |
| `over_budget: true` | the map costs more than `budget_fraction`. Nothing was dropped |
| `budget_met: false` | no setting reaches the budget; the vivid layer was kept deliberately |
| `over_ceiling: true` | the map is not being shown at all — a stub went out instead. Act on this |
| `hooks_mechanical` high | the scribe is failing the quality bar, or is unreachable |
| `grouped` large | many lines name several memories each; those are never trimmed |

If the map will not fit: lower `trigger_tokens`, narrow `pinned_types`, shorten
`fresh_days`, raise `budget_fraction` if the window can afford it — or split the store,
which is the honest answer past a few hundred memories.

### When the map alone is over the ceiling: the constellation

`[prefill] resident_mode` picks what the agent wears. `full` (the default) is
today's map; `auto` keeps the map while it fits and wears the *constellation* only
when the map alone is over the hard ceiling; `constellation` wears it always. The
constellation is a map of SECTORS — the index's own `## ` headings, no model, no
clustering — one line per sector: its memory count and up to three example titles.
It says plainly that a memory not named may still exist inside a sector, which is
why it is not the map and `map_shown` stays false while it is worn.

Every memory lands in exactly one sector (first index line wins; the rest are
`UNSECTIONED`), and the invariant `sum(sector counts) == memories` is checked in
code. Inspect it with `kura constellation` (`--json` for the counts), or watch
`constellation.unsectioned` in `kura doctor` — a large unsectioned count is the cue
to add `## ` headings to `MEMORY.md`. The trail still rides after the block when it
fits, and a constellation that will not fit itself degrades to the honest stub.

**Never** hand-edit the woven cloth. It is derived; the next weave overwrites it. Edit
the canonical `MEMORY.md`, or the memory itself, and re-weave.

**Never** hand-write a draft into `_still/drafts/` and expect it to pour on a
`distiller-only` store: the gate signs what it stages and the pour checks the signature.
Write the memory with `kura remember` on a `direct-allowed` store, or let the distiller
produce it.

## Paying the map forward

A fresh map is minutes of cold prefill on a slow mouth — measured on one machine (320B
pure-CPU llama.cpp, 16,444-token map): 796 s to bake, 283 ms to save the slot (1.5 GB
on NVMe), 655 ms to restore it after killing and rebooting the server, and the first
turn after the restore reprocessed 18 prompt tokens. `kura pay-forward` pays that bake
once, in the quiet hours, and keeps it on disk.

The mouth must be a llama.cpp server started with `--slot-save-path` — without it the
save fails (loudly, with this flag named in the error) and every run pays the bake
again:

```bash
llama-server -m model.gguf --slot-save-path /var/lib/llama/slots ...
```

```toml
[[payforward.mouths]]
name = "cpu-mouth"
url = "http://127.0.0.1:8014"   # server BASE — the slots API lives beside /v1, not under it
store = "main"                  # whose map this mouth wears
# slot = 0                      # the llama.cpp slot saved from / restored into
# model = "local"               # alias for the one-token probe call
# api_key_env = "MOUTH_KEY"     # read from the environment, never stored here
```

`kura tend` already runs it as a track after each weave. By hand, the shape is the
same as the weave's:

```bash
kura distill drain && kura weave && kura pay-forward
```

Exit codes follow the house convention, with failure checked first: 1 = ANY mouth
failed or was busy — even when others worked, because a run that baked A but found B
locked must be retried for B, not rested on; 0 = something was baked or restored and
the whole fleet is covered; 2 = every mouth fresh — VERIFIED fresh, with a restore
and a one-token probe, never assumed. Busy (`skipped-locked`) is deliberately not
fresh: a held slot lock proves another runner exists, not that it is warming your
etag (it may be finishing an older map), so it is transient and a scheduler retries.
A failure is loud, labeled, and never advances `_still/payforward.json`; the ledger
itself takes a second, millisecond-held lock for its read-modify-write, because two
mouths of one store hold two different slot locks and share one ledger file.

A mouth restart wakes up cold; the slot file makes warming it a sub-second restore, so
hang a oneshot off the mouth's unit rather than waiting for the next weave:

```ini
# ~/.config/systemd/user/kura-payforward.service
[Unit]
Description=distill-kura pay-forward
After=llama-mouth.service
BindsTo=llama-mouth.service

[Service]
Type=oneshot
Environment=KURA_CONFIG=%h/kura/kura.toml
ExecStart=/usr/bin/python3 -m distill_kura.cli pay-forward
SuccessExitStatus=2

[Install]
WantedBy=llama-mouth.service
```

`SuccessExitStatus=2` because "every mouth fresh" is the good outcome, not a failure.

Old slot files are not pruned: the slots API can save and restore a filename but
cannot list the directory, so pruning from here would be a guess about files it cannot
see. They scale with map length times the model's KV width (that 16k-token map was
1.5 GB), so sweep `--slot-save-path` by hand when it grows.

### Measuring it: `kura bench payforward`

```bash
kura bench payforward --mouth cpu-mouth            # add --skip-cold on a CPU mouth
```

One row per condition; the number that matters is `prompt_n` — the mouth's OWN count
of prompt tokens it reprocessed, not our estimate. Reading it: `restore-spine+trail`
near `trail_tokens_est` means the spine is warm; `trail-changed` smaller still means
only the tail moved; `map-changed-first-line` back near `cold-full` is the volatile-
header proof — one character at the front re-prices everything behind it;
`warm-repeat` small means the prefix cache is doing its job with no disk involved.
If no slot file exists for the current etag, a `bake-spine` row appears first (the
pay-forward bake, paid once). Exit 0 unless the mouth is unreachable (1, with the
reason); the store is never written and the mouth is left warm on the current spine.

## Scheduling by hand, and exit codes

```bash
kura distill run    # 0 = did work, 2 = nothing worth drinking
kura distill drain  # 0 = poured or tossed something, 2 = no drafts
kura distill tidy   # 0 = repaired a line, 2 = index is clean
kura prefill        # 0 = a current cloth was served, 2 = no cloth or it is stale
kura pay-forward    # 1 = ANY mouth failed or was busy — retry, even if others worked;
                    # 0 = worked, whole fleet covered; 2 = every mouth VERIFIED fresh
kura tend --once    # 0 = the requested work completed successfully
                    # 1 = work was attempted or required and did not complete — retry
                    # 2 = honestly nothing to do
```

`tend --once` prints one JSON line with the outcome: `error` is `null` on 0 and 2, and
on 1 is one of `track-failed` (the track ran and reported failure), `yielded` (the
human came back and the track was stopped), `child-error` (the child died badly, e.g.
on a signal) or `timeout` (`--timeout`, default 3600 s, passed with the track still
running). All four carry `"retryable": true`. A timeout used to exit 0 — a scheduler
read "started something, deadline expired" as done and never retried. When `--once`
returns, the track it started is stopped (TERM, short grace, KILL) and reaped: no
child of that run is left holding the seat.

**Exit 2 means "there was nothing to do".** A scheduler must distinguish it from
success. A loop that treats "found nothing" as "did work" spins, and the spinning
crowds out the steps that need idle time — measured once as 184 empty passes in 42
minutes, during which the pour step never ran at all.

A reasonable cron shape (one queue, in order, stop at the first idle step):

```bash
kura distill run && kura distill drain; kura distill tidy -n 3
```

## Watch these

```bash
curl -s localhost:8085/doctor | python3 -m json.tool
```

| signal | meaning | do |
|---|---|---|
| `how: words` in recall answers | the thinker is unreachable; quality has dropped | restart the model endpoint |
| `links_dead` growing | memories reference slugs that do not exist | fix the links or write the missing memory |
| `islands` growing | memories nothing links to; they will rarely be recalled | link them, or accept they are archive |
| `index_tokens_est` near your window | the index no longer fits in one prompt | split into a second kura, or a two-level index |
| `not_in_index` | a memory exists but nothing points at it | it is invisible to recall — index it |
| drafts piling up in `_still/drafts/` | `drain` is not running | run it; check the scribe endpoint |
| `_still/tossed.jsonl` growing fast | the brain is proposing junk | look at what it proposes; the prompt or the journal source may be wrong |
| `invalid_tags` non-empty | a `tags:` line a reader cannot parse; the memory reads as untagged | fix the line by hand (it is a JSON array of lower-case kebab words) |
| `missing_manifest` non-empty | a memory points at an evidence manifest that is gone | restore `_evidence/` from backup; the memory still reads, its provenance does not |
| `curation.tampered` non-empty | a memory's tags or sentences were edited after the gate signed them | read the file; restore from the manifest or re-pour. On a `distiller-only` store this is the one thing nobody but the gate should have done |
| `curation.unsigned_names` non-empty | tags or sentences with no mark, on a store where only the gate writes | someone wrote frontmatter by hand; decide whether it stays (it reads fine; it just is not the gate's word) |
| `tending.alive: false` | no watcher is tending this store (never started, or died) | `systemctl --user status kura-tend@<store>`; the heartbeat is `_still/tend.json` |
| `learned_profile.state: broken` | `profile.md` is unreadable or carries numbers about how much things matter; it is NOT read and the fixed charter carries on | read `why`; rewrite the profile in sentences |
| `capacity.*` | the store's size in four units — memories, index tokens, body tokens, bytes. `limit` and `pressure` are `None` on purpose | nothing acts on these yet. Watch them, and see the next section |

## When a shelf is full

Nothing is forgotten automatically, and `capacity` has no limit until a person sets
one. The unit a store is measured in, the limit, whether a wide room shares it, how
candidates are compared, where a garaged memory goes and for how long, which tags
protect absolutely and which only argue, who approves, and what finally deletes a file
— none of that is decided, and `docs/DESIGN.md` §8 says why it will be decided with
real memories in front of the people whose memories they are rather than by a default.

What you can do today is keep the material that decision will need:

- the distiller writes `belongs_because` / `keep` / `may_fade` on every memory it pours,
  so "what does this still do here?" has a first answer on file
- `kura annotate <slug> --keep "…" --may-fade "…"` adds them to older memories by hand
  (direct door: `direct-allowed` stores only)
- `kura doctor` once a week into a log, so the four capacity units have a history

When the first forgetting pass comes it will be a dry run — store, pressure, candidates,
their tags and sentences, the reason each could be released, what must be kept, and the
proposed action — and it will modify nothing.

## Back it up

A kura is plain markdown in a directory. Anything works — `git` is a good default,
because a memory store's history is worth having:

```bash
cd ~/kura/main && git init && git add . && git commit -m "kura snapshot"
```

Back up to a **different physical device**, and verify a restore. A copy that has been
silently failing for a week is the same as no copy: it is worth checking that the
newest file in the backup is actually new.

What to back up, and what not to:

| directory | keep it? |
|---|---|
| the memories and `MEMORY.md` | **yes** — this is the store |
| `_evidence/` | **yes** — the manifests memories point at. Losing them makes "why does this memory exist?" unanswerable, which is the question the whole gate exists to keep answerable |
| `_still/` | no. Workshop: drafts, watermarks, the read log, dropped and tossed candidates, metrics. Deleting `watermark.json` makes the distiller re-drink every journal from the beginning — expensive, not harmful |

`_still/metrics.jsonl` is worth keeping if you care about trend: it is one line per
distilled batch and the only place `kura bench compress` can learn how much raw material
a store actually consumed.

## Feeding it

```toml
[distill.journals]
claude = "~/.claude/projects/my-project"   # Claude Code transcripts (.jsonl)
dsh    = "~/dsh/sessions"                  # DSH sessions (.jsonl.zstd; needs `zstd`)
lna    = "~/lna/journals/v1"               # Lna Harness runs (*.lna.jsonl, one per line)
text   = "~/notes"                         # .md/.txt/.log, all treated as [USER]
```

Two harnesses may feed one kura at once. That is the ordinary shape while a node changes
hands: the agent you talk to today and the one taking over from it both leave journals,
and the memories should be continuous across the handover.

```toml
[stores.main.distill.journals]
dsh = "~/.dsh/sessions"
lna = "~/.local/share/lna-harness/journals/v1"
```

Newest journals are drunk first: today's decisions are worth the most. A batch is
`chunk_chars` (default 200k) of classed text — aim it at what your brain model swallows
comfortably in one gulp, because a bigger batch means fewer, better-contextualised
candidates rather than more of them.

Per-store journals, when two kura are fed from different places:

```toml
[stores.eq.distill]
language = "日本語"
[stores.eq.distill.journals]
text = "~/notes/dialogue"
```

## Multiple kura on one server

Everything is store-selectable, so one process is normally right. Watch two things:

- **Watermarks are per store.** Distilling the same journal into two stores is allowed
  and each keeps its own position — useful when a conversation is relevant to both, but
  it doubles model cost, so bind journals per store when you can.
- **`write_policy = "distiller-only"`** on a store refuses direct writes (a tool call,
  the CLI, `POST /remember`) while still accepting the distiller's verified pour. That is
  the recommended setting for anything an agent reads from constantly: every memory then
  has to pass the evidence gate. `frozen` refuses the pour too. The deprecated
  `readonly = true` maps to `distiller-only`. See `docs/TRUST.md`.

- **Bind each store to its own journal root.** With more than one store configured,
  nothing inherits `[distill.journals]`; a store with no `[stores.<name>.distill.journals]`
  drinks nothing. (`kura stores` does not report journal roots — read the config, or
  watch a store's intake in `kura doctor`.) This is the whole reason two rooms can
  remember two sides of one afternoon: each only ever sees its own conversations. The
  five-room example in `examples/rooms/` shows the shape.
- **A memory never changes store.** Tags do not move it, a mode change does not move
  it, and there is no move or copy command. If the same topic belongs in two rooms,
  each room distils its own memory from its own evidence.

## The wide room

A store whose charter is "understand the person, without a narrower purpose" may keep a
`profile.md` beside its charter: a few sections in sentences — enduring threads, current
interests, everyday context, conversation preferences, unresolved threads. That store's
distiller reads it after the charter, so what the room keeps from then on follows what
it has come to understand. It never enters the resident map, and `GET /profile` hands it
to a host with its state.

```bash
kura -s user profile show      # state (absent / present / broken), the text, and whether a draft waits
kura -s user profile draft     # → _still/profile.draft.md, from THIS store's memories only
$EDITOR ~/kura/user/_still/profile.draft.md
kura -s user profile apply     # a person copying a file they have read. Never automatic
```

A profile is text. One that carries numbers about how much things matter (`trading:
0.8`, `interest score 7`) is the weight this project refuses to store: it is reported as
`broken`, not read, and the fixed charter carries on — visibly, in `doctor`, not as a
quiet fallback. Whether and when a draft should ever be applied without a person is a
decision to make with real drafts in hand; the command exists so those drafts can be
collected.

## Adding a journal format

Subclass `Source` in `distill_kura/distill/sources.py`, register it in `SOURCES`, and
choose the watermark unit deliberately: byte offset for append-only files, a sequence
number carried in the events for anything that gets rewritten. Then add a key under
`[distill.journals]`. The evidence classes are the contract — get the mapping right
(`USER` / `TOOL` / `ACT` / `SELF`, and drop reasoning blocks) and everything downstream
works unchanged.

Three things bite:

- **`source_for()` is first-match over a hand-ordered tuple, and `SOURCES` order does
  not matter.** `ClaudeCodeSource.matches` answers for *any* `.jsonl`, so a new adapter
  whose files also end in `.jsonl` must be listed BEFORE `claude` there or it will never
  be reached. `lna` (`*.lna.jsonl`) is placed ahead of it for exactly this reason.
- **A journal kind nobody registered is dropped in silence.** Nothing validates the keys
  under `[distill.journals]` against the adapter names, and `discover_all` skips a kind
  it does not recognise. A typo therefore loads cleanly and reads nothing at all; check
  `kura doctor`'s intake numbers after adding a root rather than assuming it took.
- **A walk that raises stops the whole intake, not just its own file.** `Watermarks.claim()`
  iterates every discovered journal in one loop with no per-file guard, so an adapter that
  fails loud on corruption — `lna` on a completed line that will not parse, `dsh` on an
  unreadable archive — blocks that store's other journals too until the file is repaired
  or moved aside. That is the intended direction (filing a corrupt evidence source as
  drunk is worse), but it is an outage, so it belongs in the runbook: the error names the
  file and the byte offset.

## Measuring rather than guessing

```bash
kura bench compress     # store_ratio and map_ratio, from the distiller's own metrics
kura bench retention --questions bench/fixtures/questions.json
```

`bench compress` refuses to compute a ratio against journals the store never drank. Pass
`--tokenizer-command` before quoting a figure anywhere: without it the numbers carry an
`estimated` label and a warning, because the built-in estimator is up to ~20% out against
tokenizers it was not fitted on.

`bench retention` exits non-zero below 0.9, so it can sit in a scheduled job. Write your
own questions against your own corpus — the shipped fixture measures the tool, not your
store.

### Comparing writers on the same evidence

Freeze the unprocessed journal candidates first; packet preparation uses the normal
brain and deterministic gate, but does not advance the production watermark:

```bash
kura bench packets --store main --chunks 2 --out packets.json
kura bench writers --store main --packets packets.json --from-config --out writer-report
# or add one-off endpoints as --writer name=http://127.0.0.1:8011/v1/model
```

`writer-report/report.md` pairs every packet and writer and links to the exact surface
in `writer-report/<writer>/`. `report.json` keeps raw latency, optional endpoint usage,
surface length, retry outcome, and separate counts for invented numbers, quotations,
dead links, attribution, and shape failures. There is no quality score to average away
an unsafe pass. Add writers under `[[bench.writers]]`; `rep = 1.05` is shorthand for the
endpoint `extra` field `repeat_penalty` and is shown in both reports.

## Security

There is no authentication. The default bind is `127.0.0.1`. If it must be reachable
from elsewhere, put a reverse proxy with auth in front; do not add a half-built auth
layer to the server. Remember that a kura is a fairly intimate document — it holds what
someone decided, what annoyed them, and what they came back to.


## The adaptive shadow (M4)

Turn it on per store or globally with `[prefill] adaptive_triggers = true` and run
`kura weave` as usual; the production cloth is unchanged, and `_still/adaptive.json`
gains one record per trigger-layer memory. Read `summary.saved_tokens` for the size of
the prize and `summary.reasons` for what stands between you and it — "ambiguous" means
a neighbour, "not offered" means the scribe gave no cue at that rung, "negation
dropped" / "number re-bound" / "retirement word dropped" mean the cue lied. The
scribe is called once per memory and never again while the line is unchanged; the
verdicts are recomputed on every weave, which is why a shadow is cheap to keep on.

To benchmark it, render the shadow into a map (`Adaptive(store, loom).render()`; a
CLI flag arrives with the M4 benchmark step) and hand it to
`kura bench worldline --routing agent-only --resident canonical,woven,adaptive
--resident-file adaptive=/path/to/map.md`. Promote (`adaptive_apply = true`) only when
that table shows no new wrong or obsolete branch and no rise in
`remembered_but_unreachable` — the memory that exists but cannot be reached.


## Reading a worldline run (and promoting on one)

`kura bench worldline` prints two tables. The first is every runnable case per
resident variant. The second, `paired-format-valid`, restricts the same counts to
the cases that came back in a readable format in EVERY variant of that run — a map
that garbles the questions it finds hard scores those rows as failures for itself
and never for its rival, so the first table can move for a reason that has nothing
to do with the map. Promote on the second.

The header carries `case_set_sha`, the sha256 of the cases file's bytes, and so
does every trace row. Two runs whose digests differ answered different questions;
their numbers are not comparable, whatever the case counts say.

In agent-only routing three columns say how a reply failed, not merely that it
did: `format_error` (the whole reply was not the JSON array that was asked for),
`truncated` (the model hit the token cap) and `reasoning_only` (the visible answer
was empty while the model reasoned at length). The last two never excuse the
first — a cut reply and a reply that was all private thinking are both answers
nobody was given — they only say which repair is worth making: raise `max_tokens`,
or lower the reasoning effort. The trace's `reply_head` keeps the witness, prefixed
`[reasoning] ` when there was nothing visible to show.

To compare two runs:

```bash
kura bench worldline --routing agent-only --resident canonical,woven --json > A.json
# change something, run again
kura bench worldline --routing agent-only --resident canonical,woven --json > B.json
kura bench worldline-compare A.json B.json        # --json for the raw numbers
```

It refuses outright when the two `case_set_sha` values differ. Per variant present
in both it prints recovery over all cases, recovery over the cases that were
format-valid in every variant of both runs, the `format_error` delta, and the four
safety counts with their deltas. There is no composite score, and there will not
be one: the day a single number exists, it is what gets optimized.

Promote a map change only on:

> an improvement clearly above the run-to-run noise envelope observed on the frozen
> case set, reproduced by two independent readers, with no worsening of wrong
> branch, obsolete resurrection, remembered-but-unreachable, or format_error

Current observed noise envelope: about ±2 / 42 (house set v1, 2026-09-02);
re-measure when the case count changes.


## Typed worldline edges (M7)

`kura edges` prints the typed edges the store implies — `continues`, `next`,
`supersedes`, `rejected`, `blocked-by` — derived from each memory's own `[[links]]`
and the cue words on the same line, never written into a memory. `--slug S` narrows to
one memory; `--json` gives the whole payload with `counts`, `unevidenced` and
`dropped`.

Read `unevidenced` first: it counts `supersedes`/`rejected`/`blocked-by` claims that
were dropped because the source memory has no verified evidence manifest (or none with
the class the type demands — USER for the first two, USER/TOOL/ACT for `blocked-by`).
A store that distills through the gate accrues edges; a hand-written store accrues
only `continues`/`next`, and that difference is the report working, not a fault.

The edges live in one derived cache, `_still/edges.json`, and are rebuilt whenever the
store moves; delete it freely. `kura glance` shows up to three as `RELATIONS:` under
its token target, the Hot Trail gains `↳ source continues → target` lines for fresh
breadcrumbs only, and `kura bench worldline` reports `edge_says_obsolete` per trace.
`kura doctor` carries the edge counts on its health line.


## The richness gauge (plan §15)

`kura metrics richness` answers one question in two parts: is the store still
remembering, and is what it remembers still true? It reads the `_still/*.jsonl`
logs the distiller already writes — nothing is called, nothing is written — and
prints each number with its denominator, for the whole range and again per rolling
window (`--window DAYS`, default 7) so a trend shows. `--since DAYS` bounds the
range; `--json` gives the whole dict.

What each number means:

- **candidate rate** — candidates the brain found per journal segment drunk, also
  per `source_key`; a spread means one journal drifted, not the house.
- **rejections** — what the gate refused and why (`why_dropped` counts), the share
  that carried a `reason`, and how many claimed numbers nothing grounded.
- **USER survival** — `by_class.USER` candidates against `gated_kept`. The writer
  records no per-class keeps, so the kept cell says "not recorded by the writer";
  the gauge invents no proxy.
- **seeds / SELF retreat** — ideas diverted to `_still/seeds.jsonl` per
  `gated_kept`, plus what the drain TOSSed or quarantined.
- **callsigns** — cue receipts issued, receipts refused, cues the gate refused on
  dropped candidates.
- **remembered_but_unreachable** — per resident variant: the map said it, recall
  could not open it.
- **per resident_sha** — target-reached per map identity, so a re-weave is
  compared with the map that actually wore the traces.
- **fallback thinker** — reads by `why` and how often recall fell back to the
  thinker; recall's own "how" (fastpath vs meaning) reaches no log, and the
  output says so and names the writer that would have to carry it.

The one warning, computed not judged: when the latest window shows gate
rejections up AND `gated_kept` down against the previous window —

    WARN richness: rejections up (a→b), kept down (c→d) — is the gate learning
    honesty or silence?

It is a gauge, not a gate: exit code stays 0, and a human decides what the
pattern means.
