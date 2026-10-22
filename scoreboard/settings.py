"""User settings (saved as JSON) and the options menu."""

import json
import os
import textwrap

from .logos import write_atomic
from .term import BOLD, DIM, RESET, REVERSE, center, detect_color_mode, set_symbols

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".config", "scoreboard", "settings.json")
OLD_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".config", "nhl-live", "settings.json")
MAX_DELAY = 30 * 60

DEFAULTS = {
    "league": "nhl",
    "delay": 0,              # seconds the game view lags behind real time
    "hide_list_scores": False,
    "score_alert": True,
    "bell": False,
    "show_last_play": True,
    "show_stats": True,
    "show_situation": True,
    "show_record": True,
    "play_by_play": False,
    "live_styles": {},       # league -> the play-by-play view's style (v switches it)
    "logo_size": "large",
    "layout": "vertical",
    "color": "auto",
    "symbols": "auto",
    "clock_24h": False,
}

# Keys renamed when the app went multi-league (nhl-live -> scoreboard).
RENAMED = {
    "goal_alert": "score_alert",
    "show_last_goal": "show_last_play",
    "show_sog": "show_stats",
    "show_strength": "show_situation",
}

# (key, label, choices or None for the delay control)
OPTIONS = [
    ("delay", "Stream delay", None),
    ("hide_list_scores", "Hide scores in game list", [False, True]),
    ("score_alert", "Flash on scores", [True, False]),
    ("bell", "Beep on scores", [False, True]),
    ("show_last_play", "Show last play", [True, False]),
    ("show_stats", "Show team stats", [True, False]),
    ("show_situation", "Show game situation", [True, False]),
    ("show_record", "Show season records", [True, False]),
    ("play_by_play", "Play-by-play view", [False, True]),
    ("logo_size", "Logo size", ["small", "medium", "large"]),
    ("layout", "Logo layout", ["vertical", "horizontal"]),
    ("color", "Color mode", ["auto", "truecolor", "256", "16"]),
    ("symbols", "Symbols", ["auto", "full", "console", "ascii"]),
    ("clock_24h", "24-hour start times", [False, True]),
]

HINTS = {
    "delay": "Holds the game view back so it matches a delayed TV or stream.",
    "hide_list_scores": "Spoiler-free game list (the list is always real-time).",
    "score_alert": "NHL goals and NFL scores. NBA baskets are too frequent to flash.",
    "show_last_play": "NHL: last goal scorer. NBA/NFL: the latest play.",
    "show_stats": "NHL: shots on goal. NBA: timeouts and fouls. NFL: timeouts.",
    "show_situation": "NHL: power play. NBA: bonus. NFL: possession, down & distance.",
    "logo_size": "Logos always shrink to fit the window.",
    "symbols": "auto: console-safe on the Linux text console, ASCII without Unicode, full Unicode elsewhere.",
    "play_by_play": "The latest event on a field, court or rink under the clock. In a game, v switches the"
                    " style and ←/→ go back through the events.",
    "layout": "Vertical: away above home. Horizontal: side by side, clock between.",
}

LOGO_SCALE = {"small": 0.6, "medium": 0.8, "large": 1.0}
CHOICES = {k: choices for k, _, choices in OPTIONS if choices}

settings = dict(DEFAULTS)


def _valid(key, value):
    """value if the app can use it for key, else None. A delay out of range is
    brought into it rather than dropped: no delay would show games early."""
    if key == "delay":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:   # NaN
            return None
        return int(max(0, min(MAX_DELAY, value)))
    if type(value) is not type(DEFAULTS[key]) or (key in CHOICES and value not in CHOICES[key]):
        return None
    return value


def load_settings():
    """Load the saved settings over the defaults, and return what was wrong
    with them (for a warning). A file that can't be used whole is moved aside
    to settings.json.bad before anything is saved over it, so a typo in a
    hand edit doesn't quietly lose the rest (turning off the delay and the
    hidden scores, of all things)."""
    try:
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:   # -sig: a BOM from an editor is fine
            text, path = f.read(), CONFIG_PATH
    except FileNotFoundError:
        try:   # settings from before the app went multi-league (nhl-live)
            with open(OLD_CONFIG_PATH, encoding="utf-8-sig") as f:
                text, path = f.read(), None
        except (OSError, ValueError):
            return []
    except (OSError, ValueError) as e:   # unreadable, or not UTF-8
        return _set_aside([str(e)])
    try:
        saved = json.loads(text)
    except (ValueError, RecursionError) as e:
        saved, problems = {}, [str(e)]
    else:
        problems = [] if isinstance(saved, dict) else ["not a JSON object"]
    for k, v in (saved.items() if isinstance(saved, dict) else ()):
        k = RENAMED.get(k, k)
        if k in DEFAULTS:
            ok = _valid(k, v)
            if ok is None:
                problems.append("%s: %s" % (k, json.dumps(v)[:40]))
            else:
                settings[k] = ok
    return _set_aside(problems) if path and problems else []


def _set_aside(problems):
    try:
        os.replace(CONFIG_PATH, CONFIG_PATH + ".bad")
    except OSError:
        pass
    return problems


def save_settings():
    """Save the settings; False if that failed. Atomic, so a full disk, a
    Ctrl-C or another instance saving at once can't leave half a file."""
    try:
        # realpath: a settings.json linked from a dotfiles folder stays a link
        write_atomic(os.path.realpath(CONFIG_PATH), json.dumps(settings, indent=2).encode())
        return True
    except Exception:
        return False


def fmt_delay(secs):
    if secs == 0:
        return "off"
    m, s = divmod(secs, 60)
    return "%d:%02d" % (m, s) if m else "%ds" % s


def step_delay(secs, direction):
    """Bigger steps the longer the delay gets."""
    edge = secs if direction > 0 else secs - 1
    step = 5 if edge < 60 else (15 if edge < 300 else 60)
    return max(0, min(MAX_DELAY, secs + direction * step))


def fmt_value(key, value):
    if key == "delay":
        return fmt_delay(value)
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


class OptionsMenu:
    def __init__(self, colors):
        self.colors = colors
        self.sel = 0
        self.open = False

    def toggle(self):
        self.open = not self.open
        if not self.open:
            save_settings()

    def handle_key(self, key):
        if key in ("o", "esc", "enter", "q"):
            self.toggle()
            return
        if key in ("up", "k"):
            self.sel = (self.sel - 1) % len(OPTIONS)
        elif key in ("down", "j"):
            self.sel = (self.sel + 1) % len(OPTIONS)
        elif key in ("left", "right", "h", "l", " ", "-", "+", "="):
            direction = -1 if key in ("left", "h", "-") else 1
            k, _, choices = OPTIONS[self.sel]
            if choices is None:
                settings[k] = step_delay(settings[k], direction)
            else:
                i = choices.index(settings[k]) if settings[k] in choices else 0
                settings[k] = choices[(i + direction) % len(choices)]
            if k == "color":
                self.colors.mode = detect_color_mode() if settings[k] == "auto" else settings[k]
            elif k == "symbols":
                set_symbols(settings[k])

    def render(self, cols, rows):
        label_w = max(len(o[1]) for o in OPTIONS)
        head = ["", center(BOLD + "OPTIONS" + RESET, cols), ""]
        options = []
        for i, (k, label, _) in enumerate(OPTIONS):
            val = "‹ %s ›" % fmt_value(k, settings[k])
            row = "  %-*s   %-14s" % (label_w, label, val)
            if i == self.sel:
                row = REVERSE + BOLD + row + RESET
            options.append(center(row, cols))
        hint = textwrap.wrap(HINTS.get(OPTIONS[self.sel][0], ""), max(20, cols - 4))
        tail = [""] + [center(DIM + h + RESET, cols) for h in hint]
        # A short window scrolls the options, keeping the one being changed in view.
        room = max(1, rows - 1 - len(head) - len(tail))
        if len(options) > room:
            top = max(0, min(self.sel - room // 2, len(options) - room))
            options = options[top:top + room]
        lines = head + options + tail
        if len(lines) > rows - 1:
            lines = options + tail   # shorter still: the heading goes first
        while len(lines) < rows - 1:
            lines.append("")
        return lines[:rows - 1] + [center(DIM + "↑/↓ choose   ←/→ change   o/Esc close" + RESET, cols)]
