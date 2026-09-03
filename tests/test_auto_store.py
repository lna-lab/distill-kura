"""Auto-provisioned stores: a host that routes the working directory to a logical
store (an OpenCode plugin) gets its store created on first use.

Opt-in via `[server] auto_store_root = "~/kura"`. Without it, an unknown store is
the strict error it always was. A name must be a single safe path component, and a
configured store or mode always wins. No model is needed anywhere.
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
from distill_kura.thinker import Models


def make_reg(tmp_path, auto_root=None, stores=("m",)) -> Registry:
    ss = {}
    for name in stores:
        s = Store(name=name, path=str(tmp_path / name), label=name)
        s.init_files()
        ss[name] = s
    return Registry(stores=ss, modes={"m": "m"},
                    models=Models.from_config({}), default="m",
                    auto_store_root=str(tmp_path / auto_root) if auto_root else None)


def serve(reg):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(reg))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# ── the registry method ─────────────────────────────────────────────────────

def test_ensure_store_creates_a_store_under_the_root(tmp_path):
    reg = make_reg(tmp_path, auto_root="auto")
    st = reg.ensure_store("test")
    assert st is not None
    assert st.name == "test"
    assert st.path == str((tmp_path / "auto" / "test"))
    assert os.path.exists(os.path.join(st.path, "_still"))
    assert os.path.exists(st.index_path)
    assert "test" in reg.stores


def test_ensure_store_is_idempotent(tmp_path):
    reg = make_reg(tmp_path, auto_root="auto")
    a = reg.ensure_store("test")
    b = reg.ensure_store("test")
    assert a is b


def test_ensure_store_off_returns_none(tmp_path):
    reg = make_reg(tmp_path)          # no auto root
    assert reg.ensure_store("test") is None


def test_ensure_store_refuses_unsafe_names(tmp_path):
    reg = make_reg(tmp_path, auto_root="auto")
    for bad in ("../etc", "a/b", "a\\b", ".hidden", "..", "a.", "-foo", "foo-", "Foo"):
        assert reg.ensure_store(bad) is None, bad
    assert not any(reg.stores.get(b) for b in ("../etc", "a/b", ".hidden"))


def test_ensure_store_never_shadows_a_configured_store_or_mode(tmp_path):
    reg = make_reg(tmp_path, auto_root="auto")
    assert reg.ensure_store("m") is None          # configured store (also a mode)
    assert reg.ensure_store("m") is None


# ── over HTTP ───────────────────────────────────────────────────────────────

def test_http_unknown_store_is_auto_created_when_enabled(tmp_path):
    reg = make_reg(tmp_path, auto_root="auto")
    srv, base = serve(reg)
    try:
        body = json.load(urllib.request.urlopen(f"{base}/doctor?store=test"))
        assert body["store"] == "test"
        assert "test" in reg.stores
    finally:
        srv.shutdown()


def test_http_unknown_store_still_errors_when_disabled(tmp_path):
    reg = make_reg(tmp_path)          # no auto root
    srv, base = serve(reg)
    try:
        try:
            urllib.request.urlopen(f"{base}/doctor?store=test")
            assert False, "an unknown store must stay an error without auto_store_root"
        except urllib.error.HTTPError as e:
            assert e.code == 404
            assert "unknown store or mode" in json.load(e)["error"]
    finally:
        srv.shutdown()


def test_http_unsafe_store_name_still_errors_when_enabled(tmp_path):
    reg = make_reg(tmp_path, auto_root="auto")
    srv, base = serve(reg)
    try:
        try:
            urllib.request.urlopen(f"{base}/doctor?store=..%2Fetc")
            assert False, "a traversal name must never auto-provision"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()


# ── config load ─────────────────────────────────────────────────────────────

def test_load_parses_auto_store_root(tmp_path, monkeypatch):
    cfg = tmp_path / "kura.toml"
    cfg.write_text(
        "[server]\n"
        "default = 'm'\n"
        f"auto_store_root = '{tmp_path / 'auto'}'\n"
        f"[stores.m]\n"
        f"path = '{tmp_path / 'm'}'\n")
    monkeypatch.setenv("KURA_CONFIG", str(cfg))
    reg = Registry.load()
    assert reg.auto_store_root == str(tmp_path / "auto")


def test_load_without_auto_store_root(tmp_path, monkeypatch):
    cfg = tmp_path / "kura.toml"
    cfg.write_text(
        "[server]\n"
        "default = 'm'\n"
        f"[stores.m]\n"
        f"path = '{tmp_path / 'm'}'\n")
    monkeypatch.setenv("KURA_CONFIG", str(cfg))
    reg = Registry.load()
    assert reg.auto_store_root is None