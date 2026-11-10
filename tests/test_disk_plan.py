"""Recording space in minutes, not a fixed 2 GB (VM, Nov 10: an 8 GB VM could not
record at all, and /tmp-style RAM folders were not called out)."""
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GB = 1 << 30


@pytest.fixture()
def health(monkeypatch):
    for k in ("DOLLY_MIN_FREE_GB", "DOLLY_BLOCK_MINUTES", "DOLLY_WARN_MINUTES"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DOLLY_STOP_FREE_MB", "300")
    from dolly import health as h
    h = importlib.reload(h)
    yield h
    importlib.reload(h)


def _plan(h, monkeypatch, free, fs="ext4", rate=None):
    monkeypatch.setattr(h, "disk_free_bytes", lambda p: free)
    monkeypatch.setattr(h, "fs_type", lambda p: (fs, "/home"))
    if rate:
        monkeypatch.setattr(h, "recording_rate", lambda fps=60: (rate, "measured"))
    return h.disk_plan("/home/u/Videos/Dolly", 60)


def test_small_vm_disk_records_with_a_warning(health, monkeypatch):
    p = _plan(health, monkeypatch, int(1.0 * GB))                # 1 GB: was refused (needed 2 GB)
    assert p["level"] == "warn" and 6 < p["minutes"] < 10
    assert "About 9 min of recording left: 1.0 GB free in /home/u/Videos/Dolly (ext4 at /home)." in p["message"]


def test_plenty_of_space_is_quiet(health, monkeypatch):
    p = _plan(health, monkeypatch, 50 * GB)
    assert p["level"] == "ok" and p["message"] == "" and p["minutes"] > 500


def test_almost_full_blocks(health, monkeypatch):
    p = _plan(health, monkeypatch, 400 << 20)                    # 400 MB - 300 MB stop margin
    assert p["level"] == "block" and p["minutes"] < 2 and "record anyway" in p["message"]


def test_measured_rate_from_recent_recordings(health, monkeypatch):
    p = _plan(health, monkeypatch, 3 * GB, rate=10e6)            # a busy 4K screen: 10 MB/s
    assert p["level"] == "warn" and p["rate_source"] == "measured" and p["minutes"] < 5


def test_rate_from_library_cache(health, monkeypatch, tmp_path):
    from dolly import library
    cache = tmp_path / "cache.json"
    monkeypatch.setattr(library, "cache_path", lambda: cache)
    items = [{"raw_bytes": int(r * d), "duration": d} for r, d in ((1e6, 60), (2e6, 30), (3e6, 120), (9e9, 1))]
    cache.write_text(json.dumps({"version": library.INDEX_VERSION, "items": items}))
    rate, src = health.recording_rate()
    assert src == "measured" and rate == 2e6                      # median; the 1 s clip is ignored
    cache.write_text("{}")
    rate, src = health.recording_rate(30)
    assert src == "estimated" and rate == health.FALLBACK_RATE / 2


def test_tmpfs_is_always_called_out(health, monkeypatch):
    p = _plan(health, monkeypatch, 50 * GB, fs="tmpfs")
    assert p["level"] == "warn" and p["tmpfs"] and "in memory (tmpfs)" in p["message"]


def test_fs_type_finds_the_longest_mount(health):
    fs, mnt = health.fs_type("/proc/self")
    assert fs == "proc" and mnt == "/proc"
    fs, mnt = health.fs_type("/definitely/not/here/yet")
    assert mnt == "/"


def _env(tmp_path, **extra):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "notify.log"
    p = bindir / "omarchy-notification-send"
    p.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> "{log}"\necho --- >> "{log}"\n')
    p.chmod(0o755)
    e = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", PYTHONPATH=str(ROOT),
             DOLLY_DIR=str(tmp_path / "videos"), DOLLY_SKIP_DEPS="1", **extra)
    return e, log


def test_block_toast_offers_record_anyway_with_the_same_choices(tmp_path):
    e, log = _env(tmp_path, DOLLY_MIN_FREE_GB="100000")
    r = subprocess.run([sys.executable, "-m", "dolly.cli", "record", "--mode", "region", "--fps", "30",
                        "--countdown", "0"], env=e, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    # the toast is sent from a detached process: give it a moment on a slow machine
    import time
    for _ in range(50):
        if log.exists() and "---" in log.read_text():
            break
        time.sleep(0.1)
    lines = log.read_text().splitlines()
    assert "Not enough disk space to record" in lines
    i = lines.index("Record anyway")
    act = lines[i + 1:lines.index("--action", i)]
    assert act[0].endswith("bin/dolly") and act[1] == "record" and act[-1] == "--ignore-disk"
    pairs = list(zip(act[2:-1:2], act[3:-1:2]))
    assert {("--mode", "region"), ("--fps", "30"), ("--countdown", "0")} <= set(pairs)
    assert "Choose folder" in lines and lines[lines.index("Choose folder") + 2] == "settings"


def test_doctor_reports_minutes(tmp_path):
    e, _ = _env(tmp_path)
    for k in ("DOLLY_BLOCK_MINUTES", "DOLLY_WARN_MINUTES", "DOLLY_MIN_FREE_GB"):
        e.pop(k, None)
    rep = json.loads(subprocess.run([sys.executable, "-m", "dolly.cli", "doctor", "--json"], env=e,
                                    capture_output=True, text=True, timeout=120).stdout)
    c = {c["name"]: c for c in rep["checks"]}["disk space for recordings"]
    assert "of recording at" in c["detail"] and "MB/min (estimated)" in c["detail"]
    assert rep["disk"]["level"] in ("ok", "warn", "block") and rep["disk"]["rate_source"] == "estimated"
