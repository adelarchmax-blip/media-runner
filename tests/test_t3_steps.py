"""Torrent site files, stopping one item, and saved sign-ins on the runner."""
import hashlib
import json
import os
import stat
import sys

import pytest

for name, value in {"RUNNER_API_URL": "http://controller.invalid", "RUNNER_API_TOKEN": "t",
                    "RCLONE_REMOTE": "r:", "MEDIA_BASE_PATH": "m"}.items():
    os.environ.setdefault(name, value)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import worker  # noqa: E402


def benc(v) -> bytes:
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, str):
        v = v.encode()
    if isinstance(v, bytes):
        return b"%d:%s" % (len(v), v)
    if isinstance(v, list):
        return b"l" + b"".join(benc(x) for x in v) + b"e"
    return b"d" + b"".join(benc(k) + benc(v[k]) for k in sorted(v)) + b"e"


INFO = {"name": "film.mp4", "length": 1000, "piece length": 16384, "pieces": b"\x01" * 20}
DATA = benc({"announce": "udp://t.example:1/a", "info": INFO, "url-list": ["https://a.example/d/"]})
HASH = hashlib.sha1(benc(INFO)).hexdigest()
MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=film"


def test_info_span_finds_the_hash_part():
    a, b = worker._info_span(DATA)
    assert hashlib.sha1(DATA[a:b]).hexdigest() == HASH
    for bad in [b"", b"d4:infoi1e", b"l4:spam", b"x", b"d4:info" + b"l" * 100 + b"e" * 100 + b"e", b"d99999:x"]:
        with pytest.raises((ValueError, IndexError)):
            worker._info_span(bad)


class FakeResp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise worker.requests.HTTPError("bad")

    def iter_content(self, n):
        for i in range(0, len(self.data), n):
            yield self.data[i:i + n]


def test_torrent_source_uses_matching_file(tmp_path, monkeypatch):
    monkeypatch.setattr(worker.requests, "get", lambda *a, **k: FakeResp(DATA))
    path = worker.torrent_source({"torrent_url": "https://site.example/x.torrent"}, str(tmp_path), MAGNET)
    with open(path, "rb") as fh:
        assert path.endswith("source.torrent") and fh.read() == DATA


def test_torrent_source_falls_back_to_magnet(tmp_path, monkeypatch):
    other = benc({"info": dict(INFO, name="other.mp4")})
    monkeypatch.setattr(worker.requests, "get", lambda *a, **k: FakeResp(other))
    assert worker.torrent_source({"torrent_url": "https://site.example/x.torrent"}, str(tmp_path), MAGNET) == MAGNET
    monkeypatch.setattr(worker.requests, "get", lambda *a, **k: FakeResp(b"x" * (worker.MAX_TORRENT_FILE + 10)))
    assert worker.torrent_source({"torrent_url": "https://site.example/x.torrent"}, str(tmp_path), MAGNET) == MAGNET
    for bad in ["", None, "file:///etc/passwd", "https://x.example/a b", "javascript:alert(1)"]:
        assert worker.torrent_source({"torrent_url": bad}, str(tmp_path), MAGNET) == MAGNET


def test_session_cookie_only_for_its_site(tmp_path, monkeypatch):
    f = tmp_path / "s.json"
    f.write_text(json.dumps([{"domain": "site.example", "cookies": [
        {"name": "sid", "value": "1", "domain": ".site.example"}, {"name": "x", "value": "2", "domain": "other.example"}]}]))
    monkeypatch.setattr(worker, "SESSIONS_FILE", str(f))
    assert worker._session_cookie("https://www.site.example/a.torrent") == "sid=1"
    assert worker._session_cookie("https://evil.example/") == ""


def test_restore_sessions_file_is_private(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "SESSIONS_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(worker, "api", lambda path, payload: {"sessions": [{"domain": "a.example", "cookies": []}]})
    worker.restore_sessions()
    assert stat.S_IMODE(os.stat(tmp_path / "s.json").st_mode) == 0o600
    assert os.environ["MDM_SESSIONS"] == str(tmp_path / "s.json")
    monkeypatch.setattr(worker, "api", lambda path, payload: {})
    os.remove(tmp_path / "s.json")
    worker.restore_sessions()
    assert not (tmp_path / "s.json").exists()


def test_stop_from_dashboard_kills_the_item(monkeypatch):
    killed = []
    monkeypatch.setattr(worker, "report", lambda *a, **k: {"success": True, "stop": True})
    monkeypatch.setattr(worker, "_kill_group", lambda p: killed.append(p))
    p = worker.Progress(5)
    p.procs.append("proc")
    with pytest.raises(worker.Stopped):
        p.push(force=True)
    assert killed == ["proc"] and p.stopped


def test_stopped_item_is_not_reported_as_failed(tmp_path, monkeypatch):
    reports = []
    monkeypatch.setattr(worker, "WORK_DIR", str(tmp_path))
    monkeypatch.setattr(worker, "report", lambda *a, **k: reports.append((a, k)) or {"stop": True})
    monkeypatch.setattr(worker, "process_poster", lambda *a, **k: False)
    monkeypatch.setattr(worker, "work_details", lambda *a, **k: {})
    monkeypatch.setattr(worker, "destination", lambda item: ("f", "b", "f"))
    worker.process({"id": 7, "kind": "torrent", "clean_title": "x"})
    assert not any(a[1] == "failed" for a, _ in reports)
    assert not (tmp_path / "7").exists()
