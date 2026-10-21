# scoreboard

Live NHL, NBA and NFL scores in the terminal. Python 3 standard library only.

    scoreboard              # opens the league you used last
    scoreboard nba          # or: nhl | nfl
    scoreboard --delay 45   # stream delay in seconds (saved)
    scoreboard --color 256  # force a color mode: truecolor | 256 | 16
    nhl-live                # old name, same as `scoreboard nhl`

## Keys

| Screen    | Keys |
|-----------|------|
| Game list | `←/→` or `1/2/3` league · `↑/↓` move · `Enter` watch · `a` live/all · `o` options · `r` refresh · `q` quit |
| Game view | `p` expand/collapse the play list (`↑/↓` scroll, `[`/`]` page) · `+`/`-` adjust delay · `o` options · `b`/`Esc` back · `q` quit |
| Options   | `↑/↓` choose · `←/→` change · `o`/`Esc` close |

The game view puts the away team on top and the home team on the bottom, each centered in its half,
with the period and clock in the middle. Before a game starts the big clock counts down to the scheduled start
(`1:04:59`, or `2D 03:15` for days, hours and minutes), and the game list shows `in 2h 05m` for games starting within a day. With **Logo layout** set to horizontal, the teams sit side by
side (away left, home right) with the clock between them; windows too narrow for that use the vertical layout.

Press `p` to expand the last-play line into a scrollable list of earlier plays, newest first (in the horizontal
layout it fills the space between the teams, which stay put): every goal and
penalty for the NHL; for the NBA and NFL, the newest 25-50 plays, with older pages loaded as you page past
them (`]`). ESPN plays are only downloaded while the list is open. With a stream delay, the list stops at the play the delayed view is on.

Logos are fitted into a box that is square on screen, using the terminal's cell size in pixels when it
reports one, so tall and wide logos come out the same size. A red ● on a league tab means it has live games.

## Options (saved to `~/.config/scoreboard/settings.json`)

- **Stream delay** (off to 30 min): the game view lags real time to match a delayed TV or stream.
- **Hide scores in game list**: the list is always real time, so this makes it spoiler-free.
- **Flash on scores** / **Beep on scores**: NHL goals and NFL scores, on the delayed view.
- **Show last play**: NHL last goal and assists; NBA and NFL latest play.
- **Show team stats**: NHL shots on goal; NBA timeouts and team fouls; NFL timeouts.
- **Show game situation**: NHL power play and empty net; NBA bonus; NFL possession, red zone, down and distance.
- **Show season records**: next to each team name (NHL W-L-OTL from the standings). Hidden while a delayed view
  is behind a game that has already ended, since the record would give the result away.
- **Logo size**, **Logo layout** (vertical or horizontal), **Color mode**, **24-hour start times**

## Data and the clock

| League | Source | Updates |
|--------|--------|---------|
| NHL | `api-web.nhle.com` | Cached about 19 s. Polled right after each cache expiry, with the `Age` header giving each update's real time. The API says whether the clock is running. |
| NBA, NFL | ESPN (`site.api.espn.com` for lists, `sports.core.api.espn.com` while watching) | About 3 KB per poll, every 3 s (NBA) or 5 s (NFL). ESPN doesn't say whether the clock is running, so it's inferred from the last play's type (timeout, foul, incomplete pass, ...) and timed from that play's timestamp. |

Between updates the clock runs locally, absorbing small corrections smoothly. Stoppages show up only
when the data reports them, so the clock can briefly run past a whistle and then jump back.
Logos come from ESPN's CDN and are cached in `~/.cache/scoreboard/logos/`.

## Layout

    bin/scoreboard          launcher (symlinked to /usr/local/bin/scoreboard)
    scoreboard/
      app.py                argument parsing, screen loop
      screens.py            game list and game view
      model.py              league-neutral Game/Team model, stale-update filter
      clock.py              local clock running and smoothing
      settings.py           settings file and options menu
      leagues/              one adapter per data source
        nhl.py              NHL web API
        espn.py             NBA and NFL (ESPN), clock-stop rules per sport
      logos.py png.py       logo download and cache; PNG decoder
      render.py term.py     block font and half-block logos; terminal I/O and colors
      net.py                HTTP and cache-age helpers
    tests/                  unittest suite with captured API responses in fixtures/

Run the tests with `python3 -m unittest discover -s tests -t .` from this folder.

To add a league, write an adapter with `list_games()`, `open(game)` (returns a poller whose `poll()` gives
`(Game, data_time, seconds_until_next_poll)`) and `score_alert(points)`, then register it in `leagues/__init__.py`.
