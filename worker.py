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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
for noisy in ("urllib3", "requests", "PIL"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

API_URL = os.environ["RUNNER_API_URL"].rstrip("/")
API_TOKEN = os.environ["RUNNER_API_TOKEN"]
REMOTE = os.environ["RCLONE_REMOTE"].rstrip(":")
MEDIA_BASE = os.environ["MEDIA_BASE_PATH"].strip("/")
RUN_ID = os.environ.get("RUN_ID", "local")
WORK_DIR = os.path.abspath("work")

START = time.time()
MAX_SECONDS = 350 * 60
STOP_MARGIN = 25 * 60          # لا نبدأ مادة جديدة إذا تبقى أقل من 25 دقيقة
REPORT_INTERVAL = 10           # ثوانٍ بين تقارير التقدم
BATCH_SIZE = 3
MIN_VIDEO_BYTES = 5 * 1024 * 1024


def api(path: str, payload: dict) -> dict:
    for attempt in range(3):
        try:
            r = requests.post(f"{API_URL}/{path}", json=payload, timeout=30,
                              headers={"X-Runner-Token": API_TOKEN})
            if r.status_code < 500:
                return r.json() if r.content else {}
        except (requests.RequestException, ValueError):
            pass
        time.sleep(5 * (attempt + 1))
    return {}


def report(item_id: int, status: str, **extra):
    api("miinspact-report", {"item_id": item_id, "status": status, "run_id": RUN_ID,
                             "run_started_at": START, **extra})


def heartbeat_loop(stop: threading.Event):
    while not stop.wait(60):
        api("miinspact-report", {"heartbeat_only": True, "run_id": RUN_ID, "run_started_at": START})


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', " ", name or "Unknown")
    return re.sub(r"\s+", " ", name).strip()[:150] or "Unknown"


def destination(item: dict) -> tuple:
    """(مجلد الفيديو، اسم الملف، مجلد البوستر) بنفس تسمية مكتبات Jellyfin"""
    title = safe_name(item.get("clean_title"))
    year = f" ({item['year']})" if item.get("year") else ""
    arabic = bool(item.get("is_arabic"))
    ctype = item.get("content_type", "movie")
    if ctype == "series":
        season = int(item.get("season") or 1)
        episode = int(item.get("episode") or 1)
        root = f"{MEDIA_BASE}/Series/{'Arabic_Series' if arabic else 'Foreign_Series'}/{title}"
        return f"{root}/Season {season:02d}", f"{title} - S{season:02d}E{episode:02d}", root
    if ctype == "play":
        folder = f"{MEDIA_BASE}/Plays/{title}{year}"
        return folder, f"{title}{year}", folder
    folder = f"{MEDIA_BASE}/Movies/{'Arabic_Movies' if arabic else 'Foreign_Movies'}/{title}{year}"
    return folder, f"{title}{year}", folder


class Progress:
    def __init__(self, item_id: int):
        self.item_id = item_id
        self.last = 0.0
        self.dl_pct = 0
        self.speed = 0.0
        self.size_mb = 0.0

    def push(self, force: bool = False, **extra):
        if force or time.time() - self.last >= REPORT_INTERVAL:
            self.last = time.time()
            report(self.item_id, "downloading", dl_pct=self.dl_pct, speed_mbs=round(self.speed, 2),
                   size_mb=round(self.size_mb, 1), **extra)

    def ytdlp_hook(self, d: dict):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                self.dl_pct = int(d.get("downloaded_bytes", 0) * 100 / total)
                self.size_mb = total / 1048576
            self.speed = (d.get("speed") or 0) / 1048576
            self.push()


def download(item: dict, out_base: str, progress: Progress) -> str:
    import yt_dlp
    opts = {
        "outtmpl": out_base + ".%(ext)s",
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 5,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        "progress_hooks": [progress.ytdlp_hook],
        "logger": logging.getLogger("ytdlp-silent"),
    }
    logging.getLogger("ytdlp-silent").setLevel(logging.CRITICAL)
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([item["watch_url"]])
    for f in os.listdir(os.path.dirname(out_base)):
        path = os.path.join(os.path.dirname(out_base), f)
        if f.startswith(os.path.basename(out_base)) and not f.endswith((".part", ".ytdl", ".jpg")):
            return path
    raise RuntimeError("no output file")


def verify_video(path: str):
    if os.path.getsize(path) < MIN_VIDEO_BYTES:
        raise RuntimeError("file too small")
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True, timeout=60)
    if float(out.stdout.strip() or 0) < 60:
        raise RuntimeError("duration < 60s")


def remote_exists(remote_path: str) -> bool:
    r = subprocess.run(["rclone", "lsf", f"{REMOTE}:{remote_path}"], capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def upload(local: str, remote_path: str, progress: Progress) -> float:
    """رفع بملف واحد مع قراءة نسبة التقدم من إحصائيات rclone (JSON)"""
    started = time.time()
    proc = subprocess.Popen(["rclone", "copyto", local, f"{REMOTE}:{remote_path}", "--stats", "5s",
                             "--use-json-log", "--log-level", "INFO", "--drive-chunk-size", "64M"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    for line in proc.stderr:
        try:
            stats = json.loads(line).get("stats") or {}
        except ValueError:
            continue
        total = stats.get("totalBytes") or 0
        if total:
            progress.push(ul_pct=int(stats.get("bytes", 0) * 100 / total))
    if proc.wait() != 0:
        raise RuntimeError("upload failed")
    return os.path.getsize(local) / 1048576 / max(1.0, time.time() - started)


def process(item: dict):
    item_id = int(item["id"])
    job_dir = os.path.join(WORK_DIR, str(item_id))
    os.makedirs(job_dir, exist_ok=True)
    progress = Progress(item_id)
    try:
        folder, base_name, poster_folder = destination(item)
        progress.push(force=True)
        video = download(item, os.path.join(job_dir, base_name), progress)
        verify_video(video)
        progress.dl_pct = 100
        progress.size_mb = os.path.getsize(video) / 1048576
        progress.push(force=True, ul_pct=0)

        # البوستر: للمسلسل في مجلد المسلسل نفسه (مرة واحدة)، وللفيلم/المسرحية في مجلده
        poster_remote = f"{poster_folder}/poster.jpg"
        if not remote_exists(poster_remote):
            from poster import process_poster
            local_poster = os.path.join(job_dir, "poster.jpg")
            if process_poster(item.get("poster_url") or "", local_poster, title=item.get("clean_title", ""),
                              year=item.get("year")):
                subprocess.run(["rclone", "copyto", local_poster, f"{REMOTE}:{poster_remote}"],
                               check=True, capture_output=True)

        ext = os.path.splitext(video)[1]
        remote_video = f"{folder}/{base_name}{ext}"
        speed = upload(video, remote_video, progress)
        report(item_id, "uploaded", dl_pct=100, ul_pct=100, size_mb=round(progress.size_mb, 1),
               speed_mbs=round(speed, 2), gdrive_path=remote_video)
        logging.info(f"item {item_id}: uploaded ({progress.size_mb:.0f} MB)")
    except Exception as e:
        logging.warning(f"item {item_id}: failed ({type(e).__name__})")
        report(item_id, "failed", error=f"{type(e).__name__}: {str(e)[:300]}")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


def main():
    stop = threading.Event()
    threading.Thread(target=heartbeat_loop, args=(stop,), daemon=True).start()
    api("miinspact-report", {"heartbeat_only": True, "run_id": RUN_ID, "run_started_at": START})
    done = 0
    try:
        while time.time() - START < MAX_SECONDS - STOP_MARGIN:
            batch = api("miinspact-next-batch", {"n": BATCH_SIZE, "batch_id": f"{RUN_ID}-{done}"})
            items = batch.get("items") or []
            if not items:
                logging.info("queue empty")
                break
            for item in items:
                process(item)
                done += 1
    finally:
        stop.set()
    logging.info(f"processed {done} item(s) in {(time.time() - START) / 60:.1f} min")


if __name__ == "__main__":
    main()
