"""Warming the thinker after the index moves.

The one thing that must never drift: the bytes `kura warm` sends are the bytes recall
sends. If they differ by a character the mouth's prefix cache misses and the warming is
a lie that still reports success — so the byte-for-byte test is the point of this file.
Around it: the hash debounce, a mouth that is down not stopping anything, and the config
switch really switching it off.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from distill_kura import warm as warmmod                # noqa: E402
from distill_kura.recall import pick_prompt, recall     # noqa: E402
from distill_kura.registry import Registry              # noqa: E402
from distill_kura.store import Store                    # noqa: E402


class Capturing:
    """A thinker that records what it was asked and answers `answer` (None = down)."""
    def __init__(self, answer="[]", raises=None):
        self.answer, self.raises = answer, raises
        self.calls: list[dict] = []
        self.last_error = "unreachable: TimeoutError: timed out"

    def ask(self, system, user, max_tokens=400, timeout=None, temperature=None):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens,
                           "timeout": timeout})
        if self.raises:
            raise self.raises
        return self.answer


def a_store(tmp_path, name="m") -> Store:
    s = Store(name=name, path=str(tmp_path / name), label=name)
    s.init_files()
    s.remember("ssd-tier-mission", "running a huge model off an SSD tier", "body")
    s.remember("cooling", "the fans went in before the CPU run", "body")
    return s


def a_registry(tmp_path, store: Store, thinker, **warm) -> Registry:
    extra = "".join(f"{k} = {json.dumps(v)}\n" for k, v in warm.items())
    cfg = tmp_path / "kura.toml"
    cfg.write_text(f"""
[stores.{store.name}]
path = "{store.path}"
[warm]
{extra}
""", encoding="utf-8")
    reg = Registry.load(str(cfg))
    models = reg.models_for(store)
    models.thinker = thinker
    reg.models_for = lambda st, _m=models: _m          # type: ignore[assignment]
    return reg


def test_the_warm_prompt_is_byte_for_byte_the_prompt_recall_sends(tmp_path):
    st = a_store(tmp_path)
    warming, recalling = Capturing(), Capturing(answer="[]")
    warmmod.warm_thinker(st, warming, question="probe")
    recall(st, recalling, "a question nothing answers",
           fastpath_cfg={"enabled": False})
    assert warming.calls[0]["system"] == recalling.calls[0]["system"]
    assert warming.calls[0]["system"] == pick_prompt(st)
    assert warming.calls[0]["max_tokens"] == 1          # the reply is never the point
    assert warming.calls[0]["user"] == "probe"


def test_a_mouth_that_raises_is_a_measurement_not_an_exception(tmp_path):
    st = a_store(tmp_path)
    r = warmmod.warm_thinker(st, Capturing(raises=TimeoutError("timed out")))
    assert r["ok"] is False and "TimeoutError" in r["error"]
    assert r["chars"] == len(pick_prompt(st)) and r["seconds"] >= 0


def test_a_mouth_that_answers_nothing_is_not_warm(tmp_path):
    st = a_store(tmp_path)
    r = warmmod.warm_thinker(st, Capturing(answer=None))
    assert r["ok"] is False and "unreachable" in r["error"]


def test_the_same_index_is_not_warmed_twice_but_a_changed_one_is(tmp_path):
    st = a_store(tmp_path)
    th = Capturing()
    reg = a_registry(tmp_path, st, th, enabled=True)
    first = warmmod.run(reg, st)
    assert first["did"] == "warmed" and len(th.calls) == 1
    assert warmmod.run(reg, st)["did"] == "fresh" and len(th.calls) == 1
    assert warmmod.run(reg, st, force=True)["did"] == "warmed" and len(th.calls) == 2
    st.remember("a-new-memory", "the index has moved", "body")
    again = warmmod.run(reg, st)
    assert again["did"] == "warmed" and len(th.calls) == 3
    assert again["index_hash"] != first["index_hash"]


def test_a_failed_warming_is_not_remembered_as_warm(tmp_path):
    """Recording the hash on failure would tell the next run the mouth is warm when
    nothing was ever prefilled into it."""
    st = a_store(tmp_path)
    th = Capturing(answer=None)
    reg = a_registry(tmp_path, st, th)
    assert warmmod.run(reg, st)["did"] == "failed"
    assert warmmod.read_state(st) == {}
    assert warmmod.run(reg, st)["did"] == "failed" and len(th.calls) == 2


def test_switched_off_means_the_mouth_is_never_called(tmp_path):
    st = a_store(tmp_path)
    th = Capturing()
    reg = a_registry(tmp_path, st, th, enabled=False)
    assert warmmod.run(reg, st)["did"] == "disabled"
    assert th.calls == []


def test_every_attempt_leaves_a_measured_row_on_disk(tmp_path):
    st = a_store(tmp_path)
    reg = a_registry(tmp_path, st, Capturing())
    warmmod.run(reg, st)
    warmmod.run(reg, st, force=True)
    rows = [json.loads(x) for x in
            open(os.path.join(st.still, "warm.jsonl"), encoding="utf-8").read().splitlines()]
    assert len(rows) == 2
    assert all(r["did"] == "warmed" and "seconds" in r and r["index_hash"] for r in rows)


def test_the_question_is_configurable_per_store(tmp_path):
    st = a_store(tmp_path)
    th = Capturing()
    cfg = tmp_path / "kura.toml"
    cfg.write_text(f"""
[stores.m]
path = "{st.path}"
[warm]
question = "global"
[stores.m.warm]
question = "this store's own"
""", encoding="utf-8")
    reg = Registry.load(str(cfg))
    st = reg.store("m")                       # the store the registry built, extras and all
    models = reg.models_for(st)
    models.thinker = th
    reg.models_for = lambda s, _m=models: _m            # type: ignore[assignment]
    warmmod.run(reg, st)
    assert th.calls[0]["user"] == "this store's own"


def test_an_unknown_warm_key_is_refused_at_load(tmp_path):
    """`enable = false` reads as a switched-off warmer and is not one. The same
    refusal [prefill] and [fastpath] give."""
    cfg = tmp_path / "kura.toml"
    cfg.write_text(f"""
[stores.m]
path = "{tmp_path / 'm'}"
[warm]
enable = false
""", encoding="utf-8")
    try:
        Registry.load(str(cfg))
    except ValueError as e:
        assert "warm" in str(e) and "enable" in str(e)
    else:
        raise AssertionError("an unknown [warm] key must be refused at load")


def test_a_wrongly_typed_warm_key_is_refused_too(tmp_path):
    cfg = tmp_path / "kura.toml"
    cfg.write_text(f"""
[stores.m]
path = "{tmp_path / 'm'}"
[stores.m.warm]
enabled = "true"
""", encoding="utf-8")
    try:
        Registry.load(str(cfg))
    except ValueError as e:
        assert "warm" in str(e)
    else:
        raise AssertionError("a string where a bool belongs must be refused at load")


def test_the_cli_prints_the_measurement_and_says_fresh_with_exit_two(tmp_path):
    st = a_store(tmp_path)
    cfg = tmp_path / "kura.toml"
    cfg.write_text(f"""
[stores.m]
path = "{st.path}"
[models.thinker]
url = "http://127.0.0.1:9/v1"
model = "none"
""", encoding="utf-8")
    argv = [sys.executable, "-m", "distill_kura.cli", "-c", str(cfg), "-s", "m", "warm"]
    p = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=120)
    r = json.loads(p.stdout.strip().splitlines()[-1])
    assert p.returncode == 1 and r["did"] == "failed" and r["ok"] is False   # nothing listens on :9
    # Now pretend it worked, and check the fresh path is exit 2 rather than silence.
    warmmod._write_state(st, {"index_hash": warmmod.index_hash(st), "at": 0, "seconds": 0.0})
    p = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert p.returncode == 2 and json.loads(p.stdout.strip().splitlines()[-1])["did"] == "fresh"
