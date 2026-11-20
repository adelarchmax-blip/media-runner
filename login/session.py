"""The owner's sign-in browser (login.yml): real Chrome on a virtual screen, shown in the dashboard
through noVNC over a temporary Cloudflare tunnel (free, no account), with a random screen password.

- The site's address comes from the controller by the session number only (the public log never
  shows an address, the tunnel or the password: they are masked and never printed).
- Chrome has the full toolbar (address/search bar, home, back, settings) and two ad blockers
  installed and pinned by Chrome's own policy (Chrome downloads them from its Web Store and checks
  their signatures itself). Chrome saves no passwords.
- When the owner presses "done" in the dashboard, the cookies are read from Chrome (DevTools on
  127.0.0.1 only) and sent to the controller, which keeps only the cookies of the owner's sites.
- Ends by itself after 24 minutes.
"""
import json
import os
import re
import secrets
import string
import subprocess
import sys
import time

import requests

API_URL = os.environ["RUNNER_API_URL"].rstrip("/")
API_TOKEN = os.environ["RUNNER_API_TOKEN"]
SESSION = os.environ.get("LOGIN_SESSION", "")
NOVNC = os.environ.get("NOVNC_DIR", "/opt/novnc")
HERE = os.path.dirname(os.path.abspath(__file__))
LIFETIME = 24 * 60
DISPLAY = ":99"
SCREEN = (1366, 768)
POLICY_DIR = "/etc/opt/chrome/policies/managed"
EXTENSIONS = {   # Chrome Web Store ids: uBlock Origin Lite, AdBlocker Ultimate
    "ddkjiahejlhfcafbddmgiahcphecmpfh": "uBlock Origin Lite",
    "ohahllgiabjaoigichmmfljhkcfikeof": "AdBlocker Ultimate",
}
STORE = "https://clients2.google.com/service/update2/crx"
_TUNNEL = re.compile(r"https://[a-z0-9-]{1,80}\.trycloudflare\.com")


def log(msg: str):
    print(f"[login] {msg}", flush=True)


def api(op: str, **data) -> dict:
    for attempt in range(3):
        try:
            r = requests.post(f"{API_URL}/miinspact-login", json={"op": op, "session": SESSION, **data}, timeout=30,
                              headers={"X-Runner-Token": API_TOKEN})
            if r.status_code < 500:
                return r.json() if r.content else {}
        except (requests.RequestException, ValueError):
            pass
        time.sleep(3 * (attempt + 1))
    return {}


def mask(value: str):
    print(f"::add-mask::{value}", flush=True)


def policy(home: str) -> dict:
    p = {
        "ExtensionSettings": {i: {"installation_mode": "force_installed", "update_url": STORE,
                                  "toolbar_pin": "force_pinned"} for i in EXTENSIONS},
        "ShowHomeButton": True,
        "BookmarkBarEnabled": True,
        "PasswordManagerEnabled": False,       # cookies only: no password is kept on the runner
        "BrowserSignin": 0,
        "SyncDisabled": True,
        "MetricsReportingEnabled": False,
        "DefaultBrowserSettingEnabled": False,
        "PromotionalTabsEnabled": False,
        "TranslateEnabled": True,
    }
    if home:
        p.update(HomepageLocation=home, HomepageIsNewTabPage=False, RestoreOnStartup=4, RestoreOnStartupURLs=[home])
    else:
        p.update(HomepageIsNewTabPage=True)
    return p


def write_policy(home: str):
    data = json.dumps(policy(home))
    subprocess.run(["sudo", "mkdir", "-p", POLICY_DIR], check=True)
    subprocess.run(["sudo", "tee", f"{POLICY_DIR}/miinspact.json"], input=data, text=True, check=True,
                   stdout=subprocess.DEVNULL)


def start(cmd, **kw):
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, **kw)


def wait_port(port: int, seconds: int = 30) -> bool:
    import socket
    end = time.time() + seconds
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(1)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.5)
    return False


def main() -> int:
    if not re.fullmatch(r"[a-f0-9]{24}", SESSION):
        log("bad session number")
        return 1
    req = api("get")
    url = req.get("url") or ""
    if req.get("error") or (url and not re.match(r"^https?://[^\s\"'<>\\]+$", url)):
        log("the controller has no such sign-in request")
        return 1
    if url:
        mask(url)
    procs = []
    try:
        write_policy(url)
        env = {**os.environ, "DISPLAY": DISPLAY}
        procs.append(start(["Xvfb", DISPLAY, "-screen", "0", f"{SCREEN[0]}x{SCREEN[1]}x24", "-nolisten", "tcp"]))
        time.sleep(1.5)
        profile = os.path.join(os.environ.get("RUNNER_TEMP") or "/tmp", "login-chrome")
        os.makedirs(profile, exist_ok=True)
        chrome = ["google-chrome", f"--user-data-dir={profile}", "--remote-debugging-port=9222",
                  "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
                  "--password-store=basic", f"--window-size={SCREEN[0]},{SCREEN[1]}", "--window-position=0,0",
                  "--lang=ar"]
        procs.append(start(chrome + ([url] if url else []), env=env))
        if not wait_port(9222, 40):
            raise RuntimeError("Chrome did not start")
        log("Chrome is up")

        password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
        mask(password)
        pw_file = os.path.join(os.environ.get("RUNNER_TEMP") or "/tmp", "vnc.pass")
        subprocess.run(["x11vnc", "-storepasswd", password, pw_file], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.chmod(pw_file, 0o600)
        procs.append(start(["x11vnc", "-display", DISPLAY, "-rfbport", "5900", "-localhost", "-rfbauth", pw_file,
                            "-forever", "-shared", "-noxdamage", "-quiet"]))
        if not wait_port(5900, 20):
            raise RuntimeError("the screen server did not start")
        procs.append(start(["websockify", "--web", NOVNC, "6080", "127.0.0.1:5900"]))
        if not wait_port(6080, 20):
            raise RuntimeError("the web screen did not start")
        cf_log = os.path.join(os.environ.get("RUNNER_TEMP") or "/tmp", "cf.log")
        procs.append(start(["cloudflared", "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:6080",
                            "--logfile", cf_log, "--loglevel", "info"]))
        view = ""
        end = time.time() + 60
        while time.time() < end and not view:
            time.sleep(1)
            try:
                with open(cf_log) as f:
                    m = _TUNNEL.search(f.read())
            except OSError:
                m = None
            if m:
                view = m.group(0)
                mask(view)
        if not view:
            raise RuntimeError("no tunnel")
        end = time.time() + 60          # the new address takes a few seconds to answer
        while time.time() < end:
            try:
                if requests.get(view + "/vnc.html", timeout=10).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(2)
        api("ready", view=view, password=password)
        log("screen ready, waiting for the owner")

        deadline = time.time() + LIFETIME * 60
        action = "wait"
        while time.time() < deadline:
            action = api("poll").get("action") or "wait"
            if action in ("done", "cancel"):
                break
            time.sleep(3)
        if action != "done":
            log("ended without saving (" + ("cancelled" if action == "cancel" else "time is up") + ")")
            if action != "cancel":
                api("fail", error="الوقت خلص من غير ما تضغط «خلصت»")
            return 0
        out = subprocess.run(["node", os.path.join(HERE, "..", "mdm", "cookies.mjs")], capture_output=True, text=True,
                             timeout=60)
        try:
            got = json.loads(out.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            got = {}
        cookies = got.get("cookies") if isinstance(got.get("cookies"), list) else []
        res = api("save", cookies=cookies, ua=str(got.get("ua") or "")[:400])
        log(f"sign-in saved ({res.get('cookies', 0)} cookie(s))" if res.get("ok") else "sign-in was not saved")
        return 0
    except Exception as e:
        log(f"stopped: {type(e).__name__}")
        api("fail", error="متصفح الدخول ماقدرش يبدأ على الرانر، جرّب تاني")
        return 1
    finally:
        for p in reversed(procs):
            try:
                os.killpg(p.pid, 9)
            except (ProcessLookupError, PermissionError):
                pass


if __name__ == "__main__":
    sys.exit(main())
