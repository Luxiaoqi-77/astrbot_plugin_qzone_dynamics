"""2026-10-19：QQ空间改为"每天抽签"（普通日子 50%，重要日子 100%）的纯逻辑测试。"""

from __future__ import annotations

import json
import random
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot_plugin_qzone_dynamics.weekly_schedule import (
    WeeklyScheduleStore,
    make_week_plan,
    week_monday,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
MONDAY = date(2026, 10, 19)


def _local(slot: dict) -> datetime:
    return datetime.fromisoformat(slot["at"]).astimezone(SHANGHAI)


def test_zero_probability_skips_every_normal_day():
    plan = make_week_plan(MONDAY, rng=random.Random(1), daily_probability=0.0)
    assert plan["slots"] == []
    assert plan["daily_probability"] == 0.0


def test_full_probability_keeps_one_slot_per_day():
    plan = make_week_plan(MONDAY, rng=random.Random(1), daily_probability=1.0)
    days = [_local(slot).date() for slot in plan["slots"]]
    assert days == [MONDAY.replace(day=d) for d in range(19, 26)]
    for slot in plan["slots"]:
        minute = _local(slot).hour * 60 + _local(slot).minute
        assert 9 * 60 <= minute < 22 * 60


def test_special_day_always_has_slot_even_at_zero_probability():
    special = MONDAY.replace(day=21)
    plan = make_week_plan(
        MONDAY,
        rng=random.Random(1),
        special_dates=[special],
        special_times={special.isoformat(): "00:05"},
        daily_probability=0.0,
    )
    assert len(plan["slots"]) == 1
    assert _local(plan["slots"][0]).date() == special
    assert _local(plan["slots"][0]).strftime("%H:%M") == "00:05"


def test_legacy_daily_plan_is_migrated_once(tmp_path: Path):
    path = tmp_path / "weekly_publish_state.json"
    legacy = {
        "schema_version": 1,
        "week_start": MONDAY.isoformat(),
        "slots": [
            {
                "id": f"s{index}",
                "at": datetime.combine(
                    MONDAY.replace(day=19 + index),
                    datetime.min.time().replace(hour=12),
                    SHANGHAI,
                ).isoformat(),
                "status": "pending",
            }
            for index in range(7)
        ],
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")

    store = WeeklyScheduleStore(path, daily_probability=0.0)
    now = datetime(2026, 10, 19, 8, 0, tzinfo=SHANGHAI)
    plan = store.ensure_plan(now)
    assert plan["daily_probability"] == 0.0
    assert [slot["status"] for slot in plan["slots"]] == ["consumed"] * 7
    assert {slot["result"] for slot in plan["slots"]} == {"probability_skip"}

    # 第二次调用不再重抽（标记已写入）
    again = store.ensure_plan(now)
    assert [slot["status"] for slot in again["slots"]] == ["consumed"] * 7


def test_legacy_plan_keeps_special_day(tmp_path: Path):
    path = tmp_path / "weekly_publish_state.json"
    legacy = {
        "schema_version": 1,
        "week_start": MONDAY.isoformat(),
        "slots": [
            {
                "id": f"s{index}",
                "at": datetime.combine(
                    MONDAY.replace(day=19 + index),
                    datetime.min.time().replace(hour=15),
                    SHANGHAI,
                ).isoformat(),
                "status": "pending",
            }
            for index in range(7)
        ],
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")

    special = MONDAY.replace(day=21)
    store = WeeklyScheduleStore(path, daily_probability=0.0)
    now = datetime(2026, 10, 19, 8, 0, tzinfo=SHANGHAI)
    plan = store.ensure_plan(
        now,
        special_dates=[special],
        special_times={special.isoformat(): "00:00"},
    )
    pending = [slot for slot in plan["slots"] if slot["status"] == "pending"]
    assert len(pending) == 1
    assert _local(pending[0]).date() == special
    assert _local(pending[0]).strftime("%H:%M") == "00:00"


def test_empty_week_plan_is_valid(tmp_path: Path):
    path = tmp_path / "weekly_publish_state.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "week_start": MONDAY.isoformat(),
                "daily_probability": 0.5,
                "slots": [],
            }
        ),
        encoding="utf-8",
    )
    store = WeeklyScheduleStore(path, daily_probability=0.5)
    now = datetime(2026, 10, 20, 12, 0, tzinfo=SHANGHAI)
    plan = store.ensure_plan(now)
    assert plan["slots"] == []
    assert store.claim_due(now) is None


def test_week_monday_helper():
    assert week_monday(date(2026, 10, 19)) == date(2026, 10, 19)
    assert week_monday(date(2026, 10, 25)) == date(2026, 10, 19)
