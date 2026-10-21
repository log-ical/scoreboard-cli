"""User settings (saved as JSON) and the options menu."""

import json
import os

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
    "play_by_play": "A live field, court or rink under the clock. In a game, v switches its style.",
    "layout": "Vertical: away above home. Horizontal: side by side, clock between.",
}

LOGO_SCALE = {"small": 0.6, "medium": 0.8, "large": 1.0}

settings = dict(DEFAULTS)


def load_settings():
    for path in (CONFIG_PATH, OLD_CONFIG_PATH):
        try:
            with open(path) as f:
                saved = json.load(f)
        except Exception:
            continue
        for k, v in saved.items():
            k = RENAMED.get(k, k)
            if k in DEFAULTS and type(v) is type(DEFAULTS[k]):
                settings[k] = v
        return


def save_settings():
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(settings, f, indent=2)
    except Exception:
        pass


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
        lines = ["", center(BOLD + "OPTIONS" + RESET, cols), ""]
        for i, (k, label, _) in enumerate(OPTIONS):
            val = "‹ %s ›" % fmt_value(k, settings[k])
            row = "  %-*s   %-14s" % (label_w, label, val)
            if i == self.sel:
                row = REVERSE + BOLD + row + RESET
            lines.append(center(row, cols))
        lines.append("")
        lines.append(center(DIM + HINTS.get(OPTIONS[self.sel][0], "") + RESET, cols))
        while len(lines) < rows - 1:
            lines.append("")
        return lines[:rows - 1] + [center(DIM + "↑/↓ choose   ←/→ change   o/Esc close" + RESET, cols)]
