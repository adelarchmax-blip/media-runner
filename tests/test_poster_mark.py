"""A written poster (no picture found) is replaced later by a real one; a real one stays."""
import os

import sys

for name, value in {"RUNNER_API_URL": "http://controller.invalid", "RUNNER_API_TOKEN": "t",
                    "RCLONE_REMOTE": "r:", "MEDIA_BASE_PATH": "m"}.items():
    os.environ.setdefault(name, value)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


import worker  # noqa: E402


def drive(monkeypatch, files):
    calls = []

    def run(cmd, **k):
        calls.append(cmd[1])
        path = cmd[-1].split(":", 1)[1]
        if cmd[1] in ("copyto", "touch"):
            files.add(path)
        elif cmd[1] == "deletefile":
            files.discard(path)
    monkeypatch.setattr(worker.subprocess, "run", run)
    monkeypatch.setattr(worker, "remote_exists", lambda p: p in files)
    return calls


def test_a_real_picture_replaces_a_written_poster_once(monkeypatch):
    files = set()
    calls = drive(monkeypatch, files)
    worker.put_poster("p.jpg", "Show", "text")
    assert files == {"Show/poster.jpg", "Show/poster.text"}
    worker.put_poster("p.jpg", "Show", "text")              # another episode, still no picture: nothing again
    assert calls == ["copyto", "touch"]
    worker.put_poster("p.jpg", "Show", "real")
    assert files == {"Show/poster.jpg"} and calls[2:] == ["copyto", "deletefile"]
    worker.put_poster("p.jpg", "Show", "real")              # a real picture is there: it stays
    worker.put_poster("p.jpg", "Show", "text")
    assert len(calls) == 4


def test_the_first_real_picture_leaves_no_mark(monkeypatch):
    files = set()
    calls = drive(monkeypatch, files)
    worker.put_poster("p.jpg", "Film (2001)", "real")
    assert files == {"Film (2001)/poster.jpg"} and calls == ["copyto"]


def test_process_poster_says_which_kind(tmp_path, monkeypatch):
    import poster
    out = str(tmp_path / "p.jpg")
    monkeypatch.setattr(poster, "find_best_poster", lambda *a: None)
    assert poster.process_poster("", out, "Some Title", 2001) == "text"
    monkeypatch.setattr(poster, "find_best_poster", lambda *a: poster.create_fallback_poster("x"))
    assert poster.process_poster("", out, "Some Title") == "real"
    monkeypatch.setattr(poster, "find_best_poster", lambda *a: 1 / 0)
    assert poster.process_poster("", out) == ""
