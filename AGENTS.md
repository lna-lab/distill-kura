# AGENTS.md — working on distill-kura

Instructions for an agent (or a person) changing this codebase. Hosts that read
`AGENTS.md` — DeepSeek Harness via `dsh-agent-instructions`, Claude Code, others — pick
this up automatically.

## What this project is

A long-term memory for agents: recall by meaning, writing gated by evidence, several
independent stores so an agent mode change is a memory change. Standard library only,
Python ≥ 3.11. No dependencies is a feature — it lets the whole thing be dropped next
to any host, and it keeps the trust surface small for something that decides what an
agent believes.

## Boundaries

`docs/TRUST.md` states what a store boundary is and is not, and the honesty of that
statement is load-bearing. Two rules follow from it and must not be weakened:

**Every lookup resolves INTO `slug_set()`.** Never build a path from a caller-supplied
name and check whether it exists — that was the hole: `GET /memory/..%2Fprivate%2Fsecret`
returned another store's memory. Containment is membership in a set, not a blocklist of
characters. `contained()` (realpath + commonpath) sits behind it as defence in depth.

**Explicit reads are exact; only a MODEL's pick is fuzzy.** `read_exact()` for a slug from
a person, a `kura_read` call, or an HTTP route. Fuzzy `resolve()` stays for thinker picks
and `[[links]]`, where every candidate comes from the slug set — a deliberate deviation
from "links exact only", because in-store fuzzy resolution demonstrably connects real
links (`[[brain-memory]]` → `_study/brain-memory`) and cannot leave the store.

**Every path that writes into a store asks the policy.** Not just `remember_direct` and
`pour_verified` — an adversarial pass found `tidy()`, `Loom.persist()` and `init_files()`
writing into a frozen store, and `kura weave --no-model` destroying a memory's body via
`cloth_path`. When you add a code path that touches a store directory, the question to
answer in review is not "is this a memory?" but "would this run on a frozen store?".

Do not add a permission layer inside the process. An OS user boundary is stronger, older
and easier to verify than any token check this core could carry, and pretending otherwise
would invite people to rely on it.

## The one rule that outranks the others

**No model output becomes a stored fact without a mechanical check.**

Everything in `distill_kura/distill/gate.py` is deterministic Python, and it stays that
way. If you are tempted to "let the model decide" something the gate currently decides,
you are re-opening the failure this project exists to close: an agent asserts something,
the distiller records the assertion, the next agent reads it back as ground truth. That
loop is self-reinforcing. Prompt instructions do not stop it — that was measured, not
assumed.

Prompts may *help a model pass the gate honestly*. They may not *replace* it.

The same rule has a second face in `weave.py`: **compression may shorten a description,
but it may never lose, reorder or invent a link.** That is checked mechanically on every
weave and raises `WeaveError` if violated. Do not downgrade it to a warning. A memory
missing from the map does not exist as far as the agent is concerned, and the loss is
invisible — the cloth looks perfectly healthy.

## Things that must not grow here

**No number says how much a memory matters.** No `importance`, `salience`, `priority`
or `score` field; no `recurrence_count`; no retention decision that reads
`read_counts()`; no table of points per tag. Tags are words about a memory's
character, and the three sentences (`belongs_because` / `keep` / `may_fade`) are
curation judgements against the store's charter. The charter ranks; nothing else
does. `tests/test_rooms.py` greps the source for the forbidden names — keep it that
way rather than finding a synonym.

**A memory never changes store.** No move, no copy, no re-filing by tag, no router
that reads a message and picks a room. The store is chosen before the conversation by
the host, and a mode change affects only future sessions. If you find yourself
wanting to deduplicate across stores, stop: Research's "what we learned" and
Develop's "what we did" are two facts.

**A claiming tag needs its evidence.** `verify_tags()` in the gate is deterministic,
like the rest of the gate. `entrusted`, `emotion-carried`, `recurred`, `landmine`,
`formative` each name the evidence that would make them true; a model proposes, the
evidence decides, and both the basis and every refusal are in the manifest.
`recurred` is written once by the distiller against a prior memory from a different
journal — it is decided, never proposed, never counted.

**Forgetting is not designed yet, and this codebase must not design it by default.**
`doctor` observes capacity in four units with `limit` and `pressure` left `None`.
Anything that would pick a unit, set a limit, choose candidates, garage, settle,
absorb, release or delete is a conversation with the people whose memories they are
— `docs/DESIGN.md` §8 lists what is undecided. If a change needs one of those
answers to compile, it is the wrong change for now. The first forgetting pass will be
a dry run that modifies nothing.

## Layout

```
distill_kura/
  store.py       one kura: files, index, [[links]], doctor, write policy, containment.
                 No model calls at all.
  recall.py      recognition: whole index → picked slugs → walk links → context
  registry.py    stores + modes + model roles, loaded from kura.toml
  thinker.py     one OpenAI-compatible client; three roles (thinker/brain/scribe)
  server.py      the HTTP mouth, store-selectable on every route
  weave.py       the loom: the index compressed into the three-layer resident cloth
  prefill.py     the standing block a host injects. Byte-stable by contract
  tokens.py      per-script token estimation (fitted, not guessed)
  bench.py       measure instead of claim: store_ratio, map_ratio, retention
  mcp.py         MCP stdio bridge (stdlib only, single file, droppable anywhere)
  tend.py        the watcher: quiet = journal mtime; drain → distil → weave → trail → pay-forward → tidy; exit 2 rests
  cli.py         `kura`
  distill/
    sources.py   journal adapters + evidence classing. Add formats here.
    prompts.py   every prompt. Shared charter head = one cached prefix.
    gate.py      ← the deterministic floor. Treat changes here as load-bearing.
    watermark.py claim-before-drink, flock + max() merge
    seeds.py     ideas (no evidence required) and their graduation
    pipeline.py  the pass: sip → spot → gate → novelty → compose → stage → drain
dsh-plugin/      the DeepSeek Harness plugin (JavaScript)
examples/        a runnable config, two demo stores, DSH preset wiring, the five-room layout
bench/fixtures/  a synthetic corpus with facts planted on purpose, and their questions
scripts/         the clean-room demo and its scripted model
tests/           the pytest suite, no model needed
```

## House style

**Comments explain *why*, and name the failure that produced the rule.** A comment that
restates the code is noise; a comment that says "a byte offset into a recompressed
archive is a lie" saves the next person a day. Where a number or a behaviour came from
a measurement, say it was measured. Never write an estimate in the shape of a
measurement — that is the same sin the gate exists to prevent, committed in source.

**Fail loudly at load.** A bad config value throws with the offending value named. A
silently skipped plugin or a silently ignored field looks exactly like a working one,
and that is the worst failure mode there is.

**Degrade visibly.** When the thinker is unreachable, recall falls back to word overlap
*and labels the answer* `how=words`; the tools surface it as `⚠ degraded`. Never let
quality drop quietly.

**Tools: ASCII names, human descriptions.** A tool name is a function-calling key. The
description is the only thing the model knows about the tool, so it must say when to
call it *and what an empty result means* — otherwise an empty memory gets filled with
invention.

**Registration must be reversible.** In the DSH plugin, every `register()`/`guard()`
disposer goes on `ctx.effect()`. Unloading leaves no debris.

**Policy lives outside the tool.** Read-only is enforced by a monotonic
`ctx.tools.guard()`, not by an `if` inside the tool body — a guard's denial cannot be
overturned by another listener.

## Changing anything the agent reads every turn

The resident block is byte-stable by contract. A prefix cache dies from the first
changed byte onward (measured: identical 0.14 s, one word added at the front 0.66 s), so:

- Nothing volatile goes in the block. `prefill.build()` refuses a header containing a
  date, a clock or a session id, at build time.
- A warning about the map goes in the JSON, never in the text.
- The DSH prompt section's `text` provider is **synchronous** and runs on every model
  step. It returns a cached string; the fetch happens in the background. Returning a
  Promise renders `[object Promise]` into the prompt.
- If the block cannot be built, emit the honest note — never an empty string, and never
  a truncated map.

## Changing prompts

The charter text sits byte-identically at the head of every role's system prompt. On a
slow local model that is one cached prefix instead of three prefills — a real
measurement, not a theory. **Do not reword the charter per call site.** Add
task-specific text after the separator instead.

Reasoning-effort dialects (`reasoning_effort`, `thinking_effort`, `enable_thinking`) are
all sent at once on purpose: templates ignore unknown variables, and a model left on its
default deep-thinking setting can spend the whole token budget reasoning and return an
empty answer. Do not "clean this up" to one dialect.

## Changing the distiller

- Adding a journal format: subclass `Source` in `sources.py`, register it in `SOURCES`,
  and decide the watermark unit deliberately. Append-only file → byte offset. Anything
  that gets rewritten → a sequence number carried in the events. Then place it in
  `source_for()`'s tuple: that is first-match, `SOURCES` order is irrelevant, and
  `claude` answers for **any** `.jsonl` — so a new `*.something.jsonl` adapter listed
  after it is never reached.
- An adapter whose walk can **raise** must not give `claim_bound` a cheaper rule than
  the read. `claim()` writes the reserve before `sip()` runs and `advance()` only moves
  forward, so a bound returned without parsing puts the mark past the corruption the
  read is about to refuse — and the loud failure eats the healthy runs behind it.
- The watermark needs **both** halves: `flock` to serialise, and `max()` to merge. A
  lock alone still lets a stale snapshot win.
- Claim *before* reading, never after. Advance-after-read leaves a window where a
  parallel runner starts at the same offset.
- Anything that bounds coverage (top-N, sampling, a retry cap) must be logged. Silent
  truncation reads as "covered everything".

## Claims

Do not put a performance number in the README that no command produces. Two ratios exist
and are routinely confused: `store_ratio` (memories vs raw journal) and `map_ratio` (the
woven map vs the index). Both are properties of the CORPUS — measured with the shipped
fixtures they come out 0.18 and 1.14 on different material — so a headline figure is
misleading no matter which one it is.

Anything about how much was *kept* needs `bench retention`, not `bench compress`. A store
that keeps one memory in a hundred has a wonderful ratio and may be useless.

## Tests

```bash
python3 -m pytest tests -q
```

New behaviour needs a test that fails without it. For anything touching the gate, write
the test **adversarially**: not "does the happy path work" but "what is the shape of the
lie that would get through". The existing gate tests are each a real smuggling attempt.

Tests must not need a model. Use a stub endpoint (see `StubThinker`) or the scripted
HTTP server in `test_pipeline_e2e.py`.

## What belongs to the host, not to us

**Persona.** This project never renders or injects a persona. A store may record which
persona file belongs with it, exposed as a pointer at `GET /profile`; rendering and
injection are the harness's job (`dsh-persona`, `AGENTS.md`, or whatever the host uses).
Keep it that way — memory and identity switch together because the *host* binds them,
not because we grew a second identity system.

**Agent instructions.** Same: `AGENTS.md` is read by the host. We ship one for this
repo; we do not implement the mechanism.

**Authentication.** There is none. The server binds to loopback by default. If you need
auth, put something in front of it rather than growing a half-built auth layer here.
