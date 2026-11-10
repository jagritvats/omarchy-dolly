"""Linux evdev keycode -> label (US layout). XKB keycodes are evdev + 8."""

MODS = {29: "ctrl", 97: "ctrl", 56: "alt", 100: "alt", 42: "shift", 54: "shift", 125: "super", 126: "super"}

NAMES = {
    1: "Escape", 14: "BackSpace", 15: "Tab", 28: "Return", 57: "space", 111: "Delete", 110: "Insert",
    102: "Home", 107: "End", 104: "Prior", 109: "Next", 103: "Up", 108: "Down", 105: "Left", 106: "Right",
    12: "-", 13: "=", 26: "[", 27: "]", 39: ";", 40: "'", 41: "`", 43: "\\", 51: ",", 52: ".", 53: "/",
    96: "Return", 99: "Print",
}
_rows = {2: "1234567890", 16: "qwertyuiop", 30: "asdfghjkl", 44: "zxcvbnm"}
for start, chars in _rows.items():
    for i, ch in enumerate(chars):
        NAMES[start + i] = ch
for i in range(10):
    NAMES[59 + i] = f"F{i + 1}"
NAMES[87], NAMES[88] = "F11", "F12"


def label(code: int) -> str | None:
    return NAMES.get(code)
