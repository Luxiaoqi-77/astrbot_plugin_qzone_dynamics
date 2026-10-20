"""Fake-provider checks for persona, comment context, memory and the length cap."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_qzone_dynamics import adapters
from astrbot_plugin_qzone_dynamics.comment_context import load_reply_context
from astrbot_plugin_qzone_dynamics.comment_transport import (
    CommentTransportBlocked,
    NapCatCookieStore,
)

UMO = "qq_napcat_100000002:FriendMessage:100000001"


def test_generation_includes_persona_memory_and_thread_with_hard_cap(monkeypatch):
    monkeypatch.setattr(
        adapters,
        "load_reply_context",
        AsyncMock(return_value=("测试人设", ["测试阅读记忆"])),
    )
    adapter = adapters.AstrBotTextGenerationAdapter(SimpleNamespace())
    adapter._complete = AsyncMock(return_value="一起看吗？")
    result = asyncio.run(
        adapter.generate_comment_reply(
            SimpleNamespace(content="看什么书"),
            "正在阅读",
            umo=UMO,
            thread_context="前面聊过书名",
        )
    )
    assert result == "一起看吗？"
    request = adapter._complete.call_args.kwargs
    # 先允许一个多余句号（去句号后再按 20 字硬校验）。
    assert request["max_chars"] == 21
    assert "测试人设" in request["system_prompt"]
    payload = json.loads(request["prompt"].split("\n", 1)[1])
    assert payload["relevant_star_map_memories"] == ["测试阅读记忆"]
    assert payload["comment_thread"] == "前面聊过书名"


def test_model_output_over_twenty_is_rejected(monkeypatch):
    monkeypatch.setattr(
        adapters, "load_reply_context", AsyncMock(return_value=("测试人设", []))
    )
    provider = SimpleNamespace(
        text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="好" * 21))
    )
    adapter = adapters.AstrBotTextGenerationAdapter(SimpleNamespace())
    adapter._candidate_providers = AsyncMock(return_value=[("fake", provider)])
    with pytest.raises(adapters.InvalidGeneratedDraft):
        asyncio.run(
            adapter.generate_comment_reply(
                SimpleNamespace(content="评论"), "正文", umo=UMO
            )
        )
    assert provider.text_chat.call_args.kwargs["contexts"] == []
    assert "tools" not in provider.text_chat.call_args.kwargs


def test_context_uses_active_persona_and_only_owner_memories(monkeypatch):
    from data.plugins.astrbot_plugin_ob_memory.core.models import MemoryBucket

    manager = SimpleNamespace(
        list_by_session=AsyncMock(
            return_value=[
                MemoryBucket(
                    id="relevant",
                    session_id="user:100000001",
                    content="喜欢阅读",
                    name="阅读",
                    tags=["阅读"],
                ),
                MemoryBucket(
                    id="other",
                    session_id="user:100000001",
                    content="喜欢骑车",
                    name="骑车",
                    tags=["骑车"],
                ),
            ]
        )
    )
    persona_manager = SimpleNamespace(
        resolve_selected_persona=AsyncMock(
            return_value=("chosen", {"prompt": "当前人设"}, None, False)
        )
    )
    context = SimpleNamespace(
        conversation_manager=SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="conversation"),
            get_conversation=AsyncMock(
                return_value=SimpleNamespace(persona_id="chosen")
            ),
        ),
        persona_manager=persona_manager,
        get_registered_star=lambda name: SimpleNamespace(
            star_cls=SimpleNamespace(manager=manager)
        ),
    )
    persona, memories = asyncio.run(load_reply_context(context, umo=UMO, query="阅读"))
    assert persona == "当前人设"
    assert memories == ["喜欢阅读"]
    manager.list_by_session.assert_awaited_once_with(
        "user:100000001", include_archived=False
    )
    assert (
        persona_manager.resolve_selected_persona.call_args.kwargs[
            "conversation_persona_id"
        ]
        == "chosen"
    )


class FakeNapCat:
    def __init__(self, account="100000002"):
        self.account = account
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append(action)
        if action == "get_login_info":
            return {"user_id": self.account}
        return {
            "cookies": "uin=o100000002; p_uin=o100000002; p_skey=offline-fake-token"
        }


def test_napcat_cookie_checks_login_before_cookie_read():
    client = FakeNapCat()
    cookie = asyncio.run(NapCatCookieStore("100000002", client).load_cookie())
    assert "p_skey=" in cookie
    assert client.calls == ["get_login_info", "get_cookies"]


def test_napcat_cookie_wrong_account_never_requests_cookie():
    client = FakeNapCat(account="100000003")
    with pytest.raises(
        CommentTransportBlocked, match="qzone_credential_account_mismatch"
    ):
        asyncio.run(NapCatCookieStore("100000002", client).load_cookie())
    assert client.calls == ["get_login_info"]


def test_comment_reply_strips_trailing_period(monkeypatch):
    monkeypatch.setattr(
        adapters, "load_reply_context", AsyncMock(return_value=("测试人设", []))
    )
    adapter = adapters.AstrBotTextGenerationAdapter(SimpleNamespace())
    adapter._complete = AsyncMock(return_value="一起看吗。")
    result = asyncio.run(
        adapter.generate_comment_reply(
            SimpleNamespace(content="看什么书"), "正在阅读", umo=UMO
        )
    )
    assert result == "一起看吗"
    request = adapter._complete.call_args.kwargs
    assert "不要用句号" in request["system_prompt"]
    assert "结尾不要句号" in request["prompt"]


def test_period_does_not_consume_the_character_budget(monkeypatch):
    monkeypatch.setattr(
        adapters, "load_reply_context", AsyncMock(return_value=("测试人设", []))
    )
    adapter = adapters.AstrBotTextGenerationAdapter(SimpleNamespace())
    adapter._complete = AsyncMock(return_value="好" * 20 + "。")
    result = asyncio.run(
        adapter.generate_comment_reply(
            SimpleNamespace(content="评论"), "正文", umo=UMO
        )
    )
    assert result == "好" * 20
