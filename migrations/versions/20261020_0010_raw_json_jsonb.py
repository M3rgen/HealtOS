"""Store the raw JSON payload columns as `jsonb` instead of `json` on PostgreSQL.

Affects `raw_health_data.raw_json`, `webhook_events.raw_json` and
`webhook_notifications.raw_json`. Google's bytes were never in these columns: the app
stores what `json.dumps` makes of the parsed response (the signed webhook body is kept
byte for byte in `webhook_events.raw_body`, which this migration does not touch), and
`payload_hash` is a SHA-256 over a canonical re-serialization of the parsed value, not
over the column text. jsonb drops whitespace, reorders keys and keeps only the last of
duplicate keys; none of that changes the parsed value the app reads, and `json.dumps`
cannot emit duplicate keys. What jsonb could change is a few number spellings (`-0.0`
becomes `0.0`, a float written as `1e+16` comes back as the integer
`10000000000000000`), and it rejects the `\\u0000` escape outright.

So before converting, every row is read both ways (`::text` and `::jsonb::text`) and the
canonical serializations are compared. If any row would read back differently, the
migration raises and the whole transaction rolls back: no row is rewritten and the
columns stay `json`. A `\\u0000` row makes the cast itself fail, with the same outcome.

`ALTER COLUMN ... TYPE` rewrites each table under an ACCESS EXCLUSIVE lock, so this
runs while the app is down (the container migrates before it serves). For a
single-user archive of a few hundred thousand rows that is seconds to a few minutes;
see docs/RUNBOOK.md. No index is added: no query filters on raw_json today.

downgrade() casts back with `::json`, which keeps every value (the text is jsonb's
normalized spelling, not the pre-upgrade spelling). SQLite keeps its JSON (text)
columns, so nothing changes there.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "20261020_0010"
down_revision = "20261020_0009"
branch_labels = None
depends_on = None

# (table, primary key column) for every raw payload column.
TABLES = (
    ("raw_health_data", "id"),
    ("webhook_events", "payload_hash"),
    ("webhook_notifications", "element_hash"),
)
REPORTED_KEYS = 5


def _canonical(text: str) -> str:
    # Mirrors app.parsers.common.canonical_json (migrations do not import app code).
    return json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _rows_jsonb_would_change(connection: sa.Connection, table: str, key: str) -> list[str]:
    # Per-statement options: Connection.execution_options() would also make the ALTERs
    # below run through a server-side cursor.
    query = sa.text(f"SELECT {key}::text, raw_json::text, raw_json::jsonb::text FROM {table}")
    rows = connection.execute(query.execution_options(stream_results=True, yield_per=2_000))
    changed = []
    for row_key, as_json, as_jsonb in rows:
        if _canonical(as_json) != _canonical(as_jsonb):
            changed.append(row_key)
    return changed


def upgrade() -> None:
    context = op.get_context()
    if context.dialect.name != "postgresql":
        return
    if not context.as_sql:
        connection = op.get_bind()
        problems = {}
        for table, key in TABLES:
            if changed := _rows_jsonb_would_change(connection, table, key):
                problems[table] = changed
        if problems:
            summary = "; ".join(
                f"{table}: {len(keys)} row(s), e.g. {', '.join(keys[:REPORTED_KEYS])}"
                for table, keys in problems.items()
            )
            raise RuntimeError(
                "raw_json -> jsonb would change how some stored payloads read back, so "
                f"nothing was converted (columns stay json): {summary}"
            )
    for table, _ in TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN raw_json TYPE jsonb USING raw_json::jsonb")


def downgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    for table, _ in TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN raw_json TYPE json USING raw_json::json")
