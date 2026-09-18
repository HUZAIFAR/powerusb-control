"""
Countdown (sleep) timers, away mode, and the daily usage buckets.

Run:  python tests/test_away_countdown.py
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from powerusb import server as srv                            # noqa: E402
from powerusb.away import AwayMode, AwayError                 # noqa: E402
from powerusb.events import EventLog                          # noqa: E402
from powerusb.schedule import Scheduler, ScheduleError        # noqa: E402

_scratch = Path(tempfile.mkdtemp(prefix="pusb-away-"))
srv.LOG_PATH = _scratch / "server.log"

PASS = FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


class FakeController:
    """Records what it was asked to do; enough for the scheduler and away mode."""

    def __init__(self):
        self.calls = []
        self.sources = []
        self.desired = [False, False, False]

    def set_socket(self, socket_no, on, source="app"):
        self.calls.append((socket_no, on))
        self.sources.append(source)
        self.desired[socket_no - 1] = bool(on)
        return {}

    def live_states(self):
        return list(self.desired)


def when(hour, minute=0, day=18):
    """A fixed local moment in September 2026."""
    return datetime(2026, 9, day, hour, minute).astimezone()


# ------------------------------------------------------------------ countdown

def test_countdown():
    print("countdown timers")
    with tempfile.TemporaryDirectory() as tmp:
        s = Scheduler(FakeController(), Path(tmp) / "schedules.json")

        cd = s.add_countdown(1, "off", 30)
        check("created", cd["socket"] == 1 and cd["action"] == "off", cd)
        listed = s.list_countdowns()
        check("listed with time remaining",
              len(listed) == 1 and 1750 <= listed[0]["remaining"] <= 1800,
              listed)

        s.tick(now=datetime.now().astimezone() + timedelta(minutes=5))
        check("does not fire early", s.controller.calls == [], s.controller.calls)

        s.tick(now=datetime.now().astimezone() + timedelta(minutes=31))
        check("fires when due", s.controller.calls == [(1, False)], s.controller.calls)
        check("tagged as a countdown", s.controller.sources == ["countdown"],
              s.controller.sources)
        check("consumed after firing", s.list_countdowns() == [], s.list_countdowns())

        s.tick(now=datetime.now().astimezone() + timedelta(minutes=45))
        check("does not fire twice", s.controller.calls == [(1, False)],
              s.controller.calls)

    print("countdown edge cases")
    with tempfile.TemporaryDirectory() as tmp:
        s = Scheduler(FakeController(), Path(tmp) / "schedules.json")
        for bad, why in [((9, "off", 30), "socket out of range"),
                         ((1, "melt", 30), "bad action"),
                         ((1, "off", 0), "zero minutes"),
                         ((1, "off", 5000), "absurd duration"),
                         ((1, "off", "soon"), "non-numeric minutes")]:
            try:
                s.add_countdown(*bad)
                check(why, False, "was accepted")
            except ScheduleError:
                check(why, True)

        # Setting a second countdown for the same socket+action replaces it.
        s.add_countdown(2, "off", 30)
        s.add_countdown(2, "off", 60)
        check("same socket+action replaces rather than stacks",
              len(s.list_countdowns()) == 1, s.list_countdowns())
        check("the newer duration won",
              s.list_countdowns()[0]["remaining"] > 3000, s.list_countdowns())

        s.add_countdown(2, "on", 10)
        check("a different action coexists", len(s.list_countdowns()) == 2,
              s.list_countdowns())

        cid = s.list_countdowns()[0]["id"]
        s.cancel_countdown(cid)
        check("cancel removes it", len(s.list_countdowns()) == 1)
        try:
            s.cancel_countdown("nope")
            check("cancelling an unknown id errors", False)
        except ScheduleError:
            check("cancelling an unknown id errors", True)

    print("a countdown missed while the server was down")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedules.json"
        s = Scheduler(FakeController(), path)
        s.add_countdown(3, "off", 10)

        # Back within the catch-up window: still honour it.
        s2 = Scheduler(FakeController(), path)
        s2.tick(now=datetime.now().astimezone() + timedelta(minutes=20))
        check("fires late but within the window",
              s2.controller.calls == [(3, False)], s2.controller.calls)

        s3 = Scheduler(FakeController(), path)
        s3.add_countdown(3, "off", 10)
        s3b = Scheduler(FakeController(), path)
        s3b.tick(now=datetime.now().astimezone() + timedelta(hours=9))
        check("a long-stale countdown is dropped, not fired",
              s3b.controller.calls == [], s3b.controller.calls)
        check("and is cleared away", s3b.list_countdowns() == [])

    print("countdowns survive a restart")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedules.json"
        a = Scheduler(FakeController(), path)
        a.add_countdown(1, "off", 45, label="sleep")
        b = Scheduler(FakeController(), path)
        check("reloaded from disk", len(b.list_countdowns()) == 1, b.list_countdowns())
        check("label kept", b.list_countdowns()[0]["label"] == "sleep")


# ---------------------------------------------------------------------- away

def test_away():
    print("away mode settings")
    with tempfile.TemporaryDirectory() as tmp:
        a = AwayMode(FakeController(), Path(tmp) / "away.json")
        check("off by default", a.status()["enabled"] is False)

        for bad, why in [({"sockets": [9]}, "socket out of range"),
                         ({"start": "25:00"}, "bad start time"),
                         ({"end": "nope"}, "unparseable end time"),
                         ({"sockets": "all"}, "sockets not a list")]:
            try:
                a.update(dict(bad, enabled=True))
                check(why, False, "was accepted")
            except AwayError:
                check(why, True)

    print("away mode switching")
    with tempfile.TemporaryDirectory() as tmp:
        c = FakeController()
        c.desired = [True, False, True]          # state before leaving
        a = AwayMode(c, Path(tmp) / "away.json")
        a.update({"enabled": True, "sockets": [1, 2],
                  "start": "17:00", "end": "23:00"})
        check("snapshot taken on enable", a.snapshot_before == [True, False, True],
              a.snapshot_before)

        c.calls = []
        a.tick(now=when(19, 0))
        check("acts inside the window on its own sockets",
              sorted({n for n, _ in c.calls}) == [1, 2], c.calls)
        check("never touches a socket it was not given",
              all(n != 3 for n, _ in c.calls), c.calls)
        check("tagged as away", set(c.sources) == {"away"}, set(c.sources))

        # Straight after acting it should wait, not thrash.
        c.calls = []
        a.tick(now=when(19, 1))
        check("does not switch again immediately", c.calls == [], c.calls)

        c.calls = []
        a.tick(now=when(23, 30))
        check("outside the window everything it owns goes off",
              sorted(c.calls) == [(1, False), (2, False)], c.calls)
        c.calls = []
        a.tick(now=when(23, 45))
        check("and it then stays quiet", c.calls == [], c.calls)

    print("away mode restores what it found")
    with tempfile.TemporaryDirectory() as tmp:
        c = FakeController()
        c.desired = [True, False, True]
        a = AwayMode(c, Path(tmp) / "away.json")
        a.update({"enabled": True, "sockets": [1, 2], "start": "17:00", "end": "23:00"})
        a.tick(now=when(19, 0))
        a.tick(now=when(20, 30))

        c.calls = []
        a.update({"enabled": False})
        check("puts its sockets back as they were",
              c.desired[:2] == [True, False], c.desired)
        check("snapshot cleared", a.snapshot_before is None)

    print("away mode window wrapping past midnight")
    with tempfile.TemporaryDirectory() as tmp:
        a = AwayMode(FakeController(), Path(tmp) / "away.json")
        a.update({"enabled": True, "sockets": [1], "start": "21:00", "end": "01:30"})
        check("22:00 is inside", a._in_window(when(22, 0)))
        check("00:30 is inside", a._in_window(when(0, 30)))
        check("12:00 is outside", not a._in_window(when(12, 0)))

    print("away mode settings persist")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "away.json"
        a = AwayMode(FakeController(), path)
        a.update({"enabled": True, "sockets": [2, 3], "start": "18:00", "end": "22:00"})
        b = AwayMode(FakeController(), path)
        check("reloaded", b.status()["enabled"] is True and b.status()["sockets"] == [2, 3],
              b.status())


# --------------------------------------------------------------- daily usage

class UsageStrip:
    sockets = [False, False, False]
    eeprom = [False, False, False]

    def __init__(self, auto_open=False): pass
    def open(self): pass
    def close(self): pass
    def get(self, i): return UsageStrip.sockets[i - 1]
    def set(self, i, on):
        UsageStrip.sockets[i - 1] = bool(on)
        return UsageStrip.sockets[i - 1]
    def get_default(self, i, refresh=False): return UsageStrip.eeprom[i - 1]
    def set_default(self, i, on):
        UsageStrip.eeprom[i - 1] = bool(on)
        return bool(on)
    def states(self): return list(UsageStrip.sockets)
    def defaults(self): return list(UsageStrip.eeprom)


def test_usage_daily():
    print("daily usage buckets")
    with tempfile.TemporaryDirectory() as tmp:
        UsageStrip.sockets = [False, False, False]
        UsageStrip.eeprom = [False, False, False]
        srv.PowerUSB = UsageStrip
        srv.is_present = lambda: True
        srv.STATE_PATH = Path(tmp) / "state.json"
        c = srv.StripController(["Light", "Monitor", "Fan"],
                                events=EventLog(Path(tmp) / "events.jsonl"))
        c.poll_once(boot_grace=0)

        data = c.usage_daily(7)
        check("returns one entry per day", len(data["days"]) == 7, len(data["days"]))
        check("names the sockets",
              [s["name"] for s in data["sockets"]] == ["Light", "Monitor", "Fan"],
              data["sockets"])
        check("every day has a value per socket",
              all(len(d["hours"]) == 3 for d in data["days"]), data["days"])
        check("days carry a weekday label",
              all(d["weekday"] for d in data["days"]), data["days"])

        # Days before the log starts must be null, not a misleading zero.
        early = data["days"][0]["hours"]
        check("days with no history are null, not zero",
              all(h is None for h in early), early)

        today = data["days"][-1]["hours"]
        check("today has real numbers",
              all(isinstance(h, (int, float)) for h in today), today)
        check("nothing exceeds 24 hours in a day",
              all(h is None or 0 <= h <= 24 for d in data["days"] for h in d["hours"]),
              data["days"])

        check("clamps a silly range", len(c.usage_daily(999)["days"]) == 31)
        check("clamps zero", len(c.usage_daily(0)["days"]) == 7)


def run():
    test_countdown()
    test_away()
    test_usage_daily()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
