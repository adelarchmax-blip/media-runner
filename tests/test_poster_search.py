"""The cover search: IMDb first, then Google Images, then Bing; a check page is left alone; bad addresses never pass."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sites import poster_search as P  # noqa: E402


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


class Http:
    def __init__(self, hits):
        self.hits, self.asked = hits, []

    def get(self, url, **kw):
        self.asked.append(url)
        return Resp({"d": self.hits})


GOOGLE_PAGE = ('<script>var a=[["https://encrypted-tbn0.gstatic.com/x.jpg",200,300],'
               '["https://img.example.org/wide.jpg",500,900],["https://img.example.org/tall.jpg?w\\u003d1",900,600]];</script>')
BING_PAGE = '<a class="iusc" m="{&quot;murl&quot;:&quot;https://pics.example.net/cover.jpg&quot;}">x</a>'


def item(key="movie|film a|2020", title="Film A", year=2020, kind="movie"):
    return {"key": key, "title": title, "year": year, "type": kind}


def test_imdb_answer_wins_and_no_search_engine_is_opened():
    http = Http([{"l": "Film A", "y": 2020, "q": "feature", "i": {"imageUrl": "https://m.media-amazon.com/a.jpg"}}])
    pages = []
    out = P.search({"items": [item()]}, lambda u: pages.append(u), http)
    assert out == {"results": [{"key": "movie|film a|2020", "poster": "https://m.media-amazon.com/a.jpg"}]}
    assert pages == []


def test_imdb_wrong_kind_year_or_name_is_not_taken():
    http = Http([{"l": "Film A", "y": 2020, "q": "TV series", "i": {"imageUrl": "https://m.media-amazon.com/s.jpg"}},
                 {"l": "Film A", "y": 1990, "q": "feature", "i": {"imageUrl": "https://m.media-amazon.com/o.jpg"}},
                 {"l": "Other Thing", "y": 2020, "q": "feature", "i": {"imageUrl": "https://m.media-amazon.com/x.jpg"}}])
    assert P.imdb("Film A", 2020, "movie", http) is None


def test_google_tall_picture_wins_and_google_hosts_are_skipped():
    out = P.search({"items": [item()]}, lambda u: (200, u, GOOGLE_PAGE), Http([]))
    assert out["results"][0]["poster"] == "https://img.example.org/tall.jpg?w=1"


def test_google_check_page_is_left_alone_and_bing_answers():
    asked = []

    def fetch(url):
        asked.append(url.split("/")[2])
        if "google" in url:
            return 200, "https://www.google.com/sorry/index?continue=x", "<form>captcha</form>"
        return 200, url, BING_PAGE
    out = P.search({"items": [item(), item(key="series|b", title="Show B", year=None, kind="series")]}, fetch, Http([]))
    assert [r["poster"] for r in out["results"]] == ["https://pics.example.net/cover.jpg"] * 2
    assert asked == ["www.google.com", "www.bing.com", "www.bing.com"]       # جوجل اتساب بعد صفحة التحقق


def test_nothing_found_bad_items_and_bad_addresses():
    page = '[["https://x.example/a\\"onerror.jpg",900,600]] m="{&quot;murl&quot;:&quot;http://plain.example/a.jpg&quot;}"'
    out = P.search({"items": [item(), {"key": "k"}, "junk", {"key": 5, "title": "x"}]}, lambda u: (200, u, page), Http([]))
    assert out == {"results": [{"key": "movie|film a|2020", "poster": None}]}
    assert P._clean("https://a.example/x y.jpg") is None and P._clean("javascript:alert(1)") is None


def test_engine_error_goes_on_to_the_next():
    def fetch(url):
        if "google" in url:
            raise TimeoutError()
        return 200, url, BING_PAGE
    assert P.search({"items": [item()]}, fetch, Http([]))["results"][0]["poster"] == "https://pics.example.net/cover.jpg"
    json.dumps(P.search({"items": []}, fetch, Http([])))
