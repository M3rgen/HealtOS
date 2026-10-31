"""Synthetic selected sleep evidence, including real archived correction chains."""

from datetime import timedelta

from derived_records import T0, sleep, store

BAD_SLEEP_NUMBERS = (
    "NaN",
    "sNaN",
    "Infinity",
    "-Infinity",
    "1e2000000000",
    "-1e2000000000",
    "1e-2000000000",
    "-1e-2000000000",
    "1e99999999999999999999",
    "1e-99999999999999999999",
    "1e309",
    "-1e309",
    "1e-400",
    "-1e-400",
)
# Evidence the asleep reading uses; the subtraction never reads the other stage totals.
SLEEP_FIELDS = (
    "summary",
    "duration",
    "summary_awake",
    "summary_restless",
    "stage_awake",
    "stage_wake",
    "stage_restless",
    "wrapped_durationSeconds",
)
UNUSED_SLEEP_FIELDS = ("summary_deep", "stage_light")


def evidence(field, value, start=T0):
    if field == "summary":
        return {
            "summary": {"minutesAsleep": value},
            "stages": [{"type": "AWAKE", "duration": "0s"}],
        }
    if field == "duration":
        return {"duration": value + "s", "stages": [{"type": "AWAKE", "duration": "0s"}]}
    if field.startswith("summary_"):
        return {"summary": {"stagesSummary": [{"type": field[8:], "minutes": value}]}}
    entry = {
        "type": field.removeprefix("stage_"),
        "duration": value + "s",
        "startTime": start.isoformat(),
        "endTime": (start + timedelta(hours=8)).isoformat(),
    }
    if field == "wrapped_durationSeconds":
        entry["type"] = "AWAKE"
        entry["durationSeconds"] = value
        del entry["duration"]
        return {"sleepStages": {"data": [entry]}}
    return {"stages": [entry]}


async def corrected_sleep(sessions, field, value, start=T0):
    def night(**payload):
        return sleep(field, start=start, end=start + timedelta(hours=8), **payload)

    await store(
        sessions,
        "sleep",
        night(
            duration="28800s",
            summary={"minutesAsleep": 420},
            stages=[{"type": "AWAKE", "duration": "1800s"}],
        ),
    )
    return await store(sessions, "sleep", night(**evidence(field, value, start)))
