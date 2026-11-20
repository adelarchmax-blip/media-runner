"""Watch sites on the runner: the address check, the map and the daily crawl with a fake site. No network."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from sites import watch as W  # noqa: E402
from sites.torrent_scan import Memory  # noqa: E402
from test_sites import FakeChrome, fake_api, go  # noqa: E402

OLD, NEW = "https://watch.example", "https://watch2.example"
CARDS = "".join(f"<div class='card'><a href='/film/{i}'><img src='/p/{i}.jpg'><h3>Film number {i} 2020</h3></a></div>"
                for i in range(8))
HOME = f"<html><body><nav><a href='/movies'>أفلام</a></nav><div class='grid'>{CARDS}</div></body></html>"
ITEM = "<html><body><h1>Film</h1><a href='/film/0/watch'>مشاهدة</a></body></html>"
WATCH = "<html><body>" + "".join(f"<li data-link='https://player{i}.example/e/1'>سيرفر {i}</li>" for i in range(3)) + \
        "</body></html>"


def site(base: str, moved_from: str = None, extra: dict = None):
    pages = {base + "/robots.txt": "", base + "/": HOME, base: HOME, base + "/movies": HOME,
             base + "/film/0": ITEM, base + "/film/0/watch": WATCH}
    pages.update(extra or {})
    asked = []

    def fetch(url):
        asked.append(url)
        if moved_from and url.startswith(moved_from):
            return 200, base + url[len(moved_from):], pages.get(base + url[len(moved_from):], HOME)
        return (200, url, pages[url]) if url in pages else (404, url, "")
    return fetch, asked


def test_address_same_moved_found_by_search_or_down():
    fetch, _ = site(OLD)
    assert W.address({"base_url": OLD + "/"}, fetch) == {"base_url": OLD, "moved": False, "down": False}
    fetch, _ = site(NEW, moved_from=OLD)
    assert W.address({"base_url": OLD}, fetch) == {"base_url": NEW, "moved": True, "down": False}
    results = "<a class='result__url'>t.me/x</a><a class='result__url'>watch2.example/home</a>"
    fetch, asked = site(NEW, extra={W.SEARCH + "new%20address": results})
    assert W.address({"base_url": OLD, "find_query": "new address"}, fetch)["base_url"] == NEW
    assert not any("t.me" in u for u in asked)                       # social pages are never opened
    fetch, _ = site(NEW)
    assert W.address({"base_url": OLD}, fetch) == {"base_url": OLD, "moved": False, "down": True}
    with pytest.raises(ValueError):
        W.address({"base_url": "javascript:x"}, fetch)


def test_explore_keeps_choices_after_a_move_and_new_sections_start_off():
    fetch, _ = site(OLD)
    first = W.explore({"base_url": OLD}, fetch, sleep=lambda: None)["profile"]
    assert first["sections"] and all(s["enabled"] is False for s in first["sections"])
    assert "home_html" not in first
    first["sections"][0].update(enabled=True, next_url=OLD + "/movies?page=2")
    first["player"] = {"kind": "on_page"}
    fetch, _ = site(NEW, moved_from=OLD)
    got = W.explore({"base_url": OLD, "profile": first}, fetch, sleep=lambda: None)
    assert got["moved"] is True and got["base_url"] == NEW
    again = {s["url"]: s for s in got["profile"]["sections"]}
    moved = first["sections"][0]["url"].replace(OLD, NEW)
    assert again[moved]["enabled"] is True and again[moved]["next_url"] == NEW + "/movies?page=2"
    assert got["profile"]["player"] == {"kind": "on_page"}
    fetch, _ = site(NEW)
    with pytest.raises(ConnectionError):
        W.explore({"base_url": OLD}, fetch, sleep=lambda: None)


def test_crawl_sends_each_section_with_items_place_and_player_and_skips_known():
    fetch, _ = site(OLD)
    profile = W.explore({"base_url": OLD}, fetch, sleep=lambda: None)["profile"]
    profile["sections"][0]["enabled"] = True
    profile["sections"].append(dict(profile["sections"][0], url=OLD + "/off", enabled=False))
    asked, parts = [], []

    def ask(urls):
        asked.append(list(urls))
        return [OLD + "/film/7"]
    memory = Memory(ask)
    res = W.crawl({"base_url": OLD, "profile": profile}, fetch, memory, parts.append, sleep=lambda: None)
    assert res == {"base_url": OLD, "moved": False, "stats": {"sections": 1, "new": 7}}
    assert len(parts) == 1 and parts[0]["section"] == OLD + "/"
    urls = [i["url"] for i in parts[0]["items"]]
    assert OLD + "/film/7" not in urls and urls[0] == OLD + "/film/0"
    assert parts[0]["items"][0]["image"] == OLD + "/p/0.jpg" and parts[0]["state"]["full_done"] is True
    assert parts[0]["player"]["kind"] in ("watch_page", "on_page", "unknown")
    assert memory(OLD + "/film/0")                                    # handled now: not sent twice


def test_crawl_after_a_move_and_a_closed_site_stops():
    fetch, _ = site(OLD)
    profile = W.explore({"base_url": OLD}, fetch, sleep=lambda: None)["profile"]
    for s in profile["sections"]:
        s["enabled"] = True
    profile["player"] = {"kind": "closed"}
    fetch, _ = site(NEW, moved_from=OLD)
    parts = []
    res = W.crawl({"base_url": OLD, "profile": profile}, fetch, Memory(), parts.append, sleep=lambda: None)
    assert res["moved"] is True and res["base_url"] == NEW
    assert len(parts) == 1 and parts[0]["section"].startswith(NEW) and "player" not in parts[0]
    nothing = W.crawl({"base_url": OLD, "profile": {"sections": [{"url": OLD, "enabled": False}]}},
                      site(OLD)[0], Memory(), parts.append, sleep=lambda: None)
    assert nothing["stats"] == {"sections": 0, "new": 0}


def test_lane_runs_watch_jobs_in_chrome(monkeypatch):
    monkeypatch.setattr(W, "explore", lambda work, fetch: {"profile": {"sections": []}, "base_url": OLD, "moved": False})

    def crawl(work, fetch, memory, send_part):
        send_part({"section": OLD, "items": [], "state": {}})
        return {"stats": {"sections": 1, "new": 0}}
    monkeypatch.setattr(W, "crawl", crawl)
    api, answers, _ = fake_api([{"id": 1, "kind": "watch_explore", "work": {}},
                                {"id": 2, "kind": "watch_crawl", "work": {}}])
    assert go(api) == 2 and FakeChrome.opened == 0
    done = {a["id"]: a for a in answers if a["op"] == "done"}
    assert done[1]["result"]["base_url"] == OLD and done[2]["result"]["stats"]["sections"] == 1
    assert any(a["op"] == "part" and a["id"] == 2 for a in answers)
