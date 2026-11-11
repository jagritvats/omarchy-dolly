"""Export worker count. No third-party imports: `dolly doctor` uses it when
NumPy and Pillow are missing."""
import os


def default_workers(n_frames: int, fps: float) -> int:
    env = os.environ.get("DOLLY_WORKERS")
    if env:
        return max(1, int(env))
    cpus = os.cpu_count() or 1
    # each worker also drives an ffmpeg decoder + encoder; a chunk must be worth its warm-up
    return max(1, min(cpus, 6, n_frames // int(fps * 4) or 1))
