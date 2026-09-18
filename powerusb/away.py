"""
Away mode: make the place look lived-in while nobody is.

Within a nightly window it switches the chosen sockets on and off at irregular
intervals, the way someone moving around a house would, rather than on a clock
pattern that reads as automation from the street.

Design notes that matter:

  * Dwell times are randomised per socket, and the sockets are deliberately not
    synchronised, so they never all change together.
  * Outside the window everything it controls goes off once, and it then leaves
    the strip alone until the next evening.
  * It only ever touches the sockets you list. Anything else you leave on stays
    on.
  * Turning away mode off restores the sockets to exactly the states they were
    in when it was turned on, so coming home does not leave you rearranging
    everything by hand.
  * It drives the controller, not the hardware, so while the wall switch is off
    its changes queue like any other and apply when power returns.
"""

from __future__ import annotations

import json
import random
import threading
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .device import SOCKET_COUNT

TICK_SECONDS = 20

# How long a socket stays in one state before it may change again.
MIN_DWELL_MINUTES = 12
MAX_DWELL_MINUTES = 75

DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "sockets": [],          # 1-based socket numbers to simulate
    "start": "17:30",       # window during which it is "awake"
    "end": "23:15",
    # Probability a socket is on when it next changes. Above 0.5 because a lit
    # room reads as occupied and a dark one does not.
    "on_bias": 0.6,
}


class AwayError(ValueError):
    """Raised when an away-mode setting is not usable."""


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _parse_hhmm(value: Any, field: str) -> str:
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise AwayError(f"{field} must look like HH:MM, got {text!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        raise AwayError(f"{field} must look like HH:MM, got {text!r}") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise AwayError(f"{field} out of range: {text!r}")
    return f"{hour:02d}:{minute:02d}"


def _as_time(hhmm: str) -> dtime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return dtime(hour, minute)


class AwayMode:
    """Owns the away-mode settings, its thread, and the restore snapshot."""

    def __init__(self, controller, path: Path):
        self.controller = controller
        self.path = path
        self.lock = threading.RLock()
        self.settings: Dict[str, Any] = dict(DEFAULTS)
        self.settings["sockets"] = []
        # What the sockets looked like when away mode was switched on, so it
        # can be handed back unchanged.
        self.snapshot_before: Optional[List[bool]] = None
        self._next_change: Dict[int, datetime] = {}
        self._slept = False          # have we already done the outside-window off?
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="away-mode", daemon=True)
        self._log: Callable[[str], None] = lambda msg: None
        self._rng = random.Random()
        self._load()

    def set_logger(self, fn: Callable[[str], None]) -> None:
        self._log = fn

    # --------------------------------------------------------- persistence

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        try:
            self.settings = self._clean(raw.get("settings") or raw)
        except AwayError:
            return
        before = raw.get("snapshot_before")
        if isinstance(before, list) and len(before) == SOCKET_COUNT:
            self.snapshot_before = [bool(x) for x in before]

    def _save(self) -> None:
        try:
            payload = {"settings": self.settings,
                       "snapshot_before": self.snapshot_before}
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            self._log(f"could not save away mode: {exc}")

    # ------------------------------------------------------------ settings

    @staticmethod
    def _clean(data: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(DEFAULTS)
        out["enabled"] = bool(data.get("enabled", False))
        sockets = data.get("sockets")
        if sockets is None:
            sockets = []
        if not isinstance(sockets, (list, tuple)):
            raise AwayError("sockets must be a list of socket numbers")
        clean: List[int] = []
        for item in sockets:
            try:
                n = int(item)
            except (TypeError, ValueError):
                raise AwayError(f"bad socket {item!r}") from None
            if not 1 <= n <= SOCKET_COUNT:
                raise AwayError(f"socket must be 1..{SOCKET_COUNT}, got {n}")
            if n not in clean:
                clean.append(n)
        out["sockets"] = sorted(clean)
        out["start"] = _parse_hhmm(data.get("start", DEFAULTS["start"]), "start")
        out["end"] = _parse_hhmm(data.get("end", DEFAULTS["end"]), "end")
        try:
            bias = float(data.get("on_bias", DEFAULTS["on_bias"]))
        except (TypeError, ValueError):
            raise AwayError("on_bias must be a number") from None
        out["on_bias"] = min(0.95, max(0.05, bias))
        return out

    def status(self) -> Dict[str, Any]:
        with self.lock:
            out = dict(self.settings)
            out["active"] = bool(self.settings["enabled"] and self._in_window(_local_now()))
            out["restores_to"] = list(self.snapshot_before or [])
            return out

    def update(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Apply new settings, taking or restoring the snapshot as needed."""
        with self.lock:
            merged = dict(self.settings)
            merged.update({k: v for k, v in data.items()
                           if k in ("enabled", "sockets", "start", "end", "on_bias")})
            new = self._clean(merged)
            was, now_on = self.settings["enabled"], new["enabled"]
            self.settings = new

            if now_on and not was:
                self.snapshot_before = self._current_states()
                self._next_change = {}
                self._slept = False
                self._log(f"away mode ON for sockets {new['sockets']} "
                          f"between {new['start']} and {new['end']}")
            elif was and not now_on:
                self._log("away mode OFF")
                self._restore()
            self._save()
            return self.status()

    # --------------------------------------------------------------- guts

    def _current_states(self) -> List[bool]:
        live = self.controller.live_states()
        return list(live) if live else list(self.controller.desired)

    def _restore(self) -> None:
        """Put the sockets back exactly as they were before away mode began."""
        before = self.snapshot_before
        self.snapshot_before = None
        if not before:
            return
        for n in self.settings["sockets"]:
            try:
                self.controller.set_socket(n, before[n - 1], source="away")
            except Exception as exc:
                self._log(f"away restore failed for socket {n}: {exc}")
        self._log("away mode restored the previous socket states")

    def _in_window(self, now: datetime) -> bool:
        start, end = _as_time(self.settings["start"]), _as_time(self.settings["end"])
        current = now.time()
        if start <= end:
            return start <= current < end
        # Window wraps past midnight, e.g. 21:00 -> 01:30.
        return current >= start or current < end

    def _schedule_next(self, socket: int, now: datetime) -> None:
        minutes = self._rng.uniform(MIN_DWELL_MINUTES, MAX_DWELL_MINUTES)
        self._next_change[socket] = now + timedelta(minutes=minutes)

    # ---------------------------------------------------------------- loop

    def start_thread(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:          # must never take the server down
                self._log(f"away mode error: {exc}")
            self._stop.wait(TICK_SECONDS)

    def tick(self, now: Optional[datetime] = None) -> List[int]:
        """One step. Returns the sockets it changed, for tests."""
        now = now or _local_now()
        changed: List[int] = []
        with self.lock:
            if not self.settings["enabled"] or not self.settings["sockets"]:
                return changed

            if not self._in_window(now):
                if not self._slept:
                    # Night is over: settle down once, then stay quiet.
                    for n in self.settings["sockets"]:
                        try:
                            self.controller.set_socket(n, False, source="away")
                            changed.append(n)
                        except Exception as exc:
                            self._log(f"away mode could not switch socket {n}: {exc}")
                    self._slept = True
                    self._next_change = {}
                    self._log("away mode: outside the window, sockets off")
                return changed

            self._slept = False
            for n in self.settings["sockets"]:
                due = self._next_change.get(n)
                if due is not None and now < due:
                    continue
                want = self._rng.random() < self.settings["on_bias"]
                try:
                    self.controller.set_socket(n, want, source="away")
                    changed.append(n)
                except Exception as exc:
                    self._log(f"away mode could not switch socket {n}: {exc}")
                self._schedule_next(n, now)
            if changed:
                self._log("away mode switched " + ", ".join(str(n) for n in changed))
        return changed
