"""
Wall-switch behaviour tests.

The strip is fed from a switched wall outlet, so cutting the switch kills the
USB controller too. The fake below models that faithfully, including the part
that makes the whole design work: on power-up the firmware restores each
socket from its EEPROM *default*, with no host involvement.

Run:  python tests/test_mains.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from powerusb import server as srv                      # noqa: E402
from powerusb.device import PowerUSBError               # noqa: E402
from powerusb.events import EventLog                    # noqa: E402

# Keep the test out of the real project's log files.
_scratch = Path(tempfile.mkdtemp(prefix="pusb-test-"))
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
    """A PowerUSB strip whose mains supply can be cut."""

    powered = True          # class-level: the "wall switch"

    def __init__(self, auto_open=False):
        self.opened = False

    # the shared hardware state lives on the class so a new handle sees it
    sockets = [False, False, False]
    eeprom = [False, False, False]
    writes = 0              # counts EEPROM writes, to prove we avoid wear

    def _live(self):
        if not FakeStrip.powered:
            raise PowerUSBError("strip has no power")

    def open(self):
        self._live()
        self.opened = True

    def close(self):
        self.opened = False

    def get(self, i):
        self._live()
        return FakeStrip.sockets[i - 1]

    def set(self, i, on):
        self._live()
        FakeStrip.sockets[i - 1] = bool(on)
        return FakeStrip.sockets[i - 1]

    def get_default(self, i):
        self._live()
        return FakeStrip.eeprom[i - 1]

    def set_default(self, i, on):
        self._live()
        if FakeStrip.eeprom[i - 1] != bool(on):
            FakeStrip.eeprom[i - 1] = bool(on)
            FakeStrip.writes += 1
        return FakeStrip.eeprom[i - 1]

    def states(self):
        self._live()
        return list(FakeStrip.sockets)

    def defaults(self):
        self._live()
        return list(FakeStrip.eeprom)

    # ------------------------------------------------- the wall switch

    @classmethod
    def cut_mains(cls):
        cls.powered = False
        cls.sockets = [False, False, False]      # everything goes dead

    @classmethod
    def restore_mains(cls):
        cls.powered = True
        # This is the crucial firmware behaviour: sockets come back at their
        # stored defaults, before any host software has said a word.
        cls.sockets = list(cls.eeprom)


def fresh(tmp, desired=None):
    """A controller wired to a fresh fake strip."""
    FakeStrip.powered = True
    FakeStrip.sockets = [False, False, False]
    FakeStrip.eeprom = [False, False, False]
    FakeStrip.writes = 0

    srv.PowerUSB = FakeStrip
    srv.is_present = lambda: FakeStrip.powered
    srv.STATE_PATH = Path(tmp) / "state.json"

    c = srv.StripController(["One", "Two", "Three"],
                            events=EventLog(Path(tmp) / "events.jsonl"))
    if desired:
        c.desired = list(desired)
    return c


def run():
    print("normal operation")
    with tempfile.TemporaryDirectory() as tmp:
        c = fresh(tmp)
        c.poll_once(boot_grace=0)
        check("comes online when the strip is present", c.online)

        c.set_socket(1, True)
        check("socket switches", FakeStrip.sockets == [True, False, False], FakeStrip.sockets)
        check("EEPROM default follows the change",
              FakeStrip.eeprom == [True, False, False], FakeStrip.eeprom)

        before = FakeStrip.writes
        c.set_socket(1, True)           # same value again
        check("redundant EEPROM write is skipped", FakeStrip.writes == before,
              f"{before} -> {FakeStrip.writes}")

    print("wall switch off")
    with tempfile.TemporaryDirectory() as tmp:
        c = fresh(tmp)
        c.poll_once(boot_grace=0)
        c.set_socket(2, True)

        FakeStrip.cut_mains()
        c.poll_once(boot_grace=0)
        check("detects the strip disappearing", not c.online)

        snap = c.snapshot()
        check("reports offline to clients", snap["online"] is False)
        check("still reports the intended state",
              [s["on"] for s in snap["sockets"]] == [False, True, False],
              [s["on"] for s in snap["sockets"]])
        check("marks live readings as unknown",
              all(s["live"] is None for s in snap["sockets"]))

        # Commands while dark must be accepted, not rejected.
        out = c.set_socket(3, True)
        check("accepts commands while dark", out.get("queued") is True, out.get("queued"))
        check("records the queued intent", c.desired == [False, True, True], c.desired)
        check("no error surfaced to the user", "error" not in out, out.get("error"))

    print("wall switch back on")
    with tempfile.TemporaryDirectory() as tmp:
        c = fresh(tmp)
        c.poll_once(boot_grace=0)
        c.set_socket(1, True)
        c.set_socket(2, True)

        FakeStrip.cut_mains()
        c.poll_once(boot_grace=0)
        c.set_socket(2, False)          # queued while dark
        c.set_socket(3, True)           # queued while dark

        FakeStrip.restore_mains()
        # Before the host does anything, the firmware alone has restored the
        # sockets it knew about at cut time.
        check("firmware restores sockets on its own, with no host help",
              FakeStrip.sockets == [True, True, False], FakeStrip.sockets)

        c.poll_once(boot_grace=0)
        check("comes back online", c.online)
        check("queued changes are applied",
              FakeStrip.sockets == [True, False, True], FakeStrip.sockets)
        check("EEPROM re-synced for the next power cut",
              FakeStrip.eeprom == [True, False, True], FakeStrip.eeprom)

        snap = c.snapshot()
        check("clients see the reconciled state",
              [s["on"] for s in snap["sockets"]] == [True, False, True],
              [s["on"] for s in snap["sockets"]])

    print("repeated switching, as happens through a day")
    with tempfile.TemporaryDirectory() as tmp:
        c = fresh(tmp)
        c.poll_once(boot_grace=0)
        c.set_socket(1, True)
        for cycle in range(6):
            FakeStrip.cut_mains()
            c.poll_once(boot_grace=0)
            FakeStrip.restore_mains()
            c.poll_once(boot_grace=0)
        check("survives six power cycles", c.online)
        check("state held across all of them",
              FakeStrip.sockets == [True, False, False], FakeStrip.sockets)
        check("no EEPROM churn from cycling alone", FakeStrip.writes == 1,
              f"{FakeStrip.writes} writes")

    print("state survives a server restart")
    with tempfile.TemporaryDirectory() as tmp:
        c = fresh(tmp)
        c.poll_once(boot_grace=0)
        c.set_socket(3, True)

        # Simulate a restart: new controller, same state file, strip untouched.
        c2 = srv.StripController(["One", "Two", "Three"],
                                 events=EventLog(Path(tmp) / "events.jsonl"))
        check("desired state reloaded from disk", c2.desired == [False, False, True],
              c2.desired)
        c2.poll_once(boot_grace=0)
        check("hardware reconciled after restart",
              FakeStrip.sockets == [False, False, True], FakeStrip.sockets)

    print("strip present but refusing to open")
    with tempfile.TemporaryDirectory() as tmp:
        c = fresh(tmp)

        def refuse():
            raise PowerUSBError("busy")
        original = FakeStrip.open
        FakeStrip.open = lambda self: refuse()
        srv.is_present = lambda: True
        c.poll_once(boot_grace=0)
        check("stays offline rather than crashing", not c.online)
        FakeStrip.open = original
        c.poll_once(boot_grace=0)
        check("recovers once it opens again", c.online)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
