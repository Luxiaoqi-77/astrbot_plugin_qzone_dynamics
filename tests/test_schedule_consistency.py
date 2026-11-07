"""Offline checks of shared schedule constraints in both publication paths."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from astrbot_plugin_moment.core import material as mm
from astrbot_plugin_moment.core.post_engine import PostEngine
from astrbot_plugin_qzone_dynamics import main as qm
from astrbot_plugin_qzone_dynamics.adapters import (
    TEXT_SYSTEM_PROMPT,
    build_labeled_source_payload,
)
from astrbot_plugin_qzone_dynamics.core import source_fingerprint
from astrbot_plugin_qzone_dynamics.source_adapter import VirtualLifeSourceAdapter

from data.plugins.social_material_pool import MaterialSource
from data.plugins.social_material_pool.schedule_context import (
    SCHEDULE_RULES,
    read_daily_schedule_context,
)

NOW = datetime(2026, 10, 22, 18, 21, tzinfo=ZoneInfo("Asia/Shanghai"))


def setup_context():
    plan = NS(
        date="2026-10-22",
        persona_id="角色甲",
        status="ok",
        has_schedule=True,
        timeline=(
            NS(start="16:10", end="18:20", activity="海滨散步", location="海滨步道"),
            NS(
                start="18:20",
                end="19:20",
                activity="吃砂锅海鲜粥与素蒸饺晚餐",
                location="老渔家砂锅铺",
            ),
            NS(start="19:20", end="24:00", activity="回家玩解谜游戏", location="公寓"),
        ),
    )
    plugin = NS(storage=NS(get_plan=lambda *args: plan), config={}, timezone=NOW.tzinfo)
    context = NS(
        get_registered_star=lambda name: (
            NS(star_cls=plugin) if name == "astrbot_plugin_virtual_life" else None
        ),
        persona_manager=NS(get_default_persona_v3=lambda: {"name": "角色甲"}),
    )
    return context, plan


def test_facts_and_time():
    context, _ = setup_context()
    text = asyncio.run(read_daily_schedule_context(context, now=NOW))
    assert "[已结束] 海滨散步" in text
    assert "[进行中] 吃砂锅海鲜粥与素蒸饺晚餐；地点：老渔家砂锅铺" in text
    assert "[未开始] 回家玩解谜游戏" in text


@pytest.mark.parametrize(
    "field,value",
    [
        ("date", "2026-10-21"),
        ("persona_id", "角色乙"),
        ("status", "failed"),
        ("has_schedule", False),
    ],
)
def test_invalid_plan(field, value):
    context, plan = setup_context()
    setattr(plan, field, value)
    text = asyncio.run(read_daily_schedule_context(context, now=NOW))
    assert "日程不可用" in text and "海鲜粥" not in text


def test_absent_plugin():
    assert "日程不可用" in asyncio.run(
        read_daily_schedule_context(NS(get_registered_star=lambda _: None), now=NOW)
    )


def test_async_storage_and_persona():
    context, plan = setup_context()
    plugin = context.get_registered_star("astrbot_plugin_virtual_life").star_cls
    plugin.storage.get_plan = AsyncMock(return_value=plan)
    asyncio.run(read_daily_schedule_context(context, now=NOW))
    plugin.storage.get_plan.assert_awaited_once_with("2026-10-22", "角色甲")


def test_private_activity_filtered():
    context, plan = setup_context()
    plan.timeline += (
        NS(start="13:00", end="14:00", activity="去医院看医生", location="医院"),
    )
    text = asyncio.run(read_daily_schedule_context(context, now=NOW))
    assert "医院" not in text and "海鲜粥" in text


def test_moment_facts_without_primary_material(monkeypatch):
    context, _ = setup_context()
    collector = object.__new__(mm.MaterialCollector)
    collector.context, collector.config = context, {"persona_id": "角色甲"}
    collector.source_ledger = NS(reserve=lambda *args: None)
    collector._get_persona = AsyncMock(return_value="角色甲")
    collector._important_date_lines = lambda *args: []
    monkeypatch.setattr(mm, "collect_material_sources", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        mm, "collect_optional_moment_health", AsyncMock(return_value=None)
    )
    reader = AsyncMock(return_value="18:20–19:20 [进行中] 吃海鲜粥；地点：砂锅铺")
    monkeypatch.setattr(mm, "read_daily_schedule_context", reader)
    prompt = PostEngine(context, {})._build_prompt(asyncio.run(collector.collect()))
    assert "海鲜粥" in prompt and "砂锅铺" in prompt and SCHEDULE_RULES in prompt
    reader.assert_awaited_once_with(context, persona_id="角色甲")


@pytest.mark.parametrize("selected", [False, True])
def test_qzone_selection_and_fingerprint(monkeypatch, selected):
    context, _ = setup_context()
    adapter = VirtualLifeSourceAdapter(
        context,
        persona_id="角色甲",
        public_character_name="角色甲",
        public_character_description="虚构角色",
        clock=lambda: NOW,
    )
    source = asyncio.run(adapter.read_today())
    assert source is not None
    plugin = object.__new__(qm.QzoneDynamicsPlugin)
    plugin.context, plugin.config, plugin.store = context, {}, NS(list=lambda: [])
    material = MaterialSource("random_topic", "digest", "阅读", "阅读话题")
    plugin.source_ledger = NS(
        reserve=lambda *args: (material, "token") if selected else None
    )
    monkeypatch.setattr(
        qm, "collect_material_sources", AsyncMock(return_value=[material])
    )
    result = asyncio.run(plugin._reserve_primary_source(source))
    text = json.dumps(
        json.loads(build_labeled_source_payload(result)), ensure_ascii=False
    )
    assert "砂锅海鲜粥与素蒸饺" in text and "老渔家砂锅铺" in text
    assert (
        result.activity == ""
        and result.daily_schedule_context == source.daily_schedule_context
    )
    changed = replace(
        result,
        daily_schedule_context=source.daily_schedule_context.replace(
            "海鲜粥", "蔬菜粥"
        ),
    )
    assert source_fingerprint(changed) != source_fingerprint(result)


def test_incidental_encounters_allowed():
    assert "看到小野猫" in SCHEDULE_RULES and "偶遇路人" in SCHEDULE_RULES
    assert "三餐的菜品与用餐地点" in SCHEDULE_RULES
    assert SCHEDULE_RULES in TEXT_SYSTEM_PROMPT
    assert "不得虚构具体动作、场景" not in TEXT_SYSTEM_PROMPT
