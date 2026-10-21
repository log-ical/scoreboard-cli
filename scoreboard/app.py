"""Entry point: argument parsing and the screen loop."""

import argparse
import sys

from .leagues import BY_KEY
from .logos import LogoStore
from .screens import game_list_screen, game_screen
from .settings import MAX_DELAY, OptionsMenu, load_settings, save_settings, settings
from .term import SYMBOL_SETS, Colors, Terminal, detect_color_mode, set_symbols


def main(argv=None):
    p = argparse.ArgumentParser(prog="scoreboard", description="Live NHL, NBA and NFL scores in the terminal.")
    p.add_argument("league", nargs="?", choices=sorted(BY_KEY), help="league to open (default: last used)")
    p.add_argument("--color", choices=["truecolor", "256", "16"],
                   help="color mode (overrides the saved option)")
    p.add_argument("--symbols", choices=SYMBOL_SETS,
                   help="symbol set: full Unicode, console-safe or ascii (overrides the saved option)")
    p.add_argument("--delay", type=int, metavar="SECONDS",
                   help="stream delay in seconds (saved as the new default)")
    args = p.parse_args(argv)

    if not sys.stdin.isatty():
        sys.exit("scoreboard needs an interactive terminal.")

    load_settings()
    if args.league:
        settings["league"] = args.league
    if args.delay is not None:
        settings["delay"] = max(0, min(MAX_DELAY, args.delay))
    save_settings()
    mode = args.color or (settings["color"] if settings["color"] != "auto" else detect_color_mode())
    colors = Colors(mode)
    set_symbols(args.symbols or settings["symbols"])
    logos = LogoStore()
    menu = OptionsMenu(colors)

    try:
        with Terminal() as term:
            while True:
                picked = game_list_screen(term, menu)
                if picked is None:
                    break
                term.draw([])
                if not game_screen(term, *picked, logos, colors, menu):
                    break
    except KeyboardInterrupt:
        pass
