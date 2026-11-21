"""One-off library repair: every MP4/M4V/MOV already in the library whose index (`moov`) sits after
its media data is fetched, rewritten with the index at the front (streams copied, nothing
re-encoded) and put back under the same name, so the media server starts playing it at once.

Only the first bytes of each box are read remotely to decide; files already fine are never
downloaded. The log names no file (the repository is public): only counters.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import time

os.environ.setdefault("RUNNER_API_URL", "http://unused.invalid")     # the queue is never contacted here
os.environ.setdefault("RUNNER_API_TOKEN", "unused")
import worker  # noqa: E402

EXTS = (".mp4", ".m4v", ".mov")
DEADLINE = time.time() + float(os.environ.get("MAX_MINUTES", "300")) * 60
WORK = os.path.join(worker.WORK_DIR, "library")


def remote_files(base: str) -> list:
    r = subprocess.run(["rclone", "lsjson", "-R", "--files-only", f"{worker.REMOTE}:{base}"],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError("library listing failed")
    rows = json.loads(r.stdout or "[]")
    return [(f"{base}/{x['Path']}", int(x.get("Size") or 0)) for x in rows
            if isinstance(x, dict) and str(x.get("Path", "")).lower().endswith(EXTS) and int(x.get("Size") or 0) > 0]


def remote_read(path: str):
    def read(pos: int, n: int) -> bytes:
        r = subprocess.run(["rclone", "cat", "--offset", str(pos), "--count", str(n), f"{worker.REMOTE}:{path}"],
                           capture_output=True, timeout=120)
        return r.stdout if r.returncode == 0 else b""
    return read


def fix_one(path: str, size: int) -> str:
    """'fixed', 'fine', 'skipped' (no room / no time) or 'failed'. The remote copy is replaced only by a
    checked file of the same length in time."""
    if not worker.index_after_media(remote_read(path), size):
        return "fine"
    os.makedirs(worker.WORK_DIR, exist_ok=True)
    if time.time() > DEADLINE or shutil.disk_usage(worker.WORK_DIR).free < size * 2.2:
        return "skipped"
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    local = os.path.join(WORK, "item" + os.path.splitext(path)[1].lower())
    try:
        r = subprocess.run(["rclone", "copyto", f"{worker.REMOTE}:{path}", local], capture_output=True, timeout=3600)
        secs = worker.video_seconds(local) if r.returncode == 0 and os.path.exists(local) else 0.0
        if not secs or os.path.getsize(local) != size:
            return "failed"
        worker.make_faststart(local, secs)
        if worker.moov_after_mdat(local) or not worker.video_seconds(local):
            return "failed"
        r = subprocess.run(["rclone", "copyto", local, f"{worker.REMOTE}:{path}"], capture_output=True, timeout=3600)
        return "fixed" if r.returncode == 0 else "failed"
    except (OSError, subprocess.SubprocessError):
        return "failed"
    finally:
        shutil.rmtree(WORK, ignore_errors=True)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    files = remote_files(worker.MEDIA_BASE)
    logging.info(f"{len(files)} MP4 files in the library")
    counts = {"fixed": 0, "fine": 0, "skipped": 0, "failed": 0}
    for n, (path, size) in enumerate(files, 1):
        state = fix_one(path, size)
        counts[state] += 1
        if state != "fine":
            logging.info(f"  file {n}/{len(files)} ({size / 1e9:.1f} GB): {state}")
    logging.info("done: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
