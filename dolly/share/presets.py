"""Platform presets.

Limits checked Nov 2026 (re-check yearly; they change):
  GitHub   videos 10 MB on free-plan repos (100 MB on paid), images/GIFs 10 MB;
           MP4/MOV/WebM, H.264 recommended.
           https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/attaching-files
  Discord  free upload limit 20 MB since 2026-08-13 (was 10 MB from 2024).
  X        512 MB, 140 s for standard accounts (longer needs Premium).
  LinkedIn 500 MB via API (web uploads allow more), up to 30 min.
  Slack    1 GB per file.
  YouTube  256 GB / 12 h; Shorts up to 3 min, vertical.

cap_mb is a hard size ceiling: the encoder targets ~95 % of it with two-pass
bitrate control and verifies the result. None = no ceiling (quality CRF).
"""
from __future__ import annotations

PRESETS: dict[str, dict] = {
    "github": {"label": "GitHub PR / issue", "icon": "󰊤", "format": "mp4", "aspect": "16:9",
               "width": 1280, "height": 720, "fps": 30, "cap_mb": 10,
               "note": "10 MB video limit on free-plan repos (100 MB on paid plans)"},
    "github-gif": {"label": "GitHub GIF", "icon": "󰵸", "format": "gif", "aspect": "16:9",
                   "width": 800, "height": 450, "fps": 12, "cap_mb": 10,
                   "note": "10 MB image/GIF limit; shrinks width/fps until it fits"},
    "discord": {"label": "Discord", "icon": "󰙯", "format": "mp4", "aspect": "16:9",
                "width": 1920, "height": 1080, "fps": 30, "cap_mb": 20,
                "note": "20 MB free upload limit (Aug 2026)"},
    "slack": {"label": "Slack", "icon": "󰒱", "format": "mp4", "aspect": "16:9",
              "width": 1920, "height": 1080, "fps": 60, "cap_mb": 1000, "note": "1 GB file limit"},
    "x": {"label": "X", "icon": "󰕄", "format": "mp4", "aspect": "16:9",
          "width": 1920, "height": 1080, "fps": 30, "cap_mb": 512, "max_duration": 140,
          "note": "512 MB, 2:20 max for standard accounts"},
    "linkedin": {"label": "LinkedIn", "icon": "󰌻", "format": "mp4", "aspect": "1:1",
                 "width": 1080, "height": 1080, "fps": 30, "cap_mb": 500, "max_duration": 1800,
                 "note": "square reframe; 500 MB API limit"},
    "youtube": {"label": "YouTube", "icon": "󰗃", "format": "mp4", "aspect": "16:9",
                "width": 2560, "height": 1440, "fps": 60, "cap_mb": None, "quality": "best",
                "note": "high bitrate, source-ish resolution"},
    "shorts": {"label": "Shorts / Reels / TikTok", "icon": "󰄄", "format": "mp4", "aspect": "9:16",
               "width": 1080, "height": 1920, "fps": 30, "cap_mb": None, "max_duration": 180,
               "note": "vertical auto-reframe follows the action; Shorts max 3 min"},
}


def get(preset_id: str) -> dict:
    if preset_id not in PRESETS:
        raise KeyError(f"unknown preset '{preset_id}' (have: {', '.join(PRESETS)})")
    return {"id": preset_id, **PRESETS[preset_id]}


def all_presets() -> list[dict]:
    return [get(k) for k in PRESETS]
