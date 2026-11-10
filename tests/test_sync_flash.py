"""Sync marker in two steps (VM, Nov 10): under QT_QUICK_BACKEND=software a layer surface
mapped and lit at once was never painted (layer open ~70 ms, 0 of 632 frames). The
logger now arms (maps transparent), waits, then flashes (colour change only), longer on
software rendering. An older bar widget without syncArm still gets a flash."""
import json
import stat
import time

import pytest

from dolly import logger as lg


class FakeLogger:
    region = [100, 50, 800, 600]

    def __init__(self):
        self.out = []
        self.t0 = time.monotonic()

    def ms(self):
        return (time.monotonic() - self.t0) * 1000

    def write(self, ev):
        self.out.append(ev)


def _shell(tmp_path, monkeypatch, arm_body):
    log = tmp_path / "calls.log"
    p = tmp_path / "bin" / "omarchy-shell"
    p.parent.mkdir()
    p.write_text("#!/usr/bin/env bash\n"
                 f'echo "$2 $3" >> "{log}"\n'
                 f'if [[ "$2" == syncArm ]]; then {arm_body}; fi\n'
                 'echo ok\n')
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{p.parent}:" + __import__("os").environ["PATH"])
    monkeypatch.setattr(lg, "ARM_SETTLE_S", 0.05)
    return log


@pytest.mark.parametrize("backend,ms", [("", 70), ("software", 160)])
def test_arm_then_flash(tmp_path, monkeypatch, backend, ms):
    monkeypatch.delenv("DOLLY_SYNC_FLASH_MS", raising=False)
    monkeypatch.delenv("LIBGL_ALWAYS_SOFTWARE", raising=False)
    monkeypatch.delenv("QSG_RHI_BACKEND", raising=False)
    monkeypatch.setenv("QT_QUICK_BACKEND", backend)
    log = _shell(tmp_path, monkeypatch, "echo ok; exit 0")
    f = FakeLogger()
    assert lg.Logger.sync_flash(f, "start") is True
    calls = [l.split(" ", 1) for l in log.read_text().splitlines()]
    assert [c[0] for c in calls] == ["syncArm", "syncFlash"]
    assert json.loads(calls[0][1]) == {"x": 100, "y": 50, "size": 40}
    flash = json.loads(calls[1][1])
    assert flash["disarm"] is True and flash["ms"] == ms and (flash["x"], flash["y"]) == (100, 50)
    ev = f.out[-1]
    assert ev["type"] == "sync" and ev["flash_ms"] == ms
    # the logged time is the flash request, after the arm settle, not the arm request
    assert ev["ms"] >= 50 * 0.9


def test_old_bar_widget_without_arm_still_flashes(tmp_path, monkeypatch):
    log = _shell(tmp_path, monkeypatch, 'echo "Function syncArm not found" >&2; exit 1')
    f = FakeLogger()
    t = time.monotonic()
    assert lg.Logger.sync_flash(f, "stop") is True
    assert time.monotonic() - t < 1.0
    assert [l.split()[0] for l in log.read_text().splitlines()] == ["syncArm", "syncFlash"]
    assert f.out[-1]["type"] == "sync"


def test_flash_ms_override(monkeypatch):
    monkeypatch.setenv("DOLLY_SYNC_FLASH_MS", "5000")
    assert lg.flash_ms() == 1000
    monkeypatch.setenv("DOLLY_SYNC_FLASH_MS", "120")
    assert lg.flash_ms() == 120


def test_bar_widget_exposes_arm_and_disarm():
    from pathlib import Path
    qml = (Path(lg.__file__).resolve().parents[1] / "BarWidget.qml").read_text()
    for f in ("function syncArm(arg: string)", "function syncDisarm()", "function syncFlash(arg: string)",
              'color: lit ? "#ff00ff" : "transparent"', "flashDisarm"):
        assert f in qml, f
