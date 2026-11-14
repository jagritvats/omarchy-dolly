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

from . import gfx, grid

BLUR_SIGMA = 20.0        # pixels of the recording: letters of any size a screen shows are gone


# ------------------------------------------------------------------ apply
def apply_boxes(frame, boxes: list[dict], t: float):
    """Blur or pixelate, in `frame` itself (a gfx frame), every enabled box that is on at
    time t, and return it. Only what lies inside a box goes into its blur: nothing next
    to it is smeared in, nothing under it is left to read. A caller that still needs the
    untouched frame hands in a copy."""
    active = [b for b in boxes or [] if b.get("enabled", True) and b["start"] <= t <= b["end"]]
    if not active:
        return frame
    W, H = frame.get_width(), frame.get_height()
    for b in active:
        x, y, w, h = (int(round(v)) for v in b["rect"])
        x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        if b.get("mode", "blur") == "pixelate":
            gfx.pixelate_rect(frame, x0, y0, x1 - x0, y1 - y0, max(6, int(min(x1 - x0, y1 - y0) / 6)))
        else:
            gfx.blur_rect(frame, x0, y0, x1 - x0, y1 - y0, BLUR_SIGMA)
    return frame


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


_NEGATIVE = bytes(255 - v for v in range(256))


def ocr_words(img) -> list[dict]:
    """tesseract TSV -> words with boxes, grouped by line key. `img`: a gfx frame."""
    exe = shutil.which("tesseract")
    if not exe:
        raise RuntimeError("tesseract is not installed (packages tesseract and tesseract-data-eng)")
    gray = grid.luma(gfx.data(img))
    head = b"P5\n%d %d\n255\n" % (img.get_width(), img.get_height())      # a grey picture file: PGM
    outs = []
    # two passes: as-is and inverted (tesseract reads dark-on-light best; terminals are light-on-dark)
    for variant in (gray, gray.translate(_NEGATIVE)):
        with tempfile.NamedTemporaryFile(suffix=".pgm") as f:
            f.write(head + variant)
            f.flush()
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


def detect_in_frame(img, scale: float = 1.0) -> list[dict]:
    """Secret boxes (video px) in one frame (a gfx frame). `scale` = video px per OCR image px."""
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
    """Returns privacy boxes (source='secret'). `frames` (iterable of (t, gfx frame)) overrides decoding (tests)."""
    samples = []
    if frames is None:
        frames = _sample_frames(raw, every)
    last_img, last_hits = None, []
    for i, (t, img) in enumerate(frames):
        # skip OCR when the frame barely changed since the last OCR'd one
        if last_img is not None and _barely_differ(img, last_img):
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


def _barely_differ(a, b) -> bool:
    """Two frames of one size whose colours are less than 0.4 of a level apart on average."""
    if (a.get_width(), a.get_height()) != (b.get_width(), b.get_height()):
        return False
    return sum(gfx.mean(gfx.difference(a, b))) / 3 < 0.4


def _sample_frames(raw: Path, every: float):
    from .ff import ffmpeg_bin, probe
    p = probe(raw)
    v = next((s for s in p.get("streams", []) if s.get("codec_type") == "video"), {})
    w, h = int(v["width"]), int(v["height"])
    proc = subprocess.Popen([ffmpeg_bin(), "-v", "error", "-i", str(raw), "-vf", f"fps={1 / every}",
                             "-f", "rawvideo", "-pix_fmt", "bgr0", "-"], stdout=subprocess.PIPE)
    i = 0
    try:
        while True:
            img = gfx.read_frame(proc.stdout, w, h)
            if img is None:
                break
            # ffmpeg's fps filter picks the frame nearest to each tick, starting at t=0
            yield i * every, img
            i += 1
    finally:
        proc.stdout.close()
        proc.wait()


# ------------------------------------------------------------------ automatic (privacy by default)
# Which windows and toasts get a box is timeline logic with no pixels in it: it lives in
# sensitive.py, so a recording is finalized with its boxes on a machine that cannot draw
# (no cairo). The names stay importable from here.
from .sensitive import (START_LINGER, _toast_boxes, _window_boxes, auto_boxes,  # noqa: E402,F401
                        secret_window, toast_spans)
