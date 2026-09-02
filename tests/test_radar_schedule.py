from datetime import UTC, datetime, timedelta, timezone

from api.main import next_radar_sync_at

SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


def test_next_radar_sync_is_today_at_eight_before_schedule():
    now = datetime(2026, 9, 2, 7, 59, tzinfo=SHANGHAI)
    assert next_radar_sync_at(now) == datetime(2026, 9, 2, 8, 0, tzinfo=SHANGHAI)


def test_next_radar_sync_moves_to_tomorrow_at_or_after_schedule():
    now = datetime(2026, 9, 2, 8, 0, tzinfo=SHANGHAI)
    assert next_radar_sync_at(now) == datetime(2026, 9, 3, 8, 0, tzinfo=SHANGHAI)


def test_next_radar_sync_converts_input_to_beijing_time():
    now = datetime(2026, 9, 1, 23, 30, tzinfo=UTC)
    assert next_radar_sync_at(now) == datetime(2026, 9, 2, 8, 0, tzinfo=SHANGHAI)
