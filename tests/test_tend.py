"""The watcher: quiet is a journal that has not changed; nothing-to-do is exit 2 and a
rest; the human's return stops the track (unless the editor sits elsewhere); a
heartbeat says whether anyone is tending the store at all.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from distill_kura.registry import Registry     # noqa: E402
from distill_kura.store import Store           # noqa: E402
from distill_kura import tend as tendmod   # noqa: E402
from distill_kura.tend import TRACKS, Tender   # noqa: E402


def build(tmp_path, **distill):
    Store(name="m", path=str(tmp_path / "m")).init_files()
    jdir = tmp_path / "journals"; jdir.mkdir()
    (jdir / "s.jsonl").write_text(json.dumps({"type": "user", "message": {"content": [
        {"type": "text", "text": "hello " * 3000}]}}) + "\n", encoding="utf-8")
    extra = "".join(f"{k} = {json.dumps(v)}\n" for k, v in distill.items())
    cfg = tmp_path / "kura.toml"
    cfg.write_text(f"""
[stores.m]
path = "{tmp_path / 'm'}"
[models.thinker]
url = "http://127.0.0.1:9/v1"
model = "none"
[distill]
{extra}
[distill.journals]
claude = "{jdir}"
""", encoding="utf-8")
    reg = Registry.load(str(cfg))
    return reg, reg.store("m"), str(cfg), jdir / "s.jsonl"


def test_quiet_is_the_journals_mtime_and_a_fresh_journal_is_not_quiet(tmp_path):
    reg, st, cfg, j = build(tmp_path)
    t = Tender(reg, st, cfg, idle_min=10)
    stamp = t.tick(0.0)
    assert stamp == os.path.getmtime(j)
    assert t.proc is None                                   # just written: not quiet
    assert st.tend_state()["alive"]                         # but the heartbeat is there


def test_nothing_to_do_is_exit_two_and_a_rest_not_a_spin(tmp_path):
    reg, st, cfg, j = build(tmp_path, backoff_min=20)
    old = time.time() - 3600
    os.utime(j, (old, old))                                 # quiet for an hour
    t = Tender(reg, st, cfg, idle_min=10)
    t.tick(0.0)
    assert t.proc_track == "distill"                        # no drafts → distil
    t.proc.wait(timeout=120)
    t.reap()
    # a dead brain is not "nothing worth drinking": the water it reserved is kept on
    # the shelf and the track reports failure (rc 1) — either way, it rests
    assert t.next_ok["distill"] > time.time() + 19 * 60
    log = open(os.path.join(st.still, "tend.log"), encoding="utf-8").read()
    assert "resting 20 min" in log
    assert "distill run" not in log or "→ distill" in log   # decisions kept, never /dev/null
    # the next tick does not relaunch the resting track; it moves on to tidy, once
    t.next_ok["warm"] = time.time() + 9999      # not this test's subject
    t.tick(os.path.getmtime(j))
    assert t.proc_track == "tidy"
    t.proc.wait(timeout=120); t.reap()
    t.tick(os.path.getmtime(j))
    assert t.proc is None                                   # everything is resting or done


def test_the_humans_return_stops_a_running_track_unless_told_not_to(tmp_path):
    reg, st, cfg, j = build(tmp_path)
    old = time.time() - 3600
    os.utime(j, (old, old))
    t = Tender(reg, st, cfg, idle_min=10)
    t._cmd = lambda track: [sys.executable, "-c", "import time; time.sleep(60)"]   # type: ignore
    stamp = t.tick(0.0)
    assert t.proc is not None
    with open(j, "a", encoding="utf-8") as f:
        f.write("\n")                                       # the human types
    t.tick(stamp)
    assert t.proc is None
    assert "stopped — the journal changed" in open(os.path.join(st.still, "tend.log"), encoding="utf-8").read()
    # with the editor on its own seat, the verdict in flight is left to finish
    os.utime(j, (old, old))
    t2 = Tender(reg, st, cfg, idle_min=10, yield_on_return=False)
    t2._cmd = t._cmd                                        # type: ignore
    stamp = t2.tick(0.0)
    with open(j, "a", encoding="utf-8") as f:
        f.write("\n")
    t2.tick(stamp)
    assert t2.proc is not None and t2.proc.poll() is None
    t2.kill("test over")


def test_work_is_counted_and_launches_are_not(tmp_path):
    reg, st, cfg, j = build(tmp_path)
    t = Tender(reg, st, cfg, idle_min=10)
    assert set(t.done) == {"poured", "tossed", "fixed", "drafts", "woven",
                           "trailed", "paid", "warmed", "tidied"}
    assert not any(k.endswith("_runs") or "launch" in k for k in t.done)


def test_payforward_is_scheduled_after_a_weave_and_only_then(tmp_path):
    """The map cannot have changed without a weave, so the payforward track waits for
    one — and runs once per weave, not once per tick (a mouth restart is the systemd
    hook's job, not the watcher's)."""
    reg, st, cfg, j = build(tmp_path)
    t = Tender(reg, st, cfg, idle_min=10)
    now = time.time()
    for track in ("drain", "distill", "warm", "tidy"):
        t.next_ok[track] = now + 9999           # only the question at hand remains
    assert t.choose(now) is None                # no weave yet → no payforward
    t._woven_this_silence = True
    assert t.choose(now) == "trail"             # the trail follows the weave FIRST
    t._trailed_this_silence = True
    assert t.choose(now) == "payforward"
    t._paid_this_silence = True
    assert t.choose(now) is None                # once per weave, not once per tick


def test_a_pour_does_not_leave_the_trail_absent_forever(tmp_path):
    """The reviewer's scenario, end to end at the choose() level: a pour retires
    the trail (the revision moved), the watcher weaves — and the very next track
    must be the trail, or 'the current path' goes absent until a human runs
    `kura trail` by hand. The trail is model-free: this maintenance is cheap."""
    import subprocess as sp
    reg, st, cfg, j = build(tmp_path, backoff_min=20)
    old = time.time() - 3600
    os.utime(j, (old, old))
    t = Tender(reg, st, cfg, idle_min=10)
    # a real trail on disk (the store needs a fresh memory to have one), then a
    # store mutation retires it
    from distill_kura.prefill import loom_for, trail_for
    st.remember_direct("todays-work", "the seed the trail will show",
                       f"dated {time.strftime('%Y-%m-%d')}")
    cfgp = reg.prefill_cfg_for(st)
    assert trail_for(st, cfgp, loom=loom_for(st, cfgp)).write()["written"] is True
    st.remember_direct("poured-while-watching", "a memory poured in the quiet", "body")
    assert trail_for(st, cfgp, loom=loom_for(st, cfgp)).is_stale() is True
    now = time.time()
    for track in ("drain", "distill", "warm", "tidy"):
        t.next_ok[track] = now + 9999
    t._woven_this_silence = True                # the weave that follows a pour
    assert t.choose(now) == "trail"
    assert "trail" in t._cmd("trail")


def test_a_trail_past_its_own_horizon_is_rebuilt_in_the_quiet(tmp_path):
    """Time alone retires a trail — the fresh window slides with no store write.
    The quiet cycle checks the trail's own proof and rebuilds it, cheaply."""
    reg, st, cfg, j = build(tmp_path)
    from distill_kura.prefill import loom_for, trail_for
    st.remember_direct("todays-work", "the seed the trail will show",
                       f"dated {time.strftime('%Y-%m-%d')}")
    cfgp = reg.prefill_cfg_for(st)
    tr = trail_for(st, cfgp, loom=loom_for(st, cfgp))
    assert tr.write()["written"] is True
    sv = tr._state()
    sv["valid_until"] = time.time() - 1         # the horizon has passed
    json.dump(sv, open(tr.state_path, "w"))
    t = Tender(reg, st, cfg, idle_min=10)
    now = time.time()
    for track in ("drain", "distill", "warm", "tidy"):
        t.next_ok[track] = now + 9999
    assert t.choose(now) == "trail"


def test_an_absent_trail_is_not_a_per_chore(tmp_path):
    """No trail file + no weave = nothing to maintain: the time check only
    rebuilds a trail that EXISTS (the after-weave path is what creates one)."""
    reg, st, cfg, j = build(tmp_path)
    t = Tender(reg, st, cfg, idle_min=10)
    now = time.time()
    for track in ("drain", "distill", "warm", "tidy"):
        t.next_ok[track] = now + 9999
    assert t.choose(now) is None, "an absent trail is honest absence, not a chore"


def test_doctor_reports_a_dead_watcher(tmp_path):
    reg, st, cfg, j = build(tmp_path)
    assert st.doctor()["tending"] == {"alive": False, "why": "no watcher has ever run here"}
    t = Tender(reg, st, cfg, idle_min=10)
    t.tick(0.0)
    assert st.doctor()["tending"]["alive"]
    p = os.path.join(st.still, "tend.json")
    d = json.load(open(p)); d["at"] = int(time.time()) - 600
    json.dump(d, open(p, "w"))
    assert not st.doctor()["tending"]["alive"]
    assert "heartbeat" in st.doctor()["tending"]["why"]


def test_cli_once_runs_a_tick_and_exits(tmp_path):
    reg, st, cfg, j = build(tmp_path)
    old = time.time() - 3600
    os.utime(j, (old, old))
    e = {**os.environ, "PYTHONPATH": ROOT}
    p = subprocess.run([sys.executable, "-m", "distill_kura.cli", "-c", cfg, "-s", "m", "tend", "--once"],
                       capture_output=True, text=True, env=e, timeout=300)
    # The dead thinker leaves no drafts. The always-0 return used to hide that from
    # schedulers; now the code is honest — 1 because the batch is owed (the brain never
    # answered and its segments are on the shelf), 2 only when there was nothing there.
    assert p.returncode in (1, 2), p.stderr
    last = [l for l in p.stdout.splitlines() if l.startswith("{")][-1]
    out = json.loads(last)
    assert out["store"] == "m" and "done" in out
    assert not any(out["done"].values())            # nothing was poured or fixed
    assert os.path.exists(os.path.join(st.still, "tend.log"))


def test_a_store_with_no_journals_says_so_instead_of_waiting_forever(tmp_path):
    Store(name="a", path=str(tmp_path / "a")).init_files()
    Store(name="b", path=str(tmp_path / "b")).init_files()
    cfg = tmp_path / "kura.toml"
    cfg.write_text(f"""
[stores.a]
path = "{tmp_path / 'a'}"
[stores.b]
path = "{tmp_path / 'b'}"
[models.thinker]
url = "http://127.0.0.1:9/v1"
model = "none"
""", encoding="utf-8")
    reg = Registry.load(str(cfg))
    t = Tender(reg, reg.store("a"), str(cfg))
    assert t.journals == {}
    assert t.newest_mtime() == 0.0
    t.tick(0.0)
    assert t.proc is None


def test_catchup_starts_from_today_without_losing_further_marks(tmp_path):
    """Pointing a distiller at an old journal would drink all of it. catchup moves the
    marks to the end of every journal — forward only, so a mark already past stays."""
    from distill_kura.distill import Distiller
    reg, st, cfg, j = build(tmp_path)
    d = Distiller(reg, st)
    r = d.catch_up()
    assert r["ok"] and r["journals"] == 1 and r["moved"] == 1
    assert d.sip_one() is None                      # nothing left to drink
    marks = d.marks.read()
    key = "claude:" + os.path.basename(str(j))
    assert marks[key] == os.path.getsize(j)
    d.marks.advance(key, marks[key] + 10_000)       # someone is further along
    d.catch_up()
    assert d.marks.read()[key] == marks[key] + 10_000   # never pulled backwards


def _stub(t, rc, out=""):
    """Make the next track exit with `rc` after printing `out` — the exit code is the
    only thing the watcher reads, so a two-line script stands in for any track."""
    t._cmd = lambda track: [sys.executable, "-c",                     # type: ignore
                            f"print({out!r});raise SystemExit({rc})"]


def _only_payforward(t):
    """Rest every other track and put the silence one weave-and-trail in, so choose()
    has exactly one question left: does the map still need paying forward?"""
    for track in ("drain", "distill", "tidy", "trail", "warm"):
        t.next_ok[track] = time.time() + 9999
    t._woven_this_silence = True
    t._trailed_this_silence = True


def test_a_failed_payforward_is_retried_on_the_next_tick_of_the_same_silence(tmp_path):
    """The flag belongs to the outcome, not the launch. Set at launch, a pay-forward
    that failed (a mouth down) or lost the lock (another runner holds the slot — both
    exit 1) was not tried again until the next weave or the next human return: the
    map stayed cold for the rest of the quiet, exactly when the mouth needed it."""
    reg, st, cfg, j = build(tmp_path, backoff_min=20)
    old = time.time() - 3600
    os.utime(j, (old, old))
    t = Tender(reg, st, cfg, idle_min=10)
    _only_payforward(t)
    _stub(t, 1)
    stamp = t.tick(0.0)
    assert t.proc_track == "payforward"
    assert t._paid_this_silence is False, "a launch settles nothing"
    t.proc.wait(timeout=120)
    t.reap()
    assert t._paid_this_silence is False, "a failure must not suppress the retry"
    # it may retry, but not spin: the same backoff every other track rests under
    assert t.next_ok["payforward"] > time.time() + 19 * 60
    assert t.choose(time.time()) is None                # resting, not forgotten
    t.next_ok["payforward"] = 0.0                       # the rest is over
    t.tick(stamp)
    assert t.proc_track == "payforward"
    t.kill("test over")


def test_a_fresh_or_successful_payforward_is_not_retried_in_the_same_silence(tmp_path):
    """Exit 2 = every mouth verified fresh, exit 0 = the fleet is warm. Either way the
    silence is served and the next tick must not launch it again."""
    reg, st, cfg, j = build(tmp_path, backoff_min=20)
    old = time.time() - 3600
    os.utime(j, (old, old))
    for rc, out, paid in ((2, "", 0), (0, '{"worked": 2}', 2)):
        t = Tender(reg, st, cfg, idle_min=10)
        _only_payforward(t)
        _stub(t, rc, out)
        stamp = t.tick(0.0)
        assert t.proc_track == "payforward"
        t.proc.wait(timeout=120)
        t.reap()
        assert t._paid_this_silence is True, f"rc={rc} serves the silence"
        assert t.done["paid"] == paid           # work is counted, launches are not
        t.next_ok["payforward"] = 0.0           # even with no rest left to hide behind
        t.tick(stamp)
        assert t.proc is None, f"rc={rc} must not run twice in one silence"


def _only_warm(t):
    for track in ("drain", "distill", "tidy", "trail", "weave", "payforward"):
        t.next_ok[track] = time.time() + 9999


def test_the_warm_track_is_scheduled_when_the_index_has_moved_and_not_otherwise(tmp_path):
    """The debounce is the index hash, checked before the track is scheduled — a pour,
    a tidy or a memory written by hand all move it, and every one of them leaves the
    next human question paying a cold prefill (279 s measured 2026-09-03)."""
    from distill_kura import warm as warmmod
    reg, st, cfg, j = build(tmp_path)
    t = Tender(reg, st, cfg, idle_min=10)
    _only_warm(t)
    now = time.time()
    assert t.choose(now) == "warm"                      # never warmed: cold at start
    thinker = reg.models_for(st).thinker
    warmmod._write_state(st, {"index_hash": warmmod.index_hash(st),
                              "signature": warmmod.signature(st, thinker), "at": 0, "seconds": 1.0})
    assert t.choose(now) is None                        # this index is already warm
    st.remember("a-memory-written-by-hand", "no weave, no pour — the index still moved", "b")
    assert t.choose(now) == "warm"


def test_a_switched_off_warmer_is_never_scheduled(tmp_path):
    reg, st, cfg, j = build(tmp_path)
    cfg2 = str(tmp_path / "off.toml")
    open(cfg2, "w", encoding="utf-8").write(
        open(cfg, encoding="utf-8").read() + '\n[warm]\nenabled = false\n')
    reg2 = Registry.load(cfg2)
    t = Tender(reg2, reg2.store("m"), cfg2, idle_min=10)
    _only_warm(t)
    assert t.choose(time.time()) is None


def test_a_warming_that_failed_does_not_stop_the_watcher(tmp_path):
    """The mouth is down. That is a measurement, not a fault: the track rests like any
    other and the next tick goes on to the rest of the work."""
    reg, st, cfg, j = build(tmp_path, backoff_min=20)
    old = time.time() - 3600
    os.utime(j, (old, old))
    t = Tender(reg, st, cfg, idle_min=10)
    _only_warm(t)
    _stub(t, 1)
    stamp = t.tick(0.0)
    assert t.proc_track == "warm"
    t.proc.wait(timeout=120)
    t.reap()
    assert t.done["warmed"] == 0                        # a failure is not work
    assert t.next_ok["warm"] > time.time() + 19 * 60    # rests, does not spin
    t.tick(stamp)
    assert t.proc is None


def test_a_successful_warming_is_counted_and_remembered(tmp_path):
    reg, st, cfg, j = build(tmp_path, backoff_min=20)
    old = time.time() - 3600
    os.utime(j, (old, old))
    t = Tender(reg, st, cfg, idle_min=10)
    _only_warm(t)
    _stub(t, 0, '{"did": "warmed", "index_hash": "abc123", "signature": "abc123", "seconds": 279.0}')
    t.tick(0.0)
    t.proc.wait(timeout=120)
    t.reap()
    assert t.done["warmed"] == 1 and t._warmed_hash == "abc123"


def test_a_weave_that_wrote_nothing_does_not_pass_for_a_woven_map(tmp_path):
    """`weave` exits 2 when the index moved under it and nothing was written. That is
    a re-weave signal, not a new map — the trail and the mouths must not be sent off
    to follow it."""
    reg, st, cfg, j = build(tmp_path, backoff_min=20)
    old = time.time() - 3600
    os.utime(j, (old, old))
    t = Tender(reg, st, cfg, idle_min=10)
    _stub(t, 2)
    t.start("weave"); t.proc.wait(timeout=120); t.reap()
    assert t._woven_this_silence is False and t.done["woven"] == 0
    _stub(t, 0, '{"tokens_est": 10}')
    t.start("weave"); t.proc.wait(timeout=120); t.reap()
    assert t._woven_this_silence is True and t.done["woven"] == 1


def test_the_humans_return_lowers_every_per_silence_flag(tmp_path):
    """One table, one reset: a flag added to a track but forgotten in the reset would
    outlive its silence and the chore would never run again."""
    reg, st, cfg, j = build(tmp_path)
    old = time.time() - 3600
    os.utime(j, (old, old))
    t = Tender(reg, st, cfg, idle_min=10)
    flags = [tr.flag for tr in TRACKS.values() if tr.flag]
    assert len(flags) == 4                              # weave, trail, payforward, tidy
    for f in flags:
        setattr(t, f, True)
    stamp = t.tick(0.0)
    with open(j, "a", encoding="utf-8") as fh:
        fh.write("\n")                                  # the human types
    t.tick(stamp)
    assert not any(getattr(t, f) for f in flags)
    t.kill("test over")


def test_a_track_is_declared_in_exactly_one_place(tmp_path):
    """The bug this table prevents: knowledge of a track split across a name tuple, a
    command dict, the `done` keys and an elif ladder, so a new track (or a moved flag)
    could be right in three places and wrong in the fourth."""
    reg, st, cfg, j = build(tmp_path)
    t = Tender(reg, st, cfg)
    assert list(TRACKS) == ["drain", "distill", "weave", "trail", "payforward",
                            "warm", "tidy"]
    for name, tr in TRACKS.items():
        assert tr.name == name
        assert t._cmd(name)[-len(tr.argv):] == list(tr.argv)   # the command
        assert set(tr.tally) <= set(t.done)                    # the done keys
        assert name in t.next_ok                               # the backoff slot
        if tr.flag:
            assert getattr(t, tr.flag) is False                # the per-silence flag
    assert not hasattr(tendmod, "heartbeat"), "the dead wrapper: doctor reads tend_state"


def test_child_tracks_carry_the_config_the_registry_resolved_not_the_bare_flag(tmp_path, monkeypatch):
    """A watcher started without -c (systemd sets $KURA_CONFIG) used to spawn its
    children with no -c at all, leaving each child to re-resolve the config under
    whatever candidates exist at that moment. Pin them to what the parent loaded."""
    reg0, st0, cfg, j = build(tmp_path)
    monkeypatch.setenv("KURA_CONFIG", cfg)
    monkeypatch.chdir(tmp_path)
    reg = Registry.load(None)
    assert reg.config_path == cfg
    seen = {}

    class Spy(Tender):
        def __init__(self, reg, store, config_path, **kw):
            seen["config_path"] = config_path
            super().__init__(reg, store, config_path, **kw)

    monkeypatch.setattr(tendmod, "Tender", Spy)
    from distill_kura import cli
    cli.main(["-s", "m", "tend", "--once", "--idle-min", "999999"])
    assert seen["config_path"] == cfg                       # not None: a.config was unset
    t = Tender(reg, reg.store("m"), seen["config_path"])
    assert t._cmd("tidy")[3:5] == ["-c", cfg]


def test_a_fresh_heartbeat_from_a_dead_pid_is_not_alive_and_says_which_death(tmp_path):
    """The other way a watcher dies: the heartbeat is seconds old and the process is
    gone. Only the ageing-out death was tested, and `why` used to report the clock —
    "last heartbeat 0 s ago" — which sends the reader looking in the wrong place."""
    reg, st, cfg, j = build(tmp_path)
    Tender(reg, st, cfg, idle_min=10).tick(0.0)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    dead = proc.pid                      # reaped: the pid is gone, not a zombie
    p = os.path.join(st.still, "tend.json")
    d = json.load(open(p)); d["at"] = time.time(); d["pid"] = dead
    json.dump(d, open(p, "w"))
    state = st.tend_state()
    assert state["alive"] is False and state["age_s"] == 0
    assert f"pid {dead} is gone" in state["why"]


def _quiet(tmp_path, **kw):
    reg, st, cfg, j = build(tmp_path, **kw)
    old = time.time() - 3600
    os.utime(j, (old, old))                                 # quiet for an hour
    return reg, st, cfg, j


def _sleeper(t, seconds=60):
    t._cmd = lambda track: [sys.executable, "-c",           # type: ignore
                            f"import time; time.sleep({seconds})"]


def test_once_that_times_out_exits_one_with_a_retryable_record(tmp_path):
    """The failure this prevents: a `--once` whose deadline passed while the track was
    still running exited 0. A scheduler reads "started something → deadline expired →
    exit 0" as completed and never retries, so the work is dropped for good."""
    reg, st, cfg, j = _quiet(tmp_path)
    t = Tender(reg, st, cfg, idle_min=10)
    _sleeper(t)
    r = t.run_once(timeout_s=0.3, poll_s=0.02)
    assert r["code"] == 1
    assert r["ok"] is False and r["error"] == "timeout" and r["retryable"] is True
    assert r["track"] == "distill"
    log = open(os.path.join(st.still, "tend.log"), encoding="utf-8").read()
    assert '"error": "timeout"' in log and '"retryable": true' in log


def test_once_with_nothing_to_do_is_two_and_a_completed_track_is_zero(tmp_path):
    reg, st, cfg, j = _quiet(tmp_path)
    t = Tender(reg, st, cfg, idle_min=10)
    _stub(t, 2)
    assert t.run_once(timeout_s=120)["code"] == 2            # honestly nothing to do
    t2 = Tender(reg, st, cfg, idle_min=10)
    _stub(t2, 0, '{"drafts": []}')
    r = t2.run_once(timeout_s=120)
    assert r["code"] == 0 and r["ok"] is True and r["error"] is None


def test_the_five_once_outcomes_stay_five(tmp_path):
    """nothing-to-do, a track that failed, a yield because the human returned, a child
    that died badly, and a timeout are five different things. Collapsed into one
    "didn't work", a scheduler cannot tell a retry from a rest from a bug."""
    reg, st, cfg, j = _quiet(tmp_path)
    seen = {}

    t = Tender(reg, st, cfg, idle_min=10); _stub(t, 2)
    seen["nothing"] = t.run_once(timeout_s=120)

    t = Tender(reg, st, cfg, idle_min=10); _stub(t, 1)
    seen["failed"] = t.run_once(timeout_s=120)

    t = Tender(reg, st, cfg, idle_min=10)
    t._cmd = lambda track: [sys.executable, "-c",            # type: ignore
                            "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"]
    seen["child"] = t.run_once(timeout_s=120)

    t = Tender(reg, st, cfg, idle_min=10); _sleeper(t)
    seen["timeout"] = t.run_once(timeout_s=0.3, poll_s=0.02)

    t = Tender(reg, st, cfg, idle_min=10); _sleeper(t)
    real, calls = t.newest_mtime, []

    def moved():                                             # the human types
        calls.append(1)
        return real() + (0 if len(calls) == 1 else 1)
    t.newest_mtime = moved                                   # type: ignore
    seen["yield"] = t.run_once(timeout_s=120, poll_s=0.02)

    assert seen["nothing"]["code"] == 2
    assert all(seen[k]["code"] == 1 for k in ("failed", "child", "timeout", "yield"))
    marks = [(r.get("error"), r.get("reason")) for r in seen.values()]
    assert len(set(marks)) == 5, marks
    assert seen["failed"]["error"] == "track-failed"
    assert seen["child"]["error"] == "child-error"
    assert seen["timeout"]["error"] == "timeout"
    assert seen["yield"]["error"] == "yielded"
    assert seen["nothing"]["error"] is None and seen["nothing"]["reason"] == "nothing-to-do"


def test_once_leaves_no_child_running_when_it_returns(tmp_path):
    """A `--once` that returns while its track is still alive leaves an orphan holding
    the GPU seat the conversation needs — and the next run finds it there."""
    reg, st, cfg, j = _quiet(tmp_path)
    t = Tender(reg, st, cfg, idle_min=10)
    _sleeper(t, 300)
    r = t.run_once(timeout_s=0.3, poll_s=0.02)
    assert r["error"] == "timeout" and t.proc is None
    dead = r["pid"]
    assert dead
    try:
        os.kill(dead, 0)                    # really gone, not merely signalled
        raise AssertionError(f"pid {dead} is still running after --once returned")
    except ProcessLookupError:
        pass
