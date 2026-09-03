"""Warm the thinker after the index moves.

Measured 2026-09-03 on a CPU-hybrid mouth (GLM-5.3-Flash, prefill ≈100 tok/s, prefix
cache in-process): the first thinker-path recall after ANY change to `index_text()` —
a pour, a weave, a tidy, a memory written by hand — cost **279 s**. Warm repeats of the
same question cost 10–18 s. The whole difference is the cold prefill of the PICK prompt,
and it is paid by the person who asks the next question.

`pay-forward` does not cover this. It bakes the *resident index block* (`prefill.py`)
into a mouth's slot; those bytes are not the PICK prompt's bytes, so the mouth's prefix
cache misses and the human still waits. So the warming is its own mechanism: send
**exactly what recall would send** (`recall.pick_prompt`, one construction, no second
copy) with a fixed probe question and `max_tokens=1`. The reply is never parsed — the
point is entirely the mouth's cache.

Two rules this module keeps:

- **Never raise, never break a pass.** A mouth that is down or slow makes the warm
  measurement `ok: false`; the watcher goes on to the next track. Warming is a comfort,
  never a gate.
- **Measurement, not claim.** Every attempt appends a row to `_still/warm.jsonl` with
  the seconds it took and the hash of the index it warmed, so "the mouth is warm" can
  be read off disk instead of believed.

Debounce is by index hash: `_still/warm.json` holds the hash last warmed, so a restart
does not re-pay a prefill that is already in the mouth. Hand-written changes to the
store are picked up at the watcher's next tick — the existing cadence, deliberately no
inotify: one signal (quiet) and one clock is what the watcher is for.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

from .recall import pick_prompt
from .store import Store
from .thinker import Endpoint

# A neutral probe. It is never answered (max_tokens=1); it exists only so the request
# has a `user` turn of stable bytes. Configurable because a store in another language
# should not carry a Japanese sentence it never reads.
DEFAULT_QUESTION = "この家で最近決めたことと、いま動いている仕事は？"
# Cold prefill of the whole index is the thing being paid here, and it was measured at
# 279 s. The thinker's own 120 s default would time out on exactly the case this module
# exists for, and record a failure that is really a success cut short.
DEFAULT_TIMEOUT = 900.0


def index_hash(store: Store) -> str:
    """The hash of the bytes recall would send — the prompt, not just the index, so a
    changed label or a changed PICK header counts as a cold mouth too."""
    return hashlib.sha256(pick_prompt(store).encode("utf-8")).hexdigest()[:16]


def signature(store: Store, thinker: Endpoint | None) -> str:
    """What the debounce actually compares (Rina, 2026-09-03): the cache lives in the
    MOUTH, not in the store. The same index sent to another url, another model, or
    with other template settings is a cold mouth, so the identity of the thinker is
    part of the key. A mouth that merely restarted cannot be seen from here — that is
    its service manager's job (`kura warm --force` from an ExecStartPost hook)."""
    ident = "" if thinker is None else json.dumps(
        {"url": getattr(thinker, "url", ""), "model": getattr(thinker, "model", ""),
         "dialect": getattr(thinker, "dialect", ""),
         "template": thinker.template_kwargs() if hasattr(thinker, "template_kwargs") else {},
         "extra": getattr(thinker, "extra", {})},
        sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256((index_hash(store) + "\n" + ident).encode("utf-8")).hexdigest()[:16]


def warm_thinker(store: Store, thinker: Endpoint | None, *,
                 question: str = DEFAULT_QUESTION,
                 timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Send the recall prompt once, ask for one token, and report what it cost.

    Returns `{"ok", "seconds", "index_hash", "chars", "error"}` and raises nothing.
    """
    prompt = pick_prompt(store)
    rec = {"ok": False, "seconds": 0.0,
           "index_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
           "chars": len(prompt), "error": None}
    if thinker is None:
        rec["error"] = "no thinker configured"
        return rec
    t0 = time.time()
    try:
        got = thinker.ask(prompt, question, max_tokens=1, timeout=timeout)
        rec["error"] = None if got is not None else (thinker.last_error or "no answer")
        rec["ok"] = got is not None
    except Exception as e:                       # a mouth is not worth a traceback
        rec["error"] = f"{type(e).__name__}: {e}"
    rec["seconds"] = round(time.time() - t0, 2)
    return rec


# ── remembering what is already warm ──────────────────────────────────────
def _state_path(store: Store) -> str:
    return os.path.join(store.still, "warm.json")


def read_state(store: Store) -> dict:
    try:
        with open(_state_path(store), encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(store: Store, rec: dict) -> None:
    try:
        os.makedirs(store.still, exist_ok=True)
        tmp = _state_path(store) + f".tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        os.replace(tmp, _state_path(store))
    except OSError:
        pass


def _ledger(store: Store, row: dict) -> None:
    try:
        os.makedirs(store.still, exist_ok=True)
        with open(os.path.join(store.still, "warm.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def run(reg, store: Store, force: bool = False) -> dict:
    """One warming pass for a store, debounced by index hash.

    `did` is one of `warmed` (the prompt was sent), `fresh` (this index was already
    warmed — nothing to do), `disabled`, or `failed`.
    """
    cfg = reg.warm_cfg_for(store)
    thinker = reg.models_for(store).thinker
    now = index_hash(store)
    sig = signature(store, thinker)
    if not cfg.get("enabled", True):
        return {"store": store.name, "did": "disabled", "ok": True, "seconds": 0.0,
                "index_hash": now, "signature": sig, "chars": 0, "error": None}
    if not force and read_state(store).get("signature") == sig:
        return {"store": store.name, "did": "fresh", "ok": True, "seconds": 0.0,
                "index_hash": now, "signature": sig, "chars": 0, "error": None}
    rec = warm_thinker(store, thinker,
                       question=str(cfg.get("question", DEFAULT_QUESTION)),
                       timeout=float(cfg.get("timeout", DEFAULT_TIMEOUT)))
    out = {"store": store.name, "did": "warmed" if rec["ok"] else "failed", "signature": sig, **rec}
    _ledger(store, {"at": int(time.time()), **out})
    if rec["ok"]:
        # Only a success is remembered: recording a failed hash would tell the next
        # run the mouth is warm when nothing was ever prefilled into it.
        _write_state(store, {"index_hash": rec["index_hash"], "signature": sig,
                             "at": int(time.time()), "seconds": rec["seconds"]})
    return out
