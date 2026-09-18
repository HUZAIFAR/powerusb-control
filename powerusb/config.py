"""Configuration loading for the PowerUSB server and CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"

DEFAULTS: Dict[str, Any] = {
    # Bind address. 0.0.0.0 makes the GUI reachable from your phone on the LAN;
    # use 127.0.0.1 to restrict it to this PC only.
    "host": "0.0.0.0",
    "http_port": 8765,
    # Raw line-oriented TCP control port. Set to 0 to disable it.
    "tcp_port": 8766,
    # Optional shared secret. When set, HTTP requests need ?token=... or an
    # "X-Auth-Token" header, and TCP clients must send "AUTH <token>" first.
    # Leave empty for an open LAN setup.
    "token": "",
    # Friendly labels shown in the GUI, in socket order.
    "names": ["Socket 1", "Socket 2", "Socket 3"],
    # The URL people actually use to reach this, e.g. the Tailscale Serve
    # address. Shown in the startup banner and the GUI's Device tab. Leave
    # empty to fall back to this machine's LAN address.
    "public_url": "",
    # Rated watts per socket. This strip cannot measure power, so these are
    # what the energy estimate multiplies measured on-time by. 0 = unset.
    "watts": [0, 0, 0],
}


def load_config() -> Dict[str, Any]:
    """Read config.json, falling back to defaults for anything missing."""
    cfg = dict(DEFAULTS)
    cfg["names"] = list(DEFAULTS["names"])

    # A broken config must never stop the server from coming up. This runs
    # unattended and windowless, so exiting here would mean the strip is
    # simply uncontrollable after a reboot with nothing on screen to say why.
    # Carry the problem forward instead and let the caller log it loudly.
    cfg["config_error"] = None
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(user, dict):
                cfg.update(user)
            else:
                cfg["config_error"] = "config.json must contain a JSON object; using defaults"
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            cfg["config_error"] = f"config.json could not be read ({exc}); using defaults"

    # Environment overrides win, so a service wrapper can retune without edits.
    if os.environ.get("POWERUSB_HOST"):
        cfg["host"] = os.environ["POWERUSB_HOST"]
    if os.environ.get("POWERUSB_HTTP_PORT"):
        cfg["http_port"] = int(os.environ["POWERUSB_HTTP_PORT"])
    if os.environ.get("POWERUSB_TCP_PORT"):
        cfg["tcp_port"] = int(os.environ["POWERUSB_TCP_PORT"])
    if os.environ.get("POWERUSB_TOKEN"):
        cfg["token"] = os.environ["POWERUSB_TOKEN"]

    watts = list(cfg.get("watts") or [])
    while len(watts) < 3:
        watts.append(0)
    cleaned = []
    for w in watts[:3]:
        try:
            cleaned.append(max(0.0, min(3000.0, float(w))))
        except (TypeError, ValueError):
            cleaned.append(0.0)
    cfg["watts"] = cleaned

    names = list(cfg.get("names") or [])
    while len(names) < 3:
        names.append(f"Socket {len(names) + 1}")
    cfg["names"] = [str(n) for n in names[:3]]

    return cfg


def _merge_into_config(key: str, value) -> None:
    """Write one key back to config.json, preserving everything else."""
    data = {}
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    if not data:
        data = dict(DEFAULTS)
    data[key] = value
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(CONFIG_PATH)   # atomic, so a crash cannot truncate the config


def save_watts(watts: list) -> None:
    """Persist the per-socket rated watts used by the energy estimate."""
    _merge_into_config("watts", [float(w) for w in watts[:3]])


def save_names(names: list) -> None:
    """
    Persist socket labels back to config.json, preserving everything else.

    Only the names are touched, so hand-edited ports or tokens in the file
    survive a rename from the GUI.
    """
    _merge_into_config("names", [str(n) for n in names[:3]])


def write_default_config() -> Path:
    """Create config.json from the defaults if it does not exist yet."""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2) + "\n", encoding="utf-8")
    return CONFIG_PATH
