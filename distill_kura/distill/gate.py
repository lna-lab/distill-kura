"""The gate — the one place a model never touches.

A candidate arrives with quotes. We check, by plain substring match, that each quote
really exists in the raw material of the class it claims. A fabricated quote cannot
pass a substring check, no matter how confident the model is. Everything downstream
(the writer, the pourer) is a model again; this is the deterministic floor under them.

`gate()` — the candidate gate — does four things:

1. **Quote verification.** No surviving quote → the candidate is dropped.
2. **Echo suppression.** A quote that already exists verbatim in the store is not new
   material — it is the store reading itself back through a tool result. Without this,
   a store re-discovers and re-records its own contents forever.
3. **Class arithmetic.** Which classes survived decides what may be claimed:
   [TOOL]/[ACT] present → grounded; [USER] present → attributable to the human;
   [SELF] only → it is a judgement and must say so.
4. **The idea escape hatch, closed against smuggling.** Ideas need no quotes (a new
   thought is by definition not in the material) — but "the human approved X" is a
   factual report wearing an idea's coat, and is dropped.

The rest of the module is the same floor applied further downstream, and each part
carries its own banner: the final-surface floor (`composed_number_violations`,
`final_surface_violations`, `attributes_to_human`, `unknown_links`,
`invented_quotations`) re-checks every model-written surface token by token — for
numbers, for crediting the human, for links that name no memory and for quotation
marks around words nobody said; `verify_tags` and
`verify_callsigns` decide which tags and which routing words the evidence can carry;
`salvage` recovers whole objects from a truncated JSON answer.
"""
from __future__ import annotations

import re
import unicodedata

from ..store import InvalidTag, Store, normalize_tags
from .sources import Segment

MAX_QUOTE = 400
MIN_QUOTE = 12

# "the human decided/approved/said …" — a factual report, not an idea.
_FACT_IN_IDEA_CLOTHING = re.compile(
    r"\b(user|human|ken|owner|they)\b.{0,24}\b(proposed|approved|decided|asked|chose|said|"
    r"confirmed|requested|wants|noted|instructed)\b", re.I)

# A quote's claimed class, with the two legacy spellings the house used to write.
_QUOTE_TAG = re.compile(r"\[(USER|TOOL|ACT|SELF|KEN|ME)\]\s*(.*)", re.S)

# A [SELF]-only candidate has to SAY it is a judgement; these are the words that say so.
_JUDGEMENT_WORDS = re.compile(r"judge|judgement|judgment|opinion|read it|見立て|判断", re.I)

_ANY_DIGIT = re.compile(r"\d")

# Narrower than _FACT_IN_IDEA_CLOTHING on purpose: that one reads an idea-hatch blob
# (topic + why) and may cast wide, this one reads finished prose that is about to be
# stored, where a wider net would refuse honest sentences. Do not unify them.
_ATTRIBUTES_TO_HUMAN = [
    re.compile(r"ケン(は|が|の指示|の決裁|さんが|確定|裁定|方針|決定|号令|決裁|指示|裁決|承認)", re.I),
    re.compile(r"ケン\s*[:：]", re.I),
    re.compile(r"\b(the user|the human|the owner|ken)\b\s+\w*\s*"
               r"(decided|asked|approved|chose|instructed|said|ruled|confirmed)", re.I),
]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def gate(cands: list[dict], segs: list[Segment], store_text: str = "") -> tuple[list[dict], list[dict], list[dict]]:
    """→ (kept, dropped, ideas). `store_text` is the normalised full text of the store."""
    hay = {c: norm("\n".join(s.text for s in segs if s.cls == c))
           for c in ("USER", "TOOL", "ACT", "SELF")}
    kept: list[dict] = []
    dropped: list[dict] = []
    ideas: list[dict] = []

    for c in cands:
        if str(c.get("kind", "")).lower() == "idea":
            blob = f"{c.get('topic', '')} {c.get('why', '')}"
            if _FACT_IN_IDEA_CLOTHING.search(blob):
                dropped.append({**c, "why_dropped": "a factual report dressed as an idea"})
                continue
            ideas.append(c)
            continue

        good: list[dict] = []
        classes: set[str] = set()
        echoed = 0
        for q in (c.get("quotes") or [])[:6]:
            q = str(q)
            m = _QUOTE_TAG.match(q)
            claimed, body = (m.group(1), m.group(2)) if m else (None, q)
            claimed = {"KEN": "USER", "ME": "SELF"}.get(claimed, claimed)   # legacy tags
            body = norm(body)[:MAX_QUOTE]
            if len(body) < MIN_QUOTE:
                continue
            found = [k for k, v in hay.items() if body and body in v]
            if not found:
                continue                       # not in the material: fabricated or paraphrased
            if store_text and body in store_text:
                echoed += 1                    # the store reading itself back
                continue
            cls = claimed if claimed in found else found[0]
            good.append({"class": cls, "text": body})
            classes.add(cls)

        if not good:
            dropped.append({**c, "why_dropped": ("echo of text already in the store" if echoed
                                                 else "quotes not found in the raw material")})
            continue

        judgement = classes == {"SELF"}
        if judgement and not _JUDGEMENT_WORDS.search(
                f"{c.get('why', '')} {c.get('topic', '')}"):
            dropped.append({**c, "why_dropped": "turning the agent's own words into a fact"})
            continue

        claims_number = bool(_ANY_DIGIT.search(f"{c.get('why', '')} {c.get('topic', '')}"))
        grounded = bool(classes & {"TOOL", "ACT"})
        entry = {**c,
                 "evidence": good,
                 "classes": sorted(classes),
                 "judgement": judgement,
                 "unverified_numbers": claims_number and not grounded}
        # Callsigns are verified against the SURVIVING quotes, not the raw material:
        # a phrase that only ever appeared in a dropped quote was never evidence.
        cues, cue_refused = verify_callsigns(c.get("callsigns"), good)
        if cues or cue_refused:
            entry["routing_cues"] = cues
            entry["routing_cues_refused"] = cue_refused
        kept.append(entry)
    return kept, dropped, ideas


# ── the final surface, re-checked ────────────────────────────────────────
#
# The gate above verifies what the CANDIDATE brought. Models write text after
# that — the scribe composing, the judge fixing — and every one of them is a
# model: told "write no numbers", it can still write one with nothing behind it.
# The mark (HMAC) proves who staged a draft, not that its claims are grounded.
# So everything a model wrote that will be stored or indexed — title, trigger,
# section heading, body, the curation sentences — gets a deterministic floor,
# token by token: a numeric token in the final surface must equal a numeric
# token the evidence itself contains. Never by concatenating the evidence's
# digits ("899 ms … 2.3 ms" must not vouch for an invented "923"). A sign is
# meaning; a range is one claim; scientific notation is one token; Unicode
# lookalikes (−, –, —, full-width digits) are normalised before scanning, so a
# dash from a different alphabet is not a disguise. Single digits are verified
# too — "8 GPUs" and "4-bit" are exactly the claims a house full of local
# models invents — with one mechanical exception: ordered-list markers.

_SCI_OR_NUM = re.compile(r"[+-]?\d+(?:\.\d+)?[eE][+-]?\d+|[+-]?\d[\d,.:/-]*\d|[+-]?\d")
_LIST_MARKER = re.compile(r"(?m)^\s*\d+[.)]\s+")


def _num_normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).replace("\u2212", "-")   # true minus
    return re.sub(r"(?<=\d)\s?[\u2013\u2014]\s?(?=\d)", "-", s)  # digit–digit dashes


def _canon_num(t: str) -> str:
    # A comma is forgiven only as a THOUSANDS separator (\d{1,3}(,\d{3})+): erasing
    # every comma made "1,5" canonicalise to "15", so a claimed 1,5 passed on
    # evidence that said 15 — two different numbers, one vouching for the other.
    # A comma that is not a thousands separator stays, and the token fails closed.
    # "E" folds to "e": 1.23E-4 and 1.23e-4 are one magnitude, not two claims.
    return re.sub(r"(?<=\d),(?=\d{3}(\D|$))", "", t.rstrip(",.:/-").replace("E", "e"))


def _num_tokens(s: str) -> list[str]:
    return [_canon_num(m.group(0)) for m in _SCI_OR_NUM.finditer(s)]


def composed_number_violations(text: str, evidence: list[dict], allowed: str = "") -> list[str]:
    """→ numeric tokens the final text claims that its evidence never contained.

    Canonicalisation forgives formatting (commas, trailing punctuation, Unicode
    spellings) and nothing else. An unsigned token in the text may cite a signed
    one in the evidence (the magnitude is the evidence's); a signed token must
    match sign and all. `allowed` is extra text whose numbers are legitimate —
    the caller decides what that is; today the only such text is an extension
    heading's mechanically stamped date, which reaches here through
    final_surface_violations (pipeline._compose_extension). Every direct caller
    passes nothing.
    """
    hay = _num_normalize(norm(" ".join(str(e.get("text", "")) for e in evidence)) + " " + allowed)
    exact: set[str] = set()
    unsigned: set[str] = set()
    for c in _num_tokens(hay):
        exact.add(c)
        unsigned.add(c.lstrip("+-"))
    bad: list[str] = []
    for t in _num_tokens(_LIST_MARKER.sub("", _num_normalize(text))):
        ok = (t in exact) if t[:1] in "+-" else (t in unsigned)
        if not ok and t not in bad:
            bad.append(t)
    return bad


# ── two more facts about a surface: its links and its quotation marks ─────
#
# Both are topology, not taste. A `[[slug]]` either names a memory this store
# holds or it does not — a link to nothing is a dead end the next reader pays
# for, and no amount of good writing makes it live. A direct quotation either
# stands in the surviving evidence or it does not: 「はい、反復バグは解決しました」
# reads as something that was said, and a model that writes a reply nobody made
# has forged a record. Whether the advice around them is any good is the
# writer's competence and stays with the writer.

MIN_QUOTED = 8          # shorter runs inside quote marks are emphasis, not testimony
_QUOTED = re.compile(r"「([^」\n]{1,400})」|『([^』\n]{1,400})』|"
                     r"“([^”\n]{1,400})”|\"([^\"\n]{1,400})\"")


def _qnorm(s: str) -> str:
    """The one normalisation both quote checks use: NFKC (so ＡＢＣ and full-width
    punctuation do not disguise a quote) plus collapsed whitespace (so a line break
    the scribe inserted is not a difference in what was said)."""
    return norm(unicodedata.normalize("NFKC", s))


def _reads_as_speech(body: str) -> bool:
    """A quoted run is testimony when it has a space or a CJK character in it — the
    shape of a sentence. Without this the floor read every JSON string in a TAGS line
    as a quotation nobody uttered."""
    return " " in body or any("\u3040" <= ch <= "\u9fff" for ch in body)


def unknown_links(surface: str, known: frozenset[str] | set[str] | None) -> list[str]:
    """→ the `[[slug]]` names in `surface` that this store does not hold.

    `known` is the caller's slug set (the store's, plus whatever is being written in
    the same breath); None means the caller cannot say, and then nothing is checked —
    a floor that guesses at the topology would refuse honest links.
    """
    if known is None:
        return []
    bad: list[str] = []
    for name in Store.links_of(surface):
        if name not in known and name not in bad:
            bad.append(name)
    return bad


def invented_quotations(surface: str, evidence: list[dict]) -> list[str]:
    """→ quoted runs the surface presents as spoken that the evidence never contains.

    Only runs of at least MIN_QUOTED characters inside 「」/『』/“”/"" count, and only ones
    that read as speech — containing a space or a CJK character. A bare ASCII token in
    double quotes is a name, a tag or a JSON string ("emotion-carried" in a TAGS line),
    never testimony; a sentence in quotation marks IS a claim that someone said it, and
    that claim is checked the way every other claim here is — verbatim against the
    surviving evidence.
    """
    hay = _qnorm(" ".join(str(e.get("text", "")) for e in evidence))
    bad: list[str] = []
    for m in _QUOTED.finditer(unicodedata.normalize("NFKC", surface)):
        body = _qnorm(next(g for g in m.groups() if g is not None))
        if len(body) < MIN_QUOTED or not _reads_as_speech(body) or body in hay or body in bad:
            continue
        bad.append(body)
    return bad


def final_surface_violations(surface: str, evidence: list[dict], classes: list[str],
                             allowed: str = "", known_slugs=None) -> list[str]:
    """The one door every model-written surface must pass before it earns a mark.

    `surface` is everything that will be stored or indexed: title, description,
    section heading, body, curation sentences — concatenation is fine, this is a
    floor, not a parser. `allowed` is text whose numbers CODE put there (an
    extension heading's mechanically stamped date). Returns human-readable
    violations; empty means pass.
    """
    v = [f"invented number: {t}" for t in composed_number_violations(surface, evidence, allowed)]
    if attributes_to_human(surface, classes):
        v.append("credits the human with no [USER] quote")
    dead = unknown_links(surface, known_slugs)
    if dead:
        v.append("unknown links: " + ", ".join(dead))
    for q in invented_quotations(surface, evidence):
        v.append(f"invented quotation: {q[:24]}…")
    return v


# ── tags: a model proposes, the evidence decides ─────────────────────────
#
# A tag is a word, but some words are claims. `entrusted` says the human asked for this
# to be kept; `emotion-carried` says they reacted; `recurred` says they brought it up
# again. A model that could attach those freely could immortalise whatever it liked —
# so each claiming tag needs the class of evidence that would make it true, checked
# here and recorded in the manifest. The seven words reserved for a future forgetting
# pass are refused outright: no model assigns them until that pass is designed.
#
# Everything else — `decision`, `landmine`'s sibling `hypothesis`, a store's own words —
# is curation, not a claim about the human, and passes as proposed.
_ENTRUST = re.compile(
    r"(remember (this|that|it)|don'?t forget|do not forget|keep (this|that|it) in mind|"
    r"write (this|that|it) down|覚えて|忘れないで|記憶して|メモして|覚えておいて|覚えといて)", re.I)
FORGETTING_TAGS = frozenset({"superseded", "absorbed", "fulfilled", "expired",
                             "corrected", "released", "incidental"})
# A landmine is "the failure that will recur". It rests on an ACTUAL failure in tool
# output, or on the human warning or correcting — not on a quiet `df` line, which is
# tool output and nothing else. Measured on the house: without this, every bake log
# line could carry the tag, and a tag that every memory can carry protects none.
_FAILURE = re.compile(
    r"(\berror\b|errno|exception|traceback|\bfail(ed|ure|s)?\b|fatal|panic|\boom\b|out of memory|"
    r"timed? ?out|segfault|crash|killed|refused|denied|corrupt|broken|dead\b|"
    r"落ち|失敗|死ん|壊れ|止まっ|エラー|例外|タイムアウト|固まっ|動かな)", re.I)
_WARNING = re.compile(
    r"(⚠|注意|警告|やめ|ダメ|駄目|違う|間違|禁止|危な|二度と|しないで|するな|"
    r"don'?t\b|never\b|wrong|careful|warning|danger|\bstop\b|must not|mistake|not like that)", re.I)


def verify_tags(proposed, evidence: list[dict], recurred_ok: bool = False
                ) -> tuple[tuple[str, ...], dict[str, dict], dict[str, str]]:
    """→ (kept, basis, refused).

    `basis[tag]` names the evidence a kept claiming tag rests on, so the manifest can
    say why the tag exists. `refused[tag]` says why one did not make it — written to
    the manifest too, because a silently dropped tag looks like one never proposed.
    `recurred` is decided by the caller (it needs a prior memory and a different
    occasion, which a single candidate cannot see) and passes only with `recurred_ok`.
    """
    try:
        tags = normalize_tags(proposed)
    except InvalidTag as e:
        return (), {}, {"*": str(e)}
    classes = {e["class"] for e in evidence}
    user_quotes = [e["text"] for e in evidence if e["class"] == "USER"]
    kept: list[str] = []
    basis: dict[str, dict] = {}
    refused: dict[str, str] = {}

    def keep(t: str, cls: str, quote: str | None = None) -> None:
        """A kept tag and the evidence it rests on, recorded together — the two must
        never drift apart, or the manifest says a tag exists for a reason nobody can
        read. `formative` rests on a CLASS, not a quote, and passes none."""
        kept.append(t)
        basis[t] = {"class": cls} if quote is None else {"class": cls, "quote": quote}

    for t in tags:
        if t in FORGETTING_TAGS:
            refused[t] = "reserved for the forgetting pass; a model may not assign it"
        elif t in ("emotion-carried", "commitment"):
            # Both are claims about the human — what they felt, what they undertook
            # — and neither can rest on tool output or the agent's prose.
            if user_quotes:
                keep(t, "USER", user_quotes[0])
            else:
                refused[t] = "needs the human's own words; no [USER] quote survived"
        elif t == "entrusted":
            q = next((q for q in user_quotes if _ENTRUST.search(q)), None)
            if q:
                keep(t, "USER", q)
            else:
                refused[t] = "needs a [USER] quote that asks for this to be kept"
        elif t == "recurred":
            if recurred_ok:
                keep(t, "USER", user_quotes[0] if user_quotes else "")
            else:
                refused[t] = "recurrence is decided against a prior memory, not proposed"
        elif t == "landmine":
            fail = next((e for e in evidence if e["class"] in ("TOOL", "ACT")
                         and _FAILURE.search(e["text"])), None)
            warn = next((e for e in evidence if e["class"] == "USER" and _WARNING.search(e["text"])), None)
            # The human's warning outranks the machine's error as the basis: it is
            # the class that can later protect the memory absolutely.
            hit = warn or fail
            if hit:
                keep(t, hit["class"], hit["text"])
            else:
                refused[t] = ("needs an actual failure in [TOOL] output or a warning/correction "
                              "in the human's words; a quiet tool line is neither")
        elif t == "formative":
            if classes - {"SELF"}:
                keep(t, sorted(classes - {"SELF"})[0])
            else:
                refused[t] = "needs more than the agent's own prose"
        else:
            kept.append(t)
    return tuple(kept), basis, refused


# ── USER callsigns: the shared vernacular that routes to a memory ─────────
#
# A callsign is not content — it is the two-of-us word ("全員野球") that leads
# BACK to a memory. As a routing word it is worth exactly its provenance, so
# the floor is the same one quotes stand on: the phrase must exist verbatim
# inside a SURVIVING [USER] quote. A nickname the agent coined, a string a tool
# printed, a paraphrase of what the human "meant" — none of it is shared
# vocabulary, and none of it passes.

CUE_MIN, CUE_MAX, CUE_MAX_N = 3, 40, 2


def cue_key(text: str) -> str:
    """The routing-comparison key: NFKC + casefold + collapsed whitespace. The
    DISPLAY keeps the human's spelling; only comparison normalises, so '全員野球'
    and 'ＦＵＬＬ野球' never silently merge but 'FreeToken' and 'freetoken' do."""
    import unicodedata as _u
    return " ".join(_u.normalize("NFKC", text).casefold().split())


def verify_callsigns(proposed, evidence: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """→ (kept, refused). kept items carry {text, class: USER, quote} — the quote
    is the surviving [USER] evidence the phrase was found in, so the manifest can
    always say whose words the callsign is.

    Deterministic, like the rest of this file: the model proposes, the human's
    surviving words decide. The GATED VALUE is the whitespace-collapsed form —
    what is returned is exactly what a JSON round-trip gives back."""
    user_quotes = [e["text"] for e in evidence if e["class"] == "USER"]
    kept: list[dict] = []
    refused: dict[str, str] = {}
    seen_keys: set[str] = set()
    for raw in (proposed or []):
        text = " ".join(str(raw).split())            # round-trip shape, see test 7
        key = cue_key(text)
        why = None
        if not text or not any(ch.isalnum() for ch in key):
            why = "whitespace and punctuation alone do not route anywhere"
        elif not (CUE_MIN <= len(key) <= CUE_MAX):
            why = f"a callsign is {CUE_MIN}–{CUE_MAX} codepoints after normalisation"
        elif key in seen_keys:
            continue                                  # the same word, proposed twice
        else:
            hit = next((q for q in user_quotes if text in q), None)
            if hit is None:
                # Not inside a surviving [USER] quote: invented, paraphrased, or a
                # phrase only the agent or a tool used. All three are the same
                # refusal — the human never said it.
                why = "needs the human's own words: an exact substring of a surviving [USER] quote"
        if why is not None:
            refused[text or repr(raw)] = why
            continue
        if len(kept) >= CUE_MAX_N:
            refused[text] = f"at most two callsigns per memory (kept {[k['text'] for k in kept]})"
            continue
        seen_keys.add(key)
        kept.append({"text": text, "class": "USER", "quote": hit})
    return kept, refused


def salvage(raw: str) -> list[dict]:
    """Recover complete objects from a truncated JSON array.

    Readers copy quotes generously and run out of budget mid-array. The array will not
    parse, but every closed `{...}` inside it is still valid — take those.
    """
    out: list[dict] = []
    depth, start, instr, esc = 0, None, False, False
    for i, ch in enumerate(raw or ""):
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    o = __import__("json").loads(raw[start:i + 1])
                    if isinstance(o, dict) and (o.get("quotes") or
                                                str(o.get("kind", "")).lower() == "idea"):
                        out.append(o)
                except ValueError:
                    pass
                start = None
    return out


def attributes_to_human(text: str, classes: list[str], words: list[str] | None = None) -> bool:
    """True when prose credits the human with a decision but no [USER] quote survived.
    Prompt instructions get broken; this is checked mechanically instead."""
    if "USER" in classes:
        return False
    # The house writes "ケン確定 / ケン裁定 / ケン方針 / ケン決定 / ケン: ..." at least as
    # often as "ケンが決めた"; a floor that knew only the verb forms let a cue rewrite
    # who decided while every 2-gram stayed in place. The default vocabulary lives in
    # _ATTRIBUTES_TO_HUMAN; `words` lets a caller floor a text against its own list.
    if words is not None:
        return any(re.search(p, text, re.I) for p in words)
    return any(p.search(text) for p in _ATTRIBUTES_TO_HUMAN)
