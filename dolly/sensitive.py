"""Windows that hold secrets: password managers, keyrings, authentication prompts.

Pure Python (the event logger imports it): which windows count, when one had focus, and
dropping what was typed into it. The blur boxes themselves are in privacy.py.
"""
from __future__ import annotations

SECRET_CLASSES = ("keepassxc", "keepass", "bitwarden", "1password", "enpass", "proton-pass", "protonpass",
                  "seahorse", "gcr-prompter", "pinentry", "polkit", "policykit", "kwallet", "ssh-askpass",
                  "gnome-keyring", "authenticator", "secrets")
SECRET_TITLES = ("authentication required", "unlock keyring", "unlock login keyring", "enter passphrase",
                 "master password")


def secret_window(cls: str | None, title: str | None = "") -> str | None:
    """A short label when a window is secret-bearing (by class, or by a prompt's title)."""
    c, t = (cls or "").lower(), (title or "").lower()
    for name in SECRET_CLASSES:
        if name in c:
            return (cls or name).rsplit(".", 1)[-1][:24]
    for name in SECRET_TITLES:
        if name in t:
            return "password prompt"
    return None


def secret_focus_spans(snapshots: list[dict], end: float, pad: float = 0.3) -> list[tuple[float, float]]:
    """[(t0, t1)] while a secret-bearing window had keyboard focus. `snapshots` are
    {"t", "focus", "windows": [{"address", "class", "title", ...}]} in time order."""
    spans, t0 = [], None
    for s in snapshots:
        w = next((w for w in s.get("windows", []) if w.get("address") == s.get("focus")), None)
        on = bool(w and secret_window(w.get("class"), w.get("title")))
        if on and t0 is None:
            t0 = s["t"]
        elif not on and t0 is not None:
            spans.append((max(0.0, t0 - pad), s["t"] + pad))
            t0 = None
    if t0 is not None:
        spans.append((max(0.0, t0 - pad), end))
    return spans


def strip_secret_keys(events: list[dict], end: float) -> tuple[list[dict], int]:
    """Drop key events typed while a secret-bearing window had focus (a password must never
    reach events.json, the shortcut overlay or the typing zoom). -> (events, dropped)."""
    snaps = [e for e in events if e.get("type") == "clients"]
    spans = secret_focus_spans(snaps, end)
    if not spans:
        return events, 0
    keep = [e for e in events if not (e.get("type") == "key" and any(a <= e["t"] <= b for a, b in spans))]
    return keep, len(events) - len(keep)
