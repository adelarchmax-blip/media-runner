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

// The app's settings before its first start: the automation API on, no first-run windows,
// downloads into outDir.
function prepareData() {
  fs.mkdirSync(DATA, { recursive: true });
  fs.mkdirSync(outDir, { recursive: true });
  const file = path.join(DATA, "downloads.json");
  if (fs.existsSync(file)) return;
  const dirs = ["General", "Compressed", "Documents", "Music", "Programs", "Video"].map((c) => [c, outDir]);
  fs.writeFileSync(
    file,
    JSON.stringify({
      next_id: 1,
      items: [],
      version: 2,
      settings: {
        language: "en",
        language_asked: true,
        extension_never_ask: true,
        show_start_dialog: false,
        automation_api: true,
        category_dirs: dirs,
        torrent_handler: false,
      },
    }),
  );
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
async function buttonState(page) {
  for (const frame of page.frames()) {
    const host = await frame.$("mdm-video-button").catch(() => null);
    if (!host) continue;
    const cdp = frame.client;
    const { node } = await cdp.send("DOM.describeNode", { objectId: host.remoteObject().objectId, pierce: true, depth: 1 });
    const root = node.shadowRoots && node.shadowRoots[0];
    if (!root) continue;
    const { object } = await cdp.send("DOM.resolveNode", { backendNodeId: root.backendNodeId });
    const { result } = await cdp.send("Runtime.callFunctionOn", {
      objectId: object.objectId,
      returnByValue: true,
      functionDeclaration: `function () {
        const p = this.querySelector(".pill"), go = this.querySelector(".go");
        if (!p || !go) return null;
        const r = go.getBoundingClientRect();
        return { state: p.dataset.state, label: this.querySelector(".label").textContent,
                 chip: this.querySelector(".chip").textContent,
                 x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width };
      }`,
    });
    const v = result.value;
    if (!v) continue;
    const o = await frameOffset(frame);
    return { ...v, x: v.x + o.x, y: v.y + o.y };
  }
  return null;
}

// Ad networks' players and clips (any site): their videos are never the item.
const AD = /doubleclick|2mdn\.net|googlesyndication|imasdk|googleads|adservice|adnxs|springserve|spotx|teads|\/ads?\//i;

// The biggest visible video in any frame (not an ad's), its centre in the page's viewport.
async function videoBox(page) {
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
        return { x: r.left + r.width / 2, y: r.top + r.height / 2, area: r.width * r.height };
      }, AD.source)
      .catch(() => null);
    if (!r || (best && best.area >= r.area)) continue;
    const o = await frameOffset(frame);
    best = { x: r.x + o.x, y: r.y + o.y, area: r.area };
  }
  return best;
}

// General rule for any site: a player that shows no <video> yet waits for a click on play.
// In every frame, the biggest visible play control (by its label, title, class or words);
// else the centre of the biggest player-like iframe. Returns where it clicked, in the page.
async function startPlayback(page) {
  let best = null;
  for (const frame of page.frames()) {
    const r = await frame
      .evaluate(() => {
        const words = /\b(play|watch|start)\b|تشغيل|مشاهدة|شاهد|إبدأ|ابدأ/i;
        const seen = (e) => {
          const r = e.getBoundingClientRect();
          const st = getComputedStyle(e);
          return r.width >= 16 && r.height >= 16 && st.visibility !== "hidden" && st.display !== "none" && r.bottom > 0 && r.top < innerHeight ? r : null;
        };
        let pick = null;
        for (const e of document.querySelectorAll("button, [role=button], a, div, span, svg")) {
          const label = [e.getAttribute("aria-label"), e.getAttribute("title"), e.getAttribute("data-testid"), typeof e.className === "string" ? e.className : "", e.children.length ? "" : e.textContent].join(" ");
          if (!words.test(label) || /pause|إيقاف|playlist|player-?settings/i.test(label)) continue;
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

async function main() {
  prepareData();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "mdm-chrome-"));
  registerHost(profile);
  const env = { ...process.env, MDM_DATA_DIR: DATA, MDM_OFFSCREEN: "1" };

  fs.rmSync(path.join(DATA, "ipc.json"), { force: true });
  const app = spawn("misterdownloadmanager", [], { env, stdio: "ignore" });
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

    const before = new Set((await mdmList()).map((d) => d.id));
    const page = await browser.newPage();
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60_000 });
    let tried = 0;
    const v = await until(
      "the page's video",
      90_000,
      async () => {
        const box = await videoBox(page);
        if (box) return box;
        if (Date.now() - tried > 6000) {
          tried = Date.now();
          const at = await startPlayback(page);
          if (at) log(`clicked play at ${Math.round(at.x)},${Math.round(at.y)}${at.frame ? " (player frame)" : ""}`);
        }
        return null;
      },
      1000,
    );
    log("video on the page");
    let lastSeen = "";
    let lastClick = Date.now();
    if (process.env.MDM_SHOTS) await page.screenshot({ path: path.join(process.env.MDM_SHOTS, "page.png") }).catch(() => {});

    // The pointer over the video brings the button; it looks the video up, then names the best
    // quality on its chip ("ready").
    const ready = await until(
      "the extension's button to be ready",
      LOOKUP_MS,
      async () => {
        // A player that waits for its play button: one real click on the video every 15 s.
        if (Date.now() - lastClick > 15_000) {
          lastClick = Date.now();
          await page.mouse.click(v.x, v.y);
        }
        await page.mouse.move(v.x + (Date.now() % 3), v.y);
        const s = await buttonState(page);
        const seen = s ? `${s.state} "${s.label}" "${s.chip}"` : "no button";
        if (seen !== lastSeen) log(`button: ${(lastSeen = seen)}`);
        if (s && ["err", "drm", "off"].includes(s.state)) throw Object.assign(new Error(`button: ${s.state} ${s.label}`), { fatal: true });
        return s && s.state === "ready" && s.chip && s.w ? s : null;
      },
      700,
    );
    log(`button ready: ${ready.chip}`);
    await page.mouse.move(ready.x, ready.y);
    await sleep(150);
    const now = (await buttonState(page)) || ready;
    await page.mouse.click(now.x, now.y);
    log("clicked");

    const item = await until("the download in MDM's list", 60_000, async () => (await mdmList()).find((d) => !before.has(d.id)));
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
    await sleep(1500);
    app.kill();
    fs.rmSync(profile, { recursive: true, force: true });
  }
}

main().catch((e) => {
  console.log(JSON.stringify({ ok: false, error: String(e.message || e).slice(0, 300) }));
  process.exitCode = 1;
});
