"""فلتر المحتوى الإباحي: أقسام المواقع وأسماء المواد.

القاعدة عامة لأي موقع: كلمات صريحة بالعربي والإنجليزي، بتتقارن ككلمة كاملة (عشان "Sussex" مايتمسكش
بسبب "sex")، في اسم القسم أو رابطه، أو في اسم المادة ووصفها وتصنيفها. اللي بيتمسك مابيتحفظش خالص.
"""
import re

# كلمات صريحة بس: أي كلمة منها لوحدها كفاية
_EN = ("porn", "porno", "pornographic", "xxx", "hentai", "erotic", "erotica", "erotik", "nsfw", "nude", "nudes",
       "nudity", "naked", "playboy", "penthouse", "onlyfans", "brazzers", "bangbros", "milf", "camgirl", "camgirls",
       "fetish", "bdsm", "hardcore sex", "softcore", "striptease", "stripper", "escort", "xvideos", "xnxx",
       "pornhub", "youporn", "redtube", "xhamster", "sex tape", "sextape", "adult movies", "adult videos",
       "adult video", "adult film", "adult films", "adult content", "adult only", "adults only", "for adults",
       "18+", "+18", "x-rated", "x rated", "jav", "gonzo", "orgy", "blowjob", "anal", "cumshot", "creampie",
       "threesome", "lesbian sex", "gay sex", "sex", "sexy", "porntube", "uncensored", "ecchi", "doujin")
# كلمات عربي: ككلمة كاملة برضه (عشان "عريس" و"نيكول" و"الجنسيه" مايتمسكوش)، وممكن قبلها ال/و/ب/لل
_AR = ("اباحي", "اباحيه", "اباحيات", "سكس", "سكسي", "للكبار فقط", "جنسي", "جنسيا", "عري", "عاري", "عاريه",
       "عاريات", "نيك", "شرموطه", "متناك", "قحبه", "مقاطع ساخنه", "افلام ساخنه", "مشاهد ساخنه", "بورن",
       "بورنو", "سحاق", "لواط")
_PATH = re.compile(r"(?:^|[/_\-.=?&])(xxx|porn\w*|adult|hentai|erotic\w*|nsfw|sex\w*|18plus|18\+)(?:$|[/_\-.=?&])", re.I)


def _word_in(text: str, word: str) -> bool:
    if re.fullmatch(r"[a-z0-9 +\-]+", word):
        return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None
    return re.search(rf"(?<![\u0621-\u064a])(?:ال|وال|بال|لل|و|ب)?{re.escape(word)}(?![\u0621-\u064a])", text) is not None


def is_adult(*texts) -> bool:
    """هل أي نص من دول (اسم، وصف، تصنيف، رابط) فيه علامة محتوى إباحي؟"""
    for raw in texts:
        if not raw:
            continue
        raw = str(raw)
        if _PATH.search(raw):
            return True
        from sites.explorer import norm      # هنا عشان المستكشف نفسه بيستخدم الفلتر
        text = norm(raw)
        if any(_word_in(text, w) for w in _EN + _AR):
            return True
    return False
