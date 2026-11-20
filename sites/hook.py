"""Talking to the controller through the n8n webhooks, shared by both runners (worker.py, sites_runner.py)."""
import json
import logging
import os
import time

import requests


def call(base: str, token: str, path: str, payload: dict, timeout: float = 30, tries: int = 3,
         wait: float = 5) -> dict:
    """POSTs `payload` to n8n; a lost answer is sent again (the controller treats repeats as one).
    Returns the answer, or {} when n8n refused or never answered (said in the log, so a changed n8n shows)."""
    why = "no answer"
    for attempt in range(tries):
        try:
            r = requests.post(f"{base}/{path}", json=payload, timeout=timeout, headers={"X-Runner-Token": token})
            if r.status_code < 300:
                data = r.json() if r.content else {}
                return data if isinstance(data, dict) else {}
            if r.status_code < 500:                       # missing route or refused: sending again will not help
                logging.warning(f"n8n refused {path} ({r.status_code}): is the workflow imported and active?")
                return {}
            why = f"error {r.status_code}"
        except requests.RequestException as e:
            why = type(e).__name__
        except ValueError:
            why = "answer not understood"
        if attempt + 1 < tries:
            time.sleep(wait * (attempt + 1))
    logging.warning(f"n8n gave no usable answer to {path} after {tries} tries ({why})")
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
