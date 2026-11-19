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
    s.on("end", () => resolve(JSON.parse(answer.trim())));
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
  const reg = spawnSync("misterdownloadmanager-nmhost", ["--register"], { encoding: "utf8" });
  if (reg.status !== 0) throw new Error(`nmhost --register: ${reg.stderr || reg.stdout}`);
  const written = [...(reg.stdout || "").matchAll(/^registered \S+ in (.+)$/gm)].map((m) => path.join(m[1].trim(), `${HOST}.json`));
  const manifest = written.find((w) => !/mozilla/i.test(w) && fs.existsSync(w));
  if (!manifest) throw new Error(`no Chromium host manifest: ${reg.stdout}`);
  fs.mkdirSync(path.join(profile, "NativeMessagingHosts"), { recursive: true });
  fs.copyFileSync(manifest, path.join(profile, "NativeMessagingHosts", `${HOST}.json`));
}

// The extension's button lives in a closed shadow root: DevTools reaches it, page scripts can't.
async function buttonState(page, cdp) {
  const host = await page.$("mdm-video-button");
  if (!host) return null;
  const { node } = await cdp.send("DOM.describeNode", { objectId: host.remoteObject().objectId, pierce: true, depth: 1 });
  const root = node.shadowRoots && node.shadowRoots[0];
  if (!root) return null;
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
  return result.value;
}

async function videoBox(page) {
  return page.evaluate(() => {
    const v = [...document.querySelectorAll("video")].find((e) => e.getBoundingClientRect().width > 150);
    if (!v) return null;
    v.muted = true;
    v.play().catch(() => {});
    const r = v.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  });
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
      headless: true,
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
    const cdp = await page.createCDPSession();
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60_000 });
    const v = await until("the page's video", 60_000, () => videoBox(page));
    log("video on the page");

    // The pointer over the video brings the button; it looks the video up, then names the best
    // quality on its chip ("ready").
    const ready = await until(
      "the extension's button to be ready",
      LOOKUP_MS,
      async () => {
        await page.mouse.move(v.x + (Date.now() % 3), v.y);
        const s = await buttonState(page, cdp);
        if (s && ["err", "drm", "off"].includes(s.state)) throw Object.assign(new Error(`button: ${s.state} ${s.label}`), { fatal: true });
        return s && s.state === "ready" && s.chip && s.w ? s : null;
      },
      700,
    );
    log(`button ready: ${ready.chip}`);
    await page.mouse.move(ready.x, ready.y);
    await sleep(150);
    const now = (await buttonState(page, cdp)) || ready;
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
  process.exit(1);
});
