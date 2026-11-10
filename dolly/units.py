"""Human-readable sizes, one rule for every place Dolly shows a size (toasts, the
library, the bar panel's recent list, the editor). The editor's fmtSize (JS) mirrors it."""
from __future__ import annotations


def human_size(n: float | int | None) -> str:
    """412 KB under 1 MB, 4.7 MB under 10 MB, 41 MB under 1 GB, then 1.3 GB.
    Decimal units (1 MB = 10^6 bytes), like file managers and the export size caps.
    Rounding never shows a value of the next unit ("1000 KB" is "1.0 MB")."""
    b = max(0.0, float(n or 0))
    if b == 0:
        return "0 KB"
    half_up = lambda x: int(x + 0.5)          # like JS Math.round (Python's round() is banker's)
    kb = half_up(b / 1e3)
    if kb < 1000:
        return f"{max(1, kb)} KB"
    mb1 = half_up(b / 1e5) / 10
    if mb1 < 10:
        return f"{mb1:.1f} MB"
    mb = half_up(b / 1e6)
    if mb < 1000:
        return f"{mb} MB"
    return f"{b / 1e9:.1f} GB"
