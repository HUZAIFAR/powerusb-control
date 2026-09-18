"""
Scheduler tests. No hardware involved -- the controller is a stub that just
records what it was asked to do, which is exactly the contract the scheduler
depends on (it sets desired state, never touches USB itself).

Run:  python tests/test_schedule.py
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from powerusb.schedule import Scheduler, ScheduleError, Timer  # noqa: E402


class FakeController:
    """Stands in for StripController, recording calls."""

    def __init__(self):
        self.calls = []
        self.sources = []
        self.fail = False

    def set_socket(self, socket_no, on, source="app"):
        if self.fail:
            raise RuntimeError("strip exploded")
        self.calls.append((socket_no, on))
        self.sources.append(source)
        return {}


def new_scheduler(tmp):
    return Scheduler(FakeController(), Path(tmp) / "schedules.json")


PASS = FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def monday(hour, minute=0):
    """A known Monday (2026-09-14 is a Monday), local time."""
    return datetime(2026, 9, 14, hour, minute).astimezone()


def run():
    print("validation")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_scheduler(tmp)
        for bad, why in [
            ({"socket": 9, "action": "on", "time": "10:00"}, "socket out of range"),
            ({"socket": 1, "action": "melt", "time": "10:00"}, "bad action"),
            ({"socket": 1, "action": "on", "time": "25:00"}, "hour out of range"),
            ({"socket": 1, "action": "on", "time": "nope"}, "unparseable time"),
            ({"socket": 1, "action": "on", "time": "10:00", "days": [9]}, "bad day"),
        ]:
            try:
                s.add(bad)
                check(why, False, "was accepted")
            except ScheduleError:
                check(why, True)

        t = s.add({"socket": 2, "action": "on", "time": "7:5"})
        check("time normalised to HH:MM", t["time"] == "07:05", t["time"])

    print("firing")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_scheduler(tmp)
        s.add({"socket": 1, "action": "on", "time": "18:00"})
        # Added at 17:00, so the 18:00 slot is still ahead of us.
        s.timers[0].last_fired = None

        s.tick(now=monday(17, 59))
        check("does not fire early", s.controller.calls == [], s.controller.calls)

        s.tick(now=monday(18, 0))
        check("fires at the due minute", s.controller.calls == [(1, True)], s.controller.calls)
        check("action is tagged as coming from a timer",
              s.controller.sources == ["timer"], s.controller.sources)

        s.tick(now=monday(18, 1))
        s.tick(now=monday(18, 30))
        check("fires only once", s.controller.calls == [(1, True)], s.controller.calls)

    print("catch-up window")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_scheduler(tmp)
        s.add({"socket": 3, "action": "off", "time": "18:00"})
        s.timers[0].last_fired = None
        s.tick(now=monday(18, 30))          # 30 min late: inside the window
        check("late-but-recent timer still runs", s.controller.calls == [(3, False)],
              s.controller.calls)

    with tempfile.TemporaryDirectory() as tmp:
        s = new_scheduler(tmp)
        s.add({"socket": 3, "action": "off", "time": "18:00"})
        s.timers[0].last_fired = None
        s.tick(now=monday(23, 59))          # ~6h late: outside the window
        check("stale timer is skipped", s.controller.calls == [], s.controller.calls)
        check("stale timer is marked fired", s.timers[0].last_fired is not None)

    print("day selection")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_scheduler(tmp)
        # Weekdays only (Mon-Fri). 2026-09-14 is Monday, 2026-09-19 a Saturday.
        s.add({"socket": 1, "action": "on", "time": "08:00", "days": [0, 1, 2, 3, 4]})
        s.timers[0].last_fired = None
        saturday = datetime(2026, 9, 19, 8, 0).astimezone()
        s.tick(now=saturday)
        check("skips a day not in the list", s.controller.calls == [], s.controller.calls)

        s.timers[0].last_fired = None
        s.tick(now=monday(8, 0))
        check("runs on a listed day", s.controller.calls == [(1, True)], s.controller.calls)

    print("enable / disable")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_scheduler(tmp)
        created = s.add({"socket": 2, "action": "on", "time": "18:00"})
        s.update(created["id"], {"enabled": False})
        s.timers[0].last_fired = None
        s.tick(now=monday(18, 0))
        check("disabled timer does not fire", s.controller.calls == [], s.controller.calls)

    print("new timers do not fire retroactively")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_scheduler(tmp)
        s.add({"socket": 1, "action": "on", "time": "00:01"})
        s.tick()                             # "now" is whenever the test runs
        check("freshly added timer stays put", s.controller.calls == [], s.controller.calls)

    print("persistence")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedules.json"
        a = Scheduler(FakeController(), path)
        made = a.add({"socket": 1, "action": "off", "time": "23:30",
                      "days": [0, 1], "label": "bedtime"})
        b = Scheduler(FakeController(), path)
        check("timer survives a reload", len(b.timers) == 1, len(b.timers))
        check("fields survive a reload",
              b.timers[0].label == "bedtime" and b.timers[0].days == [0, 1]
              and b.timers[0].time == "23:30")
        b.delete(made["id"])
        c = Scheduler(FakeController(), path)
        check("delete is persisted", len(c.timers) == 0, len(c.timers))

    print("corrupt file does not stop startup")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedules.json"
        path.write_text("{ this is not json", encoding="utf-8")
        s = Scheduler(FakeController(), path)
        check("bad JSON tolerated", s.timers == [])
        path.write_text('{"timers": [{"socket": 99, "action": "on", "time": "10:00"},'
                        ' {"socket": 1, "action": "on", "time": "10:00"}]}', encoding="utf-8")
        s = Scheduler(FakeController(), path)
        check("invalid entries dropped, valid kept", len(s.timers) == 1, len(s.timers))

    print("a failing controller does not kill the scheduler")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_scheduler(tmp)
        s.controller.fail = True
        s.add({"socket": 1, "action": "on", "time": "18:00"})
        s.timers[0].last_fired = None
        s.tick(now=monday(18, 0))            # must not raise
        check("exception from controller is contained", True)

    print("next_due for the GUI")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_scheduler(tmp)
        t = Timer(socket=1, action="on", time="08:00", days=[])
        nxt = t.next_due(monday(9, 0))
        check("next occurrence is tomorrow when today has passed",
              nxt.day == 15 and nxt.hour == 8, nxt)
        nxt = t.next_due(monday(7, 0))
        check("next occurrence is today when still ahead",
              nxt.day == 14 and nxt.hour == 8, nxt)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
