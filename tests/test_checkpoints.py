from datetime import UTC, datetime, timedelta

from app.sync.checkpoints import incremental_start


def test_safety_window_overlaps_checkpoint():
    checkpoint = datetime(2026, 8, 18, 12, tzinfo=UTC)
    now = checkpoint + timedelta(hours=1)
    assert incremental_start(checkpoint, timedelta(hours=3), now) == checkpoint - timedelta(hours=3)


def test_no_checkpoint_starts_one_window_ago():
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    assert incremental_start(None, timedelta(hours=1), now) == now - timedelta(hours=1)
