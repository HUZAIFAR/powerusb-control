# PowerUSB control

Control the three switchable sockets on a PowerUSB strip from a web GUI, your
phone, the command line, or any script that can open a TCP socket.

Built and verified against the actual attached unit:

```
VID:PID   04D8:003F   (raw HID, vendor usage page 0xFF00)
Reports as "Microchip Technology Inc. / Simple HID Device Demo"
Model 1 (Basic), firmware 3.5
```

---

## Quick start

The app is reached over **Tailscale only**:

```
https://your-pc.your-tailnet.ts.net:9443/
```

The server binds `127.0.0.1` and is proxied by Tailscale Serve, so it is
reachable from any device on your tailnet — including away from home — and
from nowhere else. No firewall rules, no ports open to the LAN or internet.

Port 9443 is deliberate: Tailscale Funnel can only use 443/8443/10000, so the
power control can never be exposed publicly, even by accident. The existing
Funnel config on 443 is untouched.

If Serve ever needs recreating:

```bash
tailscale serve --bg --https=9443 http://127.0.0.1:8765
```

To install autostart, run **once as Administrator**:

```powershell
powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1
```

That registers a Scheduled Task which starts the server **at boot** (before
anyone logs in) and restarts it automatically if it ever stops. It verifies the
server actually answers before reporting success, and tries SYSTEM then your
user account.

**The Store-alias trap:** `python.exe`/`pythonw.exe`/`py.exe` on `PATH` here are
0-byte AppExecLink reparse points (Microsoft Store aliases). An interactive user
can launch them; a task running as SYSTEM cannot, and it fails silently with no
log at all. The installer therefore finds the real interpreter through the PEP
514 registry keys and refuses anything under `\WindowsApps\`.

---

## The wall-switch problem

This strip is fed from a switched wall outlet. When that switch is off the
strip's USB controller loses power too, so the device disappears from Windows
entirely — no software can talk to it. Two mechanisms make that seamless:

**1. The strip restores itself.** Every change is also written to the strip's
EEPROM power-up defaults (`N/F`, `G/Q`, `O/H`). When mains returns, the
firmware restores each socket from those defaults *before the PC is involved
at all* — so it works even if the machine is asleep, rebooting, or off.

**2. The server reconciles.** A monitor thread watches USB enumeration. The
strip vanishing is read as "mains off"; reappearing as "mains on", at which
point the saved desired state is re-applied to be certain.

Commands issued while the strip is dark are **not** errors. They are recorded
and applied the moment power returns — the GUI shows them as *pending*.

Verified in `tests/test_mains.py`, including six consecutive power cycles.

---

## What this hardware can and cannot do

| Capability | This unit |
|---|---|
| Switch sockets 1–3 | yes |
| Remember a power-up default per socket | yes |
| Measure current / wattage | **no** — Smart model only |
| Overload threshold | **no** — no reply from firmware |
| Watchdog auto-reboot | **no** — Watchdog model only |
| Digital IO | **no** — Digital IO model only |

The Basic model has no current sensor. It *does* reply to the read-current
command (`0xB1`), but the bytes are stale buffer contents that flip between
fixed values while the load is unchanged, so the driver deliberately returns
`None` rather than present a fabricated reading.

The **energy estimate** in the Device tab is therefore exactly that: you enter
each socket's rated watts, and it multiplies them by the on-time actually
recorded in the activity log. No PowerUSB model meters per-socket — even the
Smart model has a single sensor for the whole strip.

---

## The GUI

Four tabs, mobile-first, and installable as a home-screen app:

- **Control** — the three sockets, tap to toggle, tap the pencil to rename.
- **Timers** — recurring on/off schedules, per socket, per weekday.
- **Log** — every switch, timer firing and mains power change, grouped by day.
- **Device** — model/firmware, the LAN link to open on your phone, and the
  energy estimate.

To install on a phone: open the LAN URL, then *Add to Home Screen* (Safari) or
*Install app* (Chrome).

---

## Siri, Home Screen and Lock Screen (iOS)

iOS does **not** let a web app add a Lock Screen widget or register a Siri
phrase. Both need WidgetKit / App Intents, which means a native app compiled in
Xcode. There is no web API for either. Apple Shortcuts is the supported way to
get the same result.

The `/s/` endpoints exist for exactly this: plain `GET`, plain text reply, so a
Shortcut is a single **Get Contents of URL** action with nothing to configure.

```
/s/<name>/on        /s/<name>/off        /s/<name>/toggle
/s/all/on           /s/all/off           /s/status
```

`<name>` matches loosely — `light`, `lights`, `LIGHT` and `1` all reach the
same socket. Every reply is a short sentence ("Monitor is on."), so a
**Speak Text** action after it makes Siri read the result back.

The Device tab in the app lists every URL with a copy button.

**Make a Siri phrase** — Shortcuts app → `+` → Add Action → *Get Contents of
URL* → paste the link → rename the shortcut to the words you want to say. The
shortcut's **name is the phrase**.

**Home Screen icon** — in Shortcuts, `⋯` on the shortcut → Share → *Add to
Home Screen*.

**Lock Screen widget** — long-press the Lock Screen → Customise → tap the
widget row under the clock → Shortcuts → pick it.

**Lock Screen / Control Centre button (iOS 18+)** — Settings → Control Centre →
add a control → Shortcuts; or replace the torch/camera button in Lock Screen
customisation.

Tailscale must be on for these to reach the server, at home as well as away,
since the server is loopback-only.

---

## Command line

```bash
python pusb.py status          # show all sockets
python pusb.py on 1            # switch socket 1 on
python pusb.py off 1 3         # switch 1 and 3 off
python pusb.py toggle 2        # flip socket 2
python pusb.py all on          # everything on
python pusb.py watch           # live view
python pusb.py info            # device diagnostics
python pusb.py defaults        # show the power-up defaults
```

Only one process can hold the strip's USB handle, so the CLI talks to the
server over HTTP when it is running and drives the device directly when it is
not. `--direct` forces the latter.

---

## HTTP API

| Method | Path | Body / query |
|---|---|---|
| GET | `/api/state` | — |
| POST | `/api/socket/<n>` | `{"on":true}` or `{"toggle":true}` |
| POST | `/api/socket/<n>/name` | `{"name":"Desk lamp"}` |
| POST | `/api/socket/<n>/watts` | `{"watts":32}` |
| POST | `/api/all` | `{"on":false}` |
| GET | `/api/timers` | — |
| POST | `/api/timers` | `{"socket":1,"action":"on","time":"18:30","days":[0,1,2,3,4]}` |
| POST | `/api/timers/<id>` | any subset of the above |
| DELETE | `/api/timers/<id>` | — |
| GET | `/api/log` | `?limit=250` |
| POST | `/api/log/clear` | — |
| GET | `/api/usage` | `?hours=24` |
| GET | `/api/diag` | — |

`days` is `0`=Monday … `6`=Sunday; an empty list means every day.

```bash
curl -X POST -H 'Content-Type: application/json' \
     -d '{"on":true}' http://192.168.1.50:8765/api/socket/1
```

---

## TCP control

Line protocol on port 8766, for scripts and home-automation kit:

```
STATUS            -> OK online 0 1 0
ON 1 | OFF 1 | TOGGLE 1
ALL ON | ALL OFF
NAMES | PING | QUIT
```

`STATUS` answers `OK offline …` while mains is out, reporting the state the
strip will return to.

---

## Configuration

`config.json`:

Copy `config.example.json` to `config.json` (gitignored) and edit it:

```json
{
  "host": "127.0.0.1",
  "http_port": 8765,
  "tcp_port": 8766,
  "token": "",
  "names": ["Socket 1", "Socket 2", "Socket 3"],
  "watts": [0, 0, 0]
}
```

`token` is optional. Leave it empty on a trusted home LAN; set it and the API
requires `X-Auth-Token` (or `?token=` once, which the GUI then remembers).

**Security note:** the server deliberately sends no `Access-Control-Allow-Origin`
header. The GUI is same-origin so it needs none, and a wildcard would let any
website you happen to visit switch your sockets.

---

## Files

```
powerusb/device.py    HID driver: the wire protocol, locking, reconnection
powerusb/server.py    HTTP + TCP server, mains monitor, state reconciliation
powerusb/schedule.py  recurring timers with catch-up after downtime
powerusb/events.py    the activity log
powerusb/config.py    config.json handling
pusb.py               command line client
web/index.html        the GUI (no external dependencies; works offline)
tests/                scheduler and wall-switch tests
state.json            desired socket state (authoritative, survives restarts)
schedules.json        your timers
events.jsonl          activity history
server.log            server output, needed since autostart runs windowless
```

## Tests

```bash
python tests/test_schedule.py    # 25 checks, no hardware needed
python tests/test_latency.py     # 20 checks, guards USB round-trip count
python tests/test_mains.py       # 23 checks, simulates the wall switch
```

## Protocol notes

Two firmware quirks that look like typos but are not, both verified against
the hardware:

- socket 3's **off** byte is `P` (0x50), not `F` — the A/B, C/D, E/F sequence
  breaks at the last pair.
- socket 3's **read-state** byte is `0xAC`, not `0xA3`.

Every exchange is a 64-byte report. On Windows, hidapi requires the report ID
to be prepended, so 65 bytes go out with a leading `0x00`; padding is `0xFF`.
A read-state reply carries the state in byte 0 — the rest is stale buffer and
must be ignored.
