# Storyboards (`dolly run`)

A storyboard is a demo written as steps. Dolly performs the steps on your desktop,
records them, and exports the result. The same file gives the same take every time,
so you can re-record after every change instead of doing retakes by hand.

```sh
dolly run demo.toml --dry-run    # the timed plan and every command it would run; touches nothing
dolly run demo.toml              # perform, record, export; prints the file(s)
dolly run demo.toml --json       # the same, with a JSON result on stdout
dolly run - < demo.json          # the same structure as JSON on stdin
dolly run --check                # what this machine can perform
```

Other flags: `--no-export` (record and analyse only), `--keep-open` (skip `[[cleanup]]`
and stay where the take was made), `--check demo.toml` (validate, and say whether this
machine can run it).

## The file

```toml
title  = "Listing files"          # the recording's name, and the export's file name
record = "screen"                 # "screen" | "window:<name or selector>" | "region:X,Y,WxH"
aspect = ["16:9", "9:16"]         # one export per shape: 16:9, 9:16, 1:1, 4:5

[[setup]]                         # before recording, off camera
open = "terminal"
as   = "term"

[[step]]                          # recorded
type  = "ls -la"
enter = true

[[step]]
wait = "quiet"

[[cleanup]]                       # after recording
close = "term"
```

### Settings

| Setting | Default | |
|---|---|---|
| `title` | none | name in the Library; export file name |
| `record` | `"screen"` | the focused monitor, one window (`"window:term"`: a name given with `as` in `[[setup]]`, or a selector), or a fixed region |
| `aspect` | `["16:9"]` | one export per entry |
| `preset` | none | a share preset instead of `aspect` (`github`, `github-gif`, `discord`, `slack`, `x`, `linkedin`, `youtube`, `shorts`) |
| `quality` | `"high"` | `draft`, `fast`, `high`, `best` |
| `max_seconds` | `120` | hard limit for the recorded part (2 to 900). Past it the take is stopped and kept |
| `speed` | `11` | typing speed in characters a second (2 to 40) |
| `seed` | from the title | the typing rhythm; change it for a different, equally repeatable take |
| `stage` | `"clean"` | `"clean"`: the take happens on an empty workspace and you are brought back afterwards. `"here"`: on the current workspace |

### Steps

Each `[[setup]]`, `[[step]]` and `[[cleanup]]` table has exactly one action.

| Action | Value | Options | What happens |
|---|---|---|---|
| `open` | a command, or `"terminal"` (your terminal) | `as` (a name for later steps), `cwd`, `timeout` (10) | starts it and waits for its window |
| `focus` | a name or selector | | focuses that window (switching workspace if needed) |
| `type` | one line of text | `enter` (false), `speed` | types it with a human rhythm: quicker inside words, a beat before each word, a look before Enter |
| `key` | a chord: `"ctrl+l"`, `"enter"`, `"super+3"` | `times` (1) | presses it |
| `wait` | seconds, `"quiet"`, or `"window:<name or selector>"` | `timeout` (15) | a pause; until the screen stops changing; until that window exists |
| `move` | a point: `"50%,40%"` or `"640,360"` | `in` (a window) | moves the pointer there on an eased, slightly curved path |
| `click` | a point | `in`, `button` (left) | moves there, then clicks the window under the pointer |
| `zoom` | `"here"` | | a "zoom here" mark at the pointer, as Super+Alt+M does |
| `say` | caption text | `for` (seconds; default from its length) | a caption over the following steps (written to `captions.srt`). `[[step]]` only |
| `close` | a name or selector | | closes that window |

Names and selectors: `as = "term"` names a window; later steps say `"term"`. A selector
finds a window that already exists: `"class:foot"`, `"title:README"`, `"address:0x…"`.
Points are measured in the window given with `in`, or else in the recorded area;
percentages are of its size, plain numbers are pixels from its top-left corner.
Named keys: `enter tab escape space backspace delete up down left right home end pageup
pagedown f1`…`f12`. Modifiers: `ctrl alt shift super`.

`wait = "quiet"` that reaches its `timeout` carries on with a note (something on screen
keeps moving). Every other wait or step that fails stops the take.

## What a run does, and how it ends

1. Validates the file. Errors name the line and the step:
   `demo.toml: line 12: step 3 (key): "hyper" is not a modifier (ctrl, alt, shift, super)`.
2. Refuses to start while another recording is running.
3. Goes to an empty workspace (`stage = "clean"`), runs `[[setup]]`.
4. Starts recording with no picker and no countdown. Notifications are silenced and
   privacy settings apply as for any recording. No microphone and no camera.
5. Performs `[[step]]`s, stops, analyses the take, writes captions, exports.
6. Runs `[[cleanup]]`, then returns to the workspace, window and pointer position
   you started from.

If a step fails, the recording is stopped, **the take up to there is kept**, and the
message says which step and why (exit code 1; with `--json`, `error.where`, `error.line`,
`error.why`). Ctrl+C does the same (exit code 130). `dolly stop`, the bar button or the
record hotkey end the take early and it is exported as usual. Keys are pressed and
released one at a time, so nothing stays held when a run is cut short.

`--json` result: `{"ok", "title", "project", "outputs": [{"aspect" | "preset", "file"}],
"seconds", "steps_done", "steps", "error", "warnings"}`.

## On the hidden stage (beta)

`dolly run demo.toml --stage` performs and records the storyboard on a second, invisible
desktop, so your own pointer, keyboard and windows stay yours while it runs. Add
`--stage-size 1080x1920` for a vertical stage. Nothing in the file changes: `open`
starts programs there, `type` and `click` go there. A browser opened on the stage gets a
profile folder of its own. See "Hidden stage (beta)" in the README for what it is and
what is known not to work yet. (The `stage` setting in the file's header is something
else: it only chooses between an empty workspace and the current one.)

A storyboard is one way to drive a stage. The other is by hand, step by step, while
`dolly record --stage` records: `dolly stage type 'ls' --enter`, `dolly stage key ctrl+l`,
`dolly stage click 640 360`, `dolly stage move 200 120`, with `dolly stage shot` to look.
They perform the same steps a storyboard's `type`, `key`, `click` and `move` do, and tell
the take what they did, so it shows the right keycaps and follows the typing.

## What it needs

| | Uses | On stock Omarchy 4.5 |
|---|---|---|
| typing, key chords | `wtype` | installed |
| pointer travel, clicks, windows, workspaces | Hyprland 0.55+ (Lua dispatchers) | installed |
| `wait = "quiet"` | `grim` | installed (without it: a fixed 1.5 s) |

Nothing else has to be installed, and no background service or extra permission is
used. `dolly doctor` and `dolly run --check` show what is available. Not in this
version: dragging and scrolling. Characters outside the US keyboard layout are typed
correctly but logged as a plain key press.

The typed keys and the clicks are reported to Dolly's event log by the runner itself
(the log's `click_source` is `script`), so auto-zoom follows the typing, and the
shortcut overlay shows the chords, as for a recording you perform by hand.

## Safety

A storyboard is a script: `open` runs a command, and `type` types into whatever has
focus, with your permissions. Read one before running it, as you would a shell script.
`dolly run --dry-run` shows every command an `open` would run and every line that
would be typed, and runs nothing; validation never executes anything either. Dolly
itself makes no network requests during a run, and nothing is uploaded.
