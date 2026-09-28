from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.core.schemas import BronzeLedgerEntry, EntityType, SilverDailySummary, User


@pytest.mark.parametrize(
    "user_id",
    ["athlete-1", "a", "A0", "user_42", "a" * 64],
)
def test_valid_user_ids_are_accepted(user_id: str) -> None:
    user = User(user_id=user_id, created_at=datetime(2026, 9, 28, tzinfo=timezone.utc))
    assert user.user_id == user_id


@pytest.mark.parametrize(
    "user_id",
    [
        "",
        "-leading-hyphen",
        "_leading-underscore",
        "../escape",
        "has/slash",
        "has space",
        "user:id",
        "a" * 65,
    ],
)
def test_unsafe_user_ids_are_rejected(user_id: str) -> None:
    with pytest.raises(ValidationError):
        User(user_id=user_id, created_at=datetime(2026, 9, 28, tzinfo=timezone.utc))


def test_unsafe_user_id_rejected_on_bronze_and_silver_records() -> None:
    with pytest.raises(ValidationError):
        BronzeLedgerEntry(
            user_id="../escape",
            vendor="garmin",
            entity_type=EntityType.ACTIVITY,
            source_identifier="activity-1",
            content_hash="hash",
            fetch_timestamp=datetime(2026, 9, 28, tzinfo=timezone.utc),
            file_path="/tmp/activity-1.zip",
        )

    with pytest.raises(ValidationError):
        SilverDailySummary(
            user_id="has/slash",
            updated_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
            calendar_date="2026-09-28",
        )
