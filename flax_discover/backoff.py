"""In-memory exponential back-off for re-matching unknown-family MACs.

match_family is a pure regex; re-running it against an unchanged product_name
only churns the DB. Back off retries on unknowns. State is in-memory: a process
restart clears it (one retry of every unknown, then back-off resumes), which is
acceptable and a reasonable retry point.

Reset to "retry now" on the two events that can flip unknown -> known:
  - reset(mac)   : the MAC's product_name changed (new input)
  - clear_all()  : the family-map gained a regex (reload re-evaluates unknowns)
forget(mac) drops a MAC once its family latches.
"""


class MatchBackoff:
    def __init__(self, base_secs: float, max_secs: float):
        self._base = base_secs
        self._max = max_secs
        self._state: dict[str, dict] = {}  # mac -> {"next_after", "interval"}

    def due(self, mac: str, now: float) -> bool:
        st = self._state.get(mac)
        if st is None:
            return True
        return now >= st["next_after"]

    def record_miss(self, mac: str, now: float) -> None:
        st = self._state.get(mac)
        interval = self._base if st is None else min(st["interval"] * 2, self._max)
        self._state[mac] = {"next_after": now + interval, "interval": interval}

    def reset(self, mac: str) -> None:
        self._state.pop(mac, None)

    def clear_all(self) -> None:
        self._state.clear()

    def forget(self, mac: str) -> None:
        self._state.pop(mac, None)
