"""Background images the editor may show and render: safe lookup, listing and upload.

The editor is a local web page; it never sends file contents back to Dolly except
through upload(). Paths it names are checked here before anything is read:
an existing regular file, an image type, a sane size, a real image by its header (and a
whole one: a PNG's checksums; an upload is decoded once), and inside the user's home or
the system's shared data (theme backgrounds)."""
from __future__ import annotations

import hashlib
import os
import struct
import zlib
from pathlib import Path

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
MAX_BYTES = 50 * 1024 * 1024
MAX_PIXELS = 80_000_000
LIST_LIMIT = 200


class ImageError(ValueError):
    pass


def allowed_roots() -> list[Path]:
    roots = [Path.home(), Path("/usr/share"), Path("/usr/local/share")]
    extra = os.environ.get("DOLLY_IMAGE_ROOTS")          # tests / unusual setups, ':'-separated
    if extra:
        roots += [Path(p) for p in extra.split(":") if p]
    out = []
    for r in roots:
        try:
            out.append(r.resolve())
        except OSError:
            continue
    return out


def uploads_dir() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return base / "dolly" / "backgrounds"


def check(path: str | os.PathLike) -> Path:
    """The resolved path of a usable background image, or ImageError (never reads outside
    the allowed roots, follows symlinks before checking)."""
    if not isinstance(path, (str, os.PathLike)) or not str(path).strip():
        raise ImageError("no image path")
    s = str(path)
    if "\0" in s or "\n" in s:
        raise ImageError("invalid path")
    p = Path(s).expanduser()
    if not p.is_absolute():
        raise ImageError("use an absolute path (for example ~/Pictures/wall.png)")
    try:
        p = p.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ImageError(f"not found: {s}") from None
    if not any(p == r or r in p.parents for r in allowed_roots()):
        raise ImageError("images must be in your home folder or a theme's backgrounds")
    if p.suffix.lower() not in IMAGE_EXT:
        raise ImageError("not an image (PNG, JPEG or WebP)")
    if not p.is_file():
        raise ImageError("not a file")
    if p.stat().st_size > MAX_BYTES:
        raise ImageError("image is larger than 50 MB")
    _verify(p)
    return p


def _verify(p: Path, decode: bool = False) -> tuple[int, int]:
    """(width, height) of a PNG, JPEG or WebP file, told by its first bytes and read from
    its header: nothing is decoded and no program is started, so this also works on a
    machine that cannot draw. decode=True reads the whole picture once as well (an upload,
    before it is kept); a file damaged further in otherwise shows when the renderer loads
    it, which then draws its gradient instead."""
    from . import gfx
    try:
        w, h = gfx.image_size(p)
    except gfx.ImageError:
        raise ImageError("the file is not a readable image") from None
    if w * h > MAX_PIXELS:
        raise ImageError("image has too many pixels")
    if not _png_intact(p):
        raise ImageError("the file is not a readable image")
    if decode and gfx.available():
        try:
            gfx.load(p, size=(64, 64))
        except gfx.ImageError:
            raise ImageError("the file is not a readable image") from None
    return w, h


def _png_intact(p: Path) -> bool:
    """Whether every chunk of a PNG file is whole and has its checksum right, up to the
    closing one (a cut-off or damaged download). True for a file that is not a PNG."""
    with open(p, "rb") as f:
        if f.read(8) != b"\x89PNG\r\n\x1a\n":
            return True
        while True:
            head = f.read(8)
            if len(head) < 8:
                return False
            n, tag = struct.unpack(">I4s", head)
            body = f.read(n + 4)
            if len(body) < n + 4 or zlib.crc32(tag + body[:n]) != struct.unpack(">I", body[n:])[0]:
                return False
            if tag == b"IEND":
                return True


def info(path) -> dict:
    p = check(path)
    w, h = _verify(p)
    return {"path": str(p), "name": p.name, "width": w, "height": h}


def _scan(d: Path, depth: int, out: list, seen: set):
    if depth < 0 or len(out) >= LIST_LIMIT:
        return
    try:
        entries = sorted(d.iterdir(), key=lambda e: e.name.lower())
    except OSError:
        return
    for e in entries:
        if len(out) >= LIST_LIMIT:
            return
        if e.name.startswith("."):
            continue
        try:
            if e.is_dir():
                _scan(e, depth - 1, out, seen)
            elif e.suffix.lower() in IMAGE_EXT and e.is_file():
                rp = e.resolve()
                if rp not in seen:
                    seen.add(rp)
                    out.append(rp)
        except OSError:
            continue


def candidates() -> dict:
    """Images to offer: the theme's backgrounds, ~/Pictures (two levels deep) and
    images uploaded before. Paths only; the page loads them through /media/image."""
    from . import project as projmod
    seen: set = set()
    theme = []
    for p in projmod.wallpapers():
        rp = Path(p).resolve()
        if rp not in seen:
            seen.add(rp)
            theme.append(rp)
    pictures: list = []
    pics = Path(os.environ.get("XDG_PICTURES_DIR") or Path.home() / "Pictures")
    if pics.is_dir():
        _scan(pics, 2, pictures, seen)
    uploaded: list = []
    if uploads_dir().is_dir():
        _scan(uploads_dir(), 0, uploaded, seen)
    fmt = lambda ps: [{"path": str(p), "name": p.name} for p in ps]
    return {"theme": fmt(theme), "pictures": fmt(pictures), "uploaded": fmt(uploaded),
            "pictures_dir": str(pics)}


def save_upload(data: bytes, name: str) -> dict:
    """Store an image the user picked in the browser's file dialog (content-addressed,
    so the same file twice is one copy) and return its info."""
    if not data:
        raise ImageError("empty upload")
    if len(data) > MAX_BYTES:
        raise ImageError("image is larger than 50 MB")
    ext = Path(name or "").suffix.lower()
    if ext not in IMAGE_EXT:
        raise ImageError("not an image (PNG, JPEG or WebP)")
    from . import fsutil
    d = fsutil.private_dir(uploads_dir())   # the original may have sat in a private folder
    out = d / (hashlib.sha256(data).hexdigest()[:16] + (".jpg" if ext == ".jpeg" else ext))
    if not out.exists():
        tmp = out.with_suffix(out.suffix + ".part")
        tmp.unlink(missing_ok=True)
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
            f.write(data)
        try:
            _verify(tmp, decode=True)
        except ImageError:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(out)
    return info(out)
