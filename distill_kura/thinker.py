"""Model endpoints. One OpenAI-compatible client, three *roles*:

    thinker  — picks index lines by meaning at recall time (small, fast, always-on)
    brain    — the distiller's reader: finds what is worth keeping in raw journal
    scribe   — the distiller's writer: turns evidence into a memory in the store's language

Default is ONE model wearing all three hats (`[models.thinker]` only).
Upgrade path: give `brain` and/or `scribe` their own endpoint — a bigger local
model, or an online API (any OpenAI-compatible `/chat/completions`; the key is
read from the environment variable named in `api_key_env`, never from the file).

Reasoning-effort dialects differ between model families (`reasoning_effort`,
`thinking_effort`, `enable_thinking`). A local inference server passes them through its
chat template, which ignores what it does not know — that is why sending all of them at
once is safe there, and it matters because a model left at its default "deep thinking"
can burn the whole token budget on reasoning and return an empty answer.

A STRICT OpenAI-compatible service is a different animal: an unknown top-level field is
a 400, not something to ignore. So the body shape is chosen by `dialect`:

    vllm     (default) send chat_template_kwargs — local servers, vLLM, SGLang, llama.cpp
    openai   omit it; send only fields the OpenAI schema defines
    generic  the minimum: model, messages, temperature, max_tokens

Anything that answers `POST <url>/chat/completions` in the OpenAI shape works. That is
NOT the same as "any provider": a vendor's native API (Anthropic's, for instance) needs
an OpenAI-compatible gateway in front of it, and its own URL will not do.

On a 400 the client retries ONCE with the `generic` body, because "the server rejected a
field" and "the server is down" are different problems and only one of them is worth
giving up on. Failures are recorded on the endpoint (`last_error`) rather than collapsing
into a bare `None`: an operator needs to tell a wrong key from a wrong URL from a wrong
model name.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field


def bearer_headers(api_key_env: str | None) -> dict:
    """`{"Authorization": "Bearer <key>"}` when the named variable is set and non-empty,
    else `{}`. Empty counts as unset — the rule all three callers (ask, alive and
    payforward._post) already applied separately, in three copies of the same block."""
    key = os.environ.get(api_key_env, "") if api_key_env else ""
    return {"Authorization": f"Bearer {key}"} if key else {}


@dataclass
class Endpoint:
    # No built-in default: an unset url used to silently become 127.0.0.1:8000, so a
    # half-written config sent traffic to whatever happened to be listening there.
    url: str = ""
    model: str = "default"
    api_key_env: str | None = None
    timeout: float = 120.0
    temperature: float = 0.2
    effort: str = "low"              # low | medium | high — mapped onto every dialect
    thinking: bool = False           # for templates that only know enable_thinking
    dialect: str = "vllm"            # vllm | openai | generic — see the module docstring
    extra: dict = field(default_factory=dict)   # merged into the request body verbatim
    name: str = "thinker"
    last_error: str = ""             # why the last call failed, for health and logs
    last_usage: dict | None = None   # usage from the last answered call, when reported

    @classmethod
    def from_dict(cls, d: dict, name: str, base: "Endpoint | None" = None) -> "Endpoint":
        src = {**(base.__dict__ if base else {}), **{k: v for k, v in d.items() if v is not None}}
        src.pop("name", None)
        known = {k: src[k] for k in cls.__dataclass_fields__ if k in src}
        return cls(name=name, **known)

    def template_kwargs(self) -> dict:
        return {"enable_thinking": self.thinking,
                "reasoning_effort": self.effort,
                "thinking_effort": self.effort}

    def _body(self, system: str, user: str, max_tokens: int, temperature: float | None,
              dialect: str) -> dict:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
        }
        if dialect == "vllm":
            body["chat_template_kwargs"] = self.template_kwargs()
            body.update(self.extra)
        elif dialect == "openai":
            body.update(self.extra)
        return body                     # generic: the minimum, extras included nowhere

    def _choice(self, system: str, user: str, max_tokens: int,
                timeout: float | None, temperature: float | None) -> dict | None:
        """The raw `choices[0]` of one request, or None when the call did not produce
        one. Every caller of this endpoint goes through here, so the retry ladder, the
        `last_error` wording and the "unconfigured is unreachable" rule exist once —
        `ask()` and `ask_full()` differ only in what they read off the reply, never in
        how the call is made."""
        if not self.url:
            self.last_error = "no url configured"
            return None                 # unconfigured is unreachable, not "somewhere else"
        self.last_usage = None
        headers = {"Content-Type": "application/json", **bearer_headers(self.api_key_env)}
        # The note rides the HTTP failure instead of being written to last_error here:
        # written here it was a dead store — every exit path below reassigns last_error
        # before returning, so no operator could ever see it. A missing key surfaces as
        # a 401, and that is the message that needs to say the variable was never set.
        key_note = ("" if not self.api_key_env or "Authorization" in headers
                    else f"; {self.api_key_env} is not set")
        for dialect in (self.dialect, "generic"):
            try:
                req = urllib.request.Request(
                    self.url.rstrip("/") + "/chat/completions",
                    data=json.dumps(self._body(system, user, max_tokens, temperature,
                                               dialect)).encode(),
                    headers=headers)
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                    d = json.load(r)
                self.last_usage = d.get("usage") if isinstance(d.get("usage"), dict) else None
                choice = d["choices"][0]
                choice["message"]           # the shape ask() has always demanded
                self.last_error = ""
                return choice
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode()[:200]
                except Exception:
                    pass
                self.last_error = f"HTTP {e.code} ({dialect} body{key_note}): {detail}"
                # 400 usually means "I do not know that field". Worth one plainer attempt;
                # anything else is a key, a model name or a server, and retrying is noise.
                if e.code != 400 or dialect == "generic":
                    return None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                self.last_error = f"unreachable: {type(e).__name__}: {e}"
                return None
            except (KeyError, ValueError, IndexError) as e:
                self.last_error = f"unexpected reply shape: {type(e).__name__}: {e}"
                return None
        return None

    def ask(self, system: str, user: str, max_tokens: int = 400,
            timeout: float | None = None, temperature: float | None = None) -> str | None:
        """The answer text, or None when the call did not produce one.

        None means "degrade gracefully", never "the model said nothing" — the reason is
        left in `last_error` so an operator can tell a wrong key from a wrong URL from a
        rejected field."""
        choice = self._choice(system, user, max_tokens, timeout, temperature)
        if choice is None:
            return None
        m = choice["message"]
        # some servers put thinking in `reasoning`/`reasoning_content`; content wins
        return (m.get("content") or m.get("reasoning_content")
                or m.get("reasoning") or "").strip()

    def ask_full(self, system: str, user: str, max_tokens: int = 400,
                 timeout: float | None = None,
                 temperature: float | None = None) -> dict | None:
        """The same one request, taken apart instead of flattened: `content` (what a
        user would actually see), `reasoning` (whatever the server put in
        `reasoning_content`/`reasoning`) and `finish_reason`.

        `ask()` falls back to the reasoning when the content is empty; that is a
        thinker-side kindness — recall wants the best text going. A measurement wants
        the opposite: a model that spent its whole budget thinking and said nothing
        answered nothing, and a reply cut at the token cap must be visible as cut.
        Keeping the two apart is why this is a separate method rather than a flag."""
        choice = self._choice(system, user, max_tokens, timeout, temperature)
        if choice is None:
            return None
        m = choice["message"]
        return {"content": (m.get("content") or "").strip(),
                "reasoning": (m.get("reasoning_content") or m.get("reasoning") or "").strip(),
                "finish_reason": choice.get("finish_reason")}

    def alive(self) -> bool:
        if not self.url:
            return False
        try:
            req = urllib.request.Request(self.url.rstrip("/") + "/models",
                                         headers=bearer_headers(self.api_key_env))
            urllib.request.urlopen(req, timeout=5).read()
            return True
        except Exception:
            return False


@dataclass
class Models:
    thinker: Endpoint
    brain: Endpoint
    scribe: Endpoint

    @classmethod
    def from_config(cls, cfg: dict | None) -> "Models":
        cfg = cfg or {}
        thinker = Endpoint.from_dict(cfg.get("thinker", {}), "thinker")
        # Upgrade path: brain/scribe inherit thinker unless overridden.
        brain = Endpoint.from_dict(cfg.get("brain", {}), "brain", base=thinker)
        if "brain" in cfg and "effort" not in cfg["brain"]:
            brain.effort = "medium"          # listing work goes quiet on `low`
        scribe = Endpoint.from_dict(cfg.get("scribe", {}), "scribe", base=brain)
        if "scribe" in cfg and "temperature" not in cfg["scribe"]:
            scribe.temperature = 0.4
        return cls(thinker, brain, scribe)

    def describe(self) -> dict:
        def same(a: Endpoint, b: Endpoint) -> bool:
            return a.url == b.url and a.model == b.model
        shared = same(self.brain, self.thinker) and same(self.scribe, self.thinker)
        return {r: {"url": e.url, "model": e.model, "effort": e.effort,
                    "dialect": e.dialect, "api_key_env": e.api_key_env,
                    "last_error": e.last_error}
                for r, e in (("thinker", self.thinker), ("brain", self.brain),
                             ("scribe", self.scribe))} | {"single_model": shared}
