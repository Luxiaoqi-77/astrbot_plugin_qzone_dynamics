"""Persistent, fail-closed weekly slots for opt-in plain-text auto publishing."""

from __future__ import annotations

import json
import os
import random
import threading
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
SLOT_COUNT = 5
GRACE = timedelta(minutes=10)
MIN_SLOT_GAP = timedelta(hours=18)


def week_monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def next_full_week(now: datetime) -> date:
    """Return the Monday after the current week; never backfill a partial week."""
    return week_monday(now.astimezone(SHANGHAI).date()) + timedelta(days=7)


def make_week_plan(monday: date, *, rng: random.Random | None = None) -> dict[str, object]:
    """Make five random slots on distinct weekdays between 09:00 and 22:00."""
    if monday.weekday() != 0:
        raise ValueError("week plan must start on Monday")
    chooser = rng or random.SystemRandom()
    selected = None
    for _ in range(2000):
        weekdays = sorted(chooser.sample(range(7), SLOT_COUNT))
        minutes = [chooser.randrange(9 * 60, 22 * 60) for _ in weekdays]
        moments = [datetime.combine(monday + timedelta(days=day), time(value // 60, value % 60), SHANGHAI)
                   for day, value in zip(weekdays, minutes)]
        if all(right - left >= MIN_SLOT_GAP for left, right in zip(moments, moments[1:])):
            selected = moments
            break
    if selected is None:
        # A deterministic valid fallback protects against a pathological RNG.
        selected = [
            datetime.combine(monday + timedelta(days=day), time(hour, minute), SHANGHAI)
            for day, hour, minute in ((0, 9, 0), (1, 21, 59), (3, 16, 0), (5, 10, 0), (6, 21, 59))
        ]
    slots = []
    for local in selected:
        slots.append({"id": uuid.uuid4().hex, "at": local.isoformat(), "status": "pending"})
    return {"schema_version": 1, "week_start": monday.isoformat(), "slots": slots}


class WeeklyScheduleStore:
    """Atomic persistent slot state. A claimed slot is never claimed twice."""

    def __init__(self, path: Path, *, rng: random.Random | None = None):
        self.path = Path(path)
        self.rng = rng
        self.owner_id = uuid.uuid4().hex
        self.lock = threading.RLock()

    def _read(self) -> dict[str, object] | None:
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("weekly publish state is unreadable") from exc
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or not isinstance(value.get("slots"), list)
                or len(value["slots"]) != SLOT_COUNT):
            raise ValueError("weekly publish state has an invalid shape")
        for slot in value["slots"]:
            if (not isinstance(slot, dict) or not isinstance(slot.get("id"), str)
                    or slot.get("status") not in {"pending", "running", "consumed"}):
                raise ValueError("weekly publish state has an invalid slot")
            try:
                datetime.fromisoformat(str(slot["at"]))
            except (KeyError, ValueError) as exc:
                raise ValueError("weekly publish state has an invalid slot time") from exc
        return value

    def _write(self, value: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def ensure_plan(self, now: datetime) -> dict[str, object]:
        with self.lock:
            current = self._read()
            current_week = week_monday(now.astimezone(SHANGHAI).date())
            if current is not None:
                try:
                    saved_week = date.fromisoformat(str(current["week_start"]))
                except (KeyError, ValueError) as exc:
                    raise ValueError("weekly publish state has an invalid week") from exc
                if saved_week >= current_week:
                    return current
            plan = make_week_plan(next_full_week(now), rng=self.rng)
            self._write(plan)
            return plan

    def claim_due(self, now: datetime, *, grace: timedelta = GRACE) -> dict[str, object] | None:
        """Consume missed slots, recover interrupted claims, or durably claim one due slot."""
        with self.lock:
            plan = self.ensure_plan(now)
            current = now.astimezone(SHANGHAI)
            changed = False
            due = None
            for slot in plan["slots"]:
                if slot["status"] == "running":
                    if slot.get("owner") == self.owner_id:
                        continue
                    slot["status"] = "consumed"
                    slot["result"] = "interrupted_unknown"
                    slot.pop("owner", None)
                    changed = True
                    continue
                if slot["status"] != "pending":
                    continue
                scheduled = datetime.fromisoformat(slot["at"]).astimezone(SHANGHAI)
                if current > scheduled + grace:
                    slot["status"] = "consumed"
                    slot["result"] = "missed"
                    changed = True
                elif scheduled <= current <= scheduled + grace:
                    slot["status"] = "running"
                    slot["owner"] = self.owner_id
                    changed = True
                    due = dict(slot)
                    break
            if changed:
                self._write(plan)
            return due

    def finish(self, slot_id: str, result: str, *, draft_id: str = "", remote_tid: str = "") -> None:
        with self.lock:
            plan = self._read()
            if plan is None:
                raise ValueError("weekly publish state is missing")
            slot = next((item for item in plan["slots"] if item["id"] == slot_id), None)
            if slot is None or slot["status"] != "running":
                raise ValueError("slot is not in a running state")
            slot["status"] = "consumed"
            slot.pop("owner", None)
            slot["result"] = str(result)[:80]
            if draft_id:
                slot["draft_id"] = draft_id
            if remote_tid:
                slot["remote_tid"] = remote_tid
            self._write(plan)

