"""Privacy: blur / pixelate boxes and OCR-based secret scrubbing.

Boxes live in project.json as
    {"id": "b1", "start": s, "end": e, "rect": [x, y, w, h] (video px),
     "mode": "blur" | "pixelate", "source": "manual" | "secret", "label": "..."}
and are applied to the *source* frame before compositing, so they stay glued
to the content through zooms, reframes and speed ramps.

scan_secrets() samples frames, runs tesseract (CLI, TSV output), matches the
recognised text against token/key/e-mail patterns and merges hits across
samples into time-spanning boxes. Each box covers from the previous clean
sample to the next one, so a secret is never visible between samples.
"""
from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np

# ------------------------------------------------------------------ apply
def apply_boxes(frame: np.ndarray, boxes: list[dict], t: float) -> np.ndarray:
    active = [b for b in boxes or [] if b.get("enabled", True) and b["start"] <= t <= b["end"]]
    if not active:
        return frame
    out = frame.copy()
    H, W = out.shape[:2]
    for b in active:
        x, y, w, h = (int(round(v)) for v in b["rect"])
        x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        roi = out[y0:y1, x0:x1]
        if b.get("mode", "blur") == "pixelate":
            cell = max(6, int(min(x1 - x0, y1 - y0) / 6))
            small = cv2.resize(roi, (max(1, (x1 - x0) // cell), max(1, (y1 - y0) // cell)), interpolation=cv2.INTER_AREA)
            out[y0:y1, x0:x1] = cv2.resize(small, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST)
        else:
            # downscale-blur-upscale: strong, cheap, irreversible blur
            f = 8
            small = cv2.resize(roi, (max(1, (x1 - x0) // f), max(1, (y1 - y0) // f)), interpolation=cv2.INTER_AREA)
            small = cv2.GaussianBlur(small, (0, 0), 2.5)
            out[y0:y1, x0:x1] = cv2.resize(small, (x1 - x0, y1 - y0), interpolation=cv2.INTER_CUBIC)
    return out


# ------------------------------------------------------------------ detect
PATTERNS = [
    ("aws-key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("stripe-key", re.compile(r"\b[rs]k_(live|test)_[A-Za-z0-9]{16,}\b")),
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")),
]
ASSIGN = re.compile(r"(?i)\b[A-Z0-9_]*(api[_-]?key|secret|token|passw(or)?d|auth|credential|private[_-]?key)"
                    r"[A-Z0-9_]*\s*[=:]\s*['\"]?(?P<val>[^\s'\"]{8,})")
TOKENISH = re.compile(r"[A-Za-z0-9_\-+/=]{20,}")


def entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = {c: s.count(c) for c in set(s)}
    return -sum(n / len(s) * math.log2(n / len(s)) for n in counts.values())


def ascii_fold(text: str) -> str:
    """OCR often turns 6 into é, l into |, etc.; fold to ASCII 1:1 so spans stay aligned."""
    import unicodedata
    out = []
    for ch in text:
        if ord(ch) < 128:
            out.append(ch)
        else:
            d = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode()
            out.append(d[:1] if d else "x")
    return "".join(out)


def find_secrets(text: str) -> list[tuple[int, int, str]]:
    """(start, end, kind) spans in a line of text."""
    text = ascii_fold(text)
    hits = []
    for kind, rx in PATTERNS:
        for m in rx.finditer(text):
            hits.append((m.start(), m.end(), kind))
    for m in ASSIGN.finditer(text):
        hits.append((m.start("val"), m.end("val"), "assignment"))
    for m in TOKENISH.finditer(text):
        tok = m.group(0)
        classes = sum(bool(re.search(p, tok)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]"))
        if entropy(tok) >= 3.5 and classes >= 2 and not tok.startswith(("http", "/")):
            hits.append((m.start(), m.end(), "high-entropy"))
    # merge overlaps
    hits.sort()
    merged: list[list] = []
    for s, e, k in hits:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
            if merged[-1][2] in ("high-entropy", "assignment"):
                merged[-1][2] = k
        else:
            merged.append([s, e, k])
    return [tuple(x) for x in merged]


def ocr_words(img: np.ndarray) -> list[dict]:
    """tesseract TSV -> words with boxes, grouped by line key."""
    exe = shutil.which("tesseract")
    if not exe:
        raise RuntimeError("tesseract not installed (sudo pacman -S tesseract tesseract-data-eng)")
    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    outs = []
    # two passes: as-is and inverted (tesseract reads dark-on-light best; terminals are light-on-dark)
    for variant in (gray, 255 - gray):
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            cv2.imwrite(f.name, variant)
            outs.append(subprocess.run([exe, f.name, "-", "--psm", "11", "tsv"], capture_output=True, text=True,
                                       timeout=120).stdout)
    words = []
    for pi, out in enumerate(outs):
        words += _parse_tsv(out, pi)
    return words


def _parse_tsv(out: str, pass_id: int) -> list[dict]:
    words = []
    for line in out.splitlines()[1:]:
        p = line.split("\t")
        if len(p) < 12 or not p[11].strip():
            continue
        try:
            conf = float(p[10])
        except ValueError:
            continue
        if conf < 0:
            continue
        words.append({"line": (pass_id, p[2], p[3], p[4]), "x": int(p[6]), "y": int(p[7]), "w": int(p[8]), "h": int(p[9]),
                      "text": p[11]})
    return words


def detect_in_frame(img: np.ndarray, scale: float = 1.0) -> list[dict]:
    """Secret boxes (video px) in one frame. `scale` = video px per OCR image px."""
    words = ocr_words(img)
    lines: dict = {}
    for w in words:
        lines.setdefault(w["line"], []).append(w)
    found = []
    for ws in lines.values():
        ws.sort(key=lambda w: w["x"])
        text, spans = "", []
        for w in ws:
            if text:
                text += " "
            spans.append((len(text), len(text) + len(w["text"]), w))
            text += w["text"]
        for s, e, kind in find_secrets(text):
            cover = [w for (a, b, w) in spans if a < e and b > s]
            if not cover:
                continue
            x0 = min(w["x"] for w in cover)
            y0 = min(w["y"] for w in cover)
            x1 = max(w["x"] + w["w"] for w in cover)
            y1 = max(w["y"] + w["h"] for w in cover)
            pad = max(4, int(0.25 * (y1 - y0)))
            found.append({"rect": [(x0 - pad) * scale, (y0 - pad) * scale, (x1 - x0 + 2 * pad) * scale,
                                   (y1 - y0 + 2 * pad) * scale], "kind": kind, "text": text[s:e]})
    return found


def _iou(a, b):
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ix = max(0, min(ax0 + aw, bx0 + bw) - max(ax0, bx0))
    iy = max(0, min(ay0 + ah, by0 + bh) - max(ay0, by0))
    inter = ix * iy
    return inter / (aw * ah + bw * bh - inter + 1e-9)


def redact(s: str) -> str:
    return s[:3] + "…" + s[-2:] if len(s) > 8 else "…"


def scan_secrets(raw: Path, duration: float, every: float = 1.0, progress=None, frames=None) -> list[dict]:
    """Returns privacy boxes (source='secret'). `frames` (iterable of (t, img)) overrides decoding (tests)."""
    samples = []
    if frames is None:
        frames = _sample_frames(raw, every)
    last_img, last_hits = None, []
    for i, (t, img) in enumerate(frames):
        # skip OCR when the frame barely changed since the last OCR'd one
        if last_img is not None and img.shape == last_img.shape and \
                np.abs(img.astype(np.int16) - last_img.astype(np.int16)).mean() < 0.4:
            hits = last_hits
        else:
            hits = detect_in_frame(img)
            last_img, last_hits = img, hits
        samples.append((t, hits))
        if progress:
            progress(i + 1, None)
    # merge hits across samples into tracks
    tracks: list[dict] = []
    times = [t for t, _ in samples]
    for idx, (t, hits) in enumerate(samples):
        # dedupe the two OCR passes
        uniq = []
        for h in hits:
            if not any(_iou(h["rect"], u["rect"]) > 0.3 for u in uniq):
                uniq.append(h)
        for h in uniq:
            # join any earlier track at the same place even across missed samples: OCR is
            # flaky, and leaving a secret visible between two detections is the worse error
            tr = next((x for x in tracks if x["last_idx"] < idx and _iou(x["rect"], h["rect"]) > 0.3), None)
            if tr:
                # grow to the union so moving/growing text stays covered
                x0 = min(tr["rect"][0], h["rect"][0]); y0 = min(tr["rect"][1], h["rect"][1])
                x1 = max(tr["rect"][0] + tr["rect"][2], h["rect"][0] + h["rect"][2])
                y1 = max(tr["rect"][1] + tr["rect"][3], h["rect"][1] + h["rect"][3])
                tr["rect"] = [x0, y0, x1 - x0, y1 - y0]
                tr["last_idx"] = idx
            else:
                tracks.append({"first_idx": idx, "last_idx": idx, "rect": list(h["rect"]), "kind": h["kind"],
                               "text": h["text"]})
    boxes = []
    for i, tr in enumerate(tracks):
        start = times[tr["first_idx"] - 1] if tr["first_idx"] > 0 else 0.0
        end = times[tr["last_idx"] + 1] if tr["last_idx"] + 1 < len(times) else duration
        boxes.append({"id": f"p{i + 1}", "start": round(start, 3), "end": round(end, 3),
                      "rect": [round(v, 1) for v in tr["rect"]], "mode": "blur", "source": "secret",
                      "label": f"{tr['kind']}: {redact(tr['text'])}", "enabled": True})
    return boxes


def _sample_frames(raw: Path, every: float):
    from .render import ffmpeg_bin, probe
    p = probe(raw)
    v = next((s for s in p.get("streams", []) if s.get("codec_type") == "video"), {})
    w, h = int(v["width"]), int(v["height"])
    proc = subprocess.Popen([ffmpeg_bin(), "-v", "error", "-i", str(raw), "-vf", f"fps={1 / every}",
                             "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE)
    i = 0
    try:
        while True:
            buf = proc.stdout.read(w * h * 3)
            if len(buf) < w * h * 3:
                break
            # ffmpeg's fps filter picks the frame nearest to each tick, starting at t=0
            yield i * every, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            i += 1
    finally:
        proc.stdout.close()
        proc.wait()


# ------------------------------------------------------------------ automatic (privacy by default)
# Things that should never end up in a demo, decided from the event log with no settings:
# windows of apps whose whole job is holding secrets, authentication prompts, and
# notification toasts. Each becomes an ordinary box (source "auto") in the editor's privacy
# lane, removable like any other; they are computed once, when the recording is finalized.
from .sensitive import secret_window  # noqa: E402


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
