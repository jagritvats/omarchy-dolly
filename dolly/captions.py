"""Captions: a lane of timed lines, typed in the editor or transcribed on this machine.

Where the lines live: project.json "captions" (a list of {id, start, end, text, source} in
video seconds), edited in the editor's caption lane. A recording that has no such list yet
(made before 0.8.0, or by a storyboard's `say`) uses its captions.srt.

Transcribers (the first one present; DOLLY_TRANSCRIBER names one):
  1. whisper.cpp CLI (`whisper-cli`) + a ggml model:
     $DOLLY_WHISPER_MODEL or ~/.local/share/dolly/whisper/ggml-base.en.bin
  2. faster-whisper (python), model name $DOLLY_WHISPER_SIZE (default "base")
  3. Voxtype, the engine of Omarchy's Dictation (Omarchy menu > Setup > Defaults >
     Dictation > Voxtype). `voxtype transcribe` gives text without times, so the speech
     is first cut into stretches at its pauses (ffmpeg silencedetect), each stretch is
     transcribed on its own, and its words are spread over the stretch.
None of them is installed by Dolly; without one, Transcribe says how to get one.

What is transcribed: the microphone track (tracks/mic.mka) when there is one, else the
video's own sound, else the system-audio track. Spans muted with the mute hotkey are
silenced first, so what you muted is never written down.

Export burns the lines in (a pill at the bottom) and writes a time-remapped .srt next to
the video.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from . import gfx


class CaptionsUnavailable(RuntimeError):
    pass


MAX_CUES = 5000
MAX_TEXT = 300            # characters of one caption line (two lines on screen at most)
TRANSCRIBERS = ("whisper.cpp", "faster-whisper", "voxtype")
DICTATION_HINT = "turn on Dictation in Omarchy (Omarchy menu > Setup > Defaults > Dictation > Voxtype)"
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def _fmt(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s - int(s)) * 1000)) % 1000:03d}"


def write_srt(cues: list[dict], path: Path):
    lines = []
    for i, c in enumerate(cues, 1):
        lines += [str(i), f"{_fmt(c['start'])} --> {_fmt(c['end'])}", c["text"].strip(), ""]
    Path(path).write_text("\n".join(lines))


def parse_srt(text: str) -> list[dict]:
    cues = []
    for block in re.split(r"\n\s*\n", text.strip().replace("\r", "")):
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", block)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        st = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        en = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        body = block[m.end():].strip()
        if body:
            cues.append({"start": st, "end": en, "text": " ".join(body.splitlines())})
    return cues


def remap(cues: list[dict], timemap, offset: float = 0.0) -> list[dict]:
    """Source-time cues -> output-time cues (dropping cues fully inside sped-up stretches)."""
    out = []
    for c in cues:
        a, b = max(c["start"], timemap.t0), min(c["end"], timemap.t1)
        if b <= a:
            continue
        if timemap.speed_at((a + b) / 2) > 1.01:
            continue
        out.append({"start": timemap.to_output(a) + offset, "end": timemap.to_output(b) + offset, "text": c["text"]})
    return out


# ------------------------------------------------------------------ the caption lane
def clean_text(text) -> str:
    """One caption line as text: no control or direction characters, one line, capped."""
    if not isinstance(text, str):
        return ""
    return " ".join(_CONTROL.sub("", text).split())[:MAX_TEXT]


def clean_cues(cues) -> list[dict]:
    """The caption lane as the editor page or a recording folder gives it, kept to the shape
    Dolly writes: numbers that are numbers, text that is plain text, no overlap, sorted.
    A recording folder may come from someone else; the page is only as trusted as its input."""
    out = []
    for c in cues if isinstance(cues, list) else []:
        if not isinstance(c, dict):
            continue
        try:
            a, b = float(c.get("start")), float(c.get("end"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(a) and math.isfinite(b)) or a < 0 or b - a < 0.05 or b > 7 * 86400:
            continue
        text = clean_text(c.get("text"))
        if not text:
            continue
        cid = c.get("id")
        cid = cid if isinstance(cid, str) and re.fullmatch(r"[a-z]{1,3}[0-9]{1,6}", cid) else None
        out.append({"id": cid, "start": round(a, 3), "end": round(b, 3), "text": text,
                    "source": "manual" if c.get("source") == "manual" else "auto"})
        if len(out) >= MAX_CUES:
            break
    out.sort(key=lambda c: c["start"])
    for i in range(1, len(out)):                       # one line on screen at a time
        if out[i]["start"] < out[i - 1]["end"]:
            out[i - 1]["end"] = out[i]["start"]
    out = [c for c in out if c["end"] - c["start"] >= 0.05]
    used = {c["id"] for c in out if c["id"]}
    n = 1
    for c in out:
        if not c["id"]:
            while f"c{n}" in used:
                n += 1
            c["id"] = f"c{n}"
            used.add(c["id"])
    return out


def load(project_dir: Path) -> list[dict]:
    """The caption lane: project.json "captions", else captions.srt (recordings made before
    0.8.0 and storyboard takes), else nothing."""
    d = Path(project_dir)
    try:
        data = json.loads((d / "project.json").read_text())
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict) and isinstance(data.get("captions"), list):
        return clean_cues(data["captions"])
    f = d / "captions.srt"
    if f.exists():
        try:
            return clean_cues(parse_srt(f.read_text(errors="replace")))
        except OSError:
            return []
    return []


def has(project_dir: Path) -> bool:
    return bool(load(project_dir))


def store(project_dir: Path, cues: list[dict]) -> list[dict]:
    """Put a transcription in the caption lane (replacing it) and in captions.srt."""
    from . import analysis, fsutil
    from .project import Project
    cues = clean_cues(cues)
    with analysis.project_lock(project_dir):
        proj = Project(project_dir)
        data = proj.load()
        data["captions"] = cues
        proj.save(data)
    fsutil.atomic_write_text(Path(project_dir) / "captions.srt", _srt_text(cues))
    return cues


def _srt_text(cues: list[dict]) -> str:
    lines = []
    for i, c in enumerate(cues, 1):
        lines += [str(i), f"{_fmt(c['start'])} --> {_fmt(c['end'])}", c["text"].strip(), ""]
    return "\n".join(lines)


# ------------------------------------------------------------------ transcribers
def backend() -> str | None:
    want = os.environ.get("DOLLY_TRANSCRIBER", "")
    for name in ([want] if want in TRANSCRIBERS else TRANSCRIBERS):
        if name == "whisper.cpp" and shutil.which("whisper-cli") and _model_path():
            return name
        if name == "faster-whisper":
            try:
                import faster_whisper  # noqa: F401
                return name
            except ImportError:
                continue
        if name == "voxtype" and shutil.which("voxtype"):
            return name
    return None


def _model_path() -> str | None:
    p = os.environ.get("DOLLY_WHISPER_MODEL") or str(Path.home() / ".local/share/dolly/whisper/ggml-base.en.bin")
    return p if Path(p).exists() else None


def speech_source(project_dir: Path) -> dict:
    """What to transcribe: {"file", "map" (ffmpeg -map or None), "offset", "scale" (video s =
    offset + scale * file s), "mutes" (video-time spans to silence), "duration"}."""
    from .audio import clean_spans, clean_tracks
    from .ff import probe
    from .project import Project
    d = Path(project_dir)
    try:
        doc = json.loads((d / "events.json").read_text())
    except (OSError, ValueError):
        doc = {}
    cap = doc.get("capture") or {} if isinstance(doc, dict) else {}
    try:
        duration = float(((doc.get("video") or {}) if isinstance(doc, dict) else {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    mutes = clean_spans(cap.get("mic_mutes") or [])
    tracks = {t["track"]: t for t in clean_tracks(cap.get("audio_tracks") or []) if (d / t["file"]).is_file()}
    if "mic" in tracks:
        t = tracks["mic"]
        return {"file": d / t["file"], "map": None, "offset": t["offset"], "scale": t["scale"],
                "mutes": mutes, "duration": duration}
    raw = Project(d).raw
    if raw.exists() and any(s.get("codec_type") == "audio" for s in probe(raw).get("streams", [])):
        return {"file": raw, "map": "0:a:0", "offset": 0.0, "scale": 1.0, "mutes": mutes, "duration": duration}
    if "system" in tracks:
        t = tracks["system"]
        return {"file": d / t["file"], "map": None, "offset": t["offset"], "scale": t["scale"],
                "mutes": [], "duration": duration}
    raise CaptionsUnavailable("the recording has no sound to transcribe (record with the microphone on)")


def _mute_filter(mutes: list, offset: float, scale: float) -> str | None:
    """Muted spans (video time) as an ffmpeg volume chain in file time. Numbers only."""
    parts = []
    for a, b in mutes:
        fa, fb = (a - offset) / scale, (b - offset) / scale
        if fb > max(fa, 0.0):
            parts.append("volume=enable='between(t,%.3f,%.3f)':volume=0" % (max(fa, 0.0), fb))
    return ",".join(parts) or None


def _extract_wav(src: dict, wav: Path) -> None:
    """The sound to transcribe as 16 kHz mono WAV, muted spans silenced."""
    from .ff import ffmpeg_bin
    args = [ffmpeg_bin(), "-v", "error", "-y", "-i", str(src["file"])]
    if src["map"]:
        args += ["-map", src["map"]]
    mf = _mute_filter(src["mutes"], src["offset"], src["scale"])
    if mf:
        args += ["-af", mf]
    # no timeout: decoding takes time proportional to the recording
    subprocess.run(args + ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], check=True,
                   capture_output=True)


def speech_video_spans(project_dir: Path) -> list[tuple[float, float]] | None:
    """Where something is said, in video seconds (the microphone, else the recording's own
    sound): what "Cut pauses" must never cut. None when the recording has no sound."""
    from .ff import ffmpeg_bin, probe
    try:
        src = speech_source(project_dir)
    except CaptionsUnavailable:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "a.wav"
        _extract_wav(src, wav)
        total = float(probe(wav).get("format", {}).get("duration") or 0)
        det = subprocess.run([ffmpeg_bin(), "-hide_banner", "-nostats", "-i", str(wav), "-af",
                              "silencedetect=noise=%ddB:d=%.2f" % (SILENCE_DB, SILENCE_S), "-f", "null", "-"],
                             capture_output=True, text=True)
    out = []
    for a, b in speech_spans(det.stderr, total):
        out.append((src["offset"] + src["scale"] * a, src["offset"] + src["scale"] * b))
    return out


def _to_video(cues: list[dict], src: dict) -> list[dict]:
    """File-time cues -> video-time cues, inside the recording and outside muted spans."""
    out = []
    end = src["duration"] or float("inf")
    for c in cues:
        a = src["offset"] + src["scale"] * c["start"]
        b = src["offset"] + src["scale"] * c["end"]
        a, b = max(a, 0.0), min(b, end)
        if b - a < 0.2:
            continue
        mid = (a + b) / 2
        if any(x <= mid <= y for x, y in src["mutes"]):
            continue
        out.append({"start": a, "end": b, "text": c["text"], "source": "auto"})
    return out


def transcribe(project_dir: Path, language: str | None = None, progress=None) -> list[dict]:
    """Transcribe the recording on this machine into its caption lane (replacing what was
    there). `progress(done, total)` while it works. Returns the lane."""
    project_dir = Path(project_dir)
    src = speech_source(project_dir)
    be = backend()
    if not be:
        raise CaptionsUnavailable(f"no transcriber here: {DICTATION_HINT}, or add whisper.cpp or "
                                  "faster-whisper (docs/captions.md)")
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "a.wav"
        _extract_wav(src, wav)
        if be == "whisper.cpp":
            args = ["whisper-cli", "-m", _model_path(), "-f", str(wav), "-osrt", "-of", str(Path(tmp) / "out")]
            if language:
                args += ["-l", language]
            subprocess.run(args, check=True, capture_output=True)
            cues = parse_srt((Path(tmp) / "out.srt").read_text())
        elif be == "faster-whisper":
            from faster_whisper import WhisperModel
            model = WhisperModel(os.environ.get("DOLLY_WHISPER_SIZE", "base"), device="cpu", compute_type="int8")
            segs, _ = model.transcribe(str(wav), language=language, vad_filter=True)
            cues = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segs]
        else:
            cues = _voxtype(wav, Path(tmp), progress)
    return store(project_dir, _to_video(cues, src))


# ------------------------------------------------------------------ Voxtype
SILENCE_DB = -35          # quieter than this for SILENCE_S is a pause between phrases
SILENCE_S = 0.35
CHUNK_S = 12.0            # stretches transcribed together (one Voxtype run each)
CHUNK_GAP = 1.5           # a longer pause starts a new run
LONG_SPEECH = 15.0        # speech without a pause longer than this is cut in pieces of CHUNK_S
CUE_CHARS = 60            # a caption line at most this long
CUE_S = 4.0               # and on screen at most this long (unless one word is longer)
VOXTYPE_LIMIT = 45.0      # s for one Voxtype run (plus VOXTYPE_PER_S per second of speech; normal: 1-15 s)
VOXTYPE_PER_S = 10.0


def speech_spans(silencedetect_log: str, total: float) -> list[tuple[float, float]]:
    """ffmpeg silencedetect's stderr -> the stretches between the pauses, in file seconds."""
    spans, start = [], 0.0
    for m in re.finditer(r"silence_(start|end): (-?[0-9.]+)", silencedetect_log):
        t = max(0.0, min(float(m.group(2)), total))
        if m.group(1) == "start":
            if t - start > 0.15:
                spans.append((start, t))
            start = None
        else:
            start = t
    if start is not None and total - start > 0.15:
        spans.append((start, total))
    out = []
    for a, b in spans:                     # a little air: the first and last syllables are soft
        a, b = max(0.0, a - 0.15), min(total, b + 0.15)
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    pieces = []
    for a, b in out:
        if b - a > LONG_SPEECH:
            n = math.ceil((b - a) / CHUNK_S)
            step = (b - a) / n
            pieces += [(a + i * step, a + (i + 1) * step) for i in range(n)]
        else:
            pieces.append((a, b))
    return pieces


def chunks(spans: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    """Speech stretches grouped into Voxtype runs: neighbours closer than CHUNK_GAP, at most
    CHUNK_S from the first one's start to the last one's end."""
    out: list[list[tuple[float, float]]] = []
    for s in spans:
        if out and s[0] - out[-1][-1][1] <= CHUNK_GAP and s[1] - out[-1][0][0] <= CHUNK_S:
            out[-1].append(s)
        else:
            out.append([s])
    return out


def spread(text: str, spans: list[tuple[float, float]]) -> list[dict]:
    """Text without times over the stretches it was spoken in: words go to each stretch in
    proportion to its length, then into lines of at most CUE_CHARS / CUE_S, each line timed
    by its share of the letters. An estimate, so the lines are short."""
    words = text.split()
    total = sum(b - a for a, b in spans)
    if not words or total <= 0:
        return []
    cues, given, acc = [], 0, 0.0
    for i, (a, b) in enumerate(spans):
        acc += b - a
        upto = len(words) if i == len(spans) - 1 else round(len(words) * acc / total)
        mine, given = words[given:upto], max(given, upto)
        if not mine:
            continue
        lines, cur = [], []
        for w in mine:
            if cur and (len(" ".join(cur + [w])) > CUE_CHARS
                        or (len(cur) >= 3 and cur[-1][-1:] in ".?!")):
                lines.append(cur)
                cur = []
            cur.append(w)
        if cur:
            lines.append(cur)
        letters = sum(len(" ".join(l)) for l in lines)
        t = a
        for l in lines:
            d = (b - a) * len(" ".join(l)) / letters
            cues.append({"start": t, "end": t + d, "text": " ".join(l)})
            t += d
    out = []
    for c in cues:                         # a line too long to read at once: halves
        if c["end"] - c["start"] > CUE_S * 1.5 and len(c["text"].split()) > 1:
            w = c["text"].split()
            h = len(w) // 2
            mid = c["start"] + (c["end"] - c["start"]) * len(" ".join(w[:h])) / len(c["text"])
            out += [{"start": c["start"], "end": mid, "text": " ".join(w[:h])},
                    {"start": mid, "end": c["end"], "text": " ".join(w[h:])}]
        else:
            out.append(c)
    return out


def voxtype_text(stdout: str) -> str:
    """`voxtype transcribe` prints what it does, then an empty line and the text."""
    if "No speech detected" in stdout:
        return ""
    lines = stdout.replace("\r", "").rstrip("\n").split("\n")
    idx = max((i for i, l in enumerate(lines) if not l.strip()), default=-1)
    return clean_text(" ".join(lines[idx + 1:]))


def voxtype_threads() -> int:
    """Threads for one Voxtype run: half the CPUs, 1 to 4. With every CPU of a 4-CPU virtual
    machine, 3 of 5 runs of a 2 s piece stalled at full load until killed; with 2, none did.
    DOLLY_VOXTYPE_THREADS overrides."""
    try:
        n = int(os.environ.get("DOLLY_VOXTYPE_THREADS", "0"))
    except ValueError:
        n = 0
    if n <= 0:
        n = (len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count() or 2) // 2
    return max(1, min(4, n))


def _run_alone(cmd: list[str], tmp: Path, timeout: float) -> tuple[int | None, str, str]:
    """Run a transcriber in its own process group with its output in files: Dolly waits for
    the program to exit, not for every process it may have started to let go of a pipe, and
    nothing it started outlives it (a leftover would keep a whole CPU busy). rc None: timed out."""
    import signal
    with open(tmp / "out.txt", "w+") as out, open(tmp / "err.txt", "w+") as err:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            rc = None
        finally:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            p.wait()
        out.seek(0)
        err.seek(0)
        return rc, out.read(), err.read()


def _voxtype(wav: Path, tmp: Path, progress=None) -> list[dict]:
    from .ff import ffmpeg_bin, probe
    total = float(probe(wav).get("format", {}).get("duration") or 0)
    det = subprocess.run([ffmpeg_bin(), "-hide_banner", "-nostats", "-i", str(wav), "-af",
                          "silencedetect=noise=%ddB:d=%.2f" % (SILENCE_DB, SILENCE_S), "-f", "null", "-"],
                         capture_output=True, text=True)
    runs = chunks(speech_spans(det.stderr, total))
    cues: list[dict] = []
    for i, run in enumerate(runs):
        if progress:
            progress(i, len(runs))
        a, b = run[0][0], run[-1][1]
        piece = tmp / f"piece{i}.wav"
        subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-ss", "%.3f" % a, "-t", "%.3f" % (b - a), "-i", str(wav),
                        "-c:a", "pcm_s16le", str(piece)], check=True, capture_output=True)
        limit = VOXTYPE_LIMIT + VOXTYPE_PER_S * (b - a)
        rc, out, err = _run_alone(["voxtype", "--threads", str(voxtype_threads()), "transcribe", str(piece)], tmp, limit)
        if rc is None:            # stalled: once more on one thread before giving up
            rc, out, err = _run_alone(["voxtype", "--threads", "1", "transcribe", str(piece)], tmp, limit * 2)
        if rc is None:
            raise CaptionsUnavailable("Voxtype stopped responding on a piece of the recording")
        if rc != 0:
            why = (err or out).strip().splitlines()
            raise CaptionsUnavailable("Voxtype could not transcribe (%s); check Dictation in Omarchy's Setup menu"
                                      % clean_text(why[-1] if why else f"exit {rc}")[:160])
        cues += spread(voxtype_text(out), run)
        piece.unlink(missing_ok=True)
    if progress:
        progress(len(runs), len(runs))
    return cues


@lru_cache(maxsize=128)
def caption_image(text: str, max_w: int, scale: float, font: str | None):
    """The caption as a pill with its text: a gfx sprite (colours already multiplied by the
    alpha), which the renderer places and fades. It is kept: never draw on it.
    `font`: families, the first one installed is used ("A, B, monospace"), or a font file."""
    size = int(30 * scale)
    font = gfx.font(font, size, bold=True)
    # greedy wrap to max 2 lines
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if gfx.text_width(trial, font) <= max_w - 40 * scale or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    lines = lines[-2:] or [""]
    lh = int(size * 1.3)
    tw = int(max(gfx.text_width(l, font) for l in lines)) + int(36 * scale)
    th = lh * len(lines) + int(20 * scale)
    img = gfx.new(tw, th, alpha=True)
    c = gfx.context(img)
    gfx.rounded_rect(c, 0, 0, tw, th, int(14 * scale))
    c.set_source_rgba(14 / 255, 14 / 255, 20 / 255, 205 / 255)
    c.fill()
    for i, l in enumerate(lines):
        gfx.label(c, l, tw / 2, int(10 * scale) + lh * i + lh / 2, font, gfx.rgb((245, 246, 252)))
    return img
