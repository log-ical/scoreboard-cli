"""The game list and the live game view."""

import dataclasses
import datetime
import queue
import re
import sys
import textwrap
import threading
import time

from . import live
from .clock import ClockSmoother, clock_target
from .leagues import LEAGUES
from .model import (fmt_clock, fmt_countdown, fmt_countdown_short, fmt_start, headline, is_stale,
                    list_status, parse_utc, until_start)
from .render import big_text, logo_lines, place
from .settings import LOGO_SCALE, MAX_DELAY, fmt_delay, save_settings, settings, step_delay
from .term import BOLD, DIM, RESET, REVERSE, center, clip, printable, shorten, visible_len

LIST_REFRESH = 30   # seconds between refreshes of the game list
MAX_WAIT = 60       # longest the game feed waits between polls, whatever a poller asks
QUIET_AFTER = 60    # seconds without an update before the game view says so
YELLOW, RED = "\x1b[33m", "\x1b[31m"


def start_day(game):
    """The local date a game starts on, or None if unknown."""
    ts = parse_utc(game.start_utc)
    return datetime.date(*time.localtime(ts)[:3]) if ts is not None else None


def day_label(day):
    ahead = (day - datetime.date.today()).days
    return {-1: "Yesterday", 0: "Today", 1: "Tomorrow"}.get(ahead) or "%s %d" % (day.strftime("%a %b"), day.day)


def scroll_window(body, want, top, room, cols, note=None, headings=()):
    """The part of body that fits in room rows, scrolled from top just
    enough to show rows want[0]..want[1]. Returns (rows, new top). `note`
    (styled) takes the top row while the list isn't scrolled down. A row in
    `headings` (a day's) isn't shown last with its games all below."""
    if len(body) <= room - (1 if note else 0):
        return ([center(note, cols)] if note else []) + body, 0
    room = max(1, room - 2)   # a row above and below says there's more
    top = max(min(top, want[0]), want[1] - room + 1)
    top = max(0, min(top, len(body) - room))
    more = lambda arrow, n: center(DIM + (arrow + " more" if n else "") + RESET, cols)
    above = center(note, cols) if note and not top else more("▲", top)
    rows = body[top:top + room]
    if top + room < len(body) and top + room - 1 in headings and room > 1:
        rows[-1] = ""
    return [above] + rows + [more("▼", len(body) - top - room)], top


def with_footer(lines, rows, cols, text):
    lines = list(lines)
    while len(lines) < rows - 1:
        lines.append("")
    return lines[:rows - 1] + [center(DIM + text + RESET, cols)]


def drain(q):
    """Everything waiting in queue q, oldest first, without blocking."""
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            return items


# --------------------------------------------------------------------------
# Game list

class ListLoader:
    """Loads game lists on daemon threads, so the screen never waits on them.

    Only the screen's thread calls its methods; results come back through
    a queue as (key, result, error): a league's key with its games, or
    ("earlier", league key) with an earlier day's (day, games).
    """

    def __init__(self):
        self.results = queue.Queue()
        self.busy = set()      # keys being loaded

    def load(self, league):
        """Start loading league's games, unless that's already under way."""
        self._start(league.key, league.list_games)

    def load_earlier(self, league, day):
        """Start loading the last game day before `day`."""
        self._start(("earlier", league.key), lambda: league.earlier(day))

    def _start(self, key, job):
        if key not in self.busy:
            self.busy.add(key)
            threading.Thread(target=self._run, args=(key, job), daemon=True).start()

    def _run(self, key, job):
        try:
            self.results.put((key, job(), None))
        except Exception as e:   # the text can carry a server's words (an HTTP reason)
            self.results.put((key, None, printable(str(e))))

    def done(self):
        """Lists loaded since the last call."""
        items = drain(self.results)
        self.busy.difference_update(key for key, _, _ in items)
        return items


# Earlier game days loaded with ↑ at the top of a list, kept for the session so
# they're still there after watching one: league key -> {"pages": [(day,
# games)] earliest first, "shown": how many of the latest pages are on screen
# (leaving the league hides them again; ↑ brings them back without a fetch),
# "done": nothing earlier, "err": the last failure, "anchor": the league's
# first day when they were loaded, which they join up with}.
_earlier = {}
_picked = None    # (league key, game id) last opened, to select it again on return


def _earlier_state(key):
    return _earlier.setdefault(key, {"pages": [], "shown": 0, "done": False, "err": None, "anchor": None})


def start_index(games):
    """Where a fresh list starts: the first live game, else the next one to
    be played, else the top (so last night's finals don't come first)."""
    for state in ("live", "pre"):
        i = next((i for i, g in enumerate(games) if g.state == state), None)
        if i is not None:
            return i
    return 0


def game_list_screen(term, menu):
    """Return (league, game) to watch, or None to quit."""
    global _picked
    idx = next((i for i, lg in enumerate(LEAGUES) if lg.key == settings["league"]), 0)
    cache = {}          # league key -> (time.monotonic() fetched, games, error)
    loader = ListLoader()
    show_all, sel, top = False, 0, 0
    games = []          # the shown league's list, as of the last frame
    jump = align = True  # select the next game once the list is in, and scroll to it
    while True:
        league = LEAGUES[idx]
        for key, result, err in loader.done():
            if isinstance(key, tuple):   # an earlier day
                state = _earlier_state(key[1])
                state["err"] = err
                if not err and result[0] is None:
                    state["done"] = True
                elif not err:
                    if not state["pages"]:
                        state["anchor"] = next(getattr(lg, "first_day", None) for lg in LEAGUES if lg.key == key[1])
                    known = {g.id for _, page in state["pages"] for g in page}
                    state["pages"].insert(0, (result[0], [g for g in result[1] if g.id not in known]))
                    # Shown if it's for the list on screen; else just kept for ↑ later.
                    if key[1] == league.key and (show_all or not any(g.state == "live" for g in games)):
                        state["shown"] += 1
                        sel = step_into(state, games, sel)
                continue
            if err:
                result = cache[key][1] if key in cache else []
            cache[key] = (time.monotonic(), result, err)
        entry = cache.get(league.key)
        if entry is None or time.monotonic() - entry[0] > LIST_REFRESH:
            loader.load(league)
        _, games, err = entry or (0, [], None)
        state = _earlier_state(league.key)
        if state["pages"] and state["anchor"] != getattr(league, "first_day", None):
            # The league's day (the NFL's week) has moved on: the days kept no
            # longer join up with the list, and ↑ would skip the ones between.
            state.update(pages=[], shown=0, done=False, err=None)
        # The day to fetch the game day before, once every kept day is shown.
        start = state["pages"][0][0] if state["pages"] else getattr(league, "first_day", None)

        cols, rows = term.size()
        if menu.open:
            term.draw(menu.render(cols, rows))
            key = term.read_key(0.5)
            if key == "\x0c":   # Ctrl-L repaints here too
                term.invalidate()
            elif key:
                menu.handle_key(key)
            continue

        live = [g for g in games if g.state == "live"]
        current = {g.id for g in games}
        revealed = state["pages"][len(state["pages"]) - state["shown"]:] if state["shown"] else []
        everything = [g for _, page in revealed for g in page if g.id not in current] + games
        shown = everything if (show_all or not live) else live
        if jump and games:
            sel, jump, align = start_index(shown), False, True
        if _picked and _picked[0] == league.key and games:
            # Back from a game: select it again (it may be an earlier one).
            ids = [g.id for g in everything]
            if _picked[1] in ids:
                show_all = show_all or _picked[1] not in {g.id for g in shown}
                shown = everything if (show_all or not live) else live
                sel = [g.id for g in shown].index(_picked[1])
            _picked = None
        sel = max(0, min(sel, len(shown) - 1))
        hide = settings["hide_list_scores"]

        tabs = []
        for i, lg in enumerate(LEAGUES):
            n = sum(g.state == "live" for g in cache[lg.key][1]) if lg.key in cache else 0
            label = " %s%s " % (lg.name, " %s●%s%s" % (RED, RESET, BOLD + (REVERSE if i == idx else "")) if n else "")
            tabs.append(REVERSE + BOLD + label + RESET if i == idx else DIM + label + RESET)
        lines = ["", center("   ".join(tabs), cols), ""]
        if entry is None:
            lines.append(center(DIM + "Loading %s games…" % league.name + RESET, cols))
        elif err and not games:
            lines.append(center(RED + "Could not reach %s data: %s" % (league.name, err[:60]) + RESET, cols))
        elif not games:
            lines.append(center("No %s games scheduled." % league.name, cols))
        else:
            if not live:
                lines.append(center(DIM + "No games are live right now. Showing all scheduled games." + RESET, cols))
            else:
                lines.append(center(DIM + ("All games" if show_all else "Live games") + RESET, cols))
            lines.append("")
            # A list spanning several days (NHL and NFL: the week) gets a
            # heading per day, and scrolls to keep the selected game in view.
            days = [start_day(g) for g in shown]
            headings = len(set(days)) > 1
            body, want, heading_rows = [], (0, 0), set()
            for i, g in enumerate(shown):
                first = headings and (i == 0 or days[i] != days[i - 1])
                if first:
                    if body:
                        body.append("")
                    heading_rows.add(len(body))
                    body.append(center(BOLD + (day_label(days[i]) if days[i] else "Date unknown") + RESET, cols))
                if g.state == "pre":
                    score = "         "
                elif hide:
                    score = "  ?  - ? "
                else:
                    score = "%3s - %-3s" % (g.away.score, g.home.score)
                dot = RED + "●" + RESET if g.state == "live" else " "
                left = until_start(g) if g.state == "pre" else None
                soon = fmt_countdown_short(left) if left and left < 24 * 3600 else ""   # not once it's due
                row = " %s %-4s @ %-4s %s   %-14s %-10s" % (
                    dot, g.away.abbrev, g.home.abbrev, score,
                    list_status(g, hide, settings["clock_24h"]), soon)
                if i == sel:
                    row = REVERSE + BOLD + row + RESET
                    want = (len(body) - (1 if first else 0), len(body))   # with its day's heading
                body.append(center(row, cols))
            room = rows - len(lines) - 2 - (1 if settings["delay"] else 0)   # blank, delay, footer
            if align:   # a fresh list: its starting game (and its day's heading) at the top
                top, align = want[0], False
            note = earlier_note(state, start, loader, league, shown is live)
            window, top = scroll_window(body, want, top, room, cols, note, heading_rows)
            lines += window
        lines.append("")
        if settings["delay"]:
            lines.append(center(DIM + "Stream delay: %s" % fmt_delay(settings["delay"]) + RESET, cols))
        term.draw(with_footer(lines, rows, cols,
                              "←/→ league  ↑/↓ select  Enter watch  a live/all  o options  r refresh  q quit"))

        key = term.read_key(0.1 if loader.busy else 0.5)   # show a list soon after it arrives
        if key in ("q", "Q", "esc"):
            return None
        if key == "\x0c":   # Ctrl-L: repaint the whole screen
            term.invalidate()
        elif key in ("up", "k"):
            if sel > 0:
                sel -= 1
            elif live and not show_all:
                show_all, top = True, 0   # earlier games are in the all-games list
            elif state["shown"] < len(state["pages"]):
                state["shown"] += 1       # a day already fetched: no need to wait
                sel = step_into(state, games, sel)
            elif start and not state["done"] and games:
                loader.load_earlier(league, start)
        elif key in ("down", "j"):
            sel += 1
        elif key in ("pgup", "pgdn"):
            sel = max(0, sel + (rows // 2 if key == "pgdn" else -(rows // 2)))
        elif key in ("home", "end"):
            sel = 0 if key == "home" else len(shown) - 1
        elif key in ("left", "right", "\t", "1", "2", "3"):
            if key in ("1", "2", "3"):
                idx = min(int(key) - 1, len(LEAGUES) - 1)
            else:
                idx = (idx + (-1 if key == "left" else 1)) % len(LEAGUES)
            _earlier_state(league.key)["shown"] = 0   # kept, but hidden until ↑ asks again
            sel, show_all, top, jump = 0, False, 0, True
            settings["league"] = LEAGUES[idx].key
            save_settings()
        elif key == "a":
            show_all, sel, top, jump = not show_all, 0, 0, True
        elif key == "r":
            loader.load(league)   # the list stays up until the new one is in
        elif key == "o":
            menu.toggle()
        elif key == "enter" and shown:
            _picked = (league.key, shown[sel].id)
            return league, shown[sel]


def step_into(state, games, sel):
    """The selection after the earliest shown day was put above the list:
    from the top, onto that day's last game; else the same game as before."""
    current = {g.id for g in games}
    n = sum(1 for g in state["pages"][len(state["pages"]) - state["shown"]][1] if g.id not in current)
    return max(0, n - 1) if sel == 0 else sel + n


def earlier_note(state, start, loader, league, live_only):
    """The top row's "▲ earlier" note: what ↑ from the first game does."""
    if not start:
        return None   # the league can't look back
    if live_only:
        text = "▲ all games"
    elif state["shown"] < len(state["pages"]):
        text = "▲ earlier"   # kept from before: shows at once
    elif ("earlier", league.key) in loader.busy:
        text = "▲ loading earlier games…"
    elif state["done"]:
        text = "no games in the two weeks before"
    elif state["err"]:
        text = "▲ earlier (couldn't load: %s)" % state["err"][:40]
    else:
        text = "▲ earlier"
    return DIM + text + RESET


# --------------------------------------------------------------------------
# Game view

def team_info(team, colors, tint, scale, flash, compact, slim=False):
    """The name, stats and score in block digits; with slim, one line with
    the score in plain digits."""
    name = team.name.upper()
    if flash:
        name = REVERSE + BOLD + colors.fg(tint) + " %s %s " % (flash, name) + RESET
    else:
        name = BOLD + colors.fg(tint) + name + RESET
    if settings["show_record"] and team.record:
        name += "  " + DIM + "(%s)" % team.record + RESET
    extra = list(team.stats) if settings["show_stats"] else []
    if settings["show_situation"]:
        extra += [YELLOW + t + RESET + DIM for t in team.tags]
    extra = DIM + "   ".join(extra) + RESET if extra else ""
    if slim:
        return [name + "  " + BOLD + colors.fg(tint) + str(team.score) + RESET + ("   " + extra if extra else "")]
    if compact:
        info = [name + ("   " + extra if extra else "")]
    else:
        info = [name, extra, ""]
    info += [colors.fg(tint) + r + RESET for r in big_text(str(team.score), scale)]
    return info


def logo_width(logo_rows, aspect):
    """Columns for a logo box logo_rows tall that is square on screen."""
    return round(logo_rows * 2 / aspect)


def logo_box(logo_rows, logos, team, colors, tint, aspect):
    """Logo centered both ways in a box at least logo_rows tall, square on screen."""
    box_w = logo_width(logo_rows, aspect)
    grid = logos.grid(team.logo_url, box_w, logo_rows * 2, aspect) if logo_rows > 0 else None
    if grid:
        logo = logo_lines(grid, colors)
        lw = len(grid[0])
    else:
        # Fallback badge if the logo couldn't be fetched.
        box_w = max(box_w, 7)
        logo = [colors.bg(tint) + " " * box_w + RESET for _ in range(max(3, logo_rows))]
        logo[len(logo) // 2] = colors.bg(tint) + BOLD + team.abbrev.center(box_w) + RESET
        lw = box_w
    height = max(logo_rows, len(logo))
    lpad = (box_w - lw) // 2
    top = (height - len(logo)) // 2
    box = [" " * box_w] * top + [" " * lpad + l + RESET + " " * (box_w - lw - lpad) for l in logo]
    return box + [" " * box_w] * (height - len(box))


def team_block(logo_rows, info, logos, team, colors, tint, aspect, left_x):
    """Logo box on the left, info on the right; both teams share left_x."""
    logo = logo_box(logo_rows, logos, team, colors, tint, aspect)
    box_w = visible_len(logo[0])
    height = max(len(logo), len(info))
    top = (height - len(logo)) // 2
    logo = [" " * box_w] * top + logo + [" " * box_w] * (height - len(logo) - top)
    itop = (height - len(info)) // 2
    info = [""] * itop + info + [""] * (height - len(info) - itop)
    return [" " * left_x + l + "    " + r for l, r in zip(logo, info)]


def fit(line, width):
    """Center line in exactly width columns (cutting it if it's wider)."""
    line = clip(line, width)
    n = visible_len(line)
    pad = max(0, (width - n) // 2)
    return " " * pad + line + RESET + " " * max(0, width - n - pad)


def side_by_side(g, logos, colors, tints, flashes, middle, tail, cols, height, aspect, panel=None):
    """Horizontal layout: away | clock | home, then `tail` (the last play).

    With `panel` (a function of width and height giving the play list), the
    list replaces the tail and fills the space between the teams under the
    clock; the teams stay exactly where they are without it. None if the
    window is too narrow.
    """
    mid_w = max(visible_len(m) for m in middle)
    col_w = (cols - mid_w - 8) // 2
    for scale in (2, 1):
        infos = [team_info(t, colors, tint, scale, flash, False)
                 for t, tint, flash in zip((g.away, g.home), tints, flashes)]
        info_w = max(visible_len(s) for inf in infos for s in inf)
        if info_w <= col_w and (scale == 1 or height - len(tail) - len(infos[0]) >= 15):
            break
    else:
        return None
    max_logo_rows = height - len(tail) - len(infos[0]) - 1
    logo_rows = int(min(max_logo_rows, col_w * aspect / 2, 32) * LOGO_SCALE[settings["logo_size"]])
    if logo_rows < 4:
        logo_rows = 0
    columns = [logo_box(logo_rows, logos, team, colors, tint, aspect) + [""] + info
               for team, tint, info in zip((g.away, g.home), tints, infos)]
    team_w = max(visible_len(l) for c in columns for l in c)
    outer = (col_w - team_w) // 2
    between = 2 * (col_w - team_w - outer) + mid_w + 8   # the gap between the teams
    rows = max(len(columns[0]), len(columns[1]), len(middle))
    if panel:
        # The list takes the tail's rows too, and the clock moves to the top.
        rows += len(tail)
        middle = middle + [""] + panel(between - 4, rows - len(middle) - 1)
        tail = []
    mtop = (rows - len(middle)) // 2
    middle = [""] * mtop + middle + [""] * (rows - len(middle) - mtop)
    blank = " " * team_w
    columns = [[fit(l, team_w) for l in c] + [blank] * (rows - len(c)) for c in columns]
    return [center(" " * outer + a + fit(m, between) + h + " " * outer, cols)
            for a, m, h in zip(columns[0], middle, columns[1])] + tail


def visible_details(g, details):
    """The play-by-play view's events, cut at the view's last play like visible_plays."""
    return [d for d in details if g.play_seq is not None and d.seq <= g.play_seq]


def visible_plays(g, loaded=None):
    """Plays up to the one the (possibly delayed) view shows, newest first.

    ESPN plays (`loaded`, from the poller) are fetched while the list is open,
    so cut them at the view's last play so nothing is spoiled. NHL snapshots
    carry every goal and penalty, so use the view's own.
    """
    if loaded is None:
        return [t for _, t in g.plays]
    return [t for seq, t in loaded if g.play_seq is not None and seq <= g.play_seq]


def wrap_play(text, width):
    """Wrap a play, continuing under its text rather than its period and clock."""
    m = re.match(r"\S+\s+\S+\s+", text)
    indent = len(m.group(0)) if m and len(m.group(0)) < width // 2 else 0
    return textwrap.wrap(text, width, subsequent_indent=" " * indent) or [""]


def plays_panel(plays, scroll, height, width, loading, older=False):
    """The expanded play list in at most height rows of width columns, left
    aligned. Returns (rows, scroll clamped to the list, plays shown)."""
    if not plays:
        return [DIM + ("Loading plays…" if loading else "No plays yet.") + RESET], 0, 0
    width = max(20, min(width, max(len(p) for p in plays)))
    room = max(1, height - 1)   # the last row says where we are
    wrapped = [wrap_play(p, width) for p in plays]
    # Don't scroll past the point where the last plays fill the space.
    last, used = len(plays), 0
    while last > 0 and used + len(wrapped[last - 1]) <= room:
        last -= 1
        used += len(wrapped[last])
    scroll = max(0, min(scroll, last))
    rows, shown = [], 0
    for i in range(scroll, len(plays)):
        if rows and len(rows) + len(wrapped[i]) > room:
            break
        rows += [(BOLD if i == 0 else DIM) + w.ljust(width) + RESET for w in wrapped[i][:room]]
        shown += 1
    note = "%d-%d of %d%s" % (scroll + 1, scroll + shown, len(plays), "+" if older else "")
    if loading:
        note += "   loading…"
    elif older and scroll + shown >= len(plays):
        note += "   ] earlier plays"
    return rows + [DIM + note.rjust(width) + RESET], scroll, shown


def newer(snap, data_t, history, dated):
    """Whether a snapshot goes on the end of the history: dated after the last
    one and not an older cached copy. Copies dated by their age (dated: the
    NHL's Age header) are ordered by that alone, so a real correction (an
    overturned goal puts the clock back) isn't taken for a stale copy. Others
    are judged by what they show, unless they bring a newer play."""
    if not history:
        return True
    t, last = history[-1]
    if data_t <= t:
        return False
    return dated or (snap.play_seq or 0) > (last.play_seq or 0) or not is_stale(snap, last)


def buffering_screen(cols, rows, history, delay, now, err, footer):
    lines = [""] * (rows // 2 - 3)
    if not history:
        lines.append(center("Loading game…" if not err else "Error: " + err[:60], cols))
    else:
        wait = int(history[0][0] + delay - now) + 1
        lines.append(center(BOLD + "Stream delay %s · buffering" % fmt_delay(delay) + RESET, cols))
        lines.append("")
        lines += [center(BOLD + r + RESET, cols) for r in big_text("%d:%02d" % divmod(wait, 60))]
        lines.append("")
        lines.append(center(DIM + "Game starts showing in %ds" % wait + RESET, cols))
    return with_footer(lines, rows, cols, footer)


class GameFeed:
    """Polls a game on a daemon thread, so the screen never waits on the network.

    The thread owns the poller: nothing else touches it once it starts.
    Results come back through a queue as ("game", snapshot, data_time),
    ("error", text) and, for pollers with their own play list (ESPN:
    want_plays, load_older, play_history), ("plays", [(seq, text)] newest
    first, whether older plays exist), a copy made on the thread, so the
    screen never reads the dict the poller is changing. The screen's requests
    are fields under `lock` plus a wake-up. stop() lets the screen leave at
    once: the thread posts nothing more and exits after any request in flight.

    Only the newest "plays" and "details" are kept (the screen uses no
    other), and at most MAX_BACKLOG messages wait: while the screen can't
    draw (Ctrl-S, a stalled ssh), a backlog would grow by a full copy of the
    game's plays on every poll.
    """
    MAX_BACKLOG = 1000   # waiting messages; the delay keeps 30 min of snapshots (600 at 3 s)

    def __init__(self, poller, live=False):
        self._poller = poller
        self.has_plays = hasattr(poller, "play_history")
        self.has_live = hasattr(poller, "want_live")
        self.results = queue.Queue()
        self.newest = {}         # "plays" / "details" -> the latest such message, under lock
        self.lock = threading.Lock()
        # Requests, under the lock: play list wanted; list requests made
        # (asked) and answered by a poll (served); older-page requests made
        # and answered. Counting means a request made mid-load is never lost.
        self.want_plays, self.asked, self.served = False, 0, 0
        self.older_asked = self.older_served = 0
        self.want_live = live    # the play-by-play view is on (from the start, so the first poll has its data)
        self.wake, self.stopped = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def show_plays(self, on):
        """The play list was opened or closed: poll right away (fetching the list if open)."""
        if self.has_plays:
            with self.lock:
                self.want_plays, self.asked = on, self.asked + 1
            self.wake.set()

    def show_live(self, on):
        """The play-by-play view was turned on or off: poll right away for its data."""
        if self.has_live:
            with self.lock:
                self.want_live, self.asked = on, self.asked + 1
            self.wake.set()

    def load_older(self):
        """Fetch the page of plays before the oldest one loaded."""
        if self.has_plays:
            with self.lock:
                self.older_asked += 1
            self.wake.set()

    def plays_pending(self):
        """True while plays the open list asked for are on their way. Check
        before updates(): once it's False, updates() has them."""
        with self.lock:
            return self.want_plays and (self.served < self.asked or self.older_served < self.older_asked)

    def stop(self):
        self.stopped.set()
        self.wake.set()

    def updates(self):
        """Results posted since the last call, oldest first (the newest plays
        and details last)."""
        items = drain(self.results)
        with self.lock:
            newest, self.newest = self.newest, {}
        return items + list(newest.values())

    def _post(self, *msg):
        if self.stopped.is_set():
            return
        if msg[0] in ("plays", "details"):
            with self.lock:
                self.newest[msg[0]] = msg
            return
        if self.results.qsize() >= self.MAX_BACKLOG:
            try:
                self.results.get_nowait()   # the oldest: older than any delay shows
            except queue.Empty:
                pass   # the screen just took them all
        self.results.put(msg)

    def _run(self):
        # Scheduled on the monotonic clock, so setting the wall clock back
        # can't hold up polling; and a wait never longer than MAX_WAIT.
        poller, next_poll = self._poller, 0.0
        while True:
            self.wake.wait(min(MAX_WAIT, max(0.0, next_poll - time.monotonic())))
            self.wake.clear()   # before reading the requests, so none is missed
            if self.stopped.is_set():
                return
            with self.lock:
                want, asked, older, live = self.want_plays, self.asked, self.older_asked, self.want_live
            try:
                if self.has_plays:
                    poller.want_plays = want
                if self.has_live:
                    poller.want_live = live
                if older > self.older_served:
                    poller.load_older()   # one page for every request made before this read
                    self._post("plays", *poller.play_history())
                    with self.lock:
                        self.older_served = older
                    if self.stopped.is_set():
                        return            # left while it loaded: don't start a poll too
                if asked > self.served or time.monotonic() >= next_poll:
                    started = time.monotonic()
                    try:
                        snap, data_t, wait = poller.poll()
                        if live and hasattr(poller, "play_details"):
                            self._post("details", poller.play_details())   # a copy, as with plays
                        self._post("game", snap, data_t)
                    except Exception as e:
                        self._post("error", printable(str(e)))
                        wait = 5
                    next_poll = started + wait
                    if self.has_plays:
                        self._post("plays", *poller.play_history())
                    with self.lock:
                        self.served = asked
            except Exception as e:     # a bug, not the network: show it and carry on
                self._post("error", printable(str(e)))
                next_poll = time.monotonic() + 5


def game_screen(term, league, listed, logos, colors, menu):
    """Show one game live. Return True to go back to the list, False to quit."""
    feed = GameFeed(league.open(listed), live=settings["play_by_play"])
    try:
        return game_view(term, league, feed, logos, colors, menu)
    finally:
        feed.stop()   # don't wait for a request in flight


def game_view(term, league, feed, logos, colors, menu):
    """game_screen's loop, drawing what feed brings in."""
    history = []          # [(data_time, Game)], oldest first
    err = None
    arrived = None        # time.monotonic() the last snapshot came in, stale or not
    loaded = ([], False) if feed.has_plays else None   # ESPN: (plays, whether older ones exist)
    prev_score, alert, alert_side, alert_until = None, None, None, 0
    smoother, shown_delay = ClockSmoother(), settings["delay"]
    expanded, scroll, page, at_end = False, 0, 1, False
    paged_at = None       # how many plays were loaded when the list last paged back by itself
    details, live_on = [], feed.want_live   # the play-by-play view's events; whether it's on
    pinned = None         # the view's event when gone back in time with ← (its seq); None: the latest

    while True:
        now = time.time()
        events, at = [], None   # the view's events (to the delayed view's), and which is shown
        pending = feed.plays_pending()   # first: see plays_pending()
        updates = feed.updates()
        for kind, *msg in updates:
            if kind == "game":
                snap, data_t = msg
                if newer(snap, data_t, history, getattr(league, "dated_copies", False)):
                    history.append((data_t, snap))
                err, arrived = None, time.monotonic()
            elif kind == "error":
                err = msg[0]
            elif kind == "details":
                details = msg[0]
            else:
                loaded = msg
        if updates:
            # Drop snapshots too old for any allowed delay (keep one older).
            while len(history) > 2 and history[1][0] < now - MAX_DELAY - 60:
                history.pop(0)

        if settings["play_by_play"] != live_on:   # turned on or off in the options menu
            live_on = settings["play_by_play"]
            feed.show_live(live_on)
        footer = "b back  o options  p plays  %s+/- delay  q quit" % ("v view  ←/→ rewind  " if live_on else "")

        cols, rows = term.size()
        if menu.open:
            term.draw(menu.render(cols, rows))
            key = term.read_key(0.25)
            if key == "\x0c":   # Ctrl-L repaints here too
                term.invalidate()
            elif key:
                menu.handle_key(key)
            continue

        delay = settings["delay"]
        view_t = now - delay
        g = next((s for t, s in reversed(history) if t <= view_t), None)

        if g is not None and history[-1][1].state == "final" and g.state != "final":
            # Over in real time but not yet in the delayed view: the season
            # records may already count the result.
            g = dataclasses.replace(g, away=dataclasses.replace(g.away, record=""),
                                    home=dataclasses.replace(g.home, record=""))

        if g is None:
            term.draw(buffering_screen(cols, rows, history, delay, now, err, footer))
        else:
            # Score alerts on the (possibly delayed) view.
            score = (g.away.score, g.home.score)
            if prev_score is not None:
                for side, i in (("away", 0), ("home", 1)):
                    points = score[i] - prev_score[i]
                    text = league.score_alert(points) if points > 0 else None
                    if text:
                        alert, alert_side, alert_until = text, side, now + 10
                        if settings["bell"]:
                            sys.stdout.write("\a")
            prev_score = score
            blink_on = settings["score_alert"] and now < alert_until and int(now * 2) % 2 == 0

            if delay != shown_delay:   # a new delay: show its clock at once, not catch up to it
                smoother, shown_delay = ClockSmoother(), delay
            secs = smoother.update(g, clock_target(g, view_t), now)
            if g.state == "final":
                clock_str, tag = "00:00", DIM + "final" + RESET
            elif g.state == "pre":
                # Count down to the start on the viewer's clock (the stream is
                # delayed too), then wait for the data to say it has begun.
                left = until_start(g, view_t)
                if left is None:
                    clock_str, tag = fmt_clock(secs or 0), DIM + "not started" + RESET
                else:
                    clock_str = fmt_countdown(left)
                    tag = DIM + ("until start" if left else "starting soon") + RESET
            else:
                clock_str = fmt_clock(secs) if secs is not None else "--:--"
                tag = RED + "● LIVE" + RESET if g.clock_running else DIM + "clock stopped" + RESET
            if delay:
                tag += DIM + "   delayed %s" % fmt_delay(delay) + RESET
            head = headline(g) if g.state == "live" else (
                g.final_label if g.state == "final" else "STARTS " + fmt_start(g, settings["clock_24h"]))

            usable = rows - 2  # title row and footer row
            # Compact layout when the full one can't fit (score font is 5 rows).
            compact = (usable - 10) // 2 < 9
            # (2D 03:15 at scale 2 is 101 columns wide)
            mid_scale = 2 if rows >= 60 and cols >= 90 and visible_len(big_text(clock_str, 2)[0]) <= cols - 4 else 1
            gap = [] if compact else [""]
            middle = [BOLD + head + RESET] + gap
            middle += [BOLD + r + RESET for r in big_text(clock_str, mid_scale)]
            middle += gap + [tag]
            if settings["show_situation"] and g.note:
                middle.append(BOLD + YELLOW + g.note + RESET)
            last_play = settings["show_last_play"] and g.last_play and not compact
            aspect = term.pixel_aspect()
            tail = ["", center(DIM + shorten(g.last_play, cols - 2) + RESET, cols)] if last_play else []
            tints = (logos.tint(g.away), logos.tint(g.home))
            panel = live_fn = None
            if live_on:
                style = live.style_of(league.key, settings["live_styles"].get(league.key))
                events = visible_details(g, details)
                at = next((i for i, e in enumerate(events) if e.seq == pinned), None)
                if at is None or at == len(events) - 1:
                    pinned, at = None, None   # caught up with the latest (or it's not shown any more)

                def live_fn(width):
                    return live.view(league.key, style, g, events, width, colors, tints, league, at)
            if expanded:
                if loaded is not None:
                    plays, older = visible_plays(g, loaded[0]), loaded[1]
                    if not plays and older and not pending and len(loaded[0]) != paged_at:
                        # A long delay: every play loaded so far is after the
                        # view's. Page back until some show (while pages bring plays).
                        paged_at = len(loaded[0])
                        feed.load_older()
                        pending = True
                else:
                    plays, older = visible_plays(g), False
                loading = pending

                def panel(width, height):
                    nonlocal scroll, page, at_end
                    rows, top, page = plays_panel(plays, scroll, height, width, loading, older)
                    at_end = top + page >= len(plays)
                    if not loading:
                        scroll = top   # while plays are loading, keep a scroll past the end for them
                    return rows
            plays_fn = panel
            if live_fn:
                def panel(width, height):
                    """Between the teams: the play-by-play view (which says what
                    happened last), then the play list; the last play if the view
                    doesn't fit."""
                    out = live_fn(width)
                    if len(out) > height:
                        out = []
                    drawn = bool(out)
                    if not out:
                        out = [DIM + "(the play-by-play view needs a bigger window)" + RESET]
                    if plays_fn and height - len(out) - 1 >= 3:
                        out += [""] + plays_fn(width, height - len(out) - 1)
                    elif not plays_fn and last_play and not drawn and height - len(out) >= 2:
                        out += ["", DIM + shorten(g.last_play, width) + RESET]
                    return out
            flashes = [blink_on and alert_side == side and alert for side in ("away", "home")]

            lines = [""] * rows
            title = DIM + "%s · %s @ %s · %s" % (league.name, g.away.abbrev, g.home.abbrev, g.venue) + RESET
            quiet = time.monotonic() - arrived if arrived is not None else 0
            if err:
                title += "  " + YELLOW + "(update failed, retrying)" + RESET
            elif quiet > QUIET_AFTER:   # a request that hangs fails no faster than its timeouts
                title += "  " + YELLOW + "(no update for %ds)" % quiet + RESET
            lines[0] = center(title, cols)
            keys = "↑/↓ scroll   [ ] page   p close   b back   q quit" if expanded else footer
            lines[rows - 1] = center(DIM + keys + RESET, cols)

            body = None
            if settings["layout"] == "horizontal":
                body = side_by_side(g, logos, colors, tints, flashes, middle, tail, cols, usable, aspect, panel)
                if body:
                    place(lines, body, 1, rows - 1)

            if body is None:
                # Vertical layout (also the fallback when the window is too
                # narrow for side by side): clock in the middle of the screen,
                # each team centered in its half above and below it.
                # Each team needs its name, stats and score (and the gap to
                # the middle); the play list and the play-by-play view get
                # what's left, the list first, and the logos shrink.
                team_rows = 6 if compact else 9
                room = usable - len(middle) - 2 * team_rows   # rows the middle can still take
                list_rows = max(4, usable // 3) if plays_fn else 0
                slim = list_rows > room
                if slim:
                    # Too short (for the list, or at all): the scores go on the
                    # name lines, since block digits cut short can't be read,
                    # and the list gets the rest.
                    room = usable - len(middle) - 2
                    list_rows = room if plays_fn and room >= 4 else 0
                room -= list_rows
                drawn = False   # the view, which says what happened last
                if live_fn:
                    view = live_fn(min(cols - 4, live.MAX_WIDTH))
                    if view and len(view) + 1 <= room:
                        middle += [""] + view
                        room -= len(view) + 1
                        drawn = True
                    elif room > 0:
                        middle.append(DIM + "(the play-by-play view needs a bigger window)" + RESET)
                        room -= 1
                if list_rows:
                    middle += [""] + plays_fn(cols - 4, list_rows - 1)
                elif plays_fn and room > 0:
                    middle.append(DIM + "(the play list needs a taller window)" + RESET)
                elif last_play and not drawn and room > 0:
                    middle.append(DIM + shorten(g.last_play, cols - 2) + RESET)
                middle = [center(m, cols) for m in middle]
                if slim:
                    away, home = ([center(team_info(t, colors, tint, 1, flash, True, slim=True)[0], cols)]
                                  for t, tint, flash in zip((g.away, g.home), tints, flashes))
                    margin = 0
                else:
                    half = (usable - len(middle)) // 2
                    max_logo_rows = max(0, half - 1)
                    scale = 2 if max_logo_rows >= 14 else 1
                    infos = [team_info(t, colors, tint, scale, flash, compact)
                             for t, tint, flash in zip((g.away, g.home), tints, flashes)]
                    info_w = max(visible_len(s) for inf in infos for s in inf)
                    width_cap = (cols - 4 - 4 - info_w) * aspect / 2  # rows whose square box fits
                    logo_rows = int(min(max_logo_rows, width_cap, 32) * LOGO_SCALE[settings["logo_size"]])
                    if logo_rows < 4:
                        logo_rows = 0
                    box_w = max(logo_width(logo_rows, aspect), 7)
                    left_x = max(0, (cols - (box_w + 4 + info_w)) // 2)
                    away = team_block(logo_rows, infos[0], logos, g.away, colors, tints[0], aspect, left_x)
                    home = team_block(logo_rows, infos[1], logos, g.home, colors, tints[1], aspect, left_x)
                    margin = 0 if compact else 1
                # A middle taller than the window starts under the title and
                # is cut at the footer, rather than writing over them.
                mid_top = 1 + max(0, (usable - len(middle)) // 2)
                mid_bot = mid_top + len(middle)
                place(lines, away, 1, mid_top - margin)
                place(lines, middle, mid_top, min(mid_bot, rows - 1))
                place(lines, home, mid_bot + margin, rows - 1)
            term.draw(lines)

        key = term.read_key(0.25)
        key = {"pgup": "[", "pgdn": "]"}.get(key, key)   # page the play list
        if key in ("q", "Q"):
            return False
        if key in ("b", "esc", "\x7f"):
            return True
        if key == "\x0c":   # Ctrl-L: repaint the whole screen
            term.invalidate()
        elif key == "o":
            menu.toggle()
        elif key == "p":
            expanded, scroll, paged_at = not expanded, 0, None
            feed.show_plays(expanded)
        elif live_on and events and (key in ("left", "right", "home", "end") or (not expanded and key in ("[", "]"))):
            # Back and forth through the view's events; [ ] from score to score
            # (with the play list closed: open, they page it), Home/End to the ends.
            i = len(events) - 1 if at is None else at
            if key in ("left", "right"):
                i = max(0, i - 1) if key == "left" else i + 1
            elif key in ("home", "end"):
                i = 0 if key == "home" else len(events) - 1
            else:
                scores = [j for j, e in enumerate(events) if e.kind == "goal" or e.points]
                i = next((j for j in reversed(scores) if j < i), i) if key == "[" else \
                    next((j for j in scores if j > i), len(events) - 1)
            pinned = events[i].seq if i < len(events) - 1 else None
        elif key == "v" and live_on:   # the next style of play-by-play view, remembered per league
            chosen = live.style_of(league.key, settings["live_styles"].get(league.key))
            settings["live_styles"] = {**settings["live_styles"], league.key: live.next_style(league.key, chosen)}
            save_settings()
        elif expanded and key in ("up", "k", "down", "j", "[", "]"):
            if key in ("down", "j", "]") and at_end and loaded and loaded[1]:
                feed.load_older()   # past the loaded plays: fetch the page before them
            step = page if key in ("[", "]") else 1
            scroll = max(0, scroll + (-step if key in ("up", "k", "[") else step))
        elif key in ("+", "="):
            settings["delay"] = step_delay(settings["delay"], 1)
            save_settings()
        elif key in ("-", "_"):
            settings["delay"] = step_delay(settings["delay"], -1)
            save_settings()
