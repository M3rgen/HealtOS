"""Table names and domain constants for raw-record identities."""

IDENTITY_TABLE = "raw_health_data_identity"
SUPERSESSION_TABLE = "raw_health_data_supersessions"
CHAIN_TABLE = "raw_source_chains"
CHAIN_EVENT_TABLE = "raw_source_chain_events"
PAIR_TABLE = "raw_source_pairs"
DECISION_TABLE = "raw_source_decisions"
DEVICE_TABLE = "raw_source_devices"
AGREEMENT_TABLE = "raw_source_agreements"
SIGHTING_TABLE = "raw_source_sightings"
RECONCILE_TABLE = "identity_reconcile_state"
DEFERRAL_TABLE = "identity_deferred_deletions"
# The append-only tables of this revision.
IDENTITY_TABLES = (
    IDENTITY_TABLE,
    SUPERSESSION_TABLE,
    CHAIN_TABLE,
    CHAIN_EVENT_TABLE,
    PAIR_TABLE,
    DECISION_TABLE,
    DEVICE_TABLE,
    AGREEMENT_TABLE,
)
# Every table whose rows verify protects (the sightings may only widen).
GUARDED_TABLES = (*IDENTITY_TABLES, SIGHTING_TABLE)
# Who decides a chain: a full run of identity-backfill, the incremental reconciler, or the
# owner (identity-decide link, with its decision row).
DECIDERS = ("backfill", "reconciler", "owner")
# A chain whose two forms turned out to be two devices (raw_source_chain_events).
REFUTED = "refuted"
# The owner's decisions (raw_source_decisions): about a chain from -> to, or about a pair.
CHAIN_DECISIONS = ("link", "reject", "confirm")
PAIR_DECISIONS = ("distinct", "not-distinct")
DECISIONS = (*CHAIN_DECISIONS, *PAIR_DECISIONS)
# Why a row was superseded (app.parsers.identity.supersession_reason).
REASONS = (
    "display-name-change",
    "package-suffix-change",
    "source-change",
    "interval-shape-change",
    "growing-interval",
    "duplicate",
    # A row archived under a hollow Google name, held by the row of its own record
    # (app/services/hollow_names.py).
    "hollow-name",
)
MERGED_EVENT = "identity-merged"
