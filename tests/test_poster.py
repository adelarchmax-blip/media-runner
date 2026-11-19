"""Offline checks of the poster engine (no network)."""
import os
import sys

from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import poster  # noqa: E402


def test_titles_compare_without_letter_variants_or_brackets():
    assert poster._norm("أحمر شفايف (فلم)") == poster._norm("احمر شفايف")
    assert poster._norm("مدرسة المشاغبين") == poster._norm("مدرسه المشاغبين")
    assert poster._norm("عَلى الهَوا") == poster._norm("علي الهوا")
    assert poster._norm("The Godfather (film)") == "the godfather"
    assert poster._norm("") == ""


def test_years_match_within_one():
    assert poster._years_match("1946", 1946)
    assert poster._years_match("1947", 1946)
    assert not poster._years_match("1990", 1946)
    assert poster._years_match(None, 1946)
    assert poster._years_match("1990", None)
    assert poster._years_match("x", 1946)


def test_every_shape_becomes_full_2_by_3(tmp_path):
    out = tmp_path / "p.jpg"
    for size in [(800, 600), (300, 1200), (1000, 1500), (1, 1)]:
        assert poster.compress_and_save_poster(poster.normalize_poster_dimensions(Image.new("RGB", size)), str(out))
        with Image.open(out) as img:
            assert img.size == (poster.JELLYFIN_RATIO_W, poster.JELLYFIN_RATIO_H)
        assert out.stat().st_size <= poster.MAX_POSTER_SIZE_BYTES


def test_fallback_poster_carries_the_title():
    blank = poster.create_fallback_poster("", None)
    titled = poster.create_fallback_poster("احمر شفايف", 1946)
    assert titled.size == (poster.JELLYFIN_RATIO_W, poster.JELLYFIN_RATIO_H)
    assert titled.tobytes() != blank.tobytes()
    long = poster.create_fallback_poster("كلمة " * 60, 2001)
    assert long.size == blank.size


def test_no_title_no_lookup():
    assert list(poster._encyclopedia_posters("", 1946)) == []
    assert poster.fetch_imdb_poster("", 1946) == ""


def test_web_search_keeps_only_matching_portrait_posters(monkeypatch):
    results = [
        {"img_src": "https://a/right.jpg", "title": "بوستر فيلم أحمر شفايف 1946", "resolution": "600x900", "engines": ["bing", "google"]},
        {"img_src": "https://a/one-engine.jpg", "title": "احمر شفايف", "resolution": "600x900", "engines": ["duckduckgo"]},
        {"img_src": "https://a/wrong-year.jpg", "title": "احمر شفايف 1990", "resolution": "600x900", "engines": ["bing"]},
        {"img_src": "https://a/landscape.jpg", "title": "احمر شفايف 1946", "resolution": "1280x720", "engines": ["bing"]},
        {"img_src": "https://a/other-film.jpg", "title": "شفايف 1946", "resolution": "600x900", "engines": ["bing"]},
        {"img_src": "not-a-link", "title": "احمر شفايف 1946", "engines": ["bing"]},
    ]
    asked = []
    monkeypatch.setenv("SEARX_URL", "http://search.local/")
    monkeypatch.setattr(poster, "_json", lambda url, **p: asked.append((url, p)) or {"results": results})
    found = poster._web_posters("احمر شفايف", 1946)
    assert found == ["https://a/right.jpg", "https://a/one-engine.jpg"]
    assert asked[0][0] == "http://search.local/search" and asked[0][1]["categories"] == "images"


def test_web_search_off_without_its_address(monkeypatch):
    monkeypatch.delenv("SEARX_URL", raising=False)
    monkeypatch.setattr(poster, "_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    assert poster._web_posters("احمر شفايف", 1946) == []
    monkeypatch.setenv("SEARX_URL", "http://search.local")
    assert poster._web_posters("", 1946) == []


def test_web_search_survives_a_dead_search_service(monkeypatch):
    monkeypatch.setenv("SEARX_URL", "http://127.0.0.1:9")
    assert poster._web_posters("احمر شفايف", 1946) == []


def test_people_read_names_and_photos(monkeypatch):
    claims = {"P161": [{"mainsnak": {"datavalue": {"value": {"id": f"Q{i}"}}}} for i in (1, 2, 3)]}
    entities = {"Q1": {"labels": {"ar": {"value": "شادية"}, "en": {"value": "Shadia"}},
                       "claims": {"P18": [{"mainsnak": {"datavalue": {"value": "Shadia 1961.jpg"}}}]}},
                "Q2": {"labels": {"en": {"value": "Only English"}}},
                "Q3": {"labels": {}}}
    monkeypatch.setattr(poster, "_json", lambda url, **p: {"entities": entities})
    people = poster._people(claims, "P161", 10)
    assert people[0]["name"] == "شادية" and people[0]["photo"].startswith("https://commons.wikimedia.org/")
    assert people[1] == {"name": "Only English", "photo": ""}
    assert len(people) == 2
    assert poster._people({}, "P161", 10) == []


def test_details_empty_without_an_article(monkeypatch):
    monkeypatch.setattr(poster, "_json", lambda *a, **k: {})
    poster._articles.cache_clear()
    assert poster.work_details("عمل مالوش مقالة", 2001) == {"summary": "", "director": [], "cast": [], "link": ""}


def test_summary_cut_at_a_sentence():
    text = "جملة أولى طويلة شوية. " * 60
    short = poster._short(text, 300)
    assert len(short) <= 300 and short.endswith(".")
    assert poster._short("قصير") == "قصير"


def test_thumb_is_small_jpeg(tmp_path):
    out = tmp_path / "p.jpg"
    poster.compress_and_save_poster(poster.create_fallback_poster("اسم", 2000), str(out))
    thumb = poster.poster_thumb(str(out))
    assert thumb.startswith("data:image/jpeg;base64,") and len(thumb) < 80_000


def test_title_score_is_only_a_measure():
    assert poster._title_score("احمر شفايف", "أحمر شفايف (فلم)") == 1.0
    assert poster._title_score("اسماعيل ياسين فى متحف الشمع", "إسماعيل يس في متحف الشمع (فلم)") == 0.8
    assert poster._title_score("الب ارسلان", "الب ارسلان 2") == 2 / 3
    assert poster._title_score("", "x") == 0.0


def test_ai_answer_is_checked(monkeypatch):
    cands = [{"title": "a", "year": 1955, "about": ""}, {"title": "b", "year": None, "about": ""}]
    for answer, expected in (({"match": 1}, 1), ({"match": 0}, 0), ({"match": None}, None), ({"match": 2}, None),
                             ({"match": -1}, None), ({"match": "0"}, None), ({"match": True}, None),
                             (None, None), ("text", None), ([1], None)):
        monkeypatch.setattr(poster, "ask_ai", lambda task, a=answer: a)
        assert poster.ai_same_work("t", None, cands) == expected
    assert poster.ai_same_work("t", None, []) is None


def _fake_wiki(monkeypatch, hits, claims):
    def fake(url, **p):
        if p.get("list") == "search":
            return {"query": {"search": [{"title": h} for h in hits] if url.startswith("https://ar.") else []}}
        if "EntityData" in url:
            qid = url.rsplit("/", 1)[1][:-5]
            return {"entities": {qid: {"claims": claims.get(qid, {})}}}
        if p.get("titles"):
            return {"query": {"pages": {"1": {"title": p["titles"], "pageprops": {"wikibase_item": "Q" + str(hits.index(p["titles"]))}}}}}
        return {}
    monkeypatch.setattr(poster, "_json", fake)
    poster._articles.cache_clear()


WORK = {"P57": [{"mainsnak": {"datavalue": {"value": {"id": "Q9"}}}}]}


def test_articles_exact_near_and_person(monkeypatch):
    asked = []
    monkeypatch.setattr(poster, "ask_ai", lambda task: asked.append(task) or {"match": 0})
    # Exact title of a work: taken without asking.
    _fake_wiki(monkeypatch, ["أحمر شفايف (فلم)"], {"Q0": WORK})
    assert [a[1]["title"] for a in poster._articles("احمر شفايف", None)] == ["أحمر شفايف (فلم)"] and not asked
    # A person's article with the exact name is not a work: refused.
    _fake_wiki(monkeypatch, ["إسماعيل ياسين"], {})
    assert poster._articles("اسماعيل ياسين", None) == ()
    # Another spelling: the AI judge decides.
    _fake_wiki(monkeypatch, ["إسماعيل ياسين", "إسماعيل يس في متحف الشمع (فلم)"], {"Q1": WORK})
    assert [a[1]["title"] for a in poster._articles("اسماعيل ياسين فى متحف الشمع", None)] == ["إسماعيل يس في متحف الشمع (فلم)"]
    assert [c["title"] for c in asked[-1]["candidates"]] == ["إسماعيل يس في متحف الشمع (فلم)"]
    # The judge says no, or can't be reached: nothing.
    monkeypatch.setattr(poster, "ask_ai", lambda task: {"match": None})
    poster._articles.cache_clear()
    assert poster._articles("اسماعيل ياسين فى متحف الشمع", None) == ()
    monkeypatch.setattr(poster, "ask_ai", lambda task: None)
    poster._articles.cache_clear()
    assert poster._articles("اسماعيل ياسين فى متحف الشمع", None) == ()


def test_ai_not_asked_without_gateway(monkeypatch):
    monkeypatch.delenv("RUNNER_API_URL", raising=False)
    assert poster.ask_ai({"task": "same_work"}) is None


def test_cast_from_the_article_opening(monkeypatch):
    lead = ("{{بطاقة فيلم\n| صورة = x.jpg\n}}\n'''فيلم''' مصري إنتاج [[1955]] بطولة [[إسماعيل ياسين|إسماعيل يس]] "
            "و[[عبد الفتاح القصري]] و[[ملف:x.jpg]] و[[كيتي]].<ref>[[ليس ممثل]]</ref>")

    def fake(url, **p):
        if p.get("action") == "parse":
            return {"parse": {"wikitext": {"*": lead}}}
        return {"query": {"pages": {"1": {"title": "إسماعيل ياسين", "thumbnail": {"source": "https://upload.wikimedia.org/a.jpg"}},
                                    "2": {"title": "عبد الفتاح القصري"}, "3": {"title": "كيتي"}}}}
    monkeypatch.setattr(poster, "_json", fake)
    assert poster._lead_people("ar", "x", 10) == [
        {"name": "إسماعيل يس", "photo": "https://upload.wikimedia.org/a.jpg"},
        {"name": "عبد الفتاح القصري", "photo": ""}, {"name": "كيتي", "photo": ""}]
    monkeypatch.setattr(poster, "_json", lambda *a, **k: {})
    assert poster._lead_people("ar", "x", 10) == []
