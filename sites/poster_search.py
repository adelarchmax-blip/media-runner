"""A cover for a title that has none yet, looked up before anything is downloaded.

Order for each title (first good answer wins):
  1. IMDb title suggestions (public JSON, no key)
  2. Google Images in the real Chrome tab
  3. Bing Images in the same tab

A portrait picture (a poster is 2:3) beats a wide one. If a search engine shows a check page
("unusual traffic", CAPTCHA) it is left alone for the rest of the job; nothing tries to get past it.
The controller sends titles only; the answer is a picture address per title.
"""
import html
import json
import logging
import re
import urllib.parse

import requests

IMDB = "https://v3.sg.media-imdb.com/suggestion/x/{}.json"
GOOGLE = "https://www.google.com/search?tbm=isch&hl=en&q={}"
BING = "https://www.bing.com/images/search?form=HDRSC2&q={}"
HEADERS = {"User-Agent": "media-runner/1.0 (personal media library)", "Accept": "application/json"}
TIMEOUT = 15
MAX_ITEMS = 20
MAX_URL = 1000
_GOOGLE_IMG = re.compile(r'\["(https://(?:[^"\\]|\\.){10,900}?)",(\d{2,5}),(\d{2,5})\]')
_BLOCKED = re.compile(r"unusual traffic|/sorry/|captcha|recaptcha|not a robot", re.I)
_SKIP_HOSTS = ("gstatic.com", "googleusercontent.com", "google.com", "bing.com", "bing.net", "facebook.com", "fbsbx.com")
_BAD = re.compile(r"[\"'<>\s\\]")


def _clean(url):
    if not isinstance(url, str) or not url.startswith("https://") or len(url) > MAX_URL or _BAD.search(url):
        return None
    host = urllib.parse.urlsplit(url).netloc.lower()
    if not host or any(host == h or host.endswith("." + h) for h in _SKIP_HOSTS):
        return None
    return url


def _words(text) -> set:
    return set(re.findall(r"[\w']+", str(text or "").lower()))


def imdb(title: str, year=None, kind="movie", http=requests):
    q = re.sub(r"[^\w\s']", " ", title).strip().lower()
    if not q:
        return None
    r = http.get(IMDB.format(urllib.parse.quote(q[:80])), headers=HEADERS, timeout=TIMEOUT)
    if r.status_code != 200:
        return None
    want = _words(title)
    for hit in (r.json() or {}).get("d") or []:
        if not isinstance(hit, dict) or not isinstance(hit.get("i"), dict):
            continue
        series = str(hit.get("q") or "").lower().startswith("tv")
        if (kind == "series") != series:
            continue
        if year and isinstance(hit.get("y"), int) and abs(hit["y"] - int(year)) > 1:
            continue
        if want and not want & _words(hit.get("l")):
            continue
        img = _clean(hit["i"].get("imageUrl"))
        if img:
            return img
    return None


def _query(item: dict) -> str:
    parts = [item["title"]]
    if item.get("year"):
        parts.append(str(item["year"]))
    parts.append("tv series poster" if item.get("type") == "series" else "movie poster")
    return " ".join(parts)


def _pick(found) -> str:
    """found: (url, h, w) best first; a tall picture (a poster) wins over a wide one"""
    tall = [u for u, h, w in found if h and w and h >= w * 1.2]
    return (tall or [u for u, _h, _w in found] or [None])[0]


def _unescape(raw: str):
    try:
        return json.loads('"' + raw + '"')     # \u003d and friends, as the page script wrote them
    except ValueError:
        return None


def google(text: str) -> list:
    out = []
    for m in _GOOGLE_IMG.finditer(text):
        url = _clean(_unescape(m.group(1)))
        if url:
            out.append((url, int(m.group(2)), int(m.group(3))))
    return out[:30]


def bing(text: str) -> list:
    out = []
    for m in re.finditer(r'\bm="([^"]{20,4000})"', text):
        try:
            data = json.loads(html.unescape(m.group(1)))
        except ValueError:
            continue
        url = _clean(data.get("murl")) if isinstance(data, dict) else None
        if url:
            out.append((url, 0, 0))
    return out[:30]


def search(work: dict, fetch, http=requests) -> dict:
    items = [i for i in (work.get("items") or [])[:MAX_ITEMS]
             if isinstance(i, dict) and isinstance(i.get("title"), str) and i["title"].strip() and isinstance(i.get("key"), str)]
    blocked, results = set(), []
    for item in items:
        title, poster = item["title"].strip()[:200], None
        try:
            poster = imdb(title, item.get("year"), item.get("type"), http)
        except Exception as e:  # one database out: the search engines are next
            logging.info(f"poster: imdb {type(e).__name__}")
        for name, url, read in (("google", GOOGLE, google), ("bing", BING, bing)):
            if poster or name in blocked:
                continue
            try:
                status, _final, text = fetch(url.format(urllib.parse.quote_plus(_query(item))))
            except Exception as e:
                logging.info(f"poster: {name} {type(e).__name__}")
                continue
            if status in (403, 429) or _BLOCKED.search(text[:20000] if status != 200 else _final or ""):
                blocked.add(name)          # a check page is for a person: this engine is left alone from now on
                logging.info(f"poster: {name} shows a check page, skipped for the rest of the job")
                continue
            poster = _pick(read(text))
        results.append({"key": item["key"][:300], "poster": poster})
    return {"results": results}
