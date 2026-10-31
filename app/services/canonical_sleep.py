"""Shared sleep estimate with independent evidence for timed stages and supplied totals."""

from app.services.dashboard import sleep_estimate
from app.services.sleep_timeline import sleep_timeline


def sleep_evidence(sessions):
    return sleep_estimate(sessions), sleep_timeline(sessions)
