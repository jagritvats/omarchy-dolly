"""Upload targets: S3-compatible (AWS S3, Cloudflare R2, Backblaze B2, MinIO),
rclone remotes (Google Drive, Dropbox, OneDrive, ... via `rclone link`), and
an opt-in temporary public host (Litterbox).

S3 uses a small self-contained AWS SigV4 query-signing implementation (stdlib
only; checked against botocore in tests) so Dolly needs no boto3.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import mimetypes
import re
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import config as cfgmod


class UploadError(RuntimeError):
    pass


# RFC 3986: every character a URL may contain. No space, quote, angle bracket or control code.
_URL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~:/?#[]@!$&'()*+,;=%")
MAX_LINK = 8192            # a presigned S3 link with a session token runs to a few thousand


def checked_link(url, source: str = "the upload host") -> str:
    """A link that came back from somewhere else (an upload host's answer, rclone's output),
    before it is copied to the clipboard, saved in the share history and shown in the editor:
    one http(s) URL and nothing else. Anything more (markup, a second line that would run
    when pasted into a terminal) is an UploadError."""
    url = str(url or "").strip()
    try:
        u = urlsplit(url)
        ok = u.scheme in ("http", "https") and bool(u.hostname)
    except ValueError:
        ok = False
    if not ok or len(url) > MAX_LINK or not _URL_CHARS.issuperset(url):
        raise UploadError(f"{source} did not answer with a link: {url[:120]!r}")
    return url


def remote_name(local: Path, prefix: str = "") -> str:
    """Unguessable object name (public buckets are often listable by URL guessing)."""
    stem = "".join(c if c.isalnum() or c in "-_." else "-" for c in local.stem)[:60]
    return f"{prefix}{time.strftime('%Y/%m')}/{stem}-{secrets.token_hex(4)}{local.suffix}"


# =========================================================== S3 / SigV4
def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def presign(method: str, endpoint: str, region: str, bucket: str, key: str, access_key: str, secret_key: str,
            expires: int = 3600, addressing: str = "auto", session_token: str | None = None,
            now: dt.datetime | None = None) -> str:
    """AWS Signature V4 presigned URL (query auth, UNSIGNED-PAYLOAD)."""
    if not endpoint:
        endpoint = f"https://s3.{region}.amazonaws.com"
    u = urlsplit(endpoint)
    host = u.netloc
    if addressing == "auto":
        addressing = "virtual" if host.endswith("amazonaws.com") and "." not in bucket else "path"
    key_path = quote(key, safe="/-_.~")
    if addressing == "virtual":
        host = f"{bucket}.{host}"
        path = "/" + key_path
    else:
        path = f"/{quote(bucket, safe='')}" + ("/" + key_path if key else "")
    now = now or dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    scope = f"{datestamp}/{region}/s3/aws4_request"
    params = {"X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": f"{access_key}/{scope}",
              "X-Amz-Date": amz_date, "X-Amz-Expires": str(int(expires)), "X-Amz-SignedHeaders": "host"}
    if session_token:
        params["X-Amz-Security-Token"] = session_token
    cq = "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(params.items()))
    creq = "\n".join([method, path, cq, f"host:{host}", "", "host", "UNSIGNED-PAYLOAD"])
    sts = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(creq.encode()).hexdigest()])
    k = _sign(_sign(_sign(_sign(("AWS4" + secret_key).encode(), datestamp), region), "s3"), "aws4_request")
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    return f"{u.scheme}://{host}{path}?{cq}&X-Amz-Signature={sig}"


class S3Target:
    kind = "s3"

    def __init__(self, name: str, t: dict):
        self.name, self.t = name, t
        self.bucket = t["bucket"]
        self.region = t.get("region", "us-east-1")
        self.endpoint = t.get("endpoint", "")
        self.addressing = t.get("addressing", "auto")

    def _url(self, method, key, expires=900):
        ak, sk, tok = cfgmod.resolve_s3_credentials(self.t)
        return presign(method, self.endpoint, self.region, self.bucket, key, ak, sk, expires,
                       self.addressing, tok)

    def upload(self, path: Path, progress=None) -> dict:
        key = remote_name(path, self.t.get("prefix", ""))
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            req = urllib.request.Request(self._url("PUT", key), data=f, method="PUT",
                                         headers={"Content-Length": str(size), "Content-Type": ctype})
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    r.read()
            except urllib.error.HTTPError as e:
                raise UploadError(f"S3 PUT failed: {e.code} {e.read()[:300]!r}") from e
            except urllib.error.URLError as e:
                raise UploadError(f"S3 endpoint unreachable: {e.reason}") from e
        if self.t.get("public_base_url"):
            url = self.t["public_base_url"].rstrip("/") + "/" + quote(key, safe="/-_.~")
            expires_at, public = None, True
        else:
            days = min(7, float(self.t.get("presign_days", 7)))  # SigV4 maximum is 7 days
            secs = int(days * 86400)
            url = self._url("GET", key, secs)
            expires_at, public = time.time() + secs, False
        return {"url": url, "public": public, "expires_at": expires_at, "deletable": True,
                "remote_ref": {"bucket": self.bucket, "key": key}}

    def delete(self, ref: dict):
        if ref.get("bucket") != self.bucket or not isinstance(ref.get("key"), str) or not ref["key"]:
            raise UploadError("this upload is not in the configured bucket")
        req = urllib.request.Request(self._url("DELETE", ref["key"]), method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                r.read()
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise UploadError(f"S3 DELETE failed: {e.code}") from e


# =========================================================== rclone
class RcloneTarget:
    kind = "rclone"

    def __init__(self, name: str, t: dict):
        self.name, self.t = name, t
        self.remote = t["remote"].rstrip("/")
        self.exe = shutil.which(t.get("rclone", "rclone"))

    def _run(self, *args, timeout=900) -> str:
        if not self.exe:
            raise UploadError("rclone not installed (package rclone; then `rclone config`)")
        r = subprocess.run([self.exe, *args], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise UploadError(f"rclone {args[0]} failed: {(r.stderr or r.stdout).strip()[:400]}")
        return r.stdout.strip()

    def upload(self, path: Path, progress=None) -> dict:
        name = remote_name(path, self.t.get("prefix", ""))
        dest = f"{self.remote}/{name}"
        self._run("copyto", str(path), dest)
        link, note, expires_at = None, None, None
        args = ["link", dest]
        if self.t.get("link_expire"):
            args += ["--expire", self.t["link_expire"]]
        try:
            link = checked_link((self._run(*args, timeout=120).splitlines() or [""])[-1], "rclone link")
            if self.t.get("link_expire"):
                expires_at = time.time() + _duration_seconds(self.t["link_expire"])
        except UploadError as e:
            link, note = None, f"uploaded, but this remote cannot make links ({e})"
        return {"url": link, "public": bool(link), "expires_at": expires_at, "deletable": True,
                "remote_ref": {"path": dest}, "note": note}

    def delete(self, ref: dict):
        path = ref.get("path")
        if not isinstance(path, str) or not path.startswith(self.remote + "/") or "/../" in path:
            raise UploadError("this upload is not on the configured remote")
        self._run("deletefile", path, timeout=120)


def _header_name(name: str) -> str:
    """A file name as the value of a multipart header: no quote, backslash or line break."""
    return re.sub(r'["\\\x00-\x1f\x7f]', "_", name)


def _duration_seconds(s: str) -> float:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    s = s.strip().lower()
    return float(s[:-1]) * units[s[-1]] if s and s[-1] in units else float(s)


# =========================================================== Litterbox (public, temporary)
class LitterboxTarget:
    """https://litterbox.catbox.moe: anonymous, temporary (1h/12h/24h/72h), max 1 GB,
    anyone with the link can download, no delete. Opt-in only ("public": true)."""
    kind = "litterbox"
    ENDPOINT = "https://litterbox.catbox.moe/resources/internals/api.php"
    TIMES = {"1h": 3600, "12h": 43200, "24h": 86400, "72h": 259200}

    def __init__(self, name: str, t: dict):
        self.name, self.t = name, t
        self.time = t.get("time", "24h")
        if self.time not in self.TIMES:
            raise cfgmod.ConfigError(f"litterbox time must be one of {list(self.TIMES)}")
        self.endpoint = t.get("endpoint", self.ENDPOINT)

    def upload(self, path: Path, progress=None) -> dict:
        if path.stat().st_size > 1_000_000_000:
            raise UploadError("Litterbox limit is 1 GB")
        boundary = "----dolly" + secrets.token_hex(12)
        fields = [("reqtype", "fileupload"), ("time", self.time)]
        pre = b"".join(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
                       for k, v in fields)
        pre += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"fileToUpload\"; filename=\"{_header_name(path.name)}\"\r\n"
                f"Content-Type: {mimetypes.guess_type(path.name)[0] or 'application/octet-stream'}\r\n\r\n").encode()
        post = f"\r\n--{boundary}--\r\n".encode()
        size = len(pre) + path.stat().st_size + len(post)

        def body():
            yield pre
            with open(path, "rb") as f:
                while chunk := f.read(1 << 20):
                    yield chunk
            yield post
        req = urllib.request.Request(self.endpoint, data=body(), method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                                              "Content-Length": str(size), "User-Agent": "dolly/0.2"})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                answer = r.read(MAX_LINK + 2).decode("utf-8", "replace").strip()     # a link, not a page
        except urllib.error.URLError as e:
            raise UploadError(f"Litterbox upload failed: {e}") from e
        if not answer.startswith("http"):
            raise UploadError(f"Litterbox said: {answer[:200]!r}")
        url = checked_link(answer, "Litterbox")
        return {"url": url, "public": True, "expires_at": time.time() + self.TIMES[self.time],
                "deletable": False, "remote_ref": {}, "note": f"public temporary link, expires in {self.time}"}

    def delete(self, ref: dict):
        raise UploadError("Litterbox files cannot be deleted; they expire on their own")


def make(name: str, t: dict):
    return {"s3": S3Target, "rclone": RcloneTarget, "litterbox": LitterboxTarget}[t["type"]](name, t)


def get_target(name: str | None = None):
    cfg = cfgmod.load()
    if not cfg["targets"]:
        raise cfgmod.ConfigError(f"no share targets configured (local-only). Run `dolly share init` and edit "
                                 f"{cfgmod.config_path()}")
    if not name:
        try:                       # Settings > Sharing > Default target (when still configured)
            from ..config import load as load_settings
            pick = load_settings()["sharing"]["default_target"]
            name = pick if pick in cfg["targets"] else None
        except Exception:
            name = None
    name = name or cfg["default"] or next(iter(cfg["targets"]))
    if name not in cfg["targets"]:
        raise cfgmod.ConfigError(f"no target named '{name}' (have: {', '.join(cfg['targets'])})")
    return make(name, cfg["targets"][name])
