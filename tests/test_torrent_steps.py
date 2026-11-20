"""Torrent path of the worker: input check, file choice, subtitle names, and the download steps with
a fake MDM command (finished, stalled, failed, files outside the folder)."""
import json
import os
import socket
import stat
import sys

import pytest

for name, value in {"RUNNER_API_URL": "http://controller.invalid", "RUNNER_API_TOKEN": "t",
                    "RCLONE_REMOTE": "r:", "MEDIA_BASE_PATH": "m"}.items():
    os.environ.setdefault(name, value)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import worker  # noqa: E402

HASH = "3514d8e28544179d35e8bbc10de6f29108375b7a"
MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=His%20Girl&tr=udp://t.example:1337/announce&ws=https://a.example/d/"


def test_check_magnet_accepts_rebuilt_links_only():
    assert worker.check_magnet(" " + MAGNET + "\n") == MAGNET
    assert worker.check_magnet("magnet:?xt=urn:btih:" + "A" * 32)
    for bad in ["", None, "https://x.example/a.torrent", MAGNET + ";rm -rf /", MAGNET + " x",
                "magnet:?dn=x", "magnet:?xt=urn:btih:123", "magnet:?xt=urn:btih:" + HASH + "0",
                MAGNET + "$(id)", MAGNET + "`id`", "magnet:?xt=urn:btih:" + HASH + "&" + "a" * 8001]:
        with pytest.raises(RuntimeError):
            worker.check_magnet(bad)


def f(i, path, mb):
    return {"index": i, "path": path, "size": int(mb * 1048576)}


def test_pick_files_largest_film_and_subtitles():
    files = [f(0, "Film/Sample/film.sample.mkv", 50), f(1, "Film/film.avi", 900), f(2, "Film/film.mkv", 700),
             f(3, "Film/Trailer.mp4", 800), f(4, "Film/Subs/English.srt", 0.1), f(5, "Film/ar.srt", 0.1),
             f(6, "Film/huge.srt", 9), f(7, "Film/cover.jpg", 1), f(8, "Film/extras/making.mkv", 1200),
             {"index": "9", "path": "x.mkv", "size": 5000}, "junk", f(10, "Film/empty.mkv", 0)]
    best, subs = worker.pick_files(files)
    assert best["index"] == 2                      # mkv before a bigger avi; samples/trailers/extras skipped
    assert [s["index"] for s in subs] == [4, 5]    # oversized subtitle left out
    with pytest.raises(RuntimeError):
        worker.pick_files([f(0, "readme.txt", 1), f(1, "sample.mkv", 100)])


def test_subtitle_names_language_and_repeats():
    names = worker.subtitle_names([("/a", "Film/Subs/2_English.srt"), ("/b", "Film/Subs/3_English.srt"),
                                   ("/c", "Film/arabic/film.SRT"), ("/d", "Film/film.ass"),
                                   ("/e", "Film/Subs/eng.srt")], "Film (1940)")
    assert [n for _, n in names] == ["Film (1940).en.srt", "Film (1940).en.2.srt", "Film (1940).ar.srt",
                                     "Film (1940).ass", "Film (1940).en.3.srt"]


FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
mode = os.environ["FAKE_MODE"]
args = sys.argv[1:]
if "torrent-info" in args:
    if mode == "hang":
        time.sleep(60)
    if mode == "noinfo":
        print(json.dumps({"error": "metadata not found at https://secret.example/x"})); sys.exit(1)
    print(json.dumps({"name": "His Girl", "size": 3, "files": [
        {"index": 0, "path": "film.mp4", "size": 2000000}, {"index": 1, "path": "subs/English.srt", "size": 10},
        {"index": 2, "path": "../../escape.srt", "size": 10}, {"index": 3, "path": "notes.txt", "size": 4}]}))
    sys.exit(0)
out = args[args.index("-o") + 1]
if args[1] == "get":
    open(os.environ["FAKE_ARGS"] + ".get", "a").write(json.dumps(args) + "\n")
    if os.environ.get("FAKE_GET") == "fail" or "film" not in args[2] and "English" not in args[2]:
        open(out + ".mdm.part", "w").write("half")
        print(json.dumps({"error": "the file's SHA-1 checksum doesn't match"})); sys.exit(1)
    size = 2000000 if "film" in args[2] else 10
    open(out, "wb").write(b"y" * size)
    print(json.dumps({"event": "progress", "downloaded": size, "size": None, "speed": 5, "connections": 1}), flush=True)
    print(json.dumps({"event": "done", "path": out, "size": size})); sys.exit(0)
assert args[args.index("--files") + 1] == "0,1,2", args
open(os.environ["FAKE_ARGS"], "w").write(json.dumps(args))
if mode == "stall":
    print(json.dumps({"event": "progress", "downloaded": 5, "size": 2000000, "speed": 1, "peers": 0}), flush=True)
    time.sleep(60); sys.exit(0)
if mode == "silent":
    time.sleep(60); sys.exit(0)
if mode == "error":
    print(json.dumps({"error": "tracker said no"})); sys.exit(1)
d = os.path.join(out, "His Girl")
os.makedirs(os.path.join(d, "subs"))
open(os.path.join(d, "film.mp4"), "wb").write(b"x" * 2000000)
open(os.path.join(d, "subs", "English.srt"), "w").write("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
open(os.path.join(os.path.dirname(os.path.dirname(out)), "escape.srt"), "w").write("outside")
for got in (0, 1000000, 2000000):
    print(json.dumps({"event": "progress", "downloaded": got, "size": 2000000, "speed": 1048576, "peers": 3}), flush=True)
print(json.dumps({"event": "done", "path": d, "size": 2000000}))
'''


@pytest.fixture
def fake_mdm(tmp_path, monkeypatch):
    cli = tmp_path / "fake-mdm"
    cli.write_text(FAKE)
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(worker, "MDM_CLI", str(cli))
    monkeypatch.setattr(worker, "stop_leftover_mdm", lambda: None)
    monkeypatch.setattr(worker, "MIN_VIDEO_BYTES", 1000)
    monkeypatch.setitem(worker._DISK, "held", {})      # process() frees it; these tests call the step alone
    monkeypatch.setattr(worker, "report", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_public_web", lambda url: False)    # no web source here unless a test says so
    monkeypatch.setenv("FAKE_ARGS", str(tmp_path / "args.json"))
    job = tmp_path / "work" / "7"
    job.mkdir(parents=True)
    return tmp_path, str(job / "His Girl (1940)")


def run(base, mode, monkeypatch, url=MAGNET):
    monkeypatch.setenv("FAKE_MODE", mode)
    return worker.download_torrent({"id": 7, "kind": "torrent", "watch_url": url}, base, worker.Progress(7))


def test_download_torrent_film_and_subtitle(fake_mdm, monkeypatch):
    tmp, base = fake_mdm
    progress_seen = []
    monkeypatch.setattr(worker.Progress, "push", lambda self, **k: progress_seen.append(self.dl_pct))
    video, subs = run(base, "ok", monkeypatch)
    assert video == base + ".mp4" and os.path.getsize(video) == 2000000
    assert [n for _, n in subs] == ["His Girl (1940).en.srt"]   # the file outside the folder is ignored
    assert progress_seen[-1] == 100
    args = json.loads((tmp / "args.json").read_text())
    assert args[:3] == ["--json", "torrent-get", MAGNET] and "--seconds" in args


def test_download_torrent_failures(fake_mdm, monkeypatch):
    _, base = fake_mdm
    with pytest.raises(RuntimeError, match="no file list") as e:
        run(base, "noinfo", monkeypatch)
    assert "secret.example" not in str(e.value)
    with pytest.raises(RuntimeError, match="tracker said no"):
        run(base, "error", monkeypatch)
    with pytest.raises(RuntimeError, match="not a valid magnet"):
        run(base, "ok", monkeypatch, url="magnet:?xt=urn:btih:" + HASH + "&x=$(reboot)")


def test_download_torrent_stall_is_stopped(fake_mdm, monkeypatch):
    _, base = fake_mdm
    monkeypatch.setattr(worker, "TORRENT_STALL", 0.5)
    monkeypatch.setattr(worker, "TORRENT_CHECK", 0.2)
    with pytest.raises(RuntimeError, match="stalled"):
        run(base, "stall", monkeypatch)


def test_no_time_left(fake_mdm, monkeypatch):
    _, base = fake_mdm
    monkeypatch.setattr(worker, "START", worker.time.time() - worker.MAX_SECONDS + 5 * 60)
    with pytest.raises(RuntimeError, match="no time left"):
        run(base, "ok", monkeypatch)


def test_process_torrent_item_uploads_video_then_subtitles(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(worker, "process_poster", lambda *a, **k: False)
    monkeypatch.setattr(worker, "work_details", lambda *a, **k: {})
    reports, copies = [], []
    monkeypatch.setattr(worker, "report", lambda item_id, status, **k: reports.append((status, k)))
    monkeypatch.setattr(worker, "download", lambda *a: pytest.fail("page download used for a torrent"))

    def fake_torrent(item, out_base, progress):
        v, s = out_base + ".mp4", out_base + ".srt"
        with open(v, "wb") as fh:
            fh.write(b"v")
        with open(s, "w") as fh:
            fh.write("s")
        return v, [(s, os.path.basename(out_base) + ".en.srt")]
    monkeypatch.setattr(worker, "download_torrent", fake_torrent)
    monkeypatch.setattr(worker, "verify_video", lambda p: p)
    monkeypatch.setattr(worker, "upload", lambda local, remote, progress: copies.append(remote) or 5.0)

    class Done:
        returncode = 0
    monkeypatch.setattr(worker.subprocess, "run", lambda cmd, **k: copies.append(cmd[-1]) or Done())
    worker.process({"id": 9, "kind": "torrent", "clean_title": "His Girl Friday", "year": 1940,
                    "content_type": "movie", "section_path": "Movies/Foreign_Movies", "watch_url": MAGNET})
    folder = "m/Movies/Foreign_Movies/His Girl Friday (1940)"
    assert copies == [f"{folder}/His Girl Friday (1940).mp4", f"r:{folder}/His Girl Friday (1940).en.srt"]
    assert reports[-1][0] == "uploaded"
    assert not os.path.exists(tmp_path / "work" / "9")


def test_dead_torrent_without_file_list(fake_mdm, monkeypatch):
    _, base = fake_mdm
    monkeypatch.setattr(worker, "TORRENT_INFO_WAIT", 0.5)
    started = worker.time.time()
    with pytest.raises(RuntimeError, match="^torrent dead: no one shares its file list"):
        run(base, "hang", monkeypatch)
    assert worker.time.time() - started < 5 and not worker.TORRENT_GROUPS   # its process is gone
    with pytest.raises(RuntimeError, match="^torrent dead: no file list"):
        run(base, "noinfo", monkeypatch)


def test_dead_from_the_start_vs_stalled_later(fake_mdm, monkeypatch):
    _, base = fake_mdm
    monkeypatch.setattr(worker, "TORRENT_STALL", 0.5)
    monkeypatch.setattr(worker, "TORRENT_CHECK", 0.2)
    with pytest.raises(RuntimeError, match="^torrent dead: nobody sent"):
        run(base, "silent", monkeypatch)
    with pytest.raises(RuntimeError, match="^torrent: no one is sharing it") as e:
        run(base, "stall", monkeypatch)          # some parts came, then it stopped: tried again later
    assert not str(e.value).startswith(worker.DEAD) and not worker.TORRENT_GROUPS


def test_page_cleanup_leaves_running_torrents(monkeypatch):
    killed = []

    class R:
        stdout = "11 12 13"
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: R())
    monkeypatch.setattr(worker.os, "getpgid", lambda pid: 500 if pid == 12 else pid)
    monkeypatch.setattr(worker.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(worker, "TORRENT_GROUPS", {500})
    worker.stop_leftover_mdm()
    assert killed == [11, 13]


def test_disk_room_is_held_per_torrent(monkeypatch, tmp_path):
    GB = 1073741824

    class U:
        free = 10 * GB
    monkeypatch.setattr(worker.shutil, "disk_usage", lambda p: U())
    monkeypatch.setattr(worker, "DISK_MARGIN", 1 * GB)
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)
    monkeypatch.setitem(worker._DISK, "held", {})
    worker.hold_disk(1, 5 * GB)
    with pytest.raises(RuntimeError, match="no disk space"):
        worker.hold_disk(2, 5 * GB, wait=0)            # 10 - 5 held - 1 margin = 4 < 5
    worker.free_disk(1)
    worker.hold_disk(2, 5 * GB, wait=0)
    worker.free_disk(2)
    with pytest.raises(RuntimeError, match="no disk space"):
        worker.hold_disk(3, 20 * GB)                   # never fits, nothing else to wait for: fails at once
    assert worker._DISK["held"] == {}


def test_rolling_batch_fills_a_slot_the_moment_it_frees(monkeypatch):
    """5 torrents, 2 slots: a quick one ends -> the next starts while the slow one still runs."""
    import threading
    monkeypatch.setattr(worker, "TORRENT_SLOTS", 2)
    monkeypatch.setattr(worker, "EMPTY_RECHECK", 0.3)
    queue = [{"id": i, "kind": "torrent", "t": t} for i, t in enumerate([1.2, 0.1, 0.1, 0.1, 0.1], 1)]
    asks, events, lock = [], [], threading.Lock()
    running = {"now": 0, "max": 0}

    def next_items(kind, n, tag):
        assert kind == "torrent"
        asks.append(n)
        with lock:
            got, queue[:] = queue[:n], queue[n:]
        return got

    def process(item):
        with lock:
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
            events.append(("start", item["id"]))
        worker.time.sleep(item["t"])
        with lock:
            running["now"] -= 1
            events.append(("end", item["id"]))
    monkeypatch.setattr(worker, "next_items", next_items)
    monkeypatch.setattr(worker, "process", process)
    count, busy = {"pages": 0, "torrents": 0}, threading.Event()
    busy.set()
    started = worker.time.time()
    worker.torrent_lane(count, busy)
    assert count["torrents"] == 5 and running["max"] == 2 and not busy.is_set()
    assert asks[0] == 2 and max(asks) <= 2
    # items 3, 4, 5 all started before the slow item 1 ended
    assert events.index(("end", 1)) > max(events.index(("start", i)) for i in (3, 4, 5))
    assert worker.time.time() - started < 3


def test_page_lane_waits_for_new_pages_while_torrents_run(monkeypatch):
    import threading
    monkeypatch.setattr(worker, "EMPTY_RECHECK", 0.05)
    answers = [[], [{"id": 1}], []]
    done = []
    busy = threading.Event()
    busy.set()

    def next_items(kind, n, tag):
        assert kind == "page"
        got = answers.pop(0) if answers else []
        if not answers:
            busy.clear()            # torrents ended
        return got
    monkeypatch.setattr(worker, "next_items", next_items)
    monkeypatch.setattr(worker, "process", lambda item: done.append(item["id"]))
    worker.page_lane({"pages": 0, "torrents": 0}, busy)
    assert done == [1]


def test_web_sources_from_torrent_file_and_magnet(tmp_path):
    from test_t3_steps import benc
    info = {"name": "His Girl", "piece length": 16384, "pieces": b"\x01" * 20, "files": [
        {"path": ["film.mp4"], "length": 2000000, "sha1": "AB" * 20},
        {"path": ["subs", "English.srt"], "length": 10, "sha1": "short"}]}
    t = tmp_path / "source.torrent"
    t.write_bytes(benc({"info": info, "url-list": ["https://a.example/download/", "https://b.example/items"]}))
    web = worker.web_sources(str(t), MAGNET)
    assert web["urls"] == ["https://a.example/download/", "https://b.example/items", "https://a.example/d/"]
    assert web["multi"] is True and web["sha1"] == {"film.mp4": "ab" * 20}
    one = tmp_path / "one.torrent"
    one.write_bytes(benc({"info": {"name": "f.mp4", "length": 5, "sha1": "cd" * 20}, "url-list": "https://c.example/f.mp4"}))
    assert worker.web_sources(str(one), MAGNET) == {"urls": ["https://c.example/f.mp4", "https://a.example/d/"],
                                                     "multi": False, "sha1": {"f.mp4": "cd" * 20}}
    bad = tmp_path / "bad.torrent"
    bad.write_bytes(b"d4:infoi1ee")
    assert worker.web_sources(str(bad), MAGNET)["urls"] == []
    assert worker.web_sources(MAGNET, MAGNET) == {"urls": ["https://a.example/d/"], "multi": None, "sha1": {}}


def test_webseed_urls_follow_bep19():
    seeds = ["https://a.example/download/", "https://b.example/2/items", "https://c.example/x.mp4"]
    assert worker.webseed_urls(seeds, "His Girl", "subs/Eng lish#1.srt", True) == [
        "https://a.example/download/His%20Girl/subs/Eng%20lish%231.srt",
        "https://b.example/2/items/His%20Girl/subs/Eng%20lish%231.srt",
        "https://c.example/x.mp4/His%20Girl/subs/Eng%20lish%231.srt"]
    assert worker.webseed_urls(seeds, "f.mp4", "f.mp4", False) == [
        "https://a.example/download/f.mp4", "https://b.example/2/items", "https://c.example/x.mp4"]


def test_public_web_refuses_local_and_odd_addresses(monkeypatch):
    addrs = {"pub.example": "93.184.216.34", "lan.example": "192.168.1.5", "me.example": "127.0.0.1",
             "v6.example": "::1", "meta.example": "169.254.169.254"}

    def lookup(host, port, **k):
        if host not in addrs:
            raise OSError("no such host")
        return [(0, 0, 0, "", (addrs[host], port))]
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    assert worker._public_web("https://pub.example/a.mp4")
    for bad in ["https://lan.example/a", "http://me.example/a", "https://v6.example/a", "http://meta.example/",
                "ftp://pub.example/a", "file:///etc/passwd", "https://u:p@pub.example/a", "https://pub.example/a b",
                "https://nope.example/a", "https://pub.example:99999/a", None, "https://pub.example/" + "a" * 2001]:
        assert not worker._public_web(bad), bad


class Head:
    def __init__(self, status, headers):
        self.status_code, self.headers = status, headers
        self.is_redirect = status in (301, 302, 303, 307, 308)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_probe_checks_size_and_every_redirect(monkeypatch):
    monkeypatch.setattr(worker, "_public_web", lambda url: "lan" not in url)
    answers = {"https://a.example/f": Head(302, {"Location": "https://node.example/f"}),
               "https://node.example/f": Head(200, {"Content-Length": "100"}),
               "https://b.example/f": Head(302, {"Location": "http://lan.example/f"}),
               "https://c.example/f": Head(404, {}),
               "https://d.example/f": Head(302, {"Location": "/d/f"}),
               "https://d.example/d/f": Head(200, {})}
    monkeypatch.setattr(worker.requests, "get", lambda url, **k: answers[url])
    assert worker._probe("https://a.example/f", 100) == "https://node.example/f"
    assert worker._probe("https://a.example/f", 99) == ""          # another size: another file
    assert worker._probe("https://b.example/f", 100) == ""         # redirected into a local network
    assert worker._probe("https://c.example/f", 100) == ""
    assert worker._probe("https://d.example/f", 100) == "https://d.example/d/f"


def direct_ready(monkeypatch, tmp):
    monkeypatch.setattr(worker, "_public_web", lambda url: True)
    monkeypatch.setattr(worker, "_probe", lambda url, size: url)
    monkeypatch.setenv("FAKE_GET", "ok")
    return tmp / "args.json.get"


def test_direct_web_source_skips_the_torrent(fake_mdm, monkeypatch):
    tmp, base = fake_mdm
    calls = direct_ready(monkeypatch, tmp)
    video, subs = run(base, "ok", monkeypatch)
    assert video == base + ".mp4" and os.path.getsize(video) == 2000000
    assert [n for _, n in subs] == ["His Girl (1940).en.srt"]
    got = [json.loads(x) for x in calls.read_text().splitlines()]
    assert [a[2] for a in got][:2] == ["https://a.example/d/His%20Girl/film.mp4",
                                       "https://a.example/d/His%20Girl/subs/English.srt"]
    assert all(a[:2] == ["--json", "get"] and "--direct" in a for a in got)
    assert not (tmp / "args.json").exists()                # the torrent engine never started


def test_direct_web_source_failing_falls_back_to_torrent(fake_mdm, monkeypatch):
    tmp, base = fake_mdm
    calls = direct_ready(monkeypatch, tmp)
    monkeypatch.setenv("FAKE_GET", "fail")
    video, subs = run(base, "ok", monkeypatch)
    assert video == base + ".mp4" and calls.exists()
    assert json.loads((tmp / "args.json").read_text())[1] == "torrent-get"
    assert not os.listdir(os.path.join(os.path.dirname(base), "torrent", "direct"))   # failed tries leave nothing
