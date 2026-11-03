"""Records PostgreSQL jsonb refuses, and a SQLite stand-in for that refusal."""

from datetime import UTC, datetime, timedelta

from raw_response_records import steps
from sqlalchemy import text

START = datetime(2026, 8, 17, tzinfo=UTC)
END = START + timedelta(days=1)
KINDS = ("nul", "nul_key", "surrogate", "surrogate_key", "overflow")
BAD_NAME = "users/me/dataTypes/steps/dataPoints/synthetic-bad"
NEXT_NAME = "users/me/dataTypes/steps/dataPoints/synthetic-next"


def bad_record(kind="nul", count=9):
    record = steps(count, name=BAD_NAME)
    record["extra"] = {
        "nul": {"value": "private-marker\x00"},
        "nul_key": {"private-marker\x00": 1},
        "surrogate": {"value": "private-marker\ud800"},
        "surrogate_key": {"private-marker\udfff": 1},
        "overflow": {"value": float("inf")},
    }[kind]
    return record


def wire(raw: bytes) -> bytes:
    # json.dumps writes inf as Infinity; Google would send an overflowing number.
    return raw.replace(b"Infinity", b"1e400")


# SQLite stores JSON as ASCII-escaped text; PostgreSQL jsonb refuses these values
# ("unsupported Unicode escape sequence", lone surrogates, NaN/Infinity). Without this, SQLite
# would accept what blocks a real archive.
_REFUSED = ("\\u0000", "Infinity", "NaN", *(f"\\ud{digit}" for digit in "89abcdef"))
_WHEN = " OR ".join(f"instr(lower(NEW.raw_json), lower('{token}')) > 0" for token in _REFUSED)


async def refuse_like_jsonb(sessions) -> None:
    async with sessions.begin() as session:
        if session.get_bind().dialect.name == "postgresql":
            return  # jsonb itself refuses
        for operation in ("INSERT", "UPDATE"):
            await session.execute(
                text(
                    f"CREATE TRIGGER jsonb_like_{operation.lower()} BEFORE {operation} "
                    f"ON raw_health_data WHEN {_WHEN} BEGIN "
                    "SELECT RAISE(ABORT, 'unsupported Unicode escape sequence'); END"
                )
            )
