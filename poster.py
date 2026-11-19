import os
import io
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

def create_fallback_poster(title: str = "Media") -> Image.Image:
    """توليد غلاف سينمائي بديل أنيق بنسبة 2:3 بدقة 1000x1500 عند تعذر سحب البوستر الأصلي"""
    img = Image.new("RGB", (JELLYFIN_RATIO_W, JELLYFIN_RATIO_H), color=(15, 23, 42))
    # تدرج لوني سينمائي ناعم
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    for y in range(JELLYFIN_RATIO_H):
        r = int(15 + (y / JELLYFIN_RATIO_H) * 20)
        g = int(23 + (y / JELLYFIN_RATIO_H) * 35)
        b = int(42 + (y / JELLYFIN_RATIO_H) * 60)
        draw.line([(0, y), (JELLYFIN_RATIO_W, y)], fill=(r, g, b))
    # إطار جمالي خارجي
    draw.rectangle([(20, 20), (JELLYFIN_RATIO_W - 20, JELLYFIN_RATIO_H - 20)], outline=(56, 189, 248), width=3)
    return img

import re

def imdb_high_res(url: str, width: int = 1000) -> str:
    """روابط IMDb تقبل تحديد الدقة: نطلب عرض 1000 بكسل بدلاً من الصورة المصغرة"""
    return re.sub(r"\._V1_.*?\.(jpg|png)$", rf"._V1_SX{width}.\1", url) if "media-amazon.com" in url else url

def fetch_imdb_poster(title: str, year: int = None) -> str:
    """البحث التلقائي عن بوستر الفيلم أو المسلسل الرسمي عبر قاعدة بيانات IMDb عند عدم توفره في الموقع المصدر"""
    if not title:
        return ""
    try:
        import urllib.parse
        clean_q = title.strip()
        # إزالة الكلمات الزائدة لتحسين دقة البحث
        clean_q = re.sub(r"^(?:فيلم|مسلسل|مسرحية|عرض)\s+", "", clean_q, flags=re.IGNORECASE).strip()
        clean_q = re.sub(r"\s+(?:مترجم|مدبلج|كامل|HD|FHD).*$", "", clean_q, flags=re.IGNORECASE).strip()
        query_encoded = urllib.parse.quote(clean_q)
        url = f"https://v3.sg.media-imdb.com/suggestion/x/{query_encoded}.json"
        
        resp = requests.get(url, headers=HEADERS, timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("d", [])
            for res in results:
                img_obj = res.get("i")
                if img_obj and img_obj.get("imageUrl"):
                    item_year = res.get("y")
                    if year and item_year and abs(int(item_year) - int(year)) > 2:
                        continue
                    poster_url = imdb_high_res(img_obj["imageUrl"])
                    logging.debug("imdb poster found")
                    return poster_url
            for res in results:
                img_obj = res.get("i")
                if img_obj and img_obj.get("imageUrl"):
                    poster_url = imdb_high_res(img_obj["imageUrl"])
                    logging.debug("imdb poster found")
                    return poster_url
    except Exception as e:
        logging.debug("imdb lookup failed")
    return ""

def _download_image(url: str):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code == 200 and resp.headers.get("Content-Type", "").startswith("image"):
            img = Image.open(io.BytesIO(resp.content))
            img.load()
            return img if img.size[0] >= MIN_SOURCE_WIDTH else None
    except Exception:
        return None
    return None


def _query_variants(title: str) -> list:
    """صيغ بحث متعددة: الاسم كاملاً، الجزء اللاتيني فقط، الجزء العربي فقط"""
    variants = [title]
    latin = " ".join(re.findall(r"[A-Za-z0-9][A-Za-z0-9'&:.-]*", title)).strip()
    arabic = " ".join(re.findall(r"[\u0600-\u06FF0-9]+", title)).strip()
    for v in (latin, arabic):
        if v and len(v) > 2 and v not in variants:
            variants.append(v)
    return variants


def _wikipedia_image(query: str) -> str:
    """صورة العمل من ويكيبيديا (عربي ثم إنجليزي) كمصدر بديل مجاني بلا مفاتيح"""
    for lang in ("ar", "en"):
        try:
            r = requests.get(f"https://{lang}.wikipedia.org/w/api.php", headers=HEADERS, timeout=8, params={
                "action": "query", "format": "json", "generator": "search", "gsrsearch": query, "gsrlimit": 1,
                "prop": "pageimages", "piprop": "original"})
            pages = (r.json().get("query") or {}).get("pages") or {}
            for p in pages.values():
                src = (p.get("original") or {}).get("source")
                if src:
                    return src
        except Exception:
            continue
    return ""


def find_best_poster(source: str, title: str, year: int = None):
    """ترتيب المصادر: صورة الموقع (إن كانت بدقة كافية) ← IMDb بعدة صيغ ← ويكيبيديا ← None"""
    if source and source.startswith("http"):
        img = _download_image(imdb_high_res(source))
        if img:
            return img
    for q in _query_variants(title or ""):
        url = fetch_imdb_poster(q, year)
        if url:
            img = _download_image(url)
            if img:
                return img
    for q in _query_variants(title or ""):
        url = _wikipedia_image(f"{q} {year}" if year else q)
        if url:
            img = _download_image(url)
            if img:
                return img
    return None


def process_poster(source: str, output_path: str, title: str = "", year: int = None) -> bool:
    """بوستر Jellyfin: أفضل صورة متاحة، مطبّعة لـ 2:3 كاملة بلا قص أو مط، خفيفة الحجم"""
    try:
        img = find_best_poster(source, title, year) or create_fallback_poster(title)
        return compress_and_save_poster(normalize_poster_dimensions(img), output_path)
    except Exception as e:
        logging.error(f"poster processing failed ({type(e).__name__})")
        return False


def _legacy_process_poster(source: str, output_path: str, title: str = "", year: int = None) -> bool:
    """
    معالجة شاملة للبوستر من رابط ويب أو مسار محلي أو البحث عنه في IMDb وحفظه كـ poster.jpg مطابق لجيلي فين بنسبة 2:3 وحجم < 1 ميجابايت.
    """
    try:
        img = None
        target_source = source

        # إذا كان الرابط غير متوفر أو افتراضي: البحث الفوري في IMDb
        if not target_source or target_source == "/logo.png" or not target_source.startswith("http"):
            if title:
                logging.debug("imdb lookup")
                imdb_url = fetch_imdb_poster(title, year)
                if imdb_url:
                    target_source = imdb_url

        if target_source and (target_source.startswith("http://") or target_source.startswith("https://")):
            try:
                resp = requests.get(target_source, headers=HEADERS, timeout=12)
                if resp.status_code == 200:
                    img_data = io.BytesIO(resp.content)
                    img = Image.open(img_data)
                else:
                    logging.warning(f"⚠️ تعذر جلب البوستر (كود {resp.status_code})، محاولة البحث البديل في IMDb...")
                    if title and target_source != source:
                        imdb_url = fetch_imdb_poster(title, year)
                        if imdb_url:
                            resp_imdb = requests.get(imdb_url, headers=HEADERS, timeout=12)
                            if resp_imdb.status_code == 200:
                                img = Image.open(io.BytesIO(resp_imdb.content))
                    if not img:
                        img = create_fallback_poster(title)
            except Exception as net_err:
                logging.warning(f"poster download failed ({type(net_err).__name__}); using fallback")
                img = create_fallback_poster(title)
        elif target_source and os.path.exists(target_source):
            img = Image.open(target_source)
        else:
            img = create_fallback_poster(title)

        normalized = normalize_poster_dimensions(img)
        return compress_and_save_poster(normalized, output_path)

    except Exception as e:
        logging.error(f"poster processing failed ({type(e).__name__})")
        return False

if __name__ == "__main__":
    # اختبار ذاتي سريع
    test_img = Image.new("RGB", (800, 600), color=(30, 60, 120))
    test_out = "/tmp/test_poster.jpg"
    res = compress_and_save_poster(normalize_poster_dimensions(test_img), test_out)
    print(f"Self-test result: {res}, size: {os.path.getsize(test_out)} bytes")
