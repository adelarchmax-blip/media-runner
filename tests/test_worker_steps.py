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
