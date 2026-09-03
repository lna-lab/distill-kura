"""`kura tend` — the watcher that keeps a store in the house's quiet hours.

What it does, in order, every time the house has been quiet for `idle_min`:

    drain      drafts waiting → the editor reads each one cold and pours / fixes / tosses
    distill    no drafts     → one pass over the journal: sip → spot → gate → stage
    weave      something poured this silence → re-weave the resident map, once
    trail      after each weave → rebuild the Hot Trail (model-free, cheap); also
               whenever the trail on disk has passed its own time horizon — the
               fresh window slides with no store write, and a watcher that never
               rebuilt the trail would leave "the current path" empty forever
    payforward after each weave → pay the map's cold prefill into the registered
               mouths (`kura pay-forward`); an unchanged map is a cheap check, exit 2
    warm       whenever the store's index has moved since the last warming — after a
               pour, a weave, a tidy, or a memory a person wrote by hand — send the
               thinker the exact prompt recall sends, so the next question does not
               pay the cold prefill (279 s measured, `warm.py`)
    tidy       once per silence, only if the index has mechanically ragged lines

"Quiet" is the simplest signal there is: the newest journal file's mtime. No model is
asked whether the human is busy; a conversation file that has not changed in ten
minutes is a person who is not typing. That is what the house's first watcher used
for five days (2026-08-07 → 08-11, `nemuri.py`), and it was enough.

Lessons from that watcher, written into this one:

- **"Nothing to do" is exit code 2**, not success. A track that returns 2 is put to
  sleep for `backoff_min`, so an empty journal does not spin every fifteen seconds
  and starve the other tracks — that spin once ran `tidy` 3,122 times in a night and
  did real work three times.
- **Count work, never launches.** The summary says what was poured, tossed, fixed
  and drafted. "495 passes" was a launch counter and it was mistaken for output.
- **Keep every decision.** Track output goes to `_still/tend.log`, never to
  /dev/null — the brain's stdout was discarded once and the candidate counts of
  five days can no longer be recovered.
- **Yield when the human returns** (`yield_on_return`, default on): a running track
  is terminated the moment the journal changes, because the editor is usually the
  same GPU the conversation needs. When the editor is a separate model that does not
  compete for the same seat — a CPU model, another machine — set it off, so a verdict
  in flight is not thrown away.
- **Be easy to watch.** A heartbeat in `_still/tend.json` every tick; `doctor`
  reads it and says whether the watcher is alive. The first watcher died with the
  machine and nobody noticed for twelve days.
- **A once-per-silence chore is settled by its outcome, not by its launch.** The
  flag that says "this was done in this quiet" belongs where the exit code is read
  (`Track.raise_on`); raised at launch instead, a pay-forward that failed or lost the
  lock was never retried and the mouths stayed cold for the whole silence.

The watcher spawns `kura` subcommands as subprocesses rather than calling in-process,
so a track can be killed cleanly and its exit code means what the CLI says it means.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from .distill.sources import discover_all
from .registry import Registry
from .store import Store


@dataclass(frozen=True)
class Track:
    """Everything the watcher needs to know about one track, in one place.

    A track used to be spelled out in four: the name tuple, the command dict, the
    `done` keys, and an elif ladder in `reap`. Adding one meant remembering all four,
    and the fourth — when the per-silence flag goes up — was the one that got it
    wrong (see `raise_on` on payforward).
    """
    name: str
    argv: tuple[str, ...]                        # the `kura` subcommand to spawn
    tally: dict[str, Callable[[dict], int]] = field(default_factory=dict)
    #                                            what a finished run adds to `done`
    flag: str = ""                               # the per-silence flag it raises; "" = every tick
    raise_on: tuple[int, ...] | None = None
    # When the flag goes up. None = at launch (the attempt itself is the chore).
    # Otherwise the exit codes that mean "this silence is served" — every other code
    # leaves the flag down, so the next tick may try again after the backoff.
    after: Callable[["Tender", dict], None] | None = None


def _count(f: str) -> Callable[[dict], int]:
    return lambda r: int(r.get(f) or 0)


def _many(f: str) -> Callable[[dict], int]:
    return lambda r: len(r.get(f) or [])


def _pour_unsettles_the_map(t: "Tender", r: dict) -> None:
    if r.get("poured"):
        t._woven_this_silence = False            # the map has something new to say


def _warm_records_the_hash(t: "Tender", r: dict) -> None:
    """Remember in memory what `warm.py` wrote to disk, so a tick does not have to read
    the state file back to know it just warmed this index."""
    if r.get("did") == "warmed":
        t._warmed_hash = str(r.get("index_hash") or "")


def _weave_moves_the_map(t: "Tender", r: dict) -> None:
    t._trailed_this_silence = False              # the map moved: the trail must follow
    t._paid_this_silence = False                 # a fresh weave may have changed the map


TRACKS: dict[str, Track] = {t.name: t for t in (
    Track("drain", ("distill", "drain"),
          tally={"poured": _count("poured"), "tossed": _count("tossed"),
                 "fixed": _count("fixed")},
          after=_pour_unsettles_the_map),
    Track("distill", ("distill", "run", "--chunks", "1"),
          tally={"drafts": _many("drafts")}),
    Track("weave", ("weave",), tally={"woven": lambda r: 1},
          flag="_woven_this_silence", raise_on=(0,),
          # NOT on exit 2: that is "the index moved while weaving, nothing was
          # written". Counting it as woven would send the trail and the mouths off
          # to follow a map that never changed.
          after=_weave_moves_the_map),
    Track("trail", ("trail",), tally={"trailed": lambda r: 1 if r.get("written") else 0},
          flag="_trailed_this_silence"),
    Track("payforward", ("pay-forward",), tally={"paid": _count("worked")},
          flag="_paid_this_silence",
          # 0 = the fleet is warm, 2 = every mouth verified fresh. Exit 1 is a mouth
          # that failed or was locked out by another runner, and raising the flag on
          # it — which is what launching used to do — left the map cold for the rest
          # of the silence: no retry until the next weave or the next human return,
          # exactly when the mouth needed warming. The backoff in `reap` is what
          # keeps that retry from spinning.
          raise_on=(0, 2)),
    Track("warm", ("warm",), tally={"warmed": lambda r: 1 if r.get("did") == "warmed" else 0},
          # No per-silence flag and no `raise_on`: the debounce is the index hash, and
          # it is checked in `choose` (below) BEFORE the track is scheduled. A flag
          # would say "warmed in this quiet" — but the thing that goes cold is an
          # index that moved, and it can move twice in one silence.
          after=_warm_records_the_hash),
    Track("tidy", ("distill", "tidy"), tally={"tidied": _count("fixed")},
          flag="_tidied_this_silence"),
)}


def _log(path: str, s: str) -> None:
    line = f"{datetime.now().strftime('%m-%d %H:%M:%S')} {s}"
    print(line, flush=True)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


class Tender:
    def __init__(self, reg: Registry, store: Store, config_path: str | None,
                 idle_min: float | None = None, poll_s: float = 15.0,
                 backoff_min: float | None = None, yield_on_return: bool | None = None):
        self.reg, self.store, self.config_path = reg, store, config_path
        merged = reg.distill_cfg_for(store)   # `scfg.get(k, cfg.get(k, d))`, one rule
        pick = merged.get
        self.idle_s = float(idle_min if idle_min is not None else pick("idle_min", 10)) * 60
        self.backoff_s = float(backoff_min if backoff_min is not None else pick("backoff_min", 20)) * 60
        self.yield_on_return = bool(yield_on_return if yield_on_return is not None
                                    else pick("yield_on_return", True))
        self.poll_s = poll_s
        # Journals are the store's own — the same roots the distiller drinks from, so
        # the watcher and the distiller agree on what "the conversation" is.
        from .distill import Distiller
        self.journals = Distiller(reg, store).journals
        self.exclude = [st.path for st in reg.stores.values()]
        os.makedirs(store.still, exist_ok=True)
        self.log_path = os.path.join(store.still, "tend.log")
        self.beat_path = os.path.join(store.still, "tend.json")
        self.next_ok: dict[str, float] = {t: 0.0 for t in TRACKS}
        self.proc: subprocess.Popen | None = None
        self.proc_track = ""
        self.done = {"poured": 0, "tossed": 0, "fixed": 0, "drafts": 0, "woven": 0,
                     "trailed": 0, "paid": 0, "warmed": 0, "tidied": 0}
        self._warmed_hash = ""            # the index this process last warmed the thinker with
        self.new_silence()

    def new_silence(self) -> None:
        """Every per-silence flag comes down, from the one table — a flag that is
        raised in `tick` but forgotten here would outlive the silence it belongs to."""
        for tr in TRACKS.values():
            if tr.flag:
                setattr(self, tr.flag, False)

    # ── the signal ────────────────────────────────────────────────────────
    def newest_mtime(self) -> float:
        fs = discover_all(self.journals, exclude_roots=self.exclude) if self.journals else []
        best = 0.0
        for f in fs:
            try:
                best = max(best, os.path.getmtime(f))
            except OSError:
                continue
        return best

    # ── running a track ───────────────────────────────────────────────────
    def _cmd(self, track: str) -> list[str]:
        base = [sys.executable, "-m", "distill_kura.cli"]
        if self.config_path:
            base += ["-c", self.config_path]
        base += ["-s", self.store.name]
        return base + list(TRACKS[track].argv)

    def start(self, track: str) -> None:
        _log(self.log_path, f"→ {track}")
        # Output goes to a file, not a pipe: a pass that prints more than the pipe
        # buffer holds would block on write and the watcher would wait on it forever.
        self._out_path = os.path.join(self.store.still, f"tend.{track}.out")
        out = open(self._out_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(self._cmd(track), stdout=out, stderr=subprocess.STDOUT, text=True)
        out.close()
        self.proc_track = track

    def reap(self) -> bool:
        """Collect a finished track. Returns True when one was collected."""
        if not self.proc or self.proc.poll() is None:
            return False
        try:
            out = open(self._out_path, encoding="utf-8", errors="ignore").read()
        except OSError:
            out = ""
        rc = self.proc.returncode
        track, self.proc, self.proc_track = self.proc_track, None, ""
        tr = TRACKS[track]
        for line in out.strip().splitlines():
            _log(self.log_path, f"   {line[:400]}")
        if tr.raise_on is not None and rc in tr.raise_on:
            # Raised here and not at launch: only an outcome the track declares as
            # "served" may suppress the next attempt in this silence.
            setattr(self, tr.flag, True)
        if rc == 2:
            self.next_ok[track] = time.time() + self.backoff_s
            _log(self.log_path, f"· {track}: nothing to do — resting {int(self.backoff_s / 60)} min")
            return True
        if rc != 0:
            self.next_ok[track] = time.time() + self.backoff_s
            _log(self.log_path, f"✗ {track} failed (rc={rc}) — resting {int(self.backoff_s / 60)} min")
            return True
        last = next((l for l in reversed(out.strip().splitlines()) if l.startswith("{")), "")
        try:
            r = json.loads(last) if last else {}
        except ValueError:
            r = {}
        # Work is counted, launches are not: `paid` is bakes + restores, and a run of
        # skipped-fresh mouths exits 2 above and never reaches this line.
        for key, take in tr.tally.items():
            self.done[key] += take(r)
        if tr.after:
            tr.after(self, r)
        return True

    def kill(self, why: str) -> None:
        if self.proc and self.proc.poll() is None:
            _log(self.log_path, f"⏹ {self.proc_track} stopped — {why}")
            try:
                self.proc.send_signal(signal.SIGTERM)
                self.proc.wait(timeout=20)
            except Exception:
                self.proc.kill()
            self.proc = None
            self.proc_track = ""

    def _stop_child(self, why: str, grace: float = 5.0) -> None:
        """Stop the running track and leave NO process behind: terminate, a short
        grace, then kill — and wait after each, so the pid is really gone (and reaped)
        by the time this returns. `kill` above is the watcher's long-grace version;
        a `--once` run that is about to exit cannot afford to wait twenty seconds and
        must not hand a live child back to the scheduler."""
        p = self.proc
        self.proc, self.proc_track = None, ""
        if p is None:
            return
        if p.poll() is None:
            _log(self.log_path, f"⏹ stopped — {why}")
            for stop in (p.terminate, p.kill):
                try:
                    stop()
                except Exception:
                    pass
                try:
                    p.wait(timeout=grace)
                    return
                except Exception:
                    continue
        try:
            p.wait(timeout=grace)
        except Exception:
            pass

    # ── one shot, for a scheduler ─────────────────────────────────────────
    def run_once(self, timeout_s: float = 3600.0, poll_s: float = 0.05) -> dict:
        """One tick, wait for whatever it started, and say — honestly — what happened.

        The exit code a scheduler reads is settled here, and the five ways a `--once`
        run can end stay five: nothing-to-do (2), the track failed (1), the human came
        back (1), the child died badly (1), the deadline passed with the track still
        running (1). The last one used to exit 0 — "started something, deadline
        expired, exit 0" reads as completed, so a scheduler never retried and the work
        was silently dropped for good.
        """
        stamp = self.tick(0.0)
        self._once_stamp = stamp
        if not self.proc:
            return {"ok": True, "code": 2, "error": None, "reason": "nothing-to-do",
                    "track": "", "pid": None, "retryable": False}
        track, pid = self.proc_track, self.proc.pid
        deadline = time.time() + float(timeout_s)
        while self.proc.poll() is None:
            if time.time() >= deadline:
                self._stop_child(f"--once deadline passed after {int(timeout_s)}s")
                return self._once_record(
                    {"ok": False, "code": 1, "error": "timeout",
                     "reason": f"still running after {timeout_s:g}s",
                     "track": track, "pid": pid, "retryable": True})
            if self.yield_on_return and stamp and self.newest_mtime() != stamp:
                self._stop_child("the journal changed: the human is back")
                return self._once_record(
                    {"ok": False, "code": 1, "error": "yielded",
                     "reason": "the human returned", "track": track,
                     "pid": pid, "retryable": True})
            time.sleep(poll_s)
        rc = self.proc.returncode
        self.reap()
        if rc == 0:
            return self._once_record({"ok": True, "code": 0, "error": None,
                                      "reason": "done", "track": track, "pid": pid,
                                      "retryable": False})
        if rc == 2:
            return self._once_record({"ok": True, "code": 2, "error": None,
                                      "reason": "nothing-to-do", "track": track, "pid": pid,
                                      "retryable": False})
        if rc == 1:
            # The track ran and reported failure — a model that would not answer, a
            # mouth that was down, a lock another runner held. Retryable, and NOT the
            # same thing as the child dying under us.
            return self._once_record({"ok": False, "code": 1, "error": "track-failed",
                                      "reason": "the track reported failure (rc=1)",
                                      "track": track, "pid": pid, "retryable": True})
        return self._once_record({"ok": False, "code": 1, "error": "child-error",
                                  "reason": f"child ended with rc={rc}",
                                  "track": track, "pid": pid, "retryable": True})

    def _once_record(self, r: dict) -> dict:
        _log(self.log_path, ("· " if r["ok"] else "✗ ") + "once " +
             json.dumps(r, ensure_ascii=False))
        return r

    # ── choosing the next track ───────────────────────────────────────────
    def _trail_time_stale(self) -> bool:
        """Does a trail that EXISTS need rebuilding for time alone? Cheap and
        model-free: read the sidecar, hash the index — the trail's own proof."""
        from .prefill import loom_for, trail_for
        cfg = self.reg.prefill_cfg_for(self.store)
        t = trail_for(self.store, cfg, loom=loom_for(self.store, cfg))
        return t.text_on_disk() is not None and t.is_stale()

    def _thinker_is_cold(self) -> bool:
        """Has the index moved since the thinker was last warmed with it?

        Cheap and model-free — a hash of the prompt recall would send, compared with
        `_still/warm.json` (so a restart does not re-pay a prefill the mouth still
        holds) and with this process's own last warming. Deliberately polled on the
        watcher's existing tick rather than watched with inotify: a memory a person
        writes by hand is picked up at the next tick, and the watcher keeps one signal
        and one clock.
        """
        from . import warm as warmmod
        if not self.reg.warm_cfg_for(self.store).get("enabled", True):
            return False
        try:
            now = warmmod.index_hash(self.store)
        except OSError:
            return False                  # an unreadable store is not a cold mouth
        return now not in (self._warmed_hash, warmmod.read_state(self.store).get("index_hash"))

    def choose(self, now: float) -> str | None:
        drafts = os.path.join(self.store.still, "drafts")
        have_drafts = any(f.endswith(".md") for f in os.listdir(drafts)) if os.path.isdir(drafts) else False
        order = (["drain"] if have_drafts else ["distill"])
        if not self._woven_this_silence and self.done["poured"]:
            order.append("weave")
        if self._woven_this_silence and not self._trailed_this_silence:
            # Right after the weave, before pay-forward: the trail is model-free and
            # takes milliseconds, and a pour has retired the old one (the revision
            # moved). Without this track the "current path" would go absent the first
            # time the watcher poured something and never come back.
            order.append("trail")
        elif self._trail_time_stale():
            # The pure-time hazard: no store write at all, the fresh window simply
            # slid past the trail's own horizon. Absent trails are not maintained
            # here — the after-weave path is what creates one.
            order.append("trail")
        if self._woven_this_silence and not self._paid_this_silence:
            # Only after a weave: a map that was not re-woven cannot have changed, and
            # a mouth restart is the systemd hook's job (docs/OPERATING.md), not the
            # watcher's. When the weave changed nothing this is a cheap check → exit 2.
            order.append("payforward")
        if self._thinker_is_cold():
            # After the map's own chores, and NOT gated on a weave: the index changes
            # when a memory is poured, tidied, or written by hand, and every one of
            # those leaves the next human question paying a cold prefill.
            order.append("warm")
        if not self._tidied_this_silence:
            order.append("tidy")
        for t in order:
            if now >= self.next_ok[t]:
                return t
        return None

    # ── heartbeat, so doctor can say whether the watcher is alive ─────────
    def beat(self, idle: float, stamp: float) -> None:
        try:
            tmp = self.beat_path + f".tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "at": int(time.time()), "idle_s": int(idle),
                           "journal_mtime": int(stamp), "running": self.proc_track,
                           "next_ok": {k: int(v) for k, v in self.next_ok.items()},
                           "done": self.done}, f)
            os.replace(tmp, self.beat_path)
        except OSError:
            pass

    # ── one tick ──────────────────────────────────────────────────────────
    def tick(self, stamp_seen: float) -> float:
        """Returns the journal mtime as of this tick."""
        now = time.time()
        stamp = self.newest_mtime()
        self.reap()
        if stamp != stamp_seen and stamp_seen:
            # The human is back. Say what was done — work, not launches — and reset.
            if self.yield_on_return:
                self.kill("the journal changed: the human is back")
            if any(self.done.values()):
                _log(self.log_path, "the human is back: " + ", ".join(f"{k} {v}" for k, v in self.done.items() if v))
            self.done = {k: 0 for k in self.done}
            self.new_silence()
        idle = now - stamp if stamp else 0.0
        if stamp and idle >= self.idle_s and not self.proc:
            t = self.choose(now)
            if t:
                tr = TRACKS[t]
                if tr.flag and tr.raise_on is None:
                    setattr(self, tr.flag, True)   # a chore the attempt itself settles
                self.start(t)
        self.beat(idle, stamp)
        return stamp

    def watch(self) -> None:
        _log(self.log_path, f"tending '{self.store.name}': quiet after {int(self.idle_s / 60)} min, "
                            f"rest {int(self.backoff_s / 60)} min on nothing-to-do, "
                            f"yield_on_return={'on' if self.yield_on_return else 'off'}, "
                            f"journals={list(self.journals) or 'NONE'}")
        if not self.journals:
            _log(self.log_path, "⚠ this store has no journal roots: nothing will ever be quiet or busy. "
                                "Bind [stores.<name>.distill.journals] first.")
        stamp = 0.0
        try:
            while True:
                stamp = self.tick(stamp)
                time.sleep(self.poll_s)
        except KeyboardInterrupt:
            self.kill("watcher stopped")
