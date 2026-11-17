"""0.8.0 captions: the caption lane (project.json), and transcription through Voxtype,
the engine of Omarchy's Dictation, which gives text without times."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dolly import captions  # noqa: E402
from dolly.ff import ffmpeg_bin  # noqa: E402


# ------------------------------------------------------------------ the lane
def test_clean_cues_keeps_only_what_dolly_writes():
    got = captions.clean_cues([
        {"start": 2.0, "end": 4.0, "text": "  second\nline  ", "id": "c7", "source": "manual"},
        {"start": 0.5, "end": 2.5, "text": "first \x1b[31mred\u202e"},          # overlaps the next one
        {"start": "x", "end": 3, "text": "bad number"},
        {"start": float("nan"), "end": 3, "text": "nan"},
        {"start": 5, "end": 5.01, "text": "too short"},
        {"start": 6, "end": 7, "text": "   "},
        {"start": 8, "end": 9, "text": "y" * 1000, "id": "../evil", "source": "<b>"},
        "not a dict",
    ])
    assert [c["text"] for c in got] == ["first [31mred", "second line", "y" * captions.MAX_TEXT]
    assert got[0]["end"] == 2.0                       # trimmed so one line is on screen at a time
    assert got[1]["id"] == "c7" and got[1]["source"] == "manual"
    assert got[2]["source"] == "auto" and got[2]["id"] not in ("../evil", "c7") and got[0]["id"] != "c7"
    assert len({c["id"] for c in got}) == 3
    assert captions.clean_cues({"start": 1}) == [] and captions.clean_cues(None) == []


def test_the_lane_wins_over_the_old_srt(tmp_path):
    captions.write_srt([{"start": 1, "end": 2, "text": "from the srt"}], tmp_path / "captions.srt")
    assert [c["text"] for c in captions.load(tmp_path)] == ["from the srt"]       # before 0.8.0
    (tmp_path / "project.json").write_text(json.dumps({"captions": [{"start": 3, "end": 4, "text": "typed"}]}))
    assert [c["text"] for c in captions.load(tmp_path)] == ["typed"]
    (tmp_path / "project.json").write_text(json.dumps({"captions": []}))        # all deleted in the editor
    assert captions.load(tmp_path) == [] and not captions.has(tmp_path)
    (tmp_path / "project.json").write_text("{damaged")
    assert [c["text"] for c in captions.load(tmp_path)] == ["from the srt"]


def test_the_editor_saves_the_lane_cleaned(tmp_path):
    from dolly.editor.server import Editor
    ed = Editor(tmp_path)
    ed.save({"captions": [{"start": 1, "end": 3, "text": "Hello\tthere", "source": "manual"},
                          {"start": 2, "end": 1, "text": "backwards"}]})
    data = json.loads((tmp_path / "project.json").read_text())
    assert [(c["start"], c["end"], c["text"]) for c in data["captions"]] == [(1.0, 3.0, "Hello there")]
    assert captions.load(tmp_path) == data["captions"]


def test_the_library_sees_typed_captions(tmp_path):
    from dolly import library
    assert library._has_captions(tmp_path, {"captions": [{"start": 0, "end": 1, "text": "x"}]})
    assert not library._has_captions(tmp_path, {"captions": []})
    assert not library._has_captions(tmp_path, {})


# ------------------------------------------------------------------ Voxtype: times for text
SILENCE_LOG = """
[silencedetect @ 0x1] silence_start: 0
[silencedetect @ 0x1] silence_end: 0.8 | silence_duration: 0.8
[silencedetect @ 0x1] silence_start: 2.5
[silencedetect @ 0x1] silence_end: 3.1 | silence_duration: 0.6
[silencedetect @ 0x1] silence_start: 5.0
[silencedetect @ 0x1] silence_end: 9.0 | silence_duration: 4.0
"""


def test_speech_spans_are_the_gaps_between_pauses():
    spans = captions.speech_spans(SILENCE_LOG, 31.0)
    assert spans[0] == pytest.approx((0.65, 2.65)) and spans[1] == pytest.approx((2.95, 5.15))
    rest = spans[2:]                                   # 9 s to the end, 22 s of talk: cut in pieces
    assert rest[0][0] == pytest.approx(8.85) and rest[-1][1] == pytest.approx(31.0)
    assert len(rest) == 2 and all(b - a <= captions.CHUNK_S for a, b in rest)
    assert captions.speech_spans("", 4.0) == [(0.0, 4.0)]                  # no pause at all
    assert captions.speech_spans("silence_start: 0\n", 4.0) == []          # nothing but silence


def test_chunks_group_neighbours_up_to_a_length():
    spans = [(0.0, 2.0), (2.5, 5.0), (5.5, 11.0), (11.2, 13.0), (20.0, 21.0)]
    assert captions.chunks(spans) == [[(0.0, 2.0), (2.5, 5.0), (5.5, 11.0)], [(11.2, 13.0)], [(20.0, 21.0)]]


def test_spread_puts_every_word_in_order_inside_the_spans():
    text = ("Here is the new caption lane. You can type a line, or transcribe what you said "
            "with the dictation engine Omarchy already has, and then fix the words by hand.")
    spans = [(1.0, 4.0), (5.0, 12.0)]
    cues = captions.spread(text, spans)
    assert " ".join(c["text"] for c in cues).split() == text.split()
    assert all(any(a - 1e-6 <= c["start"] < c["end"] <= b + 1e-6 for a, b in spans) for c in cues)
    assert all(len(c["text"]) <= captions.CUE_CHARS for c in cues)
    assert [c["start"] for c in cues] == sorted(c["start"] for c in cues)
    assert cues[0]["text"].endswith("lane.")                   # a sentence ends a line
    assert captions.spread("", spans) == [] and captions.spread("words", []) == []


def test_voxtype_output_is_read_after_its_progress_lines():
    out = ("Loading audio file: \"/tmp/a.wav\"\nAudio format: 16000 Hz, 1 channel(s), Int\n"
           "Processing 48000 samples (3.00s)...\n\nHello from Dolly.\n")
    assert captions.voxtype_text(out) == "Hello from Dolly."
    assert captions.voxtype_text(out.replace("Hello from Dolly.", "No speech detected, skipping transcription.")) == ""
    assert captions.voxtype_text("") == ""


def test_mute_filter_is_numbers_in_file_time():
    f = captions._mute_filter([[2.0, 4.0], [0.0, 0.5]], offset=1.0, scale=1.0)
    assert f == "volume=enable='between(t,1.000,3.000)':volume=0"     # the second span is before the file
    assert captions._mute_filter([], 0, 1) is None


# ------------------------------------------------------------------ end to end with a stand-in Voxtype
FAKE_VOXTYPE = """#!/bin/sh
# stands in for `voxtype transcribe FILE`: progress lines, an empty line, then the text
[ "$1" = --threads ] && { THREADS=$2; shift 2; }
[ "$1" = transcribe ] || exit 2
echo "Loading audio file: $2 threads=$THREADS" >> "$FAKE_LOG"
[ -n "$FAKE_STALL" ] && [ "$THREADS" != 1 ] && sleep 30
echo "Loading audio file: \\"$2\\""
echo "Processing samples..."
echo
echo "Say this part"
"""


@pytest.fixture
def take(tmp_path, monkeypatch):
    """A recording whose microphone track says something in three bursts (0.5-2, 4-5.5,
    8-9.5 s of the track) and starts 0.7 s into the video; the middle one was muted."""
    d = tmp_path / "take"
    (d / "tracks").mkdir(parents=True)
    subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x200:rate=10:duration=11",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "raw.mkv")], check=True)
    expr = "0.5*sin(2*PI*330*t)*(between(t,0.5,2)+between(t,4,5.5)+between(t,8,9.5))"
    subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-f", "lavfi", "-i", f"aevalsrc=exprs='{expr}':s=48000:d=10.5",
                    "-c:a", "pcm_s16le", str(d / "tracks" / "mic.mka")], check=True)
    doc = {"video": {"w": 320, "h": 200, "fps": 10, "duration": 11.0},
           "capture": {"audio_tracks": [{"track": "mic", "file": "tracks/mic.mka", "offset": 0.7, "scale": 1.0}],
                       "mic_mutes": [[4.5, 6.4]]},
           "events": []}
    (d / "events.json").write_text(json.dumps(doc))
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    vx = bin_ / "voxtype"
    vx.write_text(FAKE_VOXTYPE)
    vx.chmod(vx.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "voxtype.log"))
    monkeypatch.setenv("DOLLY_TRANSCRIBER", "voxtype")
    return d


def test_transcribe_with_voxtype_fills_the_lane_in_video_time(take, tmp_path):
    assert captions.backend() == "voxtype"
    seen = []
    cues = captions.transcribe(take, progress=lambda i, n: seen.append((i, n)))
    assert [c["text"] for c in cues] == ["Say this part", "Say this part"]       # the muted burst is gone
    assert cues[0]["start"] == pytest.approx(0.5 + 0.7, abs=0.25) and cues[0]["end"] == pytest.approx(2.0 + 0.7, abs=0.25)
    assert cues[1]["start"] == pytest.approx(8.0 + 0.7, abs=0.25)
    assert all(c["source"] == "auto" for c in cues)
    assert (tmp_path / "voxtype.log").read_text().count("Loading") == 2          # one run per stretch
    assert seen[-1][0] == seen[-1][1] == 2
    assert captions.load(take) == cues                                            # in the lane
    assert "Say this part" in (take / "captions.srt").read_text()


def test_a_failing_voxtype_is_explained(take, tmp_path):
    vx = tmp_path / "bin" / "voxtype"
    vx.write_text("#!/bin/sh\necho 'Error: model file not found' >&2\nexit 1\n")
    with pytest.raises(captions.CaptionsUnavailable, match="model file not found"):
        captions.transcribe(take)
    assert not (take / "project.json").exists()                                   # the lane is untouched


def test_without_a_transcriber_it_says_how_to_get_one(take, monkeypatch):
    monkeypatch.setattr(captions, "backend", lambda: None)
    with pytest.raises(captions.CaptionsUnavailable, match="Dictation"):
        captions.transcribe(take)


def test_a_process_voxtype_leaves_behind_is_not_waited_for_and_does_not_survive(take, tmp_path):
    """Dolly waits for Voxtype to exit, not for everything it started to close the output,
    and what it started is stopped with it (seen in the VM: runs that hung to the time limit)."""
    import time
    vx = tmp_path / "bin" / "voxtype"
    pidfile = tmp_path / "left.pid"
    vx.write_text(FAKE_VOXTYPE + f"sleep 60 &\necho $! > {pidfile}\n")
    t0 = time.monotonic()
    cues = captions.transcribe(take)
    assert time.monotonic() - t0 < 20 and len(cues) == 2
    pid = int(pidfile.read_text())
    time.sleep(0.2)
    assert not _running(pid)


def _running(pid: int) -> bool:
    """Alive and not a zombie (in a container nothing may reap a killed orphan)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def test_a_stalled_voxtype_run_is_retried_on_one_thread(take, tmp_path, monkeypatch):
    monkeypatch.setattr(captions, "VOXTYPE_LIMIT", 1.5)
    monkeypatch.setattr(captions, "VOXTYPE_PER_S", 0.0)
    monkeypatch.setenv("FAKE_STALL", "1")
    monkeypatch.setenv("DOLLY_VOXTYPE_THREADS", "3")
    cues = captions.transcribe(take)
    assert len(cues) == 2
    log = (tmp_path / "voxtype.log").read_text().splitlines()
    assert [l.rsplit("threads=", 1)[1] for l in log] == ["3", "1", "3", "1"]


def test_voxtype_threads():
    import os as _os
    n = captions.voxtype_threads()
    assert 1 <= n <= 4
    _os.environ["DOLLY_VOXTYPE_THREADS"] = "9"
    try:
        assert captions.voxtype_threads() == 4
    finally:
        del _os.environ["DOLLY_VOXTYPE_THREADS"]
