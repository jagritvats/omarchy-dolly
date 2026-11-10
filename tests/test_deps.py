"""Fresh-install paths (found on an Omarchy 4.0.4 VirtualBox VM, Nov 10):
- `dolly doctor` must report missing python-numpy/opencv/pillow, not crash on them
- gpu-screen-recorder that cannot capture on the GPU ("unknown gpu vendor: VMware")
  makes wf-recorder a missing package
- recording with packages missing stops before the picker/DND and says what to
  install, with an Install button that runs `dolly deps --install`
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


def test_doctor_reports_missing_python_packages_instead_of_crashing(env):
    r = run(env, "doctor", "--json", site=False)       # -S: no site-packages, so no numpy/cv2/PIL
    assert "Traceback" not in r.stderr, r.stderr
    rep = json.loads(r.stdout)
    assert rep["ok"] is False
    pk = rep["missing_packages"]["packages"]
    assert {"python-numpy", "python-opencv", "python-pillow"} <= set(pk)
    names = {c["name"]: c for c in rep["checks"]}
    assert names["python: cv2"]["ok"] is False and "dolly deps --install" in names["python: cv2"]["detail"]
    r = run(env, "doctor", site=False)
    assert "Missing packages:" in r.stdout and "dolly deps --install" in r.stdout


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


def test_record_with_packages_missing_says_what_to_install(env, tmp_path):
    r = run(env, "record", "--mode", "screen", "--countdown", "0")
    assert r.returncode != 0
    assert "wf-recorder" in r.stderr and "dolly deps --install" in r.stderr
    log = (tmp_path / "notify.log").read_text()
    assert "Dolly needs a few packages first" in log
    assert "gpu-screen-recorder cannot capture here (unknown gpu vendor)" in log
    lines = log.splitlines()
    i = lines.index("--action")
    assert lines[i + 1] == "Install" and lines[i + 2].endswith("bin/dolly") and lines[i + 3:i + 5] == ["deps", "--install"]
    # stopped before anything changed: no recording folder, DND never touched
    assert not (tmp_path / "videos").exists() or not any((tmp_path / "videos").iterdir())
    shell = (tmp_path / "shell.log").read_text() if (tmp_path / "shell.log").exists() else ""
    assert "dnd" not in shell.lower() and "silenc" not in shell.lower()


def test_deps_install_opens_omarchy_terminal(env, tmp_path):
    bindir = tmp_path / "bin"
    _stub(bindir, "omarchy-pkg-add", "#!/usr/bin/env bash\nexit 0\n")
    _stub(bindir, "omarchy-launch-floating-terminal-with-presentation",
          '#!/usr/bin/env bash\nprintf "%s" "$1" > "$HOME/term-cmd"\n')
    r = run(env, "deps", "--install", "--json")
    out = json.loads(r.stdout)
    assert out["launched"] is True and out["packages"] == ["wf-recorder"]
    import time
    for _ in range(50):
        if (tmp_path / "term-cmd").exists():
            break
        time.sleep(0.1)
    cmd = (tmp_path / "term-cmd").read_text()
    assert cmd.startswith("omarchy-pkg-add wf-recorder && ") and cmd.endswith("bin/dolly doctor")


def test_help_has_no_internal_or_suppressed_entries(env):
    out = run(env, "--help").stdout
    assert "SUPPRESS" not in out and "log-events" not in out and "deps" in out


def test_dolly_setup_prints_the_command_when_not_in_a_terminal(env, tmp_path):
    (tmp_path / ".config" / "hypr").mkdir(parents=True)
    (tmp_path / ".config" / "hypr" / "bindings.lua").write_text("-- user binds\n")
    r = subprocess.run(["bash", str(ROOT / "bin" / "dolly-setup")], env=env, capture_output=True, text=True,
                       timeout=120, stdin=subprocess.DEVNULL)
    assert "packages: missing wf-recorder" in r.stdout
    assert "not asking (no terminal)" in r.stdout and "deps --install" in r.stdout
    assert "[y/N]" not in r.stdout and "[Y/n" not in r.stdout


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
