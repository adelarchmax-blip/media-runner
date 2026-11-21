"""Ephemeral worker: pull jobs from the controller API, download, build poster, upload, report.
Logs never contain titles, URLs or remote paths (public repository)."""
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time

import requests

from poster import poster_thumb, process_poster, work_details
from sites import hook, telegram     # telegram takes its keys out of the environment first

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
for noisy in ("urllib3", "requests", "PIL"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

API_URL = os.environ["RUNNER_API_URL"].rstrip("/")
API_TOKEN = os.environ["RUNNER_API_TOKEN"]
REMOTE = os.environ["RCLONE_REMOTE"].rstrip(":")
MEDIA_BASE = os.environ["MEDIA_BASE_PATH"].strip("/")
RUN_ID = os.environ.get("RUN_ID", "local")
TORRENTS_ONLY = os.environ.get("LANES", "all") == "torrents"     # the owner can skip the watch-page lane
WORK_DIR = os.path.abspath("work")

START = float(os.environ.get("RUN_STARTED") or 0) or time.time()
STAGE = {"now": "working"}         # what the dashboard shows this run is doing
MAX_SECONDS = 350 * 60
STOP_MARGIN = 25 * 60          # لا نبدأ مادة جديدة إذا تبقى أقل من 25 دقيقة
REPORT_INTERVAL = 10           # ثوانٍ بين تقارير التقدم
BATCH_SIZE = 3
# Torrents run in a rolling batch: this many at once, and the moment one ends its slot takes the next.
TORRENT_SLOTS = max(1, min(6, int(os.environ.get("TORRENT_SLOTS") or 3)))
EMPTY_RECHECK = 60             # ثواني: الطابور فاضي بس لسه في شغل، نسأل تاني عن جديد
DISK_MARGIN = 3 * 1073741824   # مساحة فاضية لازم تفضل على جهاز الرانر
MIN_VIDEO_BYTES = int(float(os.environ.get("MIN_VIDEO_MB", "0.5")) * 1048576)
MIN_SECONDS = float(os.environ.get("MIN_SECONDS", "5"))
MDM_GRAB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mdm", "grab.mjs")


def api(path: str, payload: dict, timeout: float = 30) -> dict:
    return hook.call(API_URL, API_TOKEN, path, payload, timeout)


def report(item_id: int, status: str, stage: str = "", **extra) -> dict:
    return api("miinspact-report", {"item_id": item_id, "status": status, "run_id": RUN_ID,
                             "run_started_at": START, "stage": stage or STAGE["now"], **extra})


def set_stage(stage: str):
    """Moves the dashboard's stage line at once (item stages travel with the item's reports)."""
    STAGE["now"] = stage
    api("miinspact-report", {"heartbeat_only": True, "run_id": RUN_ID, "run_started_at": START, "stage": stage})


def heartbeat_loop(stop: threading.Event):
    while not stop.wait(60):
        api("miinspact-report", {"heartbeat_only": True, "run_id": RUN_ID, "run_started_at": START,
                                 "stage": STAGE["now"]})


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', " ", name or "Unknown")
    return re.sub(r"\s+", " ", name).strip()[:150] or "Unknown"


_SECTION_PATH = re.compile(r"^[A-Za-z_]+(?:/[A-Za-z_]+)?$")


def library_folder(item: dict, default: str) -> str:
    """The library folder the controller chose for this item (its main library and, inside it, the
    owner's sub-section). Anything malformed falls back to the default library."""
    section = item.get("section_path") or ""
    if not _SECTION_PATH.match(section):
        section = default
    category = safe_name(item.get("category")) if item.get("category") else ""
    if category and category.strip(".") and not category.startswith("."):
        return f"{section}/{category}"
    return section


def destination(item: dict) -> tuple:
    """(مجلد الفيديو، اسم الملف، مجلد البوستر) بنفس تسمية مكتبات Jellyfin"""
    title = safe_name(item.get("clean_title"))
    year = f" ({item['year']})" if item.get("year") else ""
    arabic = bool(item.get("is_arabic"))
    ctype = item.get("content_type", "movie")
    if ctype == "series":
        season = int(item.get("season") or 1)
        episode = int(item.get("episode") or 1)
        lib = library_folder({**item, "category": None}, f"Series/{'Arabic_Series' if arabic else 'Foreign_Series'}")
        root = f"{MEDIA_BASE}/{lib}/{title}"
        return f"{root}/Season {season:02d}", f"{title} - S{season:02d}E{episode:02d}", root
    if ctype == "play":
        folder = f"{MEDIA_BASE}/{library_folder(item, 'Plays')}/{title}{year}"
        return folder, f"{title}{year}", folder
    lib = library_folder(item, f"Movies/{'Arabic_Movies' if arabic else 'Foreign_Movies'}")
    folder = f"{MEDIA_BASE}/{lib}/{title}{year}"
    return folder, f"{title}{year}", folder


class Stopped(Exception):
    """The owner stopped this one item from the dashboard."""


class Progress:
    def __init__(self, item_id: int):
        self.item_id = item_id
        self.last = 0.0
        self.dl_pct = 0
        self.speed = 0.0
        self.size_mb = 0.0
        self.stage = "item_details"    # each item its own stage: torrents run side by side
        self.stopped = False
        self.procs = []                # this item's running commands, stopped at once on "stop"
        self.lock = threading.Lock()   # the poster lookup reports only while the item is still running
        self.done = False
        self.poster = ""               # "real" or "text" (written, no picture found) once the poster is ready

    def finish(self):
        with self.lock:
            self.done = True

    def push(self, force: bool = False, **extra):
        if force or time.time() - self.last >= REPORT_INTERVAL:
            self.last = time.time()
            res = report(self.item_id, "downloading", stage=self.stage, dl_pct=self.dl_pct,
                         speed_mbs=round(self.speed, 2), size_mb=round(self.size_mb, 1), **extra)
            if isinstance(res, dict) and res.get("stop") is True:
                self.stop_now()
        self.check()

    def stop_now(self):
        self.stopped = True
        for proc in list(self.procs):
            _kill_group(proc)

    def check(self):
        if self.stopped:
            raise Stopped()


_URL = re.compile(r"\S*(?:https?://|www\.)\S*", re.I)


def last_steps(log_path: str, count: int = 15) -> list:
    """The download script's last steps, for the log of a failed item: only its own `[grab]` lines,
    each cut short and with any web address removed (the log is public)."""
    try:
        with open(log_path, errors="replace") as f:
            lines = [ln.strip() for ln in f if ln.startswith("[grab]")]
    except OSError:
        return []
    return [_URL.sub("<address>", ln)[:160] for ln in lines[-count:]]


TORRENT_GROUPS = set()          # process groups of torrents running now: a page item never stops them
_GROUPS_LOCK = threading.Lock()


def stop_leftover_mdm():
    """Mister Download Manager stops itself after each page item; whatever of it still runs is stopped
    here, so the next item starts with a fresh app. Torrents running beside it are left alone."""
    left = subprocess.run(["pgrep", "-i", "misterdownload"], capture_output=True, text=True).stdout.split()
    with _GROUPS_LOCK:
        groups = set(TORRENT_GROUPS)
    stray = []
    for pid in left:
        try:
            if os.getpgid(int(pid)) not in groups:
                stray.append(int(pid))
        except (ProcessLookupError, ValueError):
            pass
    if stray:
        logging.warning(f"MDM left {len(stray)} process(es) running: stopping them")
        for pid in stray:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass


def _start_group(cmd, env, **kw):
    """A torrent command in its own process group, so it (and only it) can be stopped as a whole."""
    proc = subprocess.Popen(cmd, env=env, start_new_session=True, **kw)
    with _GROUPS_LOCK:
        TORRENT_GROUPS.add(proc.pid)
    return proc


def _kill_group(proc):
    """Stops a command started in its own session (start_new_session) with everything it started."""
    try:
        os.killpg(proc.pid, 9)
    except (ProcessLookupError, PermissionError):
        pass


def _stop_group(proc):
    try:
        os.killpg(proc.pid, 9)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    with _GROUPS_LOCK:
        TORRENT_GROUPS.discard(proc.pid)


_DISK = {"held": {}}
_DISK_LOCK = threading.Lock()


def hold_disk(item_id: int, need: int, wait: float = 30 * 60) -> None:
    """Keeps room for a torrent before it starts: free space minus what running torrents still need.
    Waits for others to finish (they free their room after upload); fails if it never fits."""
    until = time.time() + wait
    while True:
        with _DISK_LOCK:
            others = sum(v for k, v in _DISK["held"].items() if k != item_id)
            if shutil.disk_usage(WORK_DIR if os.path.isdir(WORK_DIR) else ".").free - others - DISK_MARGIN >= need:
                _DISK["held"][item_id] = need
                return
            if not others or time.time() > until:
                raise RuntimeError(f"no disk space for this torrent ({need / 1073741824:.1f} GB)")
        time.sleep(20)


def free_disk(item_id: int) -> None:
    with _DISK_LOCK:
        _DISK["held"].pop(item_id, None)


def download(item: dict, out_base: str, progress: Progress) -> str:
    """التحميل عبر إضافة Mister Download Manager الرسمية داخل كروم، والبرنامج نفسه ينزّل الملف"""
    out_dir = os.path.dirname(out_base) + "/mdm"
    os.makedirs(out_dir, exist_ok=True)
    steps_log = os.path.dirname(out_base) + "/grab.log"
    with open(steps_log, "w") as steps:
        proc = subprocess.Popen(["xvfb-run", "-a", "-s", "-screen 0 2400x1000x24", "node", MDM_GRAB, item["watch_url"], out_dir],
                                stdout=subprocess.PIPE, stderr=steps, text=True, start_new_session=True)
        progress.procs.append(proc)
        result = {}
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "progress" in msg:
                progress.dl_pct = max(0, msg["progress"])
                progress.speed = (msg.get("speed") or 0) / 1048576
                progress.push()
            else:
                result = msg
        proc.wait()
        progress.procs.remove(proc)
    stop_leftover_mdm()
    progress.check()
    if not result.get("ok"):
        steps_seen = last_steps(steps_log)
        for step in steps_seen:
            logging.info(f"  {step}")
        raise RuntimeError(f"MDM: {result.get('error', 'no result')} | " + " ; ".join(steps_seen[-6:]))
    src = result["path"]
    progress.size_mb = os.path.getsize(src) / 1048576
    dst = out_base + os.path.splitext(src)[1]
    shutil.move(src, dst)
    return dst, []


# ---------- torrent (Mister Download Manager's own torrent engine, no browser, no seeding) ----------

MDM_CLI = "misterdownloadmanager-cli"
_MAGNET = re.compile(r"magnet:\?[A-Za-z0-9%:=&._~+\-/]+")
_BTIH = re.compile(r"(?:^|&)xt=urn:btih:(?:[0-9a-fA-F]{40}|[A-Za-z2-7]{32})(?:&|$)")
_VIDEO_EXT = re.compile(r"\.(mp4|mkv|avi|m4v|webm|mov|mpe?g|ts|wmv)$", re.I)
_SUB_EXT = re.compile(r"\.(srt|ass|ssa|vtt)$", re.I)
_NOT_FILM = re.compile(r"(?:^|[\W_])(sample|trailer|teaser|extras?|featurettes?)(?:[\W_]|$)", re.I)
MAX_SUBS = 12
MAX_SUB_BYTES = 5 * 1048576
TORRENT_STALL = 10 * 60        # بدون أي تقدّم المدة دي: التورنت وقف (مفيش حد بيشاركه)
TORRENT_CHECK = 30             # ثواني بين كل فحص للتوقف
TORRENT_INFO_WAIT = 120        # ثواني لقايمة ملفات التورنت، بعدها يعتبر ميت ويتشال من الطابور
DEAD = "torrent dead"          # أول الرسالة دي = التحكم يشيل التورنت من الطابور بدل ما يجربه تاني
_LANGS = {"ar": "ar", "ara": "ar", "arabic": "ar", "عربي": "ar", "en": "en", "eng": "en", "english": "en",
          "fr": "fr", "fre": "fr", "fra": "fr", "french": "fr", "es": "es", "spa": "es", "spanish": "es",
          "de": "de", "ger": "de", "deu": "de", "german": "de", "tr": "tr", "tur": "tr", "turkish": "tr"}


def check_magnet(link: str) -> str:
    """Only a plain magnet link with a v1 hash reaches the download command."""
    link = (link or "").strip()
    if len(link) > 8000 or not _MAGNET.fullmatch(link) or not _BTIH.search(link[8:]):
        raise RuntimeError("not a valid magnet link")
    return link


def pick_files(files: list) -> tuple:
    """(the film's file, its subtitle files) among a torrent's files: the largest video that is not a
    sample/trailer/extra, mp4/mkv first; subtitles are small text files, a limited number."""
    def ok(f):
        return isinstance(f, dict) and isinstance(f.get("path"), str) and type(f.get("index")) is int \
            and type(f.get("size")) is int and f["size"] > 0
    files = [f for f in files if ok(f)]
    vids = [f for f in files if _VIDEO_EXT.search(f["path"]) and not _NOT_FILM.search(f["path"])]
    if not vids:
        raise RuntimeError("no video file in the torrent")
    best = max(vids, key=lambda f: (f["path"].lower().endswith((".mp4", ".mkv")), f["size"]))
    subs = [f for f in files if _SUB_EXT.search(f["path"]) and f["size"] <= MAX_SUB_BYTES]
    return best, sorted(subs, key=lambda f: f["index"])[:MAX_SUBS]


def sub_language(path: str) -> str:
    """The language a subtitle file names (folder or file name), as a short code, or ''."""
    words = [w for w in re.split(r"[\W_]+", path.lower()) if w]
    for w in reversed(words[:-1]):          # the last word is the extension
        if w in _LANGS:
            return _LANGS[w]
    return ""


def subtitle_names(subs: list, base_name: str) -> list:
    """subs: [(local path, its path inside the torrent)] -> [(local path, library name)]. The name sits
    beside the video, `<video name>.<lang>.<ext>` (Jellyfin reads the language from it); repeated
    names get a number."""
    taken, out = set(), []
    for local, rel in subs:
        ext = os.path.splitext(rel)[1].lower()
        lang = "." + sub_language(rel) if sub_language(rel) else ""
        name, n = f"{base_name}{lang}{ext}", 2
        while name in taken:
            name, n = f"{base_name}{lang}.{n}{ext}", n + 1
        taken.add(name)
        out.append((local, name))
    return out


def _inside(root: str, rel: str):
    """The real path of a torrent's file under the download folder, or None if its name leaves it."""
    full = os.path.realpath(os.path.join(root, rel))
    return full if full.startswith(os.path.realpath(root) + os.sep) and os.path.isfile(full) else None


def _find(root: str, name, rel: str):
    """A torrent's file on disk: MDM saves a many-file torrent inside a folder named after it."""
    return _inside(root, rel) or (_inside(root, os.path.join(name, rel)) if isinstance(name, str) and name else None)


MAX_TORRENT_FILE = 2 * 1048576
_BROWSER_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"}
SESSIONS_FILE = os.path.join(os.environ.get("RUNNER_TEMP") or "/tmp", "miinspact-sessions.json")


def _bdecode(data: bytes):
    """A .torrent's contents (bencode) as Python values, plus where its `info` dictionary sits (that
    part's SHA-1 is the torrent's hash). Strict and bounded: a broken file raises ValueError."""
    pos, span = 0, [None]

    def val(depth):
        nonlocal pos
        if depth > 64 or pos >= len(data):
            raise ValueError("bad torrent")
        c = data[pos:pos + 1]
        if c == b"i":
            end = data.index(b"e", pos)
            num = int(data[pos + 1:end])
            pos = end + 1
            return num
        if c == b"l":
            pos += 1
            items = []
            while data[pos:pos + 1] != b"e":
                items.append(val(depth + 1))
            pos += 1
            return items
        if c == b"d":
            pos += 1
            items = {}
            while data[pos:pos + 1] != b"e":
                key = val(depth + 1)
                if not isinstance(key, bytes):
                    raise ValueError("bad torrent")
                start = pos
                items[key] = val(depth + 1)
                if depth == 0 and key == b"info":
                    span[0] = (start, pos)
            pos += 1
            return items
        if c.isdigit():
            colon = data.index(b":", pos)
            length = int(data[pos:colon])
            if length < 0 or colon + 1 + length > len(data):
                raise ValueError("bad torrent")
            pos = colon + 1 + length
            return data[colon + 1:pos]
        raise ValueError("bad torrent")
    value = val(0)
    if not isinstance(value, dict) or not span[0] or not isinstance(value.get(b"info"), dict):
        raise ValueError("no info in torrent")
    return value, span[0]


def _session_cookie(url: str) -> str:
    """Cookie header for a signed-in site (sessions the owner saved through login.yml)."""
    from urllib.parse import urlsplit
    try:
        with open(SESSIONS_FILE) as f:
            sessions = json.load(f)
    except (OSError, ValueError):
        return ""
    host = (urlsplit(url).hostname or "").lower()
    for s in sessions if isinstance(sessions, list) else []:
        dom = str(s.get("domain") or "")
        if host == dom or host.endswith("." + dom):
            return "; ".join(f"{c['name']}={c['value']}" for c in s.get("cookies", [])
                             if isinstance(c, dict) and host.endswith(str(c.get("domain", "")).lstrip(".")))
    return ""


def torrent_source(item: dict, job_dir: str, link: str) -> str:
    """The .torrent file when the site gave one (it knows the files and the site's own direct sources,
    so no peer is needed to start), checked to be the same torrent as the magnet; else the magnet."""
    import hashlib
    url = item.get("torrent_url") or ""
    if not re.match(r"^https?://[^\s\"'<>\\]+$", url) or len(url) > 2000:
        return link
    try:
        headers = dict(_BROWSER_UA)
        cookie = _session_cookie(url)
        if cookie:
            headers["Cookie"] = cookie
        with requests.get(url, headers=headers, timeout=30, stream=True) as r:
            r.raise_for_status()
            data = b""
            for chunk in r.iter_content(65536):
                data += chunk
                if len(data) > MAX_TORRENT_FILE:
                    raise ValueError("torrent file too big")
        _, (a, b) = _bdecode(data)
        want = re.search(r"btih:([0-9a-fA-F]{40})", link)
        if not want or hashlib.sha1(data[a:b]).hexdigest() != want.group(1).lower():
            raise ValueError("torrent file is not the same torrent")
        path = os.path.join(job_dir, "source.torrent")
        with open(path, "wb") as f:
            f.write(data)
        logging.info("  torrent: using the site's .torrent file")
        return path
    except (requests.RequestException, ValueError) as e:
        logging.info(f"  torrent: no .torrent file ({type(e).__name__}), using the magnet")
        return link


WEBSEED_TRIES = 4              # عناوين مباشرة تتجرّب لكل ملف قبل الرجوع للتورنت العادي
_SHA1_HEX = re.compile(r"[0-9a-fA-F]{40}")
_SAFE_EXT = re.compile(r"\.[A-Za-z0-9]{1,5}")


def web_sources(source: str, link: str) -> dict:
    """The torrent's direct web sources (BEP 19: the .torrent's `url-list`, or the magnet's `ws=`),
    whether it holds a folder of files, and each file's SHA-1 when the .torrent lists it."""
    from urllib.parse import parse_qs
    urls, multi, sha1 = parse_qs(link[8:]).get("ws", []), None, {}
    if os.path.isfile(source):
        try:
            with open(source, "rb") as fh:
                top, _ = _bdecode(fh.read())
        except (OSError, ValueError):
            return {"urls": [], "multi": None, "sha1": {}}
        found = top.get(b"url-list")
        found = [found] if isinstance(found, bytes) else found if isinstance(found, list) else []
        urls = [u.decode("utf-8", "replace") for u in found if isinstance(u, bytes)] + urls
        info = top[b"info"]
        files = info.get(b"files")
        multi = isinstance(files, list)
        name = info.get(b"name", b"").decode("utf-8", "replace")
        entries = [(f.get(b"path"), f.get(b"sha1")) for f in files if isinstance(f, dict)] if multi \
            else [([name.encode()], info.get(b"sha1"))]
        for parts, digest in entries:
            if isinstance(parts, list) and all(isinstance(x, bytes) for x in parts) and \
                    isinstance(digest, bytes) and _SHA1_HEX.fullmatch(digest.decode("ascii", "replace")):
                sha1["/".join(x.decode("utf-8", "replace") for x in parts)] = digest.decode().lower()
    return {"urls": list(dict.fromkeys(u for u in urls if isinstance(u, str)))[:20], "multi": multi, "sha1": sha1}


def webseed_urls(seeds: list, name: str, rel: str, multi) -> list:
    """Where each web source keeps one file of the torrent (BEP 19): a folder torrent's file sits at
    <source>/<torrent name>/<path>; a one-file torrent's at <source><name> or at the source itself."""
    from urllib.parse import quote
    out = []
    for s in seeds:
        if multi:
            out.append(s.rstrip("/") + "/" + quote(name, safe="") + "/" + quote(rel, safe="/"))
        else:
            out.append(s + quote(name, safe="") if s.endswith("/") else s)
    return list(dict.fromkeys(out))


def _public_web(url: str) -> bool:
    """A plain http(s) address whose host is on the public internet only (no local or private network)."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit
    if not isinstance(url, str) or len(url) > 2000 or re.search(r"[\s\"'<>\\]", url):
        return False
    try:
        u = urlsplit(url)
        port = u.port
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
            return False
        addrs = {a[4][0] for a in socket.getaddrinfo(u.hostname, port or 443, proto=socket.IPPROTO_TCP)}
        return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)
    except (OSError, UnicodeError, ValueError):
        return False


def _probe(url: str, size: int) -> str:
    """The final address of a working copy of the file (answers, same size), else ''. Every redirect
    is checked to stay on the public internet before it is followed."""
    for _ in range(6):
        if not _public_web(url):
            return ""
        try:
            with requests.get(url, headers=_BROWSER_UA, timeout=20, stream=True, allow_redirects=False) as r:
                if r.is_redirect:
                    from urllib.parse import urljoin
                    url = urljoin(url, r.headers.get("Location", ""))
                    continue
                length = r.headers.get("Content-Length")
                ok = r.status_code == 200 and (not length or int(length) == size)
                return url if ok else ""
        except (requests.RequestException, ValueError):
            return ""
    return ""


def _run_mdm(cmd: list, env: dict, progress: Progress, deadline: float, total: int = 0) -> tuple:
    """Runs one MDM download command and reads its JSON lines into the progress bar. It is stopped
    when nothing moves for TORRENT_STALL or the run's time is up. Returns (end message, bytes seen,
    most peers, why it was stopped or '')."""
    proc = _start_group(cmd, env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    progress.procs.append(proc)
    state = {"moved": time.time(), "why": ""}
    finished = threading.Event()

    def watchdog():
        while not finished.wait(TORRENT_CHECK):
            now = time.time()
            if now - state["moved"] > TORRENT_STALL or now > deadline:
                state["why"] = "stalled" if now <= deadline else "time"
                _stop_group(proc)
                return
    threading.Thread(target=watchdog, daemon=True).start()
    seen, peers, end = -1, 0, {}
    try:
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("event") == "progress":
                got, size = msg.get("downloaded") or 0, msg.get("size") or total
                if got > seen:
                    seen, state["moved"] = got, time.time()
                peers = max(peers, msg.get("peers") or 0)
                progress.dl_pct = min(100, int(got * 100 / size)) if size else 0
                progress.speed = (msg.get("speed") or 0) / 1048576
                progress.size_mb = size / 1048576
                progress.push()
            elif msg.get("event") in ("done", "paused", "stopped") or "error" in msg:
                end = msg
        proc.wait()
    finally:
        finished.set()
        _stop_group(proc)
        progress.procs.remove(proc)
        proc.stdout.close()
    progress.check()
    return end, seen, peers, state["why"]


def _why(end: dict) -> str:
    return _URL.sub("<address>", str(end.get("error") or end.get("event") or "stopped"))[:150]


def fetch_direct(files: list, web: dict, name: str, out_dir: str, env: dict, progress: Progress, deadline: float) -> dict:
    """Downloads the picked files from the torrent's direct web sources with MDM's ordinary download
    (no peer needed), checked by size and by SHA-1 when the .torrent gives it.
    Returns {path inside the torrent: local file}; a file no source gives is left out."""
    got = {}
    folder = os.path.join(out_dir, "direct")
    os.makedirs(folder, exist_ok=True)
    for f in files:
        ext = os.path.splitext(f["path"])[1]
        dst = os.path.join(folder, f"{f['index']}{ext.lower() if _SAFE_EXT.fullmatch(ext) else ''}")
        for url in webseed_urls(web["urls"], name, f["path"], web["multi"])[:WEBSEED_TRIES]:
            final = _probe(url, f["size"])
            if not final:
                continue
            # The address was checked public just now; MDM looks the name up again itself (a host that
            # changes its answer in between could point elsewhere: the runner holds no private network).
            cmd = [MDM_CLI, "--json", "get", final, "-o", dst, "--direct"]
            if web["sha1"].get(f["path"]):
                cmd += ["--checksum", "sha1:" + web["sha1"][f["path"]]]
            end, _, _, why = _run_mdm(cmd, env, progress, deadline, f["size"])
            if end.get("event") == "done" and os.path.isfile(dst) and os.path.getsize(dst) == f["size"]:
                got[f["path"]] = dst
                break
            logging.info(f"  torrent: a direct source failed ({why or _why(end)})")
            for left in (dst, dst + ".mdm.part", dst + ".mdm.json"):     # the next source starts clean
                if os.path.isfile(left):
                    os.remove(left)
            if why == "time":
                return got
    return got


def download_torrent(item: dict, out_base: str, progress: Progress) -> tuple:
    """تحميل تورنت ببرنامج Mister Download Manager: الفيلم + ملفات الترجمة بس، ومن غير مشاركة بعد
    الانتهاء. لو التورنت معاه مصادر مباشرة على الويب، الملف بيتحمّل منها الأول بالتحميل العادي،
    ولو منفعتش يرجع لمحرك التورنت. بيرجّع (مسار الفيديو، [(مسار ترجمة، اسمها في المكتبة)])."""
    link = check_magnet(item.get("watch_url"))
    job_dir = os.path.dirname(out_base)
    out_dir = os.path.join(job_dir, "torrent")
    os.makedirs(out_dir, exist_ok=True)
    env = {**os.environ, "MDM_DATA_DIR": os.path.join(job_dir, "mdm-data")}
    source = torrent_source(item, job_dir, link)
    info = _start_group([MDM_CLI, "--json", "torrent-info", source], env, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, text=True)
    progress.procs.append(info)
    try:
        out, _ = info.communicate(timeout=TORRENT_INFO_WAIT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{DEAD}: no one shares its file list") from None
    finally:
        _stop_group(info)
        progress.procs.remove(info)
        info.stdout.close()
    progress.check()
    try:
        meta = json.loads((out.strip().splitlines() or [""])[-1])
    except ValueError:
        meta = {}
    if not isinstance(meta, dict) or not isinstance(meta.get("files"), list):
        why = str(meta.get("error") or "no answer") if isinstance(meta, dict) else "bad answer"
        raise RuntimeError(f"{DEAD}: no file list ({_URL.sub('<address>', why)[:150]})")
    best, subs = pick_files(meta["files"])
    name = meta.get("name") if isinstance(meta.get("name"), str) else ""
    logging.info(f"  torrent: {len(meta['files'])} file(s), film {best['size'] / 1048576:.0f} MB + {len(subs)} subtitle(s)")
    budget = int(MAX_SECONDS - (time.time() - START) - 15 * 60)   # يفضل وقت للتحقق والرفع
    if budget < 10 * 60:
        raise RuntimeError("no time left for this torrent in this run")
    deadline = time.time() + budget
    progress.stage = "item_download"
    hold_disk(int(item["id"]), best["size"] * 2 + sum(f["size"] for f in subs))   # the film + its moved copy
    web = web_sources(source, link)
    if web["multi"] is None:     # magnet only: one file named like the torrent = a one-file torrent
        web["multi"] = not (len(meta["files"]) == 1 and best["path"] == name)
    direct = fetch_direct([best, *subs], web, name, out_dir, env, progress, deadline) if web["urls"] and name else {}
    if best["path"] in direct:
        logging.info("  torrent: the film came from its direct web source")
        src = direct[best["path"]]
        found = [(direct[f["path"]], f["path"]) for f in subs if f["path"] in direct]
    else:
        wanted = ",".join(str(f["index"]) for f in [best, *subs])
        left = int(deadline - time.time())
        if left < 5 * 60:
            raise RuntimeError("no time left for this torrent in this run")
        end, seen, peers, why = _run_mdm([MDM_CLI, "--json", "torrent-get", source, "-o", out_dir, "--files", wanted,
                                          "--seconds", str(left)], env, progress, deadline + 60)
        if end.get("event") != "done":
            if why and seen <= 0:      # not one byte from anybody: dead from the start
                raise RuntimeError(f"{DEAD}: nobody sent any part of it | peers max {peers}")
            reason = "no one is sharing it (stalled)" if why else _why(end)
            raise RuntimeError(f"torrent: {reason} | peers max {peers}")
        src = _find(out_dir, name, best["path"])
        if not src:
            raise RuntimeError("torrent: the film's file is missing after the download")
        found = [(p, f["path"]) for f in subs for p in [_find(out_dir, name, f["path"])] if p]
    progress.size_mb = os.path.getsize(src) / 1048576
    dst = out_base + os.path.splitext(src)[1].lower()
    shutil.move(src, dst)
    found = [(p, rel) for p, rel in found if 0 < os.path.getsize(p) <= MAX_SUB_BYTES]
    return dst, subtitle_names(found, os.path.basename(out_base))


def video_seconds(path: str) -> float:
    """The longest duration ffprobe reads for the file or any of its streams; 0 when none is written
    (streamed copies often carry no duration: "N/A")."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=duration", "-of", "json", path],
                         capture_output=True, text=True, timeout=120)
    try:
        info = json.loads(out.stdout or "{}")
    except ValueError:
        return 0.0
    found = [info.get("format", {}).get("duration")] + [st.get("duration") for st in info.get("streams", [])]
    secs = []
    for d in found:
        try:
            secs.append(float(d))
        except (TypeError, ValueError):
            pass
    return max(secs, default=0.0)


PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _png_end(data, at: int):
    """Where the PNG image starting at `at` ends (after its IEND chunk), or None if it isn't one."""
    q = at + 8
    while q + 12 <= len(data):
        size = int.from_bytes(data[q:q + 4], "big")
        kind = bytes(data[q + 4:q + 8])
        if size > 1 << 24 or not kind.isalpha():
            return None
        q += 12 + size
        if kind == b"IEND":
            return q if q <= len(data) else None
    return None


def unwrap_image_segments(path: str):
    """General rule for any server: some hide every piece of a streamed video behind a small fake
    PNG picture (the piece's real MPEG-TS data follows the picture), so the joined download reads
    as a few seconds of a 1x1 picture. The pictures are cut out and the pieces' own data kept, as a
    .ts file next to it (returned; the download is removed). None when the file isn't built so."""
    import mmap
    with open(path, "rb") as f:
        if f.read(8) != PNG_SIG:
            return None
        f.seek(0)
        data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            first = _png_end(data, 0)
            if first is None or first >= len(data) or data[first] != 0x47:
                return None
            out_path = os.path.splitext(path)[0] + ".unwrapped.ts"
            pictures = 0
            with open(out_path, "wb") as out:
                p, n = 0, len(data)
                while p < n:
                    if data[p:p + 8] == PNG_SIG:
                        end = _png_end(data, p)
                        if end is not None:
                            pictures += 1
                            p = end
                            continue
                    nxt = data.find(PNG_SIG, p + 1)
                    nxt = n if nxt == -1 else nxt
                    out.write(data[p:nxt])
                    p = nxt
        finally:
            data.close()
    os.remove(path)
    logging.info(f"  {pictures} fake pictures cut out of the video's pieces")
    return out_path


def verify_video(path: str) -> str:
    """Checks the download is a real video and returns its path. Pieces hidden behind fake pictures
    are unwrapped first. A copy whose header holds no duration (or an unwrapped stream) is rewritten
    (streams copied, nothing re-encoded) into a Matroska file, so the library can show its length
    and seek in it."""
    if os.path.getsize(path) < MIN_VIDEO_BYTES:
        raise RuntimeError("file too small")
    unwrapped = unwrap_image_segments(path)
    path = unwrapped or path
    secs = 0.0 if unwrapped else video_seconds(path)
    if not secs:
        fixed = os.path.splitext(path)[0] + ".fixed.mkv"
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-map", "0:v", "-map", "0:a?", "-map", "0:s?", "-c", "copy", fixed],
                           capture_output=True, text=True, timeout=1800)
        secs = video_seconds(fixed) if r.returncode == 0 and os.path.exists(fixed) else 0.0
        if not secs:
            if os.path.exists(fixed):
                os.remove(fixed)
            raise RuntimeError("not a playable video (no duration)")
        os.remove(path)
        path = re.sub(r"(\.unwrapped)?\.[^./]+$", "", path) + ".mkv"
        os.replace(fixed, path)
        logging.info(f"  rewritten with its duration ({secs / 60:.0f} min)")
    if secs < MIN_SECONDS:
        raise RuntimeError("duration too short")
    return make_faststart(path, secs)


def moov_after_mdat(path: str) -> bool:
    """True when an MP4/MOV keeps its index (`moov`) after the media data (`mdat`): a player then has
    to fetch the end of the file before it can start, which is slow from a cloud drive."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size

            def read(pos, n):
                f.seek(pos)
                return f.read(n)
            return index_after_media(read, size)
    except OSError:
        return False


def index_after_media(read, size: int) -> bool:
    """Walks the top-level boxes with read(position, count) -> bytes (a local file or a remote range)."""
    seen_mdat, pos = False, 0
    for _ in range(64):
        if pos + 8 > size:
            break
        head = read(pos, 16)
        if len(head) < 8:
            break
        box_len, kind = int.from_bytes(head[:4], "big"), head[4:8]
        if box_len == 1:
            if len(head) < 16:
                break
            box_len = int.from_bytes(head[8:16], "big")
        elif box_len == 0:
            box_len = size - pos
        if box_len < 8:
            break
        if kind == b"moov":
            return seen_mdat
        if kind == b"mdat":
            seen_mdat = True
        pos += box_len
    return False


def make_faststart(path: str, secs: float) -> str:
    """Moves the MP4 index to the front (streams copied, nothing re-encoded), so playback starts at
    once. Any failure keeps the original file untouched."""
    if os.path.splitext(path)[1].lower() not in (".mp4", ".m4v", ".mov") or not moov_after_mdat(path):
        return path
    tmp = path + ".fast" + os.path.splitext(path)[1]
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-map", "0", "-c", "copy", "-movflags", "+faststart", tmp],
                           capture_output=True, text=True, timeout=1800)
        new_secs = video_seconds(tmp) if r.returncode == 0 and os.path.exists(tmp) else 0.0
        if new_secs and abs(new_secs - secs) <= max(2.0, secs * 0.01) and not moov_after_mdat(tmp):
            os.replace(tmp, path)
            logging.info("  index moved to the front (fast start)")
            return path
    except (OSError, subprocess.SubprocessError):
        pass
    if os.path.exists(tmp):
        os.remove(tmp)
    logging.warning("  fast start skipped, original kept")
    return path


def remote_exists(remote_path: str) -> bool:
    r = subprocess.run(["rclone", "lsf", f"{REMOTE}:{remote_path}"], capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


POSTER_MARK = "poster.text"   # next to a written poster (no picture was found): a real picture replaces it later


def put_poster(local: str, folder: str, kind: str) -> None:
    """The work's poster goes up once. A written one leaves a mark, so a later episode or copy that has a
    real picture replaces it; a real picture is never replaced."""
    poster, mark = f"{folder}/poster.jpg", f"{folder}/{POSTER_MARK}"
    there = remote_exists(poster)
    if there and not (kind == "real" and remote_exists(mark)):
        return
    subprocess.run(["rclone", "copyto", local, f"{REMOTE}:{poster}"], check=True, capture_output=True)
    if kind == "text":
        subprocess.run(["rclone", "touch", f"{REMOTE}:{mark}"], capture_output=True)
    elif there:
        subprocess.run(["rclone", "deletefile", f"{REMOTE}:{mark}"], capture_output=True)


def upload(local: str, remote_path: str, progress: Progress) -> float:
    """رفع بملف واحد مع قراءة نسبة التقدم من إحصائيات rclone (JSON)"""
    started = time.time()
    proc = subprocess.Popen(["rclone", "copyto", local, f"{REMOTE}:{remote_path}", "--stats", "5s",
                             "--use-json-log", "--log-level", "INFO", "--drive-chunk-size", "64M"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
    progress.procs.append(proc)
    for line in proc.stderr:
        try:
            stats = json.loads(line).get("stats") or {}
        except ValueError:
            continue
        total = stats.get("totalBytes") or 0
        if total:
            progress.push(ul_pct=int(stats.get("bytes", 0) * 100 / total))
    code = proc.wait()
    progress.procs.remove(proc)
    progress.check()
    if code != 0:
        raise RuntimeError("upload failed")
    return os.path.getsize(local) / 1048576 / max(1.0, time.time() - started)


ART_WAIT = 120          # seconds the upload waits, at most, for a poster still being looked up


def prepare_art(item: dict, job_dir: str, local_poster: str, progress: Progress):
    """The poster and the work's details, side by side with the download (the dashboard shows them as they come)."""
    title, year = item.get("clean_title", ""), item.get("year")
    try:
        has_poster = process_poster(poster_source(item, job_dir), local_poster, title=title, year=year)
        if not has_poster and os.path.exists(local_poster):
            os.remove(local_poster)
        progress.poster = has_poster or ""
        details = {**work_details(title, year), "poster_thumb": poster_thumb(local_poster) if has_poster else ""}
        with progress.lock:
            if progress.done:
                return
            report(progress.item_id, "downloading", stage=progress.stage, dl_pct=progress.dl_pct,
                   speed_mbs=round(progress.speed, 2), size_mb=round(progress.size_mb, 1), details=details)
    except Exception as e:      # no poster or details: the video still goes up
        logging.warning(f"item {progress.item_id}: no details ({type(e).__name__})")


def process(item: dict):
    item_id = int(item["id"])
    job_dir = os.path.join(WORK_DIR, str(item_id))
    os.makedirs(job_dir, exist_ok=True)
    progress = Progress(item_id)
    try:
        folder, base_name, poster_folder = destination(item)
        progress.push(force=True)
        # The poster and the work's details are looked up while the video downloads: the download starts at once.
        local_poster = os.path.join(job_dir, "poster.jpg")
        art = threading.Thread(target=prepare_art, args=(item, job_dir, local_poster, progress), daemon=True)
        art.start()
        progress.stage = "item_download"
        progress.push(force=True)
        fetch = {"torrent": download_torrent, "telegram": download_telegram}.get(item.get("kind"), download)
        video, subtitles = fetch(item, os.path.join(job_dir, base_name), progress)
        art.join(ART_WAIT)
        has_poster = not art.is_alive() and os.path.isfile(local_poster) and os.path.getsize(local_poster) > 0 \
            and progress.poster
        progress.stage = "item_verify"
        video = verify_video(video)
        progress.dl_pct = 100
        progress.size_mb = os.path.getsize(video) / 1048576
        progress.push(force=True, ul_pct=0)

        # البوستر: للمسلسل في مجلد المسلسل نفسه (مرة واحدة)، وللفيلم/المسرحية في مجلده
        if has_poster:
            put_poster(local_poster, poster_folder, progress.poster)

        progress.stage = "item_upload"
        progress.push(force=True)
        ext = os.path.splitext(video)[1]
        remote_video = f"{folder}/{base_name}{ext}"
        speed = upload(video, remote_video, progress)
        for local, name in subtitles:     # الترجمة جنب الفيديو، والفشل فيها مايوقعش المادة
            r = subprocess.run(["rclone", "copyto", local, f"{REMOTE}:{folder}/{name}"], capture_output=True)
            if r.returncode != 0:
                logging.warning(f"item {item_id}: a subtitle file was not uploaded")
        progress.finish()
        report(item_id, "uploaded", stage=progress.stage, dl_pct=100, ul_pct=100, size_mb=round(progress.size_mb, 1),
               speed_mbs=round(speed, 2), gdrive_path=remote_video)
        logging.info(f"item {item_id}: uploaded ({progress.size_mb:.0f} MB)")
    except Stopped:
        progress.finish()
        logging.info(f"item {item_id}: stopped by the owner, its files are removed")
        if item.get("kind") in (None, "page"):
            stop_leftover_mdm()
    except Exception as e:
        progress.finish()
        if progress.stopped:      # a command cut by "stop" fails on its own: not a real failure
            logging.info(f"item {item_id}: stopped by the owner, its files are removed")
            return
        logging.warning(f"item {item_id}: failed ({type(e).__name__})")
        report(item_id, "failed", stage=progress.stage, error=f"{type(e).__name__}: {str(e)[:300]}")
    finally:
        free_disk(item_id)
        shutil.rmtree(job_dir, ignore_errors=True)


# ---------- telegram (the account's own session, no browser) ----------

def poster_source(item: dict, job_dir: str) -> str:
    """The poster address; a Telegram post picture (`tg:<channel>/<message>`) is saved first at its full size"""
    source = item.get("poster_url") or ""
    if not source.startswith("tg:"):
        return source
    path = os.path.join(job_dir, "post_picture.jpg")
    try:
        return path if telegram.client().picture(source, path) else ""
    except Exception as e:      # no picture: the poster search by title is next
        logging.info(f"telegram post picture: {type(e).__name__}")
        return ""


def download_telegram(item: dict, out_base: str, progress: Progress) -> tuple:
    """فيديو واحد من رسالة في قناة تليجرام، بحساب المالك اللي في خزنة جيت هب"""
    tg = telegram.client()
    size = tg.size(item.get("watch_url"))
    progress.size_mb = size / 1048576
    hold_disk(int(item["id"]), size * 2)          # the video + its checked copy
    started = time.time()

    def on_progress(done, total):
        progress.dl_pct = int(done * 100 / total) if total else 0
        progress.speed = done / max(1.0, time.time() - started) / 1048576
        progress.push()
    left = max(60.0, MAX_SECONDS - STOP_MARGIN - (time.time() - START))
    return tg.download(item.get("watch_url"), out_base, on_progress, timeout=left), []


def time_left() -> bool:
    return time.time() - START < MAX_SECONDS - STOP_MARGIN


def next_items(kind: str, n: int, tag: str) -> list:
    return api("miinspact-next-batch", {"n": n, "batch_id": f"{RUN_ID}-{tag}", "kinds": kind}).get("items") or []


def page_lane(count: dict, torrents_busy: threading.Event):
    """Watch pages one after another (one browser + one MDM window at a time)."""
    while time_left():
        items = next_items("page", BATCH_SIZE, f"p{count['pages']}")
        if not items:
            if not torrents_busy.is_set():
                return
            time.sleep(EMPTY_RECHECK)        # torrents still running: pages added meanwhile are picked up
            continue
        for item in items:
            process(item)
            count["pages"] += 1


def rolling_lane(kind: str, slots: int, tag: str, count: dict, keep_waiting=lambda: False):
    """A rolling batch of `slots` items: the moment one ends (done, failed or dead), its slot takes the
    next one, so the batch never waits for its slowest item. keep_waiting(): ask again later when empty."""
    active, ended = {}, threading.Event()
    asked_empty_at = 0.0
    key = {"torrent": "torrents"}.get(kind, kind)

    def one(item):
        try:
            process(item)
        finally:
            count[key] += 1
            ended.set()
    while True:
        for k in [k for k, t in active.items() if not t.is_alive()]:
            del active[k]
        free = slots - len(active)
        if free and time_left() and time.time() - asked_empty_at >= EMPTY_RECHECK:
            items = next_items(kind, free, f"{tag}{count[key]}-{len(active)}")
            asked_empty_at = 0.0 if items else time.time()
            for item in items:
                t = threading.Thread(target=one, args=(item,), daemon=True)
                active[item["id"]] = t
                t.start()
        if not active and ((asked_empty_at and not keep_waiting()) or not time_left()):
            return
        ended.wait(EMPTY_RECHECK)
        ended.clear()


def torrent_lane(count: dict, busy: threading.Event):
    try:
        rolling_lane("torrent", TORRENT_SLOTS, "t", count)
    finally:
        busy.clear()


def telegram_lane(count: dict, others_busy: threading.Event):
    """Telegram videos, as many at once as the account's kind allows (Premium more than a free one)."""
    try:
        rolling_lane("telegram", telegram.client().slots(), "g", count, others_busy.is_set)
    except Exception as e:      # the account could not be reached: its items wait for the next run
        logging.info(f"telegram lane: {type(e).__name__}")
    finally:
        telegram.close()


def restore_sessions():
    """Sign-ins come back on every new runner: Chrome (grab.mjs) and .torrent downloads use them."""
    if hook.save_sessions(api, SESSIONS_FILE):
        os.environ["MDM_SESSIONS"] = SESSIONS_FILE


def main():
    restore_sessions()
    stop = threading.Event()
    threading.Thread(target=heartbeat_loop, args=(stop,), daemon=True).start()
    set_stage("working")
    count = {"pages": 0, "torrents": 0, "telegram": 0}
    busy = threading.Event()
    busy.set()
    lane = threading.Thread(target=torrent_lane, args=(count, busy), daemon=True)
    lane.start()
    tg = threading.Thread(target=telegram_lane, args=(count, busy), daemon=True) if telegram.configured() else None
    if tg:
        tg.start()
    try:
        if not TORRENTS_ONLY:
            page_lane(count, busy)
        lane.join()
        if tg:
            tg.join()
    finally:
        stop.set()
        set_stage("done")
    logging.info(f"processed {count['pages']} page item(s) + {count['torrents']} torrent(s) + {count['telegram']} telegram "
                 f"in {(time.time() - START) / 60:.1f} min")


if __name__ == "__main__":
    main()
