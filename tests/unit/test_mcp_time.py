from datetime import UTC, datetime, timedelta

import pytest
from app.core.errors import ApplicationError
from app.modules.mcp.tools import resolve_occurred_time, resolve_reminder_time


def test_occurred_time_defaults_to_server_reference() -> None:
    reference = datetime(2026, 8, 13, 12, tzinfo=UTC)
    resolved, source = resolve_occurred_time(None, None, "Asia/Shanghai", reference)
    assert resolved == reference
    assert source == "server"


def test_occurred_local_time_uses_named_timezone() -> None:
    resolved, source = resolve_occurred_time(None, "2026-08-13T20:00:00", "Asia/Shanghai")
    assert resolved.astimezone(UTC) == datetime(2026, 8, 13, 12, tzinfo=UTC)
    assert source == "local"


def test_occurred_absolute_time_requires_offset() -> None:
    with pytest.raises(ApplicationError) as caught:
        resolve_occurred_time("2026-08-13T20:00:00", None, "Asia/Shanghai")
    assert caught.value.code == "INVALID_ARGUMENTS"


def test_occurred_time_rejects_two_inputs() -> None:
    with pytest.raises(ApplicationError) as caught:
        resolve_occurred_time(
            "2026-08-13T20:00:00+08:00",
            "2026-08-13T20:00:00",
            "Asia/Shanghai",
        )
    assert caught.value.code == "OCCURRED_TIME_INPUT_INVALID"


def test_resolve_absolute_reminder_keeps_instant() -> None:
    resolved, source = resolve_reminder_time(
        remind_at="2026-08-14T09:00:00+08:00",
        local_date_time=None,
        after_minutes=None,
        timezone_name="Asia/Shanghai",
    )
    assert resolved.astimezone(UTC) == datetime(2026, 8, 14, 1, tzinfo=UTC)
    assert source == "absolute"


def test_resolve_local_reminder_uses_iana_timezone() -> None:
    resolved, source = resolve_reminder_time(
        remind_at=None,
        local_date_time="2026-08-14T09:00:00",
        after_minutes=None,
        timezone_name="Asia/Shanghai",
    )
    assert resolved.astimezone(UTC) == datetime(2026, 8, 14, 1, tzinfo=UTC)
    assert source == "local"


def test_resolve_relative_reminder_uses_server_reference_time() -> None:
    reference = datetime(2026, 8, 13, 12, tzinfo=UTC)
    resolved, source = resolve_reminder_time(
        remind_at=None,
        local_date_time=None,
        after_minutes=30,
        timezone_name="Asia/Shanghai",
        reference_time=reference,
    )
    assert resolved == reference + timedelta(minutes=30)
    assert source == "relative"


@pytest.mark.parametrize(
    ("local_time", "code"),
    [
        ("2026-03-08T02:30:00", "LOCAL_TIME_NONEXISTENT"),
        ("2026-11-01T01:30:00", "LOCAL_TIME_AMBIGUOUS"),
    ],
)
def test_resolve_local_reminder_rejects_dst_ambiguity(local_time: str, code: str) -> None:
    with pytest.raises(ApplicationError) as caught:
        resolve_reminder_time(
            remind_at=None,
            local_date_time=local_time,
            after_minutes=None,
            timezone_name="America/New_York",
        )
    assert caught.value.code == code


@pytest.mark.parametrize(
    "values",
    [
        (None, None, None),
        ("2026-08-14T09:00:00+08:00", "2026-08-14T09:00:00", None),
    ],
)
def test_resolve_reminder_requires_exactly_one_time_input(
    values: tuple[str | None, str | None, int | None],
) -> None:
    with pytest.raises(ApplicationError) as caught:
        resolve_reminder_time(
            remind_at=values[0],
            local_date_time=values[1],
            after_minutes=values[2],
            timezone_name="Asia/Shanghai",
        )
    assert caught.value.code == "REMINDER_TIME_INPUT_INVALID"


def test_resolve_absolute_reminder_requires_offset() -> None:
    with pytest.raises(ApplicationError) as caught:
        resolve_reminder_time(
            remind_at="2026-08-14T09:00:00",
            local_date_time=None,
            after_minutes=None,
            timezone_name="Asia/Shanghai",
        )
    assert caught.value.code == "INVALID_ARGUMENTS"
