"""Torrent path of the worker: input check, file choice, subtitle names, and the download steps with
a fake MDM command (finished, stalled, failed, files outside the folder)."""
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
