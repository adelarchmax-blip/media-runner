"""Torrent life check on the runner: trackers (HTTP + UDP scrape) and web seeds served on this machine, fake answers."""
import os
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sites import health as th  # noqa: E402

HASH = "3514d8e28544179d35e8bbc10de6f29108375b7a"


def benc(v) -> bytes:
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, str):
        v = v.encode()
    if isinstance(v, bytes):
        return b"%d:%s" % (len(v), v)
    return b"d" + b"".join(benc(k) + benc(v[k]) for k in sorted(v)) + b"e"


class Fake:
    """HTTP tracker + web seed + UDP tracker on 127.0.0.1; `seeds` sets what they answer."""
    def __init__(self):
        self.seeds, self.http_hits, self.seed_ok, self.udp_silent = {}, [], True, False
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                fake.http_hits.append(self.path)
                if self.path.startswith("/scrape"):
                    files = {bytes.fromhex(h): {"complete": n, "incomplete": 2, "downloaded": 9}
                             for h, n in fake.seeds.items()}
                    body = benc({"files": files})
                elif self.path.startswith("/redirect"):
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:1/scrape")
                    self.end_headers()
                    return
                elif self.path.startswith("/big"):
                    body = b"x" * (th.MAX_BODY + 10)
                elif self.path.startswith("/seed/") and fake.seed_ok:
                    body = b"x"
                    self.send_response(206)
                    self.send_header("Content-Length", "1")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.udp.settimeout(0.2)
        self.stop = threading.Event()
        threading.Thread(target=self.udp_loop, daemon=True).start()

    def udp_loop(self):
        while not self.stop.is_set():
            try:
                data, addr = self.udp.recvfrom(2048)
            except socket.timeout:
                continue
            if self.udp_silent:
                continue
            if len(data) == 16 and struct.unpack(">QII", data)[0] == 0x41727101980:
                self.udp.sendto(struct.pack(">II", 0, struct.unpack(">QII", data)[2]) + b"C" * 8, addr)
            elif len(data) == 36 and data[:8] == b"C" * 8:
                _, tid = struct.unpack(">II", data[8:16])
                n = self.seeds.get(data[16:36].hex(), 0)
                self.udp.sendto(struct.pack(">IIIII", 2, tid, n, 5, 3), addr)

    @property
    def h(self):
        return f"http://127.0.0.1:{self.http.server_port}"

    @property
    def u(self):
        return f"udp://127.0.0.1:{self.udp.getsockname()[1]}/announce"

    def close(self):
        self.stop.set()
        self.http.shutdown()
        self.http.server_close()
        self.udp.close()


@pytest.fixture
def fake(monkeypatch):
    real = th._public_addr
    # this machine stands in for the public internet; the private-address guard is tested on its own
    monkeypatch.setattr(th, "_public_addr", lambda host, port, kind=socket.SOCK_STREAM:
                        (socket.AF_INET, ("127.0.0.1", port)) if host == "fake.test" else real(host, port, kind))
    monkeypatch.setattr(th, "TIMEOUT", 1)
    monkeypatch.setattr(th, "DEADLINE", 3)
    f = Fake()
    yield f
    f.close()


def t(trackers=(), seeds=(), name="x"):
    return {"btih": HASH, "name": name, "trackers": list(trackers), "web_seeds": list(seeds)}


def fk(url):
    return url.replace("127.0.0.1", "fake.test")


def test_scrape_url():
    assert th.scrape_url("http://a/announce") == "http://a/scrape"
    assert th.scrape_url("http://a/x/announce.php?k=1") == "http://a/x/scrape.php?k=1"
    assert th.scrape_url("http://a/a") is None


def test_alive_by_http_and_udp(fake):
    fake.seeds = {HASH: 7}
    r = th.check(t([fk(fake.h + "/announce")]))
    assert r == {"state": "alive", "seeds": 7, "peers": 2}
    assert "info_hash=" in fake.http_hits[-1]
    assert th.check(t([fk(fake.u)]))["seeds"] == 7


def test_dead_when_trackers_answer_zero(fake):
    fake.seeds = {HASH: 0}
    assert th.check(t([fk(fake.h + "/announce"), fk(fake.u)]))["state"] == "dead"
    fake.seeds = {}         # the tracker never heard of it
    assert th.check(t([fk(fake.h + "/announce")]))["state"] == "dead"


def test_web_seed_alone_is_not_life(fake):
    """A magnet's file list needs someone sharing it; a web seed only serves files."""
    fake.seeds = {HASH: 0}
    assert th.check(t([fk(fake.h + "/announce")], [fk(fake.h + "/seed/")]))["state"] == "dead"
    assert not any(p.startswith("/seed/") for p in fake.http_hits)
    assert th.check(t([], [fk(fake.h + "/seed/")]))["state"] == "unknown"


def test_unknown_when_nobody_answers(fake):
    fake.udp_silent = True
    started = time.time()
    assert th.check(t([fk(fake.u), "wss://tracker.example/", fk(fake.h + "/nothing/announce")]))["state"] == "unknown"
    assert time.time() - started < 4
    assert th.check(t())["state"] == "unknown"


def test_bad_answers_are_no_answer(fake):
    assert th.check(t([fk(fake.h + "/redirect/announce")]))["state"] == "unknown"
    assert th.check(t([fk(fake.h + "/big/announce")]))["state"] == "unknown"


def test_private_addresses_refused():
    for host in ("127.0.0.1", "localhost", "10.0.0.234", "169.254.169.254", "::1", "192.168.1.1"):
        with pytest.raises(th.Refused):
            th._public_addr(host, 80)
    # a real check against private trackers never connects: unknown
    assert th.check(t(["http://127.0.0.1:9/announce", "udp://10.0.0.1:80/announce"]))["state"] == "unknown"
