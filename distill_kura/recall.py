"""Recall = recognition, not search.

The whole index (one line per memory, ~50 tokens each) is handed to the thinker,
which names the memories that *bear on* the question by meaning. Then we walk
[[links]] from the picked memories and return the neighbourhood as context.

Why not grep / embeddings: a question about "SSD inference chips" shares no word
with a memory titled "run the 2.6T model from an SSD tier", yet they are the same
problem. A resident index + a small model recognises that in ~0.4 s. If the
thinker is unreachable we fall back to word overlap and *say so* in `how`.

Before any of that runs tier zero: `fastpath`, a deterministic recognizer that
answers a DIRECT question (one that names a memory) in well under a millisecond
and stays silent on everything else. A gated hit skips the thinker entirely;
silence changes nothing about the path above.
"""
from __future__ import annotations

import json
import re
import time

from . import fastpath
from .store import LINK_TARGET, Store
from .thinker import Endpoint
from .tokens import estimate

PICK_SYS = (
    "You are the long-term memory of {label}. Below is the INDEX of everything remembered — "
    "one line per memory, written as a recognition trigger, not a title.\n\n"
    "Given the question, name the memories that genuinely bear on it. Judge by MEANING, not by "
    "shared words: a question about an outside topic is relevant when this store is working on "
    "the same problem, even if no word matches. Prefer few and right over many.\n"
    "Output ONLY a JSON array of file slugs (no .md, no path). Empty array if nothing truly "
    "relates.\n\n=== INDEX ===\n"
)


# The words a question is scored by. Runs of one script only: a merged character
# class glues `SSD推論` into one term that can then match nothing but that same
# adjacency, and the memory it was reaching for is never scored at all.
_TERMS = re.compile(r"[A-Za-z0-9]{2,}|[ァ-ヴー]{2,}|[一-龠]{2,}")


def pick_prompt(store: Store) -> str:
    """The exact system prompt the thinker is asked to pick with.

    Factored out for `warm.py`: warming the mouth's prefix cache only works when the
    bytes it prefills are the bytes recall will send. Two copies of this concatenation
    would drift the first time either side was touched, and the drift is invisible —
    the warm pass still "succeeds", recall still pays the cold prefill.
    """
    return PICK_SYS.format(label=store.label) + store.index_text()


def pick_by_meaning(store: Store, thinker: Endpoint, question: str, top: int) -> list[str] | None:
    raw = thinker.ask(pick_prompt(store), question, max_tokens=500)
    if raw is None:
        return None
    m = re.search(r"\[.*?\]", raw, re.S)
    if m:
        try:
            got = json.loads(m.group(0))
            # Models answer with `slug`, `slug.md`, `[slug]`, `path/slug.md` — all
            # the same intent. `Store._clean` is where that shape-tidying lives; a
            # second copy here drifted from it the moment either side was touched.
            picked = [store._clean(x) for x in got if isinstance(x, str)][:top]
            if picked:
                return picked
        except ValueError:
            pass
    # Deterministic net: whatever the format, a real slug in the answer is a pick.
    hits: list[str] = []
    for slug in store.known_slugs():
        if slug in raw and slug not in hits:
            hits.append(slug)
    return hits[:top]


def pick_by_words(store: Store, question: str, top: int) -> list[str]:
    """Last resort when the thinker is unreachable: rank index lines by word overlap.

    Read through `_uncommented`, never the raw index. The header comment carries the
    format hint, and its EXAMPLE link (`- [Title](its-slug.md)`) matched a question
    about titles or triggers — so the degraded path handed back `its-slug`, a memory
    that does not exist, and crowded a real one out of `top`.

    Link TARGETS only (`LINK_TARGET`, with its `](` anchor), never a bare
    `(x.md)`: a prose parenthetical like `read the rules(AGENTS.md) first` is
    not a link, and matching it made the line score for a memory that does not
    exist while the real memory the same line names was never scored at all.
    `known_slugs()` and `doctor` were fixed for exactly this; the degraded tier
    had been left behind."""
    terms = _TERMS.findall(question)
    scored = []
    for line in store._uncommented(store.index_text()).splitlines():
        m = LINK_TARGET.search(line)          # the FIRST real link on the line
        if not m:
            continue
        s = sum(line.lower().count(t.lower()) for t in terms)
        if s:
            scored.append((s, m.group(1)))
    scored.sort(reverse=True)
    return [s for _, s in scored[:top]]


def fit(text: str, question: str, budget: int) -> str:
    """Trim a memory to `budget` chars WITHOUT cutting from the top: keep the
    frontmatter block whole (capped at 600 chars), then prefer paragraphs that mention
    the question's words. (Long memories keep their conclusions at the bottom;
    head-truncation loses them.)

    The opening paragraph is NOT pinned: the store's template always leaves a blank
    line after the closing `---`, so the head ends there and the opening competes for
    budget like every other paragraph. Pinning it would change which paragraphs
    survive, so it would be a behaviour change with its own test, not a docstring."""
    if len(text) <= budget:
        return text
    head_end = text.find("\n\n", text.find("---", 4) + 3)
    head = text[:max(0, head_end)][:600] if head_end > 0 else text[:600]
    terms = _TERMS.findall(question)
    rest = text[len(head):]
    paras = [x for x in rest.split("\n\n") if x.strip()]
    if any(len(x) > budget // 3 for x in paras):      # giant paragraph → split by line
        split: list[str] = []
        for x in paras:
            split += [ln for ln in x.split("\n") if ln.strip()] if len(x) > budget // 3 else [x]
        paras = split
    scored = sorted(((sum(p.lower().count(t.lower()) for t in terms), i, p)
                     for i, p in enumerate(paras)), key=lambda x: (-x[0], x[1]))
    keep, used = [], len(head)
    for _, i, para in scored:
        if used + len(para) + 2 > budget:
            continue
        keep.append((i, para)); used += len(para) + 2
    keep.sort()
    return head + "\n\n" + "\n".join(p for _, p in keep) + ("\n\n…(trimmed)" if used < len(text) else "")


def recall(store: Store, thinker: Endpoint | None, question: str, hops: int = 1,
           top: int = 3, chars: int = 6000, total_chars: int | None = None,
           fastpath_cfg: dict | None = None) -> dict:
    """Recall by recognition.

    `chars` is the budget for ONE memory, not for the answer: link-walking can return
    ten of them, so a caller reading it as a ceiling on the response was out by an order
    of magnitude. `total_chars` is the ceiling on the whole context; memories are filled
    in walk order until it runs out, and the reply says how much of each budget was used.

    `fastpath_cfg` is the `[fastpath]` table (`enabled`, `gate`); None means the
    defaults. The reply always says what tier zero did (`fastpath_verdict`, `fastpath_ms`).
    """
    t0 = time.time()
    cfg = fastpath_cfg or {}
    fp_ms, fp_verdict, picked, how = None, "disabled", None, ""
    fp: dict = {}
    if cfg.get("enabled", True):
        # Tier zero: a gated hit IS the pick and the thinker is never asked — which
        # also means a direct question still finds its memory when the thinker is
        # down, instead of degrading straight to word overlap. The callsign pre-head
        # answers first when the question contains a verified shared word.
        fp = fastpath.lookup(store, question, top=top,
                             gate=cfg.get("gate", fastpath.DEFAULT_GATE),
                             cues=cfg.get("cues", True))
        fp_ms, fp_verdict = fp["ms"], fp["verdict"]
        if fp["hits"]:
            picked, how = [h["slug"] for h in fp["hits"]], "fastpath"
            if fp.get("cue"):
                how = "fastpath-cue"
    if picked is None:
        picked = pick_by_meaning(store, thinker, question, top) if thinker else None
        if picked is None:
            picked, how = pick_by_words(store, question, top), "words(thinker unreachable)"
        elif not picked:
            # The thinker read the whole index and named nothing. That is an answer —
            # overriding it with word overlap would hand back look-alikes for a
            # question this store knows nothing about. Refusal is a feature.
            how = "meaning→none"
        else:
            how = "meaning"
    order = store.walk(picked, hops)
    parts, used, included = [], 0, []
    for s in order:
        room = chars if total_chars is None else min(chars, total_chars - used)
        if room <= 0:
            break                       # the budget is spent; say so rather than trim to nothing
        piece = f"=== {store.label}: {s} ===\n{fit(store.read(s), question, room)}"
        # A ceiling that is exceeded by a little is not a ceiling. `fit` keeps a memory's
        # opening whole (cutting from the top loses the frontmatter), so a piece can come
        # back larger than the room it was given — in which case it does not go in at all.
        if total_chars is not None and used + len(piece) + 2 > total_chars:
            break
        parts.append(piece)
        included.append(s)
        used += len(piece) + 2
    if order and not included and total_chars:
        # A budget too small for even one memory must not answer with silence. Give back
        # the best-ranked one, cut to fit and labelled as cut: a partial memory is an
        # answer, an empty context reads as "nothing is remembered".
        head = fit(store.read(order[0]), question, max(120, total_chars - 80))
        # The ceiling is on the WHOLE context, header included — the main loop above
        # accounts for it piece by piece, and this branch used to guess a fixed -60
        # margin that a long label + slug walked straight past.
        header = f"=== {store.label}: {order[0]} (truncated) ===\n"
        parts = [header + head[:max(0, total_chars - len(header))]]
        included = [order[0]]
    ctx = "\n\n".join(parts)
    store.note_read(included, "recall")
    return {"store": store.name, "question": question, "how": how, "picked": picked,
            "fastpath_verdict": fp_verdict, "fastpath_ms": fp_ms,
            "fastpath_cue": fp.get("cue"),
            "walked": order, "included": included,
            "dropped_for_budget": [s for s in order if s not in included],
            "context": ctx, "chars": len(ctx), "chars_per_memory": chars,
            "total_chars": total_chars, "tokens_est": estimate(ctx),
            "elapsed_s": round(time.time() - t0, 2)}
