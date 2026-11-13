"""Names from a recording's files never become ffmpeg filter text.

At export the audio tracks listed in events.json are mixed with an ffmpeg filter graph. A
track's name was used as a label in that graph, and style.audio.rnnoise_model went into it
inside quotes. A recording folder can come from someone else, and ffmpeg has filters that
write files (ametadata=mode=print:file=...): a crafted name emptied any file the user can
write when the take was exported.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_pipeline import proj  # noqa: E402,F401
from dolly import audio  # noqa: E402
from dolly.events import Timeline  # noqa: E402
from dolly.project import Project  # noqa: E402
from dolly.render import audio_filter, export, ffmpeg_bin, probe  # noqa: E402

GOOD = {"track": "mic", "file": "tracks/mic.mka", "offset": 0.12, "scale": 1.0}
# closes the label with a quote that pairs up with the next use of the name: valid syntax,
# and ffmpeg opens (empties) the file before it finds the graph does not connect
HOSTILE_NAME = "P'x];[Q]ametadata=mode=print:file={victim};[R"


def test_only_tracks_of_the_shape_dolly_writes_are_kept(tmp_path):
    bad = [{**GOOD, "track": HOSTILE_NAME}, {**GOOD, "track": "mic]x"}, {**GOOD, "track": "Mic"}, {**GOOD, "track": ""},
           {**GOOD, "file": "../../.ssh/id_ed25519"}, {**GOOD, "file": "/etc/passwd"}, {**GOOD, "file": "tracks/../x.mka"},
           {**GOOD, "file": "tracks/.hidden"}, {**GOOD, "file": "tracks/a/b.mka"}, {**GOOD, "file": 3},
           {**GOOD, "offset": "0,volume=9"}, {"track": "mic"}, None, "mic", 7]
    assert audio.clean_tracks([GOOD] + bad + [{**GOOD, "track": "system", "file": "tracks/system.mka"}]) == [
        GOOD, {**GOOD, "track": "system", "file": "tracks/system.mka"}]
    assert audio.clean_tracks("x") == [] and audio.clean_tracks(None) == []
    assert audio.clean_tracks([{**GOOD, "offset": float("nan"), "scale": 1e9}])[0]["offset"] == 0.0
    assert audio.clean_spans([[1, 2.5], ["1", "2"], [3, 1], ["a", 2], [1], None, [float("nan"), 2]]) == [[1.0, 2.5], [1.0, 2.0]]


def test_a_timeline_hands_out_clean_tracks_only():
    tl = Timeline.from_dict({"video": {"w": 640, "h": 360, "fps": 30, "duration": 3.0}, "events": [],
                             "capture": {"audio_tracks": [GOOD, {**GOOD, "track": HOSTILE_NAME}],
                                         "mic_mutes": [[0.5, 1.0], ["0", "1':volume=9"]]}})
    assert tl.capture["audio_tracks"] == [GOOD] and tl.capture["mic_mutes"] == [[0.5, 1.0]]


def test_the_filter_builder_refuses_a_name_that_is_not_a_name():
    with pytest.raises(ValueError):
        audio.mix_filter([{**GOOD, "track": HOSTILE_NAME}], 1, [(0.0, 1.0, 1.0)], 0.0, 1.0, {})
    with pytest.raises(ValueError):
        audio_filter([(0.0, 1.0, 1.0)], 0.0, 1.0, False, True, inp="1:a];[0:a]anull[x")


def test_a_denoise_model_path_is_a_plain_path_to_a_file(tmp_path):
    model = tmp_path / "voice model-1.rnnn"
    model.write_text("x")
    assert audio.model_arg(str(model)) == str(model)
    assert audio.model_arg(str(tmp_path / "missing.rnnn")) is None
    for bad in (f"{model}'", f"{model}',ametadata=mode=print:file='/tmp/x", f"{model}:mix=0", f"{model}\\", f"{model};",
                f"{model}[a]", "", None, 3, ["x"]):
        assert audio.model_arg(bad) is None, bad
    graph = audio.mix_filter([GOOD], 1, [(0.0, 1.0, 1.0)], 0.0, 1.0, {"mic": {"denoise": True}},
                             rnn_model="x',ametadata=mode=print:file='/tmp/x")
    assert "ametadata" not in graph and "afftdn" in graph          # the built-in denoiser instead
    assert f"arnndn=m='{model}'" in audio.mix_filter([GOOD], 1, [(0.0, 1.0, 1.0)], 0.0, 1.0,
                                                     {"mic": {"denoise": True}}, rnn_model=str(model))


def test_exporting_a_crafted_folder_leaves_other_files_alone(proj, tmp_path, monkeypatch):
    victim = tmp_path / "notes.txt"
    victim.write_text("important\n")
    name = HOSTILE_NAME.format(victim=victim)
    # what the old code built from that name does empty the file:
    old = (f"[0:a]anull[{name}];[{name}]anull[{name}p0];[{name}p0]anull[{name}c];[{name}c]anull[aout]")
    subprocess.run([ffmpeg_bin(), "-v", "quiet", "-f", "lavfi", "-i", "sine=d=0.2", "-filter_complex", old,
                    "-map", "[aout]", "-f", "null", "-"])
    if victim.read_text() != "":
        pytest.skip("this ffmpeg does not open the file for such a graph")
    victim.write_text("important\n")

    d = tmp_path / "take"
    shutil.copytree(proj, d)
    (d / "tracks").mkdir()
    subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-c:a", "flac", "-f", "matroska", str(d / "tracks" / "mic.mka")], check=True)
    ev = json.loads((d / "events.json").read_text())
    ev["capture"]["audio_mode"] = "mic"
    ev["capture"]["audio_tracks"] = [GOOD, {**GOOD, "track": name}]
    (d / "events.json").write_text(json.dumps(ev))
    p = Project(d)
    data = p.load()
    data["style"]["audio"]["rnnoise_model"] = f"x',ametadata=mode=print:file='{victim}"
    p.save(data)
    monkeypatch.chdir(tmp_path)
    out = export(d, d / "exports" / "a.mp4", "mp4", "fast", settings=False)
    assert victim.read_text() == "important\n"
    assert any(s.get("codec_type") == "audio" for s in probe(out).get("streams", []))     # the real track is mixed in
