"""The light runner for site work (sites.yml): torrent-site scans, maps and life checks in real Chrome
with the two ad blockers, on its own machine beside the download runner. No download tools are set up
here, so it is ready in about a minute. It waits a few minutes for new jobs before it ends."""
import logging
import os

from sites import hook, lane

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

API_URL = os.environ["RUNNER_API_URL"].rstrip("/")
API_TOKEN = os.environ["RUNNER_API_TOKEN"]
RUN_ID = os.environ.get("RUN_ID", "local")
SESSIONS_FILE = os.path.join(os.environ.get("RUNNER_TEMP") or "/tmp", "miinspact-sessions.json")


def api(path: str, payload: dict) -> dict:
    return hook.call(API_URL, API_TOKEN, path, payload, timeout=150)


def main():
    hook.save_sessions(api, SESSIONS_FILE)
    n = lane.run(api, SESSIONS_FILE, run_id=f"sites-{RUN_ID}")
    logging.info(f"{n} site job(s) done")


if __name__ == "__main__":
    main()
