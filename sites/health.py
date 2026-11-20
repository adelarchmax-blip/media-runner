"""Is a torrent alive? Asked of its trackers (scrape: how many hold the whole file),
in a few seconds, right after it is added and before it can reach the runner.

alive   -> a tracker counts at least one seeder: the item waits in the queue.
dead    -> trackers answered and none counts a seeder: the item is removed (cancelled, hidden) with a
           light notice in the activity log. A web seed alone does not keep it: the queue keeps a magnet,
           and a magnet's file list comes only from someone sharing it (a web seed serves files, not
           the list), so with no seeder the runner could never start it.
unknown -> no tracker answered (none listed, DHT only, all silent): the item waits; the runner gives
           it a short file-list check and reports it dead if nobody shares it.

Every address comes from a pasted magnet or .torrent file, so it is hostile: only public addresses are
contacted (no private, loopback or link-local ones), the socket goes to the address that was checked
(no second DNS answer), no redirects are followed and answers are size-capped.
"""
import concurrent.futures
import http.client
import ipaddress
import logging
import random
import socket
import ssl
import struct
import urllib.parse


TIMEOUT = 4                 # seconds for each tracker / web seed
DEADLINE = 7                # seconds for one torrent's whole check
MAX_ASKED = 6               # trackers asked per torrent (the first ones listed)
MAX_BODY = 64 * 1024
WORKERS = 24                # torrents checked at the same time
DEAD_NOTE = "ميت: مفيش حد بيشاركه"
_UDP_MAGIC = 0x41727101980



class Refused(Exception):
    pass


def _public_addr(host: str, port: int, kind=socket.SOCK_STREAM):
    """(family, address) of a public IP for host, or Refused: any private/reserved answer refuses it."""
    if not host or not 0 < int(port) < 65536:
        raise Refused("bad address")
    try:
        infos = socket.getaddrinfo(host, port, type=kind)
    except (socket.gaierror, UnicodeError, ValueError):
        raise Refused("no such host")
    if not infos:
        raise Refused("no such host")
    for _, _, _, _, addr in infos:
        if not ipaddress.ip_address(addr[0].split("%")[0]).is_global:
            raise Refused("private address")
    infos.sort(key=lambda i: i[0] != socket.AF_INET)       # IPv4 first (the server may have no IPv6)
    return infos[0][0], infos[0][4]


def _http_get(url: str):
    """(status, body) of a GET, no redirects, body capped; Refused / OSError on failure."""
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise Refused("bad address")
    port = p.port or (443 if p.scheme == "https" else 80)
    family, addr = _public_addr(p.hostname, port)
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(TIMEOUT)
    try:
        sock.connect(addr)
        if p.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=p.hostname)
        conn = http.client.HTTPConnection(p.hostname, port, timeout=TIMEOUT)
        conn.sock = sock
        path = (p.path or "/") + ("?" + p.query if p.query else "")
        conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", p.hostname if p.port is None else f"{p.hostname}:{port}")
        conn.putheader("User-Agent", "Mozilla/5.0")
        conn.endheaders()
        resp = conn.getresponse()
        body = resp.read(MAX_BODY + 1)
        return resp.status, body[:MAX_BODY] if len(body) <= MAX_BODY else b""
    finally:
        sock.close()


def scrape_url(announce: str):
    """BEP 48: the scrape address is the announce address with its last part 'announce' -> 'scrape'."""
    p = urllib.parse.urlsplit(announce)
    head, _, last = p.path.rpartition("/")
    if not last.startswith("announce"):
        return None
    return urllib.parse.urlunsplit(p._replace(path=f"{head}/scrape{last[8:]}"))


def _counts(stats) -> dict:
    """{seeds, peers} from a scrape answer's numbers (only plain non-negative ints count)."""
    def n(v):
        return v if type(v) is int and 0 <= v < 10 ** 9 else 0
    return {"seeds": n(stats.get(b"complete")), "peers": n(stats.get(b"incomplete"))}


def http_scrape(announce: str, btih: str):
    from sites.torrent_files import _bdecode, TorrentError
    url = scrape_url(announce)
    if not url:
        return None
    url += ("&" if "?" in url else "?") + "info_hash=" + urllib.parse.quote_from_bytes(bytes.fromhex(btih))
    status, body = _http_get(url)
    if status != 200 or not body:
        return None
    try:
        root, _ = _bdecode(body)
    except (TorrentError, IndexError, RecursionError):
        return None
    files = root.get(b"files") if isinstance(root, dict) else None
    if not isinstance(files, dict):
        return None
    stats = files.get(bytes.fromhex(btih))
    if not isinstance(stats, dict):       # the tracker does not know it: nobody announced it there
        return {"seeds": 0, "peers": 0} if not files else None
    return _counts(stats)


def udp_scrape(announce: str, btih: str):
    """BEP 15: connect, then scrape one hash."""
    p = urllib.parse.urlsplit(announce)
    if not p.hostname or not p.port:
        return None
    family, addr = _public_addr(p.hostname, p.port, socket.SOCK_DGRAM)
    s = socket.socket(family, socket.SOCK_DGRAM)
    s.settimeout(TIMEOUT / 2)
    try:
        s.connect(addr)              # only answers from that address are read

        def ask(packet, action, tid, size):
            for _ in range(2):
                s.send(packet)
                try:
                    data = s.recv(2048)
                except socket.timeout:
                    continue
                if len(data) >= size and struct.unpack(">II", data[:8]) == (action, tid):
                    return data
            return None
        tid = random.getrandbits(32)
        got = ask(struct.pack(">QII", _UDP_MAGIC, 0, tid), 0, tid, 16)
        if not got:
            return None
        conn_id = got[8:16]
        tid = random.getrandbits(32)
        got = ask(conn_id + struct.pack(">II", 2, tid) + bytes.fromhex(btih), 2, tid, 20)
        if not got:
            return None
        seeds, _done, peers = struct.unpack(">III", got[8:20])
        return {"seeds": seeds, "peers": peers}
    finally:
        s.close()


def _ask(job):
    kind, target, t = job
    try:
        fn = udp_scrape if target.lower().startswith("udp://") else http_scrape
        return kind, fn(target, t["btih"])
    except (Refused, OSError, http.client.HTTPException, ValueError, struct.error) as e:
        logging.debug(f"torrent check: {kind} gave no answer ({type(e).__name__})")
        return kind, None


def check(t: dict) -> dict:
    """t: a parsed magnet (sites.torrent_files.parse_magnet). Returns {state, seeds, peers}."""
    trackers = [u for u in t.get("trackers", []) if u.lower().startswith(("udp://", "http://", "https://"))]
    jobs = [("tracker", u, t) for u in trackers[:MAX_ASKED]]
    if not jobs:
        return {"state": "unknown", "seeds": None, "peers": None}
    seeds, peers, answered = 0, 0, 0
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs))
    try:
        futures = [ex.submit(_ask, j) for j in jobs]
        try:
            for f in concurrent.futures.as_completed(futures, timeout=DEADLINE):
                kind, res = f.result()
                if not res:
                    continue
                answered += 1
                seeds, peers = max(seeds, res["seeds"]), max(peers, res["peers"])
                if seeds > 0:
                    break                 # alive: no need to wait for the rest
        except concurrent.futures.TimeoutError:
            pass
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if seeds > 0:
        return {"state": "alive", "seeds": seeds, "peers": peers}
    if answered:
        return {"state": "dead", "seeds": 0, "peers": peers}
    return {"state": "unknown", "seeds": None, "peers": None}
