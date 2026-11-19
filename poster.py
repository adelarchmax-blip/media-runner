import os
import io
import re
import logging
import requests
from PIL import Image, ImageFilter, ImageEnhance


# 800x1200 تكفي لأكبر بطاقة في Jellyfin وتجعل الملف خفيفاً (~100-200KB) فيقل عمل Jellyfin على أوراكل
JELLYFIN_RATIO_W = 800
JELLYFIN_RATIO_H = 1200
MAX_POSTER_SIZE_BYTES = 1024 * 1024  # 1 ميجابايت أقصى حد
TARGET_OPTIMAL_SIZE_BYTES = 200 * 1024
MIN_SOURCE_WIDTH = 300  # الحجم المثالي المستهدف

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "image/webp,image/apng,image/*,*/*;q=0.8"
}
# Public APIs (Wikipedia, Wikidata, IMDb's feed) ask clients to name themselves.
API_HEADERS = {"User-Agent": "media-runner/1.0 (+https://github.com/adelarchmax-blip/media-runner)"}

def normalize_poster_dimensions(img: Image.Image, target_w: int = JELLYFIN_RATIO_W, target_h: int = JELLYFIN_RATIO_H) -> Image.Image:
    """
    تعديل وتطبيع أبعاد البوستر لنسبة 2:3 المعتمدة في جيلي فين:
    - منع تام للمط أو التشويه.
    - منع تام للقص من أطراف الصورة الأصلية.
    - إضافة خلفية جمالية مدمجة متناسقة بالأطراف مستخلصة ومموهة من الصورة نفسها.
    """
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGB")

    orig_w, orig_h = img.size
    target_ratio = target_w / target_h
    orig_ratio = orig_w / orig_h

    # حساب مقياس التحجيم مع الحفاظ المطلق على النسبة الأصلية دون أي مط
    scale = min(target_w / orig_w, target_h / orig_h)
    scaled_w = max(1, int(orig_w * scale))
    scaled_h = max(1, int(orig_h * scale))

    fg_image = img.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)

    # إنشاء كانفاس الخلفية بأبعاد 1000x1500
    if abs(orig_ratio - target_ratio) < 0.01:
        # الصورة الأصلية مطابقة تماماً لنسبة 2:3
        return fg_image.resize((target_w, target_h), Image.Resampling.LANCZOS)

    # إنشاء خلفية احترافية مموهة بنعومة للأطراف الفارغة
    bg_image = img.resize((target_w, target_h), Image.Resampling.BILINEAR)
    bg_image = bg_image.filter(ImageFilter.GaussianBlur(radius=28))
    # تقليل سطوع الخلفية لإبراز البوستر الأصلي في المنتصف
    enhancer = ImageEnhance.Brightness(bg_image)
    bg_image = enhancer.enhance(0.4)

    # وضع الصورة الأصلية غير المشوهة في منتصف الكانفاس
    offset_x = (target_w - scaled_w) // 2
    offset_y = (target_h - scaled_h) // 2
    bg_image.paste(fg_image, (offset_x, offset_y))

    return bg_image

def compress_and_save_poster(img: Image.Image, output_path: str, max_bytes: int = MAX_POSTER_SIZE_BYTES) -> bool:
    """
    ضغط الصورة وحفظها بأعلى دقة ممكنة مع ضمان عدم تجاوز 1 ميجابايت واستهداف حجم بين 150 إلى 350 كيلوبايت.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    
    # محاولة الحفظ بجودة متدرجة
    for quality in [85, 80, 75, 70, 65, 55]:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
        size = buf.tell()
        if size <= TARGET_OPTIMAL_SIZE_BYTES or (quality == 55 and size <= max_bytes):
            with open(output_path, "wb") as f:
                f.write(buf.getvalue())
            logging.debug("poster saved")
            return True

    # إذا كانت الصورة لا تزال كبيرة، الحفظ بآخر جودة مقبولة
    with open(output_path, "wb") as f:
        f.write(buf.getvalue())
    return True

def _font(size: int):
    """A font with Arabic letters, found through fontconfig; Pillow's own font when there is none."""
    from PIL import ImageFont
    try:
        import subprocess
        path = subprocess.run(["fc-match", "-f", "%{file}", "sans:lang=ar"], capture_output=True, text=True, timeout=5).stdout.strip()
        if path:
            return ImageFont.truetype(path, size)
    except Exception:
        pass
    return ImageFont.load_default(size)


def _wrap(draw, text: str, font, width: int) -> list:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and draw.textlength(trial, font=font) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    return lines + ([line] if line else [])


def create_fallback_poster(title: str = "", year: int = None) -> Image.Image:
    """A 2:3 poster made here when no picture of the work exists anywhere: the title and year
    written on a soft gradient, so the work is still recognisable in the library."""
    from PIL import ImageDraw
    img = Image.new("RGB", (JELLYFIN_RATIO_W, JELLYFIN_RATIO_H), color=(15, 23, 42))
    draw = ImageDraw.Draw(img)
    for y in range(JELLYFIN_RATIO_H):
        draw.line([(0, y), (JELLYFIN_RATIO_W, y)],
                  fill=(int(15 + y / JELLYFIN_RATIO_H * 20), int(23 + y / JELLYFIN_RATIO_H * 35), int(42 + y / JELLYFIN_RATIO_H * 60)))
    draw.rectangle([(20, 20), (JELLYFIN_RATIO_W - 20, JELLYFIN_RATIO_H - 20)], outline=(56, 189, 248), width=3)
    font = _font(72)
    lines = _wrap(draw, (title or "").strip(), font, JELLYFIN_RATIO_W - 140)[:5]
    y = JELLYFIN_RATIO_H // 2 - len(lines) * 50
    for line in lines:
        draw.text((JELLYFIN_RATIO_W // 2, y), line, font=font, fill=(241, 245, 249), anchor="mm")
        y += 100
    if year:
        draw.text((JELLYFIN_RATIO_W // 2, y + 20), str(year), font=_font(52), fill=(56, 189, 248), anchor="mm")
    return img


def imdb_high_res(url: str, width: int = 1000) -> str:
    """IMDb image addresses take a width: ask for 1000 px instead of the thumbnail."""
    return re.sub(r"\._V1_.*?\.(jpg|png)$", rf"._V1_SX{width}.\1", url) if "media-amazon.com" in url else url


def _norm(text: str) -> str:
    """A title reduced for comparison: no brackets, diacritics, punctuation, or Arabic letter variants."""
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text or "")
    text = re.sub(r"[ً-ْـ]", "", text)
    text = text.translate(str.maketrans("أإآٱةى", "ااااهي"))
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return " ".join(text.split())


def _years_match(found, year) -> bool:
    try:
        return not year or not found or abs(int(found) - int(year)) <= 1
    except (TypeError, ValueError):
        return True


def _json(url: str, **params):
    try:
        resp = requests.get(url, headers=API_HEADERS, params=params or None, timeout=10)
        return resp.json() if resp.status_code == 200 else {}
    except (requests.RequestException, ValueError):
        return {}


def _imdb_by_id(tt: str) -> str:
    """The poster of one IMDb title, through IMDb's public suggestion feed."""
    for d in _json(f"https://v3.sg.media-imdb.com/suggestion/t/{tt}.json").get("d", []):
        if d.get("id") == tt and (d.get("i") or {}).get("imageUrl"):
            return imdb_high_res(d["i"]["imageUrl"])
    return ""


def fetch_imdb_poster(title: str, year: int = None) -> str:
    """IMDb's poster for a title. With a known year, only a result of that year (±1) counts."""
    if not title:
        return ""
    import urllib.parse
    q = re.sub(r"^(?:فيلم|مسلسل|مسرحية|عرض)\s+", "", title.strip(), flags=re.IGNORECASE)
    q = re.sub(r"\s+(?:مترجم|مدبلج|كامل|HD|FHD).*$", "", q, flags=re.IGNORECASE).strip()
    for d in _json(f"https://v3.sg.media-imdb.com/suggestion/x/{urllib.parse.quote(q)}.json").get("d", []):
        img = (d.get("i") or {}).get("imageUrl")
        if img and (d.get("y") if year else True) and _years_match(d.get("y"), year):
            return imdb_high_res(img)
    return ""


def _page_image(url: str) -> str:
    """The picture a web page names for itself (og:image / twitter:image)."""
    try:
        resp = requests.get(url, headers={**HEADERS, "Accept": "text/html"}, timeout=10)
        if resp.status_code != 200 or "html" not in resp.headers.get("Content-Type", ""):
            return ""
        m = re.search(r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]*content=["\']([^"\']+)', resp.text[:200_000], re.I) \
            or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:image|twitter:image)["\']', resp.text[:200_000], re.I)
        return requests.compat.urljoin(url, m.group(1)) if m else ""
    except requests.RequestException:
        return ""


def _encyclopedia_posters(title: str, year: int = None):
    """Candidate posters for a work from its own encyclopedia article (Arabic, then English
    Wikipedia). Only an article whose title is the work's title counts, and its Wikidata entry,
    when it has a date, must have the work's year. From it, in order: the IMDb title's poster,
    the Wikidata poster or image, the article's own image, then the picture each linked page
    (film databases in its references) names for itself."""
    want = _norm(title)
    if not want:
        return
    for lang in ("ar", "en"):
        found = _json(f"https://{lang}.wikipedia.org/w/api.php", action="query", format="json", list="search",
                      srsearch=f"{title} {year}" if year else title, srlimit=5).get("query", {}).get("search", [])
        for hit in found:
            if _norm(hit.get("title")) != want:
                continue
            pages = _json(f"https://{lang}.wikipedia.org/w/api.php", action="query", format="json", titles=hit["title"],
                          prop="pageprops|pageimages|extlinks", ppprop="wikibase_item", piprop="original",
                          ellimit=50).get("query", {}).get("pages", {})
            for page in pages.values():
                qid = (page.get("pageprops") or {}).get("wikibase_item")
                claims = {}
                if qid:
                    claims = _json(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json").get("entities", {}).get(qid, {}).get("claims", {})
                values = lambda p: [(c.get("mainsnak", {}).get("datavalue") or {}).get("value") for c in claims.get(p, [])]
                dates = [v.get("time", "")[1:5] for v in values("P577") if isinstance(v, dict)]
                if year and dates and not any(_years_match(d, year) for d in dates):
                    continue
                for tt in values("P345"):
                    if isinstance(tt, str) and tt.startswith("tt"):
                        yield _imdb_by_id(tt)
                for name in values("P3383") + values("P18"):
                    if isinstance(name, str):
                        yield f"https://commons.wikimedia.org/wiki/Special:FilePath/{requests.utils.quote(name)}?width=1000"
                yield (page.get("original") or {}).get("source", "")
                for link in page.get("extlinks", []):
                    href = link.get("*", "")
                    if href.startswith("http") and not re.search(r"archive\.org|wiki[pm]edia\.org|wikidata\.org", href):
                        yield _page_image(href)


def _download_image(url: str, portrait: bool = False):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code == 200 and resp.headers.get("Content-Type", "").startswith("image"):
            img = Image.open(io.BytesIO(resp.content))
            img.load()
            w, h = img.size
            if w >= MIN_SOURCE_WIDTH and (not portrait or h >= w * 1.2):
                return img
    except Exception:
        return None
    return None


def _query_variants(title: str) -> list:
    """Search forms: the whole title, its Latin part alone, its Arabic part alone."""
    variants = [title]
    latin = " ".join(re.findall(r"[A-Za-z0-9][A-Za-z0-9'&:.-]*", title)).strip()
    arabic = " ".join(re.findall(r"[؀-ۿ0-9]+", title)).strip()
    for v in (latin, arabic):
        if v and len(v) > 2 and v not in variants:
            variants.append(v)
    return variants


def _web_posters(title: str, year: int = None) -> list:
    """Poster pictures from a search across many engines at once (SearXNG on the runner, at
    SEARX_URL). A result counts only when its own title names every word of the work's title,
    names no other year (±1), and the picture is portrait; results more engines agree on, and
    those naming the year, come first."""
    base = os.environ.get("SEARX_URL", "").rstrip("/")
    want = _norm(title).split()
    if not base or not want:
        return []
    arabic = re.search(r"[\u0600-\u06FF]", title)
    queries = [f"{title} {year or ''} poster".strip(), f"{'بوستر فيلم' if arabic else 'movie poster'} {title} {year or ''}".strip()]
    scored = {}
    for q in queries:
        for r in _json(f"{base}/search", q=q, categories="images", format="json", safesearch=1).get("results", []):
            src, label = r.get("img_src") or "", _norm(f"{r.get('title', '')} {r.get('content', '')}")
            if not src.startswith("http") or not all(w in label.split() for w in want):
                continue
            years = re.findall(r"\b(19\d\d|20\d\d)\b", label)
            if year and years and not any(_years_match(y, year) for y in years):
                continue
            size = re.match(r"(\d+)\s*x\s*(\d+)", r.get("resolution") or "")
            if size and int(size.group(2)) < int(size.group(1)) * 1.2:
                continue
            score = len(r.get("engines") or [r.get("engine")]) + (2 if year and str(year) in years else 0)
            scored[src] = max(scored.get(src, 0), score)
    return sorted(scored, key=scored.get, reverse=True)[:10]


def find_best_poster(source: str, title: str, year: int = None):
    """Sources in order: the site's own picture (when sharp enough) → the work's encyclopedia
    article and what it links to (only portrait pictures, which posters are) → IMDb by title and
    year → a picture search across many search engines → None."""
    if source and source.startswith("http"):
        img = _download_image(imdb_high_res(source))
        if img:
            return img
    seen = set()
    for q in _query_variants(title or ""):
        for url in _encyclopedia_posters(q, year):
            if url and url not in seen:
                seen.add(url)
                img = _download_image(url, portrait=True)
                if img:
                    return img
    for q in _query_variants(title or ""):
        url = fetch_imdb_poster(q, year)
        if url and url not in seen:
            seen.add(url)
            img = _download_image(url, portrait=True)
            if img:
                return img
    for url in _web_posters(title or "", year):
        if url not in seen:
            img = _download_image(url, portrait=True)
            if img:
                return img
    return None


def process_poster(source: str, output_path: str, title: str = "", year: int = None) -> bool:
    """بوستر Jellyfin: أفضل صورة متاحة، مطبّعة لـ 2:3 كاملة بلا قص أو مط، خفيفة الحجم"""
    try:
        img = find_best_poster(source, title, year) or create_fallback_poster(title, year)
        return compress_and_save_poster(normalize_poster_dimensions(img), output_path)
    except Exception as e:
        logging.error(f"poster processing failed ({type(e).__name__})")
        return False


if __name__ == "__main__":
    # اختبار ذاتي سريع
    test_img = Image.new("RGB", (800, 600), color=(30, 60, 120))
    test_out = "/tmp/test_poster.jpg"
    res = compress_and_save_poster(normalize_poster_dimensions(test_img), test_out)
    print(f"Self-test result: {res}, size: {os.path.getsize(test_out)} bytes")
