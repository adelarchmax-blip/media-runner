"""What a title is, from free public databases (no key needed): posters, a series' seasons and episodes,
and the film series a movie belongs to.

  series -> TVmaze      (show, poster, episodes per season)
  movie  -> Wikidata    (film, poster file on Wikimedia Commons, the film series and its parts in order)

The controller sends titles only; answers carry what the databases said, never a site address.
"""
import concurrent.futures
import logging
import re
import urllib.parse

import requests

TVMAZE = "https://api.tvmaze.com"
WIKIDATA = "https://www.wikidata.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"
COMMONS = "https://commons.wikimedia.org/wiki/Special:FilePath/"
HEADERS = {"User-Agent": "media-runner/1.0 (personal media library)", "Accept": "application/json"}
TIMEOUT = 15
WORKERS = 4
MAX_ITEMS = 60
MAX_PARTS = 40
FILM_TYPES = {"Q11424", "Q24869", "Q202866", "Q506240", "Q229390", "Q1259759", "Q17517379", "Q24862"}
_QID = re.compile(r"^Q\d{1,12}$")
_ARABIC = re.compile(r"[؀-ۿ]")


def _get(url: str, params: dict, http=requests):
    r = http.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _year(text) -> int:
    m = re.match(r"^\+?(\d{4})", str(text or ""))
    return int(m.group(1)) if m else None


def _near(a, b) -> bool:
    return a is None or b is None or abs(a - b) <= 1


def _https(url):
    return url if isinstance(url, str) and url.startswith("https://") else None


# ---------- series: TVmaze ----------

def series(title: str, year: int = None, http=requests) -> dict:
    hits = _get(f"{TVMAZE}/search/shows", {"q": title}, http) or []
    shows = [h.get("show") for h in hits if isinstance(h, dict) and isinstance(h.get("show"), dict)]
    show = next((s for s in shows if year and _year(s.get("premiered")) == year), None) or \
        next((s for s in shows if _near(_year(s.get("premiered")), year)), None)
    if not show or not isinstance(show.get("id"), int):
        return {}
    episodes = _get(f"{TVMAZE}/shows/{show['id']}/episodes", {}, http) or []
    seasons = {}
    for e in episodes:
        if isinstance(e, dict) and isinstance(e.get("season"), int) and isinstance(e.get("number"), int) \
                and 0 < e["season"] < 200 and 0 < e["number"] < 2000:
            seasons[str(e["season"])] = max(seasons.get(str(e["season"]), 0), e["number"])
    image = show.get("image") if isinstance(show.get("image"), dict) else {}
    return {"kind": "series", "title": str(show.get("name") or "")[:200], "year": _year(show.get("premiered")),
            "poster": _https(image.get("original") or image.get("medium")), "seasons": seasons, "source": "TVmaze"}


# ---------- movie: Wikidata ----------

def _claims(entity: dict, prop: str) -> list:
    out = []
    for c in (entity.get("claims") or {}).get(prop) or []:
        v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(v, dict) and "id" in v:
            out.append(v["id"])
        elif isinstance(v, dict) and "time" in v:
            out.append(v["time"])
        elif isinstance(v, str):
            out.append(v)
    return out


def _label(entity: dict) -> str:
    labels = entity.get("labels") or {}
    for lang in ("ar", "en"):
        if isinstance(labels.get(lang), dict):
            return str(labels[lang].get("value") or "")[:200]
    return ""


def _poster(entity: dict):
    files = _claims(entity, "P3383") or _claims(entity, "P18")
    name = files[0] if files and isinstance(files[0], str) else ""
    if not name or "/" in name:
        return None
    return COMMONS + urllib.parse.quote(name.replace(" ", "_")) + "?width=500"


def _parts(series_id: str, http=requests) -> list:
    query = f"""SELECT ?f ?fLabel (MIN(?d) AS ?date) (SAMPLE(?o) AS ?ord) WHERE {{
      ?f p:P179 ?st . ?st ps:P179 wd:{series_id} . OPTIONAL {{ ?st pq:P1545 ?o }} OPTIONAL {{ ?f wdt:P577 ?d }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "ar,en" }} }} GROUP BY ?f ?fLabel LIMIT {MAX_PARTS}"""
    data = _get(SPARQL, {"query": query, "format": "json"}, http) or {}
    parts = []
    for b in (data.get("results") or {}).get("bindings") or []:
        title = str((b.get("fLabel") or {}).get("value") or "")[:200]
        order = str((b.get("ord") or {}).get("value") or "")
        if title and not _QID.match(title):
            parts.append({"title": title, "year": _year((b.get("date") or {}).get("value")),
                          "order": int(order) if order.isdigit() else None})
    parts.sort(key=lambda p: (p["order"] is None, p["order"] or 0, p["year"] or 9999, p["title"]))
    return parts


def movie(title: str, year: int = None, http=requests) -> dict:
    lang = "ar" if _ARABIC.search(title) else "en"
    found = _get(WIKIDATA, {"action": "wbsearchentities", "search": title, "language": lang, "uselang": lang,
                            "type": "item", "limit": 7, "format": "json"}, http) or {}
    ids = [s["id"] for s in found.get("search") or [] if isinstance(s, dict) and _QID.match(str(s.get("id")))]
    if not ids:
        return {}
    data = _get(WIKIDATA, {"action": "wbgetentities", "ids": "|".join(ids), "props": "claims|labels",
                           "languages": "ar|en", "format": "json"}, http) or {}
    entities = data.get("entities") or {}
    film = next((entities[i] for i in ids if isinstance(entities.get(i), dict)
                 and FILM_TYPES & set(_claims(entities[i], "P31"))
                 and _near(min((_year(d) for d in _claims(entities[i], "P577") if _year(d)), default=None), year)),
                None)
    if not film:
        return {}
    out = {"kind": "movie", "title": _label(film) or title[:200],
           "year": min((_year(d) for d in _claims(film, "P577") if _year(d)), default=None),
           "poster": _poster(film), "source": "Wikidata"}
    series_ids = [s for s in _claims(film, "P179") if _QID.match(str(s))]
    if series_ids:
        names = _get(WIKIDATA, {"action": "wbgetentities", "ids": series_ids[0], "props": "labels",
                                "languages": "ar|en", "format": "json"}, http) or {}
        parts = _parts(series_ids[0], http)
        if len(parts) > 1:
            out["collection"] = {"name": _label((names.get("entities") or {}).get(series_ids[0]) or {}), "parts": parts}
    return out


def lookup(work: dict, http=requests) -> dict:
    """{items: [{key, title, year, type}]} -> {results: [{key, found, ...}]}"""
    items = [i for i in (work.get("items") or []) if isinstance(i, dict) and isinstance(i.get("key"), str)
             and isinstance(i.get("title"), str) and i["title"].strip()][:MAX_ITEMS]

    def one(item):
        year = item.get("year") if isinstance(item.get("year"), int) else None
        try:
            got = (series if item.get("type") == "series" else movie)(item["title"].strip()[:200], year, http)
        except Exception as e:  # one title's failure must not lose the rest
            logging.info(f"title info: {type(e).__name__}")
            return {"key": item["key"], "found": False, "error": True}
        return dict(got, key=item["key"], found=bool(got))

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        return {"results": list(ex.map(one, items))}
