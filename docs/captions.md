# Captions

Every recording has a **caption lane** on the editor's timeline, under Privacy. Its lines
are burned into the export (a pill at the bottom) and written as an `.srt` next to the
video. Turn them off for one export with **Burn in captions** in the Audio group.

## Typing them

Press **C** (or **+ Caption**) to add a line at the playhead, type it and press Enter.
Drag a line to move it, drag its edges to time it, **S** splits it at the playhead
(the words are shared out), Delete removes it, Ctrl+Z undoes. This needs nothing
installed.

## Transcribing them

**Transcribe** (Audio group, or `dolly captions [DIR]`) writes down what you said on this
computer, into the caption lane, replacing what is there. Check the lines afterwards: fix
a word by clicking its line, adjust the timing by dragging.

- What is transcribed: the microphone track, else the video's own sound, else the system
  audio. Spans you muted with the mute hotkey (Super+Alt+N) are silenced first, so what
  you muted is never written down.
- Nothing leaves the computer.

Dolly installs nothing, so Transcribe uses a speech-to-text engine you already have. The
first one found is used (`DOLLY_TRANSCRIBER` picks one: `whisper.cpp`, `faster-whisper`
or `voxtype`):

1. **whisper.cpp**: the `whisper-cli` program plus a ggml model file at
   `~/.local/share/dolly/whisper/ggml-base.en.bin` (or wherever `DOLLY_WHISPER_MODEL`
   points). It gives each line its own timing.
2. **faster-whisper**: the Python package `faster_whisper`, importable by the Python that
   runs Dolly (see below). The model size is `DOLLY_WHISPER_SIZE` (default `base`).
3. **Voxtype, Omarchy's Dictation.** Turn it on in **Omarchy menu > Setup > Defaults >
   Dictation > Voxtype**: Omarchy installs it from its own package repository and
   downloads its model. Dolly then uses `voxtype transcribe`. Voxtype gives text without
   times, so Dolly cuts the speech at its pauses, transcribes each stretch on its own
   (one Voxtype run each, a second or two per stretch on a small virtual machine) and
   shares a stretch's words out over its length. The timing is therefore an estimate:
   good at the pauses, approximate inside a long sentence. The lines are kept short.
   Voxtype's own settings (`~/.config/voxtype/config.toml`: model, language, word
   replacements) apply. Dolly runs it on half the CPUs (1 to 4; `DOLLY_VOXTYPE_THREADS`
   overrides): on every CPU of a small virtual machine it sometimes stalled at full load. A
   run that stalls is tried once more on one thread.

`dolly doctor` shows which one it found in the row "captions transcriber".

## Before 0.8.0

Recordings made earlier keep their `captions.srt`; the caption lane shows it, and the
first edit moves it into the project. A storyboard's `say` lines still arrive as
`captions.srt` and show up the same way.

## faster-whisper in a private Python environment

Arch does not let `pip` write into the system Python, and Dolly should not need it to.
A private environment keeps faster-whisper apart from everything else. `bin/dolly` uses
the Python of such an environment when one exists at
`~/.local/share/dolly/venv` (or at the path in `DOLLY_VENV`):

```bash
python3 -m venv --system-site-packages ~/.local/share/dolly/venv
~/.local/share/dolly/venv/bin/pip install faster-whisper
dolly doctor | grep captions
```

`--system-site-packages` matters: the environment must still see the system's
`python-cairo` and `python-gobject`, which Dolly draws with. `bin/dolly` therefore passes
over an environment at that place that cannot see them and runs the system Python;
`dolly doctor` then shows the row "private Python environment". That is the case for the
environment Set up offered up to 0.7.3 (for NumPy and Pillow, which Dolly no longer
uses). Delete it, or, if you put faster-whisper in it, open it to the system's packages:
set `include-system-site-packages = true` in `~/.local/share/dolly/venv/pyvenv.cfg`.
A path given in `DOLLY_VENV` is used as it is.

To undo it, delete the folder: `rm -rf ~/.local/share/dolly/venv`.
