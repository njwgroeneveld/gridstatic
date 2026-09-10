import time

_COOLDOWN_SECONDS = 60 * 60  # 1 hour


class OutsideGridThrottle:
    """Limits repeat 'price outside grid' alerts to once per cooldown window
    per side ("top"/"bottom"), while still alerting immediately the next time
    the price breaches after having returned inside the grid.
    """

    def __init__(self) -> None:
        self._active = {"top": False, "bottom": False}
        self._last_alert_s: dict[str, float] = {"top": 0.0, "bottom": 0.0}

    def should_alert(self, side: str) -> bool:
        now = time.time()
        fire = not self._active[side] or (now - self._last_alert_s[side]) >= _COOLDOWN_SECONDS
        if fire:
            self._last_alert_s[side] = now
        self._active[side] = True
        return fire

    def clear(self, side: str) -> None:
        self._active[side] = False
