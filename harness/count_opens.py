#!/usr/bin/env python3
"""count_opens.py FILE SECONDS [LABEL]: how many times FILE was opened (inotify IN_OPEN).

Starting a script opens it (the kernel for the #! line, then the interpreter), so the
number of runs is opens // 2 for a bash script. Unlike sampling /proc this cannot miss a
short-lived process. Prints one JSON line."""
import ctypes
import json
import os
import select
import struct
import sys
import time

IN_OPEN = 0x00000020
path, seconds = sys.argv[1], float(sys.argv[2])
label = sys.argv[3] if len(sys.argv) > 3 else ""
libc = ctypes.CDLL("libc.so.6", use_errno=True)
fd = libc.inotify_init1(os.O_NONBLOCK)
if fd < 0 or libc.inotify_add_watch(fd, os.path.realpath(path).encode(), IN_OPEN) < 0:
    sys.exit(f"inotify failed: {os.strerror(ctypes.get_errno())}")
t0, opens, stamps = time.time(), 0, []
while True:
    left = t0 + seconds - time.time()
    if left <= 0:
        break
    r, _, _ = select.select([fd], [], [], min(left, 0.5))
    if not r:
        continue
    data = os.read(fd, 65536)
    i = 0
    while i + 16 <= len(data):
        _wd, mask, _cookie, n = struct.unpack_from("iIII", data, i)
        i += 16 + n
        if mask & IN_OPEN:
            opens += 1
            stamps.append(round(time.time() - t0, 2))
print(json.dumps({"label": label, "seconds": seconds, "opens": opens, "runs": opens // 2, "at": stamps[:60]}))
