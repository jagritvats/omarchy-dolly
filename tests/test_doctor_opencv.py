"""`dolly doctor` no longer asks the package manager about a left-over python-opencv (it
did from 0.7.0 to 0.7.3, and printed the removal command). Dolly runs no package tool,
not even to read what is installed."""
from dolly import doctor


def test_doctor_runs_no_package_tool_and_names_no_opencv(monkeypatch):
    real_sh, ran = doctor.sh, []

    def sh(cmd, *a, **k):
        ran.append(list(cmd))
        return real_sh(cmd, *a, **k)
    monkeypatch.setattr(doctor, "sh", sh)
    checks = doctor.check()["checks"]
    assert ran and not [c for c in ran if c[0] in ("pacman", "paru", "yay", "pip", "sudo", "pkexec")]
    assert not [c for c in checks if "opencv" in c["name"].lower() or "opencv" in c["detail"].lower()]
