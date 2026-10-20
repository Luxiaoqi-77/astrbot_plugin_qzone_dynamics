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
# 2026-10-16 主人要求：QQ空间改为"每天一条"（原为 5 条/周）。
SLOT_COUNT = 7
GRACE = timedelta(minutes=10)
MIN_SLOT_GAP = timedelta(hours=18)
# 2026-10-19：主人嫌"每天一条太多"→ 改成每天抽签，默认 50%；
# 命中"重要日子"（qzone 渠道）的日子 100% 发。
DEFAULT_DAILY_PROBABILITY = 0.5


def _clamp_probability(value) -> float:
    try:
        probability = float(value)
    except (TypeError, ValueError):
        return DEFAULT_DAILY_PROBABILITY
    return min(1.0, max(0.0, probability))


def week_monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def next_full_week(now: datetime) -> date:
    """Return the Monday after the current week; never backfill a partial week."""
    return week_monday(now.astimezone(SHANGHAI).date()) + timedelta(days=7)


def _normalize_special_time(value) -> int | None:
    """"HH:MM" → 当天分钟数（00:00–23:59）；非法返回 None。"""
    if not isinstance(value, str) or ":" not in value:
        return None
    try:
        hour, minute = (int(part) for part in value.strip().split(":"))
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    # 2026-10-16：不再夹到 09:00–21:59 —— 主人要 "重要日子零点发"。
    # （09:00–21:59 只是**随机槽**的范围；固定时刻按你填的来。）
    return hour * 60 + minute


def make_week_plan(
    monday: date,
    *,
    rng: random.Random | None = None,
    special_dates=None,
    special_times=None,
    daily_probability=DEFAULT_DAILY_PROBABILITY,
) -> dict[str, object]:
    """每天按概率抽签决定要不要发（09:00–21:59 内随机时刻）。

    2026-10-16：主人要求改为"每天一条"（原为每周 5 个随机槽、≥18h 间隔）。
    2026-10-19：主人嫌每天一条太多 → 普通日子按 daily_probability（默认 50%）
    决定当天发不发；命中"重要日子"（qzone 渠道）的日子 **100% 发**，
    可用 special_times 指定当天时刻（不指定就随机）。
    """
    if monday.weekday() != 0:
        raise ValueError("week plan must start on Monday")
    chooser = rng or random.SystemRandom()
    times_map = special_times if isinstance(special_times, dict) else {}
    probability = _clamp_probability(daily_probability)
    specials = {
        day for day in (special_dates or []) if isinstance(day, date)
    }
    slots = []
    for day in range(7):
        target = monday + timedelta(days=day)
        forced = _normalize_special_time(times_map.get(target.isoformat()))
        is_special = target in specials
        if not is_special and chooser.random() >= probability:
            continue
        minute = forced if forced is not None else chooser.randrange(9 * 60, 22 * 60)
        local = datetime.combine(target, time(minute // 60, minute % 60), SHANGHAI)
        slots.append({"id": uuid.uuid4().hex, "at": local.isoformat(), "status": "pending"})
    return {
        "schema_version": 1,
        "week_start": monday.isoformat(),
        "daily_probability": probability,
        "slots": slots,
    }


class WeeklyScheduleStore:
    """Atomic persistent slot state. A claimed slot is never claimed twice."""

    def __init__(
        self,
        path: Path,
        *,
        rng: random.Random | None = None,
        daily_probability=DEFAULT_DAILY_PROBABILITY,
    ):
        self.path = Path(path)
        self.rng = rng
        self.daily_probability = _clamp_probability(daily_probability)
        self.owner_id = uuid.uuid4().hex
        self.lock = threading.RLock()

    def _read(self) -> dict[str, object] | None:
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("weekly publish state is unreadable") from exc
        # 2026-10-19：抽签式下整周 0 条是合法计划（不能因为空 slots 判成损坏）
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or not isinstance(value.get("slots"), list)):
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

    @staticmethod
    def _slot_local(slot: dict) -> datetime | None:
        try:
            return datetime.fromisoformat(str(slot["at"])).astimezone(SHANGHAI)
        except (KeyError, ValueError):
            return None

    def _reconcile_special_slots(self, plan: dict, special_dates, special_times) -> bool:
        """保证"计划覆盖的那一周"里的每一个重要日子都有槽位；返回是否有改动。

        - 当天没有槽位 → 新增一个（用指定时刻或随机）；
        - 当天已有"待发"槽位、但你指定了别的时刻 → 把那个槽改到指定时刻；
        - 已 running / consumed 的槽位不动。
        """
        try:
            week = date.fromisoformat(str(plan.get("week_start")))
        except (KeyError, ValueError):
            return False
        times_map = special_times if isinstance(special_times, dict) else {}
        chooser = self.rng or random.SystemRandom()
        changed = False
        for day in (special_dates or []):
            if not isinstance(day, date) or not (week <= day <= week + timedelta(days=6)):
                continue
            wanted = _normalize_special_time(times_map.get(day.isoformat()))
            same_day = [s for s in plan.get("slots", []) if (self._slot_local(s) or datetime.min.replace(tzinfo=SHANGHAI)).date() == day]
            if not same_day:
                minute = wanted if wanted is not None else chooser.randrange(9 * 60, 22 * 60)
                local = datetime.combine(day, time(minute // 60, minute % 60), SHANGHAI)
                plan["slots"].append({"id": uuid.uuid4().hex, "at": local.isoformat(), "status": "pending"})
                changed = True
                continue
            if wanted is None:
                continue
            for slot in same_day:
                if slot.get("status") != "pending":
                    continue
                local = self._slot_local(slot)
                if local is None:
                    continue
                if local.strftime("%H:%M") != f"{wanted // 60:02d}:{wanted % 60:02d}":
                    slot["at"] = datetime.combine(day, time(wanted // 60, wanted % 60), SHANGHAI).isoformat()
                    changed = True
                break
        return changed

    def _migrate_to_probability(self, plan: dict, special_dates, probability: float) -> bool:
        """旧计划（每天都有槽位）→ 按概率抽一次签；只动"待发"槽位，返回是否有改动。

        2026-10-19 改版前生成的计划每天一个槽；升级后若不作废，仍会天天发。
        这里一次性给"未执行"的槽位抽签：特殊日子保留，普通日子按概率保留/跳过。
        抽完写入 `daily_probability` 标记，之后不再重抽（重启也不会反复改变计划）。
        """
        if "daily_probability" in plan:
            return False
        try:
            week = date.fromisoformat(str(plan.get("week_start")))
        except (KeyError, ValueError):
            return False
        specials = {day for day in (special_dates or []) if isinstance(day, date)}
        chooser = self.rng or random.SystemRandom()
        decided: set[date] = set()
        for slot in plan.get("slots", []):
            if not isinstance(slot, dict) or slot.get("status") != "pending":
                continue
            local = self._slot_local(slot)
            if local is None:
                continue
            day = local.date()
            if not (week <= day <= week + timedelta(days=6)):
                continue
            # 特殊日子 100% 保留（多出来的同日待发槽位也作废，保证一天一条）
            if day in specials and day not in decided:
                decided.add(day)
                continue
            if day in decided:
                slot["status"] = "consumed"
                slot["result"] = "probability_skip"
                continue
            decided.add(day)
            if chooser.random() >= probability:
                slot["status"] = "consumed"
                slot["result"] = "probability_skip"
        plan["daily_probability"] = probability
        return True

    def ensure_plan(self, now: datetime, *, special_dates=None, special_times=None) -> dict[str, object]:
        with self.lock:
            current = self._read()
            current_week = week_monday(now.astimezone(SHANGHAI).date())
            if current is not None:
                try:
                    saved_week = date.fromisoformat(str(current["week_start"]))
                except (KeyError, ValueError) as exc:
                    raise ValueError("weekly publish state has an invalid week") from exc
                if saved_week >= current_week:
                    # 旧版"每天一条"计划 → 先一次性按抽签概率瘦身
                    changed = self._migrate_to_probability(
                        current, special_dates, self.daily_probability
                    )
                    # 就地补齐缺失的重要日子槽位
                    if self._reconcile_special_slots(current, special_dates, special_times):
                        changed = True
                    if changed:
                        self._write(current)
                    return current
                # 旧计划已过期（覆盖的是更早的周）→ 补**当前这一周**，
                # 不再用 next_full_week（那会隔一周跳过，导致整周不发）。
                plan = make_week_plan(
                    current_week,
                    rng=self.rng,
                    special_dates=special_dates,
                    special_times=special_times,
                    daily_probability=self.daily_probability,
                )
                self._write(plan)
                return plan
            # 首次（无任何计划）：从下一个完整周开始，不补当前的部分周
            plan = make_week_plan(
                next_full_week(now),
                rng=self.rng,
                special_dates=special_dates,
                special_times=special_times,
                daily_probability=self.daily_probability,
            )
            self._write(plan)
            return plan

    def claim_due(self, now: datetime, *, grace: timedelta = GRACE, special_dates=None, special_times=None) -> dict[str, object] | None:
        """Consume missed slots, recover interrupted claims, or durably claim one due slot."""
        with self.lock:
            plan = self.ensure_plan(now, special_dates=special_dates, special_times=special_times)
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

