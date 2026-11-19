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
