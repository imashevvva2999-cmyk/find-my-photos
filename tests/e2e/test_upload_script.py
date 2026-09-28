"""scripts/upload_folder_to_server.py moves a local folder to a server through the organiser API."""
import subprocess
import sys

import pytest
from test_browser import NASA, ROOT, server  # noqa: F401

pytestmark = pytest.mark.e2e


def test_upload_script_creates_event_and_waits_for_faces(server, tmp_path):
    folder = tmp_path / "photos"
    folder.mkdir()
    for name in ("photo_30.jpg", "photo_16.jpg", "photo_40.jpg"):
        (folder / name).write_bytes((NASA / name).read_bytes())
    (folder / "copy of photo_30.jpg").write_bytes((NASA / "photo_30.jpg").read_bytes())  # duplicate
    script = [sys.executable, "scripts/upload_folder_to_server.py", "--server", server]  # no password needed
    out = subprocess.run(script + ["--event-name", "Технокадр тест", str(folder)], cwd=ROOT,
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "3 new · 1 already there · 0 refused" in out.stdout
    assert "Done: 3 photos ready" in out.stdout and "/e/" in out.stdout
    event_id = out.stdout.split("Event ", 1)[1].split(":", 1)[0]
    again = subprocess.run(script + ["--event-id", event_id, str(folder)], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert again.returncode == 0 and "0 new · 4 already there" in again.stdout, again.stdout + again.stderr
