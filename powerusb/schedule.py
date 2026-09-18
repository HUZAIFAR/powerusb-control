"""
Recurring on/off schedules for the PowerUSB sockets.

A timer is a weekly recurrence: "socket 1 off at 23:30 on Mon-Fri". The
scheduler ticks once a minute-ish and fires anything that has come due.

Two details matter because of how this strip is wired:

  * Firing a timer sets the *desired* state through the controller, not the
    hardware directly. If the wall switch is off, the controller queues the
    intent and applies it when mains returns -- so a timer that fires into a
    dark strip is still honoured, just later.

  * Each timer records when it last fired. On startup, anything whose due
    time passed within CATCHUP_WINDOW is still run, so a quick server
    restart or a brief sleep does not silently skip the evening lights.
    Anything older than that is marked as fired and skipped, so booting the
    PC at noon does not replay last night's schedule.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .device import SOCKET_COUNT

# How late a timer may fire after its due time (e.g. after a restart).
CATCHUP_WINDOW = timedelta(minutes=60)

TICK_SECONDS = 20

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class ScheduleError(ValueError):
    """Raised when a timer definition is not usable."""


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _parse_hhmm(value: Any) -> str:
    """Validate a 'HH:MM' string and return it normalised."""
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ScheduleError(f"time must look like HH:MM, got {text!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        raise ScheduleError(f"time must look like HH:MM, got {text!r}") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError(f"time out of range: {text!r}")
    return f"{hour:02d}:{minute:02d}"


def _parse_days(value: Any) -> List[int]:
    """Days as 0=Mon..6=Sun. An empty list means every day."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ScheduleError("days must be a list of 0..6 (0 = Monday)")
    days = []
    for item in value:
        try:
            day = int(item)
        except (TypeError, ValueError):
            raise ScheduleError(f"bad day {item!r}") from None
        if not 0 <= day <= 6:
            raise ScheduleError(f"day must be 0..6, got {day}")
        if day not in days:
            days.append(day)
    return sorted(days)


class Timer:
    __slots__ = ("id", "socket", "action", "time", "days", "enabled", "label", "last_fired")

    def __init__(self, **kw):
        self.id: str = kw.get("id") or uuid.uuid4().hex[:12]
        self.socket: int = kw["socket"]
        self.action: str = kw["action"]
        self.time: str = kw["time"]
        self.days: List[int] = kw.get("days") or []
        self.enabled: bool = bool(kw.get("enabled", True))
        self.label: str = kw.get("label") or ""
        self.last_fired: Optional[str] = kw.get("last_fired")

    # ------------------------------------------------------------- parsing

    @classmethod
    def from_dict(cls, data: Dict[str, Any], existing: Optional["Timer"] = None) -> "Timer":
        """Build (or update) a timer from untrusted JSON."""
        src = {} if existing is None else existing.to_dict()

        socket = data.get("socket", src.get("socket"))
        try:
            socket = int(socket)
        except (TypeError, ValueError):
            raise ScheduleError("socket is required") from None
        if not 1 <= socket <= SOCKET_COUNT:
            raise ScheduleError(f"socket must be 1..{SOCKET_COUNT}, got {socket}")

        action = str(data.get("action", src.get("action", "on"))).lower()
        if action not in ("on", "off"):
            raise ScheduleError("action must be 'on' or 'off'")

        when = _parse_hhmm(data.get("time", src.get("time")))
        days = _parse_days(data["days"]) if "days" in data else list(src.get("days") or [])
        enabled = bool(data.get("enabled", src.get("enabled", True)))
        label = str(data.get("label", src.get("label", "")))[:40].strip()

        return cls(
            id=src.get("id"),
            socket=socket,
            action=action,
            time=when,
            days=days,
            enabled=enabled,
            label=label,
            # Editing the schedule clears the fired marker, otherwise a timer
            # moved to an earlier time today would refuse to run.
            last_fired=src.get("last_fired") if _unchanged(src, when, days) else None,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "socket": self.socket,
            "action": self.action,
            "time": self.time,
            "days": list(self.days),
            "enabled": self.enabled,
            "label": self.label,
            "last_fired": self.last_fired,
        }

    # ------------------------------------------------------------- timing

    def runs_on(self, weekday: int) -> bool:
        return not self.days or weekday in self.days

    def most_recent_due(self, now: datetime) -> Optional[datetime]:
        """The latest scheduled moment at or before `now`, within a week."""
        hour, minute = (int(x) for x in self.time.split(":"))
        for back in range(0, 8):
            day = (now - timedelta(days=back)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if day > now:
                continue
            if self.runs_on(day.weekday()):
                return day
        return None

    def next_due(self, now: datetime) -> Optional[datetime]:
        """The next scheduled moment strictly after `now` (for the GUI)."""
        hour, minute = (int(x) for x in self.time.split(":"))
        for ahead in range(0, 8):
            day = (now + timedelta(days=ahead)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if day <= now:
                continue
            if self.runs_on(day.weekday()):
                return day
        return None


def _unchanged(src: Dict[str, Any], when: str, days: List[int]) -> bool:
    return bool(src) and src.get("time") == when and list(src.get("days") or []) == days


class Scheduler:
    """Owns the timer list, persists it, and fires timers when they come due."""

    def __init__(self, controller, path: Path):
        self.controller = controller
        self.path = path
        self.lock = threading.RLock()
        self.timers: List[Timer] = []
        # One-shot "off in 30 minutes" entries. Kept separate from the weekly
        # timers because they are consumed when they fire.
        self.countdowns: List[Dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._log: Callable[[str], None] = lambda msg: None
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
        if isinstance(raw, dict):
            for item in raw.get("countdowns") or []:
                if (isinstance(item, dict) and item.get("fire_at")
                        and item.get("socket") and item.get("action") in ("on", "off")):
                    self.countdowns.append(item)
        items = raw.get("timers") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return
        loaded = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                timer = Timer.from_dict(item)
                timer.id = item.get("id") or timer.id
                timer.last_fired = item.get("last_fired")
                loaded.append(timer)
            except ScheduleError:
                continue          # drop anything corrupt rather than refuse to start
        self.timers = loaded

    def _save(self) -> None:
        try:
            payload = {"timers": [t.to_dict() for t in self.timers],
                       "countdowns": list(self.countdowns)}
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            self._log(f"could not save schedules: {exc}")

    # ---------------------------------------------------------------- crud

    def list(self) -> List[Dict[str, Any]]:
        now = _local_now()
        with self.lock:
            out = []
            for t in sorted(self.timers, key=lambda x: (x.time, x.socket)):
                item = t.to_dict()
                nxt = t.next_due(now) if t.enabled else None
                item["next"] = nxt.isoformat(timespec="minutes") if nxt else None
                out.append(item)
            return out

    def add(self, data: Dict[str, Any]) -> Dict[str, Any]:
        timer = Timer.from_dict(data)
        with self.lock:
            if len(self.timers) >= 40:
                raise ScheduleError("too many timers (40 max)")
            # A brand-new timer should not immediately fire for a time that
            # already passed earlier today.
            due = timer.most_recent_due(_local_now())
            if due is not None:
                timer.last_fired = due.isoformat(timespec="seconds")
            self.timers.append(timer)
            self._save()
            self._log(f"timer added: socket {timer.socket} {timer.action} at {timer.time}")
            return timer.to_dict()

    def update(self, timer_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            existing = self._find(timer_id)
            merged = Timer.from_dict(data, existing)
            merged.id = existing.id
            index = self.timers.index(existing)
            self.timers[index] = merged
            self._save()
            return merged.to_dict()

    def delete(self, timer_id: str) -> None:
        with self.lock:
            self.timers.remove(self._find(timer_id))
            self._save()
            self._log(f"timer {timer_id} deleted")

    def _find(self, timer_id: str) -> Timer:
        for t in self.timers:
            if t.id == timer_id:
                return t
        raise ScheduleError(f"no timer with id {timer_id!r}")

    # ------------------------------------------------------- countdowns

    def list_countdowns(self) -> List[Dict[str, Any]]:
        """Active countdowns, soonest first, with seconds remaining."""
        now = _local_now()
        with self.lock:
            out = []
            for cd in self.countdowns:
                try:
                    fire_at = datetime.fromisoformat(cd["fire_at"])
                except (ValueError, KeyError, TypeError):
                    continue
                item = dict(cd)
                item["remaining"] = max(0, int((fire_at - now).total_seconds()))
                out.append(item)
            return sorted(out, key=lambda c: c["remaining"])

    def add_countdown(self, socket: int, action: str, minutes: float,
                      label: str = "") -> Dict[str, Any]:
        try:
            socket = int(socket)
        except (TypeError, ValueError):
            raise ScheduleError("socket is required") from None
        if not 1 <= socket <= SOCKET_COUNT:
            raise ScheduleError(f"socket must be 1..{SOCKET_COUNT}, got {socket}")
        action = str(action).lower()
        if action not in ("on", "off"):
            raise ScheduleError("action must be 'on' or 'off'")
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            raise ScheduleError("minutes must be a number") from None
        if not 0 < minutes <= 24 * 60:
            raise ScheduleError("minutes must be between 0 and 1440")

        entry = {
            "id": uuid.uuid4().hex[:12],
            "socket": socket,
            "action": action,
            "label": str(label)[:40].strip(),
            "fire_at": (_local_now() + timedelta(minutes=minutes)).isoformat(
                timespec="seconds"),
            "minutes": minutes,
        }
        with self.lock:
            # One pending countdown per socket+action: setting a new one should
            # replace the old rather than leave two racing to switch it.
            self.countdowns = [c for c in self.countdowns
                               if not (c.get("socket") == socket
                                       and c.get("action") == action)]
            if len(self.countdowns) >= 20:
                raise ScheduleError("too many countdowns (20 max)")
            self.countdowns.append(entry)
            self._save()
        self._log(f"countdown set: socket {socket} -> {action} in {minutes:g} min")
        return entry

    def cancel_countdown(self, countdown_id: str) -> None:
        with self.lock:
            before = len(self.countdowns)
            self.countdowns = [c for c in self.countdowns
                               if c.get("id") != countdown_id]
            if len(self.countdowns) == before:
                raise ScheduleError(f"no countdown with id {countdown_id!r}")
            self._save()
        self._log(f"countdown {countdown_id} cancelled")

    # ----------------------------------------------------------- firing

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:          # the scheduler must never die
                self._log(f"scheduler error: {exc}")
            self._stop.wait(TICK_SECONDS)

    def _due_countdowns(self, now: datetime) -> List[Dict[str, Any]]:
        """Pop every countdown that has come due, dropping stale ones."""
        due: List[Dict[str, Any]] = []
        with self.lock:
            keep: List[Dict[str, Any]] = []
            for cd in self.countdowns:
                try:
                    fire_at = datetime.fromisoformat(cd["fire_at"])
                except (ValueError, KeyError, TypeError):
                    continue          # unparseable: drop it
                if now < fire_at:
                    keep.append(cd)
                elif now - fire_at <= CATCHUP_WINDOW:
                    due.append(cd)    # fire, then consume
                else:
                    # Server was down long past the moment. Switching now would
                    # be a surprise hours later, so drop it silently.
                    self._log(f"countdown {cd.get('id')} expired unfired (too stale)")
            if len(keep) != len(self.countdowns):
                self.countdowns = keep
                self._save()
        return due

    def tick(self, now: Optional[datetime] = None) -> List[Timer]:
        """Fire everything that has come due. Returns the timers that ran."""
        now = now or _local_now()
        fired: List[Timer] = []

        for cd in self._due_countdowns(now):
            want = cd["action"] == "on"
            self._log(f"countdown fired: socket {cd['socket']} -> {cd['action']}")
            try:
                self.controller.set_socket(cd["socket"], want, source="countdown")
            except Exception as exc:
                self._log(f"countdown {cd.get('id')} failed to apply: {exc}")
        with self.lock:
            dirty = False
            for timer in self.timers:
                if not timer.enabled:
                    continue
                due = timer.most_recent_due(now)
                if due is None:
                    continue
                if timer.last_fired:
                    try:
                        if datetime.fromisoformat(timer.last_fired) >= due:
                            continue
                    except ValueError:
                        pass          # unparseable marker: treat as never fired
                stamp = due.isoformat(timespec="seconds")
                if now - due > CATCHUP_WINDOW:
                    # Too stale to act on -- record it so it stays skipped.
                    timer.last_fired = stamp
                    dirty = True
                    continue
                timer.last_fired = stamp
                dirty = True
                fired.append(timer)
            if dirty:
                self._save()

        # Switch outside the scheduler lock: set_socket takes the controller
        # lock and can block on USB, and holding both invites a deadlock.
        for timer in fired:
            want = timer.action == "on"
            late = "" if (now - timer.most_recent_due(now)) < timedelta(seconds=90) else " (catch-up)"
            self._log(f"timer fired{late}: socket {timer.socket} -> {timer.action} ({timer.time})")
            try:
                self.controller.set_socket(timer.socket, want, source="timer")
            except Exception as exc:
                self._log(f"timer {timer.id} failed to apply: {exc}")
        return fired
