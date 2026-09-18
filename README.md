# PowerUSB control

Control the three switchable sockets of a **PowerUSB** power strip from a phone,
a browser, Siri, the command line, or any script that can open a TCP socket.

Written for a strip plugged into a **switched wall outlet**, which is the awkward
case: the strip loses its USB controller along with its power, so it vanishes
from the host entirely and comes back with no warning. Most of the design below
exists to make that invisible to you.

Built and verified against real hardware:

```
VID:PID   04D8:003F   (raw HID, vendor usage page 0xFF00)
Reports as "Microchip Technology Inc. / Simple HID Device Demo"
Model 1 (Basic), firmware 3.5
```

<img src="web/icon-180.png" width="72" alt="app icon">

---

## Contents

- [Quick start](#quick-start)
- [The wall-switch problem](#the-wall-switch-problem)
- [What this hardware can and cannot do](#what-this-hardware-can-and-cannot-do)
- [The web app](#the-web-app)
- [Siri, Home Screen and Lock Screen (iOS)](#siri-home-screen-and-lock-screen-ios)
- [Scene links](#scene-links-several-sockets-one-url)
- [Home Screen widget (Scriptable)](#home-screen-widget-scriptable)
- [Sleep timers](#sleep-timers)
- [Away mode](#away-mode)
- [Usage chart](#usage-chart)
- [Command line](#command-line)
- [HTTP API](#http-api)
- [TCP control](#tcp-control)
- [Configuration](#configuration)
- [Security model](#security-model)
- [Files](#files)
- [Tests](#tests)
- [Protocol notes](#protocol-notes)

---

## Quick start

```bash
pip install -r requirements.txt
cp config.example.json config.json      # then edit it
python -m powerusb.server
```

Open `http://127.0.0.1:8765/`. That is enough to use it locally.

### Reaching it from your phone

The recommended setup binds the server to loopback and publishes it through
**Tailscale Serve**, so it is reachable from any device on your tailnet —
including away from home — and from nowhere else:

```bash
tailscale serve --bg --https=9443 http://127.0.0.1:8765
```

That gives you `https://your-pc.your-tailnet.ts.net:9443/` with a real
Let's Encrypt certificate. Put that address in `config.json` as `public_url`.

No firewall rules, no ports open to the LAN, nothing exposed to the internet.

> **Port 9443 is deliberate.** Tailscale Funnel — the feature that *does* publish
> to the public internet — only works on 443, 8443 and 10000. Using 9443 means
> the power control can never be exposed publicly, even by accident, and any
> Funnel config you already have on 443 is untouched.

Because it is served over real HTTPS it is a genuine installable PWA. On iOS:
Safari → Share → **Add to Home Screen**. On Android: Chrome → **Install app**.

### Autostart at boot (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1
```

Run it **as Administrator**. It registers a Scheduled Task that starts the
server **at boot, before anyone logs in**, restarts it automatically if it
stops, and *verifies the server actually answers* before reporting success. It
tries SYSTEM first, then your own account via S4U.

> **The Store-alias trap.** On many Windows installs, `python.exe`,
> `pythonw.exe` and `py.exe` on `PATH` are 0-byte AppExecLink reparse points —
> Microsoft Store app-execution aliases. An interactive user can launch them; a
> Scheduled Task running as SYSTEM **cannot**, and it fails silently with
> nothing written to any log. The installer therefore locates the real
> interpreter through the PEP 514 registry keys, refuses anything under
> `\WindowsApps\`, and probes the candidate with `import hid` before trusting it.

---

## The wall-switch problem

When the wall switch is off, the strip's USB controller is unpowered, so the
device disappears from the host completely. No software can talk to it. Two
mechanisms make this seamless:

**1. The strip restores itself.** Every change is also written to the strip's
EEPROM power-up defaults. When mains returns, the firmware restores each socket
from those defaults *before the host is involved at all* — so it works even if
the PC is asleep, rebooting, or switched off.

**2. The server reconciles.** A monitor thread watches USB enumeration. The
strip vanishing is read as "mains off"; reappearing as "mains on", at which
point the saved desired state is re-applied to be certain the hardware agrees.

Commands issued while the strip is dark are **not errors**. They are recorded,
persisted, and applied the moment power returns — the app shows them as
*pending*, and timers keep running and queue their result too.

Covered by `tests/test_mains.py`, including six consecutive power cycles and a
check that the EEPROM is not rewritten by cycling alone.

---

## What this hardware can and cannot do

| Capability | This unit (Basic) |
|---|---|
| Switch sockets 1–3 | yes |
| Remember a power-up default per socket | yes |
| Measure current / wattage | **no** — Smart model only |
| Overload threshold | **no** — firmware does not reply |
| Watchdog auto-reboot | **no** — Watchdog model only |
| Digital IO | **no** — Digital IO model only |

The Basic model has no current sensor. It *does* reply to the read-current
command (`0xB1`), but the bytes are stale buffer contents that flip between
fixed values while the load is unchanged — so the driver deliberately returns
`None` rather than present a fabricated reading. `has_metering()` gates it on
the model byte.

**No PowerUSB model meters per-socket.** Even the Smart model has a single
sensor for the whole strip. The **energy estimate** in the Device tab is
therefore exactly that: you enter each socket's rated watts, and it multiplies
them by the on-time actually recorded in the activity log. Periods when mains
was off count as zero, and it only reports over the span the log actually
covers rather than inventing history.

---

## The web app

Mobile-first, installable, no external dependencies — it works with no internet
connection. Four tabs:

- **Control** — the three sockets. Tap to toggle, tap the pencil to rename.
  Icons follow the name (a socket called "Monitor" gets a monitor icon). Away
  mode lives here too.
- **Timers** — recurring on/off schedules per socket per weekday, plus one-shot
  sleep timers.
- **Log** — a seven-day usage chart, then every switch, timer firing and mains
  power change, grouped by day.
- **Device** — model and firmware, the link to open on your phone, the Siri /
  Shortcuts links, the custom link builder, and the energy estimate.

**Light and dark.** The button in the header cycles auto → light → dark and
remembers the choice; auto follows the system. Both palettes were
contrast-checked rather than eyeballed: body text is at least 4.5:1 against its
own surface and chart marks at least 3:1. The light-mode amber is much darker
than the dark-mode one out of necessity — the bright amber is 1.78:1 on white
and would be unreadable as a label or a bar.

### Staying up to date

The server fingerprints the web UI and reports it as `build`. The page
remembers the build it loaded with, and when the server reports a different one
it shows **"New version ready — tap to reload"**. There is also a **⟳ button**
in the header to force a reload at any time.

This matters for an installed PWA, which otherwise keeps its original page
indefinitely — without it you would have to remove and re-add the Home Screen
icon after every update. The reload uses a cache-busting URL rather than
`location.reload()`, because an iOS standalone web app will happily re-serve
the page it already has; the parameter is then stripped from the address bar.

---

## Siri, Home Screen and Lock Screen (iOS)

iOS does **not** let a web app add a Lock Screen widget or register a Siri
phrase. Both need WidgetKit / App Intents, which means a native app compiled in
Xcode. There is no web API for either, and no amount of manifest tweaking
changes that. **Apple Shortcuts** is the supported way to get the same result.

The `/s/` endpoints exist for exactly this: plain `GET`, plain-text reply, so a
Shortcut is a single **Get Contents of URL** action with no method, headers or
JSON body to configure.

```
/s/<name>/on      /s/<name>/off      /s/<name>/toggle
/s/all/on         /s/all/off         /s/status
/s                                   (lists every link, generated live)
```

`<name>` matches loosely — `light`, `lights`, `LIGHT`, `Light and Power Strip`
and `1` all reach the same socket. Unknown names return 404 with a list of the
real ones. Every reply is a short sentence ("Monitor is on."), so adding a
**Speak Text** action makes Siri read the result back.

**Make a Siri phrase**
1. Shortcuts app → **+**
2. **Add Action** → *Get Contents of URL*
3. Paste a link
4. Rename the shortcut to the words you want to say — **the name is the phrase**

**Home Screen icon** — Shortcuts → `⋯` on the shortcut → Share → *Add to Home Screen*

**Lock Screen widget** — long-press the Lock Screen → Customise → tap the widget
row under the clock → Shortcuts

**Control Centre / Lock Screen button (iOS 18+)** — Settings → Control Centre →
add a control → Shortcuts; or replace the torch/camera button during Lock Screen
customisation

The Device tab lists every link with a copy button, so you never have to type
one. Tailscale must be on for these to reach the server — at home as well as
away, since the server is loopback-only.

---

## Scene links: several sockets, one URL

Separate targets with `+` or `,` to switch a group together:

```
/s/light+monitor/on
/s/light+monitor/off
/s/light,monitor,fan/toggle
```

The whole group is applied as one operation — a single state save and a single
reply — so a scene link is as quick as a single-socket one. The reply reads
naturally: *"Light and Monitor are on."*, or *"Light is on. Monitor is off."*
when they disagree.

If **any** name in the group is unknown the entire request is refused with 404
and **nothing is switched**, so a typo can never half-apply a scene.

The **Custom link** card in the Device tab builds these for you: tick the
sockets, choose on / off / toggle, and copy the resulting link.

---

## Home Screen widget (Scriptable)

A Shortcut can only *fire* an action; it cannot show anything. The free
[Scriptable](https://scriptable.app) app can: a real Home Screen widget that
displays which sockets are on, with each one tappable.

The server generates a ready-to-paste copy of the script with its own address
already filled in:

```
GET /widget.js
```

There is a **Copy script** button in the app's Device tab.

1. Install **Scriptable** from the App Store.
2. Scriptable -> `+` -> paste the script.
3. Name it exactly **PowerUSB** - the tap links refer to it by name.
4. Long-press the Home Screen -> `+` -> Scriptable -> choose a size.
5. Long-press the placed widget -> **Edit Widget** -> Script: **PowerUSB**.
   For a *small* widget also set **Parameter** to one socket name, e.g. `light`.

| Size | Shows |
|---|---|
| Small | one socket, large. Tap toggles it. |
| Medium | all three side by side, each tappable |
| Large | as medium, plus the mains-power line |

**How live is it?** iOS decides when a widget redraws, typically every few
minutes; no third-party widget can refresh on demand. So the state on screen
may lag slightly. Tapping is correct regardless — the widget sends `toggle` and
the *server* resolves it against the real current state, not against what the
widget happened to be showing.

**What happens on a tap.** iOS gives widgets no way to do background network
work, so something must open briefly. The widget opens Scriptable, which fires
one request at the `/s/` endpoint, posts a notification with the reply, and
exits. Only a native app using App Intents can avoid that flash, and that needs
Xcode.

Tailscale must be connected on the phone, as with everything else here.

---

## Sleep timers

A one-shot countdown: switch something off (or on) once, after a delay.

In the app: **Timers** tab, pick a socket and tap 15 min / 30 min / 1 hour. The
remaining time counts down, and it can be cancelled.

From Siri or a Shortcut:

```
/s/fan/off/in/30        turn the fan off in 30 minutes
/s/light/on/in/90       turn the light on in an hour and a half
```

Two details that matter in practice:

- Setting a second countdown for the same socket and action **replaces** the
  first rather than stacking, so tapping "30 min" twice does not leave two
  timers racing to switch the same socket.
- A countdown missed while the server was down fires if it is recent, but is
  **dropped** if it is hours stale. You do not want the fan coming on at 3am
  because the PC rebooted.

---

## Away mode

Makes the place look lived-in while nobody is. Toggle it on the **Control** tab,
pick which sockets take part, and set the nightly window.

Inside the window it switches those sockets at irregular intervals — dwell times
are randomised per socket and deliberately not synchronised, so it does not read
as automation from the street. Outside the window everything it controls goes
off once and it then stays quiet until the next evening.

Three guarantees worth knowing:

- It **only** touches the sockets you list. Anything else you left on stays on.
- Turning it off **restores those sockets to exactly the states they were in**
  when you switched it on, so coming home does not mean rearranging everything.
- It drives the controller, not the hardware, so while the wall switch is off
  its changes queue like any other and land when power returns.

Settings live in `away.json` and survive a restart.

---

## Usage chart

The top of the **Log** tab shows how many hours each socket was on per day for
the last week, computed by replaying the activity log.

It is drawn as small multiples — one row per socket — sharing a single scale, so
the rows are directly comparable; per-row scales would make a lamp that ran for
an hour look the same as a monitor that ran for ten. Days the log does not reach
back to render as a faint tick rather than a zero-height bar, because "no data"
and "off all day" are different claims. Tap a bar for the exact figure.

`GET /api/usage/daily?days=7` returns the same data as JSON.

---

## Command line

```bash
python pusb.py status          # show all sockets
python pusb.py on 1            # switch socket 1 on
python pusb.py off 1 3         # switch 1 and 3 off
python pusb.py toggle 2        # flip socket 2
python pusb.py all on          # everything on
python pusb.py watch           # live view, updates until Ctrl-C
python pusb.py info            # device and protocol diagnostics
python pusb.py defaults        # show the power-up defaults
```

Only one process can hold the strip's USB handle, so the CLI talks to the
server over HTTP when it is running and drives the device directly when it is
not. `--direct` forces the latter.

---

## HTTP API

| Method | Path | Body / query |
|---|---|---|
| GET | `/api/state` | — (includes `build`) |
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
| GET | `/api/usage/daily` | `?days=7` — per-day on-hours, for the chart |
| GET | `/api/away` | — |
| POST | `/api/away` | `{"enabled":true,"sockets":[1,3],"start":"17:30","end":"23:15"}` |
| POST | `/api/countdown` | `{"socket":3,"action":"off","minutes":30}` |
| DELETE | `/api/countdown/<id>` | — |
| GET | `/api/diag` | — model, firmware, metering support, build |
| GET | `/api/health` | — |

`days` is `0`=Monday … `6`=Sunday; an empty list means every day.

```bash
curl -X POST -H 'Content-Type: application/json' \
     -d '{"on":true}' http://127.0.0.1:8765/api/socket/1
```

---

## TCP control

Line protocol on port 8766, for scripts and home-automation kit:

```
STATUS            -> OK online 0 1 0
ON 1 | OFF 1 | TOGGLE 1
ALL ON | ALL OFF
NAMES | PING | AUTH <token> | QUIT
```

`STATUS` answers `OK offline …` while mains is out, reporting the state the
strip will return to.

---

## Configuration

Copy `config.example.json` to `config.json` (it is gitignored) and edit:

```json
{
  "host": "127.0.0.1",
  "http_port": 8765,
  "tcp_port": 8766,
  "token": "",
  "public_url": "https://your-pc.your-tailnet.ts.net:9443/",
  "names": ["Socket 1", "Socket 2", "Socket 3"],
  "watts": [0, 0, 0]
}
```

| Key | Meaning |
|---|---|
| `host` | `127.0.0.1` for Tailscale-only; `0.0.0.0` to also serve the LAN |
| `token` | optional shared secret; empty disables auth |
| `public_url` | the address shown in the app and used to build Shortcut links |
| `names` | socket labels (also editable in the app) |
| `watts` | rated watts per socket, for the energy estimate only |

A malformed `config.json` does **not** stop the server: it logs a warning and
falls back to defaults. Running unattended, exiting here would leave the strip
uncontrollable after a reboot with nothing on screen to explain why. The
readers also accept a UTF-8 BOM, because Notepad and PowerShell both add one.

---

## Security model

- The server binds **loopback** by default. Nothing on the LAN can reach it.
- Tailscale Serve terminates TLS and proxies from localhost, so access is
  limited to your tailnet, which is already authenticated.
- **No CORS headers are sent, deliberately.** The GUI is same-origin so it needs
  none, and a wildcard would let any website you happen to visit switch your
  sockets.
- The `/s/` endpoints are `GET` and mutate state. That is a considered
  trade-off: it makes an Apple Shortcut a single action with nothing to
  configure, and it is safe here because the server is loopback-only behind an
  authenticated tailnet and holds no cookie or session a browser could be
  tricked into replaying. If you expose this more widely, set a `token` — it is
  required as `?token=` or an `X-Auth-Token` header.
- Anyone on your tailnet can control the strip. If your tailnet has other
  people on it, set a `token`.

---

## Files

```
powerusb/device.py    HID driver: wire protocol, locking, reconnection, defaults cache
powerusb/server.py    HTTP + TCP server, mains monitor, state reconciliation, scenes
powerusb/schedule.py  recurring timers and one-shot countdowns
powerusb/away.py      away-mode occupancy simulation
powerusb/events.py    the activity log (JSON Lines, bounded)
powerusb/config.py    config.json handling
pusb.py               command line client
web/index.html        the whole GUI: no build step, no external dependencies
web/widget.js         Scriptable Home Screen widget (served with the address filled in)
tools/make_icon.py    regenerates the app icons (pure-python PNG encoder)
install-autostart.ps1 Windows boot autostart
tests/                scheduler, wall-switch, latency and scene tests
```

Runtime files, all gitignored: `config.json`, `state.json`, `schedules.json`,
`events.jsonl`, `away.json`, `server.log`.

> `events.jsonl` is an occupancy record — it shows when lights went on and off,
> i.e. when somebody was home. Keep it out of version control.

---

## Tests

No hardware needed; the strip is faked, including its power supply.

```bash
python tests/test_schedule.py        # 25 checks - timer firing, catch-up, persistence
python tests/test_mains.py           # 23 checks - wall-switch power cycling
python tests/test_latency.py         # 20 checks - USB round-trip count, name matching
python tests/test_scenes.py          # 24 checks - multi-socket links
python tests/test_away_countdown.py  # 49 checks - sleep timers, away mode, daily usage
```

141 checks in total.

`test_latency.py` counts the USB exchanges a single switch performs and fails if
it grows. Each exchange costs a write settle plus a read, and those land
directly in the latency of a tap and of a Siri command — a switch is 3
exchanges, and an innocent-looking "just re-read the state" once made it 8.

---

## Protocol notes

Every exchange is a 64-byte HID report. On Windows, hidapi requires the Report
ID to be prepended, so 65 bytes go out with a leading `0x00`; padding is `0xFF`.
A read-state reply carries the state in byte 0 — the rest is stale buffer and
must be ignored.

| socket | on | off | read state | default on | default off | read default |
|---|---|---|---|---|---|---|
| 1 | `A` | `B` | `0xA1` | `N` | `F` | `0xA3` |
| 2 | `C` | `D` | `0xA2` | `G` | `Q` | `0xA4` |
| 3 | `E` | **`P`** | **`0xAC`** | `O` | `H` | `0xAD` |

Two firmware quirks that look like typos but are not, both verified against the
hardware:

- socket 3's **off** byte is `P` (0x50), not `F` — the A/B, C/D, E/F sequence
  breaks at the last pair
- socket 3's **read-state** byte is `0xAC`, not `0xA3`

The `DEF-*` commands write the socket's power-up default into EEPROM. They are
cached in memory and only written when the value actually changes, both to keep
switching fast and because EEPROM cells have finite write endurance.

---

## Licence

MIT
