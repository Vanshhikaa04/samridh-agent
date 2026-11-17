"""HUNT_ONLY in .env restricts the whole agent to some sources, e.g.  HUNT_ONLY=yc,hn_hiring,hn_jobs   (empty = everything)."""
from .config import env

JOB_SOURCE = {"hn_hiring": "hn"}  # the `source` value stored on jobs, where it differs from the source's name


def only() -> list[str]:
    return [x.strip() for x in env("HUNT_ONLY", "").split(",") if x.strip()]


def job_sources() -> list[str]:
    return [JOB_SOURCE.get(x, x) for x in only()]
