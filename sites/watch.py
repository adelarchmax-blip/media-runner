"""Watch sites (pages with a player): the address check, the sections map and the daily crawl, all in Chrome here.

  watch_explore  the site's sections map            -> explore()
  watch_crawl    new items of the sections the owner turned on, newest first -> crawl()

Both first check the site's address: the home page is opened and, when it moved, the new address is kept.
When it does not answer at all, a search (the words the controller sent) finds the new one.
The controller keeps the memory of handled pages and the queue; answers carry pages and counts only.
"""
import logging
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup

from sites import explorer

SEARCH = "https://html.duckduckgo.com/html/?q="
MAX_CANDIDATES = 8
_NOT_SITES = ("x.com", "twitter.com", "facebook.com", "youtube.com", "t.me", "telegram.me", "instagram.com",
              "tiktok.com", "wikipedia.org", "pinterest.com", "duckduckgo.com", "reddit.com", "google.")
KEEP = ("enabled", "kind", "next_url", "full_done")       # the owner's choices and the crawl's place, per section


def _origin(url: str) -> str:
    parts = urlsplit(url or "")
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme in ("http", "https") and parts.netloc else ""


def _answers(fetch, url: str):
    """The page's own address when it opens as a real page, else None."""
    try:
        status, final, html = fetch(url)
    except Exception as e:  # a dead address is the normal case here
        logging.info(f"address check: {type(e).__name__}")
        return None
    if status >= 400 or explorer.looks_blocked(status, html) or not (html or "").strip():
        return None
    return final or url


def _search(fetch, query: str) -> str:
    try:
        _status, _final, html = fetch(SEARCH + quote(query))
    except Exception as e:  # the search engine did not answer: the old address stays
        logging.info(f"address search: {type(e).__name__}")
        return ""
    seen = []
    for tag in BeautifulSoup(html or "", "html.parser").select(".result__url"):
        text = tag.get_text(strip=True)
        origin = _origin(text if text.startswith("http") else "https://" + text)
        host = urlsplit(origin).netloc.lower()
        if origin and origin not in seen and not any(bad in host for bad in _NOT_SITES):
            seen.append(origin)
        if len(seen) >= MAX_CANDIDATES:
            break
    for origin in seen:
        final = _answers(fetch, origin)
        if final:
            return _origin(final)
    return ""


def address(job: dict, fetch) -> dict:
    """{base_url, moved, down}: where the site is now."""
    old = _origin(job.get("base_url") or "")
    if not old:
        raise ValueError("bad address")
    final = _answers(fetch, old)
    if final:
        new = _origin(final) or old
        return {"base_url": new, "moved": new != old, "down": False}
    query = str(job.get("find_query") or "").strip()[:200]
    new = _search(fetch, query) if query else ""
    if new and new != old:
        return {"base_url": new, "moved": True, "down": False}
    return {"base_url": old, "moved": False, "down": True}


def _moved_url(url, old: str, new: str):
    if isinstance(url, str) and old and url.startswith(old):
        return new + url[len(old):]
    return url


def explore(job: dict, fetch, sleep=explorer.polite_sleep) -> dict:
    """The site's map. Sections the owner already had keep their choices; new ones start off."""
    where = address(job, fetch)
    if where["down"]:
        raise ConnectionError("site not answering")
    profile = explorer.explore_site(where["base_url"], fetch=fetch, sleep=sleep)
    profile.pop("home_html", None)
    old_origin = _origin(job.get("base_url") or "")
    old = {_moved_url(s.get("url"), old_origin, where["base_url"]): s
           for s in ((job.get("profile") or {}).get("sections") or []) if isinstance(s, dict)}
    for section in profile["sections"]:
        before = old.get(section["url"])
        for key in KEEP:
            if before and key in before:
                section[key] = _moved_url(before[key], old_origin, where["base_url"]) if key == "next_url" else before[key]
        section.setdefault("enabled", False)
    if isinstance((job.get("profile") or {}).get("player"), dict):
        profile["player"] = job["profile"]["player"]
    return {"profile": profile, "base_url": where["base_url"], "moved": where["moved"]}


def _slim(item: dict) -> dict:
    return {k: item.get(k) for k in ("url", "title", "image", "year") if item.get(k) is not None}


def crawl(job: dict, fetch, memory, send_part, sleep=explorer.polite_sleep) -> dict:
    """New items of the turned-on sections. Each section's items and place go back as soon as it is read,
    so a stopped run loses nothing; the player's way is learnt once, from the first new item."""
    profile = job.get("profile") if isinstance(job.get("profile"), dict) else {}
    where = address(job, fetch)
    if where["down"]:
        raise ConnectionError("site not answering")
    base, old = where["base_url"], _origin(job.get("base_url") or "")
    player = profile.get("player") if isinstance(profile.get("player"), dict) else None
    stats = {"sections": 0, "new": 0}
    for section in profile.get("sections") or []:
        if not isinstance(section, dict) or not section.get("enabled") or not section.get("fingerprint"):
            continue
        section = dict(section, url=_moved_url(section.get("url"), old, base),
                       next_url=_moved_url(section.get("next_url"), old, base))
        fresh, state = explorer.index_section(section, base, memory, fetch=fetch, sleep=sleep,
                                              max_pages=int(job.get("max_pages") or 3))
        part = {"section": section["url"], "items": [_slim(i) for i in fresh], "state": state}
        if fresh and player is None:
            session = explorer._Session(fetch, sleep, robots=profile.get("robots"), limit=3)
            got = session.get(fresh[0]["url"])
            player = explorer.learn_player(got["url"], got["html"], profile.get("robots") or {}, session.get) \
                if got and got.get("html") else {"kind": "unknown", "example": fresh[0]["url"]}
            part["player"] = player
        send_part(part)
        for item in fresh:
            memory.add(item["url"])
        stats["sections"] += 1
        stats["new"] += len(fresh)
        if player and player.get("kind") == "closed":
            break                   # the site closed its watch pages to programs: nothing of it is queued
    logging.info(f"watch crawl: {stats['sections']} sections, {stats['new']} new")
    return {"base_url": base, "moved": where["moved"], "stats": stats}
