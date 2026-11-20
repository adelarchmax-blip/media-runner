"""المستكشف العام: بيتعرف على معمارية أي موقع لوحده، من غير قواعد خاصة بموقع معين.

الخطوات (Super_Plan، الجزء 2️⃣):
1. `robots.txt`: الممنوع، وروابط خريطة الموقع (`sitemap`).
2. خريطة الموقع: أنواع الصفحات اللي فيها (مواد، أقسام، وسوم...).
3. قوائم التنقل في الصفحة الرئيسية وفي أول صفحات الأقسام: أسماء الأقسام وروابطها، وتصنيف كل قسم
   (أفلام، مسلسلات، مسرحيات، وثائقي، أحدث، أخرى) وشيل اللي مش فيديو (دخول، اتصل بنا، سياسة...).
4. صفحة القسم: الكارت هو أكبر مجموعة عناصر إخوات ليها نفس البصمة (نفس الوسم ونفس الكلاسات)،
   وكل واحد فيها رابط مادة جوه الموقع وصورة أو عنوان. القوائم مالهاش صور ولا عناوين، فمابتتحسبش.
5. التقليب: `rel=next`، وبعده زرار "التالي"، وبعده نمط رقم الصفحة في الروابط (`/page/2/`، `?page=2`).
6. صفحة مادة من كل قسم: لازم يبان فيها اسم وعلامة مشاهدة أو مشغّل.

النتيجة "خريطة الموقع" (`profile`) بتتحفظ في قاعدة البيانات، والفهرسة بتقرا بيها القوائم بسرعة.
"""
import json
import logging
import random
import re
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from sites.adult_filter import is_adult

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ar,en-US;q=0.7,en;q=0.3",
}
MAX_BYTES = 3_000_000          # الحاويه 512MB: صفحة أكبر من كده بتتقص
MIN_CARDS = 6                  # صفحة القائمة فيها عنصر بيتكرر 6 مرات أو أكتر
MAX_SECTIONS = 40              # أقصى عدد أقسام في الخريطة
MAX_FETCHES = 45               # أقصى عدد طلبات في الاستكشاف الواحد
KINDS = ("movie", "series", "play", "documentary", "latest", "other")
KIND_NAMES = {"movie": "أفلام", "series": "مسلسلات", "play": "مسرحيات", "documentary": "وثائقي",
              "latest": "أحدث الإضافات", "other": "منوعات"}

# ---------- الروابط ----------

_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|ref$|share$)", re.I)
_SKIP_SCHEMES = ("javascript:", "mailto:", "tel:", "data:", "whatsapp:", "intent:", "sms:")


def fix_url(href, base: str):
    """رابط نضيف ومطلق، أو None لو مش رابط صفحة.

    بيصلّح أخطاء الكتابة الشايعة في المواقع (`hhttps://`، `https:/x`، `//x`) وبيشيل `#` ومتغيرات التتبع.
    """
    if not isinstance(href, str):
        return None
    href = href.strip().replace("\\", "/")
    if not href or href.startswith("#") or href.lower().startswith(_SKIP_SCHEMES):
        return None
    href = re.sub(r"^h+t+p+(s?):/*", lambda m: f"http{m.group(1)}://", href, flags=re.I)
    try:
        url = urljoin(base, href)
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        port = parts.port               # رقم منفذ مش سليم (`host:abc`) بيرمي ValueError
    except ValueError:
        return None
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)])
    netloc = parts.hostname.lower() + (f":{port}" if port else "")
    return urlunsplit((parts.scheme, netloc, parts.path or "/", query, ""))


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def same_site(url: str, base: str) -> bool:
    a, b = host_of(url), host_of(base)
    return bool(a) and (a == b or a.endswith("." + b) or b.endswith("." + a))


# ---------- النصوص العربية ----------

def norm(text: str) -> str:
    """توحيد الحروف العربية والمسافات عشان المقارنة"""
    text = unquote(text or "").lower()
    text = re.sub("[أإآٱ]", "ا", text).replace("ى", "ي").replace("ة", "ه")
    text = re.sub(r"[ً-ْـ]", "", text)
    return re.sub(r"[\s_\-/]+", " ", text).strip()


# الترتيب مهم: المسرحية قبل الفيلم ("فيلم مسرحية")، والوثائقي قبل الاتنين
_KIND_WORDS = [
    ("play", ("مسرح", "plays", "play", "theater", "theatre")),
    ("documentary", ("وثائقي", "documentar", "docs")),
    ("series", ("مسلسل", "series", "حلقات", "episodes", "season", "مواسم", "tvshows", "tv shows")),
    ("movie", ("افلام", "فيلم", "movies", "movie", "films", "film", "aflam", "cinema", "سينما")),
    ("latest", ("اضيف حديثا", "احدث", "الجديد", "جديد", "recent", "latest", "newest", "new")),
    ("other", ("تلفزيون", "برامج", "منوعات", "عروض", "حفلات", "انمي", "كرتون", "رياضه", "مصارعه",
               "tv", "shows", "show", "mix", "anime", "cartoon", "program", "videos", "فيديو")),
]
_NOT_VIDEO = ("login", "signin", "sign in", "register", "signup", "sign up", "logout", "account", "profile",
              "contact", "about", "privacy", "policy", "terms", "dmca", "faq", "help", "support", "cart",
              "favorite", "favourite", "wishlist", "notification", "news", "blog", "article", "request", "upload",
              "dashboard", "admin", "wp-admin", "feed", "rss", "search", "tag", "director", "actor", "cast",
              "تسجيل", "دخول", "خروج", "حساب", "البروفايل", "بروفايل", "اتصل", "تواصل", "من نحن", "سياسه",
              "الخصوصيه", "شروط", "المفضله", "قائمتي", "اشعارات", "اخبار", "مقالات", "طلبات", "بحث", "ممثل",
              "مخرج", "نجوم", "الشروط", "اعلن", "الاعلانات")
_SOCIAL = ("facebook.", "twitter.", "x.com", "t.me", "telegram.", "instagram.", "youtube.", "tiktok.",
           "whatsapp.", "pinterest.", "linkedin.", "reddit.", "snapchat.", "discord.")


def _has_word(text: str, word: str) -> bool:
    if re.fullmatch(r"[a-z ]+", word):
        return re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", text) is not None
    return word in text


def classify_section(name: str, url: str):
    """نوع القسم من اسمه ورابطه، أو None لو مش قسم فيديو"""
    path = norm(urlsplit(url).path + " " + urlsplit(url).query)
    text = norm(name)
    both = f"{text} {path}"
    if any(_has_word(both, w) for w in _NOT_VIDEO):
        # "مسلسلات بطولة نجوم ..." قسم فيديو: الكلمة الممنوعة تتسامح بس لو اسم القسم نفسه فيه نوع صريح
        # (رابط زي `/favorite/movies` اسمه "قائمتي المفضلة" مش قسم)
        if not any(_has_word(text, w) for kind, words in _KIND_WORDS[:4] for w in words):
            return None
    for kind, words in _KIND_WORDS:
        if any(_has_word(text, w) for w in words):
            return kind
    for kind, words in _KIND_WORDS:
        if any(_has_word(path, w) for w in words):
            return kind
    return "unknown"


# ---------- robots.txt وخريطة الموقع ----------

def parse_robots(text: str) -> dict:
    """الممنوع على كل البرامج (`User-agent: *`) وروابط خريطة الموقع"""
    disallow, sitemaps, applies = [], [], False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (p.strip() for p in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            applies = value == "*"
        elif key == "disallow" and applies and value:
            disallow.append(value)
        elif key == "sitemap" and value:
            sitemaps.append(value)
    return {"disallow": disallow[:200], "sitemaps": sitemaps[:20]}


def robots_allows(robots: dict, url: str) -> bool:
    path = urlsplit(url).path or "/"
    return not any(path.startswith(rule.replace("*", "")) for rule in robots.get("disallow", []) if rule)


_SITEMAP_KINDS = [
    ("skip", ("tag", "category", "director", "actor", "cast", "author", "year", "genre", "page-sitemap",
              "misc", "archive", "taxonomy", "user", "image", "video-sitemap", "news")),
    ("episode", ("episode", "ep-",)),
    ("series", ("series", "tvshow", "show", "season")),
    ("movie", ("movie", "film")),
    ("item", ("post",)),
]


def sitemap_kind(url: str) -> str:
    name = norm(urlsplit(url).path)
    for kind, words in _SITEMAP_KINDS:
        if any(w.replace("-", " ") in name for w in words):
            return kind
    return "item"


def parse_sitemap(text: str) -> dict:
    """روابط خريطة الموقع: فهرس خرايط (`sitemapindex`) أو قائمة صفحات (`urlset`)، مع آخر تعديل"""
    soup = BeautifulSoup(text or "", "html.parser")
    entries = []
    for tag in soup.find_all(["sitemap", "url"]):
        loc = tag.find("loc")
        if loc and loc.get_text(strip=True):
            mod = tag.find("lastmod")
            entries.append({"loc": loc.get_text(strip=True), "lastmod": mod.get_text(strip=True) if mod else None})
    return {"index": soup.find("sitemapindex") is not None, "entries": entries}


# ---------- الحماية ----------

_BLOCK_MARKS = ("cf-chl", "challenge-platform", "just a moment", "checking your browser", "captcha",
                "attention required", "ddos-guard", "are you a robot", "انا لست روبوت", "verify you are human")


def looks_blocked(status: int, html: str) -> bool:
    """صفحة تحدي (Cloudflare أو CAPTCHA) بدل المحتوى: دي محتاجة كروم حقيقي على الرانر"""
    head = (html or "")[:20000].lower()
    if status in (403, 429, 503) and any(m in head for m in _BLOCK_MARKS):
        return True
    return bool(re.search(r"<title>\s*(just a moment|attention required|checking your browser)", head))


# ---------- العنوان والصورة لصفحة مادة ----------

_SEP = re.compile(r"\s+[-|–—:»]\s+")


def page_identity(soup) -> tuple:
    """(العنوان، رابط الغلاف) لصفحة مادة على أي موقع: JSON-LD ← og ← h1 ← عنوان الصفحة"""
    title = image = None
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        nodes = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for node in nodes:
            types = node.get("@type") if isinstance(node, dict) else None
            types = types if isinstance(types, list) else [types]
            if any(t in ("Movie", "TVSeries", "TVEpisode", "VideoObject", "Episode") for t in types):
                title = title or node.get("name")
                img = node.get("image") or node.get("thumbnailUrl")
                img = img[0] if isinstance(img, list) and img else img
                image = image or (img.get("url") if isinstance(img, dict) else img)

    def meta(prop):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return tag.get("content") if tag else None

    title = title or meta("og:title") or meta("twitter:title")
    image = image or meta("og:image") or meta("twitter:image")
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else (soup.title.get_text(strip=True) if soup.title else None)
    if title:
        title = strip_site_name(title.strip(), meta("og:site_name") or "")
    return title or None, image if isinstance(image, str) else None


def strip_site_name(title: str, site_name: str = "") -> str:
    """شيل اسم الموقع من آخر العنوان: "… - SiteName | اسم الموقع" ← "…"."""
    parts = _SEP.split(title)
    if len(parts) < 2:
        return title
    site = norm(site_name)
    # كل الأجزاء الأخيرة اللي جزء من اسم الموقع بتتشال
    while len(parts) > 1 and site and norm(parts[-1]) and norm(parts[-1]) in site:
        parts.pop()
    if len(parts) == len(_SEP.split(title)):
        # من غير اسم الموقع: الجزء الأخير القصير بس (زي "- Dailymotion")
        if len(parts[-1]) <= 40 and len(parts[-1].split()) <= 4:
            parts.pop()
    return " - ".join(parts).strip() or title


# ---------- الكروت ----------

_PLACEHOLDER = re.compile(r"(load|lazy|placeholder|blank|spacer|pixel|loader|default)[^/]*\.(gif|png|jpe?g|svg|webp)", re.I)
_YEAR = re.compile(r"(?<!\d)(19[2-9]\d|20[0-4]\d)(?!\d)")


def signature(tag) -> str:
    """بصمة العنصر: الوسم والكلاسات، والأرقام جوه الكلاسات بتتوحد (`entry-box-1` = `entry-box-2`)"""
    classes = sorted({re.sub(r"\d+", "#", c) for c in (tag.get("class") or [])})
    return tag.name + ("." + ".".join(classes) if classes else "")


def image_of(card, base: str):
    for img in card.find_all(["img", "source"]):
        for attr in ("data-src", "data-lazy-src", "data-original", "data-srcset", "srcset", "src", "data-bg"):
            value = img.get(attr)
            if not value:
                continue
            value = value.split(",")[0].strip().split(" ")[0]
            if value and not value.startswith("data:") and not _PLACEHOLDER.search(value):
                return fix_url(value, base)
    styled = card.find(style=re.compile(r"background(-image)?\s*:[^;]*url\(", re.I))
    if styled:
        m = re.search(r"url\(['\"]?([^'\")]+)", styled["style"])
        if m:
            return fix_url(m.group(1), base)
    return None


def card_item(card, base: str, page_url: str):
    """{url, title, image, year} من كارت، أو None لو مش كارت مادة"""
    links = [(fix_url(a.get("href"), base), a) for a in card.find_all("a", href=True)]
    if card.name == "a" and card.get("href"):
        links.insert(0, (fix_url(card.get("href"), base), card))
    links = [(u, a) for u, a in links if u and same_site(u, base) and u.rstrip("/") != page_url.rstrip("/")]
    if not links:
        return None
    url = Counter(u for u, _ in links).most_common(1)[0][0]
    heading = card.find(["h1", "h2", "h3", "h4", "h5", "h6"]) or card.find(class_=re.compile(r"title|name", re.I))
    img = card.find("img")
    candidates = [heading.get_text(" ", strip=True) if heading else "",
                  next((a.get("title", "") for u, a in links if u == url and a.get("title")), ""),
                  img.get("alt", "") if img else "",
                  max((a.get_text(" ", strip=True) for u, a in links if u == url), key=len, default="")]
    title = next((t for t in candidates if len(t.strip()) >= 2), "").strip()
    year = _YEAR.search(card.get_text(" ", strip=True))
    magnet = card.find("a", href=re.compile(r"^\s*magnet:\?", re.I))
    item = {"url": url, "title": title[:300], "image": image_of(card, base),
            "year": int(year.group(1)) if year else None}
    if magnet:
        item["magnet"] = magnet["href"].strip()[:8000]      # سطر في جدول تورنت
    return item


def _group_score(children, base, page_url):
    items = [card_item(c, base, page_url) for c in children]
    items = [i for i in items if i]
    urls = {i["url"] for i in items}
    if len(urls) < MIN_CARDS:
        return 0, []
    with_img = sum(1 for c in children if c.find("img") or c.find(style=re.compile("url\\(", re.I)))
    with_title = sum(1 for i in items if len(i["title"]) >= 2)
    with_magnet = sum(1 for i in items if i.get("magnet"))
    # القوائم: روابط من غير صور ولا عناوين، فبتاخد صفر (إلا جدول تورنت: كل سطر فيه magnet)
    if with_img < len(children) / 2 and not any(c.find(["h2", "h3", "h4", "h5"]) for c in children) \
            and with_magnet < len(items) / 2:
        return 0, []
    if with_title < len(items) / 2:
        return 0, []
    return len(urls) * (2 if with_img >= len(children) / 2 else 1), items


def find_cards(soup, page_url: str, base: str = None, min_cards: int = MIN_CARDS):
    """(البصمة، المواد) لأكبر مجموعة كروت في الصفحة، أو (None، []). صفحة البحث بتقبل أقل من MIN_CARDS"""
    base = base or page_url
    best = (0, None, [])
    for parent in soup.find_all(True):
        children = [c for c in parent.find_all(True, recursive=False)]
        if len(children) < min_cards:
            continue
        groups = {}
        for child in children:
            groups.setdefault(signature(child), []).append(child)
        for sig, members in groups.items():
            if len(members) < min_cards:
                continue
            score, items = _group_score(members, base, page_url)
            if score > best[0]:
                best = (score, {"parent": signature(parent), "card": sig}, items)
    return best[1], _unique(best[2])


def read_cards(soup, fingerprint: dict, page_url: str, base: str = None):
    """قراءة الكروت بالبصمة المحفوظة (من غير تحليل)، من كل المجموعات اللي بنفس البصمة في الصفحة"""
    base = base or page_url
    items = []
    for parent in soup.find_all(True):
        if signature(parent) != fingerprint.get("parent"):
            continue
        for child in parent.find_all(True, recursive=False):
            if signature(child) == fingerprint.get("card"):
                item = card_item(child, base, page_url)
                if item and item["title"]:
                    items.append(item)
    return _unique(items)


def _unique(items):
    seen, out = set(), []
    for item in items:
        if is_adult(item.get("title"), item.get("url")):
            continue                   # مادة إباحية: مابتتعدّش ولا بتتحفظ
        if item["url"] not in seen:
            seen.add(item["url"])
            out.append(item)
    return out


# ---------- التقليب بين الصفحات ----------

_NEXT_WORDS = ("التالي", "التالى", "next", "older")       # صريحة: بتتقبل لوحدها
_NEXT_SIGNS = ("›", "»", ">", ">>", "→", "←", "<", "‹", "«")  # اتجاهها بيختلف في العربي: لازم رقم الصفحة يأكدها
_PREV_WORDS = ("السابق", "prev", "previous", "newer", "first", "الاولى", "last", "الاخيره")
_PAGE_PATTERNS = [r"/page/(\d+)/?", r"[?&](?:page|paged|p|pg|page_number|pagenum|offset)=(\d+)", r"/(\d+)/?$"]


def page_template(url: str, number: int):
    """رابط الصفحة كقالب `{n}` لو الرقم ده هو رقم الصفحة في الرابط"""
    for pattern in _PAGE_PATTERNS:
        for m in re.finditer(pattern, url):
            if int(m.group(1)) == number:
                return url[:m.start(1)] + "{n}" + url[m.end(1):]
    return None


def find_next(soup, page_url: str, base: str = None):
    """{next, template, last_page, how} للصفحة اللي بعدها، أو None"""
    base = base or page_url
    links = [(fix_url(a.get("href"), base), a) for a in soup.find_all(["a", "link"], href=True)]
    links = [(u, a) for u, a in links if u and same_site(u, base) and u.rstrip("/") != page_url.rstrip("/")]
    current = _page_number(page_url)
    found, how = None, None
    for u, a in links:
        rel = " ".join(a.get("rel") or []).lower()
        if "next" in rel:
            found, how = u, "rel"
            break
    if not found:
        for u, a in links:
            text = norm(a.get_text(" ", strip=True) or a.get("aria-label", "") or a.get("title", ""))
            label = " ".join((a.get("class") or []) + (a.parent.get("class") or [] if a.parent else [])).lower()
            if any(w in text for w in _PREV_WORDS) or re.search(r"prev", label) or _page_number(u) < current:
                continue
            strong = any(_has_word(text, w) for w in _NEXT_WORDS) or re.search(r"(?<![a-z])next(?![a-z])", label)
            confirmed = _page_number(u) == current + 1
            if strong or (text in _NEXT_SIGNS and confirmed):
                found, how = u, "text"
                break
    if not found:
        for u, a in links:
            if norm(a.get_text(strip=True)) == str(current + 1) and page_template(u, current + 1):
                found, how = u, "number"
                break
    if not found:
        return None
    template = page_template(found, current + 1)
    last = current + 1
    if template:
        pattern = re.escape(template).replace(r"\{n\}", r"(\d+)")
        for u, _ in links:
            m = re.fullmatch(pattern, u)
            if m and int(m.group(1)) < 100000:
                last = max(last, int(m.group(1)))
    return {"next": found, "template": template, "last_page": last, "how": how}


def _page_number(url: str) -> int:
    for pattern in _PAGE_PATTERNS[:2]:
        m = re.search(pattern, url)
        if m:
            return int(m.group(1))
    return 1


# ---------- صفحة المادة ----------

_WATCH = re.compile(r"مشاهد|شاهد|تشغيل|watch|play|stream|سيرفر|server|حلقات|episodes", re.I)
_PLAYER = re.compile(r"player|embed|video|stream|vid|watch|play|\.m3u8|\.mp4", re.I)


def check_item(html: str, url: str) -> dict:
    """هل دي صفحة مادة؟ (اسم + علامة مشاهدة أو مشغّل)"""
    soup = BeautifulSoup(html or "", "html.parser")
    title, image = page_identity(soup)
    player = any((t.name == "video") or _PLAYER.search(t.get("src") or t.get("data-src") or "")
                 for t in soup.find_all(["video", "iframe"]))
    watch = any(_WATCH.search(a.get_text(" ", strip=True) + " " + " ".join(a.get("class") or []))
                for a in soup.find_all(["a", "button"]))
    return {"url": url, "title": title, "image": fix_url(image, url) if image else None,
            "player": player, "watch_link": watch, "ok": bool(title) and (player or watch)}


# ---------- طريق المشغّل (من كود الصفحة) ----------
# صفحة المادة في مواقع كتير مفيهاش مشغّل: زرار "مشاهدة" بيودّي لصفحة تانية (أحياناً بعد إعلان أو عدّاد).
# عنوان الصفحة دي مكتوب في كود صفحة المادة (لينك أو جوه سكريبت)، فبيتعرف مرة واحدة ويتحفظ كقاعدة،
# والفهرسة بتحط عنوان صفحة المشاهدة مباشرة في الطابور. ولو الموقع قافل الصفحة دي للبرامج في robots.txt،
# بيتعلّم "مقفول" وموادّه مابتدخلش الطابور.

_WATCH_SEG = re.compile(r"^(watch|play|player|stream|مشاهدة)$", re.I)
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>()\\]+")
_NOT_PLAYER = re.compile(r"\.(jpe?g|png|webp|gif|svg|avif|css|js)$", re.I)
_SERVER_ATTRS = ("data-link", "data-src", "data-url", "data-embed", "data-iframe")


def _segs(url: str) -> list:
    return [unquote(x) for x in urlsplit(url).path.split("/") if x]


def _hidden(tag) -> bool:
    for t in [tag, *tag.parents]:
        if getattr(t, "attrs", None) is None:
            continue
        style = (t.get("style") or "").replace(" ", "").lower()
        if t.get("aria-hidden") == "true" or t.has_attr("hidden") or "display:none" in style or "visibility:hidden" in style:
            return True
    return False


def watch_candidates(html: str, url: str) -> list:
    """عناوين جوه نفس الموقع في كود صفحة المادة (لينكات ظاهرة أو نصوص السكريبت) آخرها جزء مشاهدة"""
    soup = BeautifulSoup(html or "", "html.parser")
    found = []
    for a in soup.find_all("a", href=True):
        if not _hidden(a):
            found.append(fix_url(a["href"], url))
    for script in soup.find_all("script"):
        found += [fix_url(u, url) for u in _URL_IN_TEXT.findall((script.string or "").replace("\\/", "/"))]
    here = fix_url(url, url)
    out = []
    for u in found:
        if u and u != here and same_site(u, url) and any(_WATCH_SEG.match(x) for x in _segs(u)) and u not in out:
            out.append(u)
    return out


def watch_rule(item_url: str, watch_url: str):
    """القاعدة اللي بتحوّل عنوان المادة لعنوان صفحة مشاهدتها: إضافة جزء في الآخر، أو تبديل جزء"""
    a, b = _segs(item_url), _segs(watch_url)
    if len(b) == len(a) + 1 and b[:len(a)] == a and _WATCH_SEG.match(b[-1]):
        return {"append": b[-1]}
    diff = [i for i in range(min(len(a), len(b))) if a[i] != b[i]]
    if len(a) == len(b) and len(diff) == 1 and _WATCH_SEG.match(b[diff[0]]):
        return {"swap": [diff[0], a[diff[0]], b[diff[0]]]}
    return None


def apply_watch_rule(rule, item_url: str):
    """عنوان صفحة المشاهدة لمادة، أو None لو القاعدة مش ماشية عليها"""
    if not rule or not item_url:
        return None
    parts = urlsplit(item_url)
    raw = [x for x in parts.path.split("/") if x]
    if "append" in rule:
        path = "/" + "/".join(raw + [rule["append"]]) + "/"
    elif "swap" in rule:
        i, old, new = rule["swap"]
        if i >= len(raw) or unquote(raw[i]) != old:
            return None
        raw[i] = new
        path = "/" + "/".join(raw) + ("/" if parts.path.endswith("/") else "")
    else:
        return None
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def count_servers(html: str, url: str) -> int:
    """عدد سيرفرات المشاهدة في الصفحة: مشغّل ظاهر + عناوين سيرفرات من مواقع تانية في خصائص الأزرار"""
    soup = BeautifulSoup(html or "", "html.parser")
    links = set()
    for t in soup.find_all(["iframe", "video", "source"]):
        src = fix_url(t.get("src") or "", url)
        if src and not _hidden(t):
            links.add(src)
    for attr in _SERVER_ATTRS:
        for t in soup.find_all(attrs={attr: True}):
            u = fix_url(t.get(attr), url)
            if u and not same_site(u, url) and not _hidden(t) and t.name not in ("img", "iframe") and not _NOT_PLAYER.search(urlsplit(u).path):
                links.add(u)
    return len(links)


def learn_player(item_url: str, html: str, robots: dict, get) -> dict:
    """طريق المشغّل لموقع، من صفحة مادة واحدة. `get(url)` بيجيب صفحة (زي _Session.get)"""
    if check_item(html, item_url)["player"]:
        return {"kind": "on_page", "example": item_url}
    for cand in watch_candidates(html, item_url):
        rule = watch_rule(item_url, cand)
        if not rule:
            continue
        cand = apply_watch_rule(rule, item_url) or cand    # نفس العنوان اللي الطابور هيستخدمه
        if not robots_allows(robots, cand):
            return {"kind": "closed", "rule": rule, "example": cand}
        got = get(cand)
        servers = count_servers(got["html"], got["url"]) if got and got.get("html") else 0
        if servers:
            return {"kind": "watch_page", "rule": rule, "servers": servers, "example": cand}
    return {"kind": "unknown", "example": item_url}


PLAYER_NOTES = {
    "on_page": "المشغّل جوه صفحة المادة نفسها.",
    "watch_page": "المشغّل في صفحة مشاهدة منفصلة، وعنوانها اتعرف من كود الصفحة، فالرانر بيروحها على طول.",
    "closed": "الموقع قافل صفحات المشاهدة قدام البرامج في robots.txt، فموادّه مش بتدخل الطابور.",
    "unknown": "طريق المشغّل ماتعرفش من كود الصفحة، فالرانر بيدوّر عليه بنفسه.",
}


# ---------- الطلبات ----------

def http_fetch(url: str):
    """The runner fetches only through Chrome (sites.browser); every caller passes its fetch."""
    raise RuntimeError("no fetch given")


class _Session:
    """كل طلبات الاستكشاف: احترام robots، ووقت هادي بين الطلبات، وحد أقصى للعدد"""

    def __init__(self, fetch, sleep, robots=None, limit=MAX_FETCHES):
        self.fetch, self.sleep, self.robots, self.limit = fetch, sleep, robots or {}, limit
        self.count, self.blocked, self.errors = 0, 0, 0

    def get(self, url):
        if self.count >= self.limit or not robots_allows(self.robots, url):
            return None
        if self.count:
            self.sleep()
        self.count += 1
        try:
            status, final, text = self.fetch(url)
        except Exception as err:  # الشبكة: الصفحة دي بس بتتعدّى
            logging.info(f"page did not answer ({type(err).__name__})")    # no address in public logs
            self.errors += 1
            return None
        if looks_blocked(status, text):
            self.blocked += 1
            return {"status": status, "url": final, "html": "", "blocked": True}
        if status >= 400:
            self.errors += 1
            return None
        return {"status": status, "url": final or url, "html": text, "blocked": False}


def polite_sleep():
    time.sleep(random.uniform(2, 4))


# ---------- الاستكشاف ----------

def nav_links(soup, page_url: str, base: str, card_urls=frozenset(), skip=None):
    """روابط الأقسام: كل رابط جوه الموقع عليه اسم، ومش رابط مادة ولا رابط صفحة رقم.
    الأقسام الإباحية مابتدخلش الخريطة خالص، و`skip(name, url)` بيشيل أقسام زيادة (زي اللي مش فيديو)."""
    out = []
    seen = set()
    for a in soup.find_all("a", href=True):
        url = fix_url(a.get("href"), base)
        name = re.sub(r"\s+", " ", a.get_text(" ", strip=True) or a.get("title", "")).strip()
        if not url or not same_site(url, base) or url in card_urls or url in seen:
            continue
        if any(s in url.lower() for s in _SOCIAL) or not name or len(name) > 60:
            continue
        if urlsplit(url).path.strip("/") == "" and not urlsplit(url).query:
            continue  # الصفحة الرئيسية نفسها
        if page_template(url, _page_number(url)) and _page_number(url) > 1:
            continue
        in_menu = any(p.name in ("nav", "header", "footer") or re.search(r"menu|nav|categor|section|dropdown|sub",
                      " ".join(p.get("class") or []) + " " + (p.get("id") or ""), re.I) for p in a.parents)
        kind = classify_section(name, url)
        if kind is None or is_adult(name, url) or (skip and skip(name, url)):
            continue
        seen.add(url)
        out.append({"name": name, "url": url, "kind": kind, "in_menu": in_menu})
    return out


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def explore_site(base_url: str, fetch=http_fetch, sleep=polite_sleep, max_sections: int = MAX_SECTIONS,
                 limit: int = MAX_FETCHES, progress=None, skip=None) -> dict:
    """خريطة موقع كاملة من رابطه الرئيسي"""
    say = progress or (lambda msg: None)          # public runner logs carry no site names
    base = fix_url(base_url, base_url)
    if not base:
        raise ValueError("رابط الموقع مش صحيح")
    root = f"{urlsplit(base).scheme}://{urlsplit(base).netloc}/"
    profile = {"base_url": root.rstrip("/"), "explored_at": _now(), "robots": {"disallow": [], "sitemaps": []},
               "sitemaps": [], "sections": [], "needs_browser": False, "notes": []}
    session = _Session(fetch, sleep, limit=limit)

    say("بقرا robots.txt")
    got = session.get(urljoin(root, "/robots.txt"))
    if got and got["html"] and "<html" not in got["html"][:500].lower():
        profile["robots"] = parse_robots(got["html"])
    session.robots = profile["robots"]

    say("بفتح الصفحة الرئيسية")
    home = session.get(root)
    if not home:
        profile["requests"] = session.count
        profile["notes"].append("الموقع مابيردش خالص: يمكن واقع أو الدومين اتغيّر. جرّب تاني بعدين أو عدّل رابطه.")
        return profile
    if home.get("blocked"):
        profile["requests"] = session.count
        profile["needs_browser"] = True
        profile["notes"].append("الموقع عليه حماية (زي Cloudflare): الاستكشاف محتاج كروم حقيقي على الرانر.")
        return profile
    if not same_site(home["url"], root):
        root = f"{urlsplit(home['url']).scheme}://{urlsplit(home['url']).netloc}/"
        profile["base_url"] = root.rstrip("/")
        profile["notes"].append(f"الموقع بيحوّل لعنوان جديد: {profile['base_url']}")
    base = profile["base_url"]

    # خريطة الموقع: الأنواع اللي فيها بس، والفهرسة بتستخدمها بعدين
    sitemap_urls = profile["robots"]["sitemaps"] or [urljoin(root, "/sitemap.xml")]
    for sm_url in sitemap_urls[:2]:
        got = session.get(sm_url)
        if not got or not got["html"].lstrip().startswith("<"):
            continue
        parsed = parse_sitemap(got["html"])
        if not parsed["entries"]:
            continue                    # صفحة HTML اسمها sitemap، مش خريطة
        kinds = Counter(sitemap_kind(e["loc"]) for e in parsed["entries"]) if parsed["index"] else {"item": len(parsed["entries"])}
        profile["sitemaps"].append({"url": sm_url, "index": parsed["index"], "count": len(parsed["entries"]),
                                    "kinds": dict(kinds),
                                    "item_maps": [e["loc"] for e in parsed["entries"]
                                                  if parsed["index"] and sitemap_kind(e["loc"]) != "skip"][:200]})

    home_soup = BeautifulSoup(home["html"], "html.parser")
    home_fp, home_items = find_cards(home_soup, home["url"], base)
    candidates = nav_links(home_soup, home["url"], base, {i["url"] for i in home_items}, skip)
    if skip:
        profile["home_html"] = home["html"][:500000]      # موقع التورنت بيدوّر فيها على الموجز (RSS)
    if home_fp:
        profile["sections"].append(_section("الصفحة الرئيسية", home["url"], "latest", home_fp, home_items,
                                            find_next(home_soup, home["url"], base)))

    # الأقسام: اللي في القوائم الأول، وبالنوع (أفلام ومسلسلات قبل المنوعات)
    order = {k: i for i, k in enumerate(("movie", "series", "play", "documentary", "latest", "other", "unknown"))}
    queue = sorted(candidates, key=lambda c: (not c["in_menu"], order[c["kind"]]))
    known = {c["url"] for c in queue}
    checked_items = 0
    while queue and len(profile["sections"]) < max_sections and session.count < session.limit:
        cand = queue.pop(0)
        say(f"بفحص قسم: {cand['name']}")
        got = session.get(cand["url"])
        if not got:
            continue
        if got.get("blocked"):
            profile["needs_browser"] = True
            continue
        soup = BeautifulSoup(got["html"], "html.parser")
        fp, items = find_cards(soup, got["url"], base)
        # صفحات الأقسام فيها قوايم أقسام فرعية مش ظاهرة في الرئيسية
        for extra in nav_links(soup, got["url"], base, {i["url"] for i in items}, skip):
            if extra["url"] not in known and extra["in_menu"] and extra["kind"] != "unknown":
                known.add(extra["url"])
                queue.append(extra)
        if not fp:
            continue
        section = _section(cand["name"], got["url"], cand["kind"], fp, items, find_next(soup, got["url"], base))
        if _duplicate(section, profile["sections"]):
            continue
        if section["kind"] == "unknown":
            section["kind"] = _kind_from_items(items)
        if checked_items < 3 and items:
            sample = session.get(items[0]["url"])
            if sample and not sample.get("blocked"):
                section["item_check"] = check_item(sample["html"], sample["url"])
                checked_items += 1
                if "player" not in profile:
                    profile["player"] = learn_player(sample["url"], sample["html"], profile["robots"], session.get)
        profile["sections"].append(section)

    if session.blocked and not profile["sections"]:
        profile["needs_browser"] = True
    if profile["needs_browser"]:
        profile["notes"].append("بعض الصفحات عليها حماية: الأقسام دي محتاجة كروم حقيقي على الرانر.")
    if not profile["sections"]:
        profile["notes"].append("مالقيتش صفحات قوائم فيها كروت مواد: يمكن الموقع بيحمّل محتواه بالجافاسكريبت.")
    profile["requests"] = session.count
    say(f"خلص: {len(profile['sections'])} قسم في {session.count} طلب")
    return profile


def _section(name, url, kind, fp, items, pagination):
    return {"name": name[:80], "url": url, "kind": kind, "fingerprint": fp, "pagination": pagination,
            "items_on_page": len(items), "sample": items[:5], "enabled": False, "next_url": None,
            "full_done": False}


def _duplicate(section, sections) -> bool:
    """نفس المواد في رابطين (`/movies` و`/category/movies/`): بيتسجل مرة واحدة"""
    urls = {i["url"] for i in section["sample"]}
    return any(urls and urls == {i["url"] for i in s["sample"]} for s in sections)


def _kind_from_items(items) -> str:
    words = Counter()
    for item in items:
        kind = classify_section(item["title"], "")
        if kind in ("movie", "series", "play", "documentary"):
            words[kind] += 1
    return words.most_common(1)[0][0] if words else "other"


# ---------- الفهرسة بالخريطة ----------

def index_section(section: dict, base: str, is_known, fetch=http_fetch, sleep=polite_sleep,
                  max_pages: int = 3, stop_after_known: int = 5):
    """مواد قسم من الأحدث للأقدم، صفحة صفحة عن طريق رابط "التالي" في كل صفحة.

    - الفهرسة الأولى (`full_done` لسه False): بتكمّل من `next_url` اللي وقفت عنده المرة اللي فاتت،
      لحد `max_pages` صفحات في المرة.
    - بعد ما تخلص: الفهرسة اليومية بتبدأ من أول صفحة وبتقف بعد `stop_after_known` مواد معروفة ورا بعض.
    بيرجع (المواد الجديدة، حالة القسم الجديدة).
    """
    session = _Session(fetch, sleep, limit=max_pages)
    full_done = bool(section.get("full_done"))
    state = {"next_url": section.get("next_url"), "full_done": full_done}
    url = section["url"] if full_done or not state["next_url"] else state["next_url"]
    fresh, streak = [], 0
    for _ in range(max_pages):
        got = session.get(url)
        if not got or got.get("blocked"):
            break                       # الشبكة أو الحماية: نكمّل من نفس الصفحة المرة الجاية
        soup = BeautifulSoup(got["html"], "html.parser")
        items = read_cards(soup, section["fingerprint"], got["url"], base)
        nxt = find_next(soup, got["url"], base) if items else None
        if hasattr(is_known, "prime"):          # a memory that asks for a whole page at once
            is_known.prime([i["url"] for i in items])
        for item in items:
            if is_known(item["url"]):
                streak += 1
                if full_done and streak >= stop_after_known:
                    return fresh, state
            else:
                streak = 0
                fresh.append(item)
        if not nxt:
            if not full_done:
                state.update(full_done=True, next_url=None)
            return fresh, state
        url = nxt["next"]
        if not full_done:
            state["next_url"] = url
    return fresh, state
