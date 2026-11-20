"""Scanning one torrent site on the runner, in Chrome (sites.browser): its feeds, the sections the owner
left on, then the pages of new items, their magnets or .torrent files, and whether anyone shares them.
The controller sends what it already knows (sections) and keeps everything that needs its database:
which pages were handled before (asked in batches through `Memory`), comparing with the library and the
queue, saving, scheduling. Finds go back every PART_SIZE as they come (`send_part`), so a stopped run
loses nothing. Nothing here is logged with an address.
"""
import concurrent.futures
import logging
import re
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from sites import explorer, health, torrent_files
from sites.adult_filter import is_adult

KINDS_ON = ("movie", "series", "latest", "documentary", "play")
# أقسام مواقع التورنت اللي مش فيديو: مابتتعملش
_NOT_MEDIA = re.compile(r"(?<![a-z])(music|mp3|flac|audio|audiobooks?|games?|pc games|software|apps?|applications?|"
                        r"books?|e-?books?|comics?|android|ios|mac|windows|linux|images?|wallpapers?|other|misc)(?![a-z])|"
                        r"موسيقى|اغاني|أغاني|العاب|ألعاب|برامج كمبيوتر|تطبيقات|كتب|صور|خلفيات", re.I)


MAX_QUERIES = 30                  # titles looked for in one search job
MAX_ITEMS_PER_SCAN = 40            # item pages opened in one scan (the rest wait for the next)
MAX_FEED_ITEMS = 120
MAX_FILES_SENT = 200

_TORRENT_HREF = re.compile(r"\.torrent(?:$|[?#&])|[?&](?:type|action|do)=(?:torrent|download)\b|/download\.php|/dl/|/get_torrent|/torrent/download",
                           re.I)
_TORRENT_TEXT = re.compile(r"torrent|تورنت|تحميل|download", re.I)
_MOBILE = re.compile(r"(?<![a-z])(psp|ipod|pda|iphone|mobile|3gp|ppc|zune|treo)(?![a-z])", re.I)
_GOOD_EXT = re.compile(r"\.(mkv|mp4|m4v)\b", re.I)


def torrent_links(html: str, page_url: str) -> dict:
    """{magnets: [..], files: [(url, label)]} من صفحة: روابط ظاهرة بس، والملفات من نفس الموقع أو أي موقع"""
    soup = BeautifulSoup(html, "html.parser")
    magnets, files, seen = [], [], set()
    for a in soup.find_all("a", href=True):
        if explorer._hidden(a):
            continue
        href = a["href"].strip()
        label = re.sub(r"\s+", " ", a.get_text(" ", strip=True) or a.get("title", ""))[:200]
        if href.lower().startswith("magnet:?"):
            if href not in seen:
                seen.add(href)
                magnets.append(href)
            continue
        url = explorer.fix_url(href, page_url)
        if not url or url in seen:
            continue
        if _TORRENT_HREF.search(url) and (".torrent" in url.lower() or _TORRENT_TEXT.search(label + " " + url)):
            seen.add(url)
            files.append((url, label))
    return {"magnets": magnets[:10], "files": files[:10]}


# ---------- الموجز (RSS / Atom) ----------

def discover_feeds(html: str, base: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel") or []).lower()
        kind = (link.get("type") or "").lower()
        if "alternate" in rel and kind in ("application/rss+xml", "application/atom+xml"):
            url = explorer.fix_url(link["href"], base)
            if url and explorer.same_site(url, base) and not is_adult(link.get("title"), url):
                out.append(url)
    return out[:4]


def read_feed(text: str, base: str) -> list:
    """مواد موجز RSS أو Atom: {title, url, magnet, torrent_url, size_bytes, category}"""
    if not text or "<" not in text[:2000]:
        return []
    # تعريفات DOCTYPE/ENTITY بتتشال قبل القراية: بتوقّع القارئ، وممكن تستخدم لقراية ملفات (XXE)
    text = re.sub(r"<!DOCTYPE[^\[>]*(?:\[.*?\])?\s*>", "", text, count=1, flags=re.S | re.I)
    text = re.sub(r"<!ENTITY[^>]*>", "", text, flags=re.I)
    try:
        soup = BeautifulSoup(text, "xml")
    except Exception:  # lxml مش موجود أو الملف تالف
        return []
    entries = soup.find_all(["item", "entry"])[:MAX_FEED_ITEMS]
    out = []
    for e in entries:
        def txt(*names):
            for n in names:
                tag = e.find(n)
                if tag and tag.get_text(strip=True):
                    return tag.get_text(" ", strip=True)
            return ""
        title = txt("title")[:300]
        link_tag = e.find("link")
        link = (link_tag.get("href") if link_tag is not None and link_tag.get("href") else txt("link")) or txt("guid")
        magnet = ""
        torrent_url = ""
        size = None
        for tag in e.find_all(True):
            val = (tag.get_text(strip=True) or "") if not tag.find(True) else ""
            for cand in (val, tag.get("url") or "", tag.get("href") or ""):
                if cand.lower().startswith("magnet:?") and not magnet:
                    magnet = cand
            if tag.name == "enclosure":
                kind = (tag.get("type") or "").lower()
                url = tag.get("url") or ""
                if "bittorrent" in kind or url.lower().split("?")[0].endswith(".torrent"):
                    torrent_url = url
                if (tag.get("length") or "").isdigit():
                    size = int(tag["length"])
            if tag.name in ("contentLength", "size") and val.isdigit():
                size = int(val)
        if link.lower().startswith("magnet:?"):
            magnet, link = magnet or link, ""
        url = explorer.fix_url(link, base) if link else None
        torrent_url = explorer.fix_url(torrent_url, base) if torrent_url else None
        category = " ".join(c.get_text(" ", strip=True) for c in e.find_all("category"))[:200]
        if title or url:
            out.append({"title": title, "url": url, "magnet": magnet[:torrent_files.MAX_MAGNET_CHARS],
                        "torrent_url": torrent_url, "size_bytes": size, "category": category,
                        "summary": txt("description", "summary")[:600]})
    return out


_QUALITY = re.compile(r"(2160|1080|720|576|480)p", re.I)


def _choice_score(name: str, size: int) -> tuple:
    """Best copy: not a phone copy, then mp4/mkv, then quality, then size."""
    name = name or ""
    q = _QUALITY.search(name)
    quality = {"2160": 3, "1080": 4, "720": 2, "576": 1, "480": 1}.get(q.group(1) if q else "", 0)
    return (0 if _MOBILE.search(name) else 1, 1 if _GOOD_EXT.search(name) else 0, quality, size or 0)


def best_copy(copies: list):
    if not copies:
        return None
    return max(copies, key=lambda t: _choice_score(
        t.get("name") or (max(t.get("files") or [("", 0)], key=lambda f: f[1])[0]), t.get("size_bytes") or 0))


def _video_ok(t: dict) -> bool:
    files = t.get("files") or []
    return not files or any(p.lower().endswith(torrent_files.VIDEO_EXT) for p, _ in files)


def _from_page(url: str, session, get_bytes) -> list:
    """The copies on an item page: magnets, or .torrent files (each read and checked)."""
    got = session.get(url)
    if not got or got.get("blocked") or not got.get("html"):
        return []
    links = torrent_links(got["html"], got["url"])
    found = []
    for m in links["magnets"]:
        try:
            found.append(torrent_files.parse_magnet(m))
        except torrent_files.TorrentError:
            pass
    if not found:
        for file_url, _label in links["files"][:4]:
            if not explorer.robots_allows(session.robots, file_url):
                continue
            try:
                data = get_bytes(file_url)
                if data:
                    found.append(dict(torrent_files.parse_torrent_file(data), source_url=file_url))
            except Exception as e:  # a bad file or a closed connection: the next one is tried
                logging.info(f"torrent file skipped ({type(e).__name__})")
    return found


def _life(t):
    if t.get("source_url") and t.get("web_seeds"):
        return {"state": "webseed", "seeds": None, "peers": None}   # the site itself serves it
    try:
        return health.check(t)
    except Exception as e:  # a check never stops the scan
        logging.info(f"life check: {type(e).__name__}")
        return {"state": "unknown", "seeds": None, "peers": None}


def _slim(t: dict) -> dict:
    """What the controller needs of a copy: videos and subtitles of its file list, not thousands of names."""
    keep = torrent_files.VIDEO_EXT + torrent_files.SUB_EXT
    files = [f for f in (t.get("files") or []) if f[0].lower().endswith(keep)][:MAX_FILES_SENT]
    return dict(t, files=files)


def explore(job: dict, fetch, sleep=explorer.polite_sleep) -> dict:
    """A torrent site's map: the general explorer + its feeds + video sections on, adult and non-video ones out.
    Choices the owner already made on old sections are kept."""
    profile = explorer.explore_site(job["base_url"], fetch=fetch, sleep=sleep,
                                    skip=lambda name, url: is_adult(name, url) or bool(_NOT_MEDIA.search(name or "")))
    old = {s["url"]: s for s in (job.get("profile") or {}).get("sections", [])}
    for section in profile["sections"]:
        before = old.get(section["url"])
        if before:
            for key in ("enabled", "kind", "next_url", "full_done"):
                if key in before:
                    section[key] = before[key]
        else:
            section["enabled"] = section["kind"] in KINDS_ON
    home = profile.pop("home_html", None) or ""
    profile["feeds"] = discover_feeds(home, profile["base_url"]) if home else []
    profile["torrent"] = True
    return profile


PART_SIZE = 10                 # finds sent back together while the scan goes on
SEEN_BATCH = 500               # page addresses asked about in one question


class Memory:
    """Pages this site already handled. The whole memory stays on the Oracle server (no cap); `ask(urls)`
    returns the known ones, asked in batches and kept here for the rest of the run."""

    def __init__(self, ask=None):
        self.ask, self.known, self.asked = ask, set(), set()

    def prime(self, urls):
        new = [u for u in dict.fromkeys(urls) if u and u not in self.asked]
        for i in range(0, len(new), SEEN_BATCH):
            chunk = new[i:i + SEEN_BATCH]
            self.asked.update(chunk)
            if self.ask:
                try:
                    self.known.update(u for u in (self.ask(chunk) or []) if u in chunk)
                except Exception as e:  # no answer: these are opened again; the controller keeps one copy
                    logging.info(f"memory question not answered ({type(e).__name__})")

    def __call__(self, url) -> bool:
        if url not in self.asked:
            self.prime([url])
        return url in self.known

    def add(self, url):
        self.asked.add(url)
        self.known.add(url)


def _check_life(finds: list, stats: dict):
    with concurrent.futures.ThreadPoolExecutor(max_workers=health.WORKERS) as ex:
        for f, res in zip(finds, ex.map(lambda f: _life(f["copy"]), finds)):
            f["life"] = res
            stats["dead"] += res["state"] == "dead"


SEARCH_NAMES = ("q", "s", "search", "query", "keyword", "keywords", "k", "term", "search_query", "searchtext")


def search_address(html: str, base: str, query: str) -> str:
    """The site's own search box, found on its home page (any site): its GET form with a search field.
    Without one, the common `/?s=` search."""
    soup = BeautifulSoup(html or "", "html.parser")
    for form in soup.find_all("form"):
        if (form.get("method") or "get").lower() != "get":
            continue
        fields = form.find_all("input")
        box = next((i for i in fields if (i.get("type") or "").lower() == "search" and i.get("name")), None) or \
            next((i for i in fields if (i.get("name") or "").lower() in SEARCH_NAMES), None)
        if not box:
            continue
        action = urljoin(base + "/", form.get("action") or "/")
        if not explorer.same_site(action, base):
            continue
        params = {i["name"]: i.get("value") or "" for i in fields
                  if i.get("name") and (i.get("type") or "").lower() == "hidden"}
        params[box["name"]] = query
        return action.split("#")[0].split("?")[0] + "?" + urlencode(params)
    return urljoin(base + "/", "/") + "?" + urlencode({"s": query})


def _words(text: str) -> list:
    return [w for w in re.split(r"[^0-9a-z\u0621-\u064a]+", (text or "").lower()) if len(w) > 1]


def matches(title: str, wanted: str) -> bool:
    """A search result is kept only when it carries every word of the wanted title"""
    have = set(_words(title))
    need = _words(wanted)
    return bool(need) and all(w in have for w in need)


def _search_hits(html: str, url: str, base: str, profile) -> list:
    """The results on a search page: its own cards (any site), or the cards the site's sections use"""
    soup = BeautifulSoup(html or "", "html.parser")
    _fp, items = explorer.find_cards(soup, url, base, min_cards=2)
    for section in ([] if items else (profile or {}).get("sections") or []):
        if section.get("fingerprint"):
            items = explorer.read_cards(soup, section["fingerprint"], url, base)
            if items:
                break
    if not items:                              # a single result: any link to the site's own pages
        items = [{"title": a.get_text(" ", strip=True), "url": u} for a in soup.find_all("a", href=True)
                 for u in [explorer.fix_url(a["href"], url)] if u and a.get_text(strip=True)]
    return items


def _search_entries(job: dict, session, base: str, profile) -> list:
    home = session.get(urljoin(base + "/", "/"))
    entries = []
    for q in (job.get("queries") or [])[:MAX_QUERIES]:
        if not isinstance(q, dict) or not str(q.get("q") or "").strip():
            continue
        got = session.get(search_address((home or {}).get("html") or "", base, str(q["q"])[:150]))
        if not got or got.get("blocked") or not got.get("html"):
            continue
        entries += [{"title": i["title"], "url": i["url"], "magnet": i.get("magnet", ""), "poster": i.get("image"),
                     "torrent_url": None, "size_bytes": None, "category": "search", "source": "search"}
                    for i in _search_hits(got["html"], got.get("url") or base, base, profile)
                    if explorer.same_site(i.get("url") or "", base)
                    and matches(i.get("title"), str(q.get("match") or q["q"]))]
    return entries


def scan(job: dict, fetch, get_bytes, sleep=explorer.polite_sleep, check_life: bool = True,
         memory: Memory = None, send_part=None) -> dict:
    """job: {base_url, feeds, explore, profile, max_items, queries}. With `queries` ([{q, match}]) only the
    site's own search is read, for those titles. `memory` knows the pages handled before;
    `send_part({finds, done_urls})` saves finds while the scan goes on.
    Returns the rest: {finds: [{copy, url, poster, title, life}], done_urls, profile, stats}."""
    base = job.get("base_url") or ""
    max_items = int(job.get("max_items") or MAX_ITEMS_PER_SCAN)
    seen = memory if memory is not None else Memory()
    stats = {"seen": 0, "adult": 0, "known": 0, "no_torrent": 0, "dead": 0}
    profile = job.get("profile") or None
    session = explorer._Session(fetch, sleep, robots=(profile or {}).get("robots"), limit=max_items + 30)
    if session.robots == {} and not (profile or {}).get("robots"):
        got = session.get(urljoin(base + "/", "/robots.txt"))
        if got and got["html"] and "<html" not in got["html"][:500].lower():
            session.robots = explorer.parse_robots(got["html"])

    entries = []
    if job.get("queries"):                     # looking for what is missing (series seasons, film series parts)
        entries = _search_entries(job, session, base, profile)
    feeds = [] if job.get("queries") else list(job.get("feeds") or []) + list((profile or {}).get("feeds") or [])
    if not feeds and not profile and job.get("explore", True) and not job.get("queries"):
        profile = explore(job, fetch, sleep)
        feeds = list(profile.get("feeds") or [])
    for feed in dict.fromkeys(feeds):
        got = session.get(feed)
        if got and got.get("html"):
            entries += [dict(e, source="feed") for e in read_feed(got["html"], base)]
    if profile and job.get("explore", True) and not job.get("queries"):
        for section in [s for s in profile.get("sections", []) if s.get("enabled")]:
            fresh, state = explorer.index_section(section, profile["base_url"], seen,
                                                  fetch=fetch, sleep=sleep, max_pages=2)
            section.update(state)
            entries += [{"title": i["title"], "url": i["url"], "magnet": i.get("magnet", ""), "poster": i.get("image"),
                         "torrent_url": None, "size_bytes": None, "category": section["name"], "source": "section"}
                        for i in fresh]

    finds, done_urls, opened = [], [], 0

    def flush():
        nonlocal finds, done_urls
        if check_life:
            _check_life([f for f in finds if "life" not in f], stats)
        try:
            send_part({"finds": finds, "done_urls": [u for u in done_urls if u]})
            finds, done_urls = [], []
        except Exception as err:  # not sent: they go with the final answer
            logging.info(f"finds not sent yet ({type(err).__name__})")

    seen.prime([e["url"] for e in entries if e.get("url")])
    for e in entries:
        stats["seen"] += 1
        if is_adult(e.get("title"), e.get("category"), e.get("url"), e.get("summary")):
            stats["adult"] += 1
            done_urls.append(e.get("url"))
            continue
        if e.get("url") and seen(e["url"]):
            stats["known"] += 1
            continue
        copies = []
        if e.get("magnet"):
            try:
                copies.append(torrent_files.parse_magnet(e["magnet"]))
            except torrent_files.TorrentError:
                pass
        if not copies and e.get("torrent_url") and explorer.robots_allows(session.robots, e["torrent_url"]):
            try:
                data = get_bytes(e["torrent_url"])
                if data:
                    copies.append(dict(torrent_files.parse_torrent_file(data), source_url=e["torrent_url"]))
            except Exception as err:  # a bad file: the item page may still have a magnet
                logging.info(f"torrent file skipped ({type(err).__name__})")
        if not copies and e.get("url"):
            if opened >= max_items:
                continue                       # the rest wait for the next scan
            opened += 1
            copies = _from_page(e["url"], session, get_bytes)
        copies = [c for c in copies if _video_ok(c) and not is_adult(c.get("name"))]
        done_urls.append(e.get("url"))
        if e.get("url"):
            seen.add(e["url"])
        t = best_copy(copies)
        if not t:
            stats["no_torrent"] += 1
            continue
        if not t.get("name"):
            t["name"] = e.get("title") or ""
        finds.append({"copy": _slim(t), "url": e.get("url"), "poster": e.get("poster"), "title": e.get("title") or ""})
        if send_part and len([f for f in finds if "life" not in f]) >= PART_SIZE:
            flush()

    if check_life and finds:
        _check_life([f for f in finds if "life" not in f], stats)
    stats.update(requests=session.count, blocked=session.blocked, errors=session.errors)
    if profile is not None and session.robots and not profile.get("robots"):
        profile["robots"] = session.robots
    return {"finds": finds, "done_urls": [u for u in done_urls if u], "profile": profile, "stats": stats}
