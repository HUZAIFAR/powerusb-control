"""
Guards the switching hot path against creeping slow.

Every USB exchange on this strip costs a write settle plus a read, and they
land directly in the latency of a tap in the app and of a Siri command. This
counts the exchanges a single switch actually performs, so an innocent-looking
"just re-read the state" can never quietly triple it again.

Run:  python tests/test_latency.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from powerusb import server as srv                      # noqa: E402
from powerusb.device import PowerUSBError               # noqa: E402
from powerusb.events import EventLog                    # noqa: E402

_scratch = Path(tempfile.mkdtemp(prefix="pusb-lat-"))
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


class CountingStrip:
    """Fake strip that records every USB exchange it is asked to perform."""

    sockets = [False, False, False]
    eeprom = [False, False, False]
    ops = []                      # one entry per USB exchange

    def __init__(self, auto_open=False):
        pass

    @classmethod
    def reset(cls):
        cls.sockets = [False, False, False]
        cls.eeprom = [False, False, False]
        cls.ops = []

    def open(self):
        pass

    def close(self):
        pass

    # Each of these is one real request/response with the firmware.
    def get(self, i):
        CountingStrip.ops.append(f"read{i}")
        return CountingStrip.sockets[i - 1]

    def set(self, i, on):
        CountingStrip.ops.append(f"write{i}")
        CountingStrip.sockets[i - 1] = bool(on)
        CountingStrip.ops.append(f"verify{i}")      # set() reads back
        return CountingStrip.sockets[i - 1]

    def get_default(self, i, refresh=False):
        CountingStrip.ops.append(f"defread{i}")
        return CountingStrip.eeprom[i - 1]

    def set_default(self, i, on):
        # Mirrors the real driver: cached, so no traffic when already correct.
        if CountingStrip.eeprom[i - 1] == bool(on):
            return bool(on)
        CountingStrip.ops.append(f"defwrite{i}")
        CountingStrip.eeprom[i - 1] = bool(on)
        return bool(on)

    def states(self):
        CountingStrip.ops.extend(["read1", "read2", "read3"])
        return list(CountingStrip.sockets)

    def defaults(self):
        CountingStrip.ops.extend(["defread1", "defread2", "defread3"])
        return list(CountingStrip.eeprom)


def fresh(tmp):
    CountingStrip.reset()
    srv.PowerUSB = CountingStrip
    srv.is_present = lambda: True
    srv.STATE_PATH = Path(tmp) / "state.json"
    c = srv.StripController(["One", "Two", "Three"],
                            events=EventLog(Path(tmp) / "events.jsonl"))
    c.poll_once(boot_grace=0)
    return c


def run():
    print("exchanges per switch")
    with tempfile.TemporaryDirectory() as tmp:
        c = fresh(tmp)

        CountingStrip.ops = []
        c.set_socket(1, True)
        first = list(CountingStrip.ops)
        print(f"       first switch : {first}")
        # write + verify + one EEPROM write. The old code also did a full
        # three-socket re-read and an EEPROM read on top of this.
        check("a switch costs at most 4 exchanges", len(first) <= 4, first)
        check("it does not re-read the other sockets",
              "read2" not in first and "read3" not in first, first)

        CountingStrip.ops = []
        c.set_socket(1, False)
        second = list(CountingStrip.ops)
        print(f"       second switch: {second}")
        check("switching back is just as cheap", len(second) <= 4, second)

        # Setting a socket to the value it already has must not write EEPROM.
        c.set_socket(2, True)
        CountingStrip.ops = []
        c.set_socket(2, True)
        again = list(CountingStrip.ops)
        print(f"       redundant set: {again}")
        check("no EEPROM write when the default already matches",
              not any(o.startswith("defwrite") for o in again), again)

    print("reads are served from cache between polls")
    with tempfile.TemporaryDirectory() as tmp:
        c = fresh(tmp)
        c.live_states(force=True)
        CountingStrip.ops = []
        for _ in range(5):
            c.snapshot()            # what five GUI clients polling looks like
        check("repeat snapshots inside the TTL do no USB traffic",
              CountingStrip.ops == [], CountingStrip.ops)

    print("state stays correct after the optimisation")
    with tempfile.TemporaryDirectory() as tmp:
        c = fresh(tmp)
        c.set_socket(1, True)
        c.set_socket(3, True)
        snap = c.snapshot()
        check("reported state matches the hardware",
              [s["on"] for s in snap["sockets"]] == CountingStrip.sockets,
              (snap["sockets"], CountingStrip.sockets))
        check("EEPROM tracks the desired state",
              CountingStrip.eeprom == [True, False, True], CountingStrip.eeprom)

        c.set_socket(1, False)
        snap = c.snapshot()
        check("still matches after switching off",
              [s["on"] for s in snap["sockets"]] == CountingStrip.sockets,
              (snap["sockets"], CountingStrip.sockets))

    print("siri name resolution")
    with tempfile.TemporaryDirectory() as tmp:
        c = fresh(tmp)
        c.names = ["Socket 1", "Monitor", "Light and Power Strip"]
        cases = [
            ("monitor", 2), ("MONITOR", 2), ("Monitor", 2),
            ("light", 3), ("lights", 3), ("Light and Power Strip", 3),
            ("light and power strip", 3), ("2", 2), ("3", 3),
            ("banana", None), ("", None), ("9", None),
        ]
        for text, want in cases:
            got = c.resolve(text)
            check(f"resolve({text!r}) -> {want}", got == want, got)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
