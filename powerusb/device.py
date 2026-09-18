"""
Low-level driver for the PowerUSB (pwrusb.com) USB-controlled power strip.

Hardware
--------
The strip presents itself as a raw vendor-defined HID device:

    VID 0x04D8  PID 0x003F   (Microchip stack, reports as "Simple HID Device Demo")
    Usage page 0xFF00, usage 0x01

Wire protocol
-------------
Every exchange is a 64-byte HID report. On Windows, hidapi requires the
Report ID to be prepended, so we actually write 65 bytes with a leading
0x00 (this device does not use numbered reports). Unused bytes are padded
with 0xFF.

    socket   ON     OFF    READ-STATE   DEF-ON  DEF-OFF  READ-DEFAULT
    1        'A'    'B'    0xA1         'N'     'F'      0xA3
    2        'C'    'D'    0xA2         'G'     'Q'      0xA4
    3        'E'    'P'    0xAC         'O'     'H'      0xAD

The DEF-* commands write the socket's power-up default into the strip's
EEPROM. That matters a lot here: this strip is fed from a switched wall
outlet, so when the wall switch is flipped off the whole controller loses
power and the USB device disappears from the host entirely. When mains
returns, the strip's own firmware restores each socket to its stored
default before the host has even noticed the device re-enumerate. Keeping
the defaults in step with the desired state is therefore what makes the
strip come back correctly even if this PC is asleep, rebooting, or off.

Two firmware quirks worth knowing, because they look like typos but are not:
  * socket 3's OFF byte is 'P' (0x50), NOT 'F' -- the sequence breaks here.
  * socket 3's read command is 0xAC, NOT 0xA3.
Both were verified empirically against the attached device.

A read-state command is answered with a 64-byte report whose first byte is
1 (on) or 0 (off). The remaining bytes are stale firmware buffer contents
and must be ignored.

Only one process may hold the HID handle at a time, so the server owns the
device and the CLI talks to the server when it is running.
"""

from __future__ import annotations

import threading
import time
from typing import List, Optional

import hid

VENDOR_ID = 0x04D8
PRODUCT_ID = 0x003F

# Indexed 0..2 for sockets 1..3.
_CMD_ON = (0x41, 0x43, 0x45)      # 'A', 'C', 'E'
_CMD_OFF = (0x42, 0x44, 0x50)     # 'B', 'D', 'P'  <- note 'P' for socket 3
_CMD_READ = (0xA1, 0xA2, 0xAC)    # <- note 0xAC for socket 3

# Persistent power-up defaults (stored in the strip's EEPROM).
_CMD_DEF_ON = (0x4E, 0x47, 0x4F)   # 'N', 'G', 'O'
_CMD_DEF_OFF = (0x46, 0x51, 0x48)  # 'F', 'Q', 'H'
_CMD_DEF_READ = (0xA3, 0xA4, 0xAD)

SOCKET_COUNT = 3

# The firmware needs a breath between a write and the matching read, and
# a little longer for the relay to actually settle before it reports back.
_WRITE_SETTLE = 0.03
_RELAY_SETTLE = 0.12
_READ_TIMEOUT_MS = 1000


class PowerUSBError(RuntimeError):
    """Raised when the strip cannot be reached or gives an unusable answer."""


def _check_socket(socket: int) -> int:
    """Validate a 1-based socket number and return its 0-based index."""
    if not isinstance(socket, int) or isinstance(socket, bool):
        raise PowerUSBError(f"socket must be an integer, got {socket!r}")
    if not 1 <= socket <= SOCKET_COUNT:
        raise PowerUSBError(f"socket must be 1..{SOCKET_COUNT}, got {socket}")
    return socket - 1


class PowerUSB:
    """
    Thread-safe handle on the power strip.

    All public methods serialise on a single lock, so this object is safe to
    share between the HTTP threads and the TCP threads of the server. If the
    strip is unplugged and replugged, operations transparently reopen the
    handle once before giving up.
    """

    def __init__(self, auto_open: bool = True):
        self._dev: Optional[hid.device] = None
        self._lock = threading.RLock()
        # EEPROM defaults barely ever change, but set_default() is called on
        # every switch. Caching them turns that from a USB read (plus the
        # latency it adds to every Siri command) into a dict lookup.
        self._defaults: List[Optional[bool]] = [None] * SOCKET_COUNT
        if auto_open:
            self.open()

    # ---------------------------------------------------------------- plumbing

    def open(self) -> None:
        with self._lock:
            if self._dev is not None:
                return
            dev = hid.device()
            try:
                dev.open(VENDOR_ID, PRODUCT_ID)
            except OSError as exc:
                raise PowerUSBError(
                    f"cannot open PowerUSB strip ({VENDOR_ID:04X}:{PRODUCT_ID:04X}): {exc}. "
                    "Is it plugged in, and is another program (the server?) already using it?"
                ) from exc
            dev.set_nonblocking(0)
            self._dev = dev

    def close(self) -> None:
        with self._lock:
            if self._dev is not None:
                try:
                    self._dev.close()
                finally:
                    self._dev = None
            # The strip may be power-cycled while we are closed, so nothing
            # cached about it can be trusted on the next open.
            self._defaults = [None] * SOCKET_COUNT

    @property
    def is_open(self) -> bool:
        return self._dev is not None

    def _write(self, byte: int) -> None:
        """Send a single command byte as a padded 64-byte report."""
        report = bytes([0x00, byte]) + b"\xff" * 63
        self._dev.write(report)
        time.sleep(_WRITE_SETTLE)

    def _exchange(self, byte: int, expect_reply: bool) -> Optional[int]:
        """One command, optionally reading the reply's first byte."""
        self._write(byte)
        if not expect_reply:
            return None
        reply = self._dev.read(64, timeout_ms=_READ_TIMEOUT_MS)
        if not reply:
            raise PowerUSBError(f"no reply to command 0x{byte:02X} (strip stopped responding)")
        return reply[0]

    def _do(self, byte: int, expect_reply: bool) -> Optional[int]:
        """_exchange with one transparent reopen-and-retry on I/O failure."""
        with self._lock:
            self.open()
            try:
                return self._exchange(byte, expect_reply)
            except (OSError, ValueError, PowerUSBError):
                # Most likely the strip was unplugged or the handle went stale.
                self.close()
                self.open()
                try:
                    return self._exchange(byte, expect_reply)
                except (OSError, ValueError) as exc:
                    raise PowerUSBError(f"command 0x{byte:02X} failed: {exc}") from exc

    # ------------------------------------------------------------------- api

    def get(self, socket: int) -> bool:
        """True if the given socket (1..3) is currently powered."""
        idx = _check_socket(socket)
        raw = self._do(_CMD_READ[idx], expect_reply=True)
        if raw not in (0, 1):
            raise PowerUSBError(f"socket {socket}: unexpected state byte {raw!r}")
        return raw == 1

    def set(self, socket: int, on: bool) -> bool:
        """Switch a socket on or off. Returns the state read back afterwards."""
        idx = _check_socket(socket)
        # Held across the write AND the verify read, otherwise a concurrent
        # caller could switch the same socket in between and we would report
        # its result as ours.
        with self._lock:
            self._do(_CMD_ON[idx] if on else _CMD_OFF[idx], expect_reply=False)
            time.sleep(_RELAY_SETTLE)
            return self.get(socket)

    def toggle(self, socket: int) -> bool:
        """Flip a socket. Returns the new state."""
        with self._lock:
            return self.set(socket, not self.get(socket))

    def states(self) -> List[bool]:
        """Read all three sockets, in socket order."""
        with self._lock:
            return [self.get(i) for i in range(1, SOCKET_COUNT + 1)]

    def set_all(self, on: bool) -> List[bool]:
        """Switch every socket the same way. Returns the states read back."""
        with self._lock:
            for i in range(1, SOCKET_COUNT + 1):
                idx = i - 1
                self._do(_CMD_ON[idx] if on else _CMD_OFF[idx], expect_reply=False)
            time.sleep(_RELAY_SETTLE)
            return self.states()

    # ------------------------------------------------- power-up defaults

    def get_default(self, socket: int, refresh: bool = False) -> bool:
        """
        The state this socket returns to when mains power is restored.

        Served from cache unless refresh is set; only this class writes the
        defaults, so the cache cannot go stale behind our back.
        """
        idx = _check_socket(socket)
        with self._lock:
            if not refresh and self._defaults[idx] is not None:
                return self._defaults[idx]
            raw = self._do(_CMD_DEF_READ[idx], expect_reply=True)
            if raw not in (0, 1):
                raise PowerUSBError(f"socket {socket}: unexpected default byte {raw!r}")
            self._defaults[idx] = (raw == 1)
            return self._defaults[idx]

    def set_default(self, socket: int, on: bool) -> bool:
        """
        Store this socket's power-up default in the strip's EEPROM.

        Skipped when the stored value already matches, because EEPROM cells
        have a finite write endurance and this gets called on every change.
        """
        idx = _check_socket(socket)
        with self._lock:
            if self.get_default(socket) == on:
                return on           # already right: no USB traffic at all
            self._do(_CMD_DEF_ON[idx] if on else _CMD_DEF_OFF[idx], expect_reply=False)
            time.sleep(_RELAY_SETTLE)
            self._defaults[idx] = None          # force a genuine verify read
            return self.get_default(socket, refresh=True)

    def defaults(self) -> List[bool]:
        """Read all three power-up defaults, in socket order."""
        with self._lock:
            return [self.get_default(i) for i in range(1, SOCKET_COUNT + 1)]

    # ------------------------------------------------------- diagnostics

    # Identity and metering commands. Which of these the firmware actually
    # answers depends on the model; the Basic strip ignores several.
    _CMD_MODEL = 0xAA
    _CMD_FIRMWARE = 0xA7
    _CMD_CURRENT = 0xB1

    def read_model(self) -> int:
        """Model code: 1 = Basic, 2 = Digital IO, 3 = Watchdog, 4 = Smart."""
        return self._do(self._CMD_MODEL, expect_reply=True)

    def read_firmware(self) -> str:
        with self._lock:
            self._write(self._CMD_FIRMWARE)
            reply = self._dev.read(64, timeout_ms=_READ_TIMEOUT_MS)
            if not reply:
                return "unknown"
            return f"{reply[0]}.{reply[1]}"

    # Only the Smart model (4) has a current sensor. Verified on this unit:
    # model 1 replies to 0xB1, but the bytes are stale buffer contents that
    # flip between fixed values while the load is unchanged -- so the reply
    # is meaningless and must not be shown as a reading.
    MODEL_NAMES = {1: "Basic", 2: "Digital IO", 3: "Watchdog", 4: "Smart"}
    METERING_MODELS = (4,)

    def has_metering(self) -> bool:
        try:
            return self.read_model() in self.METERING_MODELS
        except PowerUSBError:
            return False

    def read_current_ma(self) -> Optional[int]:
        """Instantaneous draw in mA, or None when this model cannot measure."""
        if not self.has_metering():
            return None
        with self._lock:
            self._write(self._CMD_CURRENT)
            reply = self._dev.read(64, timeout_ms=_READ_TIMEOUT_MS)
            if not reply or len(reply) < 2:
                return None
            return (reply[0] << 8) | reply[1]

    # --------------------------------------------------------- context manager

    def __enter__(self) -> "PowerUSB":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def find_strips() -> list:
    """Every PowerUSB interface currently attached (useful for diagnostics)."""
    return hid.enumerate(VENDOR_ID, PRODUCT_ID)


def is_present() -> bool:
    """
    True if the strip is enumerated on USB right now.

    Cheap enough to poll once a second. When the wall switch is off the strip
    is unpowered and vanishes from USB, so this doubles as a mains-power
    detector without having to open the device or push any traffic at it.
    """
    try:
        return bool(hid.enumerate(VENDOR_ID, PRODUCT_ID))
    except OSError:
        return False
