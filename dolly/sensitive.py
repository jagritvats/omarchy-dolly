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


# ------------------------------------------------------------------ automatic (privacy by default)
# Things that should never end up in a demo, decided from the event log with no settings:
# windows of apps whose whole job is holding secrets, authentication prompts, and
# notification toasts. Each becomes an ordinary box (source "auto") in the editor's privacy
# lane, removable like any other; they are computed once, when the recording is finalized.


def _window_boxes(tl, pad: float = 6.0, lead: float = 0.25, settle: float = 1.0) -> list[dict]:
    """One box per secret-bearing window while it is on screen; a window being moved or
    resized is covered by the union of where it was (snapshots are 10 Hz)."""
    W, H, runs, open_ = tl.w, tl.h, [], {}
    snaps = tl.snapshots
    for i, s in enumerate(snaps):
        seen = set()
        for w in s["windows"]:
            label = secret_window(w.get("class"), w.get("title"))
            if not label:
                continue
            a, r = w["address"], tuple(w["rect"])
            seen.add(a)
            run = open_.get(a)
            if run is None:
                open_[a] = {"start": s["t"], "rect": r, "last": r, "changed": s["t"], "label": label}
            elif r != run["last"]:
                if s["t"] - run["changed"] > settle:          # it had settled: a new box where it now is
                    runs.append({**run, "end": s["t"]})
                    open_[a] = {"start": s["t"], "rect": r, "last": r, "changed": s["t"], "label": label}
                else:                                         # still moving: cover the whole path
                    run["rect"] = (min(run["rect"][0], r[0]), min(run["rect"][1], r[1]),
                                   max(run["rect"][2], r[2]), max(run["rect"][3], r[3]))
                    run["last"], run["changed"], run["moved"] = r, s["t"], True
            elif run.get("moved") and s["t"] - run["changed"] > settle:
                runs.append({**run, "end": s["t"]})           # it came to rest: back to just the window
                open_[a] = {"start": s["t"], "rect": r, "last": r, "changed": s["t"], "label": label}
        for a in [a for a in open_ if a not in seen]:
            runs.append({**open_.pop(a), "end": s["t"]})
    for run in open_.values():
        runs.append({**run, "end": tl.duration})
    out = []
    for run in runs:
        x0, y0, x1, y1 = run["rect"]
        x0, y0, x1, y1 = max(0.0, x0 - pad), max(0.0, y0 - pad), min(W, x1 + pad), min(H, y1 + pad)
        if x1 - x0 < 8 or y1 - y0 < 8:
            continue                                          # not on the recorded area
        out.append({"start": round(max(0.0, run["start"] - lead), 3), "end": round(min(tl.duration, run["end"] + 0.1), 3),
                    "rect": [round(x0, 1), round(y0, 1), round(x1 - x0, 1), round(y1 - y0, 1)],
                    "label": run["label"]})
    return out


START_LINGER = 1.5      # s a card that was up when Record was pressed can outlast the shell's count


def toast_spans(tl) -> list[tuple[float, float, int]]:
    """[(t0, t1, toasts on screen)] from the shell's own count, which the logger records on
    change (including what is already there when the recording starts).

    Two things the logger cannot see by itself come from the capture facts:
    - `toasts_before`: cards on screen when Record was pressed. A card that expires while
      the recorder starts is already gone from the count at the logger's first sample, yet
      its pixels are still in the first frames (found Nov 11). Those cards are taken to be
      there from the first frame until START_LINGER after that first sample.
    - `go`: the end of the countdown when it was recorded too (pre-roll). What was gone
      before it is not in the take, so it gets no box. The logger's count lags by up to
      its polling interval, so the count read at that instant (`toasts_at_go`) decides
      what is on screen from "go" until the logger's next change: Dolly's own last
      countdown card, taken down just before, must not leave a box on an empty corner."""
    cap = getattr(tl, "capture", None) or {}
    evs = [{"t": float(e["t"]), "count": int(e["count"])} for e in getattr(tl, "toasts", [])]
    go = float(cap["go"]) if cap.get("go") else None
    at_go = cap.get("toasts_at_go") if go is not None else None

    def counted(t: float) -> int:             # the first count is what was already there
        if at_go is not None and t >= go:
            c = int(at_go)
            for e in evs:
                if go < e["t"] <= t:
                    c = e["count"]
            return c
        c = evs[0]["count"] if evs else 0
        for e in evs:
            if e["t"] <= t:
                c = e["count"]
        return c
    before = int(cap.get("toasts_before") or 0)
    until = 0.0
    if before > 0 and counted(0.0) < before:
        until = (evs[0]["t"] if evs else 0.0) + START_LINGER
    edges = sorted({0.0, until, *(e["t"] for e in evs), *([go] if go is not None else [])})
    spans = []
    for i, t0 in enumerate(edges):
        t1 = edges[i + 1] if i + 1 < len(edges) else tl.duration
        n = max(counted(t0), before if t0 < until else 0)
        if n > 0 and t1 > t0:
            if spans and spans[-1][2] == n and abs(spans[-1][1] - t0) < 1e-9:
                spans[-1] = (spans[-1][0], t1, n)
            else:
                spans.append((t0, t1, n))
    if go is not None:
        spans = [(max(t0, go), t1, n) for t0, t1, n in spans if t1 > go]
    return spans


def _toast_boxes(tl, areas, lead: float = 0.8, tail: float = 0.5) -> list[dict]:
    """One box per stretch with the same number of toasts. Hyprland only knows the
    notification area (a click-through column that stays mapped), not each card, so the box
    is the top of that area, where the deck grows from, tall enough for that many cards
    with action buttons. It errs on the large side."""
    W, H = tl.w, tl.h
    vs = float(tl.capture.get("scale", 1.0) or 1.0)           # video px per logical px
    spans, boxes = toast_spans(tl), []
    for i, (t0, t1, n) in enumerate(spans):
        rose = i == 0 or spans[i - 1][1] < t0 or spans[i - 1][2] < n      # a card slid in: start early
        fell = i + 1 == len(spans) or spans[i + 1][0] > t1 or spans[i + 1][2] < n
        for ax0, ay0, ax1, ay1 in areas:
            x0, y0, x1, y1 = max(0.0, ax0), max(0.0, ay0), min(W, ax1), min(H, ay1, ay0 + (60 + 150 * n) * vs)
            if x1 - x0 < 8 or y1 - y0 < 8:
                continue                                      # that area is not in this recording
            boxes.append({"start": round(max(0.0, t0 - (lead if rose else 0.0)), 3),
                          "end": round(min(tl.duration, t1 + (tail if fell else 0.0)), 3),
                          "rect": [round(x0, 1), round(y0, 1), round(x1 - x0, 1), round(y1 - y0, 1)],
                          "label": "notification" if n == 1 else f"{n} notifications"})
    return boxes


def auto_boxes(tl) -> list[dict]:
    """Boxes (source "auto") for secret-bearing windows and notification toasts."""
    areas = [tuple(a) for a in (tl.capture.get("notification_areas") or [])]
    found = _window_boxes(tl) + _toast_boxes(tl, areas)
    found.sort(key=lambda b: b["start"])
    return [{"id": f"a{i + 1}", "mode": "blur", "source": "auto", "enabled": True, **b} for i, b in enumerate(found)]
