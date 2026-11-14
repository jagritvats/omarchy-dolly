"""Stand-ins for the desktop tools a recording needs: a hyprctl with one monitor and one
window, and a gpu-screen-recorder that writes a test picture (ffmpeg testsrc2 -> raw.mkv).
No third-party imports: tests/test_standard_install.py runs where NumPy is not installed."""

HYPRCTL = r'''#!/usr/bin/env bash
case "$*" in
  *monitors*) echo '[{"name":"DP-1","focused":true,"x":0,"y":0,"width":1280,"height":800,"scale":1.0,"transform":0,"activeWorkspace":{"id":1}}]';;
  *cursorpos*) python3 -c "import time,math;t=time.time()%6;print('{\"x\":%d,\"y\":%d}'%(300+150*math.floor(t/1.5),300+60*math.floor(t/1.5)))";;
  *activewindow*) echo '{"address":"0xa1","at":[10,36],"size":[620,754],"title":"~/app","class":"Alacritty"}';;
  *clients*) echo '[{"address":"0xa1","at":[10,36],"size":[620,754],"title":"~/app","class":"Alacritty","workspace":{"id":1},"floating":false,"fullscreen":0,"mapped":true}]';;
  eval*) echo "error: lua disabled in stub"; exit 1;;
  *) echo "{}";;
esac
'''
GSR = r'''#!/usr/bin/env bash
out=""; fps=60; size=1280x800
while [[ $# -gt 0 ]]; do case "$1" in -o) out="$2"; shift;; -f) fps="$2"; shift;; -region) size="${2%%+*}"; shift;; -w) case "$2" in *x*+*) size="${2%%+*}";; esac; shift;; esac; shift; done
exec ffmpeg -v error -y -re -f lavfi -i "testsrc2=size=${size}:rate=${fps}" -c:v libx264 -preset ultrafast -g 15 -pix_fmt yuv420p -f matroska "$out"
'''
