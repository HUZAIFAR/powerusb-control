// =====================================================================
//  PowerUSB - iOS Home Screen widget for the Scriptable app
//
//  The server fills in the address below and serves a ready-to-paste copy
//  at  <your server>/widget.js  -- there is a copy button in the app's
//  Device tab. If you are reading the template from the repo, replace the
//  two placeholders by hand.
//
//  SETUP
//    1. Install "Scriptable" from the App Store.
//    2. Scriptable -> + -> paste this whole file.
//    3. Name the script exactly:  PowerUSB
//       (the tap-to-switch links refer to it by name)
//    4. Long-press the Home Screen -> + -> Scriptable -> pick a size.
//    5. Long-press the placed widget -> Edit Widget -> Script: PowerUSB.
//       For a SMALL widget, also set Parameter to the socket you want,
//       e.g.  light
//
//  WHAT IT DOES
//    Small   one socket, big. Tap to toggle.
//    Medium  all three sockets side by side, each tappable.
//    Large   same as medium plus the mains-power line.
//
//  A NOTE ON LIVE-NESS
//    iOS decides when a widget re-renders, typically every few minutes -
//    a third-party widget cannot refresh on demand. So the state shown can
//    be a little stale. Tapping always acts on the real current state,
//    because the toggle is resolved by the server, not by the widget.
// =====================================================================

const BASE = "__BASE__";        // e.g. https://your-pc.your-tailnet.ts.net:9443
const TOKEN = "__TOKEN__";      // leave empty if you have not set one

// Colours match the app.
const IS_DARK = Device.isUsingDarkAppearance();
const C = {
  bg:    IS_DARK ? new Color("#0b0d12") : new Color("#f4f5f8"),
  card:  IS_DARK ? new Color("#161a22") : new Color("#ffffff"),
  text:  IS_DARK ? new Color("#e9edf5") : new Color("#14161c"),
  dim:   IS_DARK ? new Color("#8b93a3") : new Color("#4f5866"),
  mute:  IS_DARK ? new Color("#7c8593") : new Color("#5d6673"),
  on:    IS_DARK ? new Color("#ffb340") : new Color("#b45f00"),
  off:   IS_DARK ? new Color("#3a4150") : new Color("#c3c9d4"),
  warn:  IS_DARK ? new Color("#f5b333") : new Color("#8a5a00"),
  bad:   IS_DARK ? new Color("#ff6257") : new Color("#c0392b"),
};

// ---------------------------------------------------------------- network

function authed(req) {
  if (TOKEN) req.headers = { "X-Auth-Token": TOKEN };
  req.timeoutInterval = 10;
  return req;
}

async function getState() {
  const r = authed(new Request(BASE + "/api/state"));
  return await r.loadJSON();
}

async function act(target, action) {
  // Uses the plain-text /s/ endpoints: one GET, no body, no JSON.
  const url = BASE + "/s/" + encodeURIComponent(target) + "/" + action;
  const r = authed(new Request(url));
  return await r.loadString();
}

function slug(name) {
  return String(name || "").toLowerCase().replace(/[^a-z0-9]/g, "");
}

// Tapping a widget element opens this URL, which re-runs this same script
// with parameters. iOS gives no way for a widget to do network work in the
// background, so something has to open briefly - this is the least of the
// available evils, and it closes itself immediately.
function tapURL(target, action) {
  return "scriptable:///run?scriptName=" + encodeURIComponent(Script.name()) +
         "&target=" + encodeURIComponent(target) +
         "&action=" + encodeURIComponent(action);
}

// ---------------------------------------------------------------- drawing

function symbolFor(name) {
  const n = String(name || "").toLowerCase();
  if (/(monitor|screen|display|tv|pc)/.test(n)) return "display";
  if (/(fan|air|purifier|cooler)/.test(n)) return "fanblades";
  if (/(light|lamp|led|bulb|strip)/.test(n)) return "lightbulb";
  return "powerplug";
}

function icon(stack, name, isOn, size) {
  let symbolName = symbolFor(name);
  let sym;
  try {
    sym = SFSymbol.named(isOn ? symbolName + ".fill" : symbolName);
    if (!sym) sym = SFSymbol.named(symbolName);
  } catch (e) {
    sym = SFSymbol.named("powerplug");
  }
  if (!sym) return;
  const img = stack.addImage(sym.image);
  img.imageSize = new Size(size, size);
  img.tintColor = isOn ? C.on : C.off;
}

function socketTile(row, sock, online, wide) {
  const tile = row.addStack();
  tile.layoutVertically();
  tile.setPadding(10, 8, 10, 8);
  tile.backgroundColor = C.card;
  tile.cornerRadius = 12;
  tile.size = new Size(wide ? 0 : 0, 0);
  if (online) tile.url = tapURL(slug(sock.name) || String(sock.id), "toggle");

  const top = tile.addStack();
  top.centerAlignContent();
  icon(top, sock.name, sock.on, 18);
  top.addSpacer();

  tile.addSpacer(4);

  const nm = tile.addText(sock.name);
  nm.font = Font.semiboldSystemFont(11);
  nm.textColor = C.text;
  nm.lineLimit = 1;
  nm.minimumScaleFactor = 0.7;

  const st = tile.addText(!online ? "-" : sock.on ? "ON" : "OFF");
  st.font = Font.boldSystemFont(10);
  st.textColor = !online ? C.mute : sock.on ? C.on : C.mute;

  return tile;
}

function header(w, state, online) {
  const head = w.addStack();
  head.centerAlignContent();

  const title = head.addText("Power");
  title.font = Font.semiboldSystemFont(13);
  title.textColor = C.text;

  head.addSpacer();

  const lit = online ? state.sockets.filter(s => s.on).length : 0;
  const sub = head.addText(online ? lit + "/" + state.sockets.length + " on" : "mains off");
  sub.font = Font.systemFont(11);
  sub.textColor = online ? C.mute : C.warn;
}

// ---------------------------------------------------------------- widgets

function errorWidget(message) {
  const w = new ListWidget();
  w.backgroundColor = C.bg;
  const t = w.addText("Power");
  t.font = Font.semiboldSystemFont(13);
  t.textColor = C.text;
  w.addSpacer(6);
  const m = w.addText(message);
  m.font = Font.systemFont(11);
  m.textColor = C.bad;
  m.lineLimit = 4;
  w.addSpacer(4);
  const h = w.addText("Is Tailscale on?");
  h.font = Font.systemFont(10);
  h.textColor = C.mute;
  return w;
}

function smallWidget(state, online, which) {
  const w = new ListWidget();
  w.backgroundColor = C.bg;
  w.setPadding(14, 14, 14, 14);

  let sock = state.sockets[0];
  if (which) {
    const want = slug(which);
    const found = state.sockets.find(
      s => slug(s.name) === want || String(s.id) === which.trim());
    if (found) sock = found;
  }
  if (online) w.url = tapURL(slug(sock.name) || String(sock.id), "toggle");

  const top = w.addStack();
  top.centerAlignContent();
  icon(top, sock.name, online && sock.on, 26);
  top.addSpacer();
  if (!online) {
    const warn = top.addText("!");
    warn.font = Font.boldSystemFont(15);
    warn.textColor = C.warn;
  }

  w.addSpacer(8);

  const nm = w.addText(sock.name);
  nm.font = Font.semiboldSystemFont(15);
  nm.textColor = C.text;
  nm.lineLimit = 2;
  nm.minimumScaleFactor = 0.7;

  w.addSpacer(2);

  const st = w.addText(!online ? "no mains power" : sock.on ? "ON" : "OFF");
  st.font = Font.boldSystemFont(13);
  st.textColor = !online ? C.warn : sock.on ? C.on : C.mute;

  w.addSpacer();
  const hint = w.addText("tap to toggle");
  hint.font = Font.systemFont(9);
  hint.textColor = C.mute;
  return w;
}

function mediumWidget(state, online, large) {
  const w = new ListWidget();
  w.backgroundColor = C.bg;
  w.setPadding(12, 12, 12, 12);

  header(w, state, online);
  w.addSpacer(8);

  const row = w.addStack();
  row.layoutHorizontally();
  row.spacing = 7;
  state.sockets.forEach(s => socketTile(row, s, online, true));

  if (large) {
    w.addSpacer(10);
    const foot = w.addText(
      online ? "Tap a socket to toggle it."
             : "The wall switch is off. Anything you set is applied when power returns.");
    foot.font = Font.systemFont(10);
    foot.textColor = C.mute;
    foot.lineLimit = 3;
  }

  w.addSpacer();
  return w;
}

// ------------------------------------------------------------------- main

async function main() {
  const q = args.queryParameters || {};

  // Tapped from a widget: do the thing, then get out of the way.
  if (q.action && q.target) {
    try {
      const said = await act(q.target, q.action);
      if (!config.runsInWidget) {
        // A brief confirmation, then close. Nothing to interact with.
        const n = new Notification();
        n.title = "Power";
        n.body = String(said).trim();
        n.threadIdentifier = "powerusb";
        await n.schedule();
      }
    } catch (e) {
      const n = new Notification();
      n.title = "Power";
      n.body = "Could not reach the server. Is Tailscale on?";
      await n.schedule();
    }
    Script.complete();
    return;
  }

  let widget;
  try {
    const state = await getState();
    const online = !!state.online;
    const size = config.widgetFamily || "medium";
    if (size === "small") widget = smallWidget(state, online, args.widgetParameter);
    else widget = mediumWidget(state, online, size === "large");
  } catch (e) {
    widget = errorWidget("Can't reach the server.");
  }

  // Ask iOS to come back soon. It is a hint, not a guarantee.
  widget.refreshAfterDate = new Date(Date.now() + 5 * 60 * 1000);

  if (config.runsInWidget) {
    Script.setWidget(widget);
  } else {
    const size = config.widgetFamily || "medium";
    if (size === "small") await widget.presentSmall();
    else if (size === "large") await widget.presentLarge();
    else await widget.presentMedium();
  }
  Script.complete();
}

// Wrapped rather than a bare top-level `await`: top-level await is only
// legal in module context, so a plain-script evaluator rejects the whole file
// at parse time. An async IIFE is legal everywhere.
(async () => {
  try {
    await main();
  } catch (e) {
    // Never leave the widget blank with no explanation.
    if (config.runsInWidget) {
      Script.setWidget(errorWidget(String(e)));
      Script.complete();
    } else {
      console.error(e);
      Script.complete();
    }
  }
})();
