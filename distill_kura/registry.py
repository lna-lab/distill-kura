"""Registry = the set of kura a server holds, plus which *mode* maps to which kura.

Configuration is one TOML file (`kura.toml`). Resolution order for the path:
`--config` → `$KURA_CONFIG` → `./kura.toml` → `~/.config/distill-kura/kura.toml`.
With no config at all, `$KURA_DIR` (or `./memory`) becomes a single store named
`main`, so the old one-store workflow still works unchanged.

    [server]
    port = 8085
    host = "127.0.0.1"
    default = "main"                     # store used by un-prefixed routes

    [models.thinker]                     # the only required model
    url = "http://127.0.0.1:8011/v1"
    model = "local"
    [models.brain]                       # optional upgrade: a stronger reader
    url = "https://api.example.com/v1"
    model = "big-reader"
    api_key_env = "EXAMPLE_API_KEY"
    [models.scribe]                      # optional upgrade: a writer in your language

    [stores.main]
    path = "~/kura/main"
    label = "YUKI's kura"
    readonly = true                      # writes only through the distiller
    [stores.maker]
    path = "~/kura/maker"
    label = "maker mode"
    [stores.eq]
    path = "~/kura/eq"
    label = "EQ dialogue"

    [modes]                              # DSH preset / agent mode → store
    yuki = "main"
    maker = "maker"
    eq = "eq"

    [model_profiles.private.thinker]      # a store may bind its own endpoints
    url = "http://127.0.0.1:8100/v1"
    model = "private-thinker"
    [stores.project]
    path = "~/kura/project"
    model_profile = "private"            # undefined profile = load error, never a fallback

    [prefill]                            # the index as a standing system-prompt block
    window_tokens = 131072               # the agent's context window
    budget_fraction = 0.05               # keep the index under this share of it
    fresh_days = 14                      # memories touched this recently keep full lines
    pinned_types = ["feedback", "user"]  # these types always keep full lines
    trigger_tokens = 24                  # budget for one trimmed line

    [[payforward.mouths]]                # a mouth `kura pay-forward` bakes the map into
    name = "cpu-mouth"
    url = "http://127.0.0.1:8014"        # llama.cpp server BASE (slots API lives beside /v1)
    store = "main"                       # whose map this mouth wears
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field

from .store import Store, contained
from .thinker import Models
from .prefill import RESIDENT_MODES

CONFIG_CANDIDATES = ("kura.toml", os.path.expanduser("~/.config/distill-kura/kura.toml"))

# Keys a [stores.<name>] table may carry. Anything else is a typo until proven
# otherwise; extensions use an `x_` prefix so they are visibly not ours.
STORE_KEYS = {"path", "label", "readonly", "write_policy", "persona", "charter",
              "model_profile"}
NESTED_KEYS = {"distill", "prefill", "fastpath"}
_TYPES = {"path": str, "label": str, "readonly": bool, "write_policy": str,
          "persona": str, "charter": str, "model_profile": str,
          "distill": dict, "prefill": dict, "fastpath": dict}
# The nested tables need checking too: `inherit_global_journals = "false"` is a STRING,
# therefore truthy, so a store inherited the global intake it had explicitly declined.
_DISTILL_TYPES = {"inherit_global_journals": bool, "journals": dict, "language": str,
                  "scribe_slots": int, "chunk_chars": int, "max_items": int,
                  "coverage_passes": int,
                  # the watcher (`kura tend`)
                  "idle_min": (int, float), "backoff_min": (int, float), "yield_on_return": bool}
_PREFILL_TYPES = {"window_tokens": int, "budget_fraction": float, "hard_fraction": float,
                  "fresh_days": (int, float), "pinned_types": list, "trigger_tokens": int,
                  "verbatim_after": str, "cloth_path": str, "header": str,
                  "trail_tokens": int,
                  # M4 adaptive minimum recognition trigger — SHADOW by default: candidates
                  # are generated and judged, the production cloth keeps trigger_tokens
                  # until a benchmark says otherwise. An untouched old config changes nothing.
                  "adaptive_triggers": bool, "adaptive_apply": bool, "trigger_steps": list,
                  # M6: what the resident block wears when the full map is over the ceiling.
                  "resident_mode": str}
# M6 resident modes, imported so there is one list: `full` (today's map), `auto`
# (map while it fits, constellation over the ceiling), `constellation` (always).
# Tier zero of recall (`fastpath.py`). `gate` is the honesty bar: a hit below it is
# silence, and silence goes to the thinker.
_FASTPATH_TYPES = {"enabled": bool, "gate": (int, float), "cues": bool}
# One mouth `kura pay-forward` bakes the resident map into (`payforward.py`). `url` is
# the llama.cpp-compatible server's BASE — the slots API lives beside /v1, not under it
# — and `store` names whose map the mouth wears.
_MOUTH_TYPES = {"name": str, "url": str, "store": str, "slot": int, "model": str,
                "api_key_env": str}
_MOUTH_REQUIRED = ("name", "url", "store")
# [models.<role>] and [model_profiles.<p>.<role>]. Every other table is checked at
# load; these were not, so `api_key_ev = "KEY"` (typo) was silently dropped and
# requests left unauthenticated, and `timeout = "120"` crashed at call time — the
# silently-ignored field that looks exactly like a working one.
_MODEL_ROLES = {"thinker", "brain", "scribe"}
_MODEL_TYPES = {"url": str, "model": str, "api_key_env": str, "timeout": (int, float),
                "temperature": (int, float), "effort": str, "thinking": bool,
                "dialect": str, "extra": dict}
# Writer A/B entries deliberately use the endpoint's explicit fields rather than a
# second private config vocabulary. `rep` is the one bench convenience: it becomes a
# request extra so a report can say whether 1.00 or 1.05 was actually tested.
_BENCH_WRITER_TYPES = {"name": str, **_MODEL_TYPES, "rep": (int, float)}
_BENCH_WRITER_REQUIRED = ("name", "url", "model")


def _check_models(where: str, cfg) -> None:
    if not cfg:
        return                           # no [models] at all: thinker defaults apply
    if not isinstance(cfg, dict):
        raise ValueError(f"[{where}] must be a table of roles ({sorted(_MODEL_ROLES)}), "
                         f"got {type(cfg).__name__}")
    for role, rc in cfg.items():
        if role not in _MODEL_ROLES:
            raise ValueError(f"[{where}] has unknown role {role!r}. "
                             f"Known: {sorted(_MODEL_ROLES)}.")
        if not isinstance(rc, dict):
            raise ValueError(f"[{where}.{role}] must be a table, got {type(rc).__name__}")
        unknown = {k for k in rc if k not in _MODEL_TYPES and not k.startswith("x_")}
        if unknown:
            raise ValueError(f"[{where}.{role}] has unknown key(s) {sorted(unknown)}. "
                             f"Known: {sorted(_MODEL_TYPES)}.")
        _check_table(f"{where}.{role}", rc, _MODEL_TYPES)
        d = rc.get("dialect")
        if d is not None and d not in ("vllm", "openai", "generic"):
            raise ValueError(f"[{where}.{role}] dialect must be vllm, openai or generic, "
                             f"got {d!r}")


def mouth_base(url: str) -> str:
    """A mouth's normalized server base — together with the slot id, its PHYSICAL
    identity. Every other url in kura.toml carries `/v1`, so that slip is certain to
    happen here; the slots API lives BESIDE /v1, not under it, so the suffix is
    stripped rather than punished."""
    u = str(url).rstrip("/")
    if u.endswith("/v1"):
        u = u[: -len("/v1")].rstrip("/")
    return u


def _check_adaptive(section: str, t: dict) -> None:
    """The adaptive-trigger keys, refused loudly when they cannot mean what they say:
    steps that are not ascending ints would silently reorder the ladder; a step above
    trigger_tokens would "shorten" to something longer than the legacy budget; and
    `adaptive_apply` without `adaptive_triggers` is a switch wired to nothing."""
    steps = t.get("trigger_steps")
    if steps is not None:
        if not steps or any(not isinstance(x, int) or isinstance(x, bool) or x <= 0 for x in steps):
            raise ValueError(f"[{section}] trigger_steps must be a non-empty list of positive ints")
        if steps != sorted(set(steps)):
            raise ValueError(f"[{section}] trigger_steps must be strictly ascending: {steps}")
        cap = t.get("trigger_tokens", 24)
        if steps[-1] != cap:
            # The last rung IS the legacy budget: a ladder that ends below it can never
            # offer the size production wears today, and one that ends above it would
            # call a longer cue "shorter".
            raise ValueError(f"[{section}] trigger_steps must end at trigger_tokens={cap}, "
                             f"got {steps}")
    if t.get("adaptive_apply") and not t.get("adaptive_triggers"):
        raise ValueError(f"[{section}] adaptive_apply=true needs adaptive_triggers=true")


def _check_prefill(section: str, t: dict) -> None:
    """The [prefill] table's shape AND its enumerated values. A `resident_mode`
    outside the three names would either raise inside `prefill.build` on every
    request or be quietly read as `full` — named at load, like every other bad
    config value."""
    _check_unknown(section, t, _PREFILL_TYPES)
    _check_table(section, t, _PREFILL_TYPES)
    _check_adaptive(section, t)
    rm = t.get("resident_mode")
    if rm is not None and rm not in RESIDENT_MODES:
        raise ValueError(f"[{section}] resident_mode must be one of "
                         f"{list(RESIDENT_MODES)}, got {rm!r}")


def _check_unknown(where: str, table: dict, types: dict) -> None:
    """The same loud refusal the store tables give, for the tables that only ever had
    their TYPES checked. `_check_table` skips a key it does not know, so `[prefill]
    adaptive_aply = true` and `[fastpath] enable = false` changed nothing and said
    nothing — a switch the operator believes they threw. `x_`-prefixed names stay
    reserved for extensions, exactly as in [stores.<name>]."""
    unknown = {k for k in (table or {}) if k not in types and not k.startswith("x_")}
    if unknown:
        raise ValueError(f"[{where}] has unknown key(s) {sorted(unknown)}. "
                         f"Known: {sorted(types)}. "
                         f"Use an `x_`-prefixed name for your own extensions.")


def _check_table(where: str, table: dict, types: dict) -> None:
    for k, v in (table or {}).items():
        want = types.get(k)
        if want is None:
            continue
        if isinstance(want, tuple):
            ok = isinstance(v, want) and not isinstance(v, bool)
        else:
            ok = isinstance(v, want) and (want is bool or not isinstance(v, bool))
        if not ok:
            names = want.__name__ if not isinstance(want, tuple) else \
                " or ".join(t.__name__ for t in want)
            raise ValueError(f"[{where}] {k} must be {names}, "
                             f"got {type(v).__name__} ({v!r})")


def _check_types(name: str, sc: dict) -> None:
    _check_table(f"stores.{name}", sc, _TYPES)
    _check_table(f"stores.{name}.distill", sc.get("distill") or {}, _DISTILL_TYPES)
    _check_prefill(f"stores.{name}.prefill", sc.get("prefill") or {})
    _check_unknown(f"stores.{name}.fastpath", sc.get("fastpath") or {}, _FASTPATH_TYPES)
    _check_table(f"stores.{name}.fastpath", sc.get("fastpath") or {}, _FASTPATH_TYPES)


def _real(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def _inside(a: str, b: str) -> bool:
    """True when `a` is `b` or lives under it — the store's own containment check,
    kept under this name because `_check_paths` reads as "a inside b". A third
    implementation of the one predicate the trust model rests on is how the copies
    drift apart in argument order."""
    return contained(b, a)


def _check_paths(stores: dict[str, Store], raw: dict) -> None:
    """Refuse aliased, nested or journal-overlapping store roots, at load.

    Two names for one directory means a readonly alias and a writable one share data.
    A store inside another means backups and journal discovery cross. A journal root
    that contains a store means the distiller re-ingests memories as if a human had
    written them — which launders model-written text into [USER] evidence and breaks
    the one guarantee the gate exists to give.
    """
    if (raw.get("server") or {}).get("allow_path_overlap"):
        return                              # explicitly accepted; documented as dangerous
    reals = {n: _real(st.path) for n, st in stores.items()}
    names = sorted(reals)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if reals[a] == reals[b]:
                raise ValueError(f"[stores.{a}] and [stores.{b}] resolve to the same "
                                 f"directory ({reals[a]}). Two names for one store share "
                                 f"data, including their write policy.")
            if _inside(reals[a], reals[b]) or _inside(reals[b], reals[a]):
                raise ValueError(
                    f"[stores.{a}] and [stores.{b}] are nested ({reals[a]} / {reals[b]}). "
                    f"Express nesting as configuration, not as directories.")
    journals: dict[str, str] = {}
    for scope, cfg in [("distill", raw.get("distill") or {})] + [
            (f"stores.{n}.distill", (raw.get("stores") or {}).get(n, {}).get("distill") or {})
            for n in names]:
        for kind, root in (cfg.get("journals") or {}).items():
            # The documented table form (`{root = "..."}`) stringified into "{'root':
            # '...'}" and matched nothing, so it skipped this check entirely.
            r = root.get("root", "") if isinstance(root, dict) else root
            journals[f"{scope}.journals.{kind}"] = _real(str(r))
    items = sorted(journals.items())
    for i, (wa, ra) in enumerate(items):
        for wb, rb in items[i + 1:]:
            if ra != rb and (_inside(ra, rb) or _inside(rb, ra)):
                raise ValueError(
                    f"[{wa}] = {ra} and [{wb}] = {rb} are nested: the outer store would "
                    f"drink the inner store's whole intake, which is the contamination "
                    f"separate memory directories were supposed to prevent.")
    for where, root in journals.items():
        for n, real in reals.items():
            if _inside(real, root) or _inside(root, real):
                raise ValueError(
                    f"[{where}] = {root} overlaps [stores.{n}] at {real}. The distiller "
                    f"would re-ingest memories as raw material and file model-written "
                    f"text as the human's words. Move the journal root outside the store.")


def _check_mouths(raw: dict, stores: dict[str, Store]) -> None:
    """Validate `[[payforward.mouths]]` at load, the way [fastpath] and the store
    tables are: a bad mouth throws with the offending value named, because a silently
    skipped mouth looks exactly like a fleet that is warm."""
    pf = raw.get("payforward") or {}
    unknown = {k for k in pf if k != "mouths" and not k.startswith("x_")}
    if unknown:
        raise ValueError(f"[payforward] has unknown key(s) {sorted(unknown)}. "
                         f"Known: ['mouths']. Use an `x_`-prefixed name for your own "
                         f"extensions.")
    mouths = pf.get("mouths") or []
    if not isinstance(mouths, list):
        raise ValueError(f"[payforward] mouths must be an array of tables "
                         f"([[payforward.mouths]]), got {type(mouths).__name__}")
    seen: set[str] = set()
    phys: dict[tuple[str, int], str] = {}
    for i, m in enumerate(mouths):
        where = f"payforward.mouths[{i}]"
        if not isinstance(m, dict):
            raise ValueError(f"[{where}] must be a table ([[payforward.mouths]])")
        for k in _MOUTH_REQUIRED:
            if not m.get(k):
                raise ValueError(f"[{where}] needs `{k}`")         # fail loudly at load
        unknown = {k for k in m if k not in _MOUTH_TYPES and not k.startswith("x_")}
        if unknown:
            raise ValueError(f"[{where}] has unknown key(s) {sorted(unknown)}. "
                             f"Known: {sorted(_MOUTH_TYPES)}. "
                             f"Use an `x_`-prefixed name for your own extensions.")
        _check_table(where, m, _MOUTH_TYPES)
        if isinstance(m.get("slot"), int) and m["slot"] < 0:
            raise ValueError(f"[{where}] slot must be >= 0, got {m['slot']}")
        name = str(m["name"])
        if name in seen:
            raise ValueError(f"[payforward] two mouths named {name!r}: the state file "
                             f"is keyed on the name, so the second would wear the "
                             f"first one's record of what was baked.")
        seen.add(name)
        # The NAME is the state key, but the PHYSICAL identity is (base url, slot):
        # two names pointed at one slot would race on its KV, each runner saving over
        # the other's map — and the state files would both claim success.
        ident = (mouth_base(m["url"]), int(m.get("slot", 0)))
        if ident in phys:
            raise ValueError(f"[{where}] and mouth {phys[ident]!r} are the same "
                             f"physical slot ({ident[0]}, slot {ident[1]}). Two names "
                             f"for one slot race on its KV; give each mouth its own "
                             f"slot, or keep one entry.")
        phys[ident] = name
        if m["store"] not in stores:
            # A mode name is refused too, deliberately: a mouth is standing hardware
            # wearing ONE store's map, not a session-level selector.
            raise ValueError(f"[{where}] store = {m['store']!r} is not a configured "
                             f"store. Known: {sorted(stores)}")


def _check_bench_writers(raw: dict) -> None:
    """Validate benchmark writers at load, so a misspelled endpoint is not a
    successful benchmark of zero writers.

    The benchmark compares raw writer behaviour. Silently dropping `api_key_env`,
    `extra`, or a misspelled repetition setting would compare different requests than
    the operator thought they configured, which is worse than refusing the run."""
    bench = raw.get("bench")
    if bench is None:
        bench = {}
    if not isinstance(bench, dict):
        raise ValueError(f"[bench] must be a table, got {type(bench).__name__}")
    unknown = {k for k in bench if k != "writers" and not k.startswith("x_")}
    if unknown:
        raise ValueError(f"[bench] has unknown key(s) {sorted(unknown)}. "
                         "Known: ['writers']. Use an `x_`-prefixed name for your own "
                         "extensions.")
    writers = bench.get("writers") or []
    if not isinstance(writers, list):
        raise ValueError(f"[bench] writers must be an array of tables "
                         f"([[bench.writers]]), got {type(writers).__name__}")
    seen: set[str] = set()
    for i, w in enumerate(writers):
        where = f"bench.writers[{i}]"
        if not isinstance(w, dict):
            raise ValueError(f"[{where}] must be a table ([[bench.writers]])")
        for k in _BENCH_WRITER_REQUIRED:
            if not w.get(k):
                raise ValueError(f"[{where}] needs `{k}`")
        unknown = {k for k in w if k not in _BENCH_WRITER_TYPES and not k.startswith("x_")}
        if unknown:
            raise ValueError(f"[{where}] has unknown key(s) {sorted(unknown)}. "
                             f"Known: {sorted(_BENCH_WRITER_TYPES)}. "
                             "Use an `x_`-prefixed name for your own extensions.")
        _check_table(where, w, _BENCH_WRITER_TYPES)
        d = w.get("dialect")
        if d is not None and d not in ("vllm", "openai", "generic"):
            raise ValueError(f"[{where}] dialect must be vllm, openai or generic, got {d!r}")
        name = str(w["name"])
        if name in seen:
            raise ValueError(f"[bench] two writers named {name!r}: paired report rows "
                             "would no longer identify one endpoint uniquely")
        seen.add(name)


@dataclass
class Registry:
    stores: dict[str, Store]
    modes: dict[str, str]
    models: Models
    default: str
    profiles: dict = field(default_factory=dict)
    host: str = "127.0.0.1"
    port: int = 8085
    config_path: str | None = None
    raw: dict = field(default_factory=dict)

    # ── loading ──────────────────────────────────────────────────────────
    @classmethod
    def load(cls, path: str | None = None) -> "Registry":
        path = path or os.environ.get("KURA_CONFIG") or next(
            (p for p in CONFIG_CANDIDATES if os.path.exists(p)), None)
        if path:
            with open(path, "rb") as f:
                raw = tomllib.load(f)
        else:
            raw = {}
        stores: dict[str, Store] = {}
        for name, sc in (raw.get("stores") or {}).items():
            if not str(name).strip():
                raise ValueError("a store needs a name: [stores.\"\"] can never be selected, "
                                 "because an empty selector means \"the default store\".")
            if "path" not in sc:
                raise ValueError(f"[stores.{name}] needs `path`")      # fail loudly at load
            unknown = set(sc) - STORE_KEYS - NESTED_KEYS
            unknown = {k for k in unknown if not k.startswith("x_")}
            if unknown:
                # A typo used to land in `extra` and do nothing: `readnoly = true` reads
                # as a store that is protected and is not. Silence is the failure mode.
                raise ValueError(
                    f"[stores.{name}] has unknown key(s) {sorted(unknown)}. "
                    f"Known: {sorted(STORE_KEYS | NESTED_KEYS)}. "
                    f"Use an `x_`-prefixed name for your own extensions.")
            _check_types(name, sc)
            if "readonly" in sc and "write_policy" in sc:
                # The deprecated key was applied AFTER the new one and always won, so an
                # operator tightening a store while a stale `readonly = false` sat in the
                # file got a fully writable store, signalled by one word in a JSON dump.
                raise ValueError(
                    f"[stores.{name}] sets both `readonly` and `write_policy`. "
                    f"`readonly` is deprecated and would win. Keep write_policy alone.")
            stores[name] = Store(name=name,
                                 **{k: v for k, v in sc.items() if k in STORE_KEYS},
                                 extra={k: v for k, v in sc.items() if k in NESTED_KEYS
                                        or k.startswith("x_")})
        if not stores:
            d = os.environ.get("KURA_DIR", os.path.abspath("memory"))
            stores["main"] = Store(name="main", path=d, label=os.environ.get("KURA_LABEL", "kura"))
        _check_table("distill", raw.get("distill") or {}, _DISTILL_TYPES)
        _check_prefill("prefill", raw.get("prefill") or {})
        _check_unknown("fastpath", raw.get("fastpath") or {}, _FASTPATH_TYPES)
        _check_table("fastpath", raw.get("fastpath") or {}, _FASTPATH_TYPES)
        srv = raw.get("server") or {}
        default = srv.get("default") or next(iter(stores))
        if default not in stores:
            raise ValueError(f"[server] default = {default!r} is not a configured store")
        modes = {str(k): str(v) for k, v in (raw.get("modes") or {}).items()}
        for m, s in modes.items():
            if s not in stores:
                raise ValueError(f"[modes] {m} = {s!r} is not a configured store")
            # A mode named after a DIFFERENT store makes `store()` ambiguous, and the
            # store silently wins. `eq = "eq"` is fine; `eq = "maker"` alongside a store
            # called `eq` is a trap that reads as working.
            if m in stores and s != m:
                raise ValueError(
                    f"[modes] {m} = {s!r} collides with the store called {m!r}: a "
                    f"selector {m!r} would resolve to the store, not this mode. "
                    f"Rename one of them.")
        _check_paths(stores, raw)
        _check_mouths(raw, stores)
        _check_bench_writers(raw)
        models_cfg = raw.get("models")
        if not models_cfg and os.environ.get("KURA_THINKER_URL"):      # legacy env
            models_cfg = {"thinker": {"url": os.environ["KURA_THINKER_URL"],
                                      "model": os.environ.get("KURA_THINKER_MODEL", "default")}}
        _check_models("models", models_cfg)
        profiles = {}
        for pname, pcfg in (raw.get("model_profiles") or {}).items():
            _check_models(f"model_profiles.{pname}", pcfg)
        for pname, pcfg in (raw.get("model_profiles") or {}).items():
            # Models.from_config chains thinker -> brain -> scribe, so a role missing at
            # the head lands on Endpoint()'s built-in default. A profile defining only
            # `brain` sent the private index to an endpoint named nowhere in the file —
            # the exact fallback this feature exists to forbid.
            head = (pcfg or {}).get("thinker") or {}
            if not head.get("url"):
                raise ValueError(
                    f"[model_profiles.{pname}] must define thinker.url. A role left out "
                    f"falls back to the built-in default endpoint, which is how a "
                    f"private index reaches a shared model.")
            profiles[pname] = Models.from_config(pcfg)
        for n, st in stores.items():
            want = st.model_profile
            if want and want not in profiles:
                # No implicit fallback: silently using the shared endpoint is how a
                # store's whole index reaches a model it was never meant to see.
                raise ValueError(f"[stores.{n}] model_profile = {want!r} is not defined. "
                                 f"Known profiles: {sorted(profiles)}")
        # Port is coerced nowhere else silently: `8085.9` truncated to 8085 and
        # `true` became 1, both accepted — every other value in this file is
        # type-checked at load with the offender named.
        port_raw = os.environ.get("KURA_PORT", srv.get("port", 8085))
        if isinstance(port_raw, bool) or not isinstance(port_raw, (int, str)):
            raise ValueError(f"[server] port must be an integer, got {type(port_raw).__name__} "
                             f"({port_raw!r})")
        if isinstance(port_raw, str):
            if not (port_raw.strip().isascii() and port_raw.strip().isdigit()):
                raise ValueError(f"[server] port must be an integer, got {port_raw!r}")
            port = int(port_raw)
        else:
            port = port_raw
        return cls(stores=stores, modes=modes, models=Models.from_config(models_cfg),
                   profiles=profiles,
                   default=default, host=srv.get("host", "127.0.0.1"),
                   port=port,
                   config_path=path, raw=raw)

    # ── lookups ──────────────────────────────────────────────────────────
    def store(self, name: str | None = None) -> Store:
        """Accepts a store name OR a mode name. None → default."""
        if not name:
            return self.stores[self.default]
        if name in self.stores:
            return self.stores[name]
        if name in self.modes:
            return self.stores[self.modes[name]]
        raise KeyError(name)

    def store_for_mode(self, mode: str | None) -> Store:
        """Strict: an unknown mode raises.

        It used to fall back to the default store, which turned a typo in a mode name
        into "a different household's memory answered, fluently". That is the opposite
        of failing loudly, and it is nearly impossible to notice from the outside."""
        return self.store(mode)

    def store_for_mode_or_default(self, mode: str | None) -> Store:
        """The fallback, for callers that genuinely want one — named so the choice is
        visible at the call site rather than hidden in a lookup."""
        try:
            return self.store(mode)
        except KeyError:
            return self.stores[self.default]

    def models_for(self, store: Store) -> Models:
        """The model roles THIS store may use.

        One shared set of endpoints means one endpoint sees every store's index, every
        journal and every draft — so separating stores on disk buys nothing against the
        model. A store naming a profile gets that profile and nothing else; an undefined
        profile is a load error rather than a quiet fall back to the shared one."""
        want = store.model_profile
        if not want:
            return self.models
        return self.profiles[want]

    def _own(self, store: Store, table: str) -> dict:
        """A store's own `[stores.<name>.<table>]`, or {} when it has none."""
        own = store.extra.get(table)
        return own if isinstance(own, dict) else {}

    def _cfg_for(self, store: Store, table: str) -> dict:
        """Global `[<table>]`, overridden per store by `[stores.<name>.<table>]`.

        Key PRESENCE, not truthiness: an explicit `0` or `""` in a store's table is
        that store's answer, not a request to fall back to the global one.
        """
        return {**dict(self.raw.get(table) or {}), **self._own(store, table)}

    @property
    def prefill_cfg(self) -> dict:
        return dict(self.raw.get("prefill") or {})

    def prefill_cfg_for(self, store: Store) -> dict:
        return self._cfg_for(store, "prefill")

    def distill_cfg_for(self, store: Store) -> dict:
        return self._cfg_for(store, "distill")

    @property
    def payforward_mouths(self) -> list[dict]:
        """The mouths `kura pay-forward` serves, defaults applied. Validated at load,
        so a mouth returned here is complete and names a real store."""
        return [{"name": str(m["name"]), "url": str(m["url"]), "store": str(m["store"]),
                 "slot": int(m.get("slot", 0)), "model": str(m.get("model", "default")),
                 "api_key_env": m.get("api_key_env")}
                for m in (self.raw.get("payforward") or {}).get("mouths") or []]

    @property
    def bench_writers(self) -> list[dict]:
        """The configured A/B writers, with `rep` made into an endpoint extra.

        The request body is the thing being compared. Keeping the repetition setting
        in `extra` means the same endpoint can be benchmarked at 1.00 and 1.05 while
        the report records the knob that changed, instead of hiding it in a label."""
        out = []
        for raw in (self.raw.get("bench") or {}).get("writers") or []:
            w = {k: v for k, v in raw.items() if k in _MODEL_TYPES or k == "name"}
            w["name"] = str(raw["name"])
            w["url"] = str(raw["url"])
            w["model"] = str(raw["model"])
            if "extra" in w:
                w["extra"] = dict(w["extra"])
            else:
                w["extra"] = {}
            if raw.get("rep") is not None:
                # Explicit extra wins: it names the exact API field, while `rep` is
                # only shorthand. Two values would be an endpoint ambiguity.
                if "repeat_penalty" not in w["extra"] and "repetition_penalty" not in w["extra"]:
                    w["extra"]["repeat_penalty"] = raw["rep"]
                w["rep"] = raw["rep"]
            else:
                w["rep"] = (w["extra"].get("repeat_penalty")
                             if "repeat_penalty" in w["extra"]
                             else w["extra"].get("repetition_penalty"))
            out.append(w)
        return out

    @property
    def fastpath_cfg(self) -> dict:
        return dict(self.raw.get("fastpath") or {})

    def fastpath_cfg_for(self, store: Store) -> dict:
        return self._cfg_for(store, "fastpath")

    def describe(self) -> dict:
        return {
            "default": self.default,
            "stores": {n: {"label": s.label, "path": s.path,
                           "write_policy": s.write_policy,
                           "memories": len(s.slugs()), "persona": bool(s.persona),
                           "charter": bool(s.charter)} for n, s in self.stores.items()},
            "modes": self.modes,
            "models": self.models.describe(),
            "model_profiles": sorted(self.profiles),
            "prefill": self.prefill_cfg,
            "fastpath": self.fastpath_cfg,
            "payforward": {"mouths": self.payforward_mouths},
            "bench": {"writers": [{"name": w["name"], "url": w["url"],
                                    "model": w["model"], "rep": w.get("rep")}
                                   for w in self.bench_writers]},
            "config": self.config_path,
        }
