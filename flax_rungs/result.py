"""Result of one verification rung."""
from dataclasses import dataclass, field

VERDICTS = ("pass", "fail", "skip")


@dataclass
class RungResult:
    verdict: str                      # "pass" | "fail" | "skip"
    detail: dict
    artifacts: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if self.verdict not in VERDICTS:
            raise ValueError(f"bad verdict {self.verdict!r}")
