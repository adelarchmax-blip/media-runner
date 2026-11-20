// Downloads one video page the way a person does with Mister Download Manager: Chrome opens the
// page with MDM's official extension loaded, the pointer goes over the video, and a click on the
// extension's "Download" button hands it to the running MDM app. Prints one JSON line at the end:
// {"ok": true, "path": ..., "size": ...} or {"ok": false, "error": ...}.
//
//   xvfb-run -a node grab.mjs <page-url> <out-dir>
//
// Env: MDM_HOME (setup.sh's folder), MDM_EXTENSION (the unpacked extension), CHROME_BIN.
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import puppeteer from "puppeteer-core";

const [url, outDir] = process.argv.slice(2);
if (!url || !outDir) {
  console.error("usage: grab.mjs <page-url> <out-dir>");
  process.exit(2);
}
const HOME = process.env.MDM_HOME || path.join(os.homedir(), "mdm");
const DATA = path.join(HOME, "data");
const EXTENSION = process.env.MDM_EXTENSION || path.join(HOME, "extension");
const CHROME = process.env.CHROME_BIN || "/usr/bin/google-chrome";
const HOST = "com.misterdownloadmanager.host";
const LOOKUP_MS = 180_000;
const DOWNLOAD_MS = 40 * 60_000;
// MDM's window looks up what the extension sent (often the stream's own address); after this
// long without a result, the item's page address is given instead.
const RETYPE_MS = 15_000;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (msg) => console.error(`[grab] ${msg}`);

async function until(what, ms, fn, every = 500) {
  const end = Date.now() + ms;
  for (;;) {
    const v = await fn().catch((e) => {
      if (e.fatal) throw e;
      return null;
    });
    if (v) return v;
    if (Date.now() > end) throw new Error(`timed out: ${what}`);
    await sleep(every);
  }
}

// MDM's automation API: one JSON line to the port in ipc.json, with its token.
function ipc(cmd) {
  return new Promise((resolve, reject) => {
    const ep = JSON.parse(fs.readFileSync(path.join(DATA, "ipc.json"), "utf8"));
    const s = net.connect(ep.port, "127.0.0.1", () => s.write(JSON.stringify({ token: ep.token, ...cmd }) + "\n"));
    let answer = "";
    s.setTimeout(5000, () => s.destroy(new Error("no answer")));
    s.on("data", (d) => (answer += d));
    s.on("end", () => {
      try {
        resolve(JSON.parse(answer.trim()));
      } catch {
        resolve({ answer: answer.trim() });
      }
    });
    s.on("error", reject);
  });
}
async function mdmList() {
  const a = await ipc({ query: "list" });
  if (!a.downloads) throw new Error(JSON.stringify(a));
  return a.downloads;
}

// The app's settings before each start: the automation API on, no first-run windows, the most
// connections, and
// downloads into this item's outDir (each item has its own folder, removed after its upload, so
// the folder is set again for every item while the rest of the app's data is kept).
function prepareData() {
  fs.mkdirSync(DATA, { recursive: true });
  fs.mkdirSync(outDir, { recursive: true });
  const file = path.join(DATA, "downloads.json");
  let data = { next_id: 1, items: [], version: 2, settings: {} };
  try {
    data = { ...data, ...JSON.parse(fs.readFileSync(file, "utf8")) };
  } catch {}
  data.settings = {
    ...data.settings,
    language: "en",
    language_asked: true,
    extension_never_ask: true,
    show_start_dialog: false,
    automation_api: true,
    // Options → Connection → "Default max. conn. number": the pieces of a stream (HLS, DASH) and
    // the parts of a file MDM downloads at once. 32 is the most MDM allows without unlocking more.
    max_connections: 32,
    category_dirs: ["General", "Compressed", "Documents", "Music", "Programs", "Video"].map((c) => [c, path.resolve(outDir)]),
    torrent_handler: false,
  };
  fs.writeFileSync(file, JSON.stringify(data));
}

// The native messaging host for Chrome: registered by MDM's own helper, then copied into the
// browser profile (with --user-data-dir, Chrome reads host manifests from there).
function registerHost(profile) {
  // MDM registers for the browsers whose config folder exists (a fresh runner has none yet).
  fs.mkdirSync(path.join(os.homedir(), ".config", "google-chrome"), { recursive: true });
  const reg = spawnSync("misterdownloadmanager-nmhost", ["--register"], { encoding: "utf8" });
  if (reg.status !== 0) throw new Error(`nmhost --register: ${reg.stderr || reg.stdout}`);
  const written = [...(reg.stdout || "").matchAll(/^registered \S+ in (.+)$/gm)].map((m) => path.join(m[1].trim(), `${HOST}.json`));
  // The .deb registers it for everyone (/etc/opt/chrome); --register then writes nothing.
  const manifest = [...written, `/etc/opt/chrome/native-messaging-hosts/${HOST}.json`].find((w) => !/mozilla/i.test(w) && fs.existsSync(w));
  if (!manifest) throw new Error(`no Chromium host manifest: ${reg.stdout}`);
  fs.mkdirSync(path.join(profile, "NativeMessagingHosts"), { recursive: true });
  fs.copyFileSync(manifest, path.join(profile, "NativeMessagingHosts", `${HOST}.json`));
}

// Where a frame's top-left sits in the page (players often live in an iframe).
async function frameOffset(frame) {
  const el = frame.parentFrame() ? await frame.frameElement() : null;
  const box = el ? await el.boundingBox() : null;
  return box ? { x: box.x, y: box.y } : { x: 0, y: 0 };
}

// The extension's button lives in a closed shadow root: DevTools reaches it, page scripts can't.
// Searched in every frame; coordinates come back in the page's viewport.
let lastErr = "";
async function buttonState(page) {
  for (const frame of page.frames()) {
    // In the document, or inside a player's own (open) shadow DOM.
    const host = (await frame.$("mdm-video-button").catch(() => null)) || (await frame.$("pierce/mdm-video-button").catch(() => null));
    if (!host) continue;
    try {
      return await readButton(frame);
    } catch (e) {
      const m = String(e.message || e).slice(0, 200);
      if (m !== lastErr) log(`button read: ${(lastErr = m)}`);
    }
  }
  return null;
}

// MDM's own windows on xvfb's screen (xdotool): their ids and titles.
function mdmWindows() {
  const ids = spawnSync("xdotool", ["search", "--onlyvisible", "--name", "."], { encoding: "utf8" }).stdout.split("\n").filter(Boolean);
  return ids
    .map((id) => ({ id, name: spawnSync("xdotool", ["getwindowname", id], { encoding: "utf8" }).stdout.trim() }))
    .filter((w) => w.name && !/chrome|chromium/i.test(w.name));
}
// MDM's "Download video from a page" window (a stream, or a page with no one-click quality):
// brought on screen, then its own buttons pressed, at their places in its fixed layout (fractions
// of the window). First its look-up of the address the extension sent is waited for; when that
// takes too long, the item's page address goes in its "Page address" box and "Look up" is
// pressed (RETYPE_MS). "Download Now" is pressed every few seconds (it stays greyed until the look-up ends).
let windowsSeen = "";
let shotWindow = false;
let windowSince = 0;
let retyped = false;
function x(...args) {
  return spawnSync("xdotool", args, { encoding: "utf8", timeout: 8000 }).stdout || "";
}
function clickIn(geo, fx, fy) {
  x("mousemove", String(Math.round(geo.x + geo.w * fx)), String(Math.round(geo.y + fy)), "click", "1");
}
function driveVideoWindow(pageUrl) {
  const wins = mdmWindows();
  const names = wins.map((w) => w.name).join(" | ");
  if (names !== windowsSeen) log(`MDM windows: ${(windowsSeen = names) || "none"}`);
  // Any window of MDM's other than its main one ("Mister Download Manager <version>").
  const w = wins.find((v) => !/^Mister Download Manager( [\d.]+)?$/i.test(v.name));
  if (!w) return;
  if (!windowSince) windowSince = Date.now();
  x("windowmove", w.id, "1300", "0");
  x("windowraise", w.id);
  x("windowfocus", "--sync", w.id);
  const g = /Position: (\d+),(\d+)[\s\S]*Geometry: (\d+)x(\d+)/.exec(x("getwindowgeometry", w.id));
  if (!g) return;
  const geo = { x: +g[1], y: +g[2], w: +g[3], h: +g[4] };
  if (!retyped && Date.now() - windowSince > RETYPE_MS) {
    retyped = true;
    clickIn(geo, 0.4, 37);
    x("key", "--clearmodifiers", "ctrl+a");
    x("type", "--delay", "5", pageUrl);
    clickIn(geo, 0.88, 37);
    log("MDM's look-up was slow: gave it the item's page address and pressed Look up");
  }
  clickIn(geo, 0.54, geo.h - 23);
  if (process.env.MDM_SHOTS && (!shotWindow || retyped === true)) {
    shotWindow = retyped ? "after" : true;
    spawnSync("ffmpeg", ["-loglevel", "error", "-y", "-f", "x11grab", "-video_size", "2400x1000", "-i", process.env.DISPLAY, "-frames:v", "1", path.join(process.env.MDM_SHOTS, retyped ? "screen-retyped.png" : "screen.png")], { timeout: 15000 });
    if (retyped) retyped = "shot";
  }
}

// The button's closed shadow tree, read from DevTools' copy of the frame's DOM (attributes and
// text only, no script inside it): its state, words and where its main part is.
async function readButton(frame) {
  const cdp = frame.client;
  const { root } = await cdp.send("DOM.getDocument", { depth: -1, pierce: true });
  const find = (n, test) => {
    if (test(n)) return n;
    for (const k of [...(n.children || []), ...(n.shadowRoots || []), ...(n.contentDocument ? [n.contentDocument] : [])]) {
      const f = find(k, test);
      if (f) return f;
    }
    return null;
  };
  const attr = (n, name) => {
    const a = n.attributes || [];
    for (let i = 0; i < a.length; i += 2) if (a[i] === name) return a[i + 1];
    return null;
  };
  const hasClass = (c) => (n) => n.nodeType === 1 && (attr(n, "class") || "").split(/\s+/).includes(c);
  const text = (n) => (n.nodeType === 3 ? n.nodeValue : (n.children || []).map(text).join(""));
  const host = find(root, (n) => n.localName === "mdm-video-button" && n.shadowRoots && n.shadowRoots.length);
  if (!host) throw new Error("no shadow root");
  const shadow = host.shadowRoots[0];
  const pill = find(shadow, hasClass("pill"));
  const go = pill && find(pill, hasClass("go"));
  if (!pill || !go) throw new Error("no pill in the button");
  const { model } = await cdp.send("DOM.getBoxModel", { nodeId: go.nodeId });
  const q = model.border;
  const label = find(go, hasClass("label"));
  const chip = find(go, hasClass("chip"));
  const o = await frameOffset(frame);
  return {
    state: attr(pill, "data-state"),
    label: label ? text(label).trim() : "",
    chip: chip ? text(chip).trim() : "",
    x: (q[0] + q[4]) / 2 + o.x,
    y: (q[1] + q[5]) / 2 + o.y,
    w: model.width,
  };
}

// Where the extension's button shows, in the page (its host element is ours to measure).
async function hostBox(page) {
  for (const frame of page.frames()) {
    const host = (await frame.$("mdm-video-button").catch(() => null)) || (await frame.$("pierce/mdm-video-button").catch(() => null));
    const box = host && (await host.boundingBox().catch(() => null));
    if (box && box.width > 40 && box.height > 16) return box;
  }
  return null;
}

// Ad networks' players and clips (any site): their videos are never the item.
const AD = /doubleclick|2mdn\.net|googlesyndication|imasdk|googleads|adservice|adnxs|springserve|spotx|teads|\/ads?\//i;

// The biggest visible video in any frame (not an ad's), its centre in the page's viewport.
async function videoBox(page) {
  await centerPlayer(page);
  let best = null;
  for (const frame of page.frames()) {
    if (AD.test(frame.url())) continue;
    const r = await frame
      .evaluate((ad) => {
        const AD = new RegExp(ad, "i");
        const vs = [...document.querySelectorAll("video")]
          .filter((e) => !AD.test(e.currentSrc || e.src || ""))
          .map((e) => [e, e.getBoundingClientRect()])
          .filter(([, r]) => r.width >= 200 && r.height >= 112)
          .sort((a, b) => b[1].width * b[1].height - a[1].width * a[1].height);
        if (!vs.length) return null;
        const [v, r] = vs[0];
        v.muted = true;
        v.play().catch(() => {});
        // A video that holds no data yet (a player stuck loading) isn't the item yet.
        if (v.readyState < 2) return null;
        return { x: r.left + r.width / 2, y: r.top + r.height / 2, area: r.width * r.height };
      }, AD.source)
      .catch(() => null);
    if (!r || (best && best.area >= r.area)) continue;
    const o = await frameOffset(frame);
    best = { x: r.x + o.x, y: r.y + o.y, area: r.area };
  }
  return best;
}

// General rule for any site: an item page with no player of its own (no video, no player-sized
// iframe) often links to a separate watch page. The visible same-site link that names watching
// (in its address, class or short text) is returned; trailers, downloads and hidden links (traps
// for robots) never count.
async function watchPageLink(page) {
  return page
    .evaluate(() => {
      const seen = (e) => {
        const r = e.getBoundingClientRect();
        const st = getComputedStyle(e);
        return r.width >= 16 && r.height >= 12 && st.visibility !== "hidden" && st.display !== "none" && !e.closest("[aria-hidden=true]");
      };
      const big = (e) => {
        const r = e.getBoundingClientRect();
        return r.width >= 300 && r.height >= 150;
      };
      if ([...document.querySelectorAll("video")].some((v) => seen(v) && big(v))) return null;
      if ([...document.querySelectorAll("iframe")].some((f) => seen(f) && big(f) && f.src && f.src !== "about:blank")) return null;
      const here = location.href.split("#")[0];
      const not = /trailer|تريلر|اعلان|إعلان|download|تحميل|تنزيل/i;
      let pick = null;
      for (const a of document.querySelectorAll("a[href]")) {
        if (!seen(a) || a.origin !== location.origin || a.href.split("#")[0] === here) continue;
        const cls = typeof a.className === "string" ? a.className : "";
        const text = (a.textContent || "").trim();
        const byPath = /\/(watch|play|player)(\/|$|\?)/i.test(a.pathname);
        const byName = /\bwatch\b/i.test(cls) || (text.length <= 25 && /مشاهدة|شاهد الآن|\bwatch( now)?\b/i.test(text));
        if (!(byPath || byName) || not.test(cls + " " + text + " " + decodeURIComponent(a.pathname))) continue;
        const score = (byPath ? 2 : 0) + (byName ? 1 : 0);
        if (!pick || score > pick.score) pick = { href: a.href, score };
      }
      return pick && pick.href;
    })
    .catch(() => null);
}

// General rule for any site: a watch page often lists its servers in its code, as other sites'
// player addresses in its buttons' data attributes (data-link, data-src, data-url, data-embed).
// The list keeps the page's order, starting with the one already in the player frame.
async function serverLinks(page) {
  return page
    .evaluate(() => {
      const ok = (u) => {
        try {
          const x = new URL(u, location.href);
          return /^https?:$/.test(x.protocol) && x.origin !== location.origin && !/\.(jpe?g|png|webp|gif|svg|avif|css|js)(\?|$)/i.test(x.pathname) ? x.href : null;
        } catch {
          return null;
        }
      };
      const hidden = (e) => !!e.closest("[aria-hidden=true],[hidden]") || getComputedStyle(e).display === "none";
      const out = [];
      const frame = [...document.querySelectorAll("iframe")].sort((a, b) => b.offsetWidth * b.offsetHeight - a.offsetWidth * a.offsetHeight)[0];
      if (frame && ok(frame.src)) out.push(ok(frame.src));
      for (const a of ["data-link", "data-src", "data-url", "data-embed", "data-iframe"])
        for (const e of document.querySelectorAll(`[${a}]`)) {
          const u = !hidden(e) && e.tagName !== "IMG" && e.tagName !== "IFRAME" ? ok(e.getAttribute(a)) : null;
          if (u && !out.includes(u)) out.push(u);
        }
      return out.slice(0, 30);
    })
    .then(byHostInTurn)
    .catch(() => []);
}

// Servers of one host often fail together (the host blocks the runner or refuses to be framed),
// so the hosts take turns: the first of each host in page order, then the second of each, ...
// The page's own first server stays first.
function byHostInTurn(links) {
  const hosts = new Map();
  for (const u of links) {
    const h = new URL(u).hostname.replace(/^www\./, "");
    if (!hosts.has(h)) hosts.set(h, []);
    hosts.get(h).push(u);
  }
  const out = [];
  for (let i = 0; out.length < links.length; i++) for (const list of hosts.values()) if (list[i]) out.push(list[i]);
  return out;
}

// The page's player frame itself (its biggest iframe, not an ad frame inside it) shows the
// browser's own error page (the server refused to be framed or to connect): nothing will play there.
async function playerRefused(page) {
  let best = null;
  for (const el of await page.$$("iframe").catch(() => [])) {
    const box = await el.boundingBox().catch(() => null);
    if (box && (!best || box.width * box.height > best.area)) best = { el, area: box.width * box.height };
  }
  const frame = best && (await best.el.contentFrame().catch(() => null));
  return !!frame && frame.url().startsWith("chrome-error://");
}

// The page's biggest player frame loads another server (what the page's own server buttons do).
async function useServer(page, link) {
  await page
    .evaluate((u) => {
      const f = [...document.querySelectorAll("iframe")].sort((a, b) => b.offsetWidth * b.offsetHeight - a.offsetWidth * a.offsetHeight)[0];
      if (f) f.src = u;
    }, link)
    .catch(() => {});
}

// General rule for any site: the player (the biggest visible video or player-sized iframe of the
// page) is scrolled to the middle of the screen when part of it is out of view, so clicks and the
// pointer over it land on the player itself (players below the fold get neither otherwise).
async function centerPlayer(page) {
  await page
    .evaluate(() => {
      const els = [...document.querySelectorAll("video, iframe")]
        .map((e) => [e, e.getBoundingClientRect()])
        .filter(([e, r]) => r.width >= 300 && r.height >= 150 && getComputedStyle(e).visibility !== "hidden")
        .sort((a, b) => b[1].width * b[1].height - a[1].width * a[1].height);
      if (!els.length) return;
      const [e, r] = els[0];
      if (r.top < 0 || r.bottom > innerHeight) e.scrollIntoView({ block: "center", inline: "center" });
    })
    .catch(() => {});
}

// Whether the site's robots.txt ("User-agent: *" group) disallows this address's path.
async function robotsClosed(page, href) {
  const u = new URL(href);
  const txt = await page
    .evaluate((o) => fetch(o + "/robots.txt", { credentials: "omit" }).then((r) => (r.ok ? r.text() : "")), u.origin)
    .catch(() => "");
  let mine = false;
  let grouped = false;
  const rules = [];
  for (const raw of txt.split(/\r?\n/)) {
    const [k, ...rest] = raw.replace(/#.*/, "").split(":");
    const key = k.trim().toLowerCase();
    const val = rest.join(":").trim();
    if (key === "user-agent") {
      if (grouped) mine = false;
      grouped = false;
      if (val === "*") mine = true;
    } else if (key === "allow" || key === "disallow") {
      grouped = true;
      if (mine && val) rules.push([key, val]);
    }
  }
  const p = decodeURIComponent(u.pathname) + u.search;
  const path = u.pathname + u.search;
  let best = null;
  for (const [key, val] of rules) {
    const re = new RegExp("^" + val.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\\\$$/, "$"));
    if ((re.test(path) || re.test(p)) && (!best || val.length > best[1].length || (val.length === best[1].length && key === "allow"))) best = [key, val];
  }
  return !!best && best[0] === "disallow";
}

// General rule for any site: a player that shows no <video> yet waits for a click on play.
// In every frame, the biggest visible play control (by its label, title, class or words);
// else the centre of the biggest player-like iframe. With `centre`, the centre of the page's
// player itself (video or iframe): players draw their big play button there, and a named control
// elsewhere (a bar, an overlay) is not always the one that starts the video. Returns where it
// clicked, in the page.
async function startPlayback(page, centre = false) {
  await centerPlayer(page);
  if (centre) {
    const r = await page
      .evaluate(() => {
        const els = [...document.querySelectorAll("video, iframe")]
          .map((e) => e.getBoundingClientRect())
          .filter((r) => r.width >= 300 && r.height >= 150 && r.top >= 0 && r.bottom <= innerHeight)
          .sort((a, b) => b.width * b.height - a.width * a.height);
        return els.length ? { x: els[0].left + els[0].width / 2, y: els[0].top + els[0].height / 2, frame: true } : null;
      })
      .catch(() => null);
    if (r) {
      await page.mouse.click(r.x, r.y);
      return r;
    }
  }
  let best = null;
  for (const frame of page.frames()) {
    const r = await frame
      .evaluate(() => {
        const words = /\b(play|watch|start)\b|تشغيل|مشاهدة|شاهد|إبدأ|ابدأ/i;
        const seen = (e) => {
          const r = e.getBoundingClientRect();
          const st = getComputedStyle(e);
          return r.width >= 16 && r.height >= 16 && st.visibility !== "hidden" && st.display !== "none" && !e.closest("[aria-hidden=true]") && r.bottom > 0 && r.top < innerHeight ? r : null;
        };
        let pick = null;
        for (const e of document.querySelectorAll("button, [role=button], a, div, span, svg")) {
          const label = [e.getAttribute("aria-label"), e.getAttribute("title"), e.getAttribute("data-testid"), typeof e.className === "string" ? e.className : "", e.children.length ? "" : e.textContent].join(" ");
          if (!words.test(label) || /pause|إيقاف|playlist|player-?settings|trailer|تريلر|اعلان|إعلان/i.test(label)) continue;
          // A link to another page (another item, a list) is not this page's play control.
          const link = e.closest("a[href]");
          if (link && link.href.split("#")[0] !== location.href.split("#")[0]) continue;
          const r = seen(e);
          if (r && (!pick || r.width * r.height > pick.a)) pick = { x: r.left + r.width / 2, y: r.top + r.height / 2, a: r.width * r.height };
        }
        if (pick) return pick;
        for (const f of document.querySelectorAll("iframe")) {
          const r = seen(f);
          if (r && r.width >= 300 && r.height >= 150 && (!pick || r.width * r.height > pick.a)) pick = { x: r.left + r.width / 2, y: r.top + r.height / 2, a: r.width * r.height, frame: true };
        }
        return pick;
      })
      .catch(() => null);
    if (!r) continue;
    const o = await frameOffset(frame);
    const c = { x: r.x + o.x, y: r.y + o.y, a: r.a, frame: r.frame };
    // A real control beats an iframe's centre; then the bigger.
    if (!best || (best.frame && !c.frame) || (!!best.frame === !!c.frame && c.a > best.a)) best = c;
  }
  if (!best) return null;
  await page.mouse.click(best.x, best.y);
  return best;
}

// MDM is asked to quit through its API; whatever of its process group still runs after 10 s is
// stopped, so the next item starts with a fresh app and nothing of this one answers the extension.
async function stopApp(app) {
  const gone = () => {
    try {
      process.kill(-app.pid, 0);
      return false;
    } catch {
      return true;
    }
  };
  for (let i = 0; i < 20 && !gone(); i++) await sleep(500);
  for (const sig of ["SIGTERM", "SIGKILL"]) {
    if (gone()) return;
    try {
      process.kill(-app.pid, sig);
    } catch {}
    for (let i = 0; i < 10 && !gone(); i++) await sleep(300);
  }
  if (!gone()) log("MDM did not stop");
}

// Sign-ins the owner made in the runner's browser (login.yml) come back here: only the cookies of the
// item's own site, from the file worker.py wrote (MDM_SESSIONS, readable by this user only).
function sessionCookies(pageUrl) {
  let host = "";
  try { host = new URL(pageUrl).hostname.toLowerCase(); } catch { return []; }
  let sessions = [];
  try { sessions = JSON.parse(fs.readFileSync(process.env.MDM_SESSIONS || "", "utf8")); } catch { return []; }
  if (!Array.isArray(sessions)) return [];
  const out = [];
  for (const s of sessions) {
    const dom = String(s?.domain || "");
    if (!dom || !(host === dom || host.endsWith("." + dom))) continue;
    for (const c of Array.isArray(s.cookies) ? s.cookies : []) {
      if (typeof c?.name !== "string" || typeof c?.value !== "string" || typeof c?.domain !== "string") continue;
      const cookie = { name: c.name, value: c.value, domain: c.domain, path: typeof c.path === "string" ? c.path : "/",
        secure: c.secure === true, httpOnly: c.httpOnly === true, sameSite: ["Strict", "Lax", "None"].includes(c.sameSite) ? c.sameSite : "Lax" };
      if (typeof c.expires === "number" && c.expires > 0) cookie.expires = c.expires;
      out.push(cookie);
    }
  }
  return out;
}

async function main() {
  prepareData();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "mdm-chrome-"));
  registerHost(profile);
  const env = { ...process.env, MDM_DATA_DIR: DATA, MDM_OFFSCREEN: "1" };

  fs.rmSync(path.join(DATA, "ipc.json"), { force: true });
  // Its own process group, so the app and every helper it starts are stopped together at the end.
  const app = spawn("misterdownloadmanager", [], { env, stdio: "ignore", detached: true });
  let browser;
  try {
    await until("MDM to start", 30_000, () => mdmList().then(() => true));
    log("MDM running");

    browser = await puppeteer.launch({
      executablePath: CHROME,
      // A real window on xvfb's screen: some players don't load in headless Chrome.
      headless: false,
      pipe: true,
      userDataDir: profile,
      env,
      ignoreDefaultArgs: ["--disable-extensions", "--disable-component-extensions-with-background-pages"],
      args: ["--enable-unsafe-extension-debugging", "--password-store=basic", "--no-first-run", "--window-size=1280,900"],
      defaultViewport: { width: 1280, height: 900 },
    });
    const bcdp = await browser.target().createCDPSession();
    const { id } = await bcdp.send("Extensions.loadUnpacked", { path: EXTENSION });
    await until("the extension's service worker", 15_000, async () =>
      browser.targets().find((t) => t.type() === "service_worker" && t.url().includes(id)),
    );
    log(`extension ${id} loaded`);

    const signed = sessionCookies(url);
    if (signed.length) {
      await bcdp.send("Storage.setCookies", { cookies: signed }).catch(() => {});
      log(`${signed.length} saved sign-in cookie(s) restored`);
    }

    const before = new Set((await mdmList()).map((d) => d.id));
    const page = await browser.newPage();
    // General rule for any site: a click on a player often opens an ad in a new tab, which takes
    // the front, and Chrome holds back video in a tab that isn't in front. Web tabs opened from the
    // page are closed at once and the item's page comes back to the front (extension pages stay).
    browser.on("targetcreated", async (t) => {
      if (t.type() !== "page" || !/^https?:|^about:blank/.test(t.url() || "about:blank") || !t.opener()) return;
      const ad = await t.page().catch(() => null);
      if (!ad || ad === page) return;
      await ad.close().catch(() => {});
      await page.bringToFront().catch(() => {});
      log("a tab opened by a click was closed");
    });
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60_000 });
    await sleep(3000);
    const watch = await watchPageLink(page);
    if (watch) {
      // The site's own rules for robots (robots.txt, the "*" group) are kept: a watch page it
      // closes to robots is not opened, and the item fails with that reason.
      if (await robotsClosed(page, watch)) throw Object.assign(new Error("the site's robots.txt closes its watch pages to robots"), { fatal: true });
      log("no player on the item's page: its watch page is opened");
      await page.goto(watch, { waitUntil: "domcontentloaded", timeout: 60_000 });
    }
    const startUrl = page.url();
    let tried = 0;
    let clicksHere = 0;
    const servers = await serverLinks(page);
    if (servers.length) log(`${servers.length} servers listed in the page's code`);
    let server = 0;
    let since = Date.now();
    const v = await until(
      "the page's video",
      90_000 + 30_000 * Math.min(servers.length, 5),
      async () => {
        // A click on the player can open an ad in place of the page: back to the watch page.
        if (page.url() !== startUrl && !page.url().startsWith(startUrl)) {
          log("a click took the page away: back to the watch page");
          await page.goto(startUrl, { waitUntil: "domcontentloaded", timeout: 60_000 }).catch(() => {});
          if (server) await useServer(page, servers[server]);
          return null;
        }
        const box = await videoBox(page);
        if (box) return box;
        // No video 30 s after a server came up (5 s when it refused): the next listed server.
        const refused = await playerRefused(page);
        if (servers.length > server + 1 && Date.now() - since > (refused ? 5_000 : 30_000)) {
          if (refused) log(`server ${server + 1} refused to open in the page`);
          // Probe runs keep what each server showed before it was left.
          if (process.env.MDM_SHOTS) await page.screenshot({ path: path.join(process.env.MDM_SHOTS, `server-${server + 1}.png`) }).catch(() => {});
          server += 1;
          since = Date.now();
          await useServer(page, servers[server]);
          log(`server ${server + 1} of ${servers.length}: ${new URL(servers[server]).hostname}`);
          return null;
        }
        if (Date.now() - tried > 6000) {
          tried = Date.now();
          // Turns: the named play control, then the player's centre.
          const at = await startPlayback(page, clicksHere++ % 2 === 1);
          if (at) log(`clicked play at ${Math.round(at.x)},${Math.round(at.y)}${at.frame ? " (player frame)" : ""}`);
        }
        return null;
      },
      1000,
    );
    log("video on the page");
    let lastSeen = "";
    if (process.env.MDM_SHOTS) await page.screenshot({ path: path.join(process.env.MDM_SHOTS, "page.png") }).catch(() => {});

    // The pointer over the video brings the button; it looks the video up, then names the best
    // quality on its chip ("ready").
    // The pointer over the video brings the extension's button. When its state can be read, it is
    // clicked once it names the best quality ("ready"); when the page hides it from us, it is
    // clicked where it shows (its left part, "Download video") after the look-up's time. Done once
    // MDM's own list has the download; clicked again every 20 s, at most 4 times.
    await until(
      "the extension's button over the video",
      LOOKUP_MS,
      async () => {
        await page.mouse.move(v.x + (Date.now() % 3), v.y);
        const st = await buttonState(page);
        const seen = st ? `${st.state} "${st.label}" "${st.chip}"` : (await hostBox(page)) ? "shown" : "no button";
        if (seen !== lastSeen) log(`button: ${(lastSeen = seen)}`);
        if (st && ["err", "drm", "off"].includes(st.state)) throw Object.assign(new Error(`button: ${st.state} ${st.label}`), { fatal: true });
        return seen !== "no button";
      },
      700,
    );
    // The look-up's time, then a click on its main part: the best quality at once, or MDM's
    // Download video window, confirmed with Enter. Done once MDM's list has the download.
    await sleep(8000);
    let clicks = 0;
    let lastTry = 0;
    let lastEnter = 0;
    const item = await until(
      "MDM to take the video",
      LOOKUP_MS,
      async () => {
        if (page.url() !== startUrl && !page.url().startsWith(startUrl)) {
          log("the page moved to another video: back to the item's page");
          await page.goto(startUrl, { waitUntil: "domcontentloaded", timeout: 60_000 });
          await sleep(4000);
        }
        // MDM's API can stay silent while its window looks a page up; the window is still driven.
        const got = (await mdmList().catch(() => [])).find((d) => !before.has(d.id));
        if (got) return got;
        if (Date.now() - lastTry > 25_000 && clicks < 4) {
          // The button shows while the pointer moves over the video (and fades 1.5 s after it rests).
          const vb = (await videoBox(page)) || v;
          for (const dx of [-12, 6, 0]) {
            await page.mouse.move(vb.x + dx, vb.y + dx / 2, { steps: 4 });
            await sleep(150);
          }
          // Where it shows on the screen: its main part is its left side ("Download video").
          const st = await buttonState(page);
          const box = await hostBox(page);
          const at = box ? { x: box.x + Math.min(box.width * 0.35, 70), y: box.y + box.height / 2 } : null;
          if (at) {
            lastTry = Date.now();
            clicks += 1;
            await page.mouse.move(at.x, at.y);
            await sleep(200);
            await page.mouse.click(at.x, at.y);
            log(`clicked the extension's button (${clicks})${st ? `: ${st.state} "${st.label}" "${st.chip}"` : ""}`);
            if (clicks === 1 && process.env.MDM_SHOTS) await page.screenshot({ path: path.join(process.env.MDM_SHOTS, "clicked.png") }).catch(() => {});
          }
        }
        if (clicks && Date.now() - lastEnter > 5000) {
          lastEnter = Date.now();
          driveVideoWindow(startUrl);
        }
        return null;
      },
      1000,
    );
    log(`MDM took it (#${item.id})`);
    let last = "";
    const done = await until(
      "the download to finish",
      DOWNLOAD_MS,
      async () => {
        const d = (await mdmList()).find((x) => x.id === item.id);
        if (!d) return null;
        if (d.status === "Error") throw Object.assign(new Error(`MDM: ${d.error}`), { fatal: true });
        const p = d.size ? Math.floor((100 * d.downloaded) / d.size) : -1;
        const line = `${d.status} ${p}%`;
        if (line !== last) {
          last = line;
          console.log(JSON.stringify({ progress: p, status: d.status, speed: d.speed || 0 }));
        }
        return d.status === "Complete" ? d : null;
      },
      2000,
    );
    const size = fs.statSync(done.path).size;
    console.log(JSON.stringify({ ok: true, path: done.path, size }));
  } catch (e) {
    const page = browser && (await browser.pages().catch(() => []))[1];
    if (page && process.env.MDM_SHOTS) await page.screenshot({ path: path.join(process.env.MDM_SHOTS, "failed.png") }).catch(() => {});
    throw e;
  } finally {
    if (browser) await browser.close().catch(() => {});
    await ipc({ exit: true }).catch(() => {});
    await stopApp(app);
    fs.rmSync(profile, { recursive: true, force: true });
  }
}

main().catch((e) => {
  console.log(JSON.stringify({ ok: false, error: String(e.message || e).slice(0, 300) }));
  process.exitCode = 1;
});
