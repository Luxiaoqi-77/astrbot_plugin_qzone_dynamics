"""Read-only adapters for VirtualLife and an optional public mood-state API."""

from __future__ import annotations

import inspect
import math
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import SafeDraftInput, clean_source_text

VIRTUAL_LIFE_PLUGIN_ID = "astrbot_plugin_virtual_life"
MOOD_STATE_PLUGIN_ID = "astrbot_plugin_mood_state"
MOOD_STATE_CALLER_PLUGIN_ID = "astrbot_plugin_qzone_dynamics"
MOOD_MAX_AGE = timedelta(hours=24)
MOOD_FUTURE_CLOCK_SKEW = timedelta(minutes=5)
MAX_PUBLIC_CHARACTER_NAME = 80
MAX_PUBLIC_CHARACTER_DESCRIPTION = 400
MAX_SCHEDULE_ACTIVITY = 180
RECENT_ACTIVITY_WINDOW_MINUTES = 90


def _minute_of_day(value: object) -> int | None:
    """Parse a VirtualLife HH:MM boundary, including its 24:00 end marker."""
    if value == "24:00":
        return 24 * 60
    if not isinstance(value, str):
        return None
    try:
        hour_text, minute_text = value.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (ValueError, TypeError):
        return None
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        return None
    if f"{hour:02d}:{minute:02d}" != value:
        return None
    return hour * 60 + minute


def _field(value: object, key: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


class VirtualLifeSourceAdapter:
    """Read today's valid VirtualLife plan without accessing chat sessions.

    Args:
        context: AstrBot plugin context, or a test double with the same read APIs.
        persona_id: Optional explicit persona ID; otherwise use AstrBot's global default.
        public_character_name: Independent public role name configured for this plugin.
        public_character_description: Independent public role description configured here.
        include_mood: Whether to query an optional public mood-state reader.
        mood_state_reader: Optional injected read-only interface for tests or hosting.
        clock: Injectable UTC-aware clock used to determine today's date.
    """

    def __init__(
        self,
        context: object,
        *,
        persona_id: str = "",
        public_character_name: str = "",
        public_character_description: str = "",
        include_mood: bool = False,
        mood_state_reader: object | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.context = context
        self.persona_id = str(persona_id or "").strip()
        self.public_character_name = public_character_name
        self.public_character_description = public_character_description
        self.include_mood = include_mood
        self.mood_state_reader = mood_state_reader
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _timezone(self, virtual_life: object | None = None) -> ZoneInfo:
        # VirtualLife resolves this timezone once at startup and uses it to key
        # DailyPlan records. Prefer that exact runtime value so the lookup day
        # cannot drift across midnight relative to the source plan.
        runtime_timezone = getattr(virtual_life, "timezone", None)
        if isinstance(runtime_timezone, ZoneInfo):
            return runtime_timezone
        runtime_timezone_key = getattr(runtime_timezone, "key", None)
        if isinstance(runtime_timezone_key, str) and runtime_timezone_key:
            try:
                return ZoneInfo(runtime_timezone_key)
            except ZoneInfoNotFoundError:
                pass
        getter = getattr(self.context, "get_config", None)
        try:
            config = getter() if callable(getter) else {}
            timezone_name = str(config.get("timezone") or "Asia/Shanghai")
            return ZoneInfo(timezone_name)
        except Exception:
            # This matches VirtualLife's own _resolve_timezone fallback.
            return ZoneInfo("Asia/Shanghai")

    def _now_utc(self) -> datetime:
        current = self.clock()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc)

    def _today(
        self, virtual_life: object | None = None, *, current: datetime | None = None
    ) -> str:
        now = current or self._now_utc()
        return now.astimezone(self._timezone(virtual_life)).date().isoformat()

    def _registered_instance(self, plugin_id: str) -> object | None:
        getter = getattr(self.context, "get_registered_star", None)
        if not callable(getter):
            return None
        try:
            metadata = getter(plugin_id)
        except Exception:
            return None
        if not metadata or not getattr(metadata, "activated", False):
            return None
        return getattr(metadata, "star_cls", None)

    async def _persona_id(self) -> str | None:
        if self.persona_id:
            return self.persona_id
        manager = getattr(self.context, "persona_manager", None)
        getter = getattr(manager, "get_default_persona_v3", None)
        if not callable(getter):
            return None
        try:
            result = getter()
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            return None
        name = _field(result, "name", "")
        return str(name).strip() if isinstance(name, str) and name.strip() else None

    async def _public_mood(
        self,
        persona_id: str,
        session_origin: str | None,
        current: datetime,
    ) -> tuple[str, float, str] | None:
        if not isinstance(session_origin, str) or not session_origin.strip():
            return None
        reader = self.mood_state_reader
        if reader is None:
            reader = self._registered_instance(MOOD_STATE_PLUGIN_ID)
        method = getattr(reader, "read_fictional_mood_v1", None)
        if not callable(method):
            return None
        try:
            result = method(
                caller_plugin_id=MOOD_STATE_CALLER_PLUGIN_ID,
                persona_id=persona_id,
                session_origin=session_origin,
            )
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, Mapping):
                return None
            allowed_keys = {
                "schema_version", "state_kind", "is_fictional", "label",
                "intensity", "updated_at",
            }
            if set(result) - allowed_keys:
                return None
            if (
                type(result.get("schema_version")) is not int
                or result.get("schema_version") != 1
                or result.get("state_kind") != "fictional_role_mood"
                or result.get("is_fictional") is not True
            ):
                return None
            label = result.get("label")
            intensity = result.get("intensity")
            updated_at = result.get("updated_at")
            safe_label = clean_source_text(
                label, "public mood label", required=True, max_chars=40
            )
            if (
                not isinstance(intensity, (int, float))
                or isinstance(intensity, bool)
                or not math.isfinite(float(intensity))
                or not 0 <= float(intensity) <= 0.4
            ):
                return None
            safe_time = clean_source_text(
                updated_at, "public mood updated_at", required=True, max_chars=64
            )
            normalized_time = datetime.fromisoformat(
                safe_time.replace("Z", "+00:00")
            )
            if normalized_time.tzinfo is None:
                return None
            age = current - normalized_time.astimezone(timezone.utc)
            if age < -MOOD_FUTURE_CLOCK_SKEW or age > MOOD_MAX_AGE:
                return None
            return safe_label, float(intensity), safe_time
        except Exception:
            return None

    async def read_today(
        self, *, session_origin: str | None = None
    ) -> SafeDraftInput | None:
        """Return a sanitized snapshot or None when today's public sources are absent."""
        virtual_life = self._registered_instance(VIRTUAL_LIFE_PLUGIN_ID)
        storage = getattr(virtual_life, "storage", None)
        get_plan = getattr(storage, "get_plan", None)
        persona_id = await self._persona_id()
        if not persona_id:
            return None
        try:
            public_character_name = clean_source_text(
                self.public_character_name,
                "independent public character name",
                required=True,
                max_chars=MAX_PUBLIC_CHARACTER_NAME,
            )
            public_character_description = clean_source_text(
                self.public_character_description,
                "independent public character description",
                required=True,
                max_chars=MAX_PUBLIC_CHARACTER_DESCRIPTION,
            )
        except ValueError:
            return None
        current = self._now_utc()
        local_now = current.astimezone(self._timezone(virtual_life))
        source_date = local_now.date().isoformat()
        plan = None
        if callable(get_plan):
            try:
                plan = get_plan(source_date, persona_id)
                if inspect.isawaitable(plan):
                    plan = await plan
            except Exception:
                plan = None
        if (
            plan is None
            or _field(plan, "status", "") != "ok"
            or _field(plan, "date", "") != source_date
            or _field(plan, "persona_id", "") != persona_id
            or _field(plan, "has_schedule", None) is False
        ):
            timeline = ()
        else:
            timeline = _field(plan, "timeline", ())

        current_minute = local_now.hour * 60 + local_now.minute
        current_item = None
        recent_item = None
        recent_end = -1
        for item in timeline:
            start = _minute_of_day(_field(item, "start", ""))
            end = _minute_of_day(_field(item, "end", ""))
            state = _field(item, "state", "available")
            if start is None or end is None or start >= end or state == "sleep":
                continue
            try:
                activity = clean_source_text(
                    _field(item, "activity", ""),
                    "schedule activity",
                    max_chars=MAX_SCHEDULE_ACTIVITY,
                )
            except ValueError:
                continue
            if not activity:
                continue
            if start <= current_minute < end:
                current_item = (activity, state, start, end)
                break
            if (
                end <= current_minute
                and current_minute - end <= RECENT_ACTIVITY_WINDOW_MINUTES
                and end > recent_end
            ):
                recent_item = (activity, state, start, end)
                recent_end = end
        selected_item = current_item or recent_item
        if selected_item is None:
            activity, activity_state, activity_status, activity_interval = "", "", "", ""
        else:
            activity, activity_state, start_minute, end_minute = selected_item
            activity_status = "正在进行" if current_item else "刚刚结束"
            activity_interval = f"{start_minute // 60:02d}:{start_minute % 60:02d}–"
            activity_interval += (
                "24:00"
                if end_minute == 24 * 60
                else f"{end_minute // 60:02d}:{end_minute % 60:02d}"
            )

        mood = (
            await self._public_mood(persona_id, session_origin, current)
            # The precise session origin is sent only to task-14's allowlisted
            # same-process API. It is never persisted or included in the prompt.
            if self.include_mood
            else None
        )
        mood_label, mood_intensity, mood_time = mood or ("", None, "")
        source_labels = (
            "独立插件配置：公开角色名",
            "独立插件配置：公开角色描述",
        )
        if activity:
            source_labels += ("VirtualLife：当前或刚结束的虚构日程活动",)
        if mood:
            source_labels += ("task-14 read_fictional_mood_v1：label/intensity/updated_at",)
        # 2026-10-16：当天命中的"重要日子"（qzone 渠道）——只在生成提示词里注入，不改发布规则。
        important_dates = ()
        try:
            try:
                from data.plugins.important_dates import today_lines
            except ImportError:  # pragma: no cover
                from important_dates import today_lines  # type: ignore
            important_dates = tuple(today_lines("qzone"))
        except Exception:
            important_dates = ()

        return SafeDraftInput(
            persona_id=persona_id,
            source_date=source_date,
            persona_label=public_character_name,
            activity=activity,
            activity_status=activity_status,
            activity_state=str(activity_state),
            activity_interval=activity_interval,
            local_time=local_now.strftime("%H:%M"),
            persona_public_note=public_character_description,
            mood_label=mood_label,
            mood_intensity=mood_intensity,
            mood_time=mood_time,
            source_labels=source_labels,
            important_dates=important_dates,
        )
