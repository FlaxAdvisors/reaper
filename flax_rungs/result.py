"""Result of one verification rung.

`unmeasured`: the rung could not measure because its target did not answer (ssh
connect timeout/refused/no route, no IPMI session, Redfish transport error, a host
no longer in the live image ...): nothing was judged. Its detail headline says why.
It is never a pass (every gate treats it as not passed) and never a fault (the UI
draws it grey, "not measured"). A real negative result is always `fail`."""
from dataclasses import dataclass, field

VERDICTS = ("pass", "fail", "skip", "unmeasured")
UNMEASURED = "unmeasured"


@dataclass
class RungResult:
    verdict: str                      # "pass" | "fail" | "skip" | "unmeasured"
    detail: dict
    artifacts: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if self.verdict not in VERDICTS:
            raise ValueError(f"bad verdict {self.verdict!r}")


def unmeasured(why: str, **detail) -> RungResult:
    """A rung whose target did not answer: `why` is its headline (and reason)."""
    return RungResult(UNMEASURED, {"headline": why, "reason": why, **detail})


def not_passed(rung: str, verdict: str | None) -> str:
    """A gate's reason for a rung that is not `pass` ("not measured: <rung>" for an
    unmeasured one, else "<rung> <verdict or 'not run'>")."""
    if verdict == UNMEASURED:
        return f"not measured: {rung}"
    return f"{rung} {verdict or 'not run'}"
