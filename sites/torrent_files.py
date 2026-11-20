"""Reading magnet links and .torrent files as data only (bounded, no code run).
The same rules as the controller's reader, so both sides agree on a torrent's hash and name."""
import base64
import binascii
import hashlib
import re
import urllib.parse

MAX_TORRENT_BYTES = 2 * 1024 * 1024
MAX_MAGNET_CHARS = 8000
MAX_TRACKERS = 30
MAX_SEEDS = 10
MAX_FILES = 20000
MAX_DEPTH = 64
VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts", ".webm", ".mpg", ".mpeg", ".flv")
SUB_EXT = (".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx")

_HEX40 = re.compile(r"[0-9a-fA-F]{40}")
_B32 = re.compile(r"[A-Za-z2-7]{32}")
_CTRL = re.compile(r"[\x00-\x1f\x7f\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_TRACKER = re.compile(r"(?:https?|udp|wss)://[^\s\"'<>\\]+", re.I)
_SEED = re.compile(r"https?://[^\s\"'<>\\]+", re.I)


class TorrentError(ValueError):
    """Arabic message for the owner."""


def clean_text(value, limit=200) -> str:
    text = _CTRL.sub(" ", str(value or ""))
    return " ".join(text.split())[:limit]


# ---------- bencode (strict, bounded) ----------

def _bdecode(data: bytes):
    """Returns (value, info_span). info_span is the raw byte range of the top-level 'info' dict."""
    pos = 0
    count = 0
    info_span = None

    def read(depth):
        nonlocal pos, count, info_span
        if depth > MAX_DEPTH:
            raise TorrentError("ملف التورنت متداخل بزيادة")
        count += 1
        if count > 200000:
            raise TorrentError("ملف التورنت فيه بيانات كتير بزيادة")
        if pos >= len(data):
            raise TorrentError("ملف التورنت ناقص")
        c = data[pos:pos + 1]
        if c == b"i":
            end = data.find(b"e", pos)
            if end < 0:
                raise TorrentError("ملف التورنت تالف")
            raw = data[pos + 1:end]
            if not re.fullmatch(rb"-?(0|[1-9]\d{0,18})", raw) or raw == b"-0":
                raise TorrentError("ملف التورنت تالف")
            pos = end + 1
            return int(raw)
        if c == b"l":
            pos += 1
            out = []
            while data[pos:pos + 1] != b"e":
                out.append(read(depth + 1))
            pos += 1
            return out
        if c == b"d":
            pos += 1
            out = {}
            while data[pos:pos + 1] != b"e":
                key = read(depth + 1)
                if not isinstance(key, bytes) or key in out:
                    raise TorrentError("ملف التورنت تالف")
                start = pos
                out[key] = read(depth + 1)
                if depth == 0 and key == b"info":
                    info_span = (start, pos)
            pos += 1
            return out
        if c.isdigit():
            colon = data.find(b":", pos, pos + 12)
            if colon < 0 or not re.fullmatch(rb"0|[1-9]\d*", data[pos:colon]):
                raise TorrentError("ملف التورنت تالف")
            n = int(data[pos:colon])
            if colon + 1 + n > len(data):
                raise TorrentError("ملف التورنت ناقص")
            pos = colon + 1 + n
            return data[colon + 1:pos]
        raise TorrentError("ملف التورنت تالف")

    value = read(0)
    if pos != len(data):
        raise TorrentError("ملف التورنت فيه بيانات زيادة في آخره")
    return value, info_span


def _s(v) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else ""


def _urls(values, pattern, limit):
    out = []
    for v in values:
        v = clean_text(v, 400)
        if pattern.fullmatch(v) and v not in out:
            out.append(v)
        if len(out) >= limit:
            break
    return out


def parse_torrent_file(data: bytes) -> dict:
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise TorrentError("الملف فاضي")
    if len(data) > MAX_TORRENT_BYTES:
        raise TorrentError("ملف التورنت أكبر من 2MB")
    try:
        root, span = _bdecode(bytes(data))
    except (IndexError, RecursionError):
        raise TorrentError("ملف التورنت تالف")
    if not isinstance(root, dict) or not span or not isinstance(root.get(b"info"), dict):
        raise TorrentError("ده مش ملف تورنت")
    info = root[b"info"]
    if b"pieces" not in info and b"file tree" in info:
        raise TorrentError("تورنت من النوع الجديد (v2) بس، ومش مدعوم لسه")
    btih = hashlib.sha1(bytes(data[span[0]:span[1]])).hexdigest()
    name = clean_text(_s(info.get(b"name.utf-8") or info.get(b"name")))
    files = []
    if isinstance(info.get(b"files"), list):
        for f in info[b"files"][:MAX_FILES]:
            if not isinstance(f, dict):
                continue
            parts = f.get(b"path.utf-8") or f.get(b"path") or []
            path = "/".join(_s(p) for p in parts if isinstance(p, bytes))
            size = f.get(b"length") if isinstance(f.get(b"length"), int) else 0
            files.append((clean_text(path, 400), max(size, 0)))
    elif isinstance(info.get(b"length"), int):
        files.append((name, max(info[b"length"], 0)))
    if not files:
        raise TorrentError("التورنت ده مفيهوش ملفات")
    trackers = [root.get(b"announce")]
    for tier in root.get(b"announce-list") or []:
        if isinstance(tier, list):
            trackers.extend(tier)
    seeds = root.get(b"url-list")
    seeds = seeds if isinstance(seeds, list) else [seeds]
    return {"btih": btih, "name": name, "files": files, "size_bytes": sum(s for _, s in files),
            "trackers": _urls([_s(t) for t in trackers], _TRACKER, MAX_TRACKERS),
            "web_seeds": _urls([_s(t) for t in seeds], _SEED, MAX_SEEDS)}


# ---------- magnet ----------

def _btih_hex(value: str) -> str:
    if _HEX40.fullmatch(value):
        return value.lower()
    if _B32.fullmatch(value):
        return binascii.hexlify(base64.b32decode(value.upper())).decode()
    raise TorrentError("بصمة التورنت في رابط magnet مش سليمة")


def parse_magnet(text: str) -> dict:
    text = (text or "").strip()
    if len(text) > MAX_MAGNET_CHARS:
        raise TorrentError("رابط magnet طويل بزيادة")
    if not text.lower().startswith("magnet:?"):
        raise TorrentError("ده مش رابط magnet")
    params = urllib.parse.parse_qs(text[8:], keep_blank_values=False)
    btih = None
    for xt in params.get("xt", []):
        if xt.lower().startswith("urn:btih:"):
            btih = _btih_hex(xt[9:])
            break
    if not btih:
        if any(xt.lower().startswith("urn:btmh:") for xt in params.get("xt", [])):
            raise TorrentError("تورنت من النوع الجديد (v2) بس، ومش مدعوم لسه")
        raise TorrentError("رابط magnet من غير بصمة تورنت")
    size = None
    xl = (params.get("xl") or [""])[0]
    if xl.isdigit() and len(xl) < 16:
        size = int(xl)
    return {"btih": btih, "name": clean_text((params.get("dn") or [""])[0]), "files": [], "size_bytes": size,
            "trackers": _urls(params.get("tr", []), _TRACKER, MAX_TRACKERS),
            "web_seeds": _urls(params.get("ws", []), _SEED, MAX_SEEDS)}


def build_magnet(t: dict) -> str:
    parts = [("xt", "urn:btih:" + t["btih"])]
    if t.get("name"):
        parts.append(("dn", t["name"]))
    if t.get("size_bytes"):
        parts.append(("xl", str(t["size_bytes"])))
    parts += [("tr", u) for u in t.get("trackers", [])] + [("ws", u) for u in t.get("web_seeds", [])]
    return "magnet:?" + "&".join(f"{k}={urllib.parse.quote(v, safe=':/')}" for k, v in parts)
