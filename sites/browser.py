"""Every site page the runner reads goes through real Google Chrome on a virtual screen, with two ad
blockers (uBlock Origin Lite, AdBlocker Ultimate) installed and pinned by Chrome's own policy, and the
owner's saved sign-ins. Python drives it over DevTools on 127.0.0.1 only (Playwright, no bundled browser).

One Chrome starts once; every worker thread opens its own tab (`with chrome.tab() as tab`), with its
own DevTools connection, so a few sites are read side by side. On a tab:
- fetch(url) -> (status, final_url, html): the page as Chrome built it.
- get_bytes(url) -> bytes | None: a file (feed, .torrent) asked by the page itself, so the site's own
  cookies and Chrome's network go with it. Size-capped.
Tabs an ad opens are closed at once; other workers' tabs are never touched.
The policy file exists only while this browser runs, so the download browser (mdm/grab.mjs) is unchanged.
Logs never carry an address.
"""
import base64
import json
import logging
import os
import socket
import subprocess
import time
from urllib.parse import urlsplit

POLICY_DIR = "/etc/opt/chrome/policies/managed"
POLICY_FILE = f"{POLICY_DIR}/miinspact-sites.json"
EXTENSIONS = ("ddkjiahejlhfcafbddmgiahcphecmpfh",   # uBlock Origin Lite
              "ohahllgiabjaoigichmmfljhkcfikeof")   # AdBlocker Ultimate
STORE = "https://clients2.google.com/service/update2/crx"
DISPLAY = ":98"
PORT = 9223
PAGE_TIMEOUT = 30_000
MAX_BYTES = 3_000_000
MAX_FILE = 2 * 1024 * 1024

_GET_FILE = """async ([url, cap]) => {
  const r = await fetch(url, { credentials: "include", redirect: "follow" });
  if (!r.ok) return null;
  const buf = new Uint8Array(await r.arrayBuffer());
  if (buf.length > cap) return null;
  let s = ""; for (let i = 0; i < buf.length; i += 32768) s += String.fromCharCode(...buf.subarray(i, i + 32768));
  return btoa(s);
}"""


def policy() -> dict:
    return {"ExtensionSettings": {i: {"installation_mode": "force_installed", "update_url": STORE} for i in EXTENSIONS},
            "PasswordManagerEnabled": False, "BrowserSignin": 0, "SyncDisabled": True,
            "MetricsReportingEnabled": False, "PromotionalTabsEnabled": False}


def _sudo_write(path: str, data: str):
    subprocess.run(["sudo", "mkdir", "-p", os.path.dirname(path)], check=True)
    subprocess.run(["sudo", "tee", path], input=data, text=True, check=True, stdout=subprocess.DEVNULL)


def _wait_port(port: int, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.5)
    return False


def _cookies(sessions_file: str) -> list:
    """Saved sign-ins (written by worker.restore_sessions) in Playwright's cookie shape."""
    try:
        with open(sessions_file) as f:
            sessions = json.load(f)
    except (OSError, ValueError, TypeError):
        return []
    out = []
    for s in sessions if isinstance(sessions, list) else []:
        for c in (s.get("cookies") or []) if isinstance(s, dict) else []:
            if not isinstance(c, dict) or not c.get("name") or not c.get("domain"):
                continue
            item = {"name": str(c["name"]), "value": str(c.get("value", "")), "domain": str(c["domain"]),
                    "path": str(c.get("path") or "/"), "secure": bool(c.get("secure")),
                    "httpOnly": bool(c.get("httpOnly"))}
            if isinstance(c.get("expires"), (int, float)) and c["expires"] > 0:
                item["expires"] = float(c["expires"])
            out.append(item)
    return out


def _connect():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        return pw, pw.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
    except Exception:
        pw.stop()
        raise


def _quiet(*steps):
    for step in steps:
        try:
            step()
        except Exception:  # shutting down: nothing left to save
            pass


class Tab:
    """One worker's own tab, on its own connection (Playwright objects stay in the thread that made them)."""

    def __enter__(self):
        self.pw, self.browser = _connect()
        self.page = self.browser.contexts[0].new_page()
        self.page.on("popup", lambda p: _quiet(p.close))
        return self

    def _page(self):
        if self.page.is_closed():
            self.page = self.browser.contexts[0].new_page()
            self.page.on("popup", lambda p: _quiet(p.close))
        return self.page

    def fetch(self, url: str):
        page = self._page()
        resp = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        try:
            page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:  # busy pages: what is there after a few seconds is enough
            pass
        status = resp.status if resp else 0
        ctype = (resp.headers.get("content-type", "") if resp else "").lower()
        if resp and ("xml" in ctype or "text/plain" in ctype):
            text = resp.body()[:MAX_BYTES].decode("utf-8", errors="replace")
        else:
            text = page.content()[:MAX_BYTES]
        return status, page.url, text

    def get_bytes(self, url: str):
        page = self._page()
        here, there = urlsplit(page.url), urlsplit(url)
        if (here.scheme, here.netloc) != (there.scheme, there.netloc):
            page.goto(f"{there.scheme}://{there.netloc}/", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        data = page.evaluate(_GET_FILE, [url, MAX_FILE])
        return base64.b64decode(data) if data else None

    def __exit__(self, *exc):
        _quiet(lambda: self.page.close(), lambda: self.browser.close(), lambda: self.pw.stop())
        return False


class Chrome:
    """The Chrome process (virtual screen, blockers' policy, sign-ins); `tab()` gives each worker a tab."""

    def __init__(self, sessions_file: str = ""):
        self.sessions_file = sessions_file
        self.procs = []

    def __enter__(self):
        _sudo_write(POLICY_FILE, json.dumps(policy()))
        env = {**os.environ, "DISPLAY": DISPLAY}
        self.procs.append(subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", "1366x900x24", "-nolisten", "tcp"],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(1.5)
        # the profile folder is kept between runs only for the blockers' files (sites.yml caches that
        # sub-folder alone), so Chrome does not download them again; sign-ins are never cached
        profile = os.environ.get("SITES_PROFILE") or os.path.join(os.environ.get("RUNNER_TEMP") or "/tmp", "sites-chrome")
        os.makedirs(profile, exist_ok=True)
        self.procs.append(subprocess.Popen(
            [os.environ.get("CHROME_BIN", "google-chrome"), f"--user-data-dir={profile}",
             f"--remote-debugging-port={PORT}", "--remote-debugging-address=127.0.0.1", "--no-first-run",
             "--no-default-browser-check", "--password-store=basic", "--window-size=1366,900", "--lang=ar",
             "about:blank"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        if not _wait_port(PORT, 40):
            self.__exit__(None, None, None)
            raise RuntimeError("Chrome did not start")
        started = time.time()
        pw, browser = _connect()
        try:
            ctx = browser.contexts[0]
            cookies = _cookies(self.sessions_file)
            if cookies:
                try:
                    ctx.add_cookies(cookies)
                except Exception as e:  # a bad cookie must not stop the work
                    logging.info(f"sites browser: sign-ins not restored ({type(e).__name__})")
            ready = self.blockers(ctx)
        finally:
            _quiet(browser.close, pw.stop)
        logging.info(f"sites browser: Chrome up, ad blockers: {ready} of {len(EXTENSIONS)} "
                     f"in {time.time() - started:.0f}s")
        return self

    @staticmethod
    def blockers(ctx, wait: float = 45) -> int:
        """Chrome installs the policy extensions from its Web Store at start; waits until both run."""
        end = time.time() + wait
        while True:
            urls = [w.url for w in ctx.service_workers] + [p.url for p in ctx.background_pages]
            ready = sum(1 for i in EXTENSIONS if any(i in u for u in urls))
            if ready == len(EXTENSIONS) or time.time() > end:
                return ready
            time.sleep(1)

    def tab(self) -> Tab:
        return Tab()

    def __exit__(self, *exc):
        for p in reversed(self.procs):
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        subprocess.run(["sudo", "rm", "-f", POLICY_FILE], check=False)
        return False
