"""The game list and the live game view."""

import sys
import time

from .clock import ClockSmoother, clock_target
from .leagues import LEAGUES
from .model import fmt_clock, fmt_start, headline, is_stale, list_status
from .render import big_text, logo_lines, place
from .settings import LOGO_SCALE, MAX_DELAY, fmt_delay, save_settings, settings, step_delay
from .term import BOLD, DIM, RESET, REVERSE, center, visible_len

LIST_REFRESH = 30   # seconds between refreshes of the game list
YELLOW, RED = "\x1b[33m", "\x1b[31m"


def with_footer(lines, rows, cols, text):
    lines = list(lines)
    while len(lines) < rows - 1:
        lines.append("")
    return lines[:rows - 1] + [center(DIM + text + RESET, cols)]


# --------------------------------------------------------------------------
# Game list

def game_list_screen(term, menu):
    """Return (league, game) to watch, or None to quit."""
    idx = next((i for i, lg in enumerate(LEAGUES) if lg.key == settings["league"]), 0)
    cache = {}          # league key -> (fetched_at, games, error)
    show_all, sel = False, 0
    while True:
        league = LEAGUES[idx]
        entry = cache.get(league.key)
        if entry is None or time.time() - entry[0] > LIST_REFRESH:
            try:
                games, err = league.list_games(), None
            except Exception as e:
                games, err = (entry[1] if entry else []), str(e)
            entry = cache[league.key] = (time.time(), games, err)
        _, games, err = entry

        cols, rows = term.size()
        if menu.open:
            term.draw(menu.render(cols, rows))
            key = term.read_key(0.5)
            if key:
                menu.handle_key(key)
            continue

        live = [g for g in games if g.state == "live"]
        shown = games if (show_all or not live) else live
        sel = max(0, min(sel, len(shown) - 1))
        hide = settings["hide_list_scores"]

        tabs = []
        for i, lg in enumerate(LEAGUES):
            n = sum(g.state == "live" for g in cache[lg.key][1]) if lg.key in cache else 0
            label = " %s%s " % (lg.name, " %s●%s%s" % (RED, RESET, BOLD + (REVERSE if i == idx else "")) if n else "")
            tabs.append(REVERSE + BOLD + label + RESET if i == idx else DIM + label + RESET)
        lines = ["", center("   ".join(tabs), cols), ""]
        if err and not games:
            lines.append(center(RED + "Could not reach %s data: %s" % (league.name, err[:60]) + RESET, cols))
        elif not games:
            lines.append(center("No %s games today." % league.name, cols))
        else:
            if not live:
                lines.append(center(DIM + "No games are live right now. Showing all scheduled games." + RESET, cols))
            else:
                lines.append(center(DIM + ("All games today" if show_all else "Live games") + RESET, cols))
            lines.append("")
            for i, g in enumerate(shown):
                if g.state == "pre":
                    score = "         "
                elif hide:
                    score = "  ?  - ? "
                else:
                    score = "%3s - %-3s" % (g.away.score, g.home.score)
                dot = RED + "●" + RESET if g.state == "live" else " "
                row = " %s %-4s @ %-4s %s   %-14s" % (
                    dot, g.away.abbrev, g.home.abbrev, score,
                    list_status(g, hide, settings["clock_24h"]))
                if i == sel:
                    row = REVERSE + BOLD + row + RESET
                lines.append(center(row, cols))
        lines.append("")
        if settings["delay"]:
            lines.append(center(DIM + "Stream delay: %s" % fmt_delay(settings["delay"]) + RESET, cols))
        term.draw(with_footer(lines, rows, cols,
                              "←/→ league   ↑/↓ select   Enter watch   a live/all   o options   r refresh   q quit"))

        key = term.read_key(0.5)
        if key in ("q", "Q", "esc"):
            return None
        if key in ("up", "k"):
            sel -= 1
        elif key in ("down", "j"):
            sel += 1
        elif key in ("left", "right", "\t", "1", "2", "3"):
            if key in ("1", "2", "3"):
                idx = min(int(key) - 1, len(LEAGUES) - 1)
            else:
                idx = (idx + (-1 if key == "left" else 1)) % len(LEAGUES)
            sel, show_all = 0, False
            settings["league"] = LEAGUES[idx].key
            save_settings()
        elif key == "a":
            show_all, sel = not show_all, 0
        elif key == "r":
            cache.pop(league.key, None)
        elif key == "o":
            menu.toggle()
        elif key == "enter" and shown:
            return league, shown[sel]


# --------------------------------------------------------------------------
# Game view

def team_info(team, colors, tint, scale, flash, compact):
    name = team.name.upper()
    if flash:
        name = REVERSE + BOLD + colors.fg(tint) + " %s %s " % (flash, name) + RESET
    else:
        name = BOLD + colors.fg(tint) + name + RESET
    extra = list(team.stats) if settings["show_stats"] else []
    if settings["show_situation"]:
        extra += [YELLOW + t + RESET + DIM for t in team.tags]
    extra = DIM + "   ".join(extra) + RESET if extra else ""
    if compact:
        info = [name + ("   " + extra if extra else "")]
    else:
        info = [name, extra, ""]
    info += [colors.fg(tint) + r + RESET for r in big_text(str(team.score), scale)]
    return info


def team_block(logo_rows, info, logos, team, colors, tint, box_w, left_x):
    """Logo box on the left, info on the right; both teams share left_x."""
    grid = logos.grid(team.logo_url, box_w, logo_rows * 2) if logo_rows > 0 else None
    if grid:
        logo = logo_lines(grid, colors)
        lw = len(grid[0])
    else:
        # Fallback badge if the logo couldn't be fetched.
        box_w = max(box_w, 7)
        logo = [colors.bg(tint) + " " * box_w + RESET for _ in range(max(3, logo_rows))]
        logo[len(logo) // 2] = colors.bg(tint) + BOLD + team.abbrev.center(box_w) + RESET
        lw = box_w
    # Center the logo inside its box both ways.
    height = max(logo_rows, len(logo), len(info))
    lpad = (box_w - lw) // 2
    top = (height - len(logo)) // 2
    logo = [" " * box_w] * top + [" " * lpad + l + RESET + " " * (box_w - lw - lpad) for l in logo]
    logo += [" " * box_w] * (height - len(logo))
    itop = (height - len(info)) // 2
    info = [""] * itop + info + [""] * (height - len(info) - itop)
    return [" " * left_x + l + "    " + r for l, r in zip(logo, info)]


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


def game_screen(term, league, listed, logos, colors, menu):
    """Show one game live. Return True to go back to the list, False to quit."""
    poller = league.open(listed)
    history = []          # [(data_time, Game)], oldest first
    err, next_poll = None, 0
    prev_score, alert, alert_side, alert_until = None, None, None, 0
    smoother = ClockSmoother()
    footer = "b back   o options   +/- delay   q quit"

    while True:
        now = time.time()
        if now >= next_poll:
            try:
                snap, data_t, wait = poller.poll()
                # A snapshot behind the newest one is a stale cached copy.
                if not history or (data_t > history[-1][0] and not is_stale(snap, history[-1][1])):
                    history.append((data_t, snap))
                next_poll = now + wait
                err = None
            except Exception as e:
                err = str(e)
                next_poll = now + 5
            # Drop snapshots too old for any allowed delay (keep one older).
            while len(history) > 2 and history[1][0] < now - MAX_DELAY - 60:
                history.pop(0)
            now = time.time()

        cols, rows = term.size()
        if menu.open:
            term.draw(menu.render(cols, rows))
            key = term.read_key(0.25)
            if key:
                menu.handle_key(key)
            continue

        delay = settings["delay"]
        view_t = now - delay
        g = next((s for t, s in reversed(history) if t <= view_t), None)

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

            secs = smoother.update(g, clock_target(g, view_t), now)
            if g.state == "final":
                clock_str, tag = "00:00", DIM + "final" + RESET
            elif g.state == "pre":
                clock_str, tag = fmt_clock(secs or 0), DIM + "not started" + RESET
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
            mid_scale = 2 if rows >= 60 and cols >= 90 else 1
            gap = [] if compact else [""]
            middle = [BOLD + head + RESET] + gap
            middle += [BOLD + r + RESET for r in big_text(clock_str, mid_scale)]
            middle += gap + [tag]
            if settings["show_situation"] and g.note:
                middle.append(BOLD + YELLOW + g.note + RESET)
            if settings["show_last_play"] and g.last_play and not compact:
                middle.append(DIM + g.last_play[:cols - 2] + RESET)
            middle = [center(m, cols) for m in middle]

            # Vertical layout: clock in the middle of the screen, each team
            # centered in its half above and below it.
            half = (usable - len(middle)) // 2
            max_logo_rows = max(0, half - 1)
            tints = (logos.tint(g.away), logos.tint(g.home))
            scale = 2 if max_logo_rows >= 14 else 1
            infos = [
                team_info(g.away, colors, tints[0], scale, blink_on and alert_side == "away" and alert, compact),
                team_info(g.home, colors, tints[1], scale, blink_on and alert_side == "home" and alert, compact),
            ]
            info_w = max(visible_len(s) for inf in infos for s in inf)
            width_cap = (cols - 4 - 4 - info_w) // 2  # logo is 2 cols per text row
            logo_rows = int(min(max_logo_rows, width_cap, 32) * LOGO_SCALE[settings["logo_size"]])
            if logo_rows < 4:
                logo_rows = 0
            box_w = logo_rows * 2
            left_x = max(0, (cols - (max(box_w, 7) + 4 + info_w)) // 2)
            away = team_block(logo_rows, infos[0], logos, g.away, colors, tints[0], box_w, left_x)
            home = team_block(logo_rows, infos[1], logos, g.home, colors, tints[1], box_w, left_x)

            lines = [""] * rows
            title = DIM + "%s · %s @ %s · %s" % (league.name, g.away.abbrev, g.home.abbrev, g.venue) + RESET
            if err:
                title += "  " + YELLOW + "(update failed, retrying)" + RESET
            lines[0] = center(title, cols)
            mid_top = 1 + (usable - len(middle)) // 2
            mid_bot = mid_top + len(middle)
            margin = 0 if compact else 1
            place(lines, away, 1, mid_top - margin)
            place(lines, middle, mid_top, mid_bot)
            place(lines, home, mid_bot + margin, rows - 1)
            lines[rows - 1] = center(DIM + footer + RESET, cols)
            term.draw(lines)

        key = term.read_key(0.25)
        if key in ("q", "Q"):
            return False
        if key in ("b", "esc", "\x7f"):
            return True
        if key == "o":
            menu.toggle()
        elif key in ("+", "="):
            settings["delay"] = step_delay(settings["delay"], 1)
            save_settings()
        elif key in ("-", "_"):
            settings["delay"] = step_delay(settings["delay"], -1)
            save_settings()
