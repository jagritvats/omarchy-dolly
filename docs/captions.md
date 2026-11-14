# Captions: a transcriber of your choice

Burned-in captions (`dolly captions`, the editor's Transcribe) need a speech-to-text
engine. Omarchy does not ship one, and Dolly installs nothing, so captions stay off until
you add one yourself. Everything else in Dolly works without it.

Dolly looks for, in this order:

1. **whisper.cpp**: the `whisper-cli` program plus a ggml model file at
   `~/.local/share/dolly/whisper/ggml-base.en.bin` (or wherever `DOLLY_WHISPER_MODEL`
   points). whisper.cpp is in the AUR (`whisper.cpp`); the models are listed in its
   README.
2. **faster-whisper**: the Python package `faster_whisper`, importable by the Python that
   runs Dolly. The model size is `DOLLY_WHISPER_SIZE` (default `base`); the model is
   downloaded by faster-whisper itself the first time it runs.

`dolly doctor` shows which one it found in the row "captions transcriber".

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
