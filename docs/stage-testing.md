# Trying the hidden stage on a real machine

The hidden stage (`dolly ... --stage`, `dolly stage ...`) has only ever been run in a
VirtualBox VM: software rendering, no GPU, no camera, one screen. This is a ten-minute
checklist for someone with a real GPU. Nothing here changes your settings, and a stage
cannot outlive `dolly stage stop`.

What a stage is: a second Hyprland started inside your session, with one screen that
exists on no monitor. It runs under a memory limit (1500 MB) and stops by itself after
30 minutes.

## Before you start

- Omarchy 4.x on Hyprland, Dolly installed, `wf-recorder` installed
  (`omarchy-pkg-add wf-recorder`: the stage is recorded with it, also where your desktop
  is recorded with gpu-screen-recorder).
- Save your work. In the VM a stage has never disturbed the session, but that is exactly
  the claim you are testing.
- If anything looks wrong at any point: `dolly stage stop`. It ends the stage and removes
  what it left. If your screen itself misbehaves, switch to a TTY (Ctrl+Alt+F3) and run
  `pkill -f "Hyprland --config.*dolly/stage"`.

## 1. The self test (one minute)

```bash
dolly doctor --stage
```

It starts a stage, draws a frame, records a second of it, looks for the sync mark in that
recording, plays two test tones on the stage and listens for them on the stage's private
output, and stops the stage. You should hear nothing. Each step says `ok`, `FAILED` or
`skipped`, with a reason. In the VM it takes five seconds.

**Send back:** the whole output, and how long it took.

## 2. A take on the stage while you work (three minutes)

```bash
dolly run ~/.config/omarchy/plugins/io.github.jagritvats.dolly/examples/terminal-demo.toml --stage --json
```

While it runs (about 25 seconds, then the export), keep using your desktop: type in a
terminal, move windows, switch workspaces.

Look at:

- **Your screen.** Did anything of the stage show up: a window, a flash, a second
  pointer, a notification card, a change in the bar? Did your typing go anywhere it
  should not? Did your pointer jump?
- **The clip** (its path is in the JSON, `outputs`). Is the picture clean (no tearing, no
  black frames, right colours)? Is it your desktop picture behind the terminal? Do the
  zooms land where the typing is? Does the "Enter" keycap appear when the command runs,
  not before or after?
- **The JSON.** `"sync": "markers2"` means both sync marks were found. `"markers1"` or
  `"clock"` means one or both were not: say so.

**Send back:** the JSON, and what you saw.

## 3. A vertical stage, and driving it by hand (three minutes)

```bash
dolly stage start --size 1080x1920
dolly stage exec -- xdg-terminal-exec
dolly stage shot ~/stage-1.png
dolly record --stage --json            # prints the take's folder and returns
dolly stage type 'echo hello from the stage' --enter
dolly stage key ctrl+l
dolly stage click 540 960
dolly stage shot ~/stage-2.png
dolly stop --stage --json
dolly stage status
dolly stage stop
```

Look at the two screenshots: is the terminal there, tall, with your desktop picture cut to
fit behind it? In the `dolly stop` JSON: `"sync"`, and how long `dolly stop` took (on a
machine that encodes video quickly it should answer in two or three seconds).

**Send back:** `stage-2.png`, the `dolly stop` JSON, and the time it took.

## 4. Sound and a browser (two minutes)

```bash
dolly stage start
dolly stage exec -- omarchy-launch-browser https://omarchy.org
dolly record --stage --audio system --for 12 --json &
sleep 4
dolly stage exec -- ffmpeg -f lavfi -i sine=frequency=440:duration=5 -f pulse tone
wait
dolly stage shot ~/stage-browser.png
dolly stage stop
```

- You should **hear nothing** on your speakers or headphones (a 440 Hz tone plays for
  five seconds, on the stage only).
- Your own browser, if it is open, should not get a new window or tab; the page should
  be in `stage-browser.png`.
- The take's folder (in the JSON) should have `tracks/system.mka` with the tone in it
  (`mpv <folder>/tracks/system.mka`).

**Send back:** whether you heard anything, where the browser window opened, and whether
the track has sound.

## 5. What it costs (one minute)

While step 2 runs, in another terminal:

```bash
top -b -n 1 | grep -E "Hyprland|wf-recorder|quickshell" 
systemctl --user status 'dolly-stage-*' | grep -E "Memory|CPU"
```

In the VM (4 virtual CPUs, no GPU): the stage's Hyprland 7 % of one core, wf-recorder
54 to 67 %, the desktop picture's program 0 %; the stage and its programs 280 MB in all.

**Send back:** those lines, and your CPU and GPU.

## 6. The report

```bash
dolly doctor --report
```

It writes one text file (no video, no screenshots, no typed keys) with the self test of
step 1, your versions, settings and logs. **Send that file**: it is what a fix will be
made from.

## Things nobody has tried yet

If you have the patience, any of these is new information:

- two monitors, a fractional scale, or a 4K screen (the stage itself is always 1920x1080
  or the size you give it, scale 1);
- locking the screen or suspending while a stage runs;
- `--camera live` on a stage with a real camera, `--audio mic` with a real microphone;
- an Electron app, a game, or anything that insists on X11 (the stage has no X server);
- a stage left running for its full 30 minutes.
