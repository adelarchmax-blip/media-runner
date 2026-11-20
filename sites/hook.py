"""Talking to the controller through the n8n webhooks, shared by both runners (worker.py, sites_runner.py)."""
import json
import logging
import os
import time

import requests


def call(base: str, token: str, path: str, payload: dict, timeout: float = 30, tries: int = 3,
         wait: float = 5) -> dict:
    """POSTs `payload` to n8n; a lost answer is sent again (the controller treats repeats as one).
    Returns {} when n8n never answered."""
    for attempt in range(tries):
        try:
            r = requests.post(f"{base}/{path}", json=payload, timeout=timeout, headers={"X-Runner-Token": token})
            if r.status_code < 500:
                return r.json() if r.content else {}
        except (requests.RequestException, ValueError):
            pass
        if attempt + 1 < tries:
            time.sleep(wait * (attempt + 1))
    return {}


def save_sessions(api, path: str) -> int:
    """Sign-ins the owner saved (login.yml) come back on every new runner. The file is readable by this
    user only and is never logged. Returns how many."""
    sessions = api("miinspact-login", {"op": "sessions"}).get("sessions") or []
    if not isinstance(sessions, list) or not sessions:
        return 0
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(sessions, f)
    logging.info(f"{len(sessions)} saved sign-in(s) restored")
    return len(sessions)
