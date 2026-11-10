"""VirtualBox VMSVGA (vmwgfx): doctor names it and mpv gets software GL (VM, Nov 10)."""
import importlib
import os

import pytest


def _card(drm, name, driver, vendor):
    dev = drm / name / "device"
    dev.mkdir(parents=True)
    (dev / "vendor").write_text(vendor + "\n")
    drvdir = drm.parent / "drivers" / driver
    drvdir.mkdir(parents=True, exist_ok=True)
    os.symlink(drvdir, dev / "driver")


@pytest.fixture()
def health(monkeypatch, tmp_path):
    monkeypatch.setenv("DOLLY_SYSFS_DRM", str(tmp_path / "drm"))
    (tmp_path / "drm").mkdir()
    monkeypatch.delenv("DOLLY_SOFTWARE_GL", raising=False)
    monkeypatch.delenv("LIBGL_ALWAYS_SOFTWARE", raising=False)
    from dolly import health as h
    return importlib.reload(h)


def test_vmwgfx_is_virtual_and_gets_software_gl(health, tmp_path):
    _card(tmp_path / "drm", "card0", "vmwgfx", "0x15ad")
    (tmp_path / "drm" / "card0-Virtual-1").mkdir()          # connectors are ignored
    assert health.virtual_gpu() == {"driver": "vmwgfx", "name": "VMware SVGA (VirtualBox/VMware)"}
    assert health.software_gl_env({})["LIBGL_ALWAYS_SOFTWARE"] == "1"
    assert "LIBGL_ALWAYS_SOFTWARE" not in health.software_gl_env({"DOLLY_SOFTWARE_GL": "0"})


def test_real_gpu_is_left_alone(health, tmp_path):
    _card(tmp_path / "drm", "card0", "amdgpu", "0x1002")
    assert health.virtual_gpu() is None
    assert "LIBGL_ALWAYS_SOFTWARE" not in health.software_gl_env({})
    assert health.software_gl_env({"DOLLY_SOFTWARE_GL": "1"})["LIBGL_ALWAYS_SOFTWARE"] == "1"


def test_virtual_plus_real_gpu_is_not_a_vm(health, tmp_path):
    _card(tmp_path / "drm", "card0", "virtio_gpu", "0x1af4")
    _card(tmp_path / "drm", "card1", "i915", "0x8086")
    assert health.virtual_gpu() is None


def test_no_drm_at_all(health, monkeypatch, tmp_path):
    monkeypatch.setenv("DOLLY_SYSFS_DRM", str(tmp_path / "missing"))
    assert health.virtual_gpu() is None


def test_doctor_names_the_virtual_gpu(health, tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path
    _card(tmp_path / "drm", "card0", "vmwgfx", "0x15ad")
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, "-m", "dolly.cli", "doctor", "--json"], capture_output=True, text=True,
                       timeout=120, env=dict(os.environ, PYTHONPATH=str(root)))
    rep = json.loads(r.stdout)
    assert rep["virtual_gpu"]["driver"] == "vmwgfx"
    c = {c["name"]: c for c in rep["checks"]}["virtual GPU"]
    assert "LIBGL_ALWAYS_SOFTWARE=1" in c["detail"] and "wf-recorder" in c["detail"]
