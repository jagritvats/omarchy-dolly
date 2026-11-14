"""Fresh-install paths (found on an Omarchy 4.0.4 VirtualBox VM, Nov 10):
- `dolly doctor` must report a missing python-cairo, not crash on it
- gpu-screen-recorder that cannot capture on the GPU ("unknown gpu vendor: VMware")
  makes wf-recorder a missing package
- recording with a recorder missing stops before the picker/DND and says which package
  is missing and where to install it; there is no Install button: Dolly installs nothing
"""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GSR_VMWARE = ('#!/usr/bin/env bash\necho "gsr error: unknown gpu vendor: VMware, Inc." >&2\n'
              'echo "gsr error: failed to load opengl" >&2\nexit 1\n')
NOTIFY = '#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> "$NOTIFY_LOG"\necho --- >> "$NOTIFY_LOG"\n'
SHELL = '#!/usr/bin/env bash\necho "$*" >> "$SHELL_LOG"\necho false\n'


def _stub(bindir: Path, name: str, body: str):
    p = bindir / name
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


@pytest.fixture()
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "gpu-screen-recorder", GSR_VMWARE)
    _stub(bindir, "omarchy-notification-send", NOTIFY)
    _stub(bindir, "omarchy-shell", SHELL)
    # a PATH without wf-recorder (directories that have it are mirrored minus it)
    from tests._pathutil import path_without
    path = path_without("wf-recorder", tmp_path)
    e = dict(os.environ)
    e.update({"PATH": f"{bindir}:{path}", "HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state"),
              "XDG_RUNTIME_DIR": str(tmp_path / "run"), "DOLLY_DIR": str(tmp_path / "videos"),
              "PYTHONPATH": str(ROOT), "WAYLAND_DISPLAY": "wayland-test", "NOTIFY_LOG": str(tmp_path / "notify.log"),
              "SHELL_LOG": str(tmp_path / "shell.log"), "DOLLY_OPEN_EDITOR": "0"})
    for k in ("DOLLY_BACKEND", "DOLLY_SKIP_DEPS", "HYPRLAND_INSTANCE_SIGNATURE"):
        e.pop(k, None)
    (tmp_path / "run").mkdir()
    return e


def run(env, *args, site=True):
    cmd = [sys.executable] + ([] if site else ["-S"]) + ["-m", "dolly.cli", *args]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)


def test_doctor_reports_a_missing_cairo_instead_of_crashing(env):
    r = run(env, "doctor", "--json", site=False)       # -S: no site-packages, so no cairo
    assert "Traceback" not in r.stderr, r.stderr
    rep = json.loads(r.stdout)
    assert rep["ok"] is False                          # the recorder cannot capture here (VMware stub)
    pk = rep["missing_packages"]["packages"]
    assert pk == ["python-cairo", "wf-recorder"]
    names = {c["name"]: c for c in rep["checks"]}
    row = names["python: cairo"]
    assert row["ok"] is False and row["level"] == "warn" and "Omarchy menu > Install > Package" in row["detail"]
    assert not any(n.startswith(("python: numpy", "python: PIL", "python: cv2")) for n in names)
    r = run(env, "doctor", site=False)
    assert "Missing packages:" in r.stdout and "Dolly installs nothing itself" in r.stdout
    assert "deps --install" not in r.stdout


def test_gsr_that_cannot_capture_makes_wf_recorder_missing(env, tmp_path):
    r = run(env, "deps", "--json")
    m = json.loads(r.stdout)
    assert m["packages"] == ["wf-recorder"]
    assert "unknown gpu vendor" in m["why"]["wf-recorder"]
    note = json.loads((tmp_path / "state" / "dolly" / "gsr-unusable.json").read_text())
    assert note["reason"] == "unknown gpu vendor"
    # cached: a gsr that now works is not probed again until it is reinstalled (mtime)
    gsr = tmp_path / "bin" / "gpu-screen-recorder"
    st = gsr.stat()
    gsr.write_text("#!/usr/bin/env bash\necho ok\n")
    os.utime(gsr, (st.st_atime, st.st_mtime))
    assert json.loads(run(env, "deps", "--json").stdout)["packages"] == ["wf-recorder"]
    os.utime(gsr, (st.st_atime, st.st_mtime + 60))           # "updated gsr": probed again, usable
    assert json.loads(run(env, "deps", "--json").stdout)["packages"] == []
    # doctor agrees
    rep = json.loads(run(env, "doctor", "--json").stdout)
    assert rep["missing_packages"]["packages"] == []


def test_doctor_marks_unusable_gsr(env):
    rep = json.loads(run(env, "doctor", "--json").stdout)
    c = {c["name"]: c for c in rep["checks"]}
    assert c["gpu-screen-recorder can capture on this GPU"]["ok"] is False
    assert c["a capture backend"]["ok"] is False and "wf-recorder" in c["a capture backend"]["detail"]


def test_record_with_a_recorder_missing_says_what_to_install(env, tmp_path):
    r = run(env, "record", "--mode", "screen", "--countdown", "0")
    assert r.returncode != 0
    assert "missing packages: wf-recorder (install it with Omarchy menu > Install > Package)" in r.stderr
    import time
    notes = tmp_path / "notify.log"
    for _ in range(50):                        # the card is sent by a child process
        if notes.exists() and "---" in notes.read_text():
            break
        time.sleep(0.1)
    log = notes.read_text()
    assert "Dolly cannot record here yet" in log
    assert "gpu-screen-recorder cannot capture here (unknown gpu vendor)" in log
    assert "Omarchy's package installer (Omarchy menu > Install > Package)" in log
    assert "--action" not in log.splitlines() and "--install" not in log    # nothing to click that installs
    # stopped before anything changed: no recording folder, DND never touched
    assert not (tmp_path / "videos").exists() or not any((tmp_path / "videos").iterdir())
    shell = (tmp_path / "shell.log").read_text() if (tmp_path / "shell.log").exists() else ""
    assert "dnd" not in shell.lower() and "silenc" not in shell.lower()


def test_deps_names_the_packages_and_opens_nothing(env, tmp_path):
    _stub(tmp_path / "bin", "omarchy-launch-floating-terminal-with-presentation",
          '#!/usr/bin/env bash\nprintf "%s" "$1" > "$HOME/term-cmd"\n')
    r = run(env, "deps")
    assert r.returncode == 0 and r.stdout.startswith("wf-recorder ") and "unknown gpu vendor" in r.stdout
    assert "install these with Omarchy's package installer (Omarchy menu > Install > Package)" in r.stdout
    assert not (tmp_path / "term-cmd").exists()


def test_help_has_no_internal_or_suppressed_entries(env):
    out = run(env, "--help").stdout
    assert "SUPPRESS" not in out and "log-events" not in out and "deps" in out


def test_dolly_setup_reports_what_is_missing_and_installs_nothing(env, tmp_path):
    (tmp_path / ".config" / "hypr").mkdir(parents=True)
    (tmp_path / ".config" / "hypr" / "bindings.lua").write_text("-- user binds\n")
    r = subprocess.run(["bash", str(ROOT / "bin" / "dolly-setup")], env=env, capture_output=True, text=True,
                       timeout=120, stdin=subprocess.DEVNULL)
    assert "packages: wf-recorder " in r.stdout and "unknown gpu vendor" in r.stdout
    assert "packages: Dolly installs nothing itself" in r.stdout and "Omarchy menu > Install > Package" in r.stdout
    assert "[y/N]" not in r.stdout and "[Y/n" not in r.stdout and "--install" not in r.stdout


def test_gsr_info_that_exits_0_but_cannot_capture(env, tmp_path):
    # gsr 6.1 on VirtualBox: --info prints the vendor error yet exits 0 (VM, Nov 10)
    _stub(tmp_path / "bin", "gpu-screen-recorder",
          '#!/usr/bin/env bash\necho "gsr error: unknown gpu vendor: VMware, Inc." >&2\nexit 0\n')
    m = json.loads(run(env, "deps", "--json").stdout)
    assert m["packages"] == ["wf-recorder"]


def test_doctor_summary_uses_wf_when_gsr_cannot_capture(env, tmp_path):
    _stub(tmp_path / "bin", "wf-recorder", "#!/usr/bin/env bash\nexit 0\n")
    rep = json.loads(run(env, "doctor", "--json").stdout)
    c = {c["name"]: c for c in rep["checks"]}
    assert c["a capture backend"]["ok"] is True and c["a capture backend"]["detail"] == "wf-recorder"
    assert rep["paths"]["capture"].startswith("wf:") and "cannot capture here" in rep["paths"]["capture"]
    assert rep["paths"]["pause"].startswith("unavailable") and rep["paths"]["replay"].startswith("unavailable")
    assert rep["missing_packages"]["packages"] == []
