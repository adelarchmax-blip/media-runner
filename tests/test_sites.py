"""Site work on the runner: the adult filter, links and feeds in pages, a scan and a map with a fake site,
and the job lane with a fake n8n and a fake browser. No network."""
import hashlib
import os
import sys

import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from sites import lane, torrent_scan as T  # noqa: E402
from sites.adult_filter import is_adult  # noqa: E402

BASE = "https://tor.example"


def benc(v) -> bytes:
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, str):
        v = v.encode()
    if isinstance(v, bytes):
        return b"%d:%s" % (len(v), v)
    if isinstance(v, list):
        return b"l" + b"".join(benc(x) for x in v) + b"e"
    return b"d" + b"".join(benc(k) + benc(v[k]) for k in sorted(v)) + b"e"


def make_torrent(files, name):
    info = {"name": name, "piece length": 262144, "pieces": b"\x00" * 20,
            "files": [{"length": s, "path": p.split("/")} for p, s in files]}
    root = {"announce": "udp://tracker.example:1337/announce", "info": info,
            "url-list": ["https://archive.example/download/x/"]}
    return benc(root), hashlib.sha1(benc(info)).hexdigest()


@pytest.mark.parametrize("text", ["XXX Movies", "افلام سكس", "/category/porn/", "فيلم للكبار فقط", "Brazzers 2024",
                                  "Hentai pack", "مشاهد ساخنة", "Adult Videos", "18+ only", "/adult/"])
def test_adult_caught(text):
    assert is_adult(text)


@pytest.mark.parametrize("text", ["عريس من جهة أمنية", "نيكول كيدمان", "الجنسية", "Sussex Story", "Essex Boys",
                                  "Analyze This", "Movies", "مسلسلات رمضان", "Middlesex Hospital", "Adult Swim", ""])
def test_normal_titles_pass(text):
    assert not is_adult(text)


def test_torrent_links_magnet_and_files_visible_only():
    html = f"""<a href="magnet:?xt=urn:btih:{'a' * 40}&dn=X">Magnet</a>
               <a href="/dl/x.torrent">Download torrent</a>
               <a href="/download.php?id=5">تحميل تورنت</a>
               <a href="/movie/5">Movie page</a>
               <a style="display:none" href="/hidden.torrent">hidden</a>"""
    got = T.torrent_links(html, BASE + "/m/1")
    assert len(got["magnets"]) == 1
    assert [u for u, _ in got["files"]] == [BASE + "/dl/x.torrent", BASE + "/download.php?id=5"]


def test_read_feed_rss_doctype_is_dropped_and_parts_read():
    xml = f"""<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
    <rss><channel>
      <item><title>Metropolis (1927)</title><link>{BASE}/details/metropolis</link><category>Movies</category></item>
      <item><title>Nosferatu 1922 720p</title><link>magnet:?xt=urn:btih:{'b' * 40}&amp;dn=Nosferatu</link></item>
      <item><title>Other</title><link>{BASE}/x</link>
            <enclosure url="{BASE}/f.torrent" type="application/x-bittorrent" length="123"/></item>
      <item><title>&xxe;</title><link>{BASE}/y</link></item>
    </channel></rss>"""
    items = T.read_feed(xml, BASE)
    assert items[0]["url"] == BASE + "/details/metropolis" and items[0]["category"] == "Movies"
    assert items[1]["magnet"].startswith("magnet:?") and items[1]["url"] is None
    assert items[2]["torrent_url"] == BASE + "/f.torrent" and items[2]["size_bytes"] == 123
    assert all("root:" not in (i["title"] or "") for i in items)


def test_read_feed_garbage():
    assert T.read_feed("not xml at all", BASE) == []
    assert T.read_feed("", BASE) == []


def test_discover_feeds_same_site_only():
    html = """<link rel="alternate" type="application/rss+xml" href="/rss.php">
              <link rel="alternate" type="application/rss+xml" href="https://evil.example/rss">
              <link rel="alternate" type="application/rss+xml" title="XXX feed" href="/xxx.rss">"""
    assert T.discover_feeds(html, BASE) == [BASE + "/rss.php"]


def test_best_copy_prefers_real_video_over_mobile():
    copies = [{"name": "Film_PSP.MP4", "size_bytes": 100}, {"name": "Film.avi", "size_bytes": 700},
              {"name": "Film.mp4", "size_bytes": 600}]
    assert T.best_copy(copies)["name"] == "Film.mp4"
    assert T.best_copy([]) is None


def fake_site(pages: dict, files: dict):
    asked = []

    def fetch(url):
        asked.append(url)
        return (200, url, pages[url]) if url in pages else (404, url, "")
    return fetch, files.get, asked


def test_scan_feed_page_torrent_file_adult_and_seen(monkeypatch):
    questions = []

    def ask(urls):
        questions.append(list(urls))
        return [u for u in urls if u.endswith("/old")] + ["https://other.example/not-asked"]
    monkeypatch.setattr(T.health, "check", lambda t: pytest.fail("a web-seeded file needs no tracker check"))
    data, _h = make_torrent([("Metropolis/metropolis.mp4", 900 * 1048576), ("Metropolis/m.ar.srt", 50),
                             ("Metropolis/cover.jpg", 9)], "metropolis_1927")
    feed = f"""<rss><channel>
      <item><title>Metropolis (1927)</title><link>{BASE}/details/metropolis</link></item>
      <item><title>Hot XXX tape</title><link>{BASE}/details/bad</link></item>
      <item><title>No torrent here</title><link>{BASE}/details/none</link></item>
      <item><title>Old one</title><link>{BASE}/details/old</link></item>
    </channel></rss>"""
    pages = {BASE + "/robots.txt": "User-agent: *\nDisallow:\n", BASE + "/rss": feed,
             BASE + "/details/metropolis": '<a href="/download/metropolis/m.torrent">TORRENT</a>',
             BASE + "/details/none": "<p>nothing</p>"}
    fetch, get_bytes, asked = fake_site(pages, {BASE + "/download/metropolis/m.torrent": data})
    job = {"base_url": BASE, "feeds": [BASE + "/rss"], "explore": False}
    memory = T.Memory(ask)
    res = T.scan(job, fetch, get_bytes, sleep=lambda: None, memory=memory)
    assert len(questions) == 1 and len(questions[0]) == 4          # one question for the whole feed
    assert "https://other.example/not-asked" not in memory.known
    s = res["stats"]
    assert s["seen"] == 4 and s["adult"] == 1 and s["no_torrent"] == 1 and s["known"] == 1
    assert len(res["finds"]) == 1
    f = res["finds"][0]
    assert f["title"] == "Metropolis (1927)" and f["life"]["state"] == "webseed"
    assert f["copy"]["source_url"] == BASE + "/download/metropolis/m.torrent"
    assert [p for p, _ in f["copy"]["files"]] == ["Metropolis/metropolis.mp4", "Metropolis/m.ar.srt"]
    assert BASE + "/details/bad" not in asked and BASE + "/details/old" not in asked
    assert set(res["done_urls"]) == {BASE + "/details/metropolis", BASE + "/details/bad", BASE + "/details/none"}


def test_memory_batches_and_survives_no_answer(monkeypatch):
    monkeypatch.setattr(T, "SEEN_BATCH", 2)
    asked = []
    m = T.Memory(lambda urls: asked.append(urls) or urls[:1])
    m.prime(["a", "b", "c", "a", ""])
    assert asked == [["a", "b"], ["c"]] and m("a") and m("c") and not m("b")
    assert m("d") and asked[-1] == ["d"]                           # a new one is asked alone, once
    m("d")
    assert len(asked) == 3

    def down(urls):
        raise ConnectionError("n8n")
    m = T.Memory(down)
    assert m("x") is False                                         # no answer: opened again, never lost
    m.add("x")
    assert m("x") is True


def test_finds_go_back_as_they_come(monkeypatch):
    monkeypatch.setattr(T, "PART_SIZE", 2)
    monkeypatch.setattr(T.health, "check", lambda t: {"state": "alive", "seeds": 1, "peers": 0})
    items = "".join(f"<item><title>Film {i} 1950</title><link>{BASE}/t/{i}</link>"
                    f"<enclosure url='magnet:?xt=urn:btih:{i:040x}&amp;dn=Film.{i}.1950.mp4'/></item>" for i in range(1, 6))
    pages = {BASE + "/robots.txt": "", BASE + "/rss": f"<rss><channel>{items}</channel></rss>"}
    fetch, get_bytes, _asked = fake_site(pages, {})
    parts, fail = [], [False]

    def send(part):
        if fail[0]:
            raise RuntimeError("not saved")
        parts.append(part)
    job = {"base_url": BASE, "feeds": [BASE + "/rss"], "explore": False}
    res = T.scan(job, fetch, get_bytes, sleep=lambda: None, send_part=send)
    assert [len(p["finds"]) for p in parts] == [2, 2] and len(res["finds"]) == 1
    assert all(f["life"]["state"] == "alive" for p in parts for f in p["finds"]) and res["finds"][0]["life"]
    assert len(parts[0]["done_urls"]) == 2
    # a part that could not be sent goes with the final answer
    parts.clear()
    fail[0] = True
    res = T.scan(job, fetch, get_bytes, sleep=lambda: None, send_part=send)
    assert parts == [] and len(res["finds"]) == 5 and len(res["done_urls"]) == 5
    assert res["stats"]["dead"] == 0


ROWS = "".join(f'<tr class="r"><td><a href="/t/{i}">Old Film {i} 1950 720p</a></td>'
               f'<td><a href="magnet:?xt=urn:btih:{i:040x}&amp;dn=Old.Film.{i}.1950.720p.mp4">m</a></td></tr>'
               for i in range(1, 9))
SITE = f"<html><body><nav><a href='/movies'>Movies</a> <a href='/xxx'>XXX</a> <a href='/games'>Games</a></nav>" \
       f"<table><tbody class='list'>{ROWS}</tbody></table></body></html>"


def test_scan_without_feeds_maps_the_site_and_skips_adult_and_games(monkeypatch):
    monkeypatch.setattr(T.health, "check", lambda t: {"state": "alive", "seeds": 7, "peers": 2})
    page = SITE
    fetch, get_bytes, _ = fake_site({BASE + "/robots.txt": "", BASE + "/": page, BASE + "/movies": page}, {})
    res = T.scan({"base_url": BASE}, fetch, get_bytes, sleep=lambda: None)
    assert len(res["finds"]) == 8 and all(f["life"]["seeds"] == 7 for f in res["finds"])
    names = [s["name"] for s in res["profile"]["sections"]]
    assert names and not any("XXX" in n or "Games" in n for n in names)
    assert all(s["enabled"] for s in res["profile"]["sections"] if s["kind"] in T.KINDS_ON)


def test_dead_finds_are_counted(monkeypatch):
    monkeypatch.setattr(T.health, "check", lambda t: {"state": "dead", "seeds": 0, "peers": 0})
    feed = f"<rss><channel><item><title>Dead Film 1950</title><link>magnet:?xt=urn:btih:{'c' * 40}&amp;dn=Dead.Film.1950" \
           f"&amp;tr=udp://t.example:1/announce</link></item></channel></rss>"
    fetch, get_bytes, _ = fake_site({BASE + "/robots.txt": "", BASE + "/rss": feed}, {})
    res = T.scan({"base_url": BASE, "feeds": [BASE + "/rss"], "explore": False}, fetch, get_bytes, sleep=lambda: None)
    assert res["stats"]["dead"] == 1 and res["finds"][0]["life"]["state"] == "dead"


def test_explore_keeps_the_owners_old_choices():
    fetch, _get, _ = fake_site({BASE + "/robots.txt": "", BASE + "/": SITE, BASE + "/movies": SITE}, {})
    first = T.explore({"base_url": BASE}, fetch, sleep=lambda: None)
    home = first["sections"][0]
    assert home["enabled"] is True
    home["enabled"] = False
    again = T.explore({"base_url": BASE, "profile": first}, fetch, sleep=lambda: None)
    assert again["sections"][0]["url"] == home["url"] and again["sections"][0]["enabled"] is False
    assert again["torrent"] is True


# ---------- the job lane ----------

class FakeTab:
    open_now = 0

    def __enter__(self):
        FakeTab.open_now += 1
        return self

    def __exit__(self, *exc):
        FakeTab.open_now -= 1

    def fetch(self, url):
        return 404, url, ""

    def get_bytes(self, url):
        return None


class FakeChrome:
    opened = 0

    def __init__(self, _sessions):
        pass

    def __enter__(self):
        FakeChrome.opened += 1
        return self

    def __exit__(self, *exc):
        FakeChrome.opened -= 1

    def tab(self):
        return FakeTab()


def fake_api(jobs):
    answers, asks = [], []
    lock = threading.Lock()

    def api(path, data):
        assert path == lane.HOOK and data["run_id"] == "r1"
        with lock:
            if data["op"] == "next":
                asks.append(list(data["kinds"]))
                for j in list(jobs):
                    if j["kind"] in data["kinds"] or j["kind"] not in (*lane.LIGHT_KINDS, *lane.BROWSER_KINDS):
                        jobs.remove(j)
                        return {"job": j}
                return {"job": None}
            answers.append(data)
        return {"ok": True, "known": []}
    return api, answers, asks


def go(api, **kw):
    return lane.run(api, **dict({"browser_cls": FakeChrome, "idle_minutes": 0, "poll": 0.01, "run_id": "r1"}, **kw))


def test_lane_does_each_kind_and_closes_chrome(monkeypatch):
    monkeypatch.setattr(lane.health, "check", lambda t: {"state": "alive", "seeds": 3, "peers": 0})
    monkeypatch.setattr(lane.torrent_scan, "scan", lambda work, fetch, get_bytes, memory, send_part:
                        {"finds": [], "stats": {"seen": 0}})
    jobs = [{"id": 1, "kind": "life", "work": {"items": [{"id": 5, "magnet": f"magnet:?xt=urn:btih:{'a' * 40}"},
                                                         {"id": 6, "magnet": "not a magnet"}, {"id": "x"}]}},
            {"id": 2, "kind": "torrent_scan", "work": {"base_url": BASE}},
            {"id": 3, "kind": "what", "work": {}}]
    api, answers, _asks = fake_api(jobs)
    assert go(api) == 3
    assert FakeChrome.opened == 0 and FakeTab.open_now == 0
    done = {a["id"]: a for a in answers if a["op"] == "done"}
    assert done[1]["result"]["results"] == [{"state": "alive", "seeds": 3, "peers": 0, "id": 5},
                                            {"state": "dead", "seeds": 0, "peers": 0, "id": 6}]
    assert done[2] == {"op": "done", "id": 2, "run_id": "r1", "result": {"finds": [], "stats": {"seen": 0}}}
    assert done[3] == {"op": "done", "id": 3, "run_id": "r1", "error": "ValueError"}


def test_lane_reads_sites_side_by_side_with_heartbeat_memory_and_parts(monkeypatch):
    monkeypatch.setattr(lane, "BEAT", 0.05)
    together = threading.Barrier(3, timeout=5)        # fails unless three scans run at the same time

    def scan(work, fetch, get_bytes, memory, send_part):
        together.wait()
        memory.prime([BASE + "/a"])
        send_part({"finds": [], "done_urls": [BASE + "/a"]})
        time.sleep(0.15)                              # long enough for a heartbeat
        return {"finds": [], "stats": {}}
    monkeypatch.setattr(lane.torrent_scan, "scan", scan)
    jobs = [{"id": i, "kind": "torrent_scan", "work": {}} for i in (1, 2, 3)] + \
        [{"id": 4, "kind": "life", "work": {"items": []}}]
    api, answers, asks = fake_api(jobs)
    assert go(api) == 4
    ops = [a["op"] for a in answers]
    assert ops.count("done") == 4 and ops.count("seen") == 3 and ops.count("part") == 3 and "beat" in ops
    assert all(set(a["ids"]) <= {1, 2, 3, 4} for a in answers if a["op"] == "beat")
    assert list(lane.LIGHT_KINDS) in asks             # full browser slots: only light jobs are asked for


def test_lane_failure_is_reported_and_life_needs_no_browser(monkeypatch):
    def boom(work, fetch):
        raise RuntimeError(f"cannot reach {BASE}")
    monkeypatch.setattr(lane.torrent_scan, "explore", boom)
    api, answers, _ = fake_api([{"id": 9, "kind": "torrent_explore", "work": {"base_url": BASE}}])
    assert go(api) == 1
    assert answers == [{"op": "done", "id": 9, "run_id": "r1", "error": "RuntimeError"}]   # no address
    api, answers, _ = fake_api([{"id": 4, "kind": "life", "work": {"items": []}}])
    assert go(api, browser_cls=lambda s: pytest.fail("no browser for life")) == 1
    api, answers, _ = fake_api([{"id": 1, "kind": "torrent_scan", "work": {}}])
    assert go(api, minutes=0) == 0 and answers == []


def test_lane_without_chrome_still_does_life(monkeypatch):
    class NoChrome:
        def __init__(self, _s):
            pass

        def __enter__(self):
            raise RuntimeError("Chrome did not start")
    api, answers, _ = fake_api([{"id": 1, "kind": "torrent_scan", "work": {}}, {"id": 2, "kind": "life", "work": {}}])
    assert go(api, browser_cls=NoChrome) == 2
    assert {a["id"]: a.get("error") for a in answers} == {1: "RuntimeError", 2: None}


def test_lane_waits_for_new_jobs_before_it_ends(monkeypatch):
    jobs = []
    api, answers, asks = fake_api(jobs)
    t0 = time.time()
    later = threading.Timer(0.1, lambda: jobs.append({"id": 7, "kind": "life", "work": {}}))
    later.start()
    assert go(api, idle_minutes=0.6 / 60, poll=0.02) == 1        # a job written while waiting starts at once
    assert time.time() - t0 >= 0.6 and answers[0]["id"] == 7


def test_lane_page_info_returns_title_and_poster(monkeypatch):
    page = ('<html><head><meta property="og:title" content="Film (1999)">'
            '<meta property="og:image" content="/img/p.jpg"></head><body></body></html>')
    monkeypatch.setattr(FakeTab, "fetch", lambda self, url: (200, url, page))
    api, answers, _ = fake_api([{"id": 7, "kind": "page_info", "work": {"url": BASE + "/film"}},
                                {"id": 8, "kind": "page_info", "work": {"url": "javascript:alert(1)"}}])
    assert go(api) == 2
    got = {a["id"]: a for a in answers}
    assert got[7]["result"]["title"] and got[7]["result"]["poster"] == BASE + "/img/p.jpg"
    assert got[8]["error"] == "ValueError" and FakeTab.open_now == 0


def test_page_info_never_returns_an_unsafe_poster():
    page = '<meta property="og:title" content="X"><meta property="og:image" content="javascript:x">'
    res = lane._page_info({"url": BASE + "/x"}, lambda u: (200, u, page))
    assert res["poster"] is None


def test_lane_title_info_runs_without_a_browser(monkeypatch):
    monkeypatch.setattr(lane.title_info, "lookup", lambda work: {"results": [{"key": "k", "found": False}]})
    api, answers, _ = fake_api([{"id": 3, "kind": "title_info", "work": {"items": [{"key": "k", "title": "X"}]}}])
    assert go(api, browser_cls=lambda s: pytest.fail("no browser for title info")) == 1
    assert answers[0]["result"] == {"results": [{"key": "k", "found": False}]}


def test_search_address_uses_the_sites_own_box_or_the_common_one():
    home = """<form action="/search" method="get"><input type="hidden" name="cat" value="0">
      <input name="term"></form><form method="post" action="/login"><input name="q"></form>"""
    assert T.search_address(home, BASE, "Dark S02") == BASE + "/search?cat=0&term=Dark+S02"
    off = '<form action="https://other.example/find"><input type="search" name="q"></form>'
    assert T.search_address(off, BASE, "Dark") == BASE + "/?s=Dark"
    assert T.matches("Dark.S02.1080p.WEB", "Dark") and not T.matches("Darkness 2019", "Dark")
    assert not T.matches("Anything", "")


def test_search_job_reads_only_the_search_results_for_the_wanted_titles(monkeypatch):
    monkeypatch.setattr(T, "_check_life", lambda finds, stats: None)
    data, _h = make_torrent([("Dark/Dark.S02E01.mkv", 700 * 1048576)], "dark_s02")

    def card(slug, title):
        return f'<div class="it"><a href="/details/{slug}"><img src="/p/{slug}.jpg"></a><h3><a href="/details/{slug}">{title}</a></h3></div>'
    results = "<main>" + "".join(card(s, t) for s, t in [
        ("dark2", "Dark S02 Complete 1080p"), ("darkness", "Darkness 2019"), ("dark-xxx", "Dark XXX"),
        ("other", "Other Show S02"), ("lost", "Lost S02")]) + "</main>"
    pages = {BASE + "/robots.txt": "User-agent: *\nDisallow:\n",
             BASE + "/": '<form action="/search"><input name="q"></form>',
             BASE + "/search?q=Dark+S02": results,
             BASE + "/details/dark2": '<a href="/download/dark2.torrent">TORRENT</a>',
             BASE + "/rss": "<rss></rss>"}
    fetch, get_bytes, asked = fake_site(pages, {BASE + "/download/dark2.torrent": data})
    job = {"base_url": BASE, "feeds": [BASE + "/rss"], "queries": [{"q": "Dark S02", "match": "Dark"}, {"q": ""}, "bad"]}
    res = T.scan(job, fetch, get_bytes, sleep=lambda: None, memory=T.Memory(lambda urls: []))
    assert [f["title"] for f in res["finds"]] == ["Dark S02 Complete 1080p"]
    assert BASE + "/rss" not in asked and BASE + "/details/darkness" not in asked   # no feeds, no unwanted pages
    assert res["stats"]["adult"] == 1


def test_a_single_search_result_is_still_read():
    html = '<nav><a href="/">Home</a></nav><p><a href="/details/dark2">Dark S02 1080p</a></p>'
    hits = T._search_hits(html, BASE + "/?s=Dark", BASE, None)
    assert {"title": "Dark S02 1080p", "url": BASE + "/details/dark2"} in hits
