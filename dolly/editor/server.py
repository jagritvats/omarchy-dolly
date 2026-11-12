"""Local editor server (stdlib only).

Serves the single-page editor plus a small JSON API for one project. Binds to
127.0.0.1 on a random port with a per-session token in the URL, so other local
users and web pages cannot drive it.

    GET  /                      editor page (token in query)
    GET  /api/project           timeline summary, segments, style, presets
    GET  /api/path?fps=30       camera path for preview (z, cx, cy, cursor)
    POST /api/save              {segments, style, trim, zoom_config}
    POST /api/auto              re-plan auto segments {zoom_config?, keep_manual?} -> {segments, zoom_config}
    POST /api/export            {format, quality, width, fps} -> starts job
    GET  /api/export/status     progress of the running job
    GET  /media/raw.mp4         the recording (Range requests supported)
    GET  /media/wallpaper/N     theme wallpaper thumbnails
    GET  /media/export/<name>   finished export
    POST /api/open              open exports folder / file with xdg-open
    POST /api/quit
    GET  /api/nav?seq=N         long-poll: the page follows navigation requests (one editor window)
    POST /api/navigate          {view, dir?}: another `dolly edit/library/settings` reuses this editor
    GET  /api/images            background images to offer (theme, ~/Pictures, uploaded)
    GET  /media/image?path=P    a checked background image (see dolly/images.py)
    POST /api/images/check      {path} -> image info or error
    POST /api/images/upload?name=N   raw image bytes from the browser's file dialog

Only one editor runs per session: serve() first looks for a live one (runtime
editor.json), asks it to navigate, focuses its window, and only starts a server
and a window when there is none.
"""
from __future__ import annotations

import hmac
import json
import mimetypes

import numpy as np
import os
import re
import secrets
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import project as projmod
from ..events import Timeline
from ..render import export, shift_timeline
from ..zoom import Segment, ZoomConfig, build_segments

STATIC = Path(__file__).parent / "static"


class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"running": False, "done": 0, "total": 0, "fps": 0.0, "out": None, "error": None}

    def update(self, **kw):
        with self.lock:
            self.state.update(kw)

    def snapshot(self):
        with self.lock:
            return dict(self.state)


class Editor:
    """One local app: the Library (home), the editor for one recording at a time, and Settings."""

    def __init__(self, pdir: Path | None, view: str = "edit"):
        self.proj = projmod.Project(pdir) if pdir else None
        self.view = view if pdir or view != "edit" else "library"
        self.token = secrets.token_urlsafe(16)
        self.job = Job()
        self.last_ping = time.time()
        self.page_seen = 0.0             # last long-poll from the page: is a window showing us?
        self.nav_seq = 0
        self.section = None                # a Settings group to scroll to (`dolly settings --section`)
        self.nav_cv = threading.Condition()
        self._plan_wait = False          # the page was given a take whose zoom plan is still coming
        self._preview = None             # (folder, thread) while the preview proxy is being encoded

    def start_preview(self) -> None:
        """Encode the browser-friendly preview of the open take in the background. The
        window opens and shows the take's timeline at once; the page loads the video when
        /api/plan says the preview is ready. (It used to be encoded before the window opened:
        a wait that grew with the length of the take.)"""
        if not self.proj:
            return
        d = self.proj.dir
        prev, raw = d / "preview.mp4", self.proj.raw
        try:
            if prev.exists() and prev.stat().st_mtime >= raw.stat().st_mtime:
                self._preview = None
                return
        except OSError:
            return
        th = threading.Thread(target=ensure_preview, args=(d,), daemon=True)
        self._preview = (d, th)
        th.start()

    def preview_pending(self) -> bool:
        p = self._preview
        return bool(p and self.proj and p[0] == self.proj.dir and p[1].is_alive())

    def plan_pending(self) -> bool:
        """The open take's zoom plan is still being worked out (dolly.analysis). A job that
        died (crash, logout) is started again here: opening the take heals it."""
        from .. import analysis
        info = analysis.pending(self.proj.dir) if self.proj else None
        if info is None:
            return False
        now = time.time()
        # Stop starts the job itself; give it (and one we started) a moment to take the lock
        if now - float(info.get("since") or 0) > 15 and now - getattr(self, "_plan_spawned", 0.0) > 15                 and not analysis.running(self.proj.dir):
            self._plan_spawned = now
            analysis.spawn(self.proj.dir)
        return True

    def plan_payload(self) -> dict:
        """What the page asks for while it waits: the plan once it is there."""
        preview = "pending" if self.preview_pending() else "ready"
        if self.plan_pending():
            return {"analysis": "pending", "preview": preview}
        data = self.proj.load()
        tl = self.timeline(data["style"])
        zcfg = ZoomConfig.from_dict(data.get("zoom_config"))
        segs = data["segments"]
        if segs is None:
            segs = [s.to_dict() for s in _plan(tl, zcfg, self.aoi(tl, zcfg))]
        self._plan_wait = False          # from here on the page's segments and speed are saved again
        return {"analysis": "ready", "preview": preview, "segments": segs, "speed": data.get("speed") or []}

    def navigate(self, view: str, pdir: Path | None = None, section: str | None = None) -> dict:
        """Show another view / recording in the existing window (bumps the sequence the
        page long-polls on)."""
        if view not in ("edit", "share", "library", "settings"):
            raise ValueError(f"unknown view {view}")
        if pdir is not None:
            self.open(pdir)
        self.view = "edit" if view == "share" else view
        if self.view == "edit" and not self.proj:
            self.view = "library"
        self.section = str(section) if section and self.view == "settings" else None
        with self.nav_cv:
            self.nav_seq += 1
            self.nav_cv.notify_all()
        return self.nav_state()

    def nav_state(self) -> dict:
        return {"seq": self.nav_seq, "view": self.view, "project": self.proj.name if self.proj else None,
                "section": self.section, "page": time.time() - self.page_seen < 30}

    def wait_nav(self, seq: int, timeout: float = 25.0) -> dict:
        self.page_seen = time.time()
        with self.nav_cv:
            self.nav_cv.wait_for(lambda: self.nav_seq > seq, timeout=timeout)
        self.page_seen = time.time()
        return self.nav_state()

    def open(self, pdir: Path):
        """Switch the editor to another recording from the Library."""
        from .. import library
        d = Path(pdir).resolve()
        root = projmod.videos_root().resolve()
        if root not in d.parents or not (d / "events.json").exists():
            raise ValueError("not a finalized recording in the library")
        if not projmod.Project(d).raw.exists():
            raise ValueError("the raw video of this recording was cleaned up; its exports are still in the library")
        self.proj = projmod.Project(d)
        self._aoi = None
        self._plan_wait = False
        self.start_preview()
        self.view = "edit"
        library.index(refresh=False, posters=False)
        return {"ok": True, "dir": str(d)}

    def app_payload(self) -> dict:
        snap = projmod.theme_snapshot()
        return {"view": self.view, "project": self.proj.name if self.proj else None,
                "theme": {**snap, "gradients": projmod.gradient_presets(snap["colors"])},
                "version": _version(), "videos_root": str(projmod.videos_root()), "nav_seq": self.nav_seq,
                "section": self.section}

    def settings_payload(self) -> dict:
        from .. import config as cfgmod
        out = cfgmod.describe()
        from ..share import config as scfg
        try:
            sc = scfg.load()
            out["share_targets"] = [_public_target(n, t) for n, t in sc["targets"].items()]
            out["share_default"] = sc["default"]
        except scfg.ConfigError as e:
            out["share_targets"], out["share_error"] = [], str(e)
        out["share_path"] = str(scfg.config_path())
        out["hotkeys"] = hotkeys()
        from .. import integration
        out["shortcuts"] = integration.shortcut_view()
        out["set_up"] = integration.status(check_live=False)["set_up"]
        out["mic_devices"] = mic_devices()
        out["videos_root"] = str(projmod.videos_root())
        out["theme"] = projmod.theme_snapshot()
        out["capture_bakes_cursor"] = capture_bakes_cursor()
        return out

    def timeline(self, style=None) -> Timeline:
        tl = Timeline.load(self.proj.events)
        if style:
            shift_timeline(tl, float(style.get("sync_offset", 0.0)))
        return tl

    def project_payload(self) -> dict:
        data = self.proj.load()
        tl = self.timeline(data["style"])
        zcfg = ZoomConfig.from_dict(data.get("zoom_config"))
        segs = data["segments"]
        waiting = self.plan_pending()
        self._plan_wait = waiting
        if waiting:
            segs = []                    # shown at once; /api/plan delivers the zooms when they are ready
        elif segs is None:
            segs = [s.to_dict() for s in _plan(tl, zcfg, self.aoi(tl, zcfg))]
        colors = projmod.theme_colors()
        return {
            "analysis": "pending" if waiting else "ready",
            "preview": "pending" if self.preview_pending() else "ready",
            "name": self.proj.name, "dir": str(self.proj.dir),
            "video": {"w": tl.w, "h": tl.h, "fps": tl.fps, "duration": tl.duration},
            "capture": tl.capture,
            "clicks": [{"t": c["t"], "x": c["x"], "y": c["y"]} for c in tl.clicks],
            "keys": [{"t": k["t"], "key": k["key"], "mods": k["mods"]} for k in tl.keys],
            "segments": segs, "style": data["style"], "trim": data["trim"] or [0, tl.duration],
            "speed": data.get("speed") or [], "privacy": data.get("privacy") or [],
            "has_audio": self.has_audio(), "has_webcam": any((self.proj.dir / n).exists() for n in ("webcam.mkv", "webcam.mp4")),
            "has_captions": (self.proj.dir / "captions.srt").exists(),
            "audio_tracks": tl.capture.get("audio_tracks") or [], "mic_mutes": tl.capture.get("mic_mutes") or [],
            "camera_mode": tl.capture.get("camera_mode", "off"),
            "webcam_offset": float(tl.capture.get("webcam_offset", 0.0) or 0.0),
            "caption_cues": __import__("dolly.captions", fromlist=["load"]).load(self.proj.dir),
            "styles": list_styles(),
            "zoom_config": zcfg.to_dict(),
            "title": (data.get("title") or ""),
            "theme": {**projmod.theme_snapshot(), "colors": colors,
                      "pinned": data["style"].get("theme"),
                      "gradients": projmod.gradient_presets(projmod.style_colors(data["style"])),
                      "wallpapers": [str(p) for p in projmod.wallpapers()]},
            "exports": sorted([p.name for p in self.proj.exports.glob("*") if p.suffix in (".mp4", ".gif")])
            if self.proj.exports.exists() else [],
        }

    def has_audio(self) -> bool:
        from ..render import probe
        if any((self.proj.dir / t["file"]).exists() for t in (self.timeline().capture.get("audio_tracks") or [])):
            return True
        return any(s.get("codec_type") == "audio" for s in probe(self.proj.raw).get("streams", []))

    def path_payload(self, fps: float, body: dict | None = None, aspect: str = "16:9") -> dict:
        data = self.proj.load()
        if body:
            data["style"] = projmod.deep_merge(data["style"], body.get("style"))
            if body.get("segments") is not None:
                data["segments"] = body["segments"]
        tl = self.timeline(data["style"])
        zcfg = ZoomConfig.from_dict(data.get("zoom_config"))
        if self.plan_pending():
            aoi, segs = None, []         # an un-zoomed preview until the plan arrives
        else:
            aoi = self.aoi(tl, zcfg)
            segs = [Segment.from_dict(s) for s in data["segments"]] if data["segments"] is not None \
                else _plan(tl, zcfg, aoi)
        from ..render import Compositor
        comp = Compositor(tl, projmod.deep_merge(data["style"], {"output": {"fps": fps}}), segs, zcfg, 0.0,
                          tl.duration, aspect=aspect or "16:9", aoi=aoi)
        times = np.arange(int(round(tl.duration * fps))) / fps
        cur_a = [round(comp.cursor_alpha(t), 2) for t in times]
        shapes = {}
        if comp.shapes:
            for i in range(0, len(times), max(1, int(fps / 10))):
                sh = comp.cursor_shape(times[i])
                if sh != "arrow":
                    shapes[i] = sh
        r = lambda a: [round(float(v), 1) for v in a]  # noqa: E731
        if aspect and aspect != "16:9":
            c = comp.crop
            return {"fps": fps, "aspect": aspect, "cx": r(c.cx), "cy": r(c.cy), "cw": r(c.cw), "ch": r(c.ch),
                    "cur_x": r(c.cur_x), "cur_y": r(c.cur_y), "cur_a": cur_a, "cur_shape": shapes,
                    "cuts": list(c.cuts or []),
                    "geom": {"W": tl.w, "H": tl.h, "OW": comp.lay.OW, "OH": comp.lay.OH, "card": list(comp.card),
                             "card_r": comp.card_r, "cursor_base": comp.lay.cursor_base}}
        lay, p = comp.lay, comp.path
        return {**p.to_json(), "cur_a": cur_a, "cur_shape": shapes,
                "geom": {"W": lay.geom.W, "H": lay.geom.H, "px": lay.geom.px, "py": lay.geom.py,
                         "CW": lay.geom.CW, "CH": lay.geom.CH, "OW": lay.OW, "OH": lay.OH,
                         "radius_v": lay.radius_v, "cursor_base": lay.cursor_base}}

    def aoi(self, tl, zcfg):
        if not zcfg.use_aoi:
            return None
        if getattr(self, "_aoi", None) is None or self._aoi[0] != (tl.duration, len(tl.keys)):
            from ..render import load_aoi
            self._aoi = ((tl.duration, len(tl.keys)), load_aoi(self.proj.dir, tl, zcfg))
        return self._aoi[1]

    def save(self, body: dict):
        from .. import analysis
        if self._plan_wait:
            # the page still holds "no zooms yet": its segments and speed must not replace
            # the plan that is on its way (it asks /api/plan, then they are saved again)
            body = {k: v for k, v in body.items() if k not in ("segments", "speed")}
        with analysis.project_lock(self.proj.dir):
            self._save(body)

    def _save(self, body: dict):
        data = self.proj.load()
        for k in ("segments", "style", "trim", "zoom_config", "speed", "privacy"):
            if k in body:
                data[k] = body[k]
        bg = (data.get("style") or {}).get("background")
        if isinstance(bg, dict) and bg.get("type") == "wallpaper":
            # export opens this path: only a checked image (never an arbitrary file the page names)
            from .. import images
            try:
                bg["path"] = str(images.check(bg.get("path", "")))
            except images.ImageError as e:
                raise ValueError(f"background image: {e}") from None
            bg.pop("index", None)       # theme wallpaper order changes with the theme; the path is the truth
        self.proj.save(data)

    # Auto-zoom options the editor may change per recording (Settings > Auto-zoom names)
    ZOOM_KEYS = {"enabled": bool, "zoom_default": float, "zoom_clicks": bool, "follow_typing": bool,
                 "zoom_windows": bool, "glide": bool, "stay_gap": float, "motion": str}

    def auto(self, body: dict | None = None) -> dict:
        """Re-plan the automatic zooms. body.zoom_config: options for this recording;
        body.keep_manual: zooms the user added or edited stay (auto ones overlapping them go)."""
        body = body or {}
        from .. import analysis
        analysis.ensure(self.proj.dir)       # re-planning builds on the first plan's analysis
        data = self.proj.load()
        zc = dict(data.get("zoom_config") or {})
        for k, v in (body.get("zoom_config") or {}).items():
            if k in self.ZOOM_KEYS:
                try:
                    zc[k] = self.ZOOM_KEYS[k](v)
                except (TypeError, ValueError):
                    continue
        if "zoom_default" in zc:
            zc["zoom_default"] = min(max(float(zc["zoom_default"]), 1.25), 3.0)
            zc["zoom_max"] = max(float(zc.get("zoom_max", 2.5)), zc["zoom_default"])
        if "stay_gap" in zc:
            zc["stay_gap"] = min(max(float(zc["stay_gap"]), 2.0), 12.0)
        if zc.get("motion") not in (None, "calm", "normal", "quick"):
            zc["motion"] = "normal"
        data["zoom_config"] = zc
        tl = self.timeline(data["style"])
        zcfg = ZoomConfig.from_dict(zc)
        segs = [s.to_dict() for s in _plan(tl, zcfg, self.aoi(tl, zcfg))]
        if body.get("keep_manual"):
            manual = [s for s in (data.get("segments") or []) if s.get("source") == "manual"]
            keep = []
            for s in segs:            # auto zooms make room for manual ones (pieces under 0.6 s go)
                pieces = [(s["start"], s["end"])]
                for m in manual:
                    pieces = [q for a, b in pieces for q in ((a, min(b, m["start"])), (max(a, m["end"]), b)) if q[1] > q[0]]
                for k, (a, b) in enumerate(p for p in pieces if p[1] - p[0] >= 0.6):
                    keep.append({**s, "start": round(a, 3), "end": round(b, 3), "id": s["id"] + ("" if k == 0 else f"-{k}")})
            segs = sorted(manual + keep, key=lambda s: s["start"])
        data["segments"] = segs
        self.proj.save(data)
        return {"segments": segs, "zoom_config": ZoomConfig.from_dict(zc).to_dict()}

    def start_job(self, label: str, fn):
        if self.job.snapshot()["running"]:
            raise RuntimeError("another job is running")
        self.job.update(running=True, done=0, total=0, fps=0.0, out=None, error=None, label=label,
                        result=None, started=time.time())

        def run():
            try:
                res = fn(lambda i, n, el=None: self.job.update(done=i, total=n or 0, fps=(i / el) if el else 0))
                self.job.update(running=False, result=res, out=(res or {}).get("out") if isinstance(res, dict) else None)
            except Exception as e:  # pragma: no cover
                self.job.update(running=False, error=str(e))
        threading.Thread(target=run, daemon=True).start()

    def share_export(self, body: dict):
        from .. import share
        self.save(body.get("project", {}))
        preset = body["preset"]

        def fn(progress):
            r = share.share_preset(self.proj.dir, preset, progress=progress, log=lambda m: None)
            return {"out": r["file"].name, "file": str(r["file"]), "size": r["file"].stat().st_size,
                    "report": r["report"], "preset": r["preset"]["label"]}
        self.start_job(f"Export for {preset}", fn)

    def scrub(self):
        from ..privacy import scan_secrets

        def fn(progress):
            from .. import analysis
            analysis.ensure(self.proj.dir)       # one writer of project.json at a time
            tl = self.timeline()
            boxes = scan_secrets(self.proj.raw, tl.duration, progress=lambda i, n: progress(i, int(tl.duration) + 1))
            data = self.proj.load()
            data["privacy"] = [b for b in data.get("privacy", []) if b.get("source") != "secret"] + boxes
            self.proj.save(data)
            return {"privacy": data["privacy"], "found": len(boxes)}
        self.start_job("Scanning for secrets", fn)

    def captions(self):
        from ..captions import transcribe

        def fn(progress):
            cues = transcribe(self.proj.dir)
            return {"cues": cues}
        self.start_job("Transcribing", fn)

    def speed_detect(self) -> list:
        from ..timemap import SpeedConfig, cached_activity, detect_speedups
        from .. import analysis
        analysis.ensure(self.proj.dir)
        data = self.proj.load()
        segs = detect_speedups(self.timeline(), cached_activity(self.proj.dir),
                               SpeedConfig.from_dict(data.get("speed_config")))
        data["speed"] = segs
        self.proj.save(data)
        return segs

    def start_export(self, body: dict):
        if self.job.snapshot()["running"]:
            raise RuntimeError("an export is already running")
        fmt = body.get("format", "mp4")
        over = {"output": {}}
        if body.get("width"):
            w = int(body["width"])
            over["output"]["width"] = w
            over["output"]["height"] = int(round(w * 9 / 16 / 2) * 2)
        if body.get("fps"):
            over["output"]["fps"] = int(body["fps"])
        self.job.update(running=True, done=0, total=0, fps=0.0, out=None, error=None, started=time.time())

        def run():
            try:
                from .. import analysis
                analysis.ensure(self.proj.dir)       # never export a take whose zoom plan is not there yet
                out = export(self.proj.dir, None, fmt, body.get("quality"),
                             progress=lambda i, n, el: self.job.update(done=i, total=n, fps=i / el if el else 0),
                             style_over=over, aspect=body.get("aspect") or None, notify=True)
                self.job.update(running=False, out=out.name)
            except Exception as e:  # pragma: no cover
                self.job.update(running=False, error=str(e))
        threading.Thread(target=run, daemon=True).start()


def _plan(tl, zcfg, aoi) -> list:
    """The automatic zooms for a timeline. A countdown recorded before the take (pre-roll)
    is not part of it: no zoom may start there."""
    from ..capture import _from_go
    return _from_go(build_segments(tl, zcfg, aoi), (getattr(tl, "capture", None) or {}).get("go"))


STALE_PAGE = b"""<!doctype html><meta charset="utf-8"><title>Dolly</title>
<body style="font:15px system-ui,sans-serif;background:#16161e;color:#c0caf5;margin:15vh auto;max-width:34em;padding:0 2em">
<h3>This Dolly window is from an earlier session</h3>
<p>The editor it belonged to has closed. Close this window and open the editor again
from the record button on the bar, or with <code>dolly edit</code>.</p>
<script>setTimeout(function(){ window.close() }, 4000)</script>"""


def make_handler(ed: Editor):
    class H(BaseHTTPRequestHandler):
        server_version = "dolly-editor"

        def log_message(self, *a):
            pass

        def _auth(self, q) -> bool:
            tok = q.get("t", [None])[0] or self.headers.get("X-Dolly-Token")
            if not isinstance(tok, str) or not hmac.compare_digest(tok.encode(), ed.token.encode()):
                if self.command == "GET" and urlparse(self.path).path in ("/", "/index.html"):
                    # a window left over from an editor that has exited, reloaded against a
                    # newer one: say so instead of the bare "Error response" page
                    self.send_response(403)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(STALE_PAGE)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(STALE_PAGE)
                else:
                    self.send_error(403)
                return False
            return True

        def _json(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def _body(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _file(self, path: Path, ctype=None):
            if not path.exists():
                return self.send_error(404)
            size = path.stat().st_size
            ctype = ctype or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            if rng:
                m = re.match(r"bytes=(\d*)-(\d*)", rng)
                if m:
                    if m.group(1):
                        start = int(m.group(1))
                        end = int(m.group(2)) if m.group(2) else size - 1
                    else:
                        start = size - int(m.group(2))
                end = min(end, size - 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    left -= len(chunk)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            ed.last_ping = time.time()
            if u.path in ("/", "/index.html"):
                if not self._auth(q):
                    return
                return self._file(STATIC / "index.html", "text/html; charset=utf-8")
            if u.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if u.path.startswith("/static/"):
                name = u.path[len("/static/"):]
                p = (STATIC / name).resolve()
                if STATIC.resolve() not in p.parents:
                    return self.send_error(404)
                return self._file(p)
            if not self._auth(q):
                return
            try:
                if u.path == "/api/app":
                    return self._json(ed.app_payload())
                if u.path == "/api/nav":
                    return self._json(ed.wait_nav(int(q.get("seq", ["0"])[0] or 0),
                                                  min(25.0, float(q.get("timeout", ["25"])[0] or 25))))
                if u.path == "/api/images":
                    from .. import images
                    return self._json(images.candidates())
                if u.path == "/media/image":
                    from .. import images
                    try:
                        p = images.check(q.get("path", [""])[0])
                    except images.ImageError as e:
                        return self._json({"error": str(e)}, 404)
                    return self._file(p)
                if u.path == "/api/settings/look":
                    from .. import config as cfgmod
                    st = cfgmod.style_defaults()
                    return self._json({"style": {k: st[k] for k in ("background", "padding", "radius", "shadow",
                                                                     "cursor", "keys", "theme")},
                                       "gradients": projmod.gradient_presets(projmod.style_colors(st))})
                if u.path == "/api/library":
                    from .. import library
                    return self._json(library.index(refresh=q.get("refresh", ["1"])[0] == "1"))
                if u.path == "/api/settings":
                    return self._json(ed.settings_payload())
                if u.path == "/media/thumb":
                    from .. import library
                    d = _library_dir(q.get("dir", [""])[0])
                    pth = library.poster(d)
                    return self._file(pth, "image/jpeg") if pth else self.send_error(404)
                if u.path == "/media/library-export":
                    d = _library_dir(q.get("dir", [""])[0])
                    return self._file(d / "exports" / Path(q.get("name", [""])[0]).name)
                if ed.proj is None and u.path != "/api/state" and not u.path.startswith("/media/wallpaper/") and \
                        (u.path.startswith("/api/") or u.path.startswith("/media/")):
                    return self._json({"error": "no recording open", "view": ed.view}, 409)
                if u.path == "/api/project":
                    return self._json(ed.project_payload())
                if u.path == "/api/state":
                    # theme (changes mid-session) + recorder/export phase, polled by the editor
                    from .. import health
                    return self._json({"theme": projmod.theme_snapshot(),
                                       "phase": health.get_phase().get("phase", "idle"), "jobs": health.jobs()})
                if u.path == "/api/plan":
                    return self._json(ed.plan_payload())
                if u.path == "/api/path":
                    return self._json(ed.path_payload(float(q.get("fps", ["30"])[0]), aspect=q.get("aspect", ["16:9"])[0]))
                if u.path == "/api/share/presets":
                    from .. import share
                    return self._json({"presets": share.estimate_all(ed.proj.dir)})
                if u.path == "/api/share/targets":
                    from ..share import config as scfg
                    try:
                        cfg = scfg.load()
                        return self._json({"targets": [{"name": n, "type": t["type"], "public": t["type"] == "litterbox"
                                                        or bool(t.get("public_base_url"))} for n, t in cfg["targets"].items()],
                                           "default": cfg["default"], "path": str(scfg.config_path())})
                    except scfg.ConfigError as e:
                        return self._json({"targets": [], "error": str(e), "path": str(scfg.config_path())})
                if u.path == "/api/share/history":
                    from .. import share
                    return self._json({"history": share.history(ed.proj.dir)})
                if u.path == "/api/export/status":
                    return self._json(ed.job.snapshot())
                if u.path == "/media/webcam.mp4":
                    cam = ensure_webcam_preview(ed.proj.dir)
                    return self._file(cam, "video/mp4") if cam else self.send_error(404)
                if u.path == "/media/raw.mp4":
                    prev = ed.proj.dir / "preview.mp4"
                    return self._file(prev if prev.exists() else ed.proj.raw, "video/mp4")
                if u.path.startswith("/media/wallpaper/"):
                    i = int(u.path.rsplit("/", 1)[1])
                    walls = projmod.wallpapers()
                    return self._file(walls[i]) if 0 <= i < len(walls) else self.send_error(404)
                if u.path.startswith("/media/export/"):
                    name = Path(u.path.rsplit("/", 1)[1]).name
                    return self._file(ed.proj.exports / name)
            except Exception as e:
                return self._json({"error": str(e)}, 500)
            self.send_error(404)

        def do_POST(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._auth(q):
                return
            ed.last_ping = time.time()
            try:
                if u.path == "/api/images/upload":
                    from .. import images
                    n = int(self.headers.get("Content-Length", 0) or 0)
                    if n <= 0 or n > images.MAX_BYTES:
                        return self._json({"error": "image is empty or larger than 50 MB"}, 413)
                    try:
                        return self._json(images.save_upload(self.rfile.read(n), q.get("name", [""])[0]))
                    except images.ImageError as e:
                        return self._json({"error": str(e)}, 400)
                body = self._body()
                if u.path == "/api/navigate":
                    pdir = body.get("dir")
                    try:
                        return self._json(ed.navigate(str(body.get("view") or "edit"), Path(pdir) if pdir else None,
                                                      section=body.get("section")))
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                if u.path == "/api/images/check":
                    from .. import images
                    try:
                        return self._json(images.info(body.get("path", "")))
                    except images.ImageError as e:
                        return self._json({"error": str(e)}, 400)
                if u.path.startswith("/api/library/"):
                    return self._json(library_action(ed, u.path.rsplit("/", 1)[1], body))
                if u.path == "/api/settings":
                    from .. import config as cfgmod
                    try:
                        cfgmod.set_many(body.get("updates") or {})
                    except cfgmod.ConfigError as e:
                        return self._json({"error": str(e)}, 400)
                    return self._json(ed.settings_payload())
                if ed.proj is None and u.path != "/api/quit":
                    return self._json({"error": "no recording open"}, 409)
                if u.path == "/api/save":
                    ed.save(body)
                    return self._json({"ok": True})
                if u.path == "/api/path":
                    return self._json(ed.path_payload(float(body.get("fps", 30)), body, body.get("aspect", "16:9")))
                if u.path == "/api/share/export":
                    ed.share_export(body)
                    return self._json({"ok": True})
                if u.path == "/api/share/copy":
                    from .. import share
                    f = ed.proj.exports / Path(body["file"]).name
                    share.copy_to_clipboard(ed.proj.dir, f, as_text=bool(body.get("path")))
                    return self._json({"ok": True})
                if u.path == "/api/share/upload":
                    from .. import share
                    f = ed.proj.exports / Path(body["file"]).name
                    return self._json(share.upload(ed.proj.dir, f, body.get("target") or None))
                if u.path == "/api/share/delete":
                    from .. import share
                    return self._json(share.delete_remote(ed.proj.dir, body["id"]))
                if u.path == "/api/speed/detect":
                    return self._json({"speed": ed.speed_detect()})
                if u.path == "/api/scrub":
                    ed.scrub()
                    return self._json({"ok": True})
                if u.path == "/api/captions":
                    ed.captions()
                    return self._json({"ok": True})
                if u.path == "/api/styles":
                    save_style(body["name"], body["style"])
                    return self._json({"styles": list_styles()})
                if u.path == "/api/auto":
                    return self._json(ed.auto(body))
                if u.path == "/api/export":
                    ed.save(body.get("project", {}))
                    ed.start_export(body)
                    return self._json({"ok": True})
                if u.path == "/api/open":
                    target = ed.proj.exports if not body.get("file") else ed.proj.exports / Path(body["file"]).name
                    subprocess.Popen(["xdg-open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return self._json({"ok": True})
                if u.path == "/api/quit":
                    self._json({"ok": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
            except Exception as e:
                return self._json({"error": str(e)}, 500)
            self.send_error(404)

    return H


def _version() -> str:
    try:
        return json.loads((Path(__file__).resolve().parents[2] / "manifest.json").read_text())["version"]
    except (OSError, ValueError, KeyError):
        return "?"


def _library_dir(d: str) -> Path:
    p = Path(d).resolve()
    root = projmod.videos_root().resolve()
    if root not in p.parents or not p.is_dir():
        raise ValueError("not a recording in the library")
    return p


def _public_target(name: str, t: dict) -> dict:
    """A share target without anything secret (no keys, env var names only as "set"/"missing")."""
    out = {"name": name, "type": t.get("type"), "public": t.get("type") == "litterbox" or bool(t.get("public_base_url"))}
    for k in ("bucket", "region", "endpoint", "remote", "prefix", "time", "link_expire", "presign_days"):
        if t.get(k) not in (None, ""):
            out[k] = t[k]
    creds = []
    for k in ("access_key_env", "secret_key_env"):
        if t.get(k):
            creds.append(f"{t[k]} {'set' if os.environ.get(t[k]) else 'missing'}")
    if t.get("profile"):
        creds.append(f"profile {t['profile']}")
    if t.get("access_key") or t.get("secret_key"):
        creds.append("inline keys (file is 0600)")
    out["credentials"] = ", ".join(creds) or ("rclone config" if t.get("type") == "rclone" else "none needed")
    return out


def hotkeys() -> list[dict]:
    """Dolly's bindings as written by dolly-setup (read-only in Settings)."""
    f = Path.home() / ".config/hypr/bindings.lua"
    out = []
    try:
        block = re.search(r"-- >>> dolly.*?-- <<< dolly", f.read_text(), re.S)
        for m in re.finditer(r'o\.bind\("([^"]+)",\s*"([^"]+)"', block.group(0) if block else ""):
            out.append({"keys": m.group(1).replace(" + ", "+"), "action": m.group(2)})
    except OSError:
        pass
    if not out:
        out = [{"keys": k, "action": a, "default": True} for k, a in (
            ("SUPER+ALT+R", "Dolly: record / stop demo"), ("SUPER+ALT+P", "Dolly: pause / resume recording"),
            ("SUPER+ALT+M", "Dolly: zoom here (while recording)"), ("SUPER+ALT+SHIFT+R", "Dolly: save instant replay"),
            ("SUPER+ALT+L", "Dolly: recordings library"))]
    return out


def mic_devices() -> list[dict]:
    import shutil
    if not shutil.which("pactl"):
        return []
    try:
        out = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return []
    devs = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) > 1 and not parts[1].endswith(".monitor"):
            devs.append({"name": parts[1]})
    return devs


def library_action(ed: "Editor", action: str, body: dict) -> dict:
    from .. import library
    if action == "open":
        return ed.open(_library_dir(body["dir"]))
    if action == "rename":
        return {"item": library.rename(_library_dir(body["dir"]), body.get("title", ""))}
    if action == "trash":
        dirs = [_library_dir(d) for d in body.get("dirs", [])]
        if ed.proj and any(d == ed.proj.dir.resolve() for d in dirs):
            ed.proj = None
            ed.view = "library"
        return library.trash(dirs)
    if action == "undo":
        return library.undo()
    if action == "cleanup":
        return library.cleanup(float(body.get("days", 0) or 0), dry_run=bool(body.get("dry_run")))
    if action == "reveal":
        d = _library_dir(body["dir"])
        f = library.best_file(d) or d
        subprocess.Popen(library.reveal(f), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return {"ok": True}
    if action == "copy":
        from .. import share
        d = _library_dir(body["dir"])
        f = library.best_file(d)
        if not f:
            raise ValueError("nothing to copy yet")
        share.copy_to_clipboard(d, f)
        return {"ok": True, "file": f.name}
    raise ValueError(f"unknown library action {action}")


def styles_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "dolly" / "styles"


def list_styles() -> list:
    d = styles_dir()
    out = []
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            out.append({"name": f.stem, "style": json.loads(f.read_text())})
        except ValueError:
            pass
    return out


def save_style(name: str, style: dict):
    name = "".join(c for c in name if c.isalnum() or c in " -_")[:40].strip() or "style"
    d = styles_dir()
    d.mkdir(parents=True, exist_ok=True)
    keep = {k: style[k] for k in ("background", "padding", "radius", "shadow", "cursor", "keys", "spotlight",
                                   "webcam", "title", "captions") if k in style}
    (d / f"{name}.json").write_text(json.dumps(keep, indent=2))


def ensure_webcam_preview(pdir: Path) -> Path | None:
    """A browser-friendly proxy of webcam.mkv for the bubble preview."""
    from ..render import ffmpeg_bin
    cam = next((p for p in (pdir / "webcam.mkv", pdir / "webcam.mp4") if p.exists()), None)
    if not cam:
        return None
    prev = pdir / "webcam-preview.mp4"
    if not prev.exists() or prev.stat().st_mtime < cam.stat().st_mtime:
        subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-i", str(cam), "-vf", "scale=-2:360", "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", "24", "-g", "15", "-pix_fmt", "yuv420p", "-an",
                        "-movflags", "+faststart", str(prev)], check=False)
    return prev if prev.exists() else None


def ensure_preview(pdir: Path):
    """Browsers can't always decode the raw capture (e.g. 4:4:4, huge res,
    VFR). Make a light, seekable, all-keyframe-ish preview proxy once."""
    from ..render import ffmpeg_bin
    prev = pdir / "preview.mp4"
    raw = projmod.Project(pdir).raw
    if prev.exists() and prev.stat().st_mtime >= raw.stat().st_mtime:
        return
    scale = "scale='min(1920,iw)':-2"
    part = pdir / f"preview.{os.getpid()}.part.mp4"       # renamed when complete: never half a preview
    try:
        r = subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-i", str(raw), "-vf", scale, "-c:v", "libx264",
                            "-preset", "veryfast", "-crf", "20", "-g", "15", "-pix_fmt", "yuv420p", "-an",
                            "-movflags", "+faststart", str(part)], check=False)
        if r.returncode == 0 and part.exists() and part.stat().st_size > 0:
            part.replace(prev)
    finally:
        part.unlink(missing_ok=True)


def _registry_file() -> Path:
    from ..health import runtime_dir
    return runtime_dir() / "editor.json"


def _attach(view: str, pdir: Path | None, section: str | None = None):
    """Reuse the running editor: ask it to show `view` (and `pdir`), return (url, page_open)
    or None when there is no live editor. A navigation the editor refuses (e.g. a recording
    whose raw video was cleaned up) is an error for the caller, not a reason for a second editor."""
    import urllib.error
    import urllib.request
    f = _registry_file()
    try:
        reg = json.loads(f.read_text())
        base, tok = f"http://127.0.0.1:{int(reg['port'])}", str(reg["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None

    def call(path, body=None, timeout=2.0):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Dolly-Token": tok, "Content-Type": "application/json"},
                                     method="GET" if body is None else "POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")

    try:
        call("/api/app")
    except (OSError, ValueError, urllib.error.URLError):
        f.unlink(missing_ok=True)                  # a stale entry (editor exited or crashed)
        return None
    try:
        st = call("/api/navigate", {"view": view, "dir": str(pdir) if pdir else None, "section": section},
                  timeout=120)
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error") or str(e)
        except ValueError:
            msg = str(e)
        raise SystemExit(f"dolly: {msg}") from None
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return f"{base}/?t={tok}#{st['view']}", bool(st.get("page"))


def focus_window(address: str) -> bool:
    """Focus a window by address (dolly.hypr: Lua dispatcher on Hyprland 0.55+, else legacy)."""
    import shutil
    if not shutil.which("hyprctl"):
        return False
    from .. import hypr
    return hypr.focus(address)


def raise_window(address: str) -> bool:
    """On top of other windows without taking focus (a floating workspace would otherwise
    leave the editor under the window you were using)."""
    import shutil
    if not shutil.which("hyprctl"):
        return False
    from .. import hypr
    return hypr.raise_top(address)


def editor_windows() -> list[dict]:
    """Open editor app windows (Chromium --app on 127.0.0.1, page title "Dolly …"),
    most recently focused first."""
    from ..logger import hypr_json
    wins = [c for c in (hypr_json("clients") or [])
            if str(c.get("class", "")).startswith("chrome-127.0.0.1") and "dolly" in str(c.get("title", "")).lower()]
    return sorted(wins, key=lambda c: c.get("focusHistoryID", 1 << 30))


def serve(pdir: Path | None, port: int = 0, open_browser: bool = True, view: str = "edit",
          section: str | None = None):
    keep_focus = os.environ.get("DOLLY_KEEP_FOCUS") == "1"
    if os.environ.get("DOLLY_EDITOR_NEW") != "1" and not port:
        got = _attach(view, Path(pdir).resolve() if pdir else None, section)
        if got:
            url, page_open = got
            if pdir:
                (Path(pdir) / ".editor-url").write_text(url)
            print(url, flush=True)
            if open_browser:
                wins = editor_windows() if page_open else []
                if not wins:
                    launch(url)                    # the server outlived its window: one new window
                elif not keep_focus:
                    focus_window(wins[0]["address"])
                else:                              # after a recording: visible, but no focus taken
                    raise_window(wins[0]["address"])
            return
    ed = Editor(pdir, view=view)
    ed.section = str(section) if section and view == "settings" else None
    ed.start_preview()                     # in the background: the window does not wait for it
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(ed))
    url = f"http://127.0.0.1:{httpd.server_address[1]}/?t={ed.token}#{ed.view}"
    reg = _registry_file()
    try:
        reg.parent.mkdir(parents=True, exist_ok=True)
        tmp = reg.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "port": httpd.server_address[1], "token": ed.token}))
        os.chmod(tmp, 0o600)                       # the token drives the editor: user-only
        tmp.replace(reg)
    except OSError:
        reg = None
    if pdir:
        (Path(pdir) / ".editor-url").write_text(url)
    print(url, flush=True)
    if open_browser:
        launch(url)

    def idle_watch():  # exit when the page has been gone for a while
        while True:
            time.sleep(15)
            if time.time() - ed.last_ping > 300 and not ed.job.snapshot()["running"]:
                httpd.shutdown()
                return
    threading.Thread(target=idle_watch, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if reg is not None and json.loads(reg.read_text()).get("pid") == os.getpid():
                reg.unlink()
        except (OSError, ValueError):
            pass


def launch(url: str):
    """Open as an app window (Omarchy web-app style) when possible. After a recording
    (DOLLY_KEEP_FOCUS=1) the window must not steal focus: launch it with Hyprland's
    no_initial_focus exec rule, and if the browser still grabbed focus (an already
    running browser owns the new window, so the rule cannot match), hand focus back."""
    if os.environ.get("DOLLY_NO_BROWSER"):
        return
    import shutil
    from ..health import exec_unfocused
    keep = os.environ.get("DOLLY_KEEP_FOCUS") == "1"
    prev = None
    if keep:
        from ..logger import hypr_json
        prev = (hypr_json("activewindow") or {}).get("address")
    cmd = None
    if os.environ.get("DOLLY_BROWSER"):            # e.g. a Chromium wrapper (test harness: Wayland flags)
        cmd = [os.environ["DOLLY_BROWSER"], f"--app={url}"]
    elif shutil.which("omarchy-launch-webapp"):
        cmd = ["omarchy-launch-webapp", url]
    else:
        for b in ("chromium", "google-chrome-stable", "brave", "google-chrome"):
            if shutil.which(b):
                cmd = [b, f"--app={url}"]
                break
    if cmd is None:
        webbrowser.open(url)
        return
    if keep:
        exec_unfocused(cmd)
        if prev:
            threading.Thread(target=_give_focus_back, args=(prev,), daemon=True).start()
    else:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _give_focus_back(prev: str, timeout: float = 8.0):
    from ..logger import hypr_json
    t_end = time.time() + timeout
    while time.time() < t_end:
        time.sleep(0.25)
        aw = (hypr_json("activewindow") or {})
        if aw.get("address") and aw.get("address") != prev and "dolly" in (aw.get("title") or "").lower():
            focus_window(prev)          # Lua dispatcher on Hyprland 0.55+ (the legacy one fails there)
            raise_window(aw["address"])  # focusing prev raised it over the editor on floating layouts
            return


def capture_bakes_cursor() -> bool:
    """Would a recording on this system have the real cursor baked into the video
    (wf-recorder: gpu-screen-recorder missing or unable to capture here, e.g. a VM)?"""
    import shutil
    try:
        from .. import deps
        if not shutil.which("gpu-screen-recorder"):
            return bool(shutil.which("wf-recorder"))
        return bool(deps.gsr_unusable())
    except Exception:
        return False
