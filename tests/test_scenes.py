"""
Scene links: one URL that switches several sockets together.

`/s/light+monitor/on` has to behave as a single group operation -- all of them
switched, one state save, one spoken reply -- and must refuse the whole request
if any named socket is unknown, rather than half-applying it.

Run:  python tests/test_scenes.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from powerusb import server as srv                      # noqa: E402
from powerusb.device import PowerUSBError               # noqa: E402
from powerusb.events import EventLog                    # noqa: E402

_scratch = Path(tempfile.mkdtemp(prefix="pusb-scene-"))
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


class FakeStrip:
    sockets = [False, False, False]
    eeprom = [False, False, False]
    saves = 0

    def __init__(self, auto_open=False):
        pass

    @classmethod
    def reset(cls):
        cls.sockets = [False, False, False]
        cls.eeprom = [False, False, False]

    def open(self): pass
    def close(self): pass
    def get(self, i): return FakeStrip.sockets[i - 1]

    def set(self, i, on):
        FakeStrip.sockets[i - 1] = bool(on)
        return FakeStrip.sockets[i - 1]

    def get_default(self, i, refresh=False): return FakeStrip.eeprom[i - 1]

    def set_default(self, i, on):
        FakeStrip.eeprom[i - 1] = bool(on)
        return bool(on)

    def states(self): return list(FakeStrip.sockets)
    def defaults(self): return list(FakeStrip.eeprom)


def fresh(tmp):
    FakeStrip.reset()
    srv.PowerUSB = FakeStrip
    srv.is_present = lambda: True
    srv.STATE_PATH = Path(tmp) / "state.json"
    c = srv.StripController(["Light", "Monitor", "Fan"],
                            events=EventLog(Path(tmp) / "events.jsonl"))
    c.poll_once(boot_grace=0)
    return c


def parse_target(controller, target):
    """Mirrors the server's URL target parsing, so the tests exercise it."""
    wanted = [t for t in target.replace("+", ",").split(",") if t.strip()]
    out = []
    for piece in wanted:
        n = controller.resolve(piece)
        if n is None:
            return None
        if n not in out:
            out.append(n)
    return out


def run():
    print("url target parsing")
    with tempfile.TemporaryDirectory() as tmp:
        c = fresh(tmp)
        cases = [
            ("light+monitor", [1, 2]),
            ("light,monitor", [1, 2]),
            ("monitor+light", [2, 1]),
            ("light+monitor+fan", [1, 2, 3]),
            ("1+2", [1, 2]),
            ("light+light", [1]),          # duplicates collapse
            ("light", [1]),
            ("light+banana", None),        # one bad name fails the whole thing
        ]
        for text, want in cases:
            got = parse_target(c, text)
            check(f"{text!r} -> {want}", got == want, got)

    print("group switching")
    with tempfile.TemporaryDirectory() as tmp:
        c = fresh(tmp)
        c.set_many([1, 2], True)
        check("both sockets on", FakeStrip.sockets == [True, True, False],
              FakeStrip.sockets)
        check("the third is untouched", FakeStrip.sockets[2] is False)
        check("EEPROM defaults follow both",
              FakeStrip.eeprom == [True, True, False], FakeStrip.eeprom)

        c.set_many([1, 2], False)
        check("both sockets off", FakeStrip.sockets == [False, False, False],
              FakeStrip.sockets)

        c.set_many([2, 1, 2], True)
        check("duplicates tolerated", FakeStrip.sockets == [True, True, False],
              FakeStrip.sockets)

    print("a bad socket number changes nothing")
    with tempfile.TemporaryDirectory() as tmp:
        c = fresh(tmp)
        try:
            c.set_many([1, 99], True)
            check("rejected", False, "no error raised")
        except PowerUSBError:
            check("rejected", True)
        check("nothing was switched before the error",
              FakeStrip.sockets == [False, False, False], FakeStrip.sockets)
        try:
            c.set_many([], True)
            check("empty group rejected", False, "no error raised")
        except PowerUSBError:
            check("empty group rejected", True)

    print("spoken replies")
    with tempfile.TemporaryDirectory() as tmp:
        c = fresh(tmp)
        c.set_many([1, 2], True)
        said = c.spoken_group([1, 2])
        check("two sockets, same state", said == "Light and Monitor are on.", said)

        c.set_socket(2, False)
        said = c.spoken_group([1, 2])
        check("mixed states listed separately",
              said == "Light is on. Monitor is off.", said)

        c.set_many([1, 2, 3], True)
        said = c.spoken_group([1, 2, 3])
        check("three sockets read as a list",
              said == "Light, Monitor and Fan are on.", said)

        said = c.spoken_group([1])
        check("single socket still reads singular", said == "Light is on.", said)

    print("scene queued while mains is out")
    with tempfile.TemporaryDirectory() as tmp:
        c = fresh(tmp)
        srv.is_present = lambda: False
        c.poll_once(boot_grace=0)
        check("controller sees the strip gone", not c.online)

        out = c.set_many([1, 2], True)
        check("accepted, not rejected", out.get("queued") is True, out.get("queued"))
        check("intent recorded for both", c.desired[:2] == [True, True], c.desired)

        srv.is_present = lambda: True
        c.poll_once(boot_grace=0)
        check("applied when power returns",
              FakeStrip.sockets == [True, True, False], FakeStrip.sockets)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
