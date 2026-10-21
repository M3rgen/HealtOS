"""The nightly identity reconciler: ``identity-backfill --incremental`` as a scheduler job.

It runs over every archived data type that had a full ``identity-backfill`` (others are
skipped and say so), decides new source chains, records refuted ones and links the
duplicate rows written since its checkpoint (app/services/identity_backfill.py). What it leaves
to the owner (chains to review, ambiguous sources, pairs nothing decided) goes to the log and to
``health-mirror status``. The result
of each data type goes to the log and to ``identity_reconcile_state``, which
``health-mirror status`` shows. Another run of identity-backfill holding the single-run lock
makes the job skip (logged); a data type whose lock stays busy is retried on the next night.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.logging import event
from app.services.identity_backfill import identity_backfill
from app.services.source_chains import BackfillRunning

logger = logging.getLogger(__name__)


class IdentityReconciler:
    def __init__(self, sessions: async_sessionmaker) -> None:
        self.sessions = sessions

    async def run(self) -> dict | None:
        event(logger, "IDENTITY_RECONCILE_STARTED")
        try:
            results = await identity_backfill(self.sessions, incremental=True)
        except BackfillRunning:
            event(logger, "IDENTITY_RECONCILE_SKIPPED", reason="identity-backfill is running")
            return None
        except Exception as exc:
            logger.exception(
                "Identity reconciliation failed",
                extra={"event": "IDENTITY_RECONCILE_FAILED", "error_type": type(exc).__name__},
            )
            return None
        summary = {name: stats.summary() for name, stats in results.items()}
        for name, values in summary.items():
            event(logger, "IDENTITY_RECONCILE_DATA_TYPE", data_type=name, **values)
        event(
            logger,
            "IDENTITY_RECONCILE_COMPLETED",
            data_types=len(summary),
            superseded=sum(values["superseded"] for values in summary.values()),
            chains_decided=sum(values["chains_decided"] for values in summary.values()),
            ambiguous=sum(values["ambiguous"] for values in summary.values()),
            review_suggested=sum(values["review_suggested"] for values in summary.values()),
            kept_apart_undecided=sum(values["kept_apart_undecided"] for values in summary.values()),
            skipped=sum(1 for values in summary.values() if values["skipped"]),
        )
        return summary
