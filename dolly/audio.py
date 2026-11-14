"""Audio: one choice (None / Microphone / System audio / Microphone + System),
recorded as SEPARATE tracks next to the screen video, mixed at export.

Why separate tracks: wf-recorder takes one audio device, and a recorder-side
mix can't be undone. Each source is its own `ffmpeg -f pulse` process (works
on PulseAudio and PipeWire's pulse server) writing Matroska with wall-clock
timestamps, so a crash leaves a playable file and finalize can line it up
with the video through the sync-marker fit (offset + clock drift), like the
event log and the camera.

    tracks/mic.mka      default source or the picked one
    tracks/system.mka   monitor of the default sink or the picked one

At export: per-track volume/mute, mic noise suppression (arnndn when an
RNNoise model is configured, else afftdn), muted spans from the "mute mic"
hotkey (silence, not a cut), auto-ducking of system audio under the voice
(sidechaincompress), loudness normalisation of the mix, one AAC track.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from array import array
from pathlib import Path
import re
from . import log as _dlog

# What goes into an ffmpeg filter graph as text. A recording's files name its audio tracks
# and may name a denoise model, and a recording folder can come from someone else: a name
# with a quote or a bracket in it would be read by ffmpeg as more filters, and ffmpeg has
# filters that write files.
TRACK_NAME = re.compile(r"[a-z][a-z0-9_]{0,15}\Z")
# .mka only, the one container Dolly records: by its name ffmpeg would read a playlist, and a
# playlist pulls in other files from the disk
TRACK_FILE = re.compile(r"tracks/[A-Za-z0-9_][A-Za-z0-9_.-]{0,59}\.mka\Z")
_MODEL_PATH = re.compile(r"[A-Za-z0-9_./+@ -]{1,400}\Z")


def clean_tracks(tracks) -> list[dict]:
    """The audio tracks a recording lists, keeping only entries of the shape Dolly writes:
    a plain name ("mic", "system") and a file directly inside the take's tracks/ folder."""
    out = []
    for t in tracks if isinstance(tracks, list) else []:
        if not isinstance(t, dict) or not isinstance(t.get("track"), str) or not isinstance(t.get("file"), str):
            continue
        if not TRACK_NAME.match(t["track"]) or not TRACK_FILE.match(t["file"]) or ".." in t["file"]:
            continue
        try:
            off, sc = float(t.get("offset", 0.0) or 0.0), float(t.get("scale", 1.0) or 1.0)
        except (TypeError, ValueError):
            continue
        if not (-3600.0 < off < 3600.0 and 0.5 < sc < 2.0):       # also drops nan and inf
            off, sc = 0.0, 1.0
        out.append({**t, "offset": off, "scale": sc})
    return out


def clean_spans(spans) -> list[list[float]]:
    """[[start, end], ...] in seconds, numbers only."""
    out = []
    for s in spans if isinstance(spans, list) else []:
        try:
            a, b = float(s[0]), float(s[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if 0.0 <= a < b < 1e7:
            out.append([a, b])
    return out


def model_arg(path) -> str | None:
    """The denoise model path as it may stand inside arnndn=m='...': an existing file whose
    path has no character ffmpeg's filter syntax gives a meaning to. Anything else: None,
    and the built-in denoiser is used."""
    if not isinstance(path, str) or not _MODEL_PATH.match(path):
        return None
    try:
        return path if Path(path).is_file() else None
    except OSError:
        return None

MODES = ("none", "mic", "system", "both")
DEFAULT = "default"          # follow the default device (resolved when the recording starts)


def _pactl_json(*args) -> list | dict | None:
    if not shutil.which("pactl"):
        return None
    try:
        r = subprocess.run(["pactl", "-f", "json", *args], capture_output=True, text=True, timeout=2)
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None
    except Exception:
        _dlog.swallowed()
        return None


def _pactl(*args) -> str:
    if not shutil.which("pactl"):
        return ""
    try:
        return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=2).stdout.strip()
    except Exception:
        _dlog.swallowed()
        return ""


def _default(kind: str) -> str:
    """Name of the default source/sink. pactl can answer with the placeholder
    "@DEFAULT_SOURCE@" (no default chosen yet, e.g. right after the audio server starts):
    then read it from `pactl info`, and give "" rather than a name that is not a device."""
    name = _pactl(f"get-default-{kind}")
    if name and not name.startswith("@DEFAULT_"):
        return name
    label = "Default Source:" if kind == "source" else "Default Sink:"
    for ln in _pactl("info").splitlines():
        if ln.strip().startswith(label):
            v = ln.split(":", 1)[1].strip()
            if v and not v.startswith("@DEFAULT_"):
                return v
    return ""


def devices() -> dict:
    """Microphones (sources that are not monitors) and outputs (sinks, recorded through
    their monitor), each list with "Default" first."""
    out = {"available": bool(shutil.which("pactl")), "sources": [], "sinks": [],
           "default_source": _default("source"), "default_sink": _default("sink")}
    for kind, key in (("sources", "sources"), ("sinks", "sinks")):
        items = _pactl_json("list", kind)
        if items is None:
            items = [{"name": ln.split("\t")[1], "description": ln.split("\t")[1]}
                     for ln in _pactl("list", "short", kind).splitlines() if "\t" in ln]
        for it in items:
            name = it.get("name", "")
            if kind == "sources" and (name.endswith(".monitor") or
                                      (it.get("properties") or {}).get("device.class") == "monitor"):
                continue
            out[key].append({"name": name, "description": it.get("description") or name})
    return out


def resolve(mode: str, mic: str = DEFAULT, system: str = DEFAULT) -> list[tuple[str, str]]:
    """[(track, pulse source name)] for an audio mode."""
    if mode not in MODES:
        raise ValueError(f"audio mode must be one of {', '.join(MODES)}")
    tracks = []
    if mode in ("mic", "both"):
        tracks.append(("mic", mic if mic and mic != DEFAULT else (_default("source") or "@DEFAULT_SOURCE@")))
    if mode in ("system", "both"):
        if system and system != DEFAULT:
            src = system if system.endswith(".monitor") else system + ".monitor"
        else:
            sink = _default("sink")
            src = (sink + ".monitor") if sink else "@DEFAULT_MONITOR@"
        tracks.append(("system", src))
    return tracks


def track_cmd(source: str, out: Path) -> list[str]:
    from .ff import ffmpeg_bin
    # wallclock pts (pulse demuxer default) kept with -copyts: start_time = when the first
    # sample was captured, on the same clock as time.time()
    return [ffmpeg_bin(), "-v", "error", "-nostdin", "-y", "-f", "pulse", "-wallclock", "1",
            "-fragment_size", "1920", "-i", source, "-ac", "2", "-ar", "48000", "-c:a", "flac",
            "-copyts", "-f", "matroska", str(out)]


def start_tracks(pdir: Path, tracks: list[tuple[str, str]], log) -> list[dict]:
    """Start one capture process per track. A source that fails to open is reported, never
    silently dropped (the caller tells the user)."""
    out = []
    if not tracks:
        return out
    (pdir / "tracks").mkdir(exist_ok=True)
    for name, src in tracks:
        f = pdir / "tracks" / f"{name}.mka"
        cmd = track_cmd(src, f)
        log.write(f"$ {' '.join(cmd)}\n")
        log.flush()
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        except OSError as e:
            out.append({"track": name, "source": src, "error": str(e)})
            continue
        out.append({"track": name, "source": src, "pid": p.pid, "file": str(f), "proc": p})
    t_end = time.monotonic() + 2.0
    for t in out:                                   # opened? (a busy/missing device exits at once)
        p = t.get("proc")
        while p is not None and time.monotonic() < t_end and p.poll() is None and \
                not (Path(t["file"]).exists() and Path(t["file"]).stat().st_size > 0):
            time.sleep(0.05)
        if p is not None and p.poll() is not None:
            t["error"] = f"could not open {t['source']}"
            t.pop("pid", None)
    for t in out:
        t.pop("proc", None)
    return out


def stop_tracks(tracks: list[dict], timeout: float = 5.0):
    import signal
    from .health import pid_alive
    for t in tracks:
        if t.get("pid") and pid_alive(t["pid"]):
            try:
                os.kill(t["pid"], signal.SIGINT)      # ffmpeg finalises the file
            except OSError:
                pass
    end = time.monotonic() + timeout
    for t in tracks:
        while t.get("pid") and pid_alive(t["pid"]) and time.monotonic() < end:
            time.sleep(0.05)
        if t.get("pid") and pid_alive(t["pid"]):
            os.kill(t["pid"], signal.SIGKILL)


# ------------------------------------------------------------------ alignment
def start_time(f: Path) -> float | None:
    from .ff import probe
    try:
        st = probe(f).get("format", {}).get("start_time")
        return float(st) if st not in (None, "N/A") else None
    except Exception:
        _dlog.swallowed()
        return None


def align(meta: dict, fit, files: list[Path]) -> list[dict]:
    """Track wall-clock start -> logger seconds -> video seconds through the sync fit.

    meta: capture.json (wall0/mono0 clock pair, logger t0 in mono). Returns
    [{"track", "file", "offset" (video s at track s 0), "scale" (video s per track s)}]."""
    out = []
    wall0, mono0 = meta.get("clock_wall"), meta.get("clock_mono")
    log0 = meta.get("logger_t0_mono")
    for f in files:
        st = start_time(f)
        off = None
        if st is not None and wall0 and mono0 is not None:
            mono = st - (wall0 - mono0)
            if log0 is not None:
                off = fit.map(mono - log0)
            elif meta.get("t0_mono") is not None:
                off = mono - meta["t0_mono"]
        if off is None or not -10 < off < 10:
            off = 0.0
        out.append({"track": f.stem, "file": f"tracks/{f.name}", "offset": round(off, 4),
                    "scale": round(float(getattr(fit, "b", 1.0) or 1.0), 6)})
    return out


def mute_spans(events: list[dict], end: float) -> list[list[float]]:
    spans, on = [], None
    for e in sorted((e for e in events if e.get("type") == "mute"), key=lambda e: e["t"]):
        if e.get("on") and on is None:
            on = e["t"]
        elif not e.get("on") and on is not None:
            spans.append([round(on, 3), round(e["t"], 3)])
            on = None
    if on is not None:
        spans.append([round(on, 3), round(end, 3)])
    return spans


# ------------------------------------------------------------------ export mix
def _pieces_chain(label: str, pieces, n_split: int) -> tuple[str, list[str]]:
    outs = [f"[{label}s{k}]" for k in range(n_split)]
    head = f"[{label}]asplit={n_split}" + "".join(outs) if n_split > 1 else None
    chains, labels = [], []
    for k, (a, b, sp) in enumerate(pieces):
        src = outs[k] if n_split > 1 else f"[{label}]"
        ch = f"{src}atrim={a:.4f}:{b:.4f},asetpts=PTS-STARTPTS"
        if sp > 1.001:
            r = sp
            while r > 2.0 + 1e-9:
                ch += ",atempo=2.0"
                r /= 2.0
            ch += f",atempo={r:.4f},volume=0.35"
        chains.append(ch + f"[{label}p{k}]")
        labels.append(f"[{label}p{k}]")
    pre = [head] if head else []
    return ";".join(pre + chains) + ";" + "".join(labels) + f"concat=n={len(labels)}:v=0:a=1[{label}c]", [f"[{label}c]"]


def mix_filter(tracks: list[dict], first_input: int, pieces, intro: float, total: float, settings: dict,
               mutes: list | None = None, rnn_model: str | None = None) -> str:
    """filter_complex for separate tracks -> [aout]. tracks: align() entries in input order."""
    parts, mixed = [], []
    duck = bool(settings.get("duck", True))
    rnn_model = model_arg(rnn_model)
    for t in tracks:                                # the names become filter labels: see TRACK_NAME
        if not TRACK_NAME.match(str(t.get("track"))):
            raise ValueError(f"not an audio track name: {t.get('track')!r}")
    for i, t in enumerate(tracks):
        cfg = settings.get(t["track"], {})
        if cfg.get("mute"):
            continue
        lab = t["track"]
        align = []
        off, sc = float(t.get("offset", 0.0)), float(t.get("scale", 1.0) or 1.0)
        if abs(sc - 1.0) > 2e-4:
            align.append(f"atempo={1 / sc:.6f}")
        if off > 0:
            align.append(f"adelay=delays={int(round(off * 1000))}:all=1")
        elif off < 0:
            align.append(f"atrim=start={-off:.4f},asetpts=PTS-STARTPTS")
        align.append("aresample=48000,aformat=channel_layouts=stereo")
        if lab == "mic":
            for a, b in (mutes or []):              # "mute mic" spans: silence, not a cut
                align.append(f"volume=enable='between(t,{a:.3f},{b:.3f})':volume=0")
        parts.append(f"[{first_input + i}:a]" + ",".join(align) + f"[{lab}]")
        chain, outs = _pieces_chain(lab, pieces, len(pieces))
        parts.append(chain)
        post = []
        if lab == "mic" and cfg.get("denoise", True):
            post += ["highpass=f=70", f"arnndn=m='{rnn_model}'" if rnn_model else "afftdn=nf=-25:tn=1"]
        vol = float(cfg.get("volume", 1.0))
        if abs(vol - 1.0) > 1e-3:
            post.append(f"volume={vol:.3f}")
        parts.append(f"{outs[0]}{','.join(post) or 'anull'}[{lab}f]")
        mixed.append(lab)
    if not mixed:
        return f"anullsrc=r=48000:cl=stereo,atrim=0:{total:.4f}[aout]"
    if duck and "mic" in mixed and "system" in mixed:
        parts.append("[micf]asplit=2[micm][micsc]")
        parts.append("[systemf][micsc]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[systemd]")
        ins = "[micm][systemd]"
    else:
        ins = "".join(f"[{m}f]" for m in mixed)
    post = []
    if len(mixed) > 1:
        parts.append(f"{ins}amix=inputs={len(mixed)}:duration=longest:normalize=0[mix]")
        src = "[mix]"
    else:
        src = ins
    if settings.get("normalize", True):
        post.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    post.append("aresample=48000")
    if intro > 0:
        post.append(f"adelay=delays={int(intro * 1000)}:all=1")
    post.append(f"apad=whole_dur={total:.4f},atrim=0:{total:.4f}")
    parts.append(src + ",".join(post) + "[aout]")
    return ";".join(parts)


# ------------------------------------------------------------------ level meter (panel)
def peak(block: bytes) -> float:
    """The loudest sample in a block of 16-bit little-endian audio, as 0..1."""
    samples = array("h", block[:len(block) // 2 * 2])       # (a block cut off mid-sample: without its last byte)
    if sys.byteorder == "big":
        samples.byteswap()
    return max(max(samples), -min(samples)) / 32768 if samples else 0.0


def meter(source: str, seconds: float = 0, interval: float = 0.1):
    """Print {"peak": 0..1} lines for a source (parec, ~10 Hz) until killed or `seconds`.
    The panel runs it only while it is open."""
    exe = shutil.which("parec")
    if not exe:
        raise SystemExit("parec missing (pulseaudio-utils / pipewire-pulse)")
    rate = 16000
    p = subprocess.Popen([exe, "-d", source, "--raw", "--format=s16le", f"--rate={rate}", "--channels=1",
                          "--latency-msec=50"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    n = int(rate * interval) * 2
    t_end = time.monotonic() + seconds if seconds else None
    try:
        while t_end is None or time.monotonic() < t_end:
            buf = p.stdout.read(n)
            if not buf:
                break
            print(json.dumps({"peak": round(peak(buf), 3)}), flush=True)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        p.kill()
