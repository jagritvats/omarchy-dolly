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


# A key that was pressed but cannot be named: on the hidden stage every key comes from a
# typing tool with a keymap of its own, and Hyprland reports only the code (dolly/logger.py,
# on_unnamed_key). It counts as typing; it is never shown as a keycap.
UNNAMED = "?"


# What a recording keeps of a key press. A shortcut (Ctrl, Alt or Super held) and a key
# that types nothing (Enter, Esc, Tab, arrows, F-keys ...) keep their names: the key
# overlay shows them and the zoom planner reads them. A key that types a character is
# kept as "a character was typed at this time" and nothing else: typing zooms and idle
# speed-ups need the time, and what was typed (a password in a terminal or a web page)
# must never be in a recording's files.
TYPED = "char"
SHORTCUT_MODS = frozenset({"ctrl", "alt", "super"})
LEFT_ALT = 56       # evdev code. Right Alt (100) is AltGr on most layouts that are not US
                    # English: AltGr+Q is how a German keyboard types @. It makes no shortcut.


def types_text(name: str) -> bool:
    """A key name (see NAMES) that puts a character on screen."""
    return len(name) == 1 or name == "space"


def alt_only(name: str, mods) -> bool:
    """A character key with Alt and no other shortcut modifier: a shortcut with the left
    Alt key, a typed character with AltGr."""
    return types_text(name) and SHORTCUT_MODS & set(mods) == {"alt"}


def recorded(name: str, mods, left_alt: bool = False) -> tuple[str, list[str]]:
    """(key, mods) as a recording stores them. Typed characters lose their name and Shift
    (which would tell capitals from small letters); everything else is unchanged.

    A character key with Alt alone is named only when the caller knows the left Alt key was
    held (`left_alt`; the logger marks such events "lalt"). Otherwise it may have been typed
    with AltGr, and recordings made before 0.7.2 do not say which Alt it was."""
    mods = sorted(set(mods))
    if not types_text(name) or name == UNNAMED:
        return name, mods               # (UNNAMED already says nothing about the key)
    held = SHORTCUT_MODS & set(mods)
    if not held or (held == {"alt"} and not left_alt):
        return TYPED, []
    return name, mods


def stored(name: str, mods, left_alt: bool = False) -> dict:
    """The key event fields a log or events.json holds for this press."""
    key, kept = recorded(name, mods, left_alt)
    ev = {"key": key, "mods": kept}
    if key != TYPED and key != UNNAMED and alt_only(name, kept):
        ev["lalt"] = True
    return ev
