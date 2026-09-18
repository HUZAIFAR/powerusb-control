#!/usr/bin/env python3
"""
pusb -- command-line control for the PowerUSB power strip.

    pusb status                 show all sockets
    pusb on 1                   switch socket 1 on
    pusb off 1 3                switch sockets 1 and 3 off
    pusb toggle 2               flip socket 2
    pusb all on | all off       switch everything
    pusb watch                  live view, updates until Ctrl-C
    pusb info                   device and protocol diagnostics
    pusb defaults               show/set the power-up defaults

Only one process can hold the strip's USB handle at a time, so if the server
is running this talks to it over HTTP; otherwise it drives the USB device
directly. Use --direct to force the latter.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from powerusb.config import load_config  # noqa: E402
from powerusb.device import (  # noqa: E402
    PowerUSB,
    PowerUSBError,
    SOCKET_COUNT,
    find_strips,
    is_present,
)

GREEN, RED, DIM, BOLD, YELLOW, RESET = (
    "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[33m", "\033[0m",
)


# Windows consoles still default to a legacy code page, and a redirected pipe
# is worse again (cp1252), which would blow up on the status glyph. Ask for
# UTF-8, then fall back to ASCII if the stream still cannot represent it.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, OSError, ValueError):
        pass


def _glyph(preferred: str, fallback: str) -> str:
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        preferred.encode(enc)
        return preferred
    except (UnicodeEncodeError, LookupError):
        return fallback


DOT = _glyph("●", "*")


def supports_colour() -> bool:
    return sys.stdout.isatty()


def paint(text: str, colour: str) -> str:
    return f"{colour}{text}{RESET}" if supports_colour() else text


# --------------------------------------------------------------- server link


class ServerLink:
    """Thin client for the local server's JSON API."""

    def __init__(self, cfg: Dict[str, Any]):
        port = cfg["http_port"]
        self.base = f"http://127.0.0.1:{port}"
        self.token = cfg["token"]

    def _call(self, path: str, payload: Optional[Dict[str, Any]] = None,
              timeout: float = 4.0) -> Dict[str, Any]:
        url = self.base + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("X-Auth-Token", self.token)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def available(self) -> bool:
        try:
            self._call("/api/health", timeout=1.0)
            return True
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
            return False

    def state(self) -> Dict[str, Any]:
        return self._call("/api/state")

    def set(self, socket_no: int, on: bool) -> Dict[str, Any]:
        return self._call(f"/api/socket/{socket_no}", {"on": on})

    def toggle(self, socket_no: int) -> Dict[str, Any]:
        return self._call(f"/api/socket/{socket_no}", {"toggle": True})

    def all(self, on: bool) -> Dict[str, Any]:
        return self._call("/api/all", {"on": on})


# --------------------------------------------------------------- direct link


class DirectLink:
    """Fallback that drives the USB device itself when no server is running."""

    def __init__(self, cfg: Dict[str, Any]):
        self.names = cfg["names"]
        self.dev = PowerUSB(auto_open=False)

    def _snapshot(self) -> Dict[str, Any]:
        if not is_present():
            return {
                "online": False,
                "sockets": [
                    {"id": i + 1, "name": self.names[i], "on": False, "live": None}
                    for i in range(SOCKET_COUNT)
                ],
            }
        states = self.dev.states()
        return {
            "online": True,
            "sockets": [
                {"id": i + 1, "name": self.names[i], "on": states[i], "live": states[i]}
                for i in range(SOCKET_COUNT)
            ],
        }

    def available(self) -> bool:
        return True

    def state(self) -> Dict[str, Any]:
        return self._snapshot()

    def set(self, socket_no: int, on: bool) -> Dict[str, Any]:
        self.dev.set(socket_no, on)
        # Keep the power-up default aligned, same as the server does, so the
        # strip comes back correctly after the wall switch is cycled.
        self.dev.set_default(socket_no, on)
        return self._snapshot()

    def toggle(self, socket_no: int) -> Dict[str, Any]:
        return self.set(socket_no, not self.dev.get(socket_no))

    def all(self, on: bool) -> Dict[str, Any]:
        for i in range(1, SOCKET_COUNT + 1):
            self.set(i, on)
        return self._snapshot()


def get_link(cfg: Dict[str, Any], force_direct: bool = False) -> Any:
    if not force_direct:
        server = ServerLink(cfg)
        if server.available():
            return server
    return DirectLink(cfg)


# ------------------------------------------------------------------ printing


def render(snap: Dict[str, Any], via: str) -> str:
    lines = []
    if not snap.get("online", False):
        lines.append(
            paint("  strip OFFLINE", RED)
            + paint("  -- no mains power (wall switch off?)", DIM)
        )
        lines.append(paint("  showing the state it will return to:", DIM))
    for s in snap["sockets"]:
        on = s["on"]
        dot = paint(DOT, GREEN if on else RED)
        label = paint("ON ", GREEN + BOLD) if on else paint("OFF", DIM)
        pending = ""
        if not snap.get("online", False):
            pending = paint("  (pending)", YELLOW)
        lines.append(f"  {dot}  {s['id']}  {s['name']:<18} {label}{pending}")
    lines.append(paint(f"  via {via}", DIM))
    return "\n".join(lines)


def show(snap: Dict[str, Any], via: str) -> None:
    print(render(snap, via))
    if snap.get("error"):
        print(paint(f"  error: {snap['error']}", RED))


def link_name(link: Any) -> str:
    return "server" if isinstance(link, ServerLink) else "direct USB"


# ------------------------------------------------------------------ commands


def parse_sockets(values: List[str]) -> List[int]:
    out = []
    for v in values:
        try:
            n = int(v)
        except ValueError:
            raise SystemExit(f"not a socket number: {v!r}")
        if not 1 <= n <= SOCKET_COUNT:
            raise SystemExit(f"socket must be 1..{SOCKET_COUNT}, got {n}")
        out.append(n)
    return out


def cmd_info(cfg: Dict[str, Any]) -> int:
    print(paint("PowerUSB diagnostics", BOLD))
    strips = find_strips()
    print(f"  attached interfaces : {len(strips)}")
    for s in strips:
        print(f"    {s['manufacturer_string']} / {s['product_string']}")
        print(f"    VID:PID {s['vendor_id']:04X}:{s['product_id']:04X}  "
              f"usage_page 0x{s['usage_page']:04X}")
    print(f"  mains power         : {'on' if is_present() else paint('OFF', RED)}")

    server = ServerLink(cfg)
    print(f"  server              : {'running' if server.available() else 'not running'}")
    print(f"  http                : http://127.0.0.1:{cfg['http_port']}/")
    print(f"  tcp                 : port {cfg['tcp_port']}")

    if is_present() and not server.available():
        try:
            with PowerUSB() as dev:
                print(f"  live states         : {dev.states()}")
                print(f"  power-up defaults   : {dev.defaults()}")
        except PowerUSBError as exc:
            print(paint(f"  could not read: {exc}", RED))
    return 0


def cmd_defaults(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    """Inspect or set what the strip does when mains power comes back."""
    if not is_present():
        print(paint("strip has no mains power; cannot read defaults", RED))
        return 1
    server = ServerLink(cfg)
    if server.available():
        print(paint("the server holds the USB handle; stop it first "
                    "(the server keeps defaults in sync automatically)", YELLOW))
        return 1
    try:
        with PowerUSB() as dev:
            if args.set is not None:
                on = args.set == "on"
                for i in range(1, SOCKET_COUNT + 1):
                    dev.set_default(i, on)
            defs = dev.defaults()
            print(paint("power-up defaults (state after the wall switch returns):", BOLD))
            for i, d in enumerate(defs):
                dot = paint(DOT, GREEN if d else RED)
                print(f"  {dot}  {i + 1}  {cfg['names'][i]:<18} "
                      f"{paint('ON', GREEN) if d else paint('OFF', DIM)}")
    except PowerUSBError as exc:
        print(paint(f"error: {exc}", RED))
        return 1
    return 0


def cmd_watch(link: Any, via: str) -> int:
    try:
        while True:
            snap = link.state()
            block = render(snap, via)
            print("\033[2J\033[H" + paint("PowerUSB  (Ctrl-C to stop)", BOLD) + "\n")
            print(block)
            time.sleep(1.0)
    except KeyboardInterrupt:
        print()
        return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="pusb",
        description="Control the PowerUSB power strip sockets 1-3.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--direct", action="store_true",
                   help="bypass the server and drive the USB device directly")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("status", help="show all sockets")
    for name, helptext in (("on", "switch sockets on"),
                           ("off", "switch sockets off"),
                           ("toggle", "flip sockets")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("sockets", nargs="+", help=f"socket numbers 1..{SOCKET_COUNT}")

    sp_all = sub.add_parser("all", help="switch every socket")
    sp_all.add_argument("state", choices=["on", "off"])

    sub.add_parser("watch", help="live view until Ctrl-C")
    sub.add_parser("info", help="device and protocol diagnostics")

    sp_def = sub.add_parser("defaults", help="show or set the power-up defaults")
    sp_def.add_argument("--set", choices=["on", "off"], default=None,
                        help="set every socket's power-up default")

    args = p.parse_args(argv)
    cfg = load_config()

    if args.cmd == "info":
        return cmd_info(cfg)
    if args.cmd == "defaults":
        return cmd_defaults(cfg, args)

    try:
        link = get_link(cfg, force_direct=args.direct)
        via = link_name(link)

        if args.cmd in (None, "status"):
            show(link.state(), via)
            return 0
        if args.cmd == "watch":
            return cmd_watch(link, via)
        if args.cmd == "all":
            show(link.all(args.state == "on"), via)
            return 0
        if args.cmd in ("on", "off", "toggle"):
            snap = None
            for n in parse_sockets(args.sockets):
                if args.cmd == "toggle":
                    snap = link.toggle(n)
                else:
                    snap = link.set(n, args.cmd == "on")
            show(snap, via)
            return 0
    except PowerUSBError as exc:
        print(paint(f"error: {exc}", RED), file=sys.stderr)
        return 1
    except urllib.error.HTTPError as exc:
        print(paint(f"server rejected the request: {exc}", RED), file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(paint(f"could not reach the server: {exc}", RED), file=sys.stderr)
        return 1

    p.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
