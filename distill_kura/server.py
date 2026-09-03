"""The kura's one mouth: a small HTTP service over the whole registry.

Every route takes an optional store selector, so one process serves as many kura
as you configure and a client can switch modes per request:

    POST /recall            {"question", "hops": 1, "top": 3, "chars": 6000,
                             "total_chars", "store"|"mode": "eq"}
    POST /remember          {"slug","description","body","type","hook","title","tags",
                             the three sentences (flat or nested "annotations"), "store"}
    POST /annotate          {"slug","tags","belongs_because","keep","may_fade","store"}
                            merge onto an existing memory — the direct door
    GET  /index             ?store=maker
    GET  /doctor            ?store=maker          (?all=1 → every store at once)
    GET  /memory/<slug>     ?store=maker
    GET  /glance/<slug>     ?store=maker   the ~150-token confirmation, exact
    GET  /prefill           ?store=eq[&format=text][&window=N][&fraction=F]
                            the resident index block, ready to paste
    GET  /profile           ?store=eq             the store's charter + persona pointer
    GET  /stores            what exists, which mode maps where, which models
    GET  /health            liveness — and which build/pid/config is actually serving

Path-prefixed forms work too — `POST /s/eq/recall`, `GET /s/eq/index` — which is
handy for clients that can only vary a base URL per mode.

Stdlib only, threaded, no auth: bind to 127.0.0.1 unless you know what you are doing.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from . import prefill as prefill_mod
from .glance import glance
from .recall import recall
from .tokens import estimate
from .registry import Registry
from .store import ANNOTATION_KEYS

# Captured at import: the module rides in with the process, so this is when THIS code
# began serving — the one field a stale survivor of an earlier deploy cannot fake.
STARTED_AT = datetime.now().astimezone().isoformat(timespec="seconds")
MODULE_PATH = os.path.dirname(os.path.abspath(__file__))


def _annotations(p: dict) -> dict:
    """The three sentences, from either a nested `annotations` object or flat keys."""
    out = dict(p.get("annotations") or {})
    for k in ANNOTATION_KEYS:
        if p.get(k):
            out[k] = p[k]
    return out


def _make_handler(reg: Registry):
    class H(BaseHTTPRequestHandler):
        server_version = "distill-kura"

        def log_message(self, *a):
            pass

        # ── plumbing ─────────────────────────────────────────────────────
        def _send(self, code: int, obj, *,
                  ctype: str | None = "application/json; charset=utf-8",
                  headers: dict | None = None):
            """One reply. A str goes out as it stands (the `format=text` map), anything
            else as JSON; `ctype=None` sends no Content-Type at all, which is what a
            304 wants — a body type for an empty body would be a small lie."""
            b = obj.encode() if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            if ctype:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)

        def _split(self, path: str) -> tuple[str, str, dict]:
            """→ (route, store-selector, query). Understands /s/<store>/<route>."""
            u = urllib.parse.urlsplit(path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            p = u.path
            sel = q.get("store") or q.get("mode") or ""
            m = re.match(r"/s/([^/]+)(/.*)?$", p)
            if m:
                sel = urllib.parse.unquote(m.group(1))
                p = m.group(2) or "/"
            return p, sel, q

        def _store(self, sel: str):
            try:
                return reg.store(sel or None), None
            except KeyError:
                # Opt-in auto-provisioning: when `[server] auto_store_root` is set, a
                # caller-supplied store name (e.g. from an OpenCode plugin routing the
                # working directory) creates its store on first use. Anything else —
                # no root, a mode name, an unsafe name — stays the strict error.
                st = reg.ensure_store(sel)
                if st is not None:
                    return st, None
                return None, {"error": f"unknown store or mode: {sel!r}",
                              "stores": sorted(reg.stores), "modes": reg.modes}

        # ── GET ──────────────────────────────────────────────────────────
        def do_GET(self):
            # Malformed client values (?window=big, {"hops": "one"}) used to escape
            # as ValueError/TypeError and drop the connection with no reply at all.
            # The bad-json branch two doors down shows the contract: bad input is a
            # 400, an error the client can read.
            try:
                return self._do_GET()
            except (ValueError, TypeError) as e:
                return self._send(400, {"error": f"invalid argument: "
                                                 f"{type(e).__name__}: {e}"})

        def _do_GET(self):
            # Routes match EXACTLY (the query string is already split off). A prefix
            # test made `/healthz`, `/indexes` and `/storesX` answer as real routes,
            # so a client's typo — or a probe for a route this build does not have —
            # got a 200 for a different endpoint instead of an honest 404. Only
            # `/memory/` and `/glance/` are prefixes, because a slug follows.
            path, sel, q = self._split(self.path)
            if path == "/health":
                d = reg.stores[reg.default]
                return self._send(200, {
                    "ok": True, "default": reg.default,
                    "stores": {n: len(s.slugs()) for n, s in reg.stores.items()},
                    # `memories` and `dir` describe the DEFAULT store, so a client
                    # written against a single-kura service keeps working unchanged.
                    "memories": len(d.slugs()), "dir": d.path,
                    # Which BUILD is actually serving. A restart once "succeeded"
                    # while an old 0.0.0.0-bound process kept the port and served
                    # three deploys' worth of stale code — and nothing in this reply
                    # could show it. The package version does not move between
                    # commits; KURA_BUILD_ID (stamped at launch) does. Volatile
                    # fields are safe HERE: /health is never a prefix-cached surface.
                    "build_id": os.environ.get("KURA_BUILD_ID", "unknown"),
                    "version": __version__,
                    "pid": os.getpid(),
                    "started_at": STARTED_AT,
                    "module_path": MODULE_PATH,
                    "config_path": reg.config_path})
            if path == "/stores":
                return self._send(200, reg.describe())
            if path == "/doctor":
                # Bare /doctor answers for the DEFAULT store, like every other route.
                # Returning a per-store mapping here instead would silently change the
                # shape of the reply for any existing single-store client.
                if q.get("all") not in (None, "", "0", "false"):
                    return self._send(200, {n: s.doctor() for n, s in reg.stores.items()})
                st, err = self._store(sel)
                return self._send(404, err) if err else self._send(200, st.doctor())
            st, err = self._store(sel)
            if err:
                return self._send(404, err)
            if path == "/prefill":
                cfg = reg.prefill_cfg_for(st)
                loom = prefill_mod.loom_for(st, cfg)
                pf = prefill_mod.build_from_cfg(
                    st, loom, cfg,
                    window_tokens=q.get("window"), fraction=q.get("fraction"),
                    trail=prefill_mod.trail_for(st, cfg, loom=loom))
                # The map is the largest thing this server hands out and it changes a
                # few times a day, while clients re-read it every couple of minutes.
                inm = (self.headers.get("If-None-Match") or "").strip('"')
                et = {"ETag": f'"{pf.etag}"'}
                if inm and inm == pf.etag:
                    return self._send(304, "", ctype=None, headers=et)
                if q.get("format") == "text":
                    # For a shell hook or a `$(...)`: the block and nothing else.
                    return self._send(200, pf.text, ctype="text/plain; charset=utf-8",
                                      headers=et)
                return self._send(200, pf.as_dict(), headers=et)
            if path == "/index":
                t = st.index_text()
                return self._send(200, {"store": st.name, "index": t,
                                        "tokens_est": estimate(t)})
            if path == "/profile":
                # Persona lives on the HOST side (in DSH: the `persona` plugin and the
                # agent preset). We only record WHICH persona belongs with this kura and
                # hand the pointer over — we never render or inject it.
                return self._send(200, {
                    "store": st.name, "label": st.label,
                    "persona_path": st.persona,
                    "persona_exists": bool(st.persona and os.path.exists(st.persona)),
                    # Guarded like the distiller reads it (pipeline.py): a `charter`
                    # configured to a path that is not there is a config mistake, and
                    # answering "" for it beats dropping the whole /profile request.
                    "charter": (open(st.charter, encoding="utf-8").read()
                                if st.charter and os.path.exists(st.charter) else ""),
                    # The learned profile, with its state: a host that wants to show
                    # or inject it can; "absent" and "broken" are distinguishable.
                    "learned_profile": {**st.profile_state(), "text": st.profile_text()},
                    "modes": [m for m, t in reg.modes.items() if t == st.name]})
            if path.startswith("/memory/"):
                # EXACT: an explicit read answers for the memory that was named, or 404.
                # Fuzzy matching here would hand back a neighbour nobody asked for, and
                # a name is also the one place a caller could try to name a path.
                slug = urllib.parse.unquote(path.split("/memory/", 1)[1])
                t = st.read_exact(slug)
                s_ = st.resolve_exact(slug)
                return self._send(200 if t else 404,
                                  {"store": st.name, "slug": s_, "text": t,
                                   # Words about the memory, never weights: read-side
                                   # callers may show them, nothing ranks by them.
                                   "tags": list(st.tags(s_)) if s_ else [],
                                   "annotations": st.annotations(s_) if s_ else {}})
            if path.startswith("/glance/"):
                # EXACT, like read_exact: a slug the caller recognised on the map gets
                # its ~150-token mechanical confirmation, and a misspelling is an
                # honest 404 — never a neighbour. Exists so the recognition can be
                # confirmed BEFORE a full read takes its tokens.
                slug = urllib.parse.unquote(path.split("/glance/", 1)[1])
                g = glance(st, slug)
                return self._send(200 if g.get("ok") else 404, g)
            self._send(404, {"error": "not found", "path": path})

        # ── POST ─────────────────────────────────────────────────────────
        def do_POST(self):
            try:
                return self._do_POST()
            except (ValueError, TypeError) as e:
                return self._send(400, {"error": f"invalid argument: "
                                                 f"{type(e).__name__}: {e}"})

        def _do_POST(self):
            path, sel, _ = self._split(self.path)
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, {"error": "bad Content-Length"})
            try:
                p = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            sel = sel or p.get("store") or p.get("mode") or ""
            st, err = self._store(sel)
            if err:
                return self._send(404, err)
            if path == "/recall":
                tot = p.get("total_chars")
                return self._send(200, recall(st, reg.models_for(st).thinker,
                                              p.get("question", ""),
                                              int(p.get("hops", 1)), int(p.get("top", 3)),
                                              int(p.get("chars", 6000)),
                                              int(tot) if tot else None,
                                              fastpath_cfg=reg.fastpath_cfg_for(st)))
            if path == "/remember":
                # A tool call or a script: a DIRECT write, refused unless the store's
                # policy allows one. The distiller's verified pour is a different door.
                r = st.remember_direct(p.get("slug", ""), p.get("description", ""),
                                       p.get("body", ""), p.get("type", "project"),
                                       p.get("hook"), p.get("title"),
                                       tags=p.get("tags"), annotations=_annotations(p))
                return self._send(200 if r.get("ok") else 403, r)
            if path == "/annotate":
                # Tags and the three sentences on an existing memory, through the DIRECT
                # door: a distiller-only store refuses this too. The distiller's own
                # annotations go through `annotate_verified`, which has no route.
                r = st.annotate_direct(p.get("slug", ""), tags=p.get("tags"),
                                       annotations=_annotations(p))
                return self._send(200 if r.get("ok") else 403, r)
            self._send(404, {"error": "not found", "path": path})

    return H


def serve(reg: Registry, host: str | None = None, port: int | None = None) -> None:
    host, port = host or reg.host, port or reg.port
    print(f"蔵 distill-kura on {host}:{port}", flush=True)
    for n, s in reg.stores.items():
        mark = " (default)" if n == reg.default else ""
        ro = "" if s.write_policy == "direct-allowed" else f" [{s.write_policy}]"
        modes = [m for m, t in reg.modes.items() if t == n]
        print(f"  · {n}{mark}{ro}: {len(s.slugs())} memories at {s.path}"
              + (f"  modes={modes}" if modes else ""), flush=True)
    m = reg.models
    print(f"  thinker={m.thinker.model}@{m.thinker.url}"
          + ("" if m.describe()["single_model"] else
             f" brain={m.brain.model}@{m.brain.url} scribe={m.scribe.model}@{m.scribe.url}"),
          flush=True)
    ThreadingHTTPServer((host, port), _make_handler(reg)).serve_forever()
