"""PATH without some commands. Dropping whole directories that contain one (the
first approach) removes /usr/bin on Arch, and with it bash, python3 and env.
Directories that contain a hidden command are mirrored with symlinks to everything
else in them."""
import os
from pathlib import Path


def path_without(names, tmp: Path, path: str | None = None) -> str:
    names = set([names] if isinstance(names, str) else names)
    out = []
    for d in (path or os.environ["PATH"]).split(":"):
        if not d or not Path(d).is_dir():
            continue
        if any((Path(d) / n).exists() for n in names):
            mirror = tmp / ("pathmirror" + d.replace("/", "_"))
            if not mirror.exists():
                mirror.mkdir(parents=True)
                for f in Path(d).iterdir():
                    if f.name not in names:
                        (mirror / f.name).symlink_to(f)
            d = str(mirror)
        out.append(d)
    return ":".join(out)
