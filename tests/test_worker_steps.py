"""The download script's steps kept for a failed item: only its own lines, no web addresses."""
import os
import sys

for name, value in {"RUNNER_API_URL": "http://controller.invalid", "RUNNER_API_TOKEN": "t",
                    "RCLONE_REMOTE": "r:", "MEDIA_BASE_PATH": "m"}.items():
    os.environ.setdefault(name, value)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import worker  # noqa: E402


def test_last_steps_keeps_own_lines_and_hides_addresses(tmp_path):
    log = tmp_path / "grab.log"
    log.write_text("noise from chrome\n[grab] MDM running\n"
                   "[grab] opened https://site.example/watch?v=1 now\n"
                   "[grab] saw www.other.example/x\n" + "[grab] " + "x" * 300 + "\n")
    steps = worker.last_steps(str(log))
    assert steps[0] == "[grab] MDM running"
    assert steps[1] == "[grab] opened <address> now"
    assert steps[2] == "[grab] saw <address>"
    assert len(steps[3]) == 160
    assert all("example" not in s for s in steps)


def test_last_steps_count_and_missing_file(tmp_path):
    log = tmp_path / "grab.log"
    log.write_text("".join(f"[grab] step {i}\n" for i in range(30)))
    steps = worker.last_steps(str(log), count=5)
    assert steps == [f"[grab] step {i}" for i in range(25, 30)]
    assert worker.last_steps(str(tmp_path / "none.log")) == []


def test_destination_uses_chosen_library_and_sub_section():
    item = {"clean_title": "فيلم", "year": 1950, "content_type": "movie", "is_arabic": True,
            "section_path": "Movies/Arabic_Movies", "category": "أفلام عربية قديمة"}
    folder, name, poster = worker.destination(item)
    assert folder == "m/Movies/Arabic_Movies/أفلام عربية قديمة/فيلم (1950)"
    assert name == "فيلم (1950)" and poster == folder
    # The owner moved it to the foreign library, no sub-section.
    item.update(section_path="Movies/Foreign_Movies", category=None)
    assert worker.destination(item)[0] == "m/Movies/Foreign_Movies/فيلم (1950)"


def test_destination_refuses_unsafe_paths_and_old_controller():
    base = {"clean_title": "Film", "year": 2000, "content_type": "movie", "is_arabic": False}
    for bad in ("../x", "/abs", "Movies/../..", "a/b/c", "Movies/Arabic Movies"):
        assert worker.destination({**base, "section_path": bad})[0] == "m/Movies/Foreign_Movies/Film (2000)"
    for bad in ("..", ".", "...", "../../etc", "a/b"):
        folder = worker.destination({**base, "section_path": "Movies/Foreign_Movies", "category": bad})[0]
        assert ".." not in folder.split("/") and folder.count("/") <= 4
    assert worker.destination(base)[0] == "m/Movies/Foreign_Movies/Film (2000)"


def test_series_never_get_a_sub_section():
    item = {"clean_title": "Show", "content_type": "series", "season": 2, "episode": 3, "is_arabic": False,
            "section_path": "Series/Foreign_Series", "category": "x"}
    folder, name, root = worker.destination(item)
    assert folder == "m/Series/Foreign_Series/Show/Season 02" and root == "m/Series/Foreign_Series/Show"
    assert name == "Show - S02E03"


def test_verify_video_keeps_good_file_and_rewrites_one_without_duration(tmp_path, monkeypatch):
    import shutil
    import subprocess
    import pytest
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg missing")
    monkeypatch.setattr(worker, "MIN_VIDEO_BYTES", 1000)
    good = str(tmp_path / "good.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=8:s=320x240", "-c:v", "mpeg4", good], check=True)
    assert worker.verify_video(good) == good
    # A WebM stream written through a pipe has no duration in its header ("N/A"), like a streamed copy.
    raw = str(tmp_path / "raw.webm")
    with open(raw, "wb") as f:
        subprocess.run(["ffmpeg", "-v", "error", "-i", good, "-c:v", "libvpx", "-f", "webm", "pipe:1"], stdout=f, check=True)
    assert worker.video_seconds(raw) == 0.0
    fixed = worker.verify_video(raw)
    assert fixed.endswith("raw.mkv") and not os.path.exists(raw)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["good.mp4", "raw.mkv"]
    assert 7.5 < worker.video_seconds(fixed) < 8.5

def test_verify_video_refuses_a_non_video(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr(worker, "MIN_VIDEO_BYTES", 10)
    page = tmp_path / "page.mp4"
    page.write_text("<html>not a video</html>" * 10)
    with pytest.raises(RuntimeError, match="not a playable video"):
        worker.verify_video(str(page))
    assert list(tmp_path.iterdir()) == [page]
