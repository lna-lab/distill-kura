"""Runtime thinker override: the recall role can be pointed at another model
without touching kura.toml.

The host that owns the conversation (an OpenCode plugin, a DSH preset, anything
that knows the model in use) sends `POST /thinker {url, model}` and the running
server swaps the endpoint used by `recall`. The distiller's brain/scribe are
untouched — this is the recall role only. No model is needed anywhere.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from distill_kura.registry import Registry
from distill_kura.server import _make_handler
from distill_kura.store import Store
from distill_kura.thinker import Endpoint, Models


def make_reg(tmp_path, stores=("m", "n")) -> Registry:
    ss = {}
    for name in stores:
        s = Store(name=name, path=str(tmp_path / name), label=name)
        s.init_files()
        ss[name] = s
    return Registry(stores=ss, modes={}, models=Models.from_config({}), default="m")


def serve(reg):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(reg))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# ── the registry method ─────────────────────────────────────────────────────

def test_no_override_keeps_configured_models(tmp_path):
    reg = make_reg(tmp_path)
    assert reg.models_for(reg.store("m")).thinker is reg.models.thinker


def test_override_swaps_the_thinker_for_every_store(tmp_path):
    reg = make_reg(tmp_path, ("m", "n"))
    reg.set_thinker_override("http://127.0.0.1:1234/v1", "open-model")
    for name in ("m", "n"):
        got = reg.models_for(reg.store(name))
        assert got.thinker.url == "http://127.0.0.1:1234/v1"
        assert got.thinker.model == "open-model"


def test_override_leaves_brain_and_scribe_alone(tmp_path):
    reg = make_reg(tmp_path)
    brain_before = reg.models_for(reg.store("m")).brain
    scribe_before = reg.models_for(reg.store("m")).scribe
    reg.set_thinker_override("http://127.0.0.1:1234/v1", "open-model")
    got = reg.models_for(reg.store("m"))
    assert got.brain is brain_before
    assert got.scribe is scribe_before


def test_override_needs_a_url(tmp_path):
    reg = make_reg(tmp_path)
    try:
        reg.set_thinker_override("")
        assert False, "an empty url must be refused"
    except ValueError as e:
        assert "url" in str(e)


# ── over HTTP ───────────────────────────────────────────────────────────────

def test_http_thinker_route_sets_the_override(tmp_path):
    reg = make_reg(tmp_path)
    srv, base = serve(reg)
    try:
        req = urllib.request.Request(
            f"{base}/thinker",
            data=json.dumps({"url": "http://127.0.0.1:1234/v1",
                             "model": "open-model"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        body = json.load(urllib.request.urlopen(req))
        assert body["ok"] is True
        assert reg.thinker_override is not None
        assert reg.models_for(reg.store("m")).thinker.url == "http://127.0.0.1:1234/v1"
    finally:
        srv.shutdown()


def test_http_thinker_route_refuses_an_empty_url(tmp_path):
    reg = make_reg(tmp_path)
    srv, base = serve(reg)
    try:
        req = urllib.request.Request(
            f"{base}/thinker",
            data=json.dumps({"url": ""}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req)
            assert False, "an empty url must be a 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
            assert "url" in json.load(e)["error"]
    finally:
        srv.shutdown()


# ── env override at load ────────────────────────────────────────────────────

def test_env_thinker_overrides_models_in_the_file(tmp_path, monkeypatch):
    cfg = tmp_path / "kura.toml"
    cfg.write_text(
        "[server]\n"
        "default = 'm'\n"
        "[models.thinker]\n"
        f"url = 'http://127.0.0.1:8000/v1'\n"
        "model = 'file-model'\n"
        f"[stores.m]\n"
        f"path = '{tmp_path / 'm'}'\n"
        f"[stores.n]\n"
        f"path = '{tmp_path / 'n'}'\n")
    monkeypatch.setenv("KURA_CONFIG", str(cfg))
    monkeypatch.setenv("KURA_THINKER_URL", "http://127.0.0.1:1234/v1")
    monkeypatch.setenv("KURA_THINKER_MODEL", "env-model")
    reg = Registry.load()
    assert reg.models.thinker.url == "http://127.0.0.1:1234/v1"
    assert reg.models.thinker.model == "env-model"


def test_env_thinker_without_models_in_file(tmp_path, monkeypatch):
    """The env path still works when the file declares no [models] at all."""
    cfg = tmp_path / "kura.toml"
    cfg.write_text(
        "[server]\n"
        "default = 'm'\n"
        f"[stores.m]\n"
        f"path = '{tmp_path / 'm'}'\n")
    monkeypatch.setenv("KURA_CONFIG", str(cfg))
    monkeypatch.setenv("KURA_THINKER_URL", "http://127.0.0.1:1234/v1")
    monkeypatch.setenv("KURA_THINKER_MODEL", "env-model")
    reg = Registry.load()
    assert reg.models.thinker.url == "http://127.0.0.1:1234/v1"
    assert reg.models.thinker.model == "env-model"
