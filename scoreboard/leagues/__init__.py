"""League adapters. Each provides list_games(), open(game) -> poller with
poll() -> (Game, data_time, next_poll_in), and score_alert(points)."""

from .espn import NBA, NFL
from .nhl import NHL

LEAGUES = [NHL(), NBA(), NFL()]
BY_KEY = {lg.key: lg for lg in LEAGUES}
