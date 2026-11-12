"""`dolly doctor` mentions a left-over python-opencv once, and never when it is not installed."""
from dolly import doctor


def run(monkeypatch, installed):
    real_sh, real_which = doctor.sh, doctor.shutil.which

    def sh(cmd, *a, **k):
        if list(cmd[:3]) == ["pacman", "-Qq", "python-opencv"]:
            return (0, "python-opencv") if installed else (1, "")
        return real_sh(cmd, *a, **k)
    monkeypatch.setattr(doctor, "sh", sh)
    monkeypatch.setattr(doctor.shutil, "which", lambda n, *a, **k: "/usr/bin/pacman" if n == "pacman" else real_which(n, *a, **k))
    return [c for c in doctor.check()["checks"] if "python-opencv" in c["name"]]


def test_left_over_package_is_named_with_the_command(monkeypatch):
    (c,) = run(monkeypatch, True)
    assert c["level"] == "ok" and c["ok"] is True          # information, not a problem
    assert "sudo pacman -Rs python-opencv" in c["detail"] and "does not use it" in c["detail"]


def test_no_line_when_it_is_not_installed(monkeypatch):
    assert run(monkeypatch, False) == []
