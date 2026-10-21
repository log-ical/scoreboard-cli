"""Running the game clock locally between (infrequent) API updates."""


def clock_target(game, view_t):
    """Best estimate of the game clock at real time view_t."""
    if game.clock_secs is None:
        return None
    if game.state != "live" or not game.clock_running:
        return game.clock_secs
    run = max(0.0, view_t - game.clock_anchor)
    if game.clock_cap is not None:
        run = min(run, game.clock_cap)
    return max(0.0, game.clock_secs - run)


class ClockSmoother:
    """Absorbs small corrections so the display ticks steadily.

    Within 5 s of the target the clock runs at up to 2x speed to catch up,
    or pauses to let the real clock catch up. Larger errors (a stoppage the
    data reported late, a delay change) snap straight to the target.
    """

    def __init__(self):
        self.shown = None
        self.segment = None
        self.t = 0.0

    def update(self, game, target, now):
        if target is None or game.state != "live" or not game.clock_running:
            self.shown = None
            return target
        segment = (game.period, game.break_label)
        dt = now - self.t
        if self.shown is None or segment != self.segment:
            self.shown = target
        else:
            expected = self.shown - dt
            err = expected - target
            if abs(err) > 5:
                self.shown = target
            else:
                self.shown = expected - max(-dt, min(dt, err))
        self.segment, self.t = segment, now
        return max(0.0, self.shown)
