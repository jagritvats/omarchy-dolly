"""Share targets config: ~/.config/dolly/share.json (override: $DOLLY_SHARE_CONFIG).

Local-only by default: with no file (or no targets) nothing is ever uploaded.
Credentials are never bundled. They come from, in order:
  * rclone's own config (type "rclone"), Dolly never sees them
  * environment variables named in the target ("access_key_env", ...)
  * an AWS-style profile ("profile": "name" in ~/.aws/credentials)
  * inline values in share.json, refused unless the file is mode 0600

Example:
{
  "default": "r2",
  "targets": {
    "r2":     {"type": "s3", "endpoint": "https://<account>.r2.cloudflarestorage.com", "region": "auto",
               "bucket": "demos", "prefix": "dolly/", "public_base_url": "https://demos.example.dev",
               "access_key_env": "R2_ACCESS_KEY_ID", "secret_key_env": "R2_SECRET_ACCESS_KEY"},
    "s3":     {"type": "s3", "region": "eu-west-1", "bucket": "my-demos", "profile": "default",
               "presign_days": 7},
    "gdrive": {"type": "rclone", "remote": "gdrive:Dolly", "link_expire": "7d"},
    "temp":   {"type": "litterbox", "time": "24h", "public": true}
  }
}
"""
from __future__ import annotations

import configparser
import json
import os
import stat
from pathlib import Path

SECRET_FIELDS = ("access_key", "secret_key", "session_token")

TEMPLATE = {
    "default": None,
    "targets": {
        "_example_r2": {"type": "s3", "enabled": False, "endpoint": "https://ACCOUNT.r2.cloudflarestorage.com",
                        "region": "auto", "bucket": "demos", "prefix": "dolly/",
                        "public_base_url": "https://demos.example.dev",
                        "access_key_env": "R2_ACCESS_KEY_ID", "secret_key_env": "R2_SECRET_ACCESS_KEY"},
        "_example_b2": {"type": "s3", "enabled": False, "endpoint": "https://s3.us-west-004.backblazeb2.com",
                        "region": "us-west-004", "bucket": "demos", "presign_days": 7,
                        "access_key_env": "B2_KEY_ID", "secret_key_env": "B2_APP_KEY"},
        "_example_gdrive": {"type": "rclone", "enabled": False, "remote": "gdrive:Dolly", "link_expire": "7d"},
        "_example_temp_public": {"type": "litterbox", "enabled": False, "time": "24h", "public": True},
    },
}


class ConfigError(RuntimeError):
    pass


def config_path() -> Path:
    if os.environ.get("DOLLY_SHARE_CONFIG"):
        return Path(os.environ["DOLLY_SHARE_CONFIG"])
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "dolly" / "share.json"


def load() -> dict:
    """Also refreshes ~/.local/state/dolly/share-ready (the menu's pure-shell guard)."""
    try:
        cfg = _load()
    except ConfigError:
        _ready(False)
        raise
    _ready(bool(cfg["targets"]))
    return cfg


def _ready(flag: bool):
    try:
        from ..guards import set_share_ready
        set_share_ready(flag)
    except OSError:
        pass


def _load() -> dict:
    p = config_path()
    if not p.exists():
        return {"default": None, "targets": {}}
    try:
        cfg = json.loads(p.read_text())
    except ValueError as e:
        raise ConfigError(f"{p}: invalid JSON ({e})") from e
    targets = {k: v for k, v in (cfg.get("targets") or {}).items()
               if isinstance(v, dict) and v.get("enabled", True) and not k.startswith("_")}
    inline = [k for k, v in targets.items() if any(v.get(f) for f in SECRET_FIELDS)]
    if inline:
        mode = stat.S_IMODE(p.stat().st_mode)
        if mode & 0o077:
            raise ConfigError(f"{p} holds credentials for {', '.join(inline)} but is mode {oct(mode)}; "
                              f"run: chmod 600 {p}")
    for name, t in targets.items():
        if t.get("type") not in ("s3", "rclone", "litterbox"):
            raise ConfigError(f"target '{name}': unknown type {t.get('type')!r}")
        if t["type"] == "litterbox" and t.get("public") is not True:
            raise ConfigError(f"target '{name}': temporary public hosts need \"public\": true to confirm "
                              f"that anyone with the link can download the video")
    default = cfg.get("default")
    if default and default not in targets:
        default = None
    return {"default": default, "targets": targets, "path": str(p)}


def init_template() -> Path:
    p = config_path()
    if p.exists():
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(TEMPLATE, f, indent=2)
    return p


def resolve_s3_credentials(t: dict) -> tuple[str, str, str | None]:
    ak = sk = tok = None
    if t.get("access_key_env"):
        ak = os.environ.get(t["access_key_env"])
    if t.get("secret_key_env"):
        sk = os.environ.get(t["secret_key_env"])
    if t.get("session_token_env"):
        tok = os.environ.get(t["session_token_env"])
    if (not ak or not sk) and t.get("profile"):
        cp = configparser.ConfigParser()
        cp.read(os.environ.get("AWS_SHARED_CREDENTIALS_FILE", str(Path.home() / ".aws" / "credentials")))
        if cp.has_section(t["profile"]):
            sec = cp[t["profile"]]
            ak = ak or sec.get("aws_access_key_id")
            sk = sk or sec.get("aws_secret_access_key")
            tok = tok or sec.get("aws_session_token")
    ak = ak or t.get("access_key")
    sk = sk or t.get("secret_key")
    tok = tok or t.get("session_token")
    if not ak or not sk:
        if not t.get("access_key_env") and not t.get("profile"):
            ak = ak or os.environ.get("AWS_ACCESS_KEY_ID")
            sk = sk or os.environ.get("AWS_SECRET_ACCESS_KEY")
            tok = tok or os.environ.get("AWS_SESSION_TOKEN")
    if not ak or not sk:
        raise ConfigError("no S3 credentials: set the env vars named in access_key_env/secret_key_env, "
                          "a profile, or inline keys in a 0600 share.json")
    return ak, sk, tok
