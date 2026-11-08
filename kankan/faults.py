"""Test-only fault injection: ``point(name)`` crashes when ``KANKAN_FAULT`` names it."""

import os


class SimulatedCrash(BaseException):
    """Raised at a fault point; BaseException so ordinary handlers do not swallow it."""


def point(name: str) -> None:
    wanted = os.environ.get("KANKAN_FAULT")
    if wanted and name in wanted.split(","):
        raise SimulatedCrash(name)
