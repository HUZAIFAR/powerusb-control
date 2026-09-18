"""
PowerUSB control server: web GUI + JSON API over HTTP, plus a raw TCP port.

The server is the single owner of the USB handle (the strip only permits one
open handle), so everything else -- the GUI, the phone, the CLI -- goes
through here.

Surviving the wall switch
-------------------------
This strip is plugged into a switched wall outlet, so its controller loses
power along with the sockets. Two mechanisms keep that seamless:

  1. Every change is also written to the strip's EEPROM power-up defaults.
     When mains returns, the strip restores itself from firmware, with no
     help from this PC -- it works even if the machine is asleep or booting.

  2. A monitor thread watches USB enumeration. The strip disappearing is
     taken as "mains went off"; reappearing is taken as "mains came back",
     at which point the desired state is re-applied to be certain the
     hardware agrees with what the user last asked for.

Commands that arrive while the strip is dark are not errors. They update the
desired state, are persisted, and take effect the moment power returns.
"""

from __future__ import annotations

import errno
import hashlib
import json
import socket
import socketserver
import threading
import urllib.request
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

from .config import ROOT, load_config, save_names, save_watts
from .device import PowerUSB, PowerUSBError, SOCKET_COUNT, is_present
from .away import AwayMode, AwayError
from .events import EventLog
from .schedule import Scheduler, ScheduleError

WEB_DIR = ROOT / "web"
STATE_PATH = ROOT / "state.json"
SCHEDULE_PATH = ROOT / "schedules.json"
EVENTS_PATH = ROOT / "events.jsonl"
AWAY_PATH = ROOT / "away.json"

# How long a live hardware read stays fresh. Several clients polling at once
# should not turn into a storm of USB traffic.
_LIVE_TTL = 0.75

# USB enumeration fires before the firmware is necessarily ready to talk.
_BOOT_GRACE = 1.0
_MONITOR_INTERVAL = 1.0

MAX_NAME_LEN = 24

# Filled in by main() once the ports are known.
LAN_URL = ""
PUBLIC_URL = ""


_build = {"mtime": None, "id": ""}


def app_build() -> str:
    """
    Short fingerprint of the web UI currently on disk.

    The page records the build it loaded with and compares it against this on
    every poll, so an already-open app (or an installed home-screen PWA, which
    otherwise keeps its original page indefinitely) can notice it is stale and
    offer to reload itself. Cached on mtime so polling costs one stat().
    """
    try:
        path = WEB_DIR / "index.html"
        mtime = path.stat().st_mtime_ns
        if _build["mtime"] != mtime:
            _build["id"] = hashlib.sha1(path.read_bytes()).hexdigest()[:8]
            _build["mtime"] = mtime
    except OSError:
        pass
    return _build["id"]


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


LOG_PATH = ROOT / "server.log"
_LOG_MAX = 512 * 1024          # rotate past this, keeping one previous file
_log_lock = threading.Lock()


def log(msg: str) -> None:
    """
    Print and also append to server.log.

    Autostart runs the server windowless, so stdout goes nowhere -- the file
    is the only way to see what happened after an unattended restart.
    """
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with _log_lock:
        try:
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > _LOG_MAX:
                LOG_PATH.replace(LOG_PATH.with_suffix(".log.1"))
            with LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass                # logging must never take the server down


class StripController:
    """
    Owns the strip, the desired state, and the mains-power monitor.

    `desired` is authoritative and survives restarts; the hardware is made to
    match it whenever the hardware is actually reachable.
    """

    def __init__(self, names: List[str], events: Optional[EventLog] = None,
                 watts: Optional[List[float]] = None):
        self.names = list(names)
        self.events = events or EventLog(EVENTS_PATH)
        self.watts = list(watts or [0.0] * SOCKET_COUNT)
        self.lock = threading.RLock()
        self.desired: List[bool] = self._load_desired()
        self.online: bool = False
        self.last_change: Optional[str] = None
        self.last_power_event: Optional[str] = None
        self.power_events = 0
        # The first time we reach the strip is just startup, not the wall
        # switch being flipped -- logging it as a mains event would be a lie.
        self._first_connect = True

        # When each socket was last observed to change, for the "on for 12m"
        # readout in the GUI.
        self.since: List[Optional[str]] = [None] * SOCKET_COUNT
        self._prev: Optional[List[bool]] = None

        self._dev: Optional[PowerUSB] = None
        self._live: Optional[List[bool]] = None
        self._live_at = 0.0
        self._stop = threading.Event()
        self._monitor = threading.Thread(
            target=self._monitor_loop, name="mains-monitor", daemon=True
        )

    # ------------------------------------------------------------- persistence

    def _load_desired(self) -> List[bool]:
        if STATE_PATH.exists():
            try:
                data = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
                want = data.get("desired")
                if isinstance(want, list) and len(want) == SOCKET_COUNT:
                    return [bool(x) for x in want]
            except (OSError, json.JSONDecodeError, TypeError):
                log("state.json unreadable; starting from all-off")
        return [False] * SOCKET_COUNT

    def _save_desired(self) -> None:
        try:
            tmp = STATE_PATH.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({"desired": self.desired, "saved": _now()}, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp.replace(STATE_PATH)  # atomic: a crash cannot leave it truncated
        except OSError as exc:
            log(f"could not persist state.json: {exc}")

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        # Adopt the hardware's current state on first run rather than stamping
        # a stale file over sockets that are already doing something.
        if not STATE_PATH.exists() and is_present():
            try:
                with self.lock:
                    self._connect()
                    if self._dev is not None:
                        self.desired = self._dev.states()
                        self.online = True
                        self._first_connect = False
                        self._note_states(self.desired)
                        log(f"adopted current strip state: {self._fmt(self.desired)}")
                        self._save_desired()
                        # Make the EEPROM defaults agree straight away, so the
                        # very first wall-switch cycle already behaves.
                        for i in range(1, SOCKET_COUNT + 1):
                            self._dev.set_default(i, self.desired[i - 1])
            except PowerUSBError as exc:
                log(f"could not read strip at startup: {exc}")
                with self.lock:
                    self._drop()
                    self.online = False
        self._monitor.start()

    def stop(self) -> None:
        self._stop.set()
        with self.lock:
            self._drop()

    def _connect(self) -> None:
        if self._dev is None:
            self._dev = PowerUSB(auto_open=False)
        self._dev.open()

    def _drop(self) -> None:
        if self._dev is not None:
            self._dev.close()
        self._dev = None
        self._live = None

    @staticmethod
    def _fmt(states: List[bool]) -> str:
        return " ".join("on" if s else "off" for s in states)

    def _note_states(self, states: List[bool]) -> None:
        """Track per-socket change times so the GUI can show how long it's been on."""
        stamp = _now()
        if self._prev is None:
            self._prev = list(states)
            self.since = [stamp] * SOCKET_COUNT
            return
        for i, value in enumerate(states):
            if value != self._prev[i]:
                self.since[i] = stamp
        self._prev = list(states)

    # --------------------------------------------------------- mains monitor

    def poll_once(self, boot_grace: float = _BOOT_GRACE) -> None:
        """
        One mains-presence check, handling either transition.

        Split out from the loop so tests can drive power cycles directly
        instead of waiting on wall-clock timing.
        """
        present = is_present()
        with self.lock:
            online = self.online

        if present and not online:
            # Let the firmware finish booting BEFORE taking the lock. Sleeping
            # while holding it would stall every HTTP request for a second on
            # each power-up.
            if boot_grace and self._stop.wait(boot_grace):
                return
            with self.lock:
                if not self.online:
                    self._on_power_restored()
        elif not present and online:
            with self.lock:
                if self.online:
                    self._on_power_lost()

    def _monitor_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # a monitor thread must never die
                log(f"monitor error: {exc}")
            self._stop.wait(_MONITOR_INTERVAL)

    def _on_power_lost(self) -> None:
        self.online = False
        self.last_power_event = _now()
        self.power_events += 1
        self._drop()
        log("strip went away -- wall switch off (desired state kept, will re-apply)")
        self.events.add("mains", "Mains power lost - wall switch off", on=False)

    def _on_power_restored(self) -> None:
        try:
            self._connect()
        except PowerUSBError as exc:
            log(f"strip enumerated but will not open yet ({exc}); retrying")
            self._drop()
            return
        self.online = True
        if self._first_connect:
            self._first_connect = False
            log(f"connected to strip -- applying saved state: {self._fmt(self.desired)}")
            self.events.add("system", "Connected to strip")
            self._apply_all(reason="startup")
            return
        self.last_power_event = _now()
        self.power_events += 1
        log(f"strip is back -- re-applying desired state: {self._fmt(self.desired)}")
        self.events.add("mains", "Mains power restored", on=True)
        self._apply_all(reason="power restored")

    def _apply_all(self, reason: str) -> None:
        """Force hardware and EEPROM defaults to match the desired state."""
        if self._dev is None:
            return
        try:
            for i in range(1, SOCKET_COUNT + 1):
                want = self.desired[i - 1]
                if self._dev.get(i) != want:
                    self._dev.set(i, want)
                self._dev.set_default(i, want)
            self._live = self._dev.states()
            self._live_at = time.monotonic()
            self._note_states(self._live)
            log(f"strip reconciled ({reason}): {self._fmt(self._live)}")
        except PowerUSBError as exc:
            log(f"reconcile failed ({reason}): {exc}")
            self._drop()
            self.online = False

    # ------------------------------------------------------------------- api

    def live_states(self, force: bool = False) -> Optional[List[bool]]:
        """Actual hardware states, cached briefly. None when the strip is dark."""
        with self.lock:
            if not self.online or self._dev is None:
                return None
            fresh = (time.monotonic() - self._live_at) < _LIVE_TTL
            if self._live is not None and fresh and not force:
                return list(self._live)
            try:
                self._live = self._dev.states()
                self._live_at = time.monotonic()
                self._note_states(self._live)
                return list(self._live)
            except PowerUSBError as exc:
                log(f"live read failed: {exc}")
                self._drop()
                self.online = False
                return None

    def _switch(self, socket_no: int, on: bool) -> Optional[str]:
        """
        Apply one socket to the hardware. Returns an error string, or None.

        Assumes the caller holds the lock and has already recorded intent.
        """
        if not self.online or self._dev is None:
            log(
                f"socket {socket_no} -> {'on' if on else 'off'} "
                "(queued; strip has no mains power)"
            )
            return None
        try:
            result = self._dev.set(socket_no, on)
            # Keep the power-up default in step so the strip restores itself
            # correctly next time the wall switch is flipped.
            self._dev.set_default(socket_no, on)
            # set() already read this socket back, and the other two cannot
            # have changed, so re-reading all three here was three wasted USB
            # round trips on the critical path of every tap and Siri command.
            if self._live is None:
                self._live = self._dev.states()
            else:
                self._live[socket_no - 1] = result
            self._live_at = time.monotonic()
            self._note_states(self._live)
            return None
        except PowerUSBError as exc:
            log(f"socket {socket_no} switch failed: {exc}")
            self._drop()
            self.online = False
            return str(exc)

    def set_socket(self, socket_no: int, on: bool, source: str = "app") -> Dict[str, Any]:
        """Set one socket. Queues the intent if the strip is currently dark."""
        if not 1 <= socket_no <= SOCKET_COUNT:
            raise PowerUSBError(f"socket must be 1..{SOCKET_COUNT}, got {socket_no}")
        with self.lock:
            changed = self.desired[socket_no - 1] != on
            self.desired[socket_no - 1] = on
            self._save_desired()
            self.last_change = _now()
            err = self._switch(socket_no, on)
            if changed:
                self.events.add(
                    "socket",
                    f"{self.names[socket_no - 1]} turned {'on' if on else 'off'}"
                    + ("" if self.online else " (queued - no mains power)"),
                    socket=socket_no, on=on, source=source,
                )
            return self.snapshot(queued=not self.online, error=err)

    def toggle_socket(self, socket_no: int, source: str = "app") -> Dict[str, Any]:
        # Validate BEFORE indexing: every other mutator guards, and without
        # this a bad socket number raises IndexError instead of a clean 400.
        if not 1 <= socket_no <= SOCKET_COUNT:
            raise PowerUSBError(f"socket must be 1..{SOCKET_COUNT}, got {socket_no}")
        with self.lock:
            live = self.live_states()
            current = live[socket_no - 1] if live else self.desired[socket_no - 1]
            return self.set_socket(socket_no, not current, source=source)

    def set_many(self, sockets: List[int], on: bool,
                 source: str = "app") -> Dict[str, Any]:
        """
        Switch a group of sockets together.

        One state save and one snapshot for the whole group rather than per
        socket, which is what makes a scene link ("light + monitor on") as
        quick as a single one.
        """
        wanted: List[int] = []
        for n in sockets:
            if not 1 <= n <= SOCKET_COUNT:
                raise PowerUSBError(f"socket must be 1..{SOCKET_COUNT}, got {n}")
            if n not in wanted:
                wanted.append(n)
        if not wanted:
            raise PowerUSBError("no sockets given")

        with self.lock:
            err = None
            for n in wanted:
                if self.desired[n - 1] != on:
                    self.events.add(
                        "socket",
                        f"{self.names[n - 1]} turned {'on' if on else 'off'}"
                        + ("" if self.online else " (queued - no mains power)"),
                        socket=n, on=on, source=source,
                    )
                self.desired[n - 1] = on
                err = self._switch(n, on) or err
            self._save_desired()
            self.last_change = _now()
            return self.snapshot(queued=not self.online, error=err)

    def set_all(self, on: bool, source: str = "app") -> Dict[str, Any]:
        """Switch every socket, with a single state save rather than one each."""
        return self.set_many(list(range(1, SOCKET_COUNT + 1)), on, source=source)

    def rename(self, socket_no: int, name: str) -> Dict[str, Any]:
        """Relabel a socket and persist it to config.json."""
        if not 1 <= socket_no <= SOCKET_COUNT:
            raise PowerUSBError(f"socket must be 1..{SOCKET_COUNT}, got {socket_no}")
        clean = " ".join(str(name).split())[:MAX_NAME_LEN].strip()
        if not clean:
            clean = f"Socket {socket_no}"
        with self.lock:
            self.names[socket_no - 1] = clean
            try:
                save_names(self.names)
            except OSError as exc:
                log(f"could not save names: {exc}")
            log(f"socket {socket_no} renamed to {clean!r}")
            return self.snapshot()

    def diagnostics(self) -> Dict[str, Any]:
        """Identity and a live current sample, for the capability probe."""
        with self.lock:
            if not self.online or self._dev is None:
                return {"online": False, "lan_url": LAN_URL, "public_url": PUBLIC_URL}
            try:
                model = self._dev.read_model()
                return {
                    "online": True,
                    "build": app_build(),
                    "lan_url": LAN_URL,
                    "public_url": PUBLIC_URL,
                    "model": model,
                    "model_name": PowerUSB.MODEL_NAMES.get(model, f"unknown ({model})"),
                    "firmware": self._dev.read_firmware(),
                    "metering": model in PowerUSB.METERING_MODELS,
                    "current_ma": self._dev.read_current_ma(),
                }
            except PowerUSBError as exc:
                return {"online": True, "error": str(exc)}

    @staticmethod
    def _norm(text: Any) -> str:
        return "".join(ch for ch in str(text).lower() if ch.isalnum())

    def resolve(self, text: str, _retry: bool = True) -> Optional[int]:
        """
        Map "2", "monitor", "lights", "light and power strip" to a socket.

        Siri hands over whatever the user said, so matching has to be forgiving
        about case, spaces, punctuation and plurals. Returns None when nothing
        matches, or when the text is ambiguous between two sockets.
        """
        t = self._norm(text)
        if not t:
            return None
        if t.isdigit():
            n = int(t)
            return n if 1 <= n <= SOCKET_COUNT else None

        names = [self._norm(n) for n in self.names]
        if t in names:
            return names.index(t) + 1
        for i, n in enumerate(names):
            if n and (n.startswith(t) or t.startswith(n)):
                return i + 1
        hits = [i for i, n in enumerate(names) if n and (t in n or n in t)]
        if len(hits) == 1:
            return hits[0] + 1
        # "lights" -> "light"
        if _retry and t.endswith("s"):
            return self.resolve(t[:-1], _retry=False)
        return None

    def spoken(self, socket_no: Optional[int] = None) -> str:
        """A short sentence Siri can read back."""
        snap = self.snapshot()
        if not snap["online"]:
            if socket_no:
                s = snap["sockets"][socket_no - 1]
                return (f"{s['name']} will be {'on' if s['on'] else 'off'} "
                        "when the wall switch comes back on.")
            return "The power strip has no mains power. The wall switch is off."
        if socket_no:
            s = snap["sockets"][socket_no - 1]
            return f"{s['name']} is {'on' if s['on'] else 'off'}."
        return " ".join(f"{s['name']} is {'on' if s['on'] else 'off'}."
                        for s in snap["sockets"])

    def spoken_group(self, sockets: List[int]) -> str:
        """A sentence Siri can read back about several sockets at once."""
        snap = self.snapshot()
        if not snap["online"]:
            return ("The power strip has no mains power. Your change is saved and "
                    "will apply when the wall switch comes back on.")
        items = [snap["sockets"][n - 1] for n in sockets]
        names = [i["name"] for i in items]
        if len({i["on"] for i in items}) == 1:
            state = "on" if items[0]["on"] else "off"
            if len(names) == 1:
                return f"{names[0]} is {state}."
            joined = (" and ".join(names) if len(names) == 2
                      else ", ".join(names[:-1]) + " and " + names[-1])
            return f"{joined} are {state}."
        return " ".join(f"{i['name']} is {'on' if i['on'] else 'off'}." for i in items)

    def usage_summary(self, hours: float = 24.0) -> Dict[str, Any]:
        """
        Estimated energy per socket over a window, from logged on-time.

        This strip has no current sensor, so nothing here is measured power.
        It replays the activity log to work out how long each socket was
        actually on -- which IS measured -- and multiplies by the rated watts
        you configure. Periods when mains was off count as zero for every
        socket, since nothing could draw then.
        """
        now = datetime.now(timezone.utc).astimezone()
        start = now - timedelta(hours=max(0.1, float(hours)))

        parsed = []
        for entry in reversed(self.events.recent(1000)):   # oldest first
            try:
                parsed.append((datetime.fromisoformat(entry.get("t", "")), entry))
            except (ValueError, TypeError):
                continue

        state: List[Optional[bool]] = [None] * SOCKET_COUNT
        mains: Optional[bool] = None

        def apply(entry: Dict[str, Any]) -> None:
            nonlocal mains
            if entry.get("kind") == "socket" and entry.get("socket"):
                idx = int(entry["socket"]) - 1
                if 0 <= idx < SOCKET_COUNT:
                    state[idx] = bool(entry.get("on"))
            elif entry.get("kind") == "mains":
                mains = bool(entry.get("on"))

        for when, entry in parsed:
            if when >= start:
                break
            apply(entry)

        # Anything the log never mentioned is assumed to have been as it is now.
        with self.lock:
            live = self.live_states() or list(self.desired)
            online_now = self.online
        for i in range(SOCKET_COUNT):
            if state[i] is None:
                state[i] = bool(live[i])
        if mains is None:
            mains = online_now

        # Only claim the period we actually have evidence for. With an empty
        # log, assuming a socket was on all night would invent usage.
        earliest = parsed[0][0] if parsed else None
        if earliest is not None and earliest > start:
            cursor = earliest
        else:
            cursor = start

        on_secs = [0.0] * SOCKET_COUNT
        window_start = cursor

        def accrue(until: datetime) -> None:
            span = (until - cursor).total_seconds()
            if span <= 0 or not mains:
                return
            for i in range(SOCKET_COUNT):
                if state[i]:
                    on_secs[i] += span

        for when, entry in parsed:
            if when < window_start:
                continue
            accrue(when)
            cursor = when
            apply(entry)
        accrue(now)
        covered = max(0.0, (now - window_start).total_seconds())

        sockets = []
        total_kwh = 0.0
        for i in range(SOCKET_COUNT):
            watts = float(self.watts[i] or 0.0)
            kwh = watts * (on_secs[i] / 3600.0) / 1000.0
            total_kwh += kwh
            sockets.append({
                "id": i + 1,
                "name": self.names[i],
                "watts": watts,
                "on_seconds": round(on_secs[i]),
                "kwh": round(kwh, 4),
            })
        return {
            "hours": hours,
            "covered_hours": round(covered / 3600.0, 2),
            "sockets": sockets,
            "total_kwh": round(total_kwh, 4),
            "estimated": True,
            "note": "Estimated from measured on-time x the watts you set; "
                    "this strip cannot measure power.",
        }

    def usage_daily(self, days: int = 7) -> Dict[str, Any]:
        """
        On-time per socket per local day, for the usage chart.

        Same replay as usage_summary, but bucketed into days and split at local
        midnight. Time while mains was off counts for nobody, and days the log
        does not reach back to are reported as null rather than zero so the
        chart can show "no data" instead of implying the sockets were idle.
        """
        days = max(1, min(int(days or 7), 31))
        now = datetime.now(timezone.utc).astimezone()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = midnight - timedelta(days=days - 1)

        parsed = []
        for entry in reversed(self.events.recent(1000)):       # oldest first
            try:
                parsed.append((datetime.fromisoformat(entry.get("t", "")), entry))
            except (ValueError, TypeError):
                continue

        state: List[Optional[bool]] = [None] * SOCKET_COUNT
        mains: Optional[bool] = None

        def apply(entry: Dict[str, Any]) -> None:
            nonlocal mains
            if entry.get("kind") == "socket" and entry.get("socket"):
                idx = int(entry["socket"]) - 1
                if 0 <= idx < SOCKET_COUNT:
                    state[idx] = bool(entry.get("on"))
            elif entry.get("kind") == "mains":
                mains = bool(entry.get("on"))

        for when, entry in parsed:
            if when >= start:
                break
            apply(entry)

        with self.lock:
            live = self.live_states() or list(self.desired)
            online_now = self.online
        for i in range(SOCKET_COUNT):
            if state[i] is None:
                state[i] = bool(live[i])
        if mains is None:
            mains = online_now

        # Only claim the span the log actually covers.
        earliest = parsed[0][0] if parsed else None
        covered_from = max(start, earliest) if earliest else now

        buckets = [[0.0] * SOCKET_COUNT for _ in range(days)]

        def day_index(moment: datetime) -> int:
            return (moment.date() - start.date()).days

        def accrue(frm: datetime, to: datetime) -> None:
            if to <= frm or not mains:
                return
            cursor = frm
            while cursor < to:
                day_end = (cursor.replace(hour=0, minute=0, second=0, microsecond=0)
                           + timedelta(days=1))
                slice_end = min(day_end, to)
                idx = day_index(cursor)
                if 0 <= idx < days:
                    span = (slice_end - cursor).total_seconds()
                    for i in range(SOCKET_COUNT):
                        if state[i]:
                            buckets[idx][i] += span
                cursor = slice_end

        cursor = covered_from
        for when, entry in parsed:
            if when < cursor:
                continue
            accrue(cursor, when)
            cursor = when
            apply(entry)
        accrue(cursor, now)

        out_days = []
        for d in range(days):
            day = (start + timedelta(days=d)).date()
            has_data = (start + timedelta(days=d + 1)) > covered_from
            out_days.append({
                "date": day.isoformat(),
                "weekday": day.strftime("%a"),
                "hours": [round(buckets[d][i] / 3600.0, 3) if has_data else None
                          for i in range(SOCKET_COUNT)],
            })

        return {
            "days": out_days,
            "sockets": [{"id": i + 1, "name": self.names[i]} for i in range(SOCKET_COUNT)],
            "covered_from": covered_from.isoformat(timespec="seconds"),
        }

    def set_watts(self, socket_no: int, watts: float) -> None:
        if not 1 <= socket_no <= SOCKET_COUNT:
            raise PowerUSBError(f"socket must be 1..{SOCKET_COUNT}, got {socket_no}")
        try:
            value = max(0.0, min(3000.0, float(watts)))
        except (TypeError, ValueError):
            raise PowerUSBError("watts must be a number") from None
        with self.lock:
            self.watts[socket_no - 1] = value
            try:
                save_watts(self.watts)
            except OSError as exc:
                log(f"could not save watts: {exc}")

    def snapshot(self, queued: bool = False, error: Optional[str] = None) -> Dict[str, Any]:
        with self.lock:
            live = self.live_states()
            sockets = []
            for i in range(SOCKET_COUNT):
                sockets.append(
                    {
                        "id": i + 1,
                        "name": self.names[i],
                        "on": (live[i] if live else self.desired[i]),
                        "desired": self.desired[i],
                        "live": (live[i] if live else None),
                        "since": self.since[i],
                        "watts": self.watts[i],
                    }
                )
            snap: Dict[str, Any] = {
                "online": self.online,
                "sockets": sockets,
                "last_change": self.last_change,
                "last_power_event": self.last_power_event,
                "power_events": self.power_events,
                "time": _now(),
            }
            if queued:
                snap["queued"] = True
            if error:
                snap["error"] = error
            return snap


# --------------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "PowerUSB/1.0"
    protocol_version = "HTTP/1.1"  # keep-alive; the GUI polls constantly
    controller: StripController
    scheduler: Scheduler
    away: AwayMode
    token: str

    def log_message(self, fmt, *args):  # quieter than the stdlib default
        pass

    # ----------------------------------------------------------- helpers

    def _authorised(self, query: Dict[str, List[str]]) -> bool:
        if not self.token:
            return True
        supplied = self.headers.get("X-Auth-Token") or (query.get("token") or [""])[0]
        return supplied == self.token

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Deliberately no Access-Control-Allow-Origin: the GUI is served from
        # this same origin, so it needs no CORS grant -- and a wildcard would
        # let any website you happen to visit switch your sockets.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass  # client hung up mid-response; nothing useful to do

    def _send_json(self, obj: Any, status: int = 200) -> None:
        self._send(json.dumps(obj).encode("utf-8"),
                   "application/json; charset=utf-8", status)

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json({"error": "not found"}, 404)
            return
        self._send(body, content_type)

    def _body_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 64 * 1024:   # nothing legitimate is bigger
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # ----------------------------------------------------------- verbs

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        route = parsed.path.rstrip("/") or "/"

        if route in ("/", "/index.html"):
            self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            return
        if route == "/manifest.webmanifest":
            self._send_file(WEB_DIR / "manifest.webmanifest", "application/manifest+json")
            return
        if route in ("/icon.png", "/icon-180.png", "/apple-touch-icon.png",
                     "/apple-touch-icon-precomposed.png"):
            name = "icon-180.png" if "180" in route or "apple" in route else "icon.png"
            self._send_file(WEB_DIR / name, "image/png")
            return
        if route == "/favicon.ico":
            self._send(b"", "image/x-icon", 204)
            return

        if not self._authorised(query):
            self._send_json({"error": "unauthorised"}, 401)
            return

        # ---- Shortcuts / Siri surface -------------------------------------
        # Deliberately GET and deliberately plain text: an Apple Shortcut is
        # then a single "Get Contents of URL" action with no method, headers or
        # JSON body to configure, and the reply can be fed straight to Speak
        # Text. Safe here because the server is loopback-only behind Tailscale
        # and holds no cookie/session a browser could be tricked into replaying.
        if route == "/s":
            self._send(self._shortcut_help().encode("utf-8"),
                       "text/plain; charset=utf-8")
            return
        if route.startswith("/s/"):
            self._handle_shortcut(route[len("/s/"):])
            return

        if route == "/api/state":
            snap = self.controller.snapshot()
            snap["build"] = app_build()
            self._send_json(snap)
            return
        if route == "/api/timers":
            self._send_json({"timers": self.scheduler.list(),
                             "countdowns": self.scheduler.list_countdowns()})
            return
        if route == "/api/log":
            try:
                limit = int((query.get("limit") or ["200"])[0])
            except ValueError:
                limit = 200
            self._send_json({"events": self.controller.events.recent(limit)})
            return
        if route == "/api/usage/daily":
            try:
                days = int((query.get("days") or ["7"])[0])
            except ValueError:
                days = 7
            self._send_json(self.controller.usage_daily(days))
            return
        if route == "/api/away":
            self._send_json(self.away.status())
            return
        if route == "/api/usage":
            try:
                hours = float((query.get("hours") or ["24"])[0])
            except ValueError:
                hours = 24.0
            self._send_json(self.controller.usage_summary(hours))
            return
        if route == "/api/diag":
            self._send_json(self.controller.diagnostics())
            return
        if route == "/api/health":
            self._send_json({"ok": True, "online": self.controller.online})
            return

        self._send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        if not self._authorised(parse_qs(parsed.query)):
            self._send_json({"error": "unauthorised"}, 401)
            return
        if route.startswith("/api/countdown/"):
            try:
                self.scheduler.cancel_countdown(route[len("/api/countdown/"):])
            except ScheduleError as exc:
                self._send_json({"error": str(exc)}, 404)
                return
            self._send_json({"countdowns": self.scheduler.list_countdowns()})
            return
        if route.startswith("/api/timers/"):
            try:
                self.scheduler.delete(route[len("/api/timers/"):])
            except ScheduleError as exc:
                self._send_json({"error": str(exc)}, 404)
                return
            self._send_json({"timers": self.scheduler.list()})
            return
        self._send_json({"error": "not found"}, 404)

    # ------------------------------------------------------- shortcuts

    def _shortcut_help(self) -> str:
        base = PUBLIC_URL.rstrip("/") if PUBLIC_URL else ""
        lines = ["PowerUSB - URLs for Apple Shortcuts / Siri", ""]
        for s in self.controller.snapshot()["sockets"]:
            slug = self.controller._norm(s["name"]) or str(s["id"])
            for verb in ("on", "off", "toggle"):
                lines.append(f"{base}/s/{slug}/{verb}")
            lines.append("")
        lines += [f"{base}/s/all/on", f"{base}/s/all/off",
                  f"{base}/s/status", ""]
        lines.append("A socket number also works, e.g. /s/1/on")
        lines.append("")
        lines.append("Several at once, separated by + or , :")
        names = [self.controller._norm(n) or str(i + 1)
                 for i, n in enumerate(self.controller.names)]
        if len(names) >= 2:
            pair = names[0] + "+" + names[1]
            lines.append(f"{base}/s/{pair}/on")
            lines.append(f"{base}/s/{pair}/off")
        return "\n".join(lines)

    def _handle_shortcut(self, rest: str) -> None:
        parts = [p for p in rest.split("/") if p]
        target = parts[0] if parts else "status"
        action = parts[1].lower() if len(parts) > 1 else "status"

        def reply(text: str, status: int = 200) -> None:
            self._send((text + "\n").encode("utf-8"),
                       "text/plain; charset=utf-8", status)

        c = self.controller
        try:
            if target.lower() == "status":
                reply(c.spoken())
                return
            if target.lower() == "all":
                if action in ("on", "off"):
                    c.set_all(action == "on", source="siri")
                    reply(f"All sockets {action}.")
                else:
                    reply(c.spoken())
                return

            # A target may name several sockets: "light+monitor" or
            # "light,monitor". That is what makes a one-tap scene link, and it
            # switches them as one group rather than one request each.
            wanted = [t for t in target.replace("+", ",").split(",") if t.strip()]
            resolved: List[int] = []
            for piece in wanted:
                n = c.resolve(piece)
                if n is None:
                    names = ", ".join(c.names)
                    reply(f"I do not know a socket called {piece!r}. "
                          f"Try one of: {names}.", 404)
                    return
                if n not in resolved:
                    resolved.append(n)

            delayed = (len(parts) >= 4 and parts[2].lower() == "in")
            if delayed:
                pass                       # handled below, do not switch now
            elif action == "on":
                c.set_many(resolved, True, source="siri")
            elif action == "off":
                c.set_many(resolved, False, source="siri")
            elif action == "toggle":
                for n in resolved:
                    c.toggle_socket(n, source="siri")
            elif action != "status":
                reply(f"Unknown action {action!r}. Use on, off, toggle or status.", 400)
                return

            # /s/<target>/off/in/30  -- a sleep timer straight from a Shortcut.
            if len(parts) >= 4 and parts[2].lower() == "in" and action in ("on", "off"):
                try:
                    minutes = float(parts[3])
                except ValueError:
                    reply(f"{parts[3]!r} is not a number of minutes.", 400)
                    return
                try:
                    for n in resolved:
                        self.scheduler.add_countdown(n, action, minutes)
                except ScheduleError as exc:
                    reply(f"Sorry: {exc}", 400)
                    return
                names = ", ".join(c.names[n - 1] for n in resolved)
                reply(f"{names} will turn {action} in {minutes:g} minutes.")
                return

            reply(c.spoken_group(resolved))
        except PowerUSBError as exc:
            reply(f"Sorry, that did not work: {exc}", 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        route = parsed.path.rstrip("/") or "/"

        if not self._authorised(query):
            self._send_json({"error": "unauthorised"}, 401)
            return

        body = self._body_json()

        try:
            if route == "/api/all":
                self._send_json(self.controller.set_all(bool(body.get("on"))))
                return
            if route == "/api/log/clear":
                self.controller.events.clear()
                self.controller.events.add("system", "Activity log cleared")
                self._send_json({"events": self.controller.events.recent(50)})
                return
            if route == "/api/away":
                self._send_json(self.away.update(body))
                return
            if route == "/api/countdown":
                self.scheduler.add_countdown(
                    body.get("socket"), body.get("action", "off"),
                    body.get("minutes", 30), body.get("label", ""))
                self._send_json({"countdowns": self.scheduler.list_countdowns()})
                return
            if route.startswith("/api/countdown/"):
                self.scheduler.cancel_countdown(route[len("/api/countdown/"):])
                self._send_json({"countdowns": self.scheduler.list_countdowns()})
                return
            if route == "/api/timers":
                self.scheduler.add(body)
                self._send_json({"timers": self.scheduler.list()})
                return
            if route.startswith("/api/timers/"):
                tail = route[len("/api/timers/"):].split("/")
                timer_id = tail[0]
                action = tail[1] if len(tail) > 1 else ""
                if action == "delete":
                    self.scheduler.delete(timer_id)
                elif action:
                    self._send_json({"error": f"unknown action {action!r}"}, 404)
                    return
                else:
                    self.scheduler.update(timer_id, body)
                self._send_json({"timers": self.scheduler.list()})
                return
            if route.startswith("/api/socket/"):
                tail = route[len("/api/socket/"):].split("/")
                socket_no = int(tail[0])
                action = tail[1] if len(tail) > 1 else ""
                if action == "name":
                    self._send_json(self.controller.rename(socket_no, body.get("name", "")))
                elif action == "watts":
                    self.controller.set_watts(socket_no, body.get("watts", 0))
                    self._send_json(self.controller.snapshot())
                elif action:
                    self._send_json({"error": f"unknown action {action!r}"}, 404)
                elif body.get("toggle"):
                    self._send_json(self.controller.toggle_socket(socket_no))
                else:
                    self._send_json(
                        self.controller.set_socket(socket_no, bool(body.get("on")))
                    )
                return
        except (ScheduleError, AwayError) as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        except (PowerUSBError, ValueError) as exc:
            self._send_json({"error": str(exc)}, 400)
            return

        self._send_json({"error": "not found"}, 404)


# ---------------------------------------------------------------------- TCP


class TCPHandler(socketserver.StreamRequestHandler):
    """
    Line protocol, one command per line, for scripts and home-automation kit:

        STATUS | ON n | OFF n | TOGGLE n | ALL ON | ALL OFF
        NAMES  | PING | AUTH <token> | QUIT

    Replies are "OK ..." or "ERR ...". STATUS answers "OK online 0 1 0", or
    "OK offline 0 1 0" reporting the desired state while mains is out.
    """

    timeout = 300
    controller: StripController
    token: str

    def handle(self):
        authed = not self.token
        self._reply("OK PowerUSB ready" + ("" if authed else " (AUTH required)"))
        while True:
            try:
                raw = self.rfile.readline(4096)
            except (OSError, socket.timeout):
                return
            if not raw:
                return
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue

            parts = line.split()
            cmd = parts[0].upper()
            args = parts[1:]

            if cmd == "QUIT":
                self._reply("OK BYE")
                return
            if cmd == "AUTH":
                if args and args[0] == self.token:
                    authed = True
                    self._reply("OK")
                else:
                    self._reply("ERR bad token")
                continue
            if not authed:
                self._reply("ERR authenticate first: AUTH <token>")
                continue

            try:
                self._reply(self._dispatch(cmd, args))
            except (PowerUSBError, ValueError) as exc:
                self._reply(f"ERR {exc}")

    def _dispatch(self, cmd: str, args: List[str]) -> str:
        c = self.controller
        if cmd == "PING":
            return "OK PONG"
        if cmd == "NAMES":
            return "OK " + "|".join(c.names)
        if cmd == "STATUS":
            return self._status(c.snapshot())
        if cmd in ("ON", "OFF", "TOGGLE"):
            if not args:
                return f"ERR usage: {cmd} <socket 1-{SOCKET_COUNT}>"
            n = int(args[0])
            snap = (c.toggle_socket(n, source="tcp") if cmd == "TOGGLE"
                    else c.set_socket(n, cmd == "ON", source="tcp"))
            return self._status(snap)
        if cmd == "ALL":
            if not args or args[0].upper() not in ("ON", "OFF"):
                return "ERR usage: ALL ON|OFF"
            return self._status(c.set_all(args[0].upper() == "ON", source="tcp"))
        return f"ERR unknown command {cmd}"

    @staticmethod
    def _status(snap: Dict[str, Any]) -> str:
        flags = " ".join("1" if s["on"] else "0" for s in snap["sockets"])
        return f"OK {'online' if snap['online'] else 'offline'} {flags}"

    def _reply(self, text: str) -> None:
        try:
            self.wfile.write((text + "\r\n").encode("utf-8"))
        except OSError:
            pass


class ThreadedTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# --------------------------------------------------------------------- main


def lan_ip() -> str:
    """Best guess at this machine's LAN address, for the printed banner."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _already_serving(port: int) -> bool:
    """
    True if a PowerUSB server is already answering on this port.

    SO_REUSEADDR (set by allow_reuse_address) means a second bind can quietly
    succeed on Windows rather than raising, so binding is not a reliable
    singleton test. Asking the health endpoint is.
    """
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def main() -> int:
    cfg = load_config()
    if cfg.get("config_error"):
        log(f"WARNING: {cfg['config_error']}")
    events = EventLog(EVENTS_PATH)
    controller = StripController(cfg["names"], events=events, watts=cfg["watts"])
    scheduler = Scheduler(controller, SCHEDULE_PATH)
    scheduler.set_logger(log)
    away = AwayMode(controller, AWAY_PATH)
    away.set_logger(log)

    Handler.controller = controller
    Handler.scheduler = scheduler
    Handler.away = away
    Handler.token = cfg["token"]
    TCPHandler.controller = controller
    TCPHandler.token = cfg["token"]

    host = cfg["host"]
    http_port = int(cfg["http_port"])
    tcp_port = int(cfg["tcp_port"])

    if _already_serving(http_port):
        log(f"another PowerUSB server is already serving on port {http_port} - exiting")
        return 1

    try:
        http = ThreadingHTTPServer((host, http_port), Handler)
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", 10048)):
            log(f"port {http_port} is already in use -- is the server already running?")
            log("   check with:  pusb info      stop it, or change http_port in config.json")
            return 1
        raise
    http.daemon_threads = True

    tcp = None
    if tcp_port:
        try:
            tcp = ThreadedTCPServer((host, tcp_port), TCPHandler)
        except OSError as exc:
            log(f"could not bind TCP port {tcp_port} ({exc}); continuing without it")
            tcp = None

    # Only start touching hardware once the sockets are bound, so a duplicate
    # launch cannot fight the running instance over the USB handle.
    controller.start()
    scheduler.start()
    away.start_thread()
    events.add("system", "Server started")

    threading.Thread(target=http.serve_forever, name="http", daemon=True).start()
    if tcp:
        threading.Thread(target=tcp.serve_forever, name="tcp", daemon=True).start()

    ip = lan_ip()
    global LAN_URL, PUBLIC_URL
    LAN_URL = f"http://{ip}:{http_port}/"
    PUBLIC_URL = str(cfg.get("public_url") or "").strip()

    log("PowerUSB server started")
    if PUBLIC_URL:
        log(f"  web GUI      {PUBLIC_URL}   (open this on your phone)")
    log(f"  local        http://localhost:{http_port}/")
    if host in ("127.0.0.1", "localhost", "::1"):
        # Bound to loopback on purpose: reachable only through the Tailscale
        # proxy, so printing a LAN URL here would just send people nowhere.
        log(f"  bound to     {host} only - not reachable from the LAN")
    else:
        log(f"  LAN          {LAN_URL}")
    if tcp:
        log(f"  TCP control  {host}:{tcp_port}")
    log(f"  auth         {'token required' if cfg['token'] else 'none (anyone on your tailnet can control it)'}")
    log(f"  timers       {len(scheduler.timers)} configured, "
        f"{len(scheduler.countdowns)} countdown(s)")
    if away.settings["enabled"]:
        log(f"  away mode    ON for sockets {away.settings['sockets']}")
    log(f"  strip        {'online' if controller.online else 'connecting...'}")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        away.stop()
        scheduler.stop()
        controller.stop()
        http.shutdown()
        if tcp:
            tcp.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
