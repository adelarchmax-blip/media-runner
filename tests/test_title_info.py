import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sites import title_info as TI  # noqa: E402


class Resp:
    def __init__(self, data, code=200):
        self.data, self.status_code = data, code

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeHttp:
    """Answers like the public databases do; records what was asked."""
    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def get(self, url, params=None, headers=None, timeout=None):
        assert headers["User-Agent"] and timeout
        self.asked.append((url, dict(params or {})))
        for test, data in self.answers:
            if test(url, params or {}):
                return Resp(data) if data is not None else Resp(None, 404)
        return Resp(None, 404)


def claim_id(q):
    return {"mainsnak": {"datavalue": {"value": {"id": q}}}}


def claim(v):
    return {"mainsnak": {"datavalue": {"value": v}}}


SHOWS = [{"score": 0.9, "show": {"id": 1, "name": "Old Show", "premiered": "1990-01-01"}},
         {"score": 0.8, "show": {"id": 2, "name": "The Show", "premiered": "2008-01-20",
                                 "image": {"original": "https://static.tvmaze.com/p.jpg"}}}]
EPISODES = [{"season": 1, "number": n} for n in range(1, 8)] + [{"season": 2, "number": n} for n in range(1, 14)] + \
    [{"season": "x", "number": 1}, {"season": 0, "number": 1}]


def test_series_picks_the_right_year_and_counts_episodes():
    http = FakeHttp([(lambda u, p: u.endswith("/search/shows"), SHOWS),
                     (lambda u, p: u.endswith("/shows/2/episodes"), EPISODES)])
    got = TI.series("The Show", 2008, http)
    assert got == {"kind": "series", "title": "The Show", "year": 2008, "poster": "https://static.tvmaze.com/p.jpg",
                   "seasons": {"1": 7, "2": 13}, "source": "TVmaze"}
    assert TI.series("Nothing", None, FakeHttp([])) == {}


FILM = {"labels": {"en": {"value": "Film Two"}},
        "claims": {"P31": [claim_id("Q11424")], "P577": [claim({"time": "+2004-05-01T00:00:00Z"})],
                   "P3383": [claim("Film Two poster.jpg")], "P179": [claim_id("Q900")]}}
NOT_FILM = {"labels": {"en": {"value": "Film Two (novel)"}}, "claims": {"P31": [claim_id("Q7725634")]}}
PARTS = {"results": {"bindings": [
    {"fLabel": {"value": "Film Three"}, "date": {"value": "2007-01-01T00:00:00Z"}, "ord": {"value": "3"}},
    {"fLabel": {"value": "Film One"}, "date": {"value": "2001-01-01T00:00:00Z"}, "ord": {"value": "1"}},
    {"fLabel": {"value": "Q123"}},
    {"fLabel": {"value": "Film Two"}, "date": {"value": "2004-01-01T00:00:00Z"}}]}}


def wiki_http():
    return FakeHttp([
        (lambda u, p: p.get("action") == "wbsearchentities", {"search": [{"id": "Q1"}, {"id": "Q2"}, {"id": "bad id"}]}),
        (lambda u, p: p.get("action") == "wbgetentities" and p["ids"] == "Q1|Q2", {"entities": {"Q1": NOT_FILM, "Q2": FILM}}),
        (lambda u, p: p.get("action") == "wbgetentities" and p["ids"] == "Q900",
         {"entities": {"Q900": {"labels": {"ar": {"value": "سلسلة الفيلم"}}}}}),
        (lambda u, p: u == TI.SPARQL, PARTS)])


def test_movie_finds_poster_and_its_film_series_in_order():
    http = wiki_http()
    got = TI.movie("Film Two", 2004, http)
    assert got["title"] == "Film Two" and got["year"] == 2004 and got["source"] == "Wikidata"
    assert got["poster"] == TI.COMMONS + "Film_Two_poster.jpg?width=500"
    assert got["collection"]["name"] == "سلسلة الفيلم"
    assert [p["title"] for p in got["collection"]["parts"]] == ["Film One", "Film Three", "Film Two"]
    sparql = next(p["query"] for u, p in http.asked if u == TI.SPARQL)
    assert "wd:Q900" in sparql
    assert TI.movie("Film Two", 1980, wiki_http()) == {}          # the year does not match
    asked = FakeHttp([])
    TI.movie("فيلم عربي", None, asked)
    assert asked.asked[0][1]["language"] == "ar"


def test_lookup_keeps_going_when_one_title_fails():
    def boom(*a):
        raise RuntimeError("down")
    http = FakeHttp([(lambda u, p: u.endswith("/search/shows"), SHOWS),
                     (lambda u, p: u.endswith("/shows/2/episodes"), EPISODES)])
    items = [{"key": "s1", "title": "The Show", "year": 2008, "type": "series"},
             {"key": "m1", "title": "Missing", "year": None, "type": "movie"},
             {"key": "bad", "title": ""}, {"title": "no key"}]
    res = TI.lookup({"items": items}, http)["results"]
    assert [(r["key"], r["found"]) for r in res] == [("s1", True), ("m1", False)]
    orig = TI.series
    TI.series = boom
    try:
        res = TI.lookup({"items": items[:1]}, http)["results"]
    finally:
        TI.series = orig
    assert res == [{"key": "s1", "found": False, "error": True}]
